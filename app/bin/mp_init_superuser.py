#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MoviePilot fnOS 初始化脚本
===========================
在安装 / 修改配置时执行：
  1. 登记事务执行器（MoviePilot V3 的 Oper 层必需）
  2. 初始化 SQLite 数据库（建表 + alembic 迁移）
  3. 创建/同步超级管理员账号（用户名/密码来自 app.env）

环境变量：
  CONFIG_DIR  必填，MoviePilot 配置目录（含 app.env）
  MP_SRC      必填，MoviePilot 源码根目录（含 app/、version.py）
  LOG_FILE    可选，日志输出文件（默认 stdout）

为什么必须手动登记事务执行器：
  MoviePilot V3 起，Oper 层（如 UserOper）不再自己开会话，而是委托
  app.db.uow 中由"组合根"登记的 runner。应用运行时由
  app.startup.composition.database.start_database_runtime 登记；而安装期
  是独立进程、没有 lifespan，必须自己登记，否则任何 Oper 调用都会抛
  RuntimeError("同步事务执行器尚未配置")。官方 scripts/local_setup.py
  的 _apply_local_system_config_inner 用的就是下面这套三行组合。
"""
import os
import secrets
import sys
import traceback

CONFIG_DIR = os.environ.get("CONFIG_DIR")
MP_SRC = os.environ.get("MP_SRC")
LOG_FILE = os.environ.get("LOG_FILE", "")

# 引导阶段自动生成的初始密码（若 app.env 未配置 SUPERUSER_PASSWORD）
GENERATED_PASSWORD = None


def _print(msg):
    line = f"[mp_init_superuser] {msg}"
    print(line, flush=True)
    if LOG_FILE:
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass


def _configure_transaction_runners() -> bool:
    """登记同步/异步事务执行器。返回是否登记成功。

    官方组合根（app.startup.composition.database、local_setup 的配置落库路径）
    都按这个顺序登记：Session 工厂 -> TransactionalWriteRunner -> 登记到 uow。
    """
    try:
        from app.db.session import SessionFactory, async_session_scope
        from app.db.uow import configure_transaction_runners
        from app.db.adapters.transaction import TransactionalWriteRunner
    except Exception as exc:
        _print(f"WARN: 事务执行器组件不可用（{exc}），将按旧版路径继续")
        return False

    transaction_runner = TransactionalWriteRunner(
        sync_session=SessionFactory,
        async_session=async_session_scope,
    )
    configure_transaction_runners(
        sync=transaction_runner.sync,
        async_=transaction_runner.async_,
    )
    _print("事务执行器已登记（sync + async）")
    return True


def _bootstrap_superuser_password() -> None:
    """首次初始化时先准备管理员密码，再交给 Alembic 基础迁移消费。

    与官方 _sync_superuser_account_inner 的 before_alembic 回调一致：
    未配置 SUPERUSER_PASSWORD 且用户不存在时生成随机密码。
    """
    global GENERATED_PASSWORD
    try:
        from scripts.local_setup import _prepare_superuser_password_for_bootstrap
    except Exception as exc:
        _print(f"WARN: 跳过初始密码引导（{exc}）")
        return
    try:
        generated = _prepare_superuser_password_for_bootstrap()
    except Exception as exc:
        # 密码引导失败不应阻断建表迁移：账号创建阶段仍会兜底生成密码
        _print(f"WARN: 初始密码引导失败（{exc}）")
        return
    if generated:
        GENERATED_PASSWORD = generated
        _print(f"超级管理员初始密码（自动生成）：{generated}")


def _prepare_database() -> None:
    """建表 + Alembic 迁移。

    V3 起官方入口是 app.startup.initializers.database.prepare_database：
    它先校验 revision 链，必要时"先迁移再建表"，避免旧库（如 v2 升级上来）
    提前 create_all 导致结构冲突，并支持迁移前备份。
    旧版本没有该模块时回退到 init_db / update_db。
    """
    try:
        from app.startup.initializers.database import prepare_database
    except Exception:
        prepare_database = None

    if prepare_database is not None:
        _print("Step 1: prepare_database（建表 + alembic 迁移）...")
        prepare_database(before_alembic=_bootstrap_superuser_password)
        return

    _print("Step 1: init_db ...")
    from app.db.init import init_db
    init_db()
    _bootstrap_superuser_password()
    _print("Step 2: update_db (alembic) ...")
    from app.db.init import update_db
    update_db()


def _ensure_superuser_direct() -> bool:
    """直接在同步会话里创建/同步超级管理员，返回是否执行成功。

    不用官方 _ensure_superuser_account_inner 作为主路径的原因：其"更新"分支
    调用 user.update(user_oper._db, ...)，而 UserOper() 未传会话时 _db 为 None，
    已存在用户时会抛 AttributeError（NoneType 没有 add）。fnOS 修改配置时
    用户已存在，必踩该分支，因此这里自己按同样语义（补 is_active/is_superuser、
    同步密码）实现，创建/更新两条路径都可靠。
    """
    try:
        from sqlalchemy import select

        from app.application.security.token import get_password_hash
        from app.db.models.user import User
        from app.db.session import SessionFactory
        from app.runtime.config import settings
    except Exception as exc:
        _print(f"WARN: 直连建号所需模块不可用（{exc}）")
        return False

    username = str(getattr(settings, "SUPERUSER", "") or "").strip()
    if not username:
        raise RuntimeError("未配置 SUPERUSER（app.env），无法创建超级管理员")
    password = str(getattr(settings, "SUPERUSER_PASSWORD", "") or "").strip()

    with SessionFactory() as session:
        user = (
            session.execute(select(User).where(User.name == username))
            .scalars()
            .first()
        )
        if user is None:
            init_password = password or secrets.token_urlsafe(16)
            session.add(
                User(
                    name=username,
                    email="admin@movie-pilot.org",
                    hashed_password=get_password_hash(init_password),
                    is_active=True,
                    is_superuser=True,
                    avatar="",
                )
            )
            session.commit()
            _print(f"已创建超级管理员用户：{username}")
            if not password:
                _print(f"超级管理员初始密码：{init_password}")
            return True

        changed = []
        if not user.is_active:
            user.is_active = True
            changed.append("已启用")
        if not user.is_superuser:
            user.is_superuser = True
            changed.append("已提升为超级管理员")
        if password:
            user.hashed_password = get_password_hash(password)
            changed.append("已同步密码")

        if changed:
            session.commit()
            _print(f"超级管理员账号 {username}：{'，'.join(changed)}")
        else:
            _print(f"已确认超级管理员账号：{username}")
    return True


def _ensure_superuser_official() -> None:
    """兜底：走官方 scripts.local_setup 的实现。"""
    from scripts.local_setup import _ensure_superuser_account_inner

    _ensure_superuser_account_inner()


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
        # 1. 登记事务执行器（必须在任何 Oper / 建号操作之前）
        _print("Step 0: configure transaction runners ...")
        _configure_transaction_runners()

        # 2. 初始化数据库（建表 + alembic 迁移）
        _prepare_database()

        # 3. 创建/同步超级管理员
        _print("Step 3: ensure superuser ...")
        try:
            done = _ensure_superuser_direct()
        except Exception as exc:
            _print(f"WARN: 直连建号失败（{exc}），回退官方实现")
            done = False
        if not done:
            _ensure_superuser_official()

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
