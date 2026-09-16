#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MoviePilot fnOS 应用 跨平台构建脚本（推荐，Windows / Linux / macOS 通用）
==========================================================================
功能：
  1. 下载 MoviePilot V3 后端源码与前端 dist，统一收敛到 .local-build/
  2. （可选 --with-runtime）准备自带 Python 运行时与全部依赖到 .local-build/python
  3. 在 .local-build/pkg/ 组装干净的应用目录树（只含进包内容）
  4. 按开发机平台自动下载 fnpack 并打包
  5. 产物命名 moviepilot-<version>.fpk

设计（参照 fnos-transmission）：
  - 所有下载/解压/构建产物统一放在 .local-build/（已 gitignore，不入库）
  - 打包前把仓库源码(cmd/config/wizard/manifest/图标/app 源码)与构建产物
    一起组装到 .local-build/pkg/，最后 cd pkg 用 fnpack 打包
  - 打包目录里只有该进包的内容，项目根目录不残留任何构建产物

用法：
  python build.py                  # 默认
  python build.py --force          # 强制重新下载
  python build.py --clean          # 构建前清理 .local-build
  python build.py --skip-mp        # 跳过下载后端源码
  python build.py --skip-fe        # 跳过下载前端
  python build.py --arch arm64     # 显式指定目标架构（用于裁剪 sites 原生变体）
  python build.py --with-runtime --arch arm64
                                   # 打包自带 Python 3.14 运行时与全部依赖
  python build.py --with-runtime --no-build
                                   # 严格模式：禁止源码构建，只用预编译 wheel

自带 Python 运行时（--with-runtime）：
  MoviePilot V3 的 pyproject.toml 声明 requires-python >=3.14，而 fnOS 应用中心
  只提供 python312，所以应用必须自带解释器（官方 docker/Dockerfile 同样自带
  /opt/python）。做法是下载 python-build-standalone 的 CPython 3.14，用 uv 按
  uv.lock 把依赖直接装进它的 site-packages —— 不用 venv，因为 venv 的 bin/python
  是指向构建机绝对路径的符号链接，打进包搬到 NAS 必然失效。整套 app/python/
  是可重定位的，安装时无需在 NAS 上做任何二次安装。

  构建期有四道自检，任何一道不过就终止（宁可构建失败，也不打出能装不能跑的包）：
    1. 依赖自检   —— 用自带解释器实际 import fastapi/uvicorn/sqlalchemy/pydantic/orjson
    2. glibc 审计 —— wheel 标签的 manylinux 基线不得高于 fnOS(Debian 12, glibc 2.36)；
                     PBS 不自带 _manylinux 策略，pip/uv 会按 runner 的 glibc 2.39 选包，
                     所以解析阶段就用 --python-platform 把选择限制在 manylinux_2_36 内
    3. 源码审计   —— 允许源码构建（anitopy / pinyin2hanzi 等纯 Python 包没有 wheel），
                     但凡是本地编译出 .so 的，一律终止：它链接的是 runner 的 glibc
    4. 路径断言   —— python/bin/python3、bin/python、lib/python3.14/site-packages 必须存在

  仅 Linux/macOS 可用（无法交叉准备 Linux 运行时）。缺省不打包运行时，安装时
  退回 fnOS python312 在线安装依赖。

目标架构（--arch）：
  MoviePilot-Resources 内置 python311-314 × linux-amd64/aarch64/darwin/win 的
  全部 sites 原生变体（约 31M）。打包时会按目标架构只保留匹配的一个，减体积
  约 28M。--arch 缺省时取构建机架构；Windows 上缺省则不裁剪（保留全部变体），
  因为 fnpack 在 Windows 上无法产出与目标 NAS 绑定的包，宁可包大也不打出
  缺 sites 模块的坏包（该模块所在目录随上游重构变过，见 fetch_resources）。

说明：
  本应用为 Python 后端 + 预编译前端，无需交叉编译原生二进制、无需 npm 构建。
  外部资源下载走代理降级（gh-proxy.com / ghfast.top / 直连）。
"""
import os
import sys
import json
import shutil
import argparse
import hashlib
import platform
import re
import subprocess
import urllib.request
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
BUILD_DIR = PROJECT_DIR / ".local-build"      # 全部构建产物/缓存（不入库）
TOOLS_DIR = BUILD_DIR / "tools"               # fnpack 工具
PKG_DIR = BUILD_DIR / "pkg"                   # 组装后的打包目录（临时，打包后清理）
# 下载/构建产物统一放 .local-build 下，不污染项目根
MP_DIR = BUILD_DIR / "mp"                     # 后端源码
FE_DIR = BUILD_DIR / "frontend"               # 前端 dist
PYTHON_DIR = BUILD_DIR / "python"             # 自带 Python 运行时 + 依赖（--with-runtime 时）
VERSION_FILE = BUILD_DIR / "versions.json"

FNPACK_VERSION = "1.2.3"
MAIN_PROXY = "https://gh-proxy.com/"
ALT_PROXY = "https://ghfast.top/"

# 自带 Python 运行时：MoviePilot V3 的 pyproject 要求 requires-python >=3.14，
# 而 fnOS 应用中心只提供 python312，所以必须自带解释器（官方 Docker 镜像同样自带
# /opt/python）。运行时版本与打包产物必须一致，sites 原生变体也按它来挑。
PYTHON_VERSION = "3.14"
PYTHON_FULL_VERSION = "3.14.7"
# python-build-standalone 的发行版 tag 与资产命名（固定以保证构建可复现；
# 资源下架时改这两个常量即可，报错信息里会提示）
PBS_TAG = "20260901"
PBS_REPO = "astral-sh/python-build-standalone"
PBS_ASSET = "cpython-" + PYTHON_FULL_VERSION + "+" + PBS_TAG + "-{abi}-unknown-linux-gnu-install_only_stripped.tar.gz"
PBS_ASSET_ARCH = {"amd64": "x86_64", "arm64": "aarch64"}
# pyproject.toml 中的运行时依赖组（另见官方 Dockerfile 的 uv sync --group）
RUNTIME_GROUP = "runtime-standard"
# 目标系统 glibc 基线：fnOS 基于 Debian 12（bookworm），glibc 2.36。
# 用 uv 的 --python-platform 把 wheel 选择限制在该基线内，从源头杜绝
# "在 glibc 2.39 的 runner 上装到 manylinux_2_39 的 wheel、搬到 NAS 起不来"。
GLIBC_BASELINE = (2, 36)
UV_PLATFORM = {
    "amd64": f"x86_64-manylinux_{GLIBC_BASELINE[0]}_{GLIBC_BASELINE[1]}",
    "arm64": f"aarch64-manylinux_{GLIBC_BASELINE[0]}_{GLIBC_BASELINE[1]}",
}

# 打进包的仓库源码目录（相对项目根），会被组装进 pkg/app 及 pkg/
SRC_DIRS = ["cmd", "config", "wizard"]
SRC_APP_DIRS = ["bin", "ui"]


def log(msg):
    print(msg)


# ---------------------------------------------------------------------------
# 平台与 fnpack 选择
# ---------------------------------------------------------------------------
def get_platform():
    s = platform.system().lower()
    if s.startswith("win"):
        return "windows"
    if s.startswith("darwin"):
        return "darwin"
    return "linux"


def get_platform_arch():
    m = platform.machine().lower()
    if m in ("aarch64", "arm64", "arm", "armv8l"):
        return "arm64"
    return "amd64"


def get_fnpack_url():
    plat = get_platform()
    if plat == "windows":
        arch = "amd64"
    elif plat == "darwin":
        arch = get_platform_arch()
    else:
        arch = "arm" if get_platform_arch() == "arm64" else "amd64"
    return f"https://static2.fnnas.com/fnpack/fnpack-{FNPACK_VERSION}-{plat}-{arch}"


def fnpack_bin_name():
    return "fnpack.exe" if get_platform() == "windows" else "fnpack"


# ---------------------------------------------------------------------------
# 版本读取
# ---------------------------------------------------------------------------
def get_app_version():
    """从 manifest 读取 version，避免与 manifest 不一致（唯一版本来源）"""
    manifest_file = PROJECT_DIR / "manifest"
    if not manifest_file.exists():
        log("错误: 未找到 manifest 文件")
        sys.exit(1)
    for line in manifest_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("version") and "=" in line:
            return line.split("=", 1)[1].strip()
    log("错误: manifest 中未找到 version 字段")
    sys.exit(1)


def get_runtime_pyver():
    """返回自带运行时的 Python 版本号，如 "314"。

    刻意**不再**从 manifest 的 install_dep_apps 读取：那声明的是 fnOS 提供的
    运行时（只有 python312），而本应用自带 Python 3.14 运行。sites 原生变体
    必须按自带版本挑，否则会选出 ABI 不匹配的 .so。
    """
    return PYTHON_VERSION.replace(".", "")


def get_frontend_version():
    """从后端 version.py 的 FRONTEND_VERSION 自动读取前端版本，保证前端版本与后端要求始终一致。

    后端 version.py 是 MoviePilot 后端版本与前端版本的"权威"来源，动态读取避免写死脱节。
    读取失败时回退到 v3.0.0。
    """
    version_file = MP_DIR / "version.py"
    try:
        if version_file.exists():
            for line in version_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("FRONTEND_VERSION") and "=" in line:
                    return line.split("=", 1)[1].strip().strip("'\"")
    except Exception as e:
        log(f"警告: 读取后端 version.py 失败，使用默认前端版本: {e}")
    return "v3.0.0"


def get_backend_version():
    """从 GitHub API 动态获取 MoviePilot 后端最新 release tag（如 v3.0.0）。

    优先按稳定 release tag 拉取，避免写死 v3 开发分支导致版本漂移。
    无法访问时回退到 v3 分支。返回 (tag 或分支标识, 是否为 tag)。
    """
    try:
        api_url = "https://api.github.com/repos/jxxghp/MoviePilot/releases/latest"
        req = urllib.request.Request(api_url, headers={"User-Agent": "fnos-build"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        tag = (data or {}).get("tag_name")
        if tag:
            log(f"==> 后端最新 release: {tag}")
            return tag, True
    except Exception as e:
        log(f"警告: 获取后端最新 release 失败，回退到 v3 分支: {e}")
    return "v3", False


# ---------------------------------------------------------------------------
# 下载（直连 -> 代理降级）
# ---------------------------------------------------------------------------
def download(url, out_file, force=False):
    """下载顺序：直连 -> gh-proxy -> ghfast，任一成功即返回。

    先写 .part 临时文件、成功后原子改名：直接写目标文件时，中途失败的
    残缺文件会在下次构建被"已存在且非空"检查误判为完整产物。
    """
    out_file = Path(out_file)
    if out_file.exists() and out_file.stat().st_size > 0 and not force:
        return True
    tmp = out_file.with_name(out_file.name + ".part")
    urls = [url, f"{MAIN_PROXY}{url}", f"{ALT_PROXY}{url}"]
    for i, u in enumerate(urls):
        tag = "直连" if i == 0 else ("加速(gh-proxy)" if i == 1 else "加速(ghfast)")
        log(f"  [{tag}] {u}")
        try:
            req = urllib.request.Request(u, headers={"User-Agent": "MoviePilot-fnOS-build"})
            with urllib.request.urlopen(req, timeout=60) as resp, open(tmp, "wb") as f:
                shutil.copyfileobj(resp, f)
            tmp.replace(out_file)
            return True
        except Exception as e:
            log(f"  {tag}失败，尝试下一个: {e}")
            try:
                tmp.unlink()
            except OSError:
                pass
    return False


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# 下载后端源码
# ---------------------------------------------------------------------------
def fetch_moviepilot(force=False):
    mp_dir = MP_DIR
    # 缓存判断用 pyproject.toml：MoviePilot V3 起不再提供 requirements.txt，
    # 沿用旧判断会导致每次构建都重新下载一遍源码。
    if not force and (mp_dir / "pyproject.toml").exists():
        log("==> MoviePilot 源码已存在，跳过")
        return
    backend_ref, is_tag = get_backend_version()
    src_url = (f"https://github.com/jxxghp/MoviePilot/archive/refs/tags/{backend_ref}.zip"
               if is_tag else
               f"https://github.com/jxxghp/MoviePilot/archive/refs/heads/{backend_ref}.zip")
    log(f"==> 下载 MoviePilot 源码 [{backend_ref}] ...")
    if mp_dir.exists():
        shutil.rmtree(mp_dir)
    zip_path = BUILD_DIR / "moviepilot.zip"
    if not download(src_url, zip_path, force):
        log("下载 MoviePilot 源码失败")
        sys.exit(1)
    shutil.unpack_archive(str(zip_path), str(BUILD_DIR / "mp_src"))
    src = next((BUILD_DIR / "mp_src").iterdir())
    shutil.move(str(src), str(mp_dir))
    shutil.rmtree(str(BUILD_DIR / "mp_src"))
    for d in ("tests", "docs", ".github"):
        p = mp_dir / d
        if p.exists():
            shutil.rmtree(p)
    log("MoviePilot 源码就绪")


# ---------------------------------------------------------------------------
# 下载前端
# ---------------------------------------------------------------------------
def fetch_frontend(force=False):
    fe_dir = FE_DIR
    if not force and (fe_dir / "index.html").exists():
        log("==> 前端已存在，跳过")
        return
    frontend_tag = get_frontend_version()
    log(f"==> 下载 MoviePilot 前端 {frontend_tag} ...")
    if fe_dir.exists():
        shutil.rmtree(fe_dir)
    zip_path = BUILD_DIR / "frontend.zip"
    url = f"https://github.com/jxxghp/MoviePilot-Frontend/releases/download/{frontend_tag}/dist.zip"
    if not download(url, zip_path, force):
        log("下载前端失败")
        sys.exit(1)
    fe_dir.mkdir(parents=True, exist_ok=True)
    shutil.unpack_archive(str(zip_path), str(fe_dir))
    # dist.zip 可能解压出 dist/ 子目录，提升到 frontend/
    dist_sub = fe_dir / "dist"
    if dist_sub.exists():
        for item in dist_sub.iterdir():
            shutil.move(str(item), str(fe_dir / item.name))
        shutil.rmtree(str(dist_sub))
    log("前端就绪")


# ---------------------------------------------------------------------------
# 下载 MoviePilot-Resources 资源包并同步到后端源码的站点资源目录
# MoviePilot V3 的 sites 模块（Cython 扩展 + user.sites 数据）不在主仓库里，
# 由 MoviePilot-Resources 单独分发。缺失会导致
# "No module named 'app.<...>.sites'"，后端在 import 阶段就崩，无法启动。
#
# 该目录随上游重构搬过家，写死任何一个都会在某天打出起不来的包：
#   app/helper            （早期 V3）
#   app/adapters/network
#   app/infrastructure
#   app/application/site  （>= 3.0.3，当前）
# 所以一律按"上游源码里实际存在哪个目录"来定位，顺序与上游
# app.adapters.system.update._resource_source_dir() 的查找顺序保持一致。
# ---------------------------------------------------------------------------
RESOURCE_FLAG = "v3"
RESOURCES_ZIP = "https://github.com/jxxghp/MoviePilot-Resources/archive/refs/heads/main.zip"
# 相对 app/ 的候选资源目录，按优先级从新到旧
RESOURCE_SUBDIRS = (
    ("application", "site"),
    ("infrastructure",),
    ("adapters", "network"),
    ("helper",),
)


def _resource_candidate_dirs(mp_dir):
    """返回后端源码里所有存在的候选资源目录（按优先级从新到旧）。"""
    app_dir = Path(mp_dir) / "app"
    found = []
    for parts in RESOURCE_SUBDIRS:
        candidate = app_dir
        for part in parts:
            candidate = candidate / part
        if candidate.is_dir():
            found.append(candidate)
    return found


def resolve_resource_dir(mp_dir, create=False):
    """定位后端源码应当接收资源包产物的目录。

    优先用上游源码里真实存在的最"新"目录；一个都找不到时以当前约定
    （app/application/site）为准 —— 此时源码多半还没解开，按新约定建目录
    不会把文件放错地方。
    """
    found = _resource_candidate_dirs(mp_dir)
    if found:
        return found[0]
    target = Path(mp_dir) / "app"
    for part in RESOURCE_SUBDIRS[0]:
        target = target / part
    if create:
        target.mkdir(parents=True, exist_ok=True)
    return target


def fetch_resources(force=False):
    helper_dir = resolve_resource_dir(MP_DIR, create=True)
    marker = helper_dir / f"user.sites.{RESOURCE_FLAG}.bin"
    if not force and marker.exists():
        log("==> 资源包已就绪，跳过")
        return
    log("==> 下载 MoviePilot-Resources 资源包 ...")
    zip_path = BUILD_DIR / "resources.zip"
    if not download(RESOURCES_ZIP, zip_path, force):
        log("下载资源包失败")
        sys.exit(1)
    extract_dir = BUILD_DIR / "resources_src"
    if extract_dir.exists():
        shutil.rmtree(extract_dir)
    shutil.unpack_archive(str(zip_path), str(extract_dir))
    src_dir = extract_dir / "MoviePilot-Resources-main" / f"resources.{RESOURCE_FLAG}"
    if not src_dir.exists():
        log(f"资源包中未找到 resources.{RESOURCE_FLAG} 目录")
        sys.exit(1)
    helper_dir.mkdir(parents=True, exist_ok=True)
    copied = []
    for source in sorted(src_dir.iterdir()):
        if source.is_dir():
            continue
        shutil.copy2(str(source), str(helper_dir / source.name))
        copied.append(source.name)
    if not copied:
        log("资源目录中未找到可复制文件")
        sys.exit(1)
    shutil.rmtree(str(extract_dir))
    try:
        rel = helper_dir.relative_to(MP_DIR)
    except ValueError:
        rel = helper_dir
    log(f"资源同步完成，共 {len(copied)} 个文件到 {rel}")


# ---------------------------------------------------------------------------
# 自带 Python 运行时 + 依赖（实现安装时完全不联网，且不依赖 fnOS 的 Python 版本）
#
# 为什么自带解释器：MoviePilot V3 的 pyproject.toml 声明 requires-python >=3.14，
# 而 fnOS 应用中心只提供 python312。官方 docker/Dockerfile 的做法同样是自带
# /opt/python 与 /opt/venv。
#
# 为什么不用 venv：venv 的 bin/python 是指向基础解释器的符号链接，pyvenv.cfg 里
# 还写着构建机的绝对路径，打进包搬到 NAS 必然失效。这里直接把依赖装进自带解释器
# 的 site-packages —— python-build-standalone 的发行版是可重定位的（sys.prefix
# 由二进制位置推导），整个 app/python/ 目录搬到哪儿都能跑，安装时无需二次操作。
#
# 仅支持在 Linux/macOS 上准备 Linux 运行时；Windows 无法交叉准备。
# ---------------------------------------------------------------------------
PIP_MIRRORS = [
    "https://mirrors.aliyun.com/pypi/simple/",
    "https://mirrors.cloud.tencent.com/pypi/simple/",
    "https://pypi.tuna.tsinghua.edu.cn/simple/",
    "https://pypi.org/simple/",
]


def _size_mb(path):
    p = Path(path)
    if p.is_file():
        return p.stat().st_size / 1024 / 1024
    total = 0
    for dirpath, _, filenames in os.walk(p):
        for f in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, f))
            except OSError:
                pass
    return total / 1024 / 1024


def _site_packages_dir(venv_dir):
    sp = Path(venv_dir) / "lib"
    if get_platform() == "windows":
        sp = Path(venv_dir) / "Lib"
    if not sp.exists():
        return None
    for ver in sp.iterdir():
        s = ver / "site-packages"
        if s.exists():
            return s
    return None


def _log_site_packages_top(venv_dir, n=15):
    site = _site_packages_dir(venv_dir)
    if not site:
        return
    sizes = sorted(((_size_mb(c), c.name) for c in site.iterdir()), reverse=True)
    log(f"  site-packages 体积 top{n}:")
    for mb, name in sizes[:n]:
        log(f"    {mb:8.1f} MB  {name}")


def _remove_pkg_tests(venv_dir):
    """删除 site-packages 内各包自带的 tests/testing 目录，运行时不需要。"""
    removed = 0
    for dirpath, dirnames, _ in os.walk(venv_dir, topdown=True):
        keep = []
        for d in dirnames:
            if d in ("tests", "testing"):
                shutil.rmtree(os.path.join(dirpath, d), ignore_errors=True)
                removed += 1
            else:
                keep.append(d)
        dirnames[:] = keep
    log(f"==> 已移除 {removed} 个 tests/testing 目录")


def _strip_so_binaries(venv_dir):
    """strip 掉 .so 的调试符号。release wheel 常带大量调试段，去除后运行时无影响。"""
    strip = shutil.which("strip")
    if not strip:
        log("警告: 未找到 strip，跳过符号裁剪")
        return
    files = []
    for dirpath, _, filenames in os.walk(venv_dir):
        for f in filenames:
            if f.endswith(".so") or ".so." in f:
                files.append(os.path.join(dirpath, f))
    for i in range(0, len(files), 200):
        subprocess.run([strip, "--strip-unneeded", *files[i:i + 200]],
                       check=False, capture_output=True)
    log(f"==> 已 strip {len(files)} 个 .so 文件的调试符号")


def _ensure_uv():
    """返回 uv 可执行文件路径；没有就装到 .local-build/tools/uvvenv 里（不动系统环境）。"""
    uv = shutil.which("uv")
    if uv:
        log(f"==> 使用系统 uv: {uv}")
        return uv
    is_win = get_platform() == "windows"
    tool_venv = TOOLS_DIR / "uvvenv"
    uv_bin = tool_venv / ("Scripts/uv.exe" if is_win else "bin/uv")
    if uv_bin.exists():
        return str(uv_bin)
    py = shutil.which("python3") or sys.executable
    log("==> 安装 uv（用于按 uv.lock 安装依赖）...")
    subprocess.run([py, "-m", "venv", str(tool_venv)], check=True)
    pip = tool_venv / ("Scripts/pip.exe" if is_win else "bin/pip")
    for mirror in PIP_MIRRORS:
        host = mirror.split("//")[1].split("/")[0]
        r = subprocess.run([str(pip), "install", "--upgrade", "uv",
                            "-i", mirror, "--trusted-host", host],
                           capture_output=True, text=True)
        if r.returncode == 0 and uv_bin.exists():
            return str(uv_bin)
    log("错误: 安装 uv 失败（无法按 uv.lock 安装依赖）")
    sys.exit(1)


def fetch_python_runtime(target_arch, force=False):
    """下载并解出自带 Python 运行时，返回其根目录（含 bin/python3）。"""
    if get_platform() == "windows":
        log("警告: Windows 上无法准备 Linux Python 运行时，跳过自带运行时")
        return None
    abi = PBS_ASSET_ARCH.get(target_arch or "")
    if not abi:
        log(f"错误: 目标架构 {target_arch!r} 无法映射到 Python 运行时资产")
        sys.exit(1)

    if not force and (PYTHON_DIR / "bin" / "python3").exists():
        log("==> Python 运行时已就绪，跳过下载")
        return PYTHON_DIR

    asset = PBS_ASSET.format(abi=abi)
    # 资产名里的 "+" 必须编码成 %2B：GitHub 的 download 端点会把它当成空格，
    # 直连与代理转发都可能 404（API 返回的 browser_download_url 也是 %2B）。
    url = (f"https://github.com/{PBS_REPO}/releases/download/{PBS_TAG}/"
           f"{asset.replace('+', '%2B')}")
    log(f"==> 下载自带 Python 运行时 CPython {PYTHON_FULL_VERSION} ({abi}) ...")
    tar_path = BUILD_DIR / asset
    if not download(url, tar_path, force):
        log(f"下载 Python 运行时失败: {url}")
        log("     若该资源已下架，请更新 build.py 的 PBS_TAG / PYTHON_FULL_VERSION 常量")
        sys.exit(1)

    if PYTHON_DIR.exists():
        shutil.rmtree(PYTHON_DIR)
    extract = BUILD_DIR / "python_src"
    if extract.exists():
        shutil.rmtree(extract)
    shutil.unpack_archive(str(tar_path), str(extract))
    inner = extract / "python"
    if not inner.is_dir():
        log(f"错误: Python 运行时包结构异常（未找到 python/ 目录）: {tar_path}")
        sys.exit(1)
    shutil.move(str(inner), str(PYTHON_DIR))
    shutil.rmtree(extract, ignore_errors=True)
    log(f"==> Python 运行时就绪: {_size_mb(PYTHON_DIR):.1f} MB")
    return PYTHON_DIR


def export_lock_requirements(uv, mp_src):
    """把 uv.lock 导出成 requirements.txt 形式，作为依赖安装的唯一依据。

    用 --locked（与官方 Dockerfile 的 uv sync --locked 一致）：uv.lock 与
    pyproject.toml 不一致时直接报错，避免"锁文件漂移却静默装出另一套依赖"。

    用 --no-hashes：该清单同时会被打进包（app/mp/requirements.lock.txt），供 NAS 端
    走国内镜像在线补装时使用；带 hash 会让 pip 进入 require-hashes 模式，一旦镜像
    站提供的 wheel 与 PyPI 有字节差异（部分镜像会重打包）就会整体失败。镜像可靠性
    优先于校验强度 —— 主路径（自带运行时）根本不需要在线安装。
    """
    out = BUILD_DIR / "requirements.lock.txt"
    log(f"==> 从 uv.lock 导出锁定依赖清单（group={RUNTIME_GROUP}）...")
    cmd = [uv, "export", "--locked", "--no-emit-project",
           "--no-default-groups", "--group", RUNTIME_GROUP,
           "--no-hashes", "--format", "requirements-txt", "-o", str(out)]
    # 同样隔离 VIRTUAL_ENV / UV_PROJECT_ENVIRONMENT：它们会让 uv 误以为已有项目环境，
    # 从而把导出目标或解析平台换成别的东西。
    env = dict(os.environ)
    for k in ("VIRTUAL_ENV", "CONDA_PREFIX", "UV_PROJECT_ENVIRONMENT"):
        env.pop(k, None)
    r = subprocess.run(cmd, cwd=str(mp_src), capture_output=True, text=True, env=env)
    if r.returncode != 0 or not out.exists():
        log(f"错误: uv export 失败:\n{(r.stderr or r.stdout)[-1500:]}")
        sys.exit(1)
    lines = [ln for ln in out.read_text(encoding="utf-8").splitlines()
             if ln.strip() and not ln.lstrip().startswith("#")]
    log(f"    已导出 {len(lines)} 条依赖")
    return out


def install_deps(python_dir, uv, req_file, no_build=False, python_platform=None):
    """把依赖装进自带解释器的 site-packages（不用 venv，保证目录可整体搬移）。

    默认**不**加 --no-build。一开始用它强制只用预编译 wheel，结果 CI 直接挂在
    anitopy 上：该包（以及 pinyin2hanzi）在 PyPI 上根本没有 wheel，只有 sdist，
    禁止构建会让解析直接失败。官方 Dockerfile 的 uv sync 同样没加 --no-build。

    允许构建带来的风险（在 runner 上现场编译出原生扩展、链接 glibc 2.39）由
    _audit_source_builds() 事后审计兜底：纯 Python 的本地构建放行，带 .so 的
    直接终止构建。想恢复"绝不构建"的严格模式可传 no_build=True。

    为什么必须 --no-deps（关键，踩过坑）：
    uv export 导出的是 uv.lock 的**完整传递闭包**，本不需要再次解析。而 uv pip
    install 默认会重新解析每个包的依赖，此时项目级的 [tool.uv] 设置
    （exclude-dependencies / conflicts）已经丢失 —— 实测就是它把 crcmod 又拉了回来：
    pyproject 明确排除了 oss2 对 crcmod 的依赖（改用 crcmod-plus），但重新解析时
    uv 又按 oss2 的元数据装上了真正的 crcmod。而 crcmod 是 sdist-only 且带 C 扩展，
    于是被现场编译成 .so，直接触发 glibc 风险。--no-deps 让安装严格以锁文件为准，
    既避免了这个问题，也让产物完全可复现。

    关于 --system：python-build-standalone 的 install_only 前缀没有 pyvenv.cfg，属于
    "系统式"环境，直觉上似乎必须加 --system。实测（uv 0.12.15）并非如此 —— 只要用
    --python 显式给出解释器，uv 就直接以该解释器自身的前缀为安装目标，日志为
    "Using Python 3.14.x environment at: <prefix>"，无需 --system。因此这里先按默认
    方式尝试，失败才退回 --system（真 venv 加 --system 会被 uv 拒绝，所以只能兜底）。
    """
    py = python_dir / "bin" / "python3"
    if not py.exists():
        py = python_dir / "bin" / "python"
    if not py.exists():
        log(f"错误: 自带解释器不存在: {python_dir}/bin/python3")
        sys.exit(1)

    base = [uv, "pip", "install", "--python", str(py)]
    if no_build:
        base.append("--no-build")
    if python_platform:
        base += ["--python-platform", python_platform]
    # --no-deps 是必须的，不是优化：详见下方 docstring 的"为什么必须 --no-deps"。
    base += ["--no-deps", "--no-cache", "-r", str(req_file)]
    attempts = [base, base + ["--system"]]

    # 隔离环境干扰：若调用方 shell 里带着 VIRTUAL_ENV，uv 可能优先采用它而不是
    # --python 指定的解释器，导致依赖被装到别处（甚至装到构建机上）。
    env = dict(os.environ)
    env.pop("VIRTUAL_ENV", None)
    env.pop("CONDA_PREFIX", None)

    log("==> 安装 MoviePilot 依赖到自带运行时"
        f"（{'仅用预编译 wheel' if no_build else '优先 wheel，缺失时源码构建'}）...")
    err = ""
    for i, cmd in enumerate(attempts):
        r = subprocess.run(cmd, capture_output=True, text=True, env=env)
        if r.returncode == 0:
            log("==> 依赖安装完成")
            return
        err = r.stderr or r.stdout
        if i == 0 and "--system" in attempts[1]:
            log("    默认方式失败，加 --system 重试")
    log("错误: 依赖安装失败:")
    log(err[-2500:])
    sys.exit(1)


def _audit_wheel_glibc(python_dir, max_glibc=(2, 36)):
    """审计装好的 wheel 的 manylinux 基线，拦截"能装但起不来"的包。

    为什么需要这道检查：python-build-standalone 不自带 _manylinux 兼容策略模块，
    于是 pip / uv 按**运行主机**的 glibc 判定兼容上限。CI runner 是 ubuntu-24.04
    （glibc 2.39），而 fnOS 基于 Debian 12（glibc 2.36）—— 如果某个包提供了
    manylinux_2_39 的 wheel，runner 上会顺利装上，搬到 NAS 却会因
    "version `GLIBC_2.39' not found" 直接崩。wheel 标签里写着目标基线，
    在构建期读出来断言即可，比事后在 NAS 上猜要便宜得多。
    """
    site = _site_packages_dir(python_dir)
    if not site:
        log("警告: 未找到 site-packages，跳过 wheel glibc 审计")
        return
    limit = max_glibc[0] * 100 + max_glibc[1]
    bad, seen = [], 0
    for wheel in sorted(site.glob("*.dist-info/WHEEL")):
        try:
            text = wheel.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        seen += 1
        for tag in re.findall(r"^Tag:\s*(\S+)$", text, re.MULTILINE):
            m = re.search(r"manylinux_(\d+)_(\d+)", tag)
            if m and (int(m.group(1)) * 100 + int(m.group(2))) > limit:
                bad.append((wheel.parent.name, tag))
    if bad:
        log(f"错误: 以下 wheel 要求的 glibc 高于目标系统 {max_glibc[0]}.{max_glibc[1]}"
            "（fnOS / Debian 12），在 NAS 上会因找不到 GLIBC 符号而无法加载：")
        for name, tag in bad[:40]:
            log(f"    {name}  ->  {tag}")
        log("     处理办法：为该包指定 wheel 基线较老的版本（uv 的 --python-platform")
        log("     已经限制在 manylinux_2_36 以内，走到这里说明锁文件里有绕过该限制的来源），")
        log(f"     或确认目标系统 glibc 后调整 build.py 的 GLIBC_BASELINE / max_glibc。")
        sys.exit(1)
    log(f"==> wheel glibc 审计通过（{seen} 个包，基线均不高于 "
        f"{max_glibc[0]}.{max_glibc[1]}）")


def _installed_native_files(info_dir):
    """从 dist-info 的 RECORD 里挑出安装进去的原生扩展（.so）。"""
    rec = Path(info_dir) / "RECORD"
    if not rec.exists():
        return []
    out = []
    try:
        for line in rec.read_text(encoding="utf-8", errors="replace").splitlines():
            path = line.split(",")[0].strip()
            if path.endswith(".so") or ".so." in path:
                out.append(path)
    except OSError:
        pass
    return out


def _audit_source_builds(python_dir):
    """审计"从源码构建"的包，拦住在 runner 上现场编译出来的原生扩展。

    为什么不能 --no-build 一刀切：MoviePilot 有 anitopy、pinyin2hanzi 这类
    **纯 Python 的 sdist-only 依赖**（PyPI 上根本没有 wheel），禁止构建会直接
    让解析失败 —— CI 实测就挂在 anitopy 上。官方 Dockerfile 的 uv sync 也没加
    --no-build。所以这里改成"允许构建、事后审计"：

      * 纯 Python 的本地构建：无害，只记日志放行
      * 带 .so 的本地构建：危险。它链接的是构建机的 glibc（ubuntu runner 为 2.39），
        而 fnOS 基于 Debian 12（glibc 2.36），搬到 NAS 很可能因
        "version `GLIBC_2.39' not found" 崩溃 → 直接终止构建

    判定"本地构建"的依据：本地构建出的 wheel 标签是 cp314-cp314-linux_<arch>
    （不含 manylinux），而 PyPI 上的正规发行版一定带 manylinux / musllinux /
    none-any / macosx / win32 之类的标签。
    """
    site = _site_packages_dir(python_dir)
    if not site:
        log("警告: 未找到 site-packages，跳过源码构建审计")
        return
    released = re.compile(r"manylinux|musllinux|none-any|macosx|win_|win32|win_amd64|android|ios")
    pure, native = [], []
    for info in sorted(site.glob("*.dist-info")):
        wheel = info / "WHEEL"
        if not wheel.exists():
            continue
        try:
            text = wheel.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        tags = re.findall(r"^Tag:\s*(\S+)$", text, re.MULTILINE)
        if not tags or not any("linux_" in t for t in tags):
            continue
        if any(released.search(t) for t in tags):
            continue
        name = info.name.split("-")[0]
        sos = _installed_native_files(info)
        (native if sos else pure).append((name, tags[0], len(sos)))

    if pure:
        log(f"==> 本地源码构建（纯 Python，无害）：{', '.join(n for n, _, _ in pure)}")
    if native:
        log("错误: 以下依赖在构建机上从源码编译出了原生扩展（.so）：")
        for name, tag, n in native:
            log(f"    {name}  ->  {tag}（{n} 个 .so）")
        log("     它们链接的是构建机的 glibc（ubuntu runner 为 2.39），而 fnOS 基于")
        log("     Debian 12（glibc 2.36），搬到 NAS 很可能因 GLIBC_2.39 符号缺失而崩溃。")
        log("     处理办法：为该依赖指定含 cp314 manylinux wheel 的版本，或换成纯 Python 实现。")
        sys.exit(1)
    if not pure and not native:
        log("==> 源码构建审计通过（全部使用预编译 wheel）")


def _smoke_test(python_dir):
    """用自带解释器实际 import 关键依赖，验证装出来的环境真的能跑。

    CI runner 与目标 NAS 同架构（arm64 runner / amd64 runner），所以这里能直接
    执行该解释器。重点覆盖三类：纯 Python（fastapi/uvicorn）、Rust 扩展
    （pydantic_core）、C 扩展（sqlalchemy 的 C 加速）。失败即终止构建。
    """
    py = python_dir / "bin" / "python3"
    if not py.exists() or not os.access(str(py), os.X_OK):
        log(f"警告: 无法执行自带解释器（{py}），跳过依赖自检")
        return
    # anitopy 是 sdist-only 依赖（PyPI 无 wheel），带上它是为了验证"源码构建"
    # 这条新打通的路径确实产出了可导入的包，而不只是装上了文件。
    code = ("import fastapi, uvicorn, sqlalchemy, pydantic, pydantic_core, orjson, anitopy;"
            "print('SMOKE_OK')")
    r = subprocess.run([str(py), "-c", code], capture_output=True, text=True)
    if r.returncode != 0 or "SMOKE_OK" not in r.stdout:
        log("错误: 自带运行时依赖自检失败（关键依赖无法 import）:")
        log((r.stderr or r.stdout)[-1500:])
        sys.exit(1)
    ver = subprocess.run([str(py), "-V"], capture_output=True, text=True)
    log(f"==> 依赖自检通过（{(ver.stdout or ver.stderr).strip()}）")


def build_runtime(target_arch, force=False, no_build=False):
    """准备自带 Python 运行时并把 MoviePilot 依赖装进去。"""
    if get_platform() == "windows":
        log("警告: Windows 上无法准备 Linux 运行时，产物将不含依赖（安装时需在线安装）")
        return False
    if not (MP_DIR / "pyproject.toml").exists():
        log("错误: 缺少 MoviePilot 源码（pyproject.toml），无法准备依赖")
        sys.exit(1)

    marker = BUILD_DIR / ".runtime_ready"
    if not force and marker.exists() and (PYTHON_DIR / "bin" / "python3").exists():
        log("==> 自带运行时与依赖已就绪，跳过")
        return True

    uv = _ensure_uv()
    fetch_python_runtime(target_arch, force)
    req = export_lock_requirements(uv, MP_DIR)
    python_platform = UV_PLATFORM.get(target_arch) if target_arch else None
    install_deps(PYTHON_DIR, uv, req, no_build, python_platform)

    log(f"==> 依赖安装后体积: {_size_mb(PYTHON_DIR):.1f} MB")
    _log_site_packages_top(PYTHON_DIR)

    # 装完先验环境再裁剪：裁剪会动文件，出问题时应先看到"环境本身不完整"。
    _smoke_test(PYTHON_DIR)
    _audit_wheel_glibc(PYTHON_DIR)
    _audit_source_builds(PYTHON_DIR)

    log("==> 裁剪运行时：CPython 测试套件、各包 tests 目录、strip 符号 ...")
    _trim_runtime(PYTHON_DIR)
    _remove_pkg_tests(PYTHON_DIR)
    _strip_so_binaries(PYTHON_DIR)
    log(f"==> 运行时最终体积: {_size_mb(PYTHON_DIR):.1f} MB")

    # cmd/* 依赖 ${TRIM_APPDEST}/python 下的这几个路径，缺一个应用就起不来。
    # 打包前在这里断言，避免"CI 全绿但包里缺解释器"的历史问题重演。
    for rel in ("bin/python3", "bin/python", "lib/python3.14/site-packages"):
        if not (PYTHON_DIR / rel).exists():
            log(f"错误: 自带运行时缺少必要路径: python/{rel}")
            sys.exit(1)

    marker.write_text("ok", encoding="utf-8")
    log("自带运行时打包完成")
    return True


def _trim_runtime(python_dir):
    """裁剪自带运行时里运行时用不到的部分。

    只删 CPython 自带的测试套件（lib/pythonX.Y/test，几十 MB），无功能影响。

    刻意**保留**两样东西：
      - site-packages 与标准库的 __pycache__/*.pyc：它们由同一个 3.14.7 解释器
        生成，magic 号必然匹配，留着能让应用首次启动免去重编译（langchain 之类
        动辄上千个模块，现场编译会让首次启动慢很多）。
      - .dist-info / .egg-info：importlib.metadata 靠它读包版本，删掉会让依赖
        自检与版本展示出错（旧版 _trim_venv 删过，是个坑）。
    """
    if not python_dir.exists():
        return
    removed = 0
    lib = python_dir / "lib"
    if lib.is_dir():
        for ver in lib.iterdir():
            t = ver / "test"
            if t.is_dir():
                shutil.rmtree(t, ignore_errors=True)
                removed += 1
    log(f"==> 运行时裁剪: 移除 {removed} 个 CPython 测试套件目录")


# ---------------------------------------------------------------------------
# fnpack
# ---------------------------------------------------------------------------
def ensure_fnpack(force=False):
    bin_name = fnpack_bin_name()
    fnpack_bin = TOOLS_DIR / bin_name
    if fnpack_bin.exists() and fnpack_bin.stat().st_size > 0 and not force:
        return fnpack_bin
    log("==> 下载 fnpack ...")
    TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    if not download(get_fnpack_url(), fnpack_bin, force):
        log("下载 fnpack 失败")
        sys.exit(1)
    if get_platform() != "windows":
        fnpack_bin.chmod(0o755)
    return fnpack_bin


# ---------------------------------------------------------------------------
# 目标架构解析 + sites 二进制按架构过滤
# ---------------------------------------------------------------------------
# fpk 分架构构建（CI matrix amd64/arm64），资源包只需保留对应的一个变体
SITES_ABI = {"amd64": "x86_64", "arm64": "aarch64"}
# 允许裁剪的编译产物后缀：只删这些，sites.py 之类源码一律保留
SITES_BIN_SUFFIXES = (".so", ".pyd")


def resolve_target_arch(cli_arch=None):
    """确定 fpk 的目标 CPU 架构，返回 "amd64" / "arm64" / None。

    返回 None 表示"无法安全判定目标架构"，调用方应跳过一切按架构裁剪的逻辑
    （保留全部变体），宁可包大也不打出缺模块的坏包。

    - 显式 --arch 最优先（在 Linux/macOS 上为另一架构构建时必须显式指定）；
    - 否则取构建机架构：Linux/macOS 本地构建，架构通常与目标 NAS 一致；
    - Windows 上返回 None：fnpack 在 Windows 上产出的包并未与目标架构绑定
      （build.py 也无法交叉编译 Linux venv），构建机架构不能代表目标 NAS。
    """
    if cli_arch:
        return cli_arch
    if get_platform() == "windows":
        return None
    return get_platform_arch()


def _filter_sites_binaries(pkg_mp_dir, target_arch=None):
    """按目标架构过滤 MoviePilot-Resources 的 sites 编译产物。

    资源包内置 python311-314 × linux-amd64/aarch64/darwin + win 的全部变体
    （约 31M），而 NAS 运行时固定为 manifest install_dep_apps 指定的 Python
    版本、fpk 本就分架构构建，只需保留匹配的一个 .so，可减原始体积约 28M。
    只操作打包副本（pkg），.local-build/mp 缓存保持完整；资源包命名变更时
    跳过过滤并告警，宁可包大也不打出缺模块的坏包。

    target_arch 为 None（无法判定目标架构）时跳过过滤、保留全部变体。
    """
    helper = _resource_candidate_dirs(pkg_mp_dir)
    if not helper:
        return
    helper = helper[0]
    if not target_arch:
        log("警告: 未指定且无法判定目标架构，跳过 sites 裁剪（保留全部变体，包体较大但兼容）")
        return
    abi = SITES_ABI.get(target_arch)
    keep_name = f"sites.cpython-{get_runtime_pyver()}-{abi}-linux-gnu.so" if abi else ""
    if not keep_name or not (helper / keep_name).exists():
        log(f"警告: 未找到目标 sites 变体（{keep_name or '未知架构'}），跳过过滤")
        return
    removed = 0
    removed_mb = 0.0
    for f in sorted(helper.iterdir()):
        # 保留目标变体、.resource-compat 与 user.sites.*.bin 数据文件
        if f.name == keep_name or f.name == ".resource-compat" \
                or f.name.startswith("user.sites."):
            continue
        # 只删编译产物（.so/.pyd）；sites.py 等纯 Python 源码必须保留
        if f.name.startswith("sites.") and f.name.endswith(SITES_BIN_SUFFIXES):
            removed_mb += _size_mb(f)
            f.unlink()
            removed += 1
    log(f"==> sites 过滤: 目标架构 {target_arch}，保留 {keep_name}，"
        f"移除 {removed} 个变体（{removed_mb:.1f} MB）")


# ---------------------------------------------------------------------------
# 组装打包目录（参照 fnos-transmission）
# 在 .local-build/pkg/ 下组装干净的应用目录树，只含该进包的内容。
# 打包目录结构需与 manifest 约定一致：
#   pkg/
#     manifest, ICON.PNG, ICON_256.PNG, README.md
#     cmd/  config/  wizard/
#     app/
#       bin/  mp/  frontend/  python/  ui/
# ---------------------------------------------------------------------------
def prepare_pkg(include_runtime, target_arch=None):
    """把仓库源码 + 构建产物组装到 .local-build/pkg/，返回 pkg 目录。"""
    if PKG_DIR.exists():
        shutil.rmtree(PKG_DIR)
    pkg_app = PKG_DIR / "app"

    # 排除字节码缓存：__pycache__/*.pyc 是构建机产物，跨 Python 版本无效，
    # 还会把 cpython-38 之类的陈旧字节码打进包（运行时由解释器自行重建）。
    ignore_junk = shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo")

    # 1. 仓库源码目录（cmd/config/wizard）
    for sub in SRC_DIRS:
        src = PROJECT_DIR / sub
        if src.exists():
            shutil.copytree(src, PKG_DIR / sub, dirs_exist_ok=True, ignore=ignore_junk)

    # 2. 仓库 app 源码（bin/ui）与构建产物（mp/frontend）组装到 pkg/app/
    #    注意：app 源码要复制（保留项目根），构建产物用 copy（保留 .local-build 缓存供复用）
    for sub in SRC_APP_DIRS:
        src = PROJECT_DIR / "app" / sub
        if src.exists():
            shutil.copytree(src, pkg_app / sub, dirs_exist_ok=True, ignore=ignore_junk)

    for sub, src_dir in [("mp", MP_DIR), ("frontend", FE_DIR)]:
        if src_dir.exists():
            shutil.copytree(src_dir, pkg_app / sub, dirs_exist_ok=True, ignore=ignore_junk)
        else:
            log(f"警告: 缺少构建产物 app/{sub}，打包可能不完整")
    _filter_sites_binaries(pkg_app / "mp", target_arch)

    # 3. 自带 Python 运行时 + 依赖。cmd/* 以 ${TRIM_APPDEST}/python 作为解释器根，
    #    所以这里**不能**套 ignore_junk —— 标准库与 site-packages 的 .pyc 由同一个
    #    3.14.7 解释器生成，留着能让首次启动免去重编译。
    if include_runtime and PYTHON_DIR.exists():
        shutil.copytree(PYTHON_DIR, pkg_app / "python", dirs_exist_ok=True)
    elif include_runtime:
        log("警告: 缺少自带 Python 运行时 app/python，安装时将退回 fnOS python312 在线装依赖")

    # 4. 锁定依赖清单随包分发：NAS 端在线补装时用它（上游 V3 已无 requirements.txt）
    lock = BUILD_DIR / "requirements.lock.txt"
    if lock.exists() and (pkg_app / "mp").is_dir():
        shutil.copy2(lock, pkg_app / "mp" / "requirements.lock.txt")

    # 5. 顶层文件
    for f in ["manifest", "ICON.PNG", "ICON_256.PNG", "README.md"]:
        src = PROJECT_DIR / f
        if src.exists():
            shutil.copy2(src, PKG_DIR / f)

    log(f"==> 打包目录已组装: {PKG_DIR}")
    return PKG_DIR


def build_fpk(fnpack_bin, include_runtime, target_arch=None):
    """在 .local-build/pkg/ 下调用 fnpack 打包，产物输出到项目根。"""
    pkg = prepare_pkg(include_runtime, target_arch)
    log("==> 打包 ...")
    result = subprocess.run([str(fnpack_bin), "build", "."], cwd=str(pkg))
    if result.returncode != 0:
        log("fnpack build 失败")
        sys.exit(result.returncode)
    fpk = pkg / "moviepilot.fpk"
    if fpk.exists():
        version = get_app_version()
        out = PROJECT_DIR / f"moviepilot-{version}.fpk"
        shutil.move(str(fpk), str(out))
        log(f"构建成功: {out} ({out.stat().st_size / 1024 / 1024:.1f} MB)")
    else:
        log("未找到构建产物 moviepilot.fpk")
        sys.exit(1)
    # 清理组装目录
    if PKG_DIR.exists():
        shutil.rmtree(PKG_DIR, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser(description="MoviePilot fnOS 应用构建")
    parser.add_argument("--force", action="store_true", help="强制重新下载外部资源")
    parser.add_argument("--clean", action="store_true", help="构建前清理 .local-build")
    parser.add_argument("--skip-mp", action="store_true", help="跳过下载后端源码")
    parser.add_argument("--skip-fe", action="store_true", help="跳过下载前端")
    parser.add_argument("--arch", choices=["amd64", "arm64"], default=None,
                        help="目标 CPU 架构，用于裁剪 sites 原生变体。缺省取构建机架构；"
                             "Windows 上缺省则不裁剪（保留全部变体以保证兼容）")
    parser.add_argument("--with-runtime", "--with-venv", dest="with_runtime",
                        action="store_true",
                        help="把自带 Python 3.14 运行时与全部依赖一起打包进 fpk"
                             "（安装时完全不联网；仅 Linux/macOS 可用）。"
                             "--with-venv 为兼容旧命令保留的别名")
    parser.add_argument("--no-build", dest="no_build", action="store_true",
                        help="严格模式：禁止从源码构建，只用预编译 wheel。"
                             "默认关闭 —— MoviePilot 有 anitopy、pinyin2hanzi 等"
                             "纯 Python 的 sdist-only 依赖，禁止构建会直接解析失败。"
                             "原生扩展的 glibc 风险由 _audit_source_builds 审计兜底")
    args = parser.parse_args()

    target_arch = resolve_target_arch(args.arch)
    if target_arch:
        log(f"==> 目标架构: {target_arch}（构建机 {get_platform()}/{get_platform_arch()}）")
    else:
        log("==> 目标架构: 未显式指定（Windows 本地构建），sites 原生变体将全部保留")

    # 自带运行时的原生扩展（.so）是按构建机架构装出来的，跨架构捆绑会得到
    # 一个能装但起不来的包，必须提前拦住。
    if args.with_runtime and target_arch and target_arch != get_platform_arch():
        log(f"错误: --with-runtime 要求目标架构与构建机架构一致，"
            f"当前目标 {target_arch} / 构建机 {get_platform_arch()}。")
        log("      请在与目标架构一致的机器（或 CI runner）上构建，"
            "或去掉 --with-runtime（安装时在线安装依赖）。")
        sys.exit(1)

    if args.clean and BUILD_DIR.exists():
        shutil.rmtree(BUILD_DIR)
    BUILD_DIR.mkdir(parents=True, exist_ok=True)

    if not args.skip_mp:
        fetch_moviepilot(args.force)
    if not args.skip_fe:
        fetch_frontend(args.force)
    fetch_resources(args.force)   # 资源包为 MoviePilot V3 必需，默认始终同步

    # 运行时是否真的打进包，必须显式确认：历史上 build_venv() 静默 return False，
    # main() 又不看返回值，结果 CI 全绿却打出零依赖的包。这里把返回值当硬条件，
    # 并且把结论打印在最后一行，便于在 CI 日志里一眼确认。
    runtime_bundled = False
    if args.with_runtime:
        runtime_bundled = build_runtime(target_arch, args.force, args.no_build)
        if not runtime_bundled:
            log("")
            log("!!! 警告: 本次产物**不含** Python 运行时与依赖 !!!")
            log("!!! 真机安装时将退回 fnOS python312 在线安装依赖（需联网且要求 NAS 可访问 PyPI）")
            log("")

    fnpack_bin = ensure_fnpack(args.force)
    build_fpk(fnpack_bin, args.with_runtime, target_arch)

    log("")
    log("==> 构建结论")
    log(f"    目标架构     : {target_arch or '未指定（保留全部 sites 变体）'}")
    log(f"    自带运行时   : {'是（CPython ' + PYTHON_FULL_VERSION + ' + 全部依赖）' if runtime_bundled else '否（安装时需联网装依赖）'}")
    log(f"    依赖清单     : {'app/mp/requirements.lock.txt' if (BUILD_DIR / 'requirements.lock.txt').exists() else '未生成'}")


if __name__ == "__main__":
    main()
