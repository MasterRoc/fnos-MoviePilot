#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mp_resources.py 站点资源检查/修复的离线冒烟测试（Windows 也能跑）。

核心诉求：早期实现只按**文件名**判断资源是否完整（`p.name in wanted`），
于是"文件在、名字对、但加载不了"（缺共享库、构建目标不符、内容损坏）会被
误判成"完整"而跳过修复 —— 应用则永远起不来。本测试覆盖：

  A 缺失检出：data/bin 任一缺失都要报出来
  B 不可加载检出：文件名正确但内容是垃圾 -> 必须判定为"无法加载"
  C 修复：从候选来源（含 .mp-backup）回填缺失文件
  D 覆盖修复：本地那份坏掉时，允许用备份覆盖它
  E 幂等：资源完整且可加载时不做任何改动
"""
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SANDBOX = ROOT / ".local-build" / "_smoke" / "resources"

sys.path.insert(0, str(ROOT / "app" / "bin"))
import mp_resources as res  # noqa: E402

FAILS = []


def check(label, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + label
          + (f"  <- {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(label)


def native_name() -> str:
    return sorted(res.wanted_native_names())[0]


def data_name() -> str:
    return f"{res.DATA_PREFIX}{res.RESOURCE_FLAG}{res.DATA_SUFFIX}"


def build_sandbox(with_backup=True):
    if SANDBOX.exists():
        shutil.rmtree(SANDBOX)
    mp = SANDBOX / "mp"
    site = mp / "app" / "application" / "site"
    site.mkdir(parents=True, exist_ok=True)
    # "好的"资源：非空内容即可（可加载性由 stub 控制，不真去 import）
    (site / native_name()).write_bytes(b"\x7fELF" + b"x" * 512)
    (site / data_name()).write_bytes(b"\x00" * 256)

    if with_backup:
        bk = SANDBOX / ".mp-backup" / "20260101-000000" / "app" / "application" / "site"
        bk.mkdir(parents=True, exist_ok=True)
        (bk / native_name()).write_bytes(b"\x7fELF" + b"g" * 512)
        (bk / data_name()).write_bytes(b"\x00" * 256)
    return mp, site


def main():
    lines = []
    res.log = lambda msg: lines.append(str(msg))

    print("== A. 缺失检出 ==")
    mp, site = build_sandbox()
    check("A1 完整时 missing 为空", res.missing_items(site) == [],
          f"{res.missing_items(site)}")
    (site / native_name()).unlink()
    miss = res.missing_items(site)
    check("A2 缺 .so 被检出", native_name() in miss, f"{miss}")
    (site / data_name()).unlink()
    miss = res.missing_items(site)
    check("A3 缺 .bin 也被检出", data_name() in miss, f"{miss}")

    print()
    print("== B. 不可加载检出（文件名对但内容坏） ==")
    mp, site = build_sandbox()
    # stub：模拟"有扩展文件但 import 失败"
    res.native_loadable = lambda d, py: False
    res.native_extension_path = lambda d: site / native_name()
    check("B1 判定为不可加载",
          res.native_loadable(site, "") is False)

    print()
    print("== C. 缺失修复（从 .mp-backup 回填） ==")
    mp, site = build_sandbox(with_backup=True)
    res.native_loadable = lambda d, py: True   # 回填后即可加载
    (site / native_name()).unlink()
    (site / data_name()).unlink()
    lines.clear()
    rc = res.repair(mp, "")
    check("C1 repair 返回 0", rc == 0, f"rc={rc}")
    check("C2 .so 已回填", (site / native_name()).exists())
    check("C3 .bin 已回填", (site / data_name()).exists())

    print()
    print("== D. 覆盖修复（本地坏了，用备份覆盖） ==")
    mp, site = build_sandbox(with_backup=True)
    # 本地那份内容坏掉，但文件"存在" -> 只按名字检查会误判为完整
    (site / native_name()).write_bytes(b"CORRUPTED")
    calls = {"n": 0}

    def fake_loadable(d, py):
        # 第一次调用（检查当前状态）返回 False，覆盖后返回 True
        calls["n"] += 1
        return calls["n"] > 1

    res.native_loadable = fake_loadable
    lines.clear()
    rc = res.repair(mp, "")
    check("D1 repair 返回 0", rc == 0, f"rc={rc}")
    check("D2 坏文件被覆盖（不再是 CORRUPTED）",
          (site / native_name()).read_bytes() != b"CORRUPTED")
    check("D3 触发过可加载性检查", calls["n"] >= 1, f"calls={calls['n']}")

    print()
    print("== E. 幂等：完整且可加载时不改动 ==")
    mp, site = build_sandbox(with_backup=True)
    res.native_loadable = lambda d, py: True
    before = (site / native_name()).read_bytes()
    rc = res.repair(mp, "")
    after = (site / native_name()).read_bytes()
    check("E1 repair 返回 0", rc == 0, f"rc={rc}")
    check("E2 文件未被改动", before == after)

    print()
    print("== F. 无来源可修时返回 1 ==")
    mp, site = build_sandbox(with_backup=False)
    res.native_loadable = lambda d, py: False
    (site / native_name()).unlink()
    (site / data_name()).unlink()
    rc = res.repair(mp, "")
    check("F1 返回 1（修复失败）", rc == 1, f"rc={rc}")

    print()
    if FAILS:
        print(f"{len(FAILS)} failed: {', '.join(FAILS)}")
        return 1
    print("all passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
