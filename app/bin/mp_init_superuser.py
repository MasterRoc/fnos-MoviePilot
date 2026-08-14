#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MoviePilot fnOS 初始化脚本
===========================
在安装时执行：
  1. 初始化 SQLite 数据库（建表 + alembic 迁移）
  2. 创建/同步超级管理员账号（用户名/密码来自 app.env）

环境变量：
  CONFIG_DIR  必填，MoviePilot 配置目录（含 app.env）
  MP_SRC      必填，MoviePilot 源码根目录（含 app/、version.py）
  LOG_FILE    可选，日志输出文件（默认 stdout）
"""
import os
import sys
import traceback

CONFIG_DIR = os.environ.get("CONFIG_DIR")
MP_SRC = os.environ.get("MP_SRC")
LOG_FILE = os.environ.get("LOG_FILE", "")


def _print(msg):
    line = f"[mp_init_superuser] {msg}"
    print(line, flush=True)
    if LOG_FILE:
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass


def main():
    if not CONFIG_DIR or not MP_SRC:
        _print("ERROR: CONFIG_DIR 和 MP_SRC 环境变量必填")
        sys.exit(2)

    # 把源码根目录加入 sys.path（关键：保证 ROOT_PATH 可解析、version 模块可导入）
    if MP_SRC not in sys.path:
        sys.path.insert(0, MP_SRC)

    _print(f"CONFIG_DIR = {CONFIG_DIR}")
    _print(f"MP_SRC     = {MP_SRC}")
    _print(f"python     = {sys.executable}")

    try:
        # 1. 初始化数据库（建表）
        _print("Step 1: init_db ...")
        from app.db.init import init_db
        init_db()

        # 2. alembic 数据库迁移
        _print("Step 2: update_db (alembic) ...")
        from app.db.init import update_db
        update_db()

        # 3. 创建/同步超级管理员（复用 MoviePilot 官方逻辑）
        _print("Step 3: ensure superuser ...")
        # 官方 sync_superuser_account 函数会自动判断当前 python 与设置是否一致
        # 这里直接调用其内部实现，避免传 runtime_python
        from scripts.local_setup import _ensure_superuser_account_inner
        _ensure_superuser_account_inner()

        _print("OK: MoviePilot initialized successfully")
        sys.exit(0)
    except SystemExit as e:
        raise
    except Exception as e:
        _print(f"ERROR: {e}")
        _print(traceback.format_exc())
        sys.exit(1)


if __name__ == "__main__":
    main()