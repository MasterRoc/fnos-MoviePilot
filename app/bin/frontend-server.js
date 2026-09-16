#!/usr/bin/env node
/**
 * MoviePilot fnOS 前端服务
 * =========================
 * 作用：
 *   - 提供 MoviePilot 前端静态资源（dist 目录）
 *   - 将 /api 与 /cookiecloud 反向代理到 Python 后端
 *   - SPA 回退到 index.html
 *
 * 环境变量：
 *   MP_FRONTEND_DIR  前端 dist 目录（必填）
 *   MP_BACKEND_HOST  后端地址，默认 127.0.0.1
 *   MP_BACKEND_PORT  后端端口，默认 3001
 *   MP_FRONTEND_PORT 本服务监听端口，默认 3000
 */
"use strict";

const path = require("path");
const fs = require("fs");
const http = require("http");

const FRONTEND_DIR = process.env.MP_FRONTEND_DIR;
const BACKEND_HOST = process.env.MP_BACKEND_HOST || "127.0.0.1";
const BACKEND_PORT = parseInt(process.env.MP_BACKEND_PORT || "3001", 10);
const FRONTEND_PORT = parseInt(process.env.MP_FRONTEND_PORT || "3000", 10);

if (!FRONTEND_DIR) {
  console.error("[moviepilot-frontend] MP_FRONTEND_DIR is not set");
  process.exit(1);
}

const MIME = {
  ".html": "text/html; charset=utf-8",
  ".js": "application/javascript; charset=utf-8",
  ".mjs": "application/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".gif": "image/gif",
  ".svg": "image/svg+xml",
  ".ico": "image/x-icon",
  ".webp": "image/webp",
  ".woff": "font/woff",
  ".woff2": "font/woff2",
  ".ttf": "font/ttf",
  ".eot": "application/vnd.ms-fontobject",
  ".txt": "text/plain; charset=utf-8",
  ".map": "application/json",
  ".webmanifest": "application/manifest+json",
  ".mp4": "video/mp4",
  ".webm": "video/webm",
};

function contentType(p) {
  return MIME[path.extname(p).toLowerCase()] || "application/octet-stream";
}

// 带这些扩展名的请求必须命中真实文件：找不到就 404。
// 若像 SPA 路由一样回退 index.html，浏览器会把 HTML 当 JS/CSS 解析并抛
// "Unexpected token '<'"，升级后残留的旧 hash chunk 会表现为整页白屏且难以定位。
// 官方 nginx 对 js/css 同样是 try_files $uri =404。
const ASSET_EXT = new Set([
  ".js", ".mjs", ".css", ".map", ".json", ".txt", ".webmanifest",
  ".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg", ".webp",
  ".woff", ".woff2", ".ttf", ".eot", ".otf", ".mp4", ".webm",
]);

const IMAGE_EXT = new Set([".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg", ".webp"]);
const FONT_EXT = new Set([".woff", ".woff2", ".ttf", ".eot", ".otf"]);
const SCRIPT_EXT = new Set([".js", ".mjs", ".css", ".map"]);

// 缓存策略对齐官方 docker/nginx.common.conf：
//   /assets/ 与图片、字体 -> 1 年 immutable（文件名带内容哈希）
//   js/css/map            -> 30 天
//   service-worker.js / manifest.webmanifest -> no-cache（否则前端更新后浏览器
//                            可能继续注册旧版本）
//   index.html 与 SPA 回退 -> no-store
function cacheControlFor(pathname) {
  const p = pathname.toLowerCase();
  const base = p.slice(p.lastIndexOf("/") + 1);
  if (base === "service-worker.js" || base === "service.js" ||
      base === "manifest.webmanifest") {
    return "no-cache, must-revalidate";
  }
  if (p.endsWith(".html") || p === "/") {
    return "no-cache, no-store, must-revalidate";
  }
  const ext = path.extname(p);
  if (p.startsWith("/assets/")) return "public, max-age=31536000, immutable";
  if (IMAGE_EXT.has(ext) || FONT_EXT.has(ext)) {
    return "public, max-age=31536000, immutable";
  }
  if (SCRIPT_EXT.has(ext)) return "public, max-age=2592000";
  return "no-cache";
}

// 与官方 nginx 的 rewrite ^.+mock-server/?(?<suffix>.*)$ /$suffix break; 对齐：
// CookieCloud 客户端把服务地址配成含 mock-server 的形式时，只保留其后部分。
const MOCK_MARKER = "mock-server";

function rewriteMockServer(pathname) {
  const idx = pathname.indexOf(MOCK_MARKER);
  // idx === 0 时 nginx 的 `.+` 无法匹配，保持原路径
  if (idx <= 0) return pathname;
  return "/" + pathname.slice(idx + MOCK_MARKER.length).replace(/^\/+/, "");
}

// 幂等方法：连接级失败（多半是复用了被对端静默关闭的 keep-alive 连接）时
// 自动重试一次，避免前端健康检查（如 system/ping）偶发失败误报"正在重新连接"。
const IDEMPOTENT_METHODS = new Set(["GET", "HEAD", "OPTIONS"]);

function proxyRequest(req, res, pathname, retried) {
  // 与官方 nginx 对齐的 mock-server 路径重写（CookieCloud 客户端场景）
  pathname = rewriteMockServer(pathname);

  // 构造安全的后端转发路径：只对 pathname 编码，避免 ERR_UNESCAPED_CHARACTERS。
  // query string 由浏览器已正确 URL 编码，必须原样透传，绝不能再次 encodeURI——
  // 否则 `server=%E9%A3%9E...` 里的 `%` 会被二次编码成 `%25`，后端解码后得到
  // 字面字符串 `%E9%A3%9E...`，导致按 server 名匹配媒体服务器配置失败、
  // 媒体库数据加载失败（如 mediacenter/library?server=飞牛影视）。
  function encodeSafePath(p) {
    let s;
    try {
      s = encodeURI(p);
    } catch (e) {
      s = "/";
    }
    return s;
  }

  // 从 req.url 提取 query string（如 "?a=1&b=2"，无则空串）。
  // 注意：不能使用 req.url.search —— req.url 是字符串，.search 是 String.prototype 的原生方法，
  // 会得到 "function search() { [native code] }"，拼接出畸形路径导致后端 404。
  let queryStr = "";
  try {
    const rawUrl = req.url || "";
    const qIdx = rawUrl.indexOf("?");
    if (qIdx >= 0) queryStr = rawUrl.slice(qIdx);
  } catch (e) {
    queryStr = "";
  }

  let fullPath;
  try {
    // 只对 pathname 做安全编码，query 已由浏览器编码、原样透传，避免二次编码
    fullPath = encodeSafePath(pathname) + queryStr;
  } catch (e) {
    fullPath = "/";
  }
  // 空路径兜底
  if (!fullPath || fullPath === "") fullPath = "/";

  const options = {
    hostname: BACKEND_HOST,
    port: BACKEND_PORT,
    path: fullPath,
    method: req.method,
    headers: Object.assign({}, req.headers, {
      host: `${BACKEND_HOST}:${BACKEND_PORT}`,
    }),
  };

  let proxy;
  try {
    proxy = http.request(options, (pRes) => {
      const headers = Object.assign({}, pRes.headers);
      res.writeHead(pRes.statusCode, headers);
      pRes.pipe(res);
    });
  } catch (err) {
    console.error("[moviepilot-frontend] proxy create error:", err.message);
    if (!res.headersSent) {
      res.writeHead(502, { "Content-Type": "text/plain; charset=utf-8" });
      res.end("Bad Gateway");
    }
    return;
  }

  proxy.on("error", (err) => {
    console.error("[moviepilot-frontend] proxy error:", err.message);
    if (!res.headersSent) {
      // 幂等请求的连接级错误（ECONNRESET/EPIPE/ETIMEDOUT 等，通常源于后端
      // uvicorn 的短 keep-alive 超时静默关闭了复用连接），重试一次即可成功，
      // 避免后端轻微抖动被放大为前端连接状态误判。
      if (!retried && IDEMPOTENT_METHODS.has(req.method)) {
        console.error("[moviepilot-frontend] retrying once:", req.method, pathname);
        try { proxy.destroy(); } catch (e) {}
        proxyRequest(req, res, pathname, true);
        return;
      }
      res.writeHead(502, { "Content-Type": "text/plain; charset=utf-8" });
      res.end("Bad Gateway: backend not reachable");
    } else {
      res.end();
    }
  });

  // 防止 req 读取出错导致崩溃
  req.on("error", (err) => {
    console.error("[moviepilot-frontend] request error:", err.message);
    try { proxy.destroy(); } catch (e) {}
  });

  try {
    req.pipe(proxy);
  } catch (e) {
    console.error("[moviepilot-frontend] pipe error:", e.message);
    if (!res.headersSent) {
      res.writeHead(502);
      res.end();
    }
  }
}

function serveStatic(req, res, pathname) {
  // 归一化，防止路径穿越
  let rel;
  try {
    rel = decodeURIComponent(pathname);
  } catch (e) {
    rel = pathname;
  }
  if (rel.indexOf("\0") !== -1) {
    res.writeHead(400);
    res.end();
    return;
  }
  // 必须带 path.sep 校验前缀：只比到 FRONTEND_DIR 字符串前缀的话，
  // "../frontend-evil/x" 这类兄弟前缀目录能通过校验造成路径穿越。
  // FRONTEND_DIR 本身（根路径 "/"）也放行，交给后面的 SPA 回退。
  const root = path.normalize(FRONTEND_DIR + path.sep);
  let filePath = path.normalize(path.join(FRONTEND_DIR, rel));
  if (filePath !== path.normalize(FRONTEND_DIR) && !filePath.startsWith(root)) {
    res.writeHead(403);
    res.end("Forbidden");
    return;
  }

  if (!fs.existsSync(filePath) || !fs.statSync(filePath).isFile()) {
    // 静态资源缺失必须 404，不能回退 index.html（见 ASSET_EXT 注释）
    if (ASSET_EXT.has(path.extname(filePath).toLowerCase())) {
      res.writeHead(404, {
        "Content-Type": "text/plain; charset=utf-8",
        "Cache-Control": "no-cache",
      });
      res.end("Not Found");
      return;
    }
    // SPA 回退：非文件请求全部返回 index.html
    const indexFile = path.join(FRONTEND_DIR, "index.html");
    if (fs.existsSync(indexFile)) {
      res.writeHead(200, {
        "Content-Type": "text/html; charset=utf-8",
        "Cache-Control": "no-cache, no-store, must-revalidate",
      });
      fs.createReadStream(indexFile).pipe(res);
      return;
    }
    res.writeHead(404, { "Cache-Control": "no-cache" });
    res.end("Not Found");
    return;
  }

  res.writeHead(200, {
    "Content-Type": contentType(filePath),
    "Cache-Control": cacheControlFor(rel),
  });
  fs.createReadStream(filePath).pipe(res);
}

// ---------------------------------------------------------------------------
// WebSocket 升级转发
// ---------------------------------------------------------------------------
// Node 的 http.Server 在没有 'upgrade' 监听时，会把带 Upgrade 的请求当作普通
// 请求处理（走 SPA 回退返回 200 HTML），浏览器握手必然失败。网关代理
// (gateway-proxy.py) 有完整的 WS 隧道，若这里不转发，整条 WS 链路就是断的。
// 对应官方 nginx 的 proxy_set_header Upgrade $http_upgrade。
function isProxyPath(pathname) {
  return pathname === "/api" || pathname.startsWith("/api/") ||
    pathname === "/cookiecloud" || pathname.startsWith("/cookiecloud/");
}

function writeRawResponse(socket, res) {
  const lines = [`HTTP/1.1 ${res.statusCode} ${res.statusMessage}`];
  const raw = res.rawHeaders || [];
  for (let i = 0; i + 1 < raw.length; i += 2) {
    lines.push(`${raw[i]}: ${raw[i + 1]}`);
  }
  socket.write(lines.join("\r\n") + "\r\n\r\n");
}

function proxyUpgrade(req, socket, head) {
  const pathname = (req.url || "/").split("?")[0];
  if (!isProxyPath(pathname)) {
    socket.destroy();
    return;
  }

  // 原样透传客户端头（含 Sec-WebSocket-* 与 Cookie/Authorization），
  // 仅替换 host；Connection/Upgrade 必须保留，否则后端不会返回 101。
  const headers = Object.assign({}, req.headers, {
    host: `${BACKEND_HOST}:${BACKEND_PORT}`,
  });

  let proxyReq;
  try {
    proxyReq = http.request({
      hostname: BACKEND_HOST,
      port: BACKEND_PORT,
      path: req.url || "/",
      method: req.method || "GET",
      headers,
    });
  } catch (err) {
    console.error("[moviepilot-frontend] upgrade request error:", err.message);
    socket.destroy();
    return;
  }

  proxyReq.on("upgrade", (pRes, pSocket, pHead) => {
    writeRawResponse(socket, pRes);
    // 客户端在握手请求之后立刻发出的字节（罕见）与后端 101 之后带出的字节，
    // 都要按顺序补给对端，否则会丢帧。
    if (head && head.length) pSocket.write(head);
    if (pHead && pHead.length) socket.write(pHead);
    pSocket.pipe(socket);
    socket.pipe(pSocket);
    const cleanup = () => {
      try { pSocket.destroy(); } catch (e) { /* ignore */ }
      try { socket.destroy(); } catch (e) { /* ignore */ }
    };
    pSocket.on("error", cleanup);
    socket.on("error", cleanup);
    pSocket.on("close", cleanup);
    socket.on("close", cleanup);
  });

  // 后端拒绝升级（401/404 等）：原样回传状态与头部后关闭
  proxyReq.on("response", (pRes) => {
    writeRawResponse(socket, pRes);
    pRes.pipe(socket);
  });

  proxyReq.on("error", (err) => {
    console.error("[moviepilot-frontend] upgrade proxy error:", err.message);
    try {
      socket.write("HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\nContent-Length: 0\r\n\r\n");
    } catch (e) { /* ignore */ }
    socket.destroy();
  });

  proxyReq.end();
}

const server = http.createServer((req, res) => {
  const pathname = req.url.split("?")[0];

  // API 与 CookieCloud 代理到后端
  if (isProxyPath(pathname)) {
    proxyRequest(req, res, pathname);
    return;
  }

  serveStatic(req, res, pathname);
});

server.on("upgrade", proxyUpgrade);

server.on("error", (err) => {
  console.error("[moviepilot-frontend] server error:", err.message);
  // 端口被占用(EADDRINUSE)时，短暂等待再退出，给残留进程释放端口留出时间，
  // 避免 supervisor 立即拉起重启再次抢端口形成风暴。
  if (err.code === "EADDRINUSE") {
    console.error("[moviepilot-frontend] port already in use, waiting before exit");
    setTimeout(() => process.exit(1), 3000);
  } else {
    process.exit(1);
  }
});

// 拉长 keep-alive 存活时间，与 gateway-proxy.py 连接池(60s)匹配，
// 避免代理池复用的空闲连接被 Node 默认 keepAliveTimeout(5s) 提前关闭，
// 导致 "request failed, retry fresh: ... Remote end closed connection without response"。
server.keepAliveTimeout = 60000;
server.headersTimeout = 61000;

server.listen(FRONTEND_PORT, "127.0.0.1", () => {
  console.log(`[moviepilot-frontend] serving ${FRONTEND_DIR} on 127.0.0.1:${FRONTEND_PORT}`);
  console.log(`[moviepilot-frontend] backend at ${BACKEND_HOST}:${BACKEND_PORT}`);
});

// 优雅退出
process.on("SIGTERM", () => {
  console.log("[moviepilot-frontend] SIGTERM received, shutting down");
  server.close(() => process.exit(0));
  setTimeout(() => process.exit(0), 3000).unref();
});
process.on("SIGINT", () => {
  process.exit(0);
});
