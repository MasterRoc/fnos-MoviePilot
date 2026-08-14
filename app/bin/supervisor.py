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


def _out():
    """子进程 stdout 目标"""
    for f in (LOG_FILE, SHARE_LOG):
        if f:
            try:
                return open(f, "a", encoding="utf-8")
            except Exception:
                continue
    return subprocess.DEVNULL


def _backend_out():
    """后端 stdout 目标。

    后端(MoviePilot)会自己把应用日志和 stdio 日志分别写入
    config/logs/moviepilot.log 和 config/logs/moviepilot.stdout.log，
    因此这里丢弃后端的 stdout，避免在顶层 moviepilot.log 重复记录。
    前端与网关无独立日志文件，仍写入顶层 LOG_FILE（见 _out）。
    """
    return subprocess.DEVNULL


def _cleanup_stale_backend():
    """清理残留的后端进程，避免两个后端进程并发访问同一个 SQLite 数据库。

    SQLite 在同一时刻只允许一个进程写。旧后端进程未完全退出时若启动新后端，
    会出现 SQLITE_IOERR_SHORT_READ / disk I/O error 等偶发并发 I/O 错误。

    注意：MoviePilot 后端进程启动后会 setproctitle 改名为 "MoviePilot"，
    因此用 pgrep "app/main.py" 抓不到改名后的残留进程。这里按进程名(comm)精确匹配
    "MoviePilot" 或命令行含 "app/main.py" 的进程，避免误伤 supervisor/gateway/frontend
    （它们的命令行虽含 /appcenter/moviepilot/ 路径，但进程名不是 MoviePilot）。
    """
    my_pid = os.getpid()
    pids = []
    try:
        out = subprocess.check_output(
            ["ps", "-e", "-o", "pid=,comm=", "-o", "args="],
            stderr=subprocess.DEVNULL,
        ).decode()
        for line in out.splitlines():
            parts = line.split(None, 1)
            if len(parts) < 1:
                continue
            pid = parts[0].strip()
            if not pid.isdigit() or int(pid) == my_pid:
                continue
            rest = line[len(pid):].strip()
            comm = parts[1] if len(parts) > 1 else ""
            if comm == "MoviePilot" or "app/main.py" in rest:
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

    前端进程名通常是 node，但命令行含 "frontend-server.js"，据此精确匹配，
    避免误伤后端/网关代理（命令行虽含 /appcenter/moviepilot/ 路径但不含
    frontend-server.js）。
    """
    my_pid = os.getpid()
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
            if "frontend-server.js" in rest:
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
        proc = subprocess.Popen(
            [py, "app/main.py"],
            cwd=MP_SRC,
            env=env,
            stdout=_backend_out(),
            stderr=subprocess.STDOUT,
        )
        log(f"后端已启动 pid={proc.pid}")
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
                if self.backend is not None:
                    log("后端已退出，尝试重启")
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
    # 启动前清理所有残留的后端进程，确保同一时刻只有一个后端进程访问 SQLite 数据库，
    # 避免历史遗留/上次未杀干净的旧后端与新后端并发导致 disk I/O error。
    _cleanup_stale_backend()
    # 启动前清理残留的前端进程，避免旧前端仍占用 FRONTEND_PORT(TCP 3005)，
    # 导致新前端 listen EADDRINUSE 退出并触发重启风暴。
    _cleanup_stale_frontend()
    svc = Services()
    svc.ensure_all()
    log("主管进程就绪")

    def _sigterm(signum, frame):
        log("收到 SIGTERM，正在停止...")
        svc.stop_all()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _sigterm)
    signal.signal(signal.SIGINT, _sigterm)

    while True:
        time.sleep(5)
        try:
            svc.ensure_all()
        except Exception:
            log(f"自愈循环异常: {traceback.format_exc()}")


if __name__ == "__main__":
    main()
