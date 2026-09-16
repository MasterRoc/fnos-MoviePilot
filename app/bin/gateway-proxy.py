#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MoviePilot fnOS 统一网关反向代理
=================================
作用：把 fnOS 网关转发的 Unix Socket 流量，桥接到 MoviePilot 前端服务端口(默认 3000)，
并完成：
  - 剥离网关前缀 /app/moviepilot
  - 注入 JS polyfill（fetch/XHR/WebSocket 路径改写 + 反逃生）
  - WebSocket 原始 TCP 双向透传
  - 移除 X-Frame-Options、追加允许 iframe 的 CSP
  - 静态资源 LRU 缓存 + 后端连接池

用法：
    python3 gateway-proxy.py <socket_path> <target_host> <port>

依赖：仅 Python 标准库。
"""
import http.server
import socket
import sys
import os
import signal
import re
import time
import threading
import gzip
import zlib
import select
import logging
from http.client import HTTPConnection
from collections import OrderedDict

# ---------------------------------------------------------------------------
# 配置（与 manifest / app/ui/config 保持一致）
# ---------------------------------------------------------------------------
APPNAME = "moviepilot"
GATEWAY_PREFIX = "/app/moviepilot"

STATIC_EXTENSIONS = frozenset({
    'js', 'css', 'png', 'jpg', 'jpeg', 'gif', 'svg', 'ico', 'woff', 'woff2',
    'ttf', 'eot', 'webp', 'mp4', 'webm',
})

# 懒加载 queue 模块（保持启动快）
_queue = None
def _get_queue():
    global _queue
    if _queue is None:
        import queue
        _queue = queue
    return _queue

# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stderr,
)

_RE_HTML_ATTR = re.compile(rb'(src|href|action)=([\'"])/(?!/?(?:app|cgi)/)')
_RE_REFERER = re.compile(r'^https?://[^/]+')

# ---------------------------------------------------------------------------
# JS polyfill 模板（注入到 HTML <head>）
# ---------------------------------------------------------------------------
def _build_polyfill():
    P = GATEWAY_PREFIX
    return (
        '<script>'
        '(function(){'
        'var P="%s";'
        'var _f=window.fetch;'
        'window.fetch=function(u,o){'
        'if(typeof u==="string"&&u.charAt(0)==="/"&&!u.startsWith(P)){u=P+u;}'
        'return _f.call(this,u,o);};'
        'var _o=XMLHttpRequest.prototype.open;'
        'XMLHttpRequest.prototype.open=function(m,u,s){'
        'if(typeof u==="string"&&u.charAt(0)==="/"&&!u.startsWith(P)){arguments[1]=P+u;}'
        'return _o.apply(this,arguments);};'
        'var _cw=window.WebSocket;'
        'if(_cw){'
        'window.WebSocket=function(u,p){'
        'if(typeof u==="string"&&u.charAt(0)==="/"&&!u.startsWith(P)){'
        'var _proto=location.protocol==="https:"?"wss:":"ws:";'
        'u=_proto+"//"+location.host+P+u;}'
        'return p?new _cw(u,p):new _cw(u);};'
        'window.WebSocket.prototype=_cw.prototype;}'
        'var _ws=window.WebSocket;'
        'if(_ws&&_ws.prototype){'
        'var _oOpen=_ws.prototype.open;'
        'if(!_oOpen){'
        'try{'
        'var _orig=_ws;'
        'Object.defineProperty(window,"WebSocket",{get:function(){return _wrap;}});'
        'function _wrap(u,p){'
        'if(typeof u==="string"&&u.charAt(0)==="/"&&!u.startsWith(P)){'
        'var _proto2=location.protocol==="https:"?"wss:":"ws:";'
        'u=_proto2+"//"+location.host+P+u;}'
        'return new _orig(u,p);}'
        '_wrap.prototype=_orig.prototype;'
        '}catch(e){}}'
        '}'
        '})();'
        '</script>' % P
    )

_POLYFILL_BYTES = _build_polyfill().encode()

# ---------------------------------------------------------------------------
# 命令行参数
# ---------------------------------------------------------------------------
if len(sys.argv) < 4:
    logging.error("Usage: gateway-proxy.py <socket_path> <target_host> <port>")
    sys.exit(1)
SOCK_PATH = sys.argv[1]
TARGET_HOST = sys.argv[2]
INITIAL_PORT = int(sys.argv[3])

# ---------------------------------------------------------------------------
# 连接池
# ---------------------------------------------------------------------------
class PooledHTTPConnection(HTTPConnection):
    """带 TCP keepalive 的后端连接。

    对端（Node 前端/后端服务）闲置超时后会静默关闭连接，而本代理池可能
    复用到这种"半死"连接，导致首个请求（如前端 system/ping 健康检查）失败，
    触发 MoviePilot 前端"正在重新连接"提示。TCP keepalive 让 OS 更快发现
    对端异常关闭；配合连接池空闲超时（idle_timeout）主动丢弃长期闲置连接，
    显著降低复用死连接的几率。
    """

    def connect(self):
        super().connect()
        try:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            try:
                # Linux: 空闲 15s 后每 5s 探测一次，3 次无响应判定连接死亡
                self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 15)
                self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 5)
                self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)
            except (AttributeError, OSError):
                pass  # 非 Linux 或系统不支持，忽略
        except OSError:
            pass


class ConnectionPool:
    def __init__(self, host, port, maxsize=10, idle_timeout=20, timeout=60):
        self._host, self._port, self._timeout = host, port, timeout
        self._idle_timeout = idle_timeout
        self._pool = _get_queue().Queue(maxsize)
        self._released_at = {}  # conn -> 最近释放(入库)时间(time.monotonic)

    @staticmethod
    def _is_sock_alive(sock):
        # getpeername() 无法检测对端已关闭的连接（FIN 后仍处于 CLOSE_WAIT）。
        # 用非阻塞可读性检查：可读说明有数据/FIN/RST，此时 peek 0 字节即已死亡。
        try:
            r, _, _ = select.select([sock], [], [], 0)
            if r:
                try:
                    return len(sock.recv(1, socket.MSG_PEEK)) > 0
                except (BlockingIOError, OSError):
                    return False
            return True
        except (OSError, ValueError):
            return False

    def _new_conn(self):
        return PooledHTTPConnection(self._host, self._port, timeout=self._timeout)

    def acquire(self):
        q = _get_queue()
        now = time.monotonic()
        while True:
            try:
                conn = self._pool.get_nowait()
            except q.Empty:
                break
            released_at = self._released_at.pop(conn, None)
            # 空闲超过 idle_timeout 的连接直接丢弃：
            # 对端（Node keepAliveTimeout=60s、uvicorn 等默认仅数秒）可能已
            # 静默关闭，与其在请求时踩雷再重试，不如主动重建，让首个请求
            # （如 system/ping）始终命中健康连接。
            if released_at is not None and now - released_at > self._idle_timeout:
                conn.close()
                continue
            if conn.sock is not None and self._is_sock_alive(conn.sock):
                return conn
            conn.close()
            break  # 只丢弃一条坏连接即可，其余留待后续使用
        return self._new_conn()

    def release(self, conn):
        q = _get_queue()
        if conn.sock is None:
            # 连接已被关闭（如请求失败后 close 过），不再放回池中复用
            conn.close()
            self._released_at.pop(conn, None)
            return
        self._released_at[conn] = time.monotonic()
        try:
            self._pool.put_nowait(conn)
        except q.Full:
            conn.close()
            self._released_at.pop(conn, None)

    def close_all(self):
        q = _get_queue()
        while True:
            try:
                conn = self._pool.get_nowait()
                self._released_at.pop(conn, None)
                conn.close()
            except q.Empty:
                break
        self._released_at.clear()

# ---------------------------------------------------------------------------
# 静态缓存（LRU）
# ---------------------------------------------------------------------------
class StaticCache:
    def __init__(self, max_size=50):
        self._cache = OrderedDict()
        self._max = max_size
        self._lock = threading.Lock()
    def get(self, key):
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                return self._cache[key]
            return None
    def set(self, key, status, headers, body):
        with self._lock:
            if len(self._cache) >= self._max:
                self._cache.popitem(last=False)
            self._cache[key] = (status, headers, body)

_static_cache = StaticCache()

# ---------------------------------------------------------------------------
# 反向代理请求处理器
# ---------------------------------------------------------------------------
_REMOVE_HEADERS = frozenset({"x-frame-options"})

class ProxyHandler(http.server.BaseHTTPRequestHandler):
    _conn_pool = None
    protocol_version = "HTTP/1.1"

    def _strip_prefix(self):
        """剥离网关前缀，保留 query string。

        必须按路径段边界匹配：裸 startswith 会把 /app/moviepilotXYZ 也剥成
        "XYZ"（无前导斜杠），拼出非法的请求行。
        """
        path = self.path
        if path == GATEWAY_PREFIX:
            return "/"
        if path.startswith(GATEWAY_PREFIX + "/"):
            return path[len(GATEWAY_PREFIX):]
        return path

    def _read_chunked_body(self):
        """读取 chunked 编码的请求体（解码为原始字节）。

        BaseHTTPRequestHandler 不会自动解码 chunked body，若直接走 Content-Length
        分支会得到 None，POST body 丢失导致后端 422。这里按 RFC 7230 逐块解析：
        每个 chunk 先是一行十六进制长度，随后是该长度的数据 + CRLF，直到长度 0。
        """
        chunks = []
        try:
            while True:
                size_line = self.rfile.readline(65536)
                if not size_line:
                    break
                size_hex = size_line.split(b";", 1)[0].strip()
                if not size_hex:
                    continue
                try:
                    size = int(size_hex, 16)
                except ValueError:
                    break
                if size == 0:
                    # 读取 chunked 结尾的 trailer 直到空行
                    while True:
                        trailer = self.rfile.readline(65536)
                        if not trailer or trailer in (b"\r\n", b"\n"):
                            break
                    break
                chunk = self.rfile.read(size)
                if not chunk:
                    break
                chunks.append(chunk)
                self.rfile.read(2)  # 吃掉 chunk 后的 CRLF
        except (OSError, ValueError):
            return None
        return b"".join(chunks)

    def _rewrite_html(self, data):
        data = data.replace(b'</head>', _POLYFILL_BYTES + b'</head>', 1)
        data = _RE_HTML_ATTR.sub(rb'\1=\2' + GATEWAY_PREFIX.encode() + rb'/', data)
        return data

    def do_request(self):
        # 避免 HTTP/1.1 长连接在 BaseHTTPRequestHandler 中的 keep-alive 问题
        self.close_connection = True

        if self.path == GATEWAY_PREFIX:
            self.send_response(301)
            self.send_header("Location", GATEWAY_PREFIX + "/")
            self.send_header("Connection", "close")
            self.end_headers()
            return

        # WebSocket 升级
        if self.headers.get("Upgrade", "").lower() == "websocket":
            self._handle_ws()
            return

        path = self._strip_prefix()

        is_head = self.command == "HEAD"
        port = INITIAL_PORT
        pool = ProxyHandler._conn_pool
        conn = pool.acquire() if pool else PooledHTTPConnection(TARGET_HOST, port, timeout=60)

        # 构造转发头
        headers = {}
        skip = frozenset({"host", "connection", "transfer-encoding",
                          "accept-encoding", "origin", "referer", "accept"})
        for k, v in self.headers.items():
            if k.lower() not in skip:
                headers[k] = v
        backend_url = "http://%s:%d" % (TARGET_HOST, port)
        headers["Host"] = "%s:%d" % (TARGET_HOST, port)
        headers["Accept-Encoding"] = "gzip, deflate"
        headers["Accept"] = "*/*"
        referer = self.headers.get("Referer", "")
        if referer:
            headers["Referer"] = _RE_REFERER.sub(backend_url, referer)

        body = None
        te = (self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in te:
            # 浏览器/axios 对较大请求体可能用 chunked 编码。若按 Content-Length 读取
            # 会得到 None，转发时丢失整个 POST body，导致后端收到空 body 返回 422。
            body = self._read_chunked_body()
        else:
            clen = self.headers.get("Content-Length")
            if clen:
                try:
                    body = self.rfile.read(int(clen))
                except (ValueError, OSError):
                    body = None

        # 静态缓存命中
        cache_key = self.command + ":" + path
        cacheable = self.command == "GET" and path.rfind('.') > 0 and \
            path[path.rfind('.') + 1:].lower() in STATIC_EXTENSIONS
        if cacheable:
            cached = _static_cache.get(cache_key)
            if cached:
                c_status, c_headers, c_body = cached
                try:
                    self.send_response(c_status)
                    for k, v in c_headers:
                        self.send_header(k, v)
                    self.send_header("Content-Length", str(len(c_body)))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    if not is_head:
                        self.wfile.write(c_body)
                finally:
                    if pool:
                        pool.release(conn)
                    else:
                        conn.close()
                return

        try:
            conn.request(self.command, path, body, headers)
            resp = conn.getresponse()
        except Exception as e:
            logging.warning("request failed, retry fresh: %s -> %s", self.command, e)
            conn.close()
            fresh = PooledHTTPConnection(TARGET_HOST, port, timeout=60)
            try:
                fresh.request(self.command, path, body, headers)
                resp = fresh.getresponse()
                conn = fresh
            except Exception as e2:
                self.send_error(502, str(e2))
                fresh.close()
                return

        all_headers = resp.getheaders()
        content_type = next(
            (v for k, v in all_headers if k.lower() == "content-type"), "")
        is_html = "text/html" in content_type
        # SSE 实时日志等流式响应：必须逐块透传，不能 resp.read() 等流结束，
        # 否则 MoviePilot 系统日志（text/event-stream 长连接）永远收不到数据而显示空白。
        is_streaming = content_type.startswith("text/event-stream")
        content_encoding = next(
            (v for k, v in all_headers if k.lower() == "content-encoding"), None)

        try:
            self.send_response(resp.status)
            for k, v in all_headers:
                kl = k.lower()
                if kl in _REMOVE_HEADERS:
                    continue
                if kl == "set-cookie":
                    self.send_header(k, v)
                    continue
                if kl == "content-encoding" and is_html:
                    continue
                if kl in ("transfer-encoding", "connection", "content-length"):
                    continue
                self.send_header(k, v)

            if is_streaming:
                # 流式透传：不发 Content-Length，保持长连接逐块转发，
                # 直到后端流结束或浏览器断开。
                # 必须用 read1() 而非 read()：对裸流 SSE（Connection: close + 非 chunked，
                # 无 Content-Length），read(amt) 会阻塞等待填满 amt 字节，导致后端推送的
                # 小块日志永远无法返回给浏览器（系统日志空白）。read1() 只返回当前可用数据。
                self.send_header("Connection", "close")
                self.end_headers()
                if not is_head:
                    try:
                        while True:
                            chunk = resp.read1(65536)
                            if not chunk:
                                break
                            try:
                                self.wfile.write(chunk)
                                self.wfile.flush()
                            except (BrokenPipeError, ConnectionResetError):
                                break  # 浏览器提前断开
                    except Exception as e:
                        logging.debug("stream read error: %s", e)
                return

            data = resp.read()
            if is_html:
                if content_encoding == "gzip":
                    data = gzip.decompress(data)
                elif content_encoding == "deflate":
                    data = zlib.decompress(data)
                data = self._rewrite_html(data)
                self.send_header("Content-Security-Policy",
                    "default-src 'self' 'unsafe-inline' 'unsafe-eval' data: blob:; "
                    "frame-ancestors *; img-src 'self' data: blob: https:; "
                    "font-src 'self' data: https://fonts.gstatic.com; "
                    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
                    "connect-src 'self' https: http://127.0.0.1:*/api/ ws: wss:;")
            elif cacheable and 200 <= resp.status < 300:
                ch = [(k, v) for k, v in all_headers
                      if k.lower() not in ("transfer-encoding", "connection",
                                           "content-length", "set-cookie")]
                _static_cache.set(cache_key, resp.status, ch, data)

            self.send_header("Content-Length", str(len(data)))
            self.send_header("Connection", "close")
            self.end_headers()
            if not is_head:
                self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            # 客户端（浏览器）提前断开连接：刷新/取消请求时常见，属正常现象，
            # 静默忽略，避免向日志刷 traceback。
            pass
        except Exception as e:
            logging.warning("response write error: %s", e)
        finally:
            if is_streaming:
                # SSE 长连接结束后对端连接已关闭/处于流式未完成态，直接关闭，
                # 不要放回连接池复用，避免污染复用连接的读写状态。
                try:
                    conn.close()
                except Exception:
                    pass
            elif pool:
                pool.release(conn)
            else:
                conn.close()

    # WS 握手头部：下列头由代理显式构造或按规范不得透传
    _WS_SKIP_HEADERS = frozenset({
        "host", "connection", "upgrade", "origin",
        "sec-websocket-key", "sec-websocket-version",
        "sec-websocket-protocol", "sec-websocket-extensions",
        "content-length", "transfer-encoding",
    })

    def _handle_ws(self):
        path = self._strip_prefix()
        port = INITIAL_PORT
        backend = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        backend.settimeout(10)
        try:
            backend.connect((TARGET_HOST, port))
        except Exception as e:
            backend.close()
            self.send_error(502, str(e))
            return

        ws_key = self.headers.get("Sec-WebSocket-Key", "")
        ws_ver = self.headers.get("Sec-WebSocket-Version", "13")
        extra = self.headers.get("Sec-WebSocket-Protocol", "")

        # 透传客户端其余头部。早期实现只发握手必需的 4 个头，Cookie/Authorization
        # 全部丢失，任何需要登录态的 WebSocket 都会被后端拒绝（401/403）。
        passthrough = []
        for k, v in self.headers.items():
            if k.lower() not in self._WS_SKIP_HEADERS:
                passthrough.append("%s: %s\r\n" % (k, v))

        req_line = (
            "GET %s HTTP/1.1\r\n"
            "Host: %s:%d\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "%s"
            "Sec-WebSocket-Version: %s\r\n"
            "%s"
            "%s"
            "Origin: http://%s:%d\r\n\r\n"
        ) % (path, TARGET_HOST, port,
             ("Sec-WebSocket-Key: %s\r\n" % ws_key) if ws_key else "",
             ws_ver,
             ("Sec-WebSocket-Protocol: %s\r\n" % extra) if extra else "",
             "".join(passthrough),
             TARGET_HOST, port)
        try:
            backend.sendall(req_line.encode())
        except Exception as e:
            backend.close()
            self.send_error(502, str(e))
            return

        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = backend.recv(4096)
            if not chunk:
                backend.close()
                self.send_error(502, "backend closed")
                return
            resp += chunk
        hdr_end = resp.index(b"\r\n\r\n")
        hdr_raw = resp[:hdr_end].decode("utf-8", errors="replace")
        remaining = resp[hdr_end + 4:]
        try:
            status_code = int(hdr_raw.split(" ", 2)[1])
        except Exception:
            status_code = 502

        try:
            self.send_response(status_code)
            for line in hdr_raw.split("\r\n")[1:]:
                if ":" in line:
                    k, v = line.split(":", 1)
                    self.send_header(k.strip(), v.strip())
            self.end_headers()
            if remaining:
                self.wfile.write(remaining)
                self.wfile.flush()
        except Exception as e:
            logging.warning("ws response write failed: %s", e)
            backend.close()
            return

        client = self.connection
        backend.setblocking(True)
        client.setblocking(True)
        # 两端开启 TCP keepalive：WebSocket 长时间无业务消息是常态，靠应用层超时
        # 判断存活不可靠，交给内核探测半开连接（60s 空闲 + 20s×3 探测）。
        for s in (client, backend):
            try:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                s.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 60)
                s.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 20)
                s.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)
            except (AttributeError, OSError):
                pass  # 非 Linux 或系统不支持，忽略

        def tunnel(a, b):
            try:
                while True:
                    # 超时只用于周期性唤醒，不能因"空闲"就关闭连接：WebSocket 静默
                    # 数分钟是正常状态，早期实现空闲 30s 即 return，会把正常连接
                    # 反复踢断。真正的断线由 recv 返回空、sendall 抛错或 TCP
                    # keepalive 判定。
                    r, _, _ = select.select([a, b], [], [], 30)
                    if not r:
                        continue
                    for s in r:
                        data = s.recv(65536)
                        if not data:
                            return
                        if s is a:
                            b.sendall(data)
                        else:
                            a.sendall(data)
            except Exception:
                pass
            finally:
                try: a.close()
                except Exception: pass
                try: b.close()
                except Exception: pass

        t = threading.Thread(target=tunnel, args=(client, backend))
        t.daemon = True
        t.start()
        # 必须阻塞到隧道结束：ThreadingHTTPServer 在 handler 返回后会立即对客户端
        # socket 执行 shutdown(SHUT_WR)+close，不等待的话 WebSocket 握手成功后
        # 毫秒级即被框架断开，前端只能无限静默重连。
        t.join()

    do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = do_PATCH = do_OPTIONS = do_request

    def log_message(self, format, *args):
        logging.info(format % args)


class ThreadedUnixHTTPServer(http.server.ThreadingHTTPServer):
    address_family = socket.AF_UNIX
    daemon_threads = True
    def server_bind(self):
        self.socket.bind(self.server_address)
        os.chmod(self.server_address, 0o660)


def cleanup(signum, frame):
    logging.info("shutting down")
    server.server_close()
    if os.path.exists(SOCK_PATH):
        os.unlink(SOCK_PATH)
    sys.exit(0)


if __name__ == "__main__":
    ProxyHandler._conn_pool = ConnectionPool(TARGET_HOST, INITIAL_PORT)
    if os.path.exists(SOCK_PATH):
        os.unlink(SOCK_PATH)
    server = ThreadedUnixHTTPServer(SOCK_PATH, ProxyHandler)
    signal.signal(signal.SIGTERM, cleanup)
    signal.signal(signal.SIGINT, cleanup)
    logging.info("gateway-proxy started: %s -> %s:%d", SOCK_PATH, TARGET_HOST, INITIAL_PORT)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    server.server_close()
    if ProxyHandler._conn_pool:
        ProxyHandler._conn_pool.close_all()
    if os.path.exists(SOCK_PATH):
        os.unlink(SOCK_PATH)
