#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
站点资源（MoviePilot-Resources）在位性检查与修复
================================================
MoviePilot 的 sites 模块是 Cython 扩展 + 索引数据，不在主仓库里，由
MoviePilot-Resources 单独分发，打包/更新时回填进后端源码。这个模块在
import 阶段就被 app 各处引用，缺了后端根本起不来，典型报错：

    ModuleNotFoundError: No module named 'app.application.site.sites'
    ModuleNotFoundError: No module named 'app.helper.sites'

更麻烦的是这个目录随上游重构搬过家（app/helper → app/adapters/network →
app/infrastructure → app/application/site）。老包把资源放在 app/helper，
一旦自更新到 3.0.3+，新代码要的是 app/application/site，于是"更新成功但
起不来"。本脚本就是这条缝的兜底：

  - 按当前源码结构解析出应当存放资源的目录
  - 缺什么就从历史目录、自更新备份（.mp-backup/*）里找回并复制过去
  - 只认与当前解释器 ABI 匹配的 sites 原生扩展，绝不复制跑不了的变体

纯标准库、只读不写业务数据、可重复执行（幂等）。

用法：
    python mp_resources.py --check    # 只检查，缺失时退出码 1
    python mp_resources.py            # 检查并修复

环境变量：
    MP_SRC   后端源码目录（${TRIM_APPDEST}/mp）
"""
from __future__ import annotations

import argparse
import os
import platform
import shutil
import sys
from pathlib import Path

RESOURCE_FLAG = "v3"
# 相对 app/ 的候选资源目录，按优先级从新到旧（与上游查找顺序一致）
RESOURCE_SUBDIRS = (
    ("application", "site"),
    ("infrastructure",),
    ("adapters", "network"),
    ("helper",),
)
# 资源仓库产物的文件名特征
DATA_SUFFIX = ".bin"
DATA_PREFIX = "user.sites."
NATIVE_PREFIX = "sites."
NATIVE_SUFFIXES = (".so", ".pyd", ".dylib")


def log(msg: str) -> None:
    print(msg, flush=True)


def existing_resource_dirs(root: Path) -> list:
    """返回 root 下所有存在的候选资源目录（新 → 旧）。"""
    app_dir = Path(root) / "app"
    found = []
    for parts in RESOURCE_SUBDIRS:
        candidate = app_dir
        for part in parts:
            candidate = candidate / part
        if candidate.is_dir():
            found.append(candidate)
    return found


def resolve_resource_dir(root: Path, create: bool = False) -> Path:
    """定位 root 下应当存放资源的目录。"""
    found = existing_resource_dirs(root)
    if found:
        return found[0]
    target = Path(root) / "app"
    for part in RESOURCE_SUBDIRS[0]:
        target = target / part
    if create:
        target.mkdir(parents=True, exist_ok=True)
    return target


def machine_tag() -> str:
    m = platform.machine().lower()
    if m in ("arm64", "aarch64"):
        return "aarch64"
    if m in ("x86_64", "amd64"):
        return "x86_64"
    return m


def wanted_native_names() -> set:
    """返回当前解释器与平台可用的 sites 扩展文件名。"""
    ver = f"{sys.version_info.major}{sys.version_info.minor}"
    if sys.platform == "darwin":
        return {f"sites.cpython-{ver}-darwin.so"}
    if os.name == "nt":
        return {f"sites.cp{ver}-win_amd64.pyd"}
    return {f"sites.cpython-{ver}-{machine_tag()}-linux-gnu.so"}


def missing_items(res_dir: Path) -> list:
    """返回资源目录里缺失的项目名（['user.sites.v3.bin', ...]）。"""
    wanted = wanted_native_names()
    have_data = False
    have_native = False
    if res_dir.is_dir():
        for p in res_dir.iterdir():
            if not p.is_file():
                continue
            if p.name.startswith(DATA_PREFIX) and p.suffix == DATA_SUFFIX:
                have_data = True
            elif p.name in wanted:
                have_native = True
    missing = []
    if not have_data:
        missing.append(f"{DATA_PREFIX}{RESOURCE_FLAG}{DATA_SUFFIX}")
    if not have_native:
        missing.extend(sorted(wanted))
    return missing


def source_dirs(mp_src: Path) -> list:
    """按优先级列出所有可以"借"资源文件的目录。"""
    roots = [mp_src]
    backup_root = mp_src.parent / ".mp-backup"
    if backup_root.is_dir():
        # 新一代备份在前：越新越可能与当前版本匹配
        roots.extend(sorted((p for p in backup_root.iterdir() if p.is_dir()),
                            reverse=True))
    dirs = []
    for root in roots:
        for d in existing_resource_dirs(root):
            if d not in dirs:
                dirs.append(d)
    return dirs


def repair(mp_src: Path) -> int:
    res_dir = resolve_resource_dir(mp_src, create=True)
    try:
        rel = res_dir.relative_to(mp_src)
    except ValueError:
        rel = res_dir
    missing = missing_items(res_dir)
    if not missing:
        log(f"站点资源完整: {rel}")
        return 0

    log(f"站点资源缺失: {rel} 缺 {', '.join(missing)}")
    wanted = wanted_native_names()
    copied = 0
    for src in source_dirs(mp_src):
        if src.resolve() == res_dir.resolve():
            continue
        for p in sorted(src.iterdir()):
            if not p.is_file():
                continue
            is_data = p.name.startswith(DATA_PREFIX) and p.suffix == DATA_SUFFIX
            is_native = p.name in wanted
            if not (is_data or is_native):
                continue
            dst = res_dir / p.name
            if dst.exists():
                continue
            try:
                shutil.copy2(str(p), str(dst))
            except OSError as e:
                log(f"  复制失败 {p.name}: {e}")
                continue
            log(f"  从 {src.parent.name}/{src.name} 恢复 {p.name}")
            copied += 1
        if not missing_items(res_dir):
            break

    still = missing_items(res_dir)
    if still:
        log(f"修复未完成，仍缺: {', '.join(still)}")
        log("提示: 资源文件在本机找不到，通常需要重新安装应用包（fpk）")
        return 1
    log(f"修复完成，补回 {copied} 个文件到 {rel}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="MoviePilot 站点资源检查/修复")
    parser.add_argument("--check", action="store_true", help="只检查不修复")
    parser.add_argument("--mp-src", default=None, help="后端源码目录（默认取 $MP_SRC）")
    args = parser.parse_args()

    mp_src = Path(args.mp_src or os.environ.get("MP_SRC") or "").resolve()
    if not mp_src.is_dir():
        log(f"错误: 后端源码目录不存在: {mp_src}")
        return 2

    if args.check:
        missing = missing_items(resolve_resource_dir(mp_src))
        if missing:
            log(f"缺失: {', '.join(missing)}")
            return 1
        log("OK")
        return 0
    return repair(mp_src)


if __name__ == "__main__":
    sys.exit(main())
