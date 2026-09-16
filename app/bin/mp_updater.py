#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MoviePilot fnOS 应用自更新器（重启即升级）
==========================================
上游 MoviePilot 发新版本时，不必重新构建 fpk：应用每次启动（重启）前都会
检查上游 Release，若有新版本就下载并就地替换后端源码与前端 dist，然后照常
用新代码启动。

为什么自己实现，而不复用上游自带的更新机制：
  上游 V3 的 SystemUpdateManager 本地（非 Docker）安装路径最终走
  `scripts/local_setup.py update`，而它要求 **程序目录是一个 git 仓库**
  （`git fetch` / `git checkout`），并且用 uv 重建 venv。fnOS 应用是 fpk
  解包出来的普通目录（没有 .git），NAS 上也不一定有 git/uv，所以这条路走不通。
  这里改为"下载 Release 压缩包 + 目录级替换"，只依赖标准库。

流程：
  1. 读本地版本（mp/version.py 的 APP_VERSION）与状态文件（冷却/失败计数）
  2. 查 GitHub Release（release=仅正式版 / prerelease=含测试版）
  3. 有新版本 → 下载后端 zip，校验结构，解析出 FRONTEND_VERSION
  4. 依赖预检：用新 uv.lock 对比已装环境，必要时 pip 补装（可关闭）
  5. 备份 → 替换（app/config/database/scripts/skills/moviepilot + version.py 等）
     → 回填资源包文件（资源仓库不在上游 zip 里，必须保留）
  6. 前端 dist 替换
  7. 自检（依赖 import + 语法编译），失败则整树回滚到备份
  8. 更新状态文件

退出码（cmd/main 依此决定后续动作）：
  0  无需更新 / 已是最新 / 跳过
  10 更新成功（需要修正运行时权限）
  1  更新失败（已回滚，继续用当前版本启动）
  2  环境或用法错误

环境变量（优先于 app.env）：
  MP_SRC          后端源码目录（${TRIM_APPDEST}/mp）
  FRONTEND_DIR    前端 dist 目录
  CONFIG_DIR      配置目录（状态文件放这里）
  APP_PYTHON      应用主解释器（自带运行时）
  TRIM_PKGTMP     暂存目录
  MP_UPDATE_LOG / SHARE_LOG   日志
  MP_AUTO_UPDATE      1/0      是否开启（默认 1）
  MP_UPDATE_CHANNEL   release|prerelease|off（默认 release）
  MP_UPDATE_INTERVAL  检查间隔秒（默认 21600）
  MP_UPDATE_DEPS      1/0      是否同步 Python 依赖（默认 1）
  GITHUB_PROXY        加速前缀（app.env 里已配，直连失败时使用）
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

try:  # Python 3.11+，自带运行时是 3.14，必定可用
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - 理论分支
    tomllib = None


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
BACKEND_REPO = "jxxghp/MoviePilot"
FRONTEND_REPO = "jxxghp/MoviePilot-Frontend"

API_LATEST = "https://api.github.com/repos/{repo}/releases/latest"
API_LIST = "https://api.github.com/repos/{repo}/releases?per_page=10"
BACKEND_ZIP = "https://github.com/{repo}/archive/refs/tags/{tag}.zip"
FRONTEND_ZIP = "https://github.com/{repo}/releases/download/{tag}/dist.zip"

# 直连失败时的加速前缀（gh-proxy 只认 github.com 系域名，api 失败就退回直连）
DEFAULT_PROXIES = ("https://gh-proxy.com/", "https://ghfast.top/")

PIP_MIRRORS = (
    "https://pypi.tuna.tsinghua.edu.cn/simple/",
    "https://mirrors.aliyun.com/pypi/simple/",
    "https://mirrors.cloud.tencent.com/pypi/simple/",
    "https://pypi.org/simple/",
)

# 需要整体替换的目录（上游 zip 里的顶层目录）
SYNC_DIRS = ("app", "config", "database", "scripts", "skills", "moviepilot")
# 需要整体替换的单文件
SYNC_FILES = ("version.py", "pyproject.toml", "uv.lock", "mypy.ini", "pytest.ini",
              "app.ico", "README.md")
# 上游 zip 不含资源仓库产物（sites 二进制等），替换后必须回填
HELPER_KEEP_PREFIX = ("user.sites.", "sites.", ".resource-compat")
HELPER_KEEP_EXACT = (".resource-compat",)

# 只认 v3.x.y（可带 alpha/beta/rc 后缀）：上游仓库同时存在 v1/v2 历史 tag 与
# dev 之类非版本引用，一律不接受，避免"升级"到旧版本或不明引用
TAG_RE = re.compile(r"^v3\.\d+\.\d+(?:[-.](?:alpha|beta|rc)[.-]?\d*)?$", re.I)

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_UPDATED = 10

MAX_FAILURES = 2          # 同一版本连续失败次数上限，超过就跳过（避免每次重启都重下一遍坏包）
KEEP_BACKUPS = 2          # 保留的备份代数


class UpdateError(RuntimeError):
    """可预期的更新失败（会触发回滚）。"""


# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------
LOG_FILE = os.environ.get("MP_UPDATE_LOG") or os.environ.get("LOG_FILE") or ""
SHARE_LOG = os.environ.get("SHARE_LOG", "")


def log(msg: str) -> None:
    line = f"[updater] {time.strftime('%Y-%m-%d %H:%M:%S')} - {msg}"
    print(line, flush=True)
    for path in (LOG_FILE, SHARE_LOG):
        if not path:
            continue
        try:
            with open(path, "a", encoding="utf-8") as fp:
                fp.write(line + "\n")
        except OSError:
            pass


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
def _load_env_file(path: Path) -> dict:
    """极简 dotenv 解析：只认 KEY=VALUE，注释与空行忽略。

    不用 python-dotenv：本脚本必须在"依赖还没装好"的场景也能跑，只能靠标准库。
    """
    data = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return data
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip("'\"")
        data[key.strip()] = value
    return data


class Config:
    def __init__(self) -> None:
        self.mp_src = Path(os.environ.get("MP_SRC", "")).resolve()
        self.frontend_dir = Path(os.environ.get("FRONTEND_DIR", "")).resolve()
        self.config_dir = Path(os.environ.get("CONFIG_DIR", "")).resolve()
        tmp = os.environ.get("TRIM_PKGTMP") or os.environ.get("TMPDIR") or "/tmp"
        self.tmp_dir = Path(tmp)
        self.python = os.environ.get("APP_PYTHON") or sys.executable
        self._file_env = _load_env_file(self.config_dir / "app.env") if self.config_dir else {}
        self.state_file = self.config_dir / "mp_update.json" if self.config_dir else None
        # 备份根目录放在 mp 的同级（${TRIM_APPDEST}/.mp-backup），与 mp 同一文件系统，
        # 备份用 rename 完成，瞬间且不需要额外空间复制
        self.backup_root = self.mp_src.parent / ".mp-backup" if self.mp_src else Path()

    def get(self, key: str, default: str = "") -> str:
        """环境变量优先，其次 app.env。"""
        value = os.environ.get(key)
        if value is None:
            value = self._file_env.get(key)
        if value is None or str(value).strip() == "":
            return default
        return str(value).strip()

    def get_bool(self, key: str, default: bool) -> bool:
        raw = self.get(key, "1" if default else "0").lower()
        return raw in ("1", "true", "yes", "on")

    def get_int(self, key: str, default: int) -> int:
        try:
            return int(self.get(key, str(default)))
        except ValueError:
            return default

    @property
    def channel(self) -> str:
        return self.get("MP_UPDATE_CHANNEL", "release").lower()

    @property
    def proxies(self) -> tuple:
        """加速前缀：app.env 的 GITHUB_PROXY 优先，再补内置列表。"""
        out = []
        custom = self.get("GITHUB_PROXY", "")
        if custom and custom.lower() != "none":
            if not custom.endswith("/"):
                custom += "/"
            out.append(custom)
        for p in DEFAULT_PROXIES:
            if p not in out:
                out.append(p)
        return tuple(out)


# ---------------------------------------------------------------------------
# 版本与状态
# ---------------------------------------------------------------------------
def read_py_value(path: Path, key: str) -> str:
    """从 version.py 之类的文件里读 `KEY = 'value'`。"""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    m = re.search(rf"^{re.escape(key)}\s*=\s*['\"]([^'\"]+)['\"]", text, re.MULTILINE)
    return m.group(1).strip() if m else ""


_PRE_RANK = {"": 1, "rc": 0, "beta": -1, "alpha": -2}


def version_key(version: str) -> tuple:
    """把 v3.0.4 / v3.1.0-beta2 变成可比较的元组。

    正式版 > rc > beta > alpha，同级别再比序号；完全无法解析的返回 (0,)，
    保证"看不懂的版本"永远不会被判定为更新。
    """
    if not version:
        return (0,)
    m = re.match(r"^v?(\d+)\.(\d+)\.(\d+)(?:[-.](alpha|beta|rc)[.-]?(\d*))?",
                 version.strip(), re.I)
    if not m:
        return (0,)
    pre = (m.group(4) or "").lower()
    num = int(m.group(5) or 0) if m.group(5) else 0
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)),
            _PRE_RANK.get(pre, -3), num)


def load_state(cfg: Config) -> dict:
    if not cfg.state_file or not cfg.state_file.exists():
        return {}
    try:
        data = json.loads(cfg.state_file.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(cfg: Config, state: dict) -> None:
    if not cfg.state_file:
        return
    try:
        cfg.state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = cfg.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(cfg.state_file)
    except OSError as e:
        log(f"警告: 写入状态文件失败: {e}")


# ---------------------------------------------------------------------------
# 网络（直连 -> 加速）
# ---------------------------------------------------------------------------
def _candidate_urls(url: str, proxies: tuple) -> list:
    return [url] + [f"{p}{url}" for p in proxies]


def http_json(url: str, proxies: tuple, timeout: int = 15):
    for u in _candidate_urls(url, proxies):
        try:
            req = urllib.request.Request(
                u, headers={"User-Agent": "moviepilot-fnos-updater",
                            "Accept": "application/vnd.github+json"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001 - 逐个降级，最后统一报错
            log(f"  请求失败 {u}: {e}")
    return None


def download_file(url: str, dest: Path, proxies: tuple, timeout: int = 60) -> bool:
    """下载到 .part 再原子改名，避免中断留下残缺文件被当成完整包。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    for u in _candidate_urls(url, proxies):
        try:
            req = urllib.request.Request(u, headers={"User-Agent": "moviepilot-fnos-updater"})
            with urllib.request.urlopen(req, timeout=timeout) as resp, open(tmp, "wb") as f:
                total = int(resp.headers.get("Content-Length") or 0)
                done = 0
                mark = 0
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    if done - mark >= 8 * 1024 * 1024:
                        suffix = f"/{total / 1048576:.1f}" if total else ""
                        log(f"  已下载 {done / 1048576:.1f}{suffix} MB")
                        mark = done
            if not tmp.exists() or tmp.stat().st_size == 0:
                raise UpdateError("下载结果为空")
            tmp.replace(dest)
            log(f"  下载完成 {dest.name}（{dest.stat().st_size / 1048576:.1f} MB）")
            return True
        except Exception as e:  # noqa: BLE001
            log(f"  下载失败 {u}: {e}")
            try:
                tmp.unlink()
            except OSError:
                pass
    return False


# ---------------------------------------------------------------------------
# 版本发现
# ---------------------------------------------------------------------------
def fetch_latest_release(cfg: Config):
    """返回 (tag, meta)；查不到返回 (None, None)。

    release 通道走 /releases/latest（GitHub 自动排除预发布）；
    prerelease 通道走 /releases 列表取第一个符合 v3.x.y 命名的 tag。
    """
    channel = cfg.channel
    if channel == "prerelease":
        data = http_json(API_LIST.format(repo=BACKEND_REPO), cfg.proxies)
        items = data if isinstance(data, list) else []
        for item in items:
            tag = str(item.get("tag_name") or "")
            if TAG_RE.match(tag):
                return tag, item
        return None, None
    data = http_json(API_LATEST.format(repo=BACKEND_REPO), cfg.proxies)
    if not isinstance(data, dict):
        return None, None
    tag = str(data.get("tag_name") or "")
    if not TAG_RE.match(tag):
        return None, None
    return tag, data


# ---------------------------------------------------------------------------
# 依赖：用新 uv.lock 对比当前环境
# ---------------------------------------------------------------------------
def installed_versions() -> dict:
    """当前解释器里已安装的发行版 {规范化名: 版本}。"""
    out = {}
    try:
        import importlib.metadata as md
        for dist in md.distributions():
            name = dist.metadata["Name"]
            if not name:
                continue
            try:
                out[name.lower().replace("_", "-")] = dist.version
            except Exception:  # noqa: BLE001
                continue
    except Exception as e:  # noqa: BLE001
        log(f"警告: 读取已安装依赖失败: {e}")
    return out


def _norm_version(v: str) -> str:
    return str(v).strip().lstrip("v=~^>< ").split()[0] if str(v).strip() else ""


def parse_requirements(path: Path) -> dict:
    """解析 requirements.lock.txt → {名: 版本}。"""
    pins = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return pins
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        m = re.match(r"^([A-Za-z0-9_.\-]+)\s*(\[[^\]]*\])?\s*(==|~=|>=)?\s*([^\s;]+)", line)
        if m:
            pins[m.group(1).lower().replace("_", "-")] = m.group(4)
    return pins


def uv_lock_pins(path: Path) -> dict:
    """解析 uv.lock → {名: 版本}（跳过本地/可编辑来源，如项目自身）。"""
    if tomllib is None or not path.exists():
        return {}
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        log(f"警告: 解析 uv.lock 失败: {e}")
        return {}
    pins = {}
    for pkg in data.get("package", []) or []:
        name = str(pkg.get("name") or "")
        version = str(pkg.get("version") or "")
        if not name or not version:
            continue
        # uv.lock 里**每个**包都带 source（正规的 registry 来源也带），所以不能
        # 见到 source 就跳过；只排除非 PyPI 来源：项目自身（editable/virtual）、
        # 本地路径（directory/path）、git 依赖 —— 它们不是 pip 能按名字装到的发行版。
        source = pkg.get("source")
        if isinstance(source, dict) and not (set(source.keys()) & {"registry"}):
            continue
        pins[name.lower().replace("_", "-")] = version
    return pins


def pyproject_direct_deps(path: Path) -> set:
    """解析 pyproject.toml 的 [project].dependencies → 直接依赖名集合。

    用途：全新出现的包（打包时没有、现在也没装）无法从"旧锁定清单"判断它是
    Linux 需要还是 Windows 专有；只要它是上游的**直接**依赖，就认为需要装。
    """
    if tomllib is None or not path.exists():
        return set()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return set()
    deps = (data.get("project") or {}).get("dependencies") or []
    names = set()
    for dep in deps:
        m = re.match(r"^([A-Za-z0-9_.\-]+)", str(dep))
        if m:
            names.add(m.group(1).lower().replace("_", "-"))
    return names


def packaged_pins(cfg: Config) -> dict:
    """随包分发的锁定清单（构建期由 uv.lock 导出，代表本机平台真正需要的依赖）。"""
    return parse_requirements(cfg.mp_src / "requirements.lock.txt")


def plan_dependencies(cfg: Config, new_root: Path, base: dict) -> tuple:
    """返回 (待安装 pin 列表, 缺失包名列表)。

    判断口径：
      * 打包时导出的 requirements.lock.txt 是"本机平台真正需要"的依赖全集，
        用它当作候选范围，天然排除 pywin32 / pyobjc 之类其他平台的包；
      * 已安装的包即使不在旧清单里也要纳入（版本可能变化）；
      * 全新出现的包，只有同时是上游直接依赖时才纳入。
    """
    new_pins = uv_lock_pins(new_root / "uv.lock")
    if not new_pins:
        return [], []
    direct = pyproject_direct_deps(new_root / "pyproject.toml")
    installed = installed_versions()

    to_install, missing = [], []
    for name, version in sorted(new_pins.items()):
        known = name in base or name in installed or name in direct
        if not known:
            continue
        cur = installed.get(name)
        if cur is None:
            missing.append(name)
        elif _norm_version(cur) == _norm_version(version):
            continue
        to_install.append(f"{name}=={version}")
    return to_install, missing


def refresh_lock_file(cfg: Config, new_root: Path, base: dict) -> int:
    """按新 uv.lock 重写随包分发的 requirements.lock.txt，返回写入条数。

    该文件不在上游 zip 里，替换源码后仍然存在；保持它与当前版本一致，
    既服务于下次更新的依赖比对，也服务于"在线补装依赖"这条兜底路径。
    """
    new_pins = uv_lock_pins(new_root / "uv.lock")
    if not new_pins:
        return 0
    direct = pyproject_direct_deps(new_root / "pyproject.toml")
    installed = installed_versions()
    lines = ["# 由自更新器按上游 uv.lock 生成（供在线补装依赖与下次更新比对使用）"]
    for name, version in sorted(new_pins.items()):
        if name in base or name in installed or name in direct:
            lines.append(f"{name}=={version}")
    if len(lines) == 1:
        return 0
    try:
        (cfg.mp_src / "requirements.lock.txt").write_text("\n".join(lines) + "\n",
                                                          encoding="utf-8")
    except OSError as e:
        log(f"警告: 刷新 requirements.lock.txt 失败: {e}")
        return 0
    return len(lines) - 1


def _mirror_host(mirror: str) -> str:
    return mirror.split("//", 1)[-1].split("/", 1)[0]


def install_dependencies(cfg: Config, pins: list) -> bool:
    """pip 补装缺失/变更的依赖。

    --no-deps：uv.lock 里已经包含完整传递闭包，再让 pip 解析一次既没必要也有害
    （构建期的踩坑：uv 重新解析会把 pyproject 里被 exclude 的 crcmod 又拉回来）。
    先尝试 --only-binary=:all:：NAS 上通常没有编译器，装纯 wheel 最快也最稳；
    若某个包只有 sdist，再退一轮允许本地构建（此时编出来的是 NAS 自己的
    glibc，不存在跨机不兼容问题）。
    """
    if not pins:
        return True
    for only_binary in (True, False):
        for mirror in PIP_MIRRORS:
            cmd = [cfg.python, "-m", "pip", "install",
                   "--no-deps", "--no-cache-dir", "--disable-pip-version-check",
                   "-i", mirror, "--trusted-host", _mirror_host(mirror),
                   "--timeout", "30", "--retries", "2"]
            if only_binary:
                cmd.append("--only-binary=:all:")
            cmd += pins
            log(f"==> 同步依赖（{'仅 wheel' if only_binary else '允许源码构建'}，{mirror}）："
                f"{len(pins)} 个包")
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
            except subprocess.TimeoutExpired:
                log("    依赖安装超时，换下一个镜像")
                continue
            if r.returncode == 0:
                log("==> 依赖同步完成")
                return True
            tail = (r.stderr or r.stdout or "").strip().splitlines()[-3:]
            log("    失败：" + " | ".join(tail))
    return False


# ---------------------------------------------------------------------------
# 目录替换 / 备份 / 回滚
# ---------------------------------------------------------------------------
def ensure_writable(path: Path) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".mp_update_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as e:
        raise UpdateError(f"目录不可写，无法就地更新: {path}（{e}）") from e


def extract_zip(zip_path: Path, dest_dir: Path) -> Path:
    """解压并返回内部唯一顶层目录。"""
    dest_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(dest_dir)
    entries = [p for p in dest_dir.iterdir() if p.name not in ("__MACOSX",)]
    if len(entries) == 1 and entries[0].is_dir():
        return entries[0]
    # 极少数情况下 zip 没有顶层目录，直接用解压目录
    return dest_dir


def fix_permissions(root: Path) -> None:
    """统一权限：目录 755、文件 644、脚本 755。

    zipfile 解压出来的文件权限取决于压缩包里的外部属性，可能是 600（属主
    root）那样"应用用户读不了"的权限；后端以专用用户运行，读不到源码会直接
    起不来。这里显式统一，避免依赖压缩包的属性。
    """
    if not root.exists():
        return
    for dirpath, dirnames, filenames in os.walk(root):
        try:
            os.chmod(dirpath, 0o755)
        except OSError:
            pass
        for name in filenames:
            p = Path(dirpath) / name
            mode = 0o755 if p.suffix in (".sh", ".py", ".bin") or name in ("moviepilot",) else 0o644
            try:
                os.chmod(p, mode)
            except OSError:
                pass


def stage_replace(src_root: Path, dst_root: Path, backup_root: Path) -> list:
    """把 src_root 里的同步目标搬到 dst_root，旧内容先 rename 进 backup_root。

    返回 [(dst, backup)] 供回滚使用。备份用 rename（同一文件系统，瞬时完成），
    不像复制那样既慢又额外占空间。
    """
    moves = []
    for name in tuple(SYNC_DIRS) + tuple(SYNC_FILES):
        src = src_root / name
        if not src.exists():
            continue
        dst = dst_root / name
        if dst.exists() or dst.is_symlink():
            backup = backup_root / name
            backup.parent.mkdir(parents=True, exist_ok=True)
            dst.rename(backup)
            moves.append((dst, backup))
        if src.is_dir():
            shutil.move(str(src), str(dst))
        else:
            shutil.copy2(str(src), str(dst))
        log(f"    替换 {name}")
    return moves


def restore_helper_resources(backup_root: Path, dst_root: Path) -> int:
    """回填资源仓库产物。

    上游 zip 里没有 MoviePilot-Resources 的 sites 二进制（它们在独立仓库），
    而打包时我们已把它们放进 app/helper。整体替换 app/ 后必须把这些文件搬回来，
    否则会出现 "No module named 'app.helper.sites'"。
    """
    old_helper = backup_root / "app" / "helper"
    new_helper = dst_root / "app" / "helper"
    if not old_helper.is_dir():
        return 0
    new_helper.mkdir(parents=True, exist_ok=True)
    restored = 0
    for f in sorted(old_helper.iterdir()):
        if not f.is_file():
            continue
        keep = f.name in HELPER_KEEP_EXACT or f.name.startswith(HELPER_KEEP_PREFIX)
        if not keep or (new_helper / f.name).exists():
            continue
        shutil.copy2(str(f), str(new_helper / f.name))
        restored += 1
    if restored:
        log(f"    回填 {restored} 个资源包文件到 app/helper")
    return restored


def replace_frontend(staged: Path, frontend_dir: Path, backup_root: Path) -> bool:
    """整体替换前端 dist。staged 里若是 dist/ 子目录则自动提升一层。"""
    inner = staged / "dist"
    if inner.is_dir():
        staged = inner
    if not any(staged.iterdir()):
        raise UpdateError("前端包为空")
    if frontend_dir.exists():
        frontend_dir.rename(backup_root / "frontend")
    frontend_dir.mkdir(parents=True, exist_ok=True)
    for item in sorted(staged.iterdir()):
        shutil.move(str(item), str(frontend_dir / item.name))
    return True


def rollback(moves: list, backup_root: Path, frontend_dir: Path) -> None:
    """把 stage_replace / replace_frontend 的改动全部还原。"""
    log("==> 回滚到更新前的版本")
    for dst, backup in reversed(moves):
        try:
            if dst.is_symlink() or dst.is_file():
                dst.unlink()
            elif dst.is_dir():
                shutil.rmtree(dst, ignore_errors=True)
        except OSError:
            pass
        if backup.exists():
            backup.rename(dst)
    fe_backup = backup_root / "frontend"
    if fe_backup.exists():
        if frontend_dir.exists():
            shutil.rmtree(frontend_dir, ignore_errors=True)
        fe_backup.rename(frontend_dir)


def prune_backups(backup_root: Path) -> None:
    if not backup_root.exists():
        return
    dirs = sorted((p for p in backup_root.iterdir() if p.is_dir()),
                  key=lambda p: p.name, reverse=True)
    for old in dirs[KEEP_BACKUPS:]:
        shutil.rmtree(old, ignore_errors=True)


# ---------------------------------------------------------------------------
# 复用上游（MoviePilot 自带更新）已下载的安装包
# ---------------------------------------------------------------------------
def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def upstream_artifacts(cfg: Config, current: str):
    """取回 MoviePilot 自带更新下载好的包，命中就不重复下载。

    上游 SystemUpdateManager 把 backend.zip / frontend.zip 与清单写在
    TEMP_PATH/movietpilot-update/（TEMP_PATH = CONFIG_PATH/temp）：
      * install.json  —— 用户在界面点过"安装"后的安装意图
      * prepared.json —— 仅下载完成、等待确认

    说明：上游本地（非 Docker）路径在下载阶段就要求 .git，所以这份产物在
    fnOS 上通常不会出现；这里只是"万一有就别浪费"的兼容，未来上游去掉 git
    依赖后即可无缝衔接。所有 sha256 一律校验，残缺包直接忽略。
    """
    root = cfg.config_dir / "temp" / "moviepilot-update"
    if not root.is_dir():
        return None
    for name in ("install.json", "prepared.json"):
        manifest = root / name
        if not manifest.is_file():
            continue
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        version = str(data.get("version") or "")
        if not version or version_key(version) <= version_key(current):
            continue
        backend = Path(str(data.get("backend_archive") or ""))
        if not backend.is_file():
            continue
        try:
            if data.get("backend_sha256") and sha256_file(backend) != str(data["backend_sha256"]):
                log(f"警告: 上游后端包校验失败，忽略: {backend}")
                continue
        except OSError:
            continue

        frontend = Path(str(data.get("frontend_archive") or ""))
        if frontend.is_file() and data.get("frontend_sha256"):
            try:
                if sha256_file(frontend) != str(data["frontend_sha256"]):
                    log(f"警告: 上游前端包校验失败，忽略: {frontend}")
                    frontend = Path("")
            except OSError:
                frontend = Path("")

        resources = []
        for item in data.get("resource_files") or []:
            if not isinstance(item, dict):
                continue
            p = Path(str(item.get("path") or ""))
            if not p.is_file():
                continue
            try:
                if item.get("sha256") and sha256_file(p) != str(item["sha256"]):
                    continue
            except OSError:
                continue
            resources.append(p)

        return {
            "manifest": manifest,
            "version": version,
            "frontend_version": str(data.get("frontend_version") or ""),
            "backend": backend,
            "frontend": frontend if frontend.is_file() else None,
            "resources": resources,
        }
    return None


def apply_resource_files(cfg: Config, files: list) -> int:
    """把上游下载好的站点资源文件装进 app/helper（打包时它们也放在这里）。"""
    helper = cfg.mp_src / "app" / "helper"
    helper.mkdir(parents=True, exist_ok=True)
    applied = 0
    for p in files:
        try:
            shutil.copy2(str(p), str(helper / p.name))
            applied += 1
        except OSError as e:
            log(f"警告: 应用资源文件失败 {p.name}: {e}")
    if applied:
        log(f"    应用 {applied} 个上游资源文件到 app/helper")
    return applied


def consume_upstream_manifest(prepared: dict) -> None:
    """消费掉上游清单，避免它停留在 installing 状态被反复应用。"""
    if not prepared:
        return
    manifest = prepared["manifest"]
    try:
        manifest.unlink()
    except OSError:
        pass
    # prepared.json 里可能还挂着"站点资源"更新，只清掉主程序相关字段
    other = manifest.parent / "prepared.json"
    if other.is_file() and other != manifest:
        try:
            data = json.loads(other.read_text(encoding="utf-8"))
            for key in ("version", "frontend_version", "backend_archive",
                        "frontend_archive", "backend_sha256", "frontend_sha256"):
                data.pop(key, None)
            targets = [t for t in (data.get("targets") or []) if t == "resources"]
            if targets:
                data["targets"] = targets
            else:
                data.pop("targets", None)
            if data.get("resource_files"):
                other.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
            else:
                other.unlink(missing_ok=True)
        except (OSError, json.JSONDecodeError):
            pass


# ---------------------------------------------------------------------------
# 自检
# ---------------------------------------------------------------------------
def smoke_test(cfg: Config) -> None:
    """更新后自检：关键依赖 import + 源码语法编译。

    只验"能不能跑起来"的最低面：真正的业务正确性由启动后的日志体现。
    编译失败视为致命（语法错误必然起不来），但编译超时不判失败（慢设备常见）。
    """
    code = "import fastapi, uvicorn, sqlalchemy, pydantic, orjson; print('SMOKE_OK')"
    try:
        r = subprocess.run([cfg.python, "-c", code], capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        raise UpdateError("依赖自检超时") from None
    if r.returncode != 0 or "SMOKE_OK" not in (r.stdout or ""):
        raise UpdateError("依赖自检失败：" + (r.stderr or r.stdout or "")[-500:])

    try:
        r = subprocess.run([cfg.python, "-m", "compileall", "-q", str(cfg.mp_src / "app")],
                           capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        log("警告: 源码编译检查超时，跳过（不视为失败）")
        return
    if r.returncode != 0:
        raise UpdateError("源码语法检查失败：" + (r.stderr or r.stdout or "")[-800:])


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def do_update(cfg: Config, args) -> int:
    state = load_state(cfg)
    now = time.time()

    if not args.force:
        if not cfg.get_bool("MP_AUTO_UPDATE", True):
            log("自动更新已关闭（MP_AUTO_UPDATE=0），跳过")
            return EXIT_OK
        if cfg.channel == "off":
            log("更新通道为 off，跳过")
            return EXIT_OK
        last = float(state.get("checked_at") or 0)
        interval = max(cfg.get_int("MP_UPDATE_INTERVAL", 21600), 0)
        if last and now - last < interval:
            log(f"距上次检查仅 {int(now - last)}s（间隔 {interval}s），跳过")
            return EXIT_OK

    current = read_py_value(cfg.mp_src / "version.py", "APP_VERSION")
    if not current:
        log(f"错误: 无法从 {cfg.mp_src / 'version.py'} 读取 APP_VERSION")
        return EXIT_FAILED
    current_fe = state.get("frontend_version") or \
        read_py_value(cfg.mp_src / "version.py", "FRONTEND_VERSION")
    log(f"当前版本: {current}（前端 {current_fe or '未知'}）")

    # 先看有没有 MoviePilot 自带更新下载好的包：有就不用再联网查和下载
    prepared = upstream_artifacts(cfg, current)
    if prepared:
        tag = prepared["version"]
        meta = {"name": f"上游已下载的安装包（{prepared['manifest'].name}）"}
        log(f"发现上游更新产物: {tag}")
    else:
        tag, meta = fetch_latest_release(cfg)
    state["checked_at"] = now
    if not tag:
        state["last_error"] = "无法获取上游最新版本（网络不可达或通道无匹配版本）"
        save_state(cfg, state)
        log("错误: " + state["last_error"])
        return EXIT_FAILED

    log(f"上游最新: {tag}（{meta.get('name') or ''}）")
    if version_key(tag) <= version_key(current):
        state.update({"backend_version": current, "last_error": None})
        save_state(cfg, state)
        log("已是最新版本，无需更新")
        return EXIT_OK

    failures = state.get("failures") or {}
    if int(failures.get(tag, 0)) >= MAX_FAILURES:
        log(f"{tag} 已连续失败 {failures.get(tag)} 次，本次跳过（删除状态文件可重试）")
        return EXIT_OK

    ensure_writable(cfg.mp_src)
    ensure_writable(cfg.frontend_dir)
    ensure_writable(cfg.tmp_dir)

    work = cfg.tmp_dir / "mp-update"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    backup_dir = cfg.backup_root / time.strftime("%Y%m%d-%H%M%S")
    moves = []
    try:
        # 1) 后端源码（优先复用上游已下载的包）
        if prepared:
            log(f"==> 复用上游已下载的安装包 {prepared['version']}")
            src_root = extract_zip(prepared["backend"], work / "src")
        else:
            zip_path = work / "backend.zip"
            log(f"==> 下载后端源码 {tag} ...")
            if not download_file(BACKEND_ZIP.format(repo=BACKEND_REPO, tag=tag),
                                 zip_path, cfg.proxies):
                raise UpdateError(f"后端源码下载失败: {tag}")
            src_root = extract_zip(zip_path, work / "src")
        if not (src_root / "app").is_dir() or not (src_root / "version.py").is_file():
            raise UpdateError("后端包结构异常（缺少 app/ 或 version.py）")
        new_version = read_py_value(src_root / "version.py", "APP_VERSION")
        if not new_version or version_key(new_version) != version_key(tag):
            raise UpdateError(f"后端包版本与 tag 不一致（{new_version} != {tag}）")
        new_fe = read_py_value(src_root / "version.py", "FRONTEND_VERSION")
        fix_permissions(src_root)

        # 2) 依赖预检（放在替换之前：装依赖失败就不用动代码）
        base_pins = packaged_pins(cfg)
        pins, missing = plan_dependencies(cfg, src_root, base_pins)
        if pins:
            log(f"==> 依赖差异: {len(pins)} 个（新增 {len(missing)}: "
                f"{', '.join(missing[:8]) if missing else '无'}）")
        if not cfg.get_bool("MP_UPDATE_DEPS", True):
            if missing:
                raise UpdateError(
                    "新版本需要新增依赖 " + ", ".join(missing[:8]) +
                    "，但 MP_UPDATE_DEPS=0；请开启依赖同步或重新安装应用")
            if pins:
                log("警告: MP_UPDATE_DEPS=0，跳过依赖同步（可能缺少模块）")
        elif pins and not install_dependencies(cfg, pins):
            raise UpdateError("依赖同步失败（详见日志），已保持当前版本")

        # 3) 前端
        fe_staged = None
        if new_fe and new_fe != current_fe:
            if prepared and prepared.get("frontend"):
                log(f"==> 复用上游已下载的前端 {new_fe}")
                fe_staged = extract_zip(prepared["frontend"], work / "fe")
            else:
                log(f"==> 下载前端 {new_fe} ...")
                fe_zip = work / "frontend.zip"
                if not download_file(FRONTEND_ZIP.format(repo=FRONTEND_REPO, tag=new_fe),
                                     fe_zip, cfg.proxies):
                    raise UpdateError(f"前端下载失败，放弃本次更新: {new_fe}")
                fe_staged = extract_zip(fe_zip, work / "fe")
            if not (fe_staged / "index.html").is_file() and not (fe_staged / "dist" / "index.html").is_file():
                raise UpdateError("前端包结构异常（缺少 index.html）")
            fix_permissions(fe_staged)
        elif new_fe:
            log(f"==> 前端版本未变化（{new_fe}），跳过下载")

        # 4) 替换（备份 -> 移动 -> 回填资源）
        log(f"==> 应用更新 {current} -> {new_version}")
        backup_dir.mkdir(parents=True, exist_ok=True)
        moves = stage_replace(src_root, cfg.mp_src, backup_dir)
        if not moves:
            raise UpdateError("没有任何内容被替换，疑似包结构异常")
        restore_helper_resources(backup_dir, cfg.mp_src)
        if prepared and prepared.get("resources"):
            apply_resource_files(cfg, prepared["resources"])
        if fe_staged is not None:
            replace_frontend(fe_staged, cfg.frontend_dir, backup_dir)
            current_fe = new_fe
        # 刷新随包分发的锁定清单：它不在上游 zip 里（构建时由 uv 生成），
        # 更新后需按新 uv.lock 重写，否则下次更新时"候选依赖范围"会一直停留在
        # 打包时的旧集合，新增依赖可能被误判为"其他平台专有"而跳过。
        refresh_lock_file(cfg, src_root, base_pins)

        # 5) 自检，失败即回滚
        smoke_test(cfg)
    except Exception as e:  # noqa: BLE001 - 任何异常都要回滚，绝不留半新半旧
        log(f"错误: 更新失败: {e}")
        try:
            rollback(moves, backup_dir, cfg.frontend_dir)
        except Exception as re_:  # noqa: BLE001
            log(f"错误: 回滚失败（请手动检查 {backup_dir}）: {re_}")
        failures = state.get("failures") or {}
        failures[str(tag)] = int(failures.get(str(tag), 0)) + 1
        state.update({"failures": failures, "last_error": str(e), "checked_at": time.time()})
        save_state(cfg, state)
        return EXIT_FAILED
    finally:
        shutil.rmtree(work, ignore_errors=True)

    consume_upstream_manifest(prepared)
    state.update({
        "backend_version": new_version,
        "frontend_version": current_fe,
        "updated_at": time.time(),
        "last_error": None,
        "failures": {},
    })
    save_state(cfg, state)
    prune_backups(cfg.backup_root)
    log(f"==> 更新完成: {current} -> {new_version}")
    log(f"    备份保留在 {backup_dir}（可用 --rollback 回退）")
    return EXIT_UPDATED


def do_check(cfg: Config) -> int:
    current = read_py_value(cfg.mp_src / "version.py", "APP_VERSION")
    tag, meta = fetch_latest_release(cfg)
    if not tag:
        log("无法获取上游最新版本（网络不可达或通道无匹配版本）")
        return EXIT_FAILED
    state = load_state(cfg)
    print(json.dumps({
        "current": current,
        "latest": tag,
        "name": (meta or {}).get("name"),
        "published_at": (meta or {}).get("published_at"),
        "update_available": version_key(tag) > version_key(current or ""),
        "channel": cfg.channel,
        "auto_update": cfg.get_bool("MP_AUTO_UPDATE", True),
        "last_error": state.get("last_error"),
    }, ensure_ascii=False, indent=2))
    return EXIT_OK


def do_rollback(cfg: Config) -> int:
    if not cfg.backup_root.exists():
        log("没有可用的备份")
        return EXIT_FAILED
    dirs = sorted((p for p in cfg.backup_root.iterdir() if p.is_dir()),
                  key=lambda p: p.name, reverse=True)
    if not dirs:
        log("没有可用的备份")
        return EXIT_FAILED
    backup = dirs[0]
    log(f"==> 从备份恢复: {backup}")
    restored = 0
    for item in sorted(backup.iterdir()):
        target = cfg.frontend_dir if item.name == "frontend" else cfg.mp_src / item.name
        if target.exists():
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target, ignore_errors=True)
            else:
                target.unlink(missing_ok=True)
        shutil.move(str(item), str(target))
        restored += 1
    shutil.rmtree(backup, ignore_errors=True)
    state = load_state(cfg)
    state.update({"last_error": None, "failures": {}})
    save_state(cfg, state)
    log(f"==> 已恢复 {restored} 项，请重启应用")
    return EXIT_UPDATED


def main() -> int:
    parser = argparse.ArgumentParser(description="MoviePilot fnOS 自更新器")
    parser.add_argument("--check", action="store_true", help="只检查并打印版本信息，不做任何改动")
    parser.add_argument("--rollback", action="store_true", help="回滚到最近一次更新前的备份")
    parser.add_argument("--force", action="store_true",
                        help="忽略 MP_AUTO_UPDATE 开关与检查冷却（手动更新用）")
    args = parser.parse_args()

    cfg = Config()
    if not cfg.mp_src or not cfg.mp_src.is_dir():
        log(f"错误: MP_SRC 无效: {cfg.mp_src}")
        return EXIT_USAGE
    if not cfg.config_dir:
        log("错误: 未设置 CONFIG_DIR")
        return EXIT_USAGE

    try:
        if args.check:
            return do_check(cfg)
        if args.rollback:
            return do_rollback(cfg)
        return do_update(cfg, args)
    except UpdateError as e:
        log(f"错误: {e}")
        return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
