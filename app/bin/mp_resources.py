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
import subprocess
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
            # 索引必须**恰好是**当前约定的那一版（user.sites.<RESOURCE_FLAG>.bin）。
            # 只判断 "user.sites.*.bin" 会放过历史版本：上游资源仓库同时分发
            # user.sites.bin / user.sites.v2.bin / user.sites.v3.bin，站点扩展只认与
            # 自己匹配的那一份。若拿着一份 v2 索引去喂 v3 扩展，站点列表同样是空的
            # （表现为"站点认证"页 No data available），但检查却报"资源完整"，
            # 于是修复逻辑被跳过、故障永远无法自愈。
            if p.name == f"{DATA_PREFIX}{RESOURCE_FLAG}{DATA_SUFFIX}":
                have_data = True
            elif p.name in wanted:
                have_native = True
    missing = []
    if not have_data:
        missing.append(f"{DATA_PREFIX}{RESOURCE_FLAG}{DATA_SUFFIX}")
    if not have_native:
        missing.extend(sorted(wanted))
    return missing


def stale_index_files(res_dir: Path) -> list:
    """返回资源目录里存在的、非当前版本的站点索引文件。

    这些文件不会被使用（扩展只认 user.sites.<RESOURCE_FLAG>.bin），留着的唯一
    后果是干扰排查：目录看起来"有索引"，实际解不出站点。修复时顺手清掉。
    """
    if not res_dir.is_dir():
        return []
    current = f"{DATA_PREFIX}{RESOURCE_FLAG}{DATA_SUFFIX}"
    out = []
    for p in res_dir.iterdir():
        if not p.is_file():
            continue
        if p.name == current:
            continue
        if p.name.startswith(DATA_PREFIX) and p.suffix == DATA_SUFFIX:
            out.append(p)
    return out


def native_extension_path(res_dir: Path):
    """返回当前解释器可用的 sites 原生扩展路径（不存在返回 None）。"""
    wanted = wanted_native_names()
    if not res_dir.is_dir():
        return None
    for p in res_dir.iterdir():
        if p.is_file() and p.name in wanted:
            return p
    return None


def native_loadable(res_dir: Path, python_bin: str) -> bool:
    """在子进程里真实 import 一次 sites 扩展，判断它是否**可加载**。

    为什么不能只看文件名：`sites.cpython-314-x86_64-linux-gnu.so` 存在且 ABI
    标签正确，不代表它能被当前解释器加载。原生扩展加载失败（缺共享库、构建
    目标不符、符号缺失）在 Python 里同样表现为
    "ModuleNotFoundError: No module named 'app.application.site.sites'"
    —— 与"文件不存在"完全同形，只看文件名会把这种情况误判成"资源完整"而
    跳过修复，应用则永远起不来。

    返回 True 表示可加载；False 表示确实加载不了。无法判定（解释器缺失等）
    时返回 True，避免把不确定当成故障去反复搬运文件。
    """
    ext = native_extension_path(res_dir)
    if ext is None:
        return False
    if not python_bin or not os.path.isfile(python_bin):
        return True  # 判不了，不阻塞
    # res_dir 形如 <mp>/app/application/site → 上游包根是 <mp>
    # （RESOURCE_SUBDIRS[0] == ("application", "site")，再上溯一级 "app"）
    mp_src = res_dir.parent.parent.parent
    try:
        proc = subprocess.run(
            [python_bin, "-c", "import app.application.site.sites"],
            cwd=str(mp_src),
            env={**os.environ, "PYTHONPATH": str(mp_src)},
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return True  # 探测本身失败，不据此判定资源损坏
    if proc.returncode == 0:
        return True
    detail = (proc.stdout or b"").decode("utf-8", errors="replace").strip()
    if detail:
        log("原生扩展加载失败：")
        for line in detail.splitlines()[-8:]:
            log("  " + line)
    return False


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


def _purge_stale_indexes(res_dir: Path) -> int:
    """删除不匹配当前版本的站点索引，返回删除数量。

    这些文件永远不会被 sites 扩展读取（它只认 user.sites.<RESOURCE_FLAG>.bin），
    删除是安全的；保留它们只会掩盖"当前版索引其实缺失"这一事实。
    """
    removed = 0
    for stale in stale_index_files(res_dir):
        try:
            stale.unlink()
            log(f"  清理过期的站点索引 {stale.name}"
                f"（仅使用 {DATA_PREFIX}{RESOURCE_FLAG}{DATA_SUFFIX}）")
            removed += 1
        except OSError:
            pass
    return removed


def repair(mp_src: Path, python_bin: str = "") -> int:
    res_dir = resolve_resource_dir(mp_src, create=True)
    try:
        rel = res_dir.relative_to(mp_src)
    except ValueError:
        rel = res_dir
    missing = missing_items(res_dir)
    # 文件齐全不代表能用：原生扩展可能"在但加载不了"（缺共享库/构建目标不符），
    # 这种情况 Python 同样报 ModuleNotFoundError。必须真实 import 一次才能发现。
    broken = False
    if not missing:
        if native_loadable(res_dir, python_bin):
            log(f"站点资源完整且可加载: {rel}")
            # 即便无需修复，也要清掉不匹配的历史索引：它们永远不会被读取，
            # 留着只会让排查时误以为"索引在位"。
            _purge_stale_indexes(res_dir)
            return 0
        log(f"站点资源文件齐全但无法加载，尝试覆盖修复: {rel}")
        broken = True

    if not broken:
        log(f"站点资源缺失: {rel} 缺 {', '.join(missing)}")
    wanted = wanted_native_names()
    current_index = f"{DATA_PREFIX}{RESOURCE_FLAG}{DATA_SUFFIX}"
    copied = 0
    # 可加载性失败时，本地那份已经是坏的，必须允许从备份覆盖它
    overwrite = broken
    for src in source_dirs(mp_src):
        if src.resolve() == res_dir.resolve():
            continue
        for p in sorted(src.iterdir()):
            if not p.is_file():
                continue
            # 只搬"当前版本"的索引：历史版本（user.sites.bin / user.sites.v2.bin）
            # 搬过去也没用，反而让目录看起来已有索引、掩盖真正缺的那一份。
            is_data = p.name == current_index
            is_native = p.name in wanted
            if not (is_data or is_native):
                continue
            dst = res_dir / p.name
            if dst.exists() and not overwrite:
                continue
            try:
                shutil.copy2(str(p), str(dst))
            except OSError as e:
                log(f"  复制失败 {p.name}: {e}")
                continue
            log(f"  从 {src.parent.name}/{src.name} 恢复 {p.name}")
            copied += 1
        if not missing_items(res_dir):
            # 缺失项补齐后，若原本是"能加载"的诉求，还要再验一次
            if not overwrite or native_loadable(res_dir, python_bin):
                break
            # 这份来源依然是坏的，继续找下一个来源覆盖
            continue

    still = missing_items(res_dir)
    if still:
        log(f"修复未完成，仍缺: {', '.join(still)}")
        log("提示: 资源文件在本机找不到，通常需要重新安装应用包（fpk）")
        return 1
    if overwrite and not native_loadable(res_dir, python_bin):
        log("修复未完成：所有可用来源的 sites 原生扩展均无法加载")
        log("提示: 本机资源全部不可用，通常需要重新安装应用包（fpk）")
        return 1
    # 清掉不匹配的历史索引：它们不会被使用，留着只会让排查时误以为索引在位。
    _purge_stale_indexes(res_dir)
    log(f"修复完成，补回 {copied} 个文件到 {rel}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="MoviePilot 站点资源检查/修复")
    parser.add_argument("--check", action="store_true", help="只检查不修复")
    parser.add_argument("--mp-src", default=None, help="后端源码目录（默认取 $MP_SRC）")
    parser.add_argument("--python", default=None,
                        help="用于验证扩展可加载性的解释器（默认取 $APP_PYTHON）")
    args = parser.parse_args()

    mp_src = Path(args.mp_src or os.environ.get("MP_SRC") or "").resolve()
    if not mp_src.is_dir():
        log(f"错误: 后端源码目录不存在: {mp_src}")
        return 2

    # 校验"能否 import"必须用**应用自己的解释器**：系统 python 与自带 3.14 的
    # ABI 不同，用错解释器会把好扩展误判成坏的。
    python_bin = args.python or os.environ.get("APP_PYTHON") or ""
    if not python_bin:
        candidate = mp_src.parent / "python" / "bin" / "python"
        if candidate.is_file():
            python_bin = str(candidate)

    if args.check:
        res_dir = resolve_resource_dir(mp_src)
        missing = missing_items(res_dir)
        if missing:
            log(f"缺失: {', '.join(missing)}")
            return 1
        if not native_loadable(res_dir, python_bin):
            log("原生扩展存在但无法加载")
            return 1
        log("OK")
        return 0
    return repair(mp_src, python_bin)


if __name__ == "__main__":
    sys.exit(main())
