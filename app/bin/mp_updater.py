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
     四级降级：API（只直连）→ 网页 latest → releases.atom → 分支 version.py
     见 fetch_latest_release —— 越靠后越"能过镜像"，越靠前信息越权威
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
  GITHUB_PROXY        加速前缀，用于 github.com 系 URL（app.env 里已配）
  GITHUB_PROXY_MIRRORS 额外加速前缀，逗号分隔（本更新器专用，见 Config.proxies）

注意：加速前缀只作用于 github.com 系 URL（归档下载、网页兜底版本发现）。
api.github.com 一律直连，不走加速（镜像对它的支持参差不齐）。
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import platform
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

# 不依赖 api.github.com 的版本发现入口（三级降级的后两级，见 fetch_latest_release）。
# api.github.com 是**独立的域名**，而加速镜像多半只转发 github.com / raw：
# 一旦镜像不转发 api，请求会一直挂到 SSL 握手超时（实测 ghfast.top 就是这样）。
# 改用网页端后，"查得到版本"与"下得动包"落在同一批通道上 —— 能下包就一定查得到版本。
WEB_LATEST = "https://github.com/{repo}/releases/latest"
WEB_ATOM = "https://github.com/{repo}/releases.atom"

# 最后一级：分支上的 version.py（raw 文件型 URL）。
# 价值在于 raw 是**文件**型 URL —— 几乎每个镜像都转发它，而同一个镜像对 releases
# 网页/订阅源往往直接 403 / 404（实测 gh-proxy.com 就是如此）。也就是说：当所有
# "页面型"通道都不可用时，这一级仍然能通过加速镜像查到版本。
# 分支名是上游的内部选择，所以候选多个；main 上是 v1.9.19，会被 TAG_RE 自然滤掉。
RAW_VERSION = "https://raw.githubusercontent.com/{repo}/{ref}/version.py"
RAW_REFS = ("v3", "main", "master")

# 加速前缀（顺序即尝试顺序），只作用于 github.com 系 URL
DEFAULT_PROXIES = ("https://gh-proxy.com/", "https://ghfast.top/")

# 传给 http_json 表示"不加任何加速前缀，只直连"。api.github.com 必须走这个：
# 镜像对 api 域名的支持参差不齐，不支持时不会快速失败，而是一直挂到 SSL 握手
# 超时（实测 ghfast.top），白等一个超时周期还把日志刷满。
DIRECT_ONLY: tuple = ()

UA = "moviepilot-fnos-updater"
# 版本发现：单次请求超时与整体预算。更新检查挂在应用启动路径上，
# 全部通道都不可达时宁可放弃检查，也不能把启动拖成几分钟。
DISCOVERY_TIMEOUT = 10
# 直连 api.github.com 只有一次机会（没有加速通道可退），给足时间
API_TIMEOUT = 15
# 四级通道 × 各 1~3 个前缀，最坏情况要留够 —— 预算太小会把"最后那级能用的通道"
# 直接饿死（它恰恰是受限网络里最可能成功的一级）
DISCOVERY_BUDGET = 120
# 版本发现失败后的重试间隔（秒）：网络抖动不该让应用整整一个检查周期（默认 6h）不再查
RETRY_AFTER_FAILURE = 900

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
# 站点资源目录随上游重构搬过家，写死任何一个都会在升级后打出/留下起不来的应用。
# 顺序与上游 app.adapters.system.update._resource_source_dir() 保持一致（新 → 旧）。
RESOURCE_SUBDIRS = (
    ("application", "site"),
    ("infrastructure",),
    ("adapters", "network"),
    ("helper",),
)
# 站点索引的版本标记，与 mp_resources.RESOURCE_FLAG 必须一致。
# 上游资源仓库同时分发 user.sites.bin / user.sites.v2.bin / user.sites.v3.bin，
# 只有与当前 sites 扩展匹配的那一份能被解出站点列表；用旧版会让"站点认证"页
# 显示 No data available，而文件看起来都在。
RESOURCE_FLAG = "v3"

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
        """加速前缀：GITHUB_PROXY → GITHUB_PROXY_MIRRORS → 内置列表，去重保序。

        只用于 github.com 系 URL（归档下载、网页兜底版本发现）；api.github.com
        直连，不受这里影响（见 DIRECT_ONLY）。

        GITHUB_PROXY 是**单值**且上游 MoviePilot 自己也在用（资源包下载），不能改成
        列表；GITHUB_PROXY_MIRRORS 是本更新器专用的多值扩展（逗号分隔），
        内置镜像失效时可以不动代码就换一批。
        """
        out = []
        raw = [self.get("GITHUB_PROXY", "")] + self.get("GITHUB_PROXY_MIRRORS", "").split(",")
        for item in raw:
            p = item.strip()
            if not p or p.lower() == "none" or not p.startswith(("http://", "https://")):
                continue
            if not p.endswith("/"):
                p += "/"
            if p not in out:
                out.append(p)
        for p in DEFAULT_PROXIES:
            if p not in out:
                out.append(p)
        return tuple(out)


# ---------------------------------------------------------------------------
# 版本与状态
# ---------------------------------------------------------------------------
def parse_py_value(text: str, key: str) -> str:
    """从 version.py 之类的文本里读 `KEY = 'value'`。"""
    m = re.search(rf"^{re.escape(key)}\s*=\s*['\"]([^'\"]+)['\"]", text or "", re.MULTILINE)
    return m.group(1).strip() if m else ""


def read_py_value(path: Path, key: str) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return parse_py_value(text, key)


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
# 网络（github.com 系 URL：直连 -> 加速；api.github.com：只直连）
# ---------------------------------------------------------------------------
def _candidate_urls(url: str, proxies: tuple) -> list:
    return [url] + [f"{p}{url}" for p in proxies]


def _budget_exhausted(deadline) -> bool:
    return deadline is not None and time.monotonic() > deadline


def http_json(url: str, proxies: tuple, timeout: int = DISCOVERY_TIMEOUT, deadline=None):
    """GET 一个 JSON。proxies 传空元组（DIRECT_ONLY）表示只直连、不加加速前缀。"""
    for u in _candidate_urls(url, proxies):
        if _budget_exhausted(deadline):
            log("  版本发现已超出时间预算，停止尝试")
            return None
        try:
            req = urllib.request.Request(
                u, headers={"User-Agent": UA,
                            "Accept": "application/vnd.github+json"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001 - 逐个降级，最后统一报错
            log(f"  请求失败 {u}: {e}")
    return None


def fetch_text(url: str, proxies: tuple, timeout: int = DISCOVERY_TIMEOUT,
               deadline=None, limit: int = 512 * 1024):
    """取一小段文本（HTML / XML），返回 (最终URL, 文本)；全部通道失败返回 (None, None)。

    只读前 limit 字节：这里要的是页面里的 tag，不是整页内容。
    同时返回最终 URL —— 通道转发重定向时，URL 本身就带着 /releases/tag/vX.Y.Z，
    比解析正文更可靠。

    注意不要加 Accept-Encoding：urllib 不会自动解压，带上就得自己解 gzip。
    """
    for u in _candidate_urls(url, proxies):
        if _budget_exhausted(deadline):
            log("  版本发现已超出时间预算，停止尝试")
            return None, None
        try:
            req = urllib.request.Request(u, headers={
                "User-Agent": UA,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                final = resp.geturl()
                text = resp.read(limit).decode("utf-8", errors="replace")
            if text.strip():
                return final, text
            log(f"  响应为空 {u}")
        except Exception as e:  # noqa: BLE001
            log(f"  请求失败 {u}: {e}")
    return None, None


def download_file(url: str, dest: Path, proxies: tuple, timeout: int = 60) -> bool:
    """下载到 .part 再原子改名，避免中断留下残缺文件被当成完整包。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    for u in _candidate_urls(url, proxies):
        try:
            req = urllib.request.Request(u, headers={"User-Agent": UA})
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
# /releases/tag/v3.0.4 这个片段在网页正文、og:url、canonical、atom 里形状一致，
# 一套正则应付全部来源。
_TAG_PATH_RE = re.compile(r"/releases/tag/([A-Za-z0-9._\-]+)")
_OG_URL_RE = re.compile(r"<meta[^>]+property=[\"']og:url[\"'][^>]+content=[\"']([^\"']+)", re.I)
_CANONICAL_RE = re.compile(r"<link[^>]+rel=[\"']canonical[\"'][^>]+href=[\"']([^\"']+)", re.I)
_ATOM_ENTRY_RE = re.compile(r"<entry\b.*?</entry>", re.I | re.S)
_PRE_SUFFIX_RE = re.compile(r"[-.](alpha|beta|rc)", re.I)


def is_prerelease(tag: str) -> bool:
    """v3.1.0-beta2 / v3.1.0.rc1 是预发布，v3.1.0 不是。"""
    return bool(_PRE_SUFFIX_RE.search(tag or ""))


def tag_from_release_page(text: str) -> str:
    """从 releases 网页里取 tag：先信 og:url / canonical（唯一且必在 <head>），
    再退化为全文首个匹配。"""
    for rx in (_OG_URL_RE, _CANONICAL_RE):
        m = rx.search(text or "")
        if m:
            found = _TAG_PATH_RE.search(m.group(1))
            if found:
                return found.group(1)
    m = _TAG_PATH_RE.search(text or "")
    return m.group(1) if m else ""


def parse_atom_releases(xml_text: str) -> list:
    """解析 releases.atom → [(tag, title, published_at)]，保持订阅源顺序（新 → 旧）。"""
    out = []
    for block in _ATOM_ENTRY_RE.findall(xml_text or ""):
        link = _TAG_PATH_RE.search(block)
        if not link:
            continue
        title = re.search(r"<title>(.*?)</title>", block, re.S)
        updated = re.search(r"<updated>([^<]+)</updated>", block)
        out.append((link.group(1),
                    html.unescape(title.group(1).strip()) if title else "",
                    updated.group(1).strip() if updated else ""))
    return out


def fetch_raw_version(proxies: tuple, deadline=None):
    """兜底通道：从分支的 version.py 取 APP_VERSION，取不到返回 (None, None)。

    只读几 KB 文本，不下载任何包；靠 TAG_RE 过滤掉 v1/v2 分支的版本号。
    """
    for ref in RAW_REFS:
        final, text = fetch_text(RAW_VERSION.format(repo=BACKEND_REPO, ref=ref), proxies,
                                 deadline=deadline, limit=8192)
        tag = parse_py_value(text or "", "APP_VERSION")
        if tag and TAG_RE.match(tag):
            log(f"  版本来源: {ref} 分支的 version.py（{tag}）")
            return tag, {"tag_name": tag, "name": "", "published_at": None}
    return None, None


def fetch_latest_release(cfg: Config):
    """返回 (tag, meta)；查不到返回 (None, None)。

    按"信息量 / 可靠性"排序依次尝试，任一成功即返回：

      1. GitHub API            元数据最全（name / published_at）；**只直连，不走加速**
      2. 网页 releases/latest  github.com 域名，GitHub 只把它指向**正式版**
      3. releases.atom 订阅源  按发布顺序列出 tag（含预发布，按通道过滤）
      4. 分支 version.py       raw 文件型 URL，镜像普遍转发（见 RAW_VERSION 注释）

    第 1 级用 DIRECT_ONLY：加速镜像对 api.github.com 的支持参差不齐，不支持时
    会挂到 SSL 握手超时而不是快速报错。第 2~4 级存在的理由见 WEB_LATEST 与
    RAW_VERSION 的注释：能下包（github.com 系）就一定查得到版本。
    prerelease 通道没有"latest"语义，只能从列表 / 订阅源 / 分支里挑最新的匹配项。
    整体受 DISCOVERY_BUDGET 约束。
    """
    proxies = cfg.proxies
    prerelease = cfg.channel == "prerelease"
    deadline = time.monotonic() + DISCOVERY_BUDGET

    # 1) API（只直连）
    if prerelease:
        data = http_json(API_LIST.format(repo=BACKEND_REPO), DIRECT_ONLY,
                         timeout=API_TIMEOUT, deadline=deadline)
        for item in (data if isinstance(data, list) else []):
            tag = str(item.get("tag_name") or "")
            if TAG_RE.match(tag):
                return tag, item
    else:
        data = http_json(API_LATEST.format(repo=BACKEND_REPO), DIRECT_ONLY,
                         timeout=API_TIMEOUT, deadline=deadline)
        if isinstance(data, dict):
            tag = str(data.get("tag_name") or "")
            if TAG_RE.match(tag):
                return tag, data

    # 2) 网页 latest（只对正式版通道有意义：它永远不含预发布）
    if not prerelease:
        final, text = fetch_text(WEB_LATEST.format(repo=BACKEND_REPO), proxies,
                                 deadline=deadline)
        m = _TAG_PATH_RE.search(final or "")
        tag = m.group(1) if m else tag_from_release_page(text or "")
        if tag and TAG_RE.match(tag) and not is_prerelease(tag):
            log(f"  版本来源: 网页 releases/latest（{tag}）")
            return tag, {"tag_name": tag, "name": "", "published_at": None}

    # 3) atom 订阅源
    final, text = fetch_text(WEB_ATOM.format(repo=BACKEND_REPO), proxies, deadline=deadline)
    for tag, title, published in parse_atom_releases(text or ""):
        if not TAG_RE.match(tag):
            continue
        if not prerelease and is_prerelease(tag):
            continue
        log(f"  版本来源: 网页 releases.atom（{tag}）")
        return tag, {"tag_name": tag, "name": title, "published_at": published}

    # 4) 分支 version.py（raw 文件型 URL，镜像普遍转发）
    tag, meta = fetch_raw_version(proxies, deadline=deadline)
    if tag and (prerelease or not is_prerelease(tag)):
        return tag, meta
    return None, None


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


# ---------------------------------------------------------------------------
# 平台 marker 求值（依赖清单里必须过滤掉其他平台专属的包）
# ---------------------------------------------------------------------------
_MARKER_ATOM_RE = re.compile(
    r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*(==|!=)\s*['\"]([^'\"]*)['\"]\s*$")


def _marker_env() -> dict:
    """当前平台在 PEP 508 marker 里的取值。"""
    sys_platform = {"linux": "linux", "darwin": "darwin",
                    "win32": "win32", "cygwin": "win32"}.get(sys.platform, sys.platform)
    machine = platform.machine().lower()
    platform_machine = {"amd64": "AMD64", "x86_64": "x86_64", "x86-64": "x86_64",
                        "aarch64": "aarch64", "arm64": "arm64"}.get(machine, machine)
    return {"sys_platform": sys_platform, "platform_machine": platform_machine}


def _marker_atom_ok(atom: str) -> bool:
    """单个 `var == 'value'` / `var != 'value'` 原子；看不懂的一律放行。"""
    m = _MARKER_ATOM_RE.match(atom)
    if not m:
        return True
    var, op, value = m.group(1), m.group(2), m.group(3)
    env = _marker_env()
    if var not in env:
        return True          # python_version / implementation_name 等不参与过滤
    return env[var] == value if op == "==" else env[var] != value


def marker_allows(marker: str) -> bool:
    """判断 PEP 508 marker 在当前平台是否成立（只服务于"别装其他平台的包"）。

    uv export 产出的清单是 **universal** 的：darwin 专属的 pyobjc-*、win32 专属的
    pywin32 都带着 marker 躺在 requirements.lock.txt 里。早期实现只看包名，把它们
    当成本机依赖，于是在 NAS（Linux）上 pip 必然装不出 macOS 框架包（连 sdist 都
    构建不了：Failed to build 'pyobjc-core'），整个自更新因此失败并回滚 ——
    "重启即升级"就此永久失效。

    求值策略刻意保守：
      * 空 marker、含 extra 的 → True（extra 在导出时已按 group 定好，不在这里判）
      * 顶层按 or、段内按 and 拆，原子交给 _marker_atom_ok
      * 任何解析不出的部分都算 True：宁可多装一个包，也不能因为看不懂 marker 而
        漏掉本机真正需要的依赖（最坏退回"不过滤"的旧行为，而不是装出起不来的版本）
    """
    text = (marker or "").strip()
    if not text or "extra" in text:
        return True
    for or_part in re.split(r"\bor\b", text):
        atoms = [a.strip().strip("()").strip() for a in re.split(r"\band\b", or_part)]
        atoms = [a for a in atoms if a]
        if atoms and all(_marker_atom_ok(a) for a in atoms):
            return True
    return False


_REQUIREMENT_RE = re.compile(
    r"^([A-Za-z0-9_.\-]+)\s*(\[[^\]]*\])?\s*(==|~=|>=)?\s*([^\s;#]+)\s*(?:;(.*))?")


def parse_requirements(path: Path) -> dict:
    """解析 requirements.lock.txt → {名: 版本}（按当前平台过滤 marker，见 marker_allows）。"""
    pins = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return pins
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        m = _REQUIREMENT_RE.match(line)
        if m and marker_allows(m.group(5) or ""):
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
        text = str(dep)
        m = re.match(r"^([A-Za-z0-9_.\-]+)", text)
        if not m:
            continue
        # 平台专属的直接依赖同样要过滤（如 `pywin32==312 ; sys_platform == 'win32'`），
        # 否则它会被当成"本机也需要"而拉进安装列表。
        if not marker_allows(text.split(";", 1)[1] if ";" in text else ""):
            continue
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


# pip 找不到某个**具体版本**时的报错（两种措辞：新版 pip 报 No matching distribution，
# 旧版/某些镜像报 Could not find a version that satisfies）。
_PIP_NOT_FOUND_RE = re.compile(
    r"(?:No matching distribution found for|Could not find a version that satisfies "
    r"the requirement)\s+([A-Za-z0-9_.\-]+)==([^\s;,)]+)")


def _pin_parts(pin: str) -> tuple:
    """`name==version` → (规范化名, 版本)。"""
    name, _, ver = pin.partition("==")
    return name.lower().replace("_", "-"), ver


def install_dependencies(cfg: Config, pins: list) -> bool:
    """pip 补装缺失/变更的依赖。

    --no-deps：uv.lock 里已经包含完整传递闭包，再让 pip 解析一次既没必要也有害
    （构建期的踩坑：uv 重新解析会把 pyproject 里被 exclude 的 crcmod 又拉回来）。
    先尝试 --only-binary=:all:：NAS 上通常没有编译器，装纯 wheel 最快也最稳；
    若某个包只有 sdist，再退一轮允许本地构建（此时编出来的是 NAS 自己的
    glibc，不存在跨机不兼容问题）。

    容错（真实故障）：上游 uv.lock 可能**先于 PyPI 发布**引用某个版本（实测
    moviepilot-rust==0.3.6，各镜像最高只有 0.3.5）。此时那个包永远装不上，
    旧实现直接判"依赖同步失败"并回滚 —— 一次发布顺序问题就让"重启即升级"
    永久失效。现在改为：只有当**所有镜像、两种模式全都试过**之后，才把
    「源上确实没有这个版本」且「本机已装同名包」的包降级为警告并跳过；
    本机没装的包（= 缺失依赖）仍然整体失败，绝不放过。
    """
    if not pins:
        return True
    installed = installed_versions()
    skipped = {}                     # {规范化名: 源上找不到的目标版本}
    for _round in (0, 1):
        not_found = set()            # 本轮所有镜像报过"没有该版本"的 (名, 版本)
        for only_binary in (True, False):
            for mirror in PIP_MIRRORS:
                pending = [p for p in pins if _pin_parts(p)[0] not in skipped]
                if not pending:
                    log("==> 依赖同步完成")
                    return True
                cmd = [cfg.python, "-m", "pip", "install",
                       "--no-deps", "--no-cache-dir", "--disable-pip-version-check",
                       "-i", mirror, "--trusted-host", _mirror_host(mirror),
                       "--timeout", "30", "--retries", "2"]
                if only_binary:
                    cmd.append("--only-binary=:all:")
                cmd += pending
                log(f"==> 同步依赖（{'仅 wheel' if only_binary else '允许源码构建'}，"
                    f"{mirror}）：{len(pending)} 个包")
                try:
                    r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
                except subprocess.TimeoutExpired:
                    log("    依赖安装超时，换下一个镜像")
                    continue
                if r.returncode == 0:
                    log("==> 依赖同步完成")
                    return True
                err = (r.stderr or "") + (r.stdout or "")
                tail = err.strip().splitlines()[-3:]
                log("    失败：" + " | ".join(tail))
                not_found.update(_PIP_NOT_FOUND_RE.findall(err))
        # 整轮（全部镜像 × 两种模式）都失败：只对"源上没这个版本 + 本机已装"降级跳过，
        # 并在日志里留痕，便于运行异常时定位到"某个包其实没升上去"。
        added = False
        for name, ver in sorted(not_found):
            key = name.lower().replace("_", "-")
            have = installed.get(key)
            if key in skipped or not have:
                continue
            skipped[key] = ver
            log(f"    警告：{key}=={ver} 在所有镜像上都不可得（上游锁文件早于 PyPI 发布？），"
                f"保留已装版本 {have} 继续更新；若运行异常可用 cmd/main rollback 回退")
            added = True
        if not added:
            return False
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


def _existing_resource_dirs(root: Path) -> list:
    """返回 root 下所有存在的候选站点资源目录（新 → 旧）。"""
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
    """定位 root 下应当存放站点资源产物的目录。

    优先取源码里真实存在的最"新"目录；一个都找不到时按当前约定
    （app/application/site）返回，create=True 时顺带建出来。
    """
    found = _existing_resource_dirs(root)
    if found:
        return found[0]
    target = Path(root) / "app"
    for part in RESOURCE_SUBDIRS[0]:
        target = target / part
    if create:
        target.mkdir(parents=True, exist_ok=True)
    return target


def restore_helper_resources(backup_root: Path, dst_root: Path) -> int:
    """回填资源仓库产物。

    上游 zip 里没有 MoviePilot-Resources 的 sites 二进制（它们在独立仓库），
    而打包时我们已把它们放进后端源码的站点资源目录。整体替换 app/ 后必须
    把这些文件搬回**新版代码认的那个目录**，否则后端在 import 阶段就崩
    （老版本是 "No module named 'app.helper.sites'"，3.0.3 起变成
    "No module named 'app.application.site.sites'"）。

    源目录要在备份树里按新旧顺序全找一遍：老版本把资源放在 app/helper，
    新版代码却要 app/application/site，只认"与新版同名的目录"会一个都
    找不到，于是更新完就起不来。

    两个刻意的取舍：
      * 只回填满足 RESOURCE_FLAG 的那份索引（user.sites.<flag>.bin）。备份里
        可能同时躺着 user.sites.bin / v2 / v3，把旧版一起搬过去没有意义，
        它们永远不会被扩展读取。
      * 目标已存在同名文件时默认不覆盖（保留新版自带的资源），但**索引例外**：
        索引是纯数据，其格式与 sites 扩展存在版本约定。若新代码自带了更新的
        扩展而目标目录里仍是上一代的索引，就必须用备份里的当前版覆盖，否则
        更新后站点列表为空（站点认证页 No data available）而文件却"都在"。
    """
    src_dirs = _existing_resource_dirs(backup_root)
    if not src_dirs:
        return 0
    dst_dir = resolve_resource_dir(dst_root, create=True)
    current_index = f"user.sites.{RESOURCE_FLAG}.bin"
    restored = 0
    for src_dir in src_dirs:
        for f in sorted(src_dir.iterdir()):
            if not f.is_file():
                continue
            is_index = f.name.startswith("user.sites.") and f.suffix == ".bin"
            if is_index and f.name != current_index:
                continue  # 旧版索引不回填
            keep = f.name in HELPER_KEEP_EXACT or f.name.startswith(HELPER_KEEP_PREFIX)
            if not keep:
                continue
            dst = dst_dir / f.name
            if dst.exists() and not is_index:
                continue
            shutil.copy2(str(f), str(dst))
            restored += 1
    if restored:
        log(f"    回填 {restored} 个资源包文件到 "
            f"{dst_dir.relative_to(dst_root) if str(dst_dir).startswith(str(dst_root)) else dst_dir}")
    return restored


def verify_site_resources(mp_src: Path) -> None:
    """校验当前代码的站点资源目录里有本机能用的 sites 扩展。

    这条检查是拿"更新器自己用的解释器"当判据的：cmd/main 用 $APP_PYTHON
    跑本脚本，也就是后端真正用的那个解释器，所以这里的 ABI 标签与后端
    运行期完全一致。缺失就直接判更新失败，宁可回滚也不留一个起不来的版本。
    """
    res_dir = resolve_resource_dir(mp_src)
    if not res_dir.is_dir():
        raise UpdateError(f"站点资源目录不存在: {res_dir}")
    # 索引必须**恰好**是当前约定的那一版。上游资源仓库同时分发
    # user.sites.bin / user.sites.v2.bin / user.sites.v3.bin，扩展只认与自己匹配的
    # 那一份；只判断 "user.sites.*.bin" 会放过"只有旧版索引"的情况，于是更新闸门
    # 放行、留下一个站点列表为空的版本（站点认证页 No data available）。
    wanted_index = f"user.sites.{RESOURCE_FLAG}.bin"
    if not (res_dir / wanted_index).is_file():
        present = sorted(p.name for p in res_dir.iterdir()
                         if p.is_file() and p.name.startswith("user.sites.")
                         and p.suffix == ".bin")
        raise UpdateError(
            f"站点资源目录缺少 {wanted_index}"
            + (f"（现有旧版索引: {', '.join(present)}）" if present else "")
            + f": {res_dir}")
    ver = f"{sys.version_info.major}{sys.version_info.minor}"
    machine = platform.machine().lower()
    if machine in ("arm64", "aarch64"):
        machine = "aarch64"
    elif machine in ("x86_64", "amd64"):
        machine = "x86_64"
    wanted = []
    if os.name == "posix" and sys.platform != "darwin":
        wanted.append(f"sites.cpython-{ver}-{machine}-linux-gnu.so")
    elif sys.platform == "darwin":
        wanted.append(f"sites.cpython-{ver}-darwin.so")
    else:
        wanted.append(f"sites.cp{ver}-win_amd64.pyd")
    if not any((res_dir / name).is_file() for name in wanted):
        raise UpdateError(
            f"站点资源目录缺少本机可用的 sites 扩展（需要 {' / '.join(wanted)}）: {res_dir}")


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


def collect_prepared_resources(data: dict) -> list:
    """从上游清单里挑出校验通过的站点资源文件（残缺/校验失败的一律丢弃）。"""
    files = []
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
        files.append(p)
    return files


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
    """把上游下载好的站点资源文件装进当前代码认的站点资源目录。

    目录名随上游重构变过（app/helper → app/application/site），这里按源码
    实际结构解析，不能写死。
    """
    res_dir = resolve_resource_dir(cfg.mp_src, create=True)
    applied = 0
    for p in files:
        try:
            shutil.copy2(str(p), str(res_dir / p.name))
            applied += 1
        except OSError as e:
            log(f"警告: 应用资源文件失败 {p.name}: {e}")
    if applied:
        try:
            rel = res_dir.relative_to(cfg.mp_src)
        except ValueError:
            rel = res_dir
        log(f"    应用 {applied} 个上游资源文件到 {rel}")
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
        state["last_error"] = "无法获取上游最新版本（API 与网页通道均不可达，或通道内无匹配版本）"
        # 网络抖动不该让应用整整一个检查周期（默认 6h）都不再查：失败只冷却
        # RETRY_AFTER_FAILURE。把 checked_at 记在"interval - 重试间隔"之前，
        # 等价于下次检查提前到 RETRY_AFTER_FAILURE 之后。
        interval = max(cfg.get_int("MP_UPDATE_INTERVAL", 21600), 0)
        state["checked_at"] = now - max(interval - RETRY_AFTER_FAILURE, 0)
        save_state(cfg, state)
        log("错误: " + state["last_error"])
        log("      提示: 内置镜像不可用时，可在 app.env 里用 GITHUB_PROXY_MIRRORS="
            "https://镜像A/,https://镜像B/ 追加可用加速前缀（逗号分隔）")
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
        # 资源回填失败等于"更新成功但起不来"，必须在回滚窗口内拦住。
        # 只验文件存在性（不 import sites —— 它是 Cython 扩展，导入需要完整
        # 依赖环境，而这里只想挡住"资源根本没搬过来"这一类问题）。
        verify_site_resources(cfg.mp_src)
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
    # --auto 是"遵守 MP_AUTO_UPDATE 开关与检查冷却"的**默认行为**，本身不做任何事。
    # 但必须显式声明：cmd/main 的启动路径调用的是 `mp_updater --auto`，缺了它
    # argparse 会在解析阶段直接 SystemExit(2)，do_update() 根本没机会执行，于是
    # "重启即升级"静默失效（rc=2 落进 cmd/main 的 `*)` 分支，只记一行
    # "更新检查未完成（rc=2）"）。历史上这个功能因此一次都没生效过。
    parser.add_argument("--auto", action="store_true",
                        help="遵守 MP_AUTO_UPDATE 开关与检查冷却（启动路径用，默认行为）")
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
