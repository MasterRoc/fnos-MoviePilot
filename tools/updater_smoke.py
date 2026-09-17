#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mp_updater.py 离线冒烟测试（Windows 也能跑）。

用沙箱目录模拟 fnOS 的安装布局：
  sandbox/appdest/mp          —— 后端源码（旧版 v3.0.3）
  sandbox/appdest/frontend    —— 前端 dist
  sandbox/pkgvar/config       —— CONFIG_DIR（放 app.env 与状态文件）
  sandbox/pkgvar/config/temp/movietpilot-update/ —— 上游"已下载"的安装包

场景：
  A 正常更新（禁用自检以避免真去 import fastapi）：验证替换/资源回填/前端/状态
  B 自检失败：验证自动回滚（版本、资源、前端全部还原）与失败计数
  C 上游产物校验：sha256 不符时忽略
  D 纯函数：版本比较、uv.lock 解析、依赖计划
  E/F 真实网络（可选，不可达时 SKIP）
  G 命令行参数契约
  H 版本发现降级链（API / 网页 latest / atom / 分支 version.py）与镜像配置（离线打桩）
  I 依赖清单的平台 marker 过滤（pyobjc-* 等其他平台专属包不得进安装计划）
  J 源上不可得的版本：本机已装则跳过继续，本机未装则仍然失败
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import zipfile
from argparse import Namespace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SANDBOX = ROOT / ".local-build" / "_smoke" / "updater"
REAL_SRC = ROOT / ".local-build" / "_src" / "v303" / "MoviePilot-3.0.3"

os.environ.setdefault("MP_UPDATE_LOG", str(SANDBOX / "update.log"))
sys.path.insert(0, str(ROOT / "app" / "bin"))
import mp_updater as mod  # noqa: E402

# 场景 A 会把 mod.install_dependencies 打桩成 no-op（Windows 上不能真装 179 个包）
# 且**故意不复原**（B 之后的场景都依赖这个桩）。想测真函数必须提前留一份引用，
# 否则测的就是那个 lambda —— 表现为"用例永远通过/永远失败"。
REAL_INSTALL_DEPS = mod.install_dependencies

FAILS = []


def check(label, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + label + (f"  <- {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(label)


def sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def build_sandbox(tag="v3.0.4"):
    if SANDBOX.exists():
        shutil.rmtree(SANDBOX)
    appdest = SANDBOX / "appdest"
    mp = appdest / "mp"
    fe = appdest / "frontend"
    cfgdir = SANDBOX / "pkgvar" / "config"
    tmp = SANDBOX / "pkgvar" / "tmp"
    for d in (mp / "app" / "helper", fe, cfgdir, tmp):
        d.mkdir(parents=True, exist_ok=True)

    (mp / "version.py").write_text(
        "APP_VERSION = 'v3.0.3'\nFRONTEND_VERSION = 'v3.0.3'\n", encoding="utf-8")
    (mp / "app" / "main.py").write_text("print('old backend')\n", encoding="utf-8")
    (mp / "app" / "helper" / "user.sites.v3.bin").write_text("OLD-SITES-DATA", encoding="utf-8")
    (mp / "app" / "helper" / "sites.cpython-314-aarch64-linux-gnu.so").write_bytes(b"OLD-SO")
    (mp / "app" / "helper" / ".resource-compat").write_text("compat", encoding="utf-8")
    # 空清单：候选范围为空集，真实 pip 安装由各场景自行打桩
    (mp / "requirements.lock.txt").write_text("", encoding="utf-8")
    (fe / "index.html").write_text("<h1>old</h1>", encoding="utf-8")
    (cfgdir / "app.env").write_text("CONFIG_DIR=%s\nMP_UPDATE_DEPS=1\n" % cfgdir,
                                    encoding="utf-8")

    # 新版本源码树（顶层目录名与上游 zip 一致）
    new_root = SANDBOX / "newsrc" / f"MoviePilot-{tag.lstrip('v')}"
    (new_root / "app").mkdir(parents=True, exist_ok=True)
    (new_root / "app" / "main.py").write_text("print('new backend')\n", encoding="utf-8")
    (new_root / "version.py").write_text(
        f"APP_VERSION = '{tag}'\nFRONTEND_VERSION = '{tag}'\n", encoding="utf-8")
    if REAL_SRC.exists():
        for name in ("pyproject.toml", "uv.lock"):
            src = REAL_SRC / name
            if src.exists():
                shutil.copy2(src, new_root / name)

    # 后端 zip（含顶层目录）
    backend_zip = SANDBOX / "backend.zip"
    with zipfile.ZipFile(backend_zip, "w") as zf:
        for p in new_root.rglob("*"):
            zf.write(p, f"{new_root.name}/{p.relative_to(new_root)}")
    # 前端 zip（故意套一层 dist/，验证提升逻辑）
    frontend_zip = SANDBOX / "frontend.zip"
    with zipfile.ZipFile(frontend_zip, "w") as zf:
        zf.writestr("dist/index.html", "<h1>new</h1>")

    up = cfgdir / "temp" / "moviepilot-update"
    up.mkdir(parents=True, exist_ok=True)
    manifest = {
        "targets": ["application"],
        "version": tag,
        "frontend_version": tag,
        "backend_archive": str(backend_zip),
        "frontend_archive": str(frontend_zip),
        "backend_sha256": sha256(backend_zip),
        "frontend_sha256": sha256(frontend_zip),
    }
    (up / "install.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                                     encoding="utf-8")

    os.environ["MP_SRC"] = str(mp)
    os.environ["FRONTEND_DIR"] = str(fe)
    os.environ["CONFIG_DIR"] = str(cfgdir)
    os.environ["APP_PYTHON"] = sys.executable
    os.environ["TRIM_PKGTMP"] = str(tmp)
    os.environ["MP_UPDATE_DEPS"] = "1"
    os.environ["MP_AUTO_UPDATE"] = "1"
    os.environ.pop("SHARE_LOG", None)
    return mp, fe, cfgdir


def scenario_a():
    print("\n=== 场景 A：正常更新（复用上游已下载包）===")
    mp, fe, cfgdir = build_sandbox()
    cfg = mod.Config()
    # 两个外部副作用打桩：Windows 上既不能真装 179 个包，也没有 fastapi 可 import
    mod.smoke_test = lambda c: None
    mod.install_dependencies = lambda c, pins: True
    rc = mod.do_update(cfg, Namespace(check=False, rollback=False, force=True))
    check("A1 退出码=10（已更新）", rc == mod.EXIT_UPDATED, f"rc={rc}")
    check("A2 后端版本已升级", "APP_VERSION = 'v3.0.4'" in (mp / "version.py").read_text())
    check("A3 后端代码已替换", "new backend" in (mp / "app" / "main.py").read_text())
    helper = mp / "app" / "helper"
    check("A4 资源文件已回填（user.sites）",
          (helper / "user.sites.v3.bin").exists()
          and (helper / "user.sites.v3.bin").read_text() == "OLD-SITES-DATA")
    check("A5 资源文件已回填（sites.so）",
          (helper / "sites.cpython-314-aarch64-linux-gnu.so").exists())
    check("A6 前端已替换（dist/ 提升）", "new" in (fe / "index.html").read_text())
    state = json.loads((cfgdir / "mp_update.json").read_text(encoding="utf-8"))
    check("A7 状态文件记录新版本", state.get("backend_version") == "v3.0.4", str(state))
    check("A8 上游 install.json 已消费",
          not (cfgdir / "temp" / "moviepilot-update" / "install.json").exists())
    backups = sorted(p.name for p in (mp.parent / ".mp-backup").iterdir())
    check("A9 已生成备份", len(backups) == 1, str(backups))


def scenario_a2():
    print("\n=== 场景 A2：关闭依赖同步且缺新依赖时应拒绝更新 ===")
    mp, fe, cfgdir = build_sandbox()
    os.environ["MP_UPDATE_DEPS"] = "0"       # 关键：不允许装依赖
    cfg = mod.Config()
    mod.smoke_test = lambda c: None
    mod.install_dependencies = lambda c, pins: True
    rc = mod.do_update(cfg, Namespace(check=False, rollback=False, force=True))
    os.environ["MP_UPDATE_DEPS"] = "1"
    check("A2-1 退出码=1（拒绝更新）", rc == mod.EXIT_FAILED, f"rc={rc}")
    check("A2-2 代码未被改动", "v3.0.3" in (mp / "version.py").read_text())
    check("A2-3 前端未被改动", "old" in (fe / "index.html").read_text())
    state = json.loads((cfgdir / "mp_update.json").read_text(encoding="utf-8"))
    check("A2-4 错误说明指向依赖", "新增依赖" in str(state.get("last_error")))


def scenario_b():
    print("\n=== 场景 B：自检失败应自动回滚 ===")
    mp, fe, cfgdir = build_sandbox()
    cfg = mod.Config()
    mod.install_dependencies = lambda c, pins: True

    def failing_smoke(c):
        raise mod.UpdateError("模拟自检失败")

    mod.smoke_test = failing_smoke
    rc = mod.do_update(cfg, Namespace(check=False, rollback=False, force=True))
    check("B1 退出码=1（失败）", rc == mod.EXIT_FAILED, f"rc={rc}")
    check("B2 版本保持旧版", "v3.0.3" in (mp / "version.py").read_text())
    check("B3 后端代码保持旧版", "old backend" in (mp / "app" / "main.py").read_text())
    check("B4 资源文件仍在",
          (mp / "app" / "helper" / "user.sites.v3.bin").read_text() == "OLD-SITES-DATA")
    check("B5 前端保持旧版", "old" in (fe / "index.html").read_text())
    state = json.loads((cfgdir / "mp_update.json").read_text(encoding="utf-8"))
    check("B6 记录失败次数", int((state.get("failures") or {}).get("v3.0.4", 0)) == 1, str(state))


def scenario_c():
    print("\n=== 场景 C：上游产物校验 ===")
    mp, fe, cfgdir = build_sandbox()
    up = cfgdir / "temp" / "moviepilot-update"
    data = json.loads((up / "install.json").read_text(encoding="utf-8"))
    data["backend_sha256"] = "0" * 64          # 故意改坏
    (up / "install.json").write_text(json.dumps(data), encoding="utf-8")
    cfg = mod.Config()
    check("C1 sha256 不符时忽略产物", mod.upstream_artifacts(cfg, "v3.0.3") is None)
    (up / "install.json").unlink()
    check("C2 无清单时返回 None", mod.upstream_artifacts(cfg, "v3.0.3") is None)
    # 版本不高于当前也应忽略
    mp2, fe2, cfg2 = build_sandbox(tag="v3.0.2")
    cfg = mod.Config()
    check("C3 版本不高于当前时忽略", mod.upstream_artifacts(cfg, "v3.0.3") is None)


def scenario_d():
    print("\n=== 场景 D：纯函数 ===")
    vk = mod.version_key
    check("D1 版本比较 3.0.4 > 3.0.3", vk("v3.0.4") > vk("v3.0.3"))
    check("D2 正式版 > rc", vk("v3.1.0") > vk("v3.1.0-rc1"))
    check("D3 rc > beta", vk("v3.1.0-rc1") > vk("v3.1.0-beta2"))
    check("D4 无法解析的版本不参与比较", vk("dev") == (0,) and vk("dev") < vk("v3.0.0"))
    check("D5 相同版本相等", vk("v3.0.3") == vk("3.0.3"))

    if not REAL_SRC.exists():
        print("SKIP 依赖相关用例：缺少 .local-build/_src/v303 上游源码")
        return
    pins = mod.uv_lock_pins(REAL_SRC / "uv.lock")
    check("D6 uv.lock 解析出依赖", len(pins) > 100, f"共 {len(pins)} 个")
    check("D7 项目自身被排除", "moviepilot" not in pins, str(sorted(pins)[:5]))
    direct = mod.pyproject_direct_deps(REAL_SRC / "pyproject.toml")
    check("D8 pyproject 直接依赖解析", "fastapi" in direct and "httpx" in direct)
    # 候选范围为空（base/installed/direct 都不认识）时不产生安装计划。
    # 注意 direct（pyproject 直接依赖）本身也是候选来源，所以这里用"只有 uv.lock、
    # 没有 pyproject"的目录来隔离，否则 uv.lock 里上游直接依赖仍会被纳入。
    mp, fe, cfgdir = build_sandbox()
    lock_only = SANDBOX / "lockonly"
    lock_only.mkdir(parents=True, exist_ok=True)
    shutil.copy2(REAL_SRC / "uv.lock", lock_only / "uv.lock")
    cfg = mod.Config()
    # 打桩"已装清单"：候选范围 = 随包清单 ∪ 已装环境 ∪ 直接依赖，而开发机上往往
    # 恰好装着 requests/urllib3 之类同名不同版本的包，会让"候选范围为空"这个前提
    # 不成立（用例在开发机假失败、在干净环境才通过）。显式清空才测的是意图。
    real_installed = mod.installed_versions
    mod.installed_versions = lambda: {}
    to_install, missing = mod.plan_dependencies(cfg, lock_only, {})
    check("D9 无候选范围时不装包", to_install == [], f"{len(to_install)} 个")
    # 候选范围含 fastapi 时应产生计划（fastapi 必然已装或缺失）
    to_install2, missing2 = mod.plan_dependencies(cfg, lock_only, {"fastapi": "0.100.0"})
    mod.installed_versions = real_installed
    check("D10 候选范围内会产出计划", any(p.startswith("fastapi==") for p in to_install2),
          str(to_install2[:3]))


def scenario_e():
    print("\n=== 场景 E：真实网络检查（可选）===")
    mp, fe, cfgdir = build_sandbox()
    cfg = mod.Config()
    try:
        rc = mod.do_check(cfg)
        check("E1 版本检查联网成功", rc == mod.EXIT_OK, "rc=%s" % rc)
    except Exception as e:  # noqa: BLE001
        print(f"SKIP 网络不可达：{e}")


def scenario_f():
    print("\n=== 场景 F：真实下载链路（GitHub / 加速代理）===")
    cfg = mod.Config()
    dl = SANDBOX / "dl"
    dl.mkdir(parents=True, exist_ok=True)
    target = dl / "backend.zip"
    try:
        ok = mod.download_file(
            mod.BACKEND_ZIP.format(repo=mod.BACKEND_REPO, tag="v3.0.3"),
            target, cfg.proxies, timeout=120)
    except Exception as e:  # noqa: BLE001
        ok = False
        print(f"SKIP 下载异常：{e}")
    if not ok:
        print("SKIP 后端包下载失败（网络或加速代理不可达）")
        return
    size = target.stat().st_size
    check("F1 后端 zip 可下载", size > 1_000_000, f"{size} 字节")
    with zipfile.ZipFile(target) as zf:
        names = zf.namelist()
    check("F2 zip 顶层目录符合预期", bool(names) and names[0].startswith("MoviePilot-"),
          names[0] if names else "空")
    check("F3 zip 内含 version.py", any(n.endswith("/version.py") for n in names))
    fe_target = dl / "frontend.zip"
    if mod.download_file(mod.FRONTEND_ZIP.format(repo=mod.FRONTEND_REPO, tag="v3.0.3"),
                         fe_target, cfg.proxies, timeout=120):
        check("F4 前端 dist.zip 可下载", fe_target.stat().st_size > 100_000)
    else:
        print("SKIP 前端包下载失败")


def scenario_g():
    """G 命令行契约：cmd/main 传的每个开关都必须是合法参数。

    历史故障：cmd/main 的启动路径调用 `mp_updater --auto`，而 argparse 没有
    定义 --auto，于是解析阶段直接 SystemExit(2)，do_update() 从未执行 ——
    "重启即升级"静默失效（rc=2 被 cmd/main 的 `*)` 分支吞掉）。

    这条检查不依赖平台与网络，直接钉住"脚本会拒绝自己的调用方"这类问题。
    """
    print("\n=== 场景 G：命令行参数契约（与 cmd/main 保持一致）===")
    script = ROOT / "app" / "bin" / "mp_updater.py"
    expected = ["--check", "--rollback", "--force", "--auto"]

    # 1) 直接解析 argparse 定义，而不真的执行各开关：
    #    --force / --rollback 会真的联网下载或回滚，跑起来既慢又不确定。
    r = subprocess.run([sys.executable, str(script), "--help"],
                       capture_output=True, text=True, timeout=60)
    help_text = (r.stdout or "") + (r.stderr or "")
    for name in expected:
        check(f"G1 --help 列出 {name}", name in help_text,
              help_text.strip().splitlines()[-1] if help_text else "(无输出)")

    # 2) 关键回归：--auto 必须能被解析。
    #    注意不能只看 rc==2 —— 脚本自身也用 EXIT_USAGE=2 表示"环境/用法错误"，
    #    两者撞码。argparse 拒绝的**唯一可靠特征**是它打印 "unrecognized arguments"。
    for name in ("--auto", "--force"):
        try:
            r = subprocess.run(
                [sys.executable, str(script), name],
                capture_output=True, text=True, timeout=45,
                env={**os.environ,
                     "CONFIG_DIR": str(SANDBOX / "pkgvar" / "config"),
                     "MP_SRC": str(SANDBOX / "appdest" / "mp"),
                     # 关掉自动更新，让 --auto 立刻走"跳过"分支，避免联网
                     "MP_AUTO_UPDATE": "0"})
        except subprocess.TimeoutExpired:
            # 超时说明参数已被接受并进入了真实流程（旧代码是瞬间被拒绝）
            check(f"G2 {name} 被接受（进入主流程而非 argparse 拒绝）", True)
            continue
        out = (r.stdout or "") + (r.stderr or "")
        rejected = "unrecognized arguments" in out
        check(f"G2 {name} 不是 argparse 拒绝的参数", not rejected,
              f"rc={r.returncode} {out.strip()[:200]}")

    # 3) 钉住 cmd/main 实际使用的调用形式，防止两边再次漂移
    main_sh = (ROOT / "cmd" / "main").read_text(encoding="utf-8", errors="replace")
    used = set()
    for line in main_sh.splitlines():
        s = line.strip()
        if s.startswith("run_updater"):
            for tok in s.split()[1:]:
                if tok.startswith("--"):
                    used.add(tok)
    missing = sorted(t for t in used if t not in expected)
    check("G3 cmd/main 用到的开关都在预期集合内", not missing, f"未定义: {missing}")


def scenario_h():
    """H 版本发现降级链：API → 网页 latest → releases.atom，以及镜像配置。

    真实故障（本次修复的起点）：api.github.com 被网络挡住或镜像不转发该域名时，
    旧实现直接放弃更新；而"下载"走的 github.com 通道其实是通的。这里全部用打桩
    钉住降级链、通道过滤与失败后的重试冷却，不依赖网络。
    """
    print("\n=== 场景 H：版本发现降级链（离线打桩）===")
    mp, fe, cfgdir = build_sandbox()

    check("H1 预发布后缀被识别", mod.is_prerelease("v3.1.0-beta2")
          and mod.is_prerelease("v3.1.0.rc1"))
    check("H2 正式版不误判", not mod.is_prerelease("v3.0.4"))

    latest_html = ('<html><head><meta property="og:url" '
                   'content="https://github.com/jxxghp/MoviePilot/releases/tag/v3.0.4" />'
                   "</head><body>...</body></html>")
    check("H3 从 og:url 取 tag", mod.tag_from_release_page(latest_html) == "v3.0.4")
    check("H4 canonical 兜底",
          mod.tag_from_release_page('<link rel="canonical" href='
                                    '"https://github.com/x/y/releases/tag/v3.0.5">') == "v3.0.5")

    atom = ('<feed>'
            '<entry><link rel="alternate" href="https://github.com/jxxghp/MoviePilot'
            '/releases/tag/v3.1.0-beta2"/><title>v3.1.0-beta2</title>'
            '<updated>2026-09-10T00:00:00Z</updated></entry>'
            '<entry><link rel="alternate" href="https://github.com/jxxghp/MoviePilot'
            '/releases/tag/v3.0.4"/><title>v3.0.4</title>'
            '<updated>2026-09-01T00:00:00Z</updated></entry>'
            "</feed>")
    parsed = mod.parse_atom_releases(atom)
    check("H5 atom 解析保序（新 → 旧）",
          [t[0] for t in parsed] == ["v3.1.0-beta2", "v3.0.4"], str(parsed))
    check("H6 atom 带出发布时间", parsed[1][2].startswith("2026-09-01"), str(parsed[1]))

    real_json, real_text, real_fetch = mod.http_json, mod.fetch_text, mod.fetch_latest_release
    os.environ["MP_UPDATE_CHANNEL"] = "release"

    # API 全挂 -> 网页 latest 兜底
    mod.http_json = lambda *a, **k: None
    mod.fetch_text = lambda *a, **k: (
        "https://github.com/jxxghp/MoviePilot/releases/tag/v3.0.4", latest_html)
    tag, _meta = mod.fetch_latest_release(mod.Config())
    check("H7 API 失败时网页 latest 兜底", tag == "v3.0.4", f"tag={tag}")

    # API 与网页 latest 都挂 -> atom 兜底；正式版通道必须跳过顶部的预发布
    mod.fetch_text = lambda url, *a, **k: (None, atom if "atom" in url else "")
    tag, _meta = mod.fetch_latest_release(mod.Config())
    check("H8 atom 兜底并跳过预发布（正式版通道）", tag == "v3.0.4", f"tag={tag}")

    os.environ["MP_UPDATE_CHANNEL"] = "prerelease"
    tag, _meta = mod.fetch_latest_release(mod.Config())
    check("H9 prerelease 通道取最新预发布", tag == "v3.1.0-beta2", f"tag={tag}")
    os.environ["MP_UPDATE_CHANNEL"] = "release"

    # 全部"页面型"通道都挂 -> 分支 version.py 兜底（raw 是文件型 URL，镜像普遍转发）
    def raw_only(url, *a, **k):
        if "raw.githubusercontent.com" not in url:
            return None, ""
        if "/v3/" in url:
            return url, "APP_VERSION = 'v3.0.4'\nFRONTEND_VERSION = 'v3.0.4'\n"
        # main / master 上是 v1 时代的版本号，必须被 TAG_RE 挡掉
        return url, "APP_VERSION = 'v1.9.19'\n"

    mod.fetch_text = raw_only
    tag, _meta = mod.fetch_latest_release(mod.Config())
    check("H10 末级 raw 分支兜底", tag == "v3.0.4", f"tag={tag}")

    mod.fetch_text = lambda url, *a, **k: (
        url if "raw.githubusercontent.com" in url else None,
        "APP_VERSION = 'v1.9.19'\n" if "raw.githubusercontent.com" in url else "")
    tag, _meta = mod.fetch_latest_release(mod.Config())
    check("H11 raw v1 分支版本号被过滤", tag is None, f"tag={tag}")

    mod.http_json, mod.fetch_text = real_json, real_text

    # 镜像配置：GITHUB_PROXY_MIRRORS 逗号分隔追加，非法项忽略，与内置项去重
    os.environ["GITHUB_PROXY"] = "https://gh-proxy.com/"
    os.environ["GITHUB_PROXY_MIRRORS"] = "https://a.example/ , not-a-url , https://b.example"
    check("H12 镜像列表去重保序",
          mod.Config().proxies[:3] == ("https://gh-proxy.com/", "https://a.example/",
                                       "https://b.example/"),
          str(mod.Config().proxies))
    os.environ.pop("GITHUB_PROXY_MIRRORS", None)
    os.environ.pop("GITHUB_PROXY", None)

    # API 必须只直连：带上加速前缀时，不转发 api 的镜像会挂到 SSL 握手超时而不是
    # 快速报错，白白拖长启动路径的更新检查（实测 ghfast.top）。
    seen = []
    mod.http_json = lambda url, proxies, **k: (seen.append((url, proxies)), None)[1]
    mod.fetch_text = lambda *a, **k: (None, "")
    mod.fetch_latest_release(mod.Config())
    check("H13 API 只走直连（不带加速前缀）",
          len(seen) == 1 and seen[0][1] == (), str(seen))
    mod.http_json, mod.fetch_text = real_json, real_text

    # 版本发现失败：只冷却 RETRY_AFTER_FAILURE，不占用整个检查周期
    mp, fe, cfgdir = build_sandbox()
    (cfgdir / "temp" / "moviepilot-update" / "install.json").unlink()  # 逼它走联网查版本
    os.environ["MP_UPDATE_INTERVAL"] = "21600"
    mod.fetch_latest_release = lambda c: (None, None)
    rc = mod.do_update(mod.Config(), Namespace(check=False, rollback=False, force=True))
    state = json.loads((cfgdir / "mp_update.json").read_text(encoding="utf-8"))
    remaining = 21600 - (time.time() - float(state.get("checked_at") or 0))
    check("H14 发现失败返回失败码", rc == mod.EXIT_FAILED, f"rc={rc}")
    check("H15 失败后仅待 RETRY_AFTER_FAILURE 再查",
          abs(remaining - mod.RETRY_AFTER_FAILURE) < 10, f"remaining={remaining:.0f}s")
    mod.fetch_latest_release = real_fetch
    os.environ.pop("MP_UPDATE_INTERVAL", None)


def scenario_i():
    """I 依赖清单的平台 marker 过滤（离线）。

    真实故障：requirements.lock.txt 由 `uv export` 生成，是 **universal** 清单 ——
    darwin 专属的 pyobjc-* 带着 marker 躺在里面。旧实现只看包名，于是在 Linux 的
    NAS 上试图 pip install pyobjc-core，必然失败（No matching distribution /
    Failed to build），整个自更新因此回滚，"重启即升级"永久失效。
    """
    print("\n=== 场景 I：依赖清单的平台 marker 过滤 ===")
    mp, fe, cfgdir = build_sandbox()
    cfg = mod.Config()

    check("I1 空 marker 放行", mod.marker_allows(""))
    check("I2 未知变量保守放行（不误删本机依赖）",
          mod.marker_allows("python_version >= '3.14'"))
    check("I3 extra 条件放行（由导出 group 决定）", mod.marker_allows("extra == 'runtime'"))

    darwin_only = ("(platform_machine == 'arm64' and sys_platform == 'darwin') or "
                   "(platform_machine == 'x86_64' and sys_platform == 'darwin')")
    env = mod._marker_env()
    check("I4 darwin 专属 marker 按平台判定",
          mod.marker_allows(darwin_only) == (env["sys_platform"] == "darwin"), str(env))

    # 真实清单节选（与 uv export 的输出同形）
    (mp / "requirements.lock.txt").write_text(
        "# 节选（uv export 的 universal 输出）\n"
        "moviepilot-rust==0.3.5 ; (platform_machine == 'arm64' and sys_platform == 'darwin')"
        " or (platform_machine == 'x86_64' and sys_platform == 'darwin')"
        " or (platform_machine == 'aarch64' and sys_platform == 'linux')"
        " or (platform_machine == 'x86_64' and sys_platform == 'linux')"
        " or (platform_machine == 'AMD64' and sys_platform == 'win32')\n"
        f"pyobjc-core==12.2.2 ; {darwin_only}\n"
        "pywin32==312 ; platform_machine == 'AMD64' and sys_platform == 'win32'\n"
        "orjson==3.12.0\n",
        encoding="utf-8")
    base = mod.packaged_pins(cfg)
    check("I5 base 里没有 pyobjc-core", "pyobjc-core" not in base, str(sorted(base)))
    check("I6 pywin32 只在 win32 平台保留",
          ("pywin32" in base) == (env["sys_platform"] == "win32"), str(sorted(base)))
    check("I7 无条件依赖照常解析出版本号", base.get("orjson") == "3.12.0", str(base))

    # 端到端：真实 uv.lock 下的依赖计划里不得出现任何 pyobjc-*
    real_installed = mod.installed_versions
    mod.installed_versions = lambda: {"moviepilot-rust": "0.0.1", "orjson": "0.0.1",
                                      "pystray": "0.19.5"}
    new_root = SANDBOX / "newsrc" / "MoviePilot-3.0.4"
    pins, missing = mod.plan_dependencies(cfg, new_root, base)
    mod.installed_versions = real_installed
    if pins:
        check("I8 依赖计划里不含 pyobjc-*",
              not any("pyobjc" in p for p in pins), str(pins))
        if "moviepilot-rust" in base:
            check("I9 平台相关的包仍会被计划升级",
                  any(p.startswith("moviepilot-rust") for p in pins), str(pins))
        else:
            print("SKIP I9 当前平台不匹配 moviepilot-rust 的 marker")
    else:
        print("SKIP I8/I9 缺少本地 uv.lock 样本（.local-build/_src）")


def scenario_j():
    """J 依赖容错：源上没有该版本时不整体失败（离线打桩）。

    真实故障：上游 uv.lock 引用 moviepilot-rust==0.3.6，而各镜像最高只有 0.3.5。
    旧实现直接判"依赖同步失败"并回滚 —— 一次发布顺序问题就让"重启即升级"永久失效。
    现在的口径：所有镜像都试过仍缺该版本 + **本机已装**同名包 → 警告跳过；
    本机没装的包（缺失依赖）→ 仍然失败，绝不放过。
    """
    print("\n=== 场景 J：源上不可得的版本如何处理（离线打桩）===")
    mp, fe, cfgdir = build_sandbox()
    cfg = mod.Config()

    class _FailResult:
        returncode = 1
        stdout = ""
        stderr = ("ERROR: Could not find a version that satisfies the requirement "
                  "moviepilot-rust==0.3.6 (from versions: 0.3.5)\n"
                  "ERROR: No matching distribution found for moviepilot-rust==0.3.6\n")

    real_run = mod.subprocess.run
    real_mirrors = mod.PIP_MIRRORS
    real_installed = mod.installed_versions
    mod.subprocess.run = lambda *a, **k: _FailResult()
    mod.PIP_MIRRORS = ("https://mirror-a/", "https://mirror-b/")

    mod.installed_versions = lambda: {"moviepilot-rust": "0.3.5"}
    check("J1 本机已装的包在源上不可得 → 跳过并继续",
          REAL_INSTALL_DEPS(cfg, ["moviepilot-rust==0.3.6"]) is True)

    mod.installed_versions = lambda: {}
    check("J2 本机未装的包不可得 → 仍然失败（缺失依赖不放过）",
          REAL_INSTALL_DEPS(cfg, ["moviepilot-rust==0.3.6"]) is False)

    mod.subprocess.run = real_run
    mod.PIP_MIRRORS = real_mirrors
    mod.installed_versions = real_installed


if __name__ == "__main__":
    mod._real_smoke_test = mod.smoke_test
    scenario_a()
    scenario_a2()
    scenario_b()
    scenario_c()
    scenario_d()
    scenario_e()
    scenario_f()
    scenario_g()
    scenario_h()
    scenario_i()
    scenario_j()
    print("\n" + ("全部通过" if not FAILS else f"失败 {len(FAILS)} 项: {FAILS}"))
    sys.exit(1 if FAILS else 0)
