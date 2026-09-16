#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MoviePilot fnOS 应用 跨平台构建脚本（推荐，Windows / Linux / macOS 通用）
==========================================================================
功能：
  1. 下载 MoviePilot V3 后端源码与前端 dist，统一收敛到 .local-build/
  2. （可选 --with-venv）构建 Python 依赖 venv 到 .local-build/venv
  3. 在 .local-build/pkg/ 组装干净的应用目录树（只含进包内容）
  4. 按开发机平台自动下载 fnpack 并打包
  5. 产物命名 moviepilot-<version>.fpk

设计（参照 fnos-transmission）：
  - 所有下载/解压/构建产物统一放在 .local-build/（已 gitignore，不入库）
  - 打包前把仓库源码(cmd/config/wizard/manifest/图标/app 源码)与构建产物
    一起组装到 .local-build/pkg/，最后 cd pkg 用 fnpack 打包
  - 打包目录里只有该进包的内容，项目根目录不残留任何构建产物

用法：
  python build.py                # 默认
  python build.py --force        # 强制重新下载
  python build.py --clean        # 构建前清理 .local-build
  python build.py --skip-mp      # 跳过下载后端源码
  python build.py --skip-fe      # 跳过下载前端
  python build.py --arch arm64   # 显式指定目标架构（用于裁剪 sites 原生变体）

目标架构（--arch）：
  MoviePilot-Resources 内置 python311-314 × linux-amd64/aarch64/darwin/win 的
  全部 sites 原生变体（约 31M）。打包时会按目标架构只保留匹配的一个，减体积
  约 28M。--arch 缺省时取构建机架构；Windows 上缺省则不裁剪（保留全部变体），
  因为 fnpack 在 Windows 上无法产出与目标 NAS 绑定的包，宁可包大也不打出
  缺 app.helper.sites 的坏包。

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
VENV_DIR = BUILD_DIR / "venv"                 # Python 虚拟环境（--with-venv 时）
VERSION_FILE = BUILD_DIR / "versions.json"

FNPACK_VERSION = "1.2.3"
MAIN_PROXY = "https://gh-proxy.com/"
ALT_PROXY = "https://ghfast.top/"

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
    """从 manifest install_dep_apps 读取依赖运行时 Python 版本（python312 -> "312"）。"""
    manifest_file = PROJECT_DIR / "manifest"
    try:
        for line in manifest_file.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("install_dep_apps") and "=" in line:
                m = re.search(r"python(\d{3})", line)
                if m:
                    return m.group(1)
    except Exception as e:
        log(f"警告: 读取 install_dep_apps 失败，使用默认 312: {e}")
    return "312"


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
    if not force and (mp_dir / "requirements.txt").exists():
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
# 下载 MoviePilot-Resources 资源包并同步到 mp/app/helper
# MoviePilot V3 依赖资源仓库提供 app/helper/sites.py 等文件，缺失会导致
# "No module named 'app.helper.sites'" 而无法初始化数据库/超级管理员
# ---------------------------------------------------------------------------
RESOURCE_FLAG = "v3"
RESOURCES_ZIP = "https://github.com/jxxghp/MoviePilot-Resources/archive/refs/heads/main.zip"


def fetch_resources(force=False):
    helper_dir = MP_DIR / "app" / "helper"
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
    log(f"资源同步完成，共 {len(copied)} 个文件到 app/helper")


# ---------------------------------------------------------------------------
# 打包 Python 依赖 venv（实现安装时完全不联网）
# 仅支持在 Linux/macOS 上按目标架构构建；Windows 无法交叉编译 Linux venv。
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


def build_venv(force=False):
    plat = get_platform()
    if plat == "windows":
        log("警告: Windows 上无法交叉编译 Linux venv，跳过全量打包（安装时将在线安装依赖）")
        return False

    mp_src = MP_DIR
    venv_dir = VENV_DIR
    marker = venv_dir / ".bundled_deps_done"

    if not force and marker.exists():
        log("==> 已存在打包好的 venv，跳过")
        return True

    if not (mp_src / "requirements.txt").exists():
        log("警告: 缺少 MoviePilot 源码，无法构建 venv")
        return False

    py = shutil.which("python3")
    if not py:
        log("错误: 未找到 python3，无法构建 venv")
        sys.exit(1)

    log("==> 构建并打包 Python 依赖 venv ...")
    if venv_dir.exists():
        shutil.rmtree(venv_dir)
    subprocess.run([py, "-m", "venv", str(venv_dir)], check=True)
    pip = venv_dir / "bin" / "pip"
    if not pip.exists():
        pip = venv_dir / "Scripts" / "pip.exe"

    ok = False
    for i, mirror in enumerate(PIP_MIRRORS):
        host = mirror.split("//")[1].split("/")[0]
        log(f"  pip 镜像({i + 1}/{len(PIP_MIRRORS)}): {mirror}")
        try:
            subprocess.run([str(pip), "install", "--upgrade", "pip",
                            "-i", mirror, "--trusted-host", host],
                           capture_output=True, text=True)
            r = subprocess.run([str(pip), "install", "-r", str(mp_src / "requirements.txt"),
                                "-i", mirror, "--trusted-host", host],
                               capture_output=True, text=True)
            if r.returncode == 0:
                ok = True
                break
            log(f"  pip 镜像失败: {r.stderr[-300:]}")
        except Exception as e:
            log(f"  pip 镜像异常: {e}")
    if not ok:
        log("错误: Python 依赖安装失败，无法全量打包")
        sys.exit(1)

    log(f"==> venv 原始体积: {_size_mb(venv_dir):.1f} MB")
    _log_site_packages_top(venv_dir)

    log("==> 清理 venv：tests/testing 目录、strip 调试符号、缓存/元数据 ...")
    _remove_pkg_tests(venv_dir)
    _strip_so_binaries(venv_dir)
    _trim_venv(venv_dir)
    log(f"==> venv 清理后体积: {_size_mb(venv_dir):.1f} MB")

    marker.write_text("ok", encoding="utf-8")
    log("venv 打包完成")
    return True


def _trim_venv(venv_dir):
    """删除 venv 中可安全移除的缓存/元数据，减小包体。

    只清理以下（运行时自动重建，删除无副作用）：
      - __pycache__ 目录
      - *.pyc / *.pyo 字节码
      - *.dist-info / *.egg-info 安装元数据
    绝不删除 .so/.pyd/.dylib/.dll 等二进制及包本体。
    """
    import re
    if not venv_dir.exists():
        return
    removed_files = 0
    removed_dirs = 0
    site_pkgs = venv_dir / "lib"
    if get_platform() == "windows":
        site_pkgs = venv_dir / "Lib"

    # 扫描范围：site-packages + 顶层
    scan_roots = [venv_dir]
    if site_pkgs.exists():
        for ver in site_pkgs.iterdir():
            sp = ver / "site-packages"
            if sp.exists():
                scan_roots.append(sp)

    for root in scan_roots:
        if not root.exists():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            # 删除 __pycache__ 目录
            if "__pycache__" in dirnames:
                pycache = os.path.join(dirpath, "__pycache__")
                shutil.rmtree(pycache, ignore_errors=True)
                removed_dirs += 1
                dirnames.remove("__pycache__")
            # 删除 .dist-info / .egg-info 目录
            keep_dirs = []
            for d in dirnames:
                if d.endswith(".dist-info") or d.endswith(".egg-info"):
                    shutil.rmtree(os.path.join(dirpath, d), ignore_errors=True)
                    removed_dirs += 1
                else:
                    keep_dirs.append(d)
            dirnames[:] = keep_dirs
            # 删除 .pyc/.pyo 文件
            for f in filenames:
                if f.endswith((".pyc", ".pyo")):
                    try:
                        os.remove(os.path.join(dirpath, f))
                        removed_files += 1
                    except OSError:
                        pass
    log(f"清理完成: 删除 {removed_dirs} 个缓存目录, {removed_files} 个字节码文件")


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
    helper = Path(pkg_mp_dir) / "app" / "helper"
    if not helper.is_dir():
        return
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
#       bin/  mp/  frontend/  venv/  ui/
# ---------------------------------------------------------------------------
def prepare_pkg(include_venv, target_arch=None):
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

    # 2. 仓库 app 源码（bin/ui）与构建产物（mp/frontend/venv）组装到 pkg/app/
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

    if include_venv and VENV_DIR.exists():
        shutil.copytree(VENV_DIR, pkg_app / "venv", dirs_exist_ok=True, ignore=ignore_junk)

    # 3. 顶层文件
    for f in ["manifest", "ICON.PNG", "ICON_256.PNG", "README.md"]:
        src = PROJECT_DIR / f
        if src.exists():
            shutil.copy2(src, PKG_DIR / f)

    log(f"==> 打包目录已组装: {PKG_DIR}")
    return PKG_DIR


def build_fpk(fnpack_bin, include_venv, target_arch=None):
    """在 .local-build/pkg/ 下调用 fnpack 打包，产物输出到项目根。"""
    pkg = prepare_pkg(include_venv, target_arch)
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
    parser.add_argument("--with-venv", action="store_true",
                        help="把 Python 依赖 venv 一起打包进 fpk（安装时完全不联网；仅 Linux/macOS 可用）")
    args = parser.parse_args()

    target_arch = resolve_target_arch(args.arch)
    if target_arch:
        log(f"==> 目标架构: {target_arch}（构建机 {get_platform()}/{get_platform_arch()}）")
    else:
        log("==> 目标架构: 未显式指定（Windows 本地构建），sites 原生变体将全部保留")

    # 捆绑 venv 时目标架构必须与构建机一致：venv 里的原生扩展（.so/.pyd）是按
    # 构建机架构安装的，跨架构捆绑会得到一个能装但起不来的包，必须提前拦住。
    if args.with_venv and target_arch and target_arch != get_platform_arch():
        log(f"错误: --with-venv 要求目标架构与构建机架构一致，"
            f"当前目标 {target_arch} / 构建机 {get_platform_arch()}。")
        log("      请在与目标架构一致的机器（或 CI runner）上构建，"
            "或去掉 --with-venv（安装时在线安装依赖）。")
        sys.exit(1)

    if args.clean and BUILD_DIR.exists():
        shutil.rmtree(BUILD_DIR)
    BUILD_DIR.mkdir(parents=True, exist_ok=True)

    if not args.skip_mp:
        fetch_moviepilot(args.force)
    if not args.skip_fe:
        fetch_frontend(args.force)
    fetch_resources(args.force)   # 资源包为 MoviePilot V3 必需，默认始终同步
    if args.with_venv:
        build_venv(args.force)

    fnpack_bin = ensure_fnpack(args.force)
    build_fpk(fnpack_bin, args.with_venv, target_arch)


if __name__ == "__main__":
    main()
