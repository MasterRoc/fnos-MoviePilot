#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""supervisor.py 后端自愈逻辑的离线冒烟测试（Windows 也能跑）。

背景：后端最常见的失败形态是 import 阶段崩溃（典型如站点资源
app/application/site/sites.* 缺失）。早期 supervisor 对此完全静默：
`_watch_backend_ready` 在 `proc.poll() is not None` 时直接 return，
主管日志里没有任何"后端崩过"的记录，而且重启前不做资源修复，
于是"启动→秒退→重启→再秒退"会无限循环。

本测试不启动真实后端，只驱动 supervisor 的状态机与决策函数：

  A 失败记录：就绪前退出会记录退出码 + 转存后端日志末尾
  B 修复触发：从未就绪即退出 → 必须调用资源修复
  C 就绪后崩溃：属普通抖动，不应触发资源修复
  D 退避：连续失败达阈值后，重启被推迟而不是 5s 一轮
  E 去重：同一次死亡只报一次，失败计数不翻倍
  F 恢复：成功就绪后清零失败计数与退避状态
"""
import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SANDBOX = ROOT / ".local-build" / "_smoke" / "supervisor"

# 必须在 import supervisor 之前设好：该模块在导入期就读这些环境变量
os.environ["MP_SRC"] = str(SANDBOX / "appdest" / "mp")
os.environ["VENV_DIR"] = str(SANDBOX / "appdest" / "python")
os.environ["BIN_DIR"] = str(SANDBOX / "appdest" / "bin")
os.environ["FRONTEND_DIR"] = str(SANDBOX / "appdest" / "frontend")
os.environ["CONFIG_DIR"] = str(SANDBOX / "pkgvar" / "config")
os.environ["LOG_FILE"] = str(SANDBOX / "pkgvar" / "moviepilot.log")
os.environ["SHARE_LOG"] = ""
os.environ["BACKEND_PORT"] = "39998"

sys.path.insert(0, str(ROOT / "app" / "bin"))
import supervisor as sup  # noqa: E402

FAILS = []


def check(label, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + label
          + (f"  <- {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(label)


class FakeProc:
    """模拟 subprocess.Popen：可控存活/退出，且 poll() 返回设定退出码。"""

    def __init__(self, alive=True, code=1):
        self._alive = alive
        self._code = code
        self.pid = 4242

    def kill(self):
        self._alive = False

    def poll(self):
        return None if self._alive else self._code

    @property
    def returncode(self):
        return None if self._alive else self._code


def reset_state():
    with sup._boot_state_lock:
        sup._boot_state.update({
            "started_at": 0.0, "ready": False, "reported_dead": False,
            "fail_streak": 0, "next_retry_at": 0.0, "backoff_logged": False,
        })


def build_sandbox():
    if SANDBOX.exists():
        shutil.rmtree(SANDBOX)
    for d in ("appdest/mp/app/application/site", "appdest/bin",
              "appdest/frontend", "pkgvar/config"):
        (SANDBOX / d).mkdir(parents=True, exist_ok=True)
    # 空的后端日志，供 _dump_backend_log_tail 读取
    (SANDBOX / "pkgvar" / "backend.log").write_text(
        "Traceback (most recent call last):\n"
        "ModuleNotFoundError: No module named 'app.application.site.sites'\n",
        encoding="utf-8")
    sup.BACKEND_LOG = str(SANDBOX / "pkgvar" / "backend.log")


def main():
    build_sandbox()
    reset_state()

    # 捕获 supervisor 的 log() 输出，断言日志内容
    lines = []
    sup.log = lambda msg: lines.append(str(msg))

    # 让 _port_open 恒为 False：模拟"后端始终没监听端口"
    sup._port_open = lambda host, port, timeout=1.0: False

    # 拦截资源修复，只记录是否被调用
    repairs = []
    sup.repair_backend_resources = lambda: repairs.append(time.time())

    print("== A. 就绪前退出：记录退出码与后端日志末尾 ==")
    lines.clear()
    proc = FakeProc(alive=False, code=1)
    sup._mark_backend_started()
    # 直接跑一次线程函数体（它会在 poll() 非空时立即返回）
    sup._watch_backend_ready(proc)
    joined = "\n".join(lines)
    check("A1 记录退出码", "退出码 1" in joined, joined[:200])
    check("A2 记录'从未监听'", "从未监听" in joined, joined[:200])
    check("A3 转存后端日志末尾",
          "ModuleNotFoundError" in joined, joined[:300])
    check("A4 计入失败次数", sup._backend_fail_streak() == 1,
          f"streak={sup._backend_fail_streak()}")

    print()
    print("== B. 从未就绪即退出 -> 必须触发资源修复 ==")
    repairs.clear()
    reset_state()
    svc = sup.Services()
    svc.backend = FakeProc(alive=False, code=1)
    sup._mark_backend_started()          # ready=False
    # start_backend 不真起进程
    sup.start_backend = lambda: FakeProc()
    sup.start_frontend = lambda: FakeProc()
    sup.start_proxy = lambda: FakeProc()
    svc.ensure_all()
    check("B1 修复被调用", len(repairs) == 1, f"calls={len(repairs)}")

    print()
    print("== C. 曾就绪后崩溃 -> 普通抖动，不修资源 ==")
    repairs.clear()
    reset_state()
    svc = sup.Services()
    svc.backend = FakeProc(alive=False, code=137)
    sup._mark_backend_started()
    sup._mark_backend_ready()            # 曾经就绪过
    svc.ensure_all()
    check("C1 未触发修复", len(repairs) == 0, f"calls={len(repairs)}")

    print()
    print("== D. 退避：连续失败达阈值后推迟重启 ==")
    reset_state()
    proc = FakeProc(alive=False, code=1)
    for _ in range(sup._BACKOFF_AFTER):
        sup._mark_backend_started()
        sup._watch_backend_ready(proc)
    check("D1 失败计数达阈值",
          sup._backend_fail_streak() == sup._BACKOFF_AFTER,
          f"streak={sup._backend_fail_streak()}")
    check("D2 退避已生效（剩余>0）", sup._backoff_remaining() > 0,
          f"remaining={sup._backoff_remaining()}")

    # 退避期间 ensure_all 不应再拉起后端
    started = []
    sup.start_backend = lambda: (started.append(1), FakeProc())[1]
    svc = sup.Services()
    svc.backend = FakeProc(alive=False, code=1)
    sup._mark_backend_started()
    svc.ensure_all()
    check("D3 退避期间不重启后端", len(started) == 0, f"starts={len(started)}")

    print()
    print("== E. 去重：同一次死亡只报一次，计数不翻倍 ==")
    reset_state()
    proc = FakeProc(alive=False, code=1)
    sup._mark_backend_started()
    sup._watch_backend_ready(proc)       # 第一次：报告 + 计数
    sup._watch_backend_ready(proc)       # 第二次：不应再计数
    check("E1 计数仍为 1", sup._backend_fail_streak() == 1,
          f"streak={sup._backend_fail_streak()}")

    print()
    print("== F. 就绪后清零失败计数与退避 ==")
    reset_state()
    proc = FakeProc(alive=False, code=1)
    for _ in range(sup._BACKOFF_AFTER):
        sup._mark_backend_started()
        sup._watch_backend_ready(proc)
    check("F1 清零前处于退避", sup._backoff_remaining() > 0)
    sup._mark_backend_ready()
    check("F2 失败计数归零", sup._backend_fail_streak() == 0,
          f"streak={sup._backend_fail_streak()}")
    check("F3 退避解除", sup._backoff_remaining() == 0,
          f"remaining={sup._backoff_remaining()}")

    print()
    if FAILS:
        print(f"{len(FAILS)} 项失败: {', '.join(FAILS)}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
