#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MoviePilot fnOS 主管进程
========================
统一管理三个子服务：
  1. 后端    python app/main.py       (127.0.0.1:PORT)
  2. 前端    node frontend-server.js  (127.0.0.1:NGINX_PORT)
  3. 网关代理 gateway-proxy.py        (Unix socket)

作为 fnOS 应用唯一的"主进程"，子服务全部由其守护：
  - 任何一个崩溃都会自动重启（自愈），单服务失败不影响主管
  - 收到 SIGTERM 时优雅停止全部子服务
  - 持续前台运行（fnOS 通过 PID 追踪本进程）

环境变量：
  MP_SRC / VENV_DIR / BIN_DIR / FRONTEND_DIR / CONFIG_DIR
  BACKEND_PORT / FRONTEND_PORT / GATEWAY_SOCK / LOG_FILE
  PYTHON_BIN（系统 python312，用于启动代理）
  SHARE_LOG（可选，额外写日志到共享目录，便于诊断）
"""
import os
import sys
import time
import signal
import subprocess
import threading
import traceback

MP_SRC = os.environ.get("MP_SRC", "")
VENV_DIR = os.environ.get("VENV_DIR", "")
BIN_DIR = os.environ.get("BIN_DIR", "")
FRONTEND_DIR = os.environ.get("FRONTEND_DIR", "")
CONFIG_DIR = os.environ.get("CONFIG_DIR", "")
BACKEND_PORT = os.environ.get("BACKEND_PORT", "3002")
FRONTEND_PORT = os.environ.get("FRONTEND_PORT", "3005")
GATEWAY_SOCK = os.environ.get("GATEWAY_SOCK", "")
LOG_FILE = os.environ.get("LOG_FILE", "")
SHARE_LOG = os.environ.get("SHARE_LOG", "")
PYTHON_BIN = os.environ.get("PYTHON_BIN", "python3")
BACKEND_HOST = os.environ.get("BACKEND_HOST", "127.0.0.1")

# 后端 stdio 单独落盘。早期实现把后端 stdout 直接丢给 DEVNULL，导致后端一旦在
# import 阶段崩溃（缺依赖、Python 版本不符、权限问题）就完全没有线索可查，
# 只能看到前端不断刷 502 —— 而这恰恰是最需要日志的场景。
BACKEND_LOG = os.environ.get("BACKEND_LOG", "")
if not BACKEND_LOG and LOG_FILE:
    BACKEND_LOG = os.path.join(os.path.dirname(LOG_FILE) or ".", "backend.log")
BACKEND_LOG_MAX = 8 * 1024 * 1024


def log(msg):
    line = f"[supervisor] {time.strftime('%Y-%m-%d %H:%M:%S')} - {msg}"
    print(line, flush=True)
    for f in (LOG_FILE, SHARE_LOG):
        if f:
            try:
                with open(f, "a", encoding="utf-8") as fp:
                    fp.write(line + "\n")
            except Exception:
                pass


_LOG_HANDLES = {}


def _log_handle(path):
    """打开并缓存追加模式的日志句柄。

    该句柄会以 fd 形式复制给子进程使用，父进程必须长期持有它。若每次启动子服务
    都新开一个句柄且从不关闭，前端/代理进入崩溃重启循环时会持续泄漏 fd，最终
    supervisor 因 EMFILE 而无法再拉起任何子服务。
    """
    handle = _LOG_HANDLES.get(path)
    if handle is not None and not handle.closed:
        return handle
    try:
        handle = open(path, "a", encoding="utf-8")
    except Exception:
        return None
    _LOG_HANDLES[path] = handle
    return handle


def _out():
    """子进程 stdout 目标（复用缓存句柄，避免每次重启泄漏 fd）"""
    for f in (LOG_FILE, SHARE_LOG):
        if f:
            handle = _log_handle(f)
            if handle is not None:
                return handle
    return subprocess.DEVNULL


def _rotate_backend_log():
    """后端日志超过上限时轮转一次。

    只在每次（重新）启动后端时检查：后端重启次数有限，这里的开销可以忽略；
    而 MoviePilot 的崩溃回溯可能很长，必须留够空间。
    """
    try:
        if os.path.getsize(BACKEND_LOG) < BACKEND_LOG_MAX:
            return
    except OSError:
        return
    old = _LOG_HANDLES.pop(BACKEND_LOG, None)
    if old is not None:
        try:
            old.close()
        except Exception:
            pass
    try:
        os.replace(BACKEND_LOG, BACKEND_LOG + ".1")
    except OSError:
        pass


def _backend_out():
    """后端 stdout 目标：独立文件 backend.log。

    后端(MoviePilot)会自己把应用日志写入 config/logs/moviepilot.log，
    但那要求它已经跑过配置加载；启动早期的 ImportError / 语法错误 / 缺 .so
    只会落在 stdio 上。这里单独接住，避免与顶层 moviepilot.log 混在一起，
    也让"后端已启动"与"后端已可服务"两件事能被区分记录。
    """
    if not BACKEND_LOG:
        return subprocess.DEVNULL
    _rotate_backend_log()
    handle = _LOG_HANDLES.get(BACKEND_LOG)
    if handle is not None and not handle.closed:
        return handle
    try:
        handle = open(BACKEND_LOG, "a", encoding="utf-8", errors="replace")
    except Exception:
        return subprocess.DEVNULL
    _LOG_HANDLES[BACKEND_LOG] = handle
    return handle


def _dump_backend_log_tail(lines=30):
    """把后端日志末尾转存到主日志，排查时无需再另开文件。"""
    if not BACKEND_LOG or not os.path.exists(BACKEND_LOG):
        return
    try:
        with open(BACKEND_LOG, "r", encoding="utf-8", errors="replace") as fp:
            tail = fp.readlines()[-lines:]
    except OSError:
        return
    if not tail:
        return
    log("---- 后端日志末尾 ----")
    for line in tail:
        log(line.rstrip("\n"))
    log("---- 后端日志结束 ----")


def _port_open(host, port, timeout=1.0):
    import socket
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# 后端启动健康度跟踪
# ---------------------------------------------------------------------------
# 为什么需要这个：supervisor 原先只按"进程是否存活"判断后端健康（ensure_all
# 里的 proc.poll()）。但后端最常见的失败形态是 import 阶段崩溃——典型如站点
# 资源（app/application/site/sites.*）缺失：
#
#     ModuleNotFoundError: No module named 'app.application.site.sites'
#
# 此时进程确实是"起来了又立刻退出"，若只等 poll() 发现，会陷入
# "启动→秒退→重启→再秒退"的静默循环，而崩溃原因只能靠翻 backend.log 猜。
# 更要命的是重启前不做任何修复，资源缺了就永远是缺的，应用永远起不来。
#
# 所以这里记录"本次启动是否真的就绪过"，让重启路径能区分：
#   - 曾就绪后崩溃 → 普通抖动，直接重启
#   - 从未就绪就退出 → 启动期故障，先修资源再重启
_boot_state_lock = threading.Lock()
_boot_state = {
    "started_at": 0.0,
    "ready": False,
    "reported_dead": False,
    # 连续"启动即失败"次数。用于退避：资源不可恢复时（例如资源只存在于 fpk
    # 包内、本机已无任何来源），repair 每轮都失败且结果不变，此时按 5s 一轮
    # 无限重启只会刷爆日志、反复拉起注定失败的进程。累计到阈值后放慢重试。
    "fail_streak": 0,
    # 退避截止时间（time.monotonic），在此之前不重启后端
    "next_retry_at": 0.0,
    # 是否已提示过进入退避，避免每轮重复刷同一句
    "backoff_logged": False,
}

# 连续失败达到该次数后开始退避
_BACKOFF_AFTER = 3
# 退避期间的重试间隔（秒）；仍会重试，只是不再 5s 一轮
_BACKOFF_INTERVAL = 300


def _mark_backend_started():
    with _boot_state_lock:
        _boot_state["started_at"] = time.time()
        _boot_state["ready"] = False
        _boot_state["reported_dead"] = False


def _mark_backend_ready():
    with _boot_state_lock:
        _boot_state["ready"] = True
        # 成功就绪即清零失败计数，恢复正常守护节奏
        _boot_state["fail_streak"] = 0
        _boot_state["next_retry_at"] = 0.0
        _boot_state["backoff_logged"] = False


def _backend_was_ready():
    with _boot_state_lock:
        return _boot_state["ready"]


def _note_backend_failure() -> int:
    """记一次"启动即失败"，返回当前连续失败次数。

    达到阈值时设定退避截止时间；返回的计数用于让调用方判断是否刚进入退避。
    """
    with _boot_state_lock:
        _boot_state["fail_streak"] += 1
        streak = _boot_state["fail_streak"]
        if streak >= _BACKOFF_AFTER:
            _boot_state["next_retry_at"] = time.monotonic() + _BACKOFF_INTERVAL
        return streak


def _backoff_remaining() -> float:
    """退避剩余秒数；0 表示可以立即重启。"""
    with _boot_state_lock:
        return max(0.0, _boot_state["next_retry_at"] - time.monotonic())


def _backend_fail_streak() -> int:
    with _boot_state_lock:
        return _boot_state["fail_streak"]


def _backoff_log_once() -> bool:
    """进入退避后只提示一次，返回 True 表示本次该打日志。"""
    with _boot_state_lock:
        if _boot_state["backoff_logged"]:
            return False
        _boot_state["backoff_logged"] = True
        return True


def _claim_death_report():
    """返回 True 表示本次死亡尚未被上报过（避免重复刷日志）。"""
    with _boot_state_lock:
        if _boot_state["reported_dead"]:
            return False
        _boot_state["reported_dead"] = True
        return True


def _watch_backend_ready(proc):
    """后台线程：等待后端真正监听端口并记录耗时。

    uvicorn 的监听端口是在 lifespan 全部完成之后才 bind 的（建库/迁移、插件
    安装、调度器启动都在那之前），首次启动可能要几十秒到几分钟。期间前端转发
    必然 ECONNREFUSED。这里把"进程已启动"和"端口已可服务"分开记录，并在迟迟
    未就绪时周期性告警，避免把启动慢误判成启动失败。

    进程在就绪前退出时，这里负责把它变成一条**显式**的失败记录（含退出码与
    后端日志末尾）。早期实现在这一分支直接 return，导致"后端 import 崩溃"
    在主管日志里完全不留痕迹——只有前端在一路刷 502。
    """
    port = int(BACKEND_PORT)
    started = time.time()
    deadline = started + 900
    next_warn = started + 30
    while time.time() < deadline:
        if proc.poll() is not None:
            if _claim_death_report():
                log("后端进程已退出（退出码 %s），且从未监听 %s:%s —— 启动期故障"
                    % (proc.returncode, BACKEND_HOST, port))
                _dump_backend_log_tail()
                _note_backend_failure()
            return
        if _port_open(BACKEND_HOST, port):
            _mark_backend_ready()
            log("后端已就绪：%s:%s 可连接（启动耗时 %.1fs）"
                % (BACKEND_HOST, port, time.time() - started))
            return
        now = time.time()
        if now >= next_warn:
            log("后端进程存活但尚未监听 %s:%s（已等待 %.0fs，uvicorn 需先完成 "
                "lifespan 才 bind 端口）" % (BACKEND_HOST, port, now - started))
            next_warn = now + 60
        time.sleep(2)
    log("警告：后端启动超过 900s 仍未监听 %s:%s" % (BACKEND_HOST, port))


def repair_backend_resources():
    """回填站点资源（app/application/site/sites.*）。

    后端 import 阶段就依赖 sites 扩展，缺了必然起不来。资源随上游重构搬过家
    （app/helper → app/application/site），且自更新器替换整个 mp/ 时可能把它
    漏掉，所以在**每次启动后端起不来时**都要兜一次，而不只是在 cmd/main start
    阶段做一次。

    幂等、纯标准库、失败不抛（修复不了就继续尝试启动，由日志暴露问题）。
    """
    script = os.path.join(BIN_DIR, "mp_resources.py")
    python = os.path.join(VENV_DIR, "bin", "python")
    if not (os.path.isfile(script) and os.path.isfile(python) and MP_SRC):
        return
    try:
        proc = subprocess.run(
            [python, script],
            env={**os.environ, "MP_SRC": MP_SRC, "APP_PYTHON": python},
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=180,
        )
        out = (proc.stdout or b"").decode("utf-8", errors="replace").strip()
        if out:
            for line in out.splitlines():
                log("  [resources] " + line)
        if proc.returncode == 0:
            log("站点资源检查通过，重启后端")
        else:
            log("站点资源修复未完成（退出码 %s），仍尝试重启后端" % proc.returncode)
    except subprocess.TimeoutExpired:
        log("站点资源修复超时（120s），继续重启后端")
    except Exception as e:
        log("站点资源修复异常（忽略）: %s" % e)


def _cleanup_stale_backend():
    """清理残留的后端进程，避免两个后端进程并发访问同一个 SQLite 数据库。

    SQLite 在同一时刻只允许一个进程写。旧后端进程未完全退出时若启动新后端，
    会出现 SQLITE_IOERR_SHORT_READ / disk I/O error 等偶发并发 I/O 错误。

    注意：MoviePilot 后端进程启动后会 setproctitle 改名为 "MoviePilot"，
    因此用 pgrep "app/main.py" 抓不到改名后的残留进程。这里按两路匹配：
      - comm 精确等于 "MoviePilot"（改名后的进程）
      - 命令行含本应用的 mp/app/main.py 绝对路径（尚未改名的进程）
    匹配只用本应用 MP_SRC 下的精确路径，避免误杀其他应用的同名脚本进程。
    """
    my_pid = os.getpid()
    backend_script = os.path.join(MP_SRC, "app", "main.py")
    pids = []
    try:
        out = subprocess.check_output(
            ["ps", "-e", "-o", "pid=,comm=", "-o", "args="],
            stderr=subprocess.DEVNULL,
        ).decode()
        for line in out.splitlines():
            # ps 每行是 "pid comm args" 三列，必须 split(None, 2) 才能单独取出
            # comm；split(None, 1) 会把 args 拼进 comm，导致 "MoviePilot"
            # 精确匹配永不成立，改名后的残留进程漏杀。
            parts = line.split(None, 2)
            if len(parts) < 2:
                continue
            pid = parts[0]
            if not pid.isdigit() or int(pid) == my_pid:
                continue
            comm = parts[1]
            args = parts[2] if len(parts) > 2 else ""
            if comm == "MoviePilot" or backend_script in args:
                pids.append(int(pid))
    except Exception:
        pids = []
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            continue
        # 等待优雅退出，最多 15 秒
        waited = 0
        while waited < 15:
            try:
                os.kill(pid, 0)
            except OSError:
                break  # 进程已退出
            time.sleep(1)
            waited += 1
        else:
            # 仍未退出，强制杀掉
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        log(f"已清理残留后端进程 pid={pid}")
    if pids:
        time.sleep(1)  # 给文件系统/checkpoint 一点时间


def _cleanup_stale_frontend():
    """清理残留的前端进程，避免旧前端仍占用 FRONTEND_PORT(TCP 3005)，
    导致新前端启动时 listen EADDRINUSE 而立即退出，引发 supervisor 重启风暴。

    前端进程名是 node，只能按命令行匹配本应用 BIN_DIR 下 frontend-server.js
    的精确路径，避免误伤其他应用的 node 进程。
    """
    my_pid = os.getpid()
    frontend_script = os.path.join(BIN_DIR, "frontend-server.js")
    pids = []
    try:
        out = subprocess.check_output(
            ["ps", "-e", "-o", "pid=,args="],
            stderr=subprocess.DEVNULL,
        ).decode()
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            pid, _, rest = line.partition(" ")
            if not pid.isdigit() or int(pid) == my_pid:
                continue
            if frontend_script in rest:
                pids.append(int(pid))
    except Exception:
        pids = []
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            continue
        waited = 0
        while waited < 15:
            try:
                os.kill(pid, 0)
            except OSError:
                break
            time.sleep(1)
            waited += 1
        else:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        log(f"已清理残留前端进程 pid={pid}")
    if pids:
        time.sleep(1)


def start_backend():
    """启动后端"""
    _cleanup_stale_backend()
    py = os.path.join(VENV_DIR, "bin", "python")
    if not os.path.exists(py):
        log(f"错误: 后端 python 不存在 {py}")
        return None
    env = os.environ.copy()
    env.update({
        "CONFIG_DIR": CONFIG_DIR,
        "PYTHONPATH": MP_SRC,
        "HOST": "127.0.0.1",
        "PORT": BACKEND_PORT,
    })
    try:
        # 绝对路径启动，便于残留进程清理与 stop 兜底用命令行精确定位本应用后端
        proc = subprocess.Popen(
            [py, os.path.join(MP_SRC, "app", "main.py")],
            cwd=MP_SRC,
            env=env,
            stdout=_backend_out(),
            stderr=subprocess.STDOUT,
        )
        log(f"后端已启动 pid={proc.pid}（stdio -> {BACKEND_LOG or 'DEVNULL'}）")
        _mark_backend_started()
        threading.Thread(target=_watch_backend_ready, args=(proc,),
                         daemon=True).start()
        return proc
    except Exception as e:
        log(f"后端启动异常: {e}\n{traceback.format_exc()}")
        return None


def start_frontend():
    """启动前端"""
    _cleanup_stale_frontend()
    node = shutil_which_node()
    if not node:
        log("错误: 未找到 node 可执行文件")
        return None
    env = os.environ.copy()
    env.update({
        "MP_FRONTEND_DIR": FRONTEND_DIR,
        "MP_BACKEND_HOST": "127.0.0.1",
        "MP_BACKEND_PORT": BACKEND_PORT,
        "MP_FRONTEND_PORT": FRONTEND_PORT,
    })
    try:
        proc = subprocess.Popen(
            [node, os.path.join(BIN_DIR, "frontend-server.js")],
            env=env,
            stdout=_out(),
            stderr=subprocess.STDOUT,
        )
        log(f"前端已启动 pid={proc.pid} (node={node})")
        return proc
    except Exception as e:
        log(f"前端启动异常: {e}\n{traceback.format_exc()}")
        return None


def shutil_which_node():
    """查找 node：优先 fnOS nodejs_v24，其次 PATH"""
    candidates = [
        "/var/apps/nodejs_v24/target/bin/node",
        "/var/apps/nodejs_v20/target/bin/node",
        "node",
    ]
    for c in candidates:
        if os.path.isabs(c):
            if os.path.exists(c) and os.access(c, os.X_OK):
                return c
        else:
            import shutil
            r = shutil.which(c)
            if r:
                return r
    return None


def start_proxy():
    """启动网关代理"""
    try:
        proc = subprocess.Popen(
            [PYTHON_BIN, os.path.join(BIN_DIR, "gateway-proxy.py"),
             GATEWAY_SOCK, "127.0.0.1", FRONTEND_PORT],
            env=os.environ.copy(),
            stdout=_out(),
            stderr=subprocess.STDOUT,
        )
        log(f"网关代理已启动 pid={proc.pid}")
        return proc
    except Exception as e:
        log(f"网关代理启动异常: {e}\n{traceback.format_exc()}")
        return None


class Services:
    def __init__(self):
        self.backend = None
        self.frontend = None
        self.proxy = None
        self._lock = threading.Lock()
        self._stop = False

    def ensure_all(self):
        with self._lock:
            if self._stop:
                return
            if self.backend is None or self.backend.poll() is not None:
                was_ready = _backend_was_ready()
                if self.backend is not None:
                    # 带上退出码与日志末尾：没有这两样，"后端反复重启"就只能靠猜
                    log(f"后端已退出（退出码 {self.backend.returncode}），尝试重启")
                    _dump_backend_log_tail()
                    # 从未就绪就退出 = 启动期故障（最常见是站点资源缺失导致
                    # import 失败）。此时必须先回填资源再拉起，否则就是
                    # "启动→秒退→重启→再秒退"的死循环，应用永远起不来。
                    if not was_ready:
                        # 资源修复与日志上报分开：上报用 _claim_death_report
                        # 去重（线程与本循环都会看到死亡），但**修复不能去重**
                        # —— 线程可能先抢到上报权而只打日志，若把修复也挂在
                        # 同一标志下，这一轮就再没人修资源了。修复本身幂等，
                        # 重复调用无副作用。
                        log("上次后端从未就绪即退出，尝试修复站点资源后再启动")
                        repair_backend_resources()
                        if _claim_death_report():
                            _note_backend_failure()
                # 退避：反复启动失败（例如资源在本机已无任何来源可修）时，
                # 不再 5s 一轮无限重启，改为按 _BACKOFF_INTERVAL 放慢重试。
                if _backoff_remaining() > 0:
                    if _backoff_log_once():
                        log("后端已连续 %d 次启动失败，进入退避：每 %d 秒重试一次"
                            % (_backend_fail_streak(), _BACKOFF_INTERVAL))
                    return
                self.backend = start_backend()
            if self.frontend is None or self.frontend.poll() is not None:
                if self.frontend is not None:
                    log("前端已退出，尝试重启")
                self.frontend = start_frontend()
            if self.proxy is None or self.proxy.poll() is not None:
                if os.path.exists(GATEWAY_SOCK):
                    try:
                        os.unlink(GATEWAY_SOCK)
                    except OSError:
                        pass
                if self.proxy is not None:
                    log("网关代理已退出，尝试重启")
                self.proxy = start_proxy()

    def stop_all(self):
        self._stop = True
        for name, proc in [("proxy", self.proxy), ("frontend", self.frontend),
                           ("backend", self.backend)]:
            if proc is not None and proc.poll() is None:
                log(f"停止{name} pid={proc.pid}")
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
        log("全部子服务已停止")


def main():
    if not MP_SRC or not VENV_DIR:
        log("错误: 缺少 MP_SRC / VENV_DIR 环境变量")
        sys.exit(1)

    log(f"主管进程启动: MP_SRC={MP_SRC}, PORT={BACKEND_PORT}/{FRONTEND_PORT}")
    svc = Services()

    def _sigterm(signum, frame):
        log("收到 SIGTERM，正在停止...")
        svc.stop_all()
        sys.exit(0)

    # 信号处理必须在任何耗时操作（残留清理、子服务启动）之前注册：否则启动阶段
    # 收到 SIGTERM 会走默认动作直接退出，既不执行 stop_all()，也来不及收尾，留下
    # 一批孤儿后端/前端/代理进程（后端还会继续占用 SQLite）。
    signal.signal(signal.SIGTERM, _sigterm)
    signal.signal(signal.SIGINT, _sigterm)

    # 启动前清理所有残留的后端进程，确保同一时刻只有一个后端进程访问 SQLite 数据库，
    # 避免历史遗留/上次未杀干净的旧后端与新后端并发导致 disk I/O error。
    _cleanup_stale_backend()
    # 启动前清理残留的前端进程，避免旧前端仍占用 FRONTEND_PORT(TCP 3005)，
    # 导致新前端 listen EADDRINUSE 退出并触发重启风暴。
    _cleanup_stale_frontend()
    svc.ensure_all()
    log("主管进程就绪")

    while True:
        time.sleep(5)
        try:
            svc.ensure_all()
        except Exception:
            log(f"自愈循环异常: {traceback.format_exc()}")


if __name__ == "__main__":
    main()
