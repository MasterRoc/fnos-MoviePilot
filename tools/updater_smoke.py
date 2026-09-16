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
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile
from argparse import Namespace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SANDBOX = ROOT / ".local-build" / "_smoke" / "updater"
REAL_SRC = ROOT / ".local-build" / "_src" / "v303" / "MoviePilot-3.0.3"

os.environ.setdefault("MP_UPDATE_LOG", str(SANDBOX / "update.log"))
sys.path.insert(0, str(ROOT / "app" / "bin"))
import mp_updater as mod  # noqa: E402

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
    to_install, missing = mod.plan_dependencies(cfg, lock_only, {})
    check("D9 无候选范围时不装包", to_install == [], f"{len(to_install)} 个")
    # 候选范围含 fastapi 时应产生计划（fastapi 必然已装或缺失）
    to_install2, missing2 = mod.plan_dependencies(cfg, lock_only, {"fastapi": "0.100.0"})
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


if __name__ == "__main__":
    mod._real_smoke_test = mod.smoke_test
    scenario_a()
    scenario_a2()
    scenario_b()
    scenario_c()
    scenario_d()
    scenario_e()
    scenario_f()
    print("\n" + ("全部通过" if not FAILS else f"失败 {len(FAILS)} 项: {FAILS}"))
    sys.exit(1 if FAILS else 0)
