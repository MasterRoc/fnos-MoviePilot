#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cmd 生命周期脚本的 app.env 引导冒烟测试（Windows 也能跑）。

背景（本测试要钉住的线上事故）：
    install_callback 早期无条件 `cat > app.env` 重建配置，而 fnOS 的"重新安装"同样
    会走 install_callback。app.env 不只是部署参数 —— 它同时是 MoviePilot 自己的
    配置落点（后端用 dotenv 的 set_key 把用户设置写回这里），并承载两个密钥。
    一次覆盖安装就把密钥和用户配置整份清空，实际表现是：
      * RESOURCE_SECRET_KEY 变化 -> 站点索引解不开、站点页无数据，
        且只认资源 Cookie 的 system/message、system/logging 持续 401；
      * SECRET_KEY 变化 -> 登录令牌作废，前端 401 后直接登出。

测试分两层：
  A-H 行为：用真实 bash 跑 cmd/lib.sh 的四个函数（新装/覆盖安装/向导显式值/幂等）
  I-K 契约：静态断言"覆盖安装路径不得重建 app.env、不得用 sed 写 app.env、
        启动路径必须兜底补密钥"，防止以后被改回去

依赖：需要 `bash`。没有 bash 时只做静态检查并明确 SKIP 行为层，不误判失败。
"""
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CMD = ROOT / "cmd"
SANDBOX = ROOT / ".local-build" / "_smoke" / "cmdenv"

FAILS = []


def check(label, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + label
          + (f"  <- {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(label)


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 行为层：真实 bash 调用
# ---------------------------------------------------------------------------
_BEHAVIOR_SH = r"""#!/usr/bin/env bash
# 由 tools/cmd_env_smoke.py 生成，直接 source 真实 lib.sh 后调用被测函数。
set -u
REPO="$1"
WORK="$2"
. "${REPO}/cmd/lib.sh"

fail=0
ok()  { echo "  ok   $1"; }
bad() { echo "  BAD  $1"; fail=1; }
expect_eq() {  # $1 名称  $2 期望  $3 实际
    if [ "$2" = "$3" ]; then ok "$1"; else bad "$1（期望 [$2] 实际 [$3]）"; fi
}
key_value() {  # $1 文件  $2 键  -> 取第一个匹配行的值（保留 = 之后的全部字符）
    grep -m1 "^$2=" "$1" | cut -d= -f2-
}
key_count() {  # $1 文件  $2 键
    grep -c "^$2=" "$1"
}

echo "--- A 全新安装 ---"
ENV_A="${WORK}/a.env"
rm -f "${ENV_A}"
export wizard_superuser_password='Adm1n@2026'
out="$(mp_env_bootstrap "${ENV_A}" "/vol1/@appdata/moviepilot/config")"
expect_eq "A1 全新安装返回 fresh" "fresh" "${out}"
expect_eq "A2 SECRET_KEY 唯一" "1" "$(key_count "${ENV_A}" SECRET_KEY)"
expect_eq "A3 RESOURCE_SECRET_KEY 唯一" "1" "$(key_count "${ENV_A}" RESOURCE_SECRET_KEY)"
expect_eq "A4 SECRET_KEY 为 44 字符（32 字节 base64）" "44" \
    "$(printf '%s' "$(key_value "${ENV_A}" SECRET_KEY)" | wc -c | tr -d ' ')"
expect_eq "A5 RESOURCE_SECRET_KEY 为 44 字符" "44" \
    "$(printf '%s' "$(key_value "${ENV_A}" RESOURCE_SECRET_KEY)" | wc -c | tr -d ' ')"
expect_eq "A6 CONFIG_DIR 写入模板值" "/vol1/@appdata/moviepilot/config" \
    "$(key_value "${ENV_A}" CONFIG_DIR)"
expect_eq "A7 向导密码写入" "Adm1n@2026" "$(key_value "${ENV_A}" SUPERUSER_PASSWORD)"

echo "--- B 覆盖安装：保留密钥与用户配置 ---"
ENV_B="${WORK}/b.env"
cat > "${ENV_B}" <<'ENVEOF'
CONFIG_DIR=/vol1/@appdata/moviepilot/config
PORT=3011
SECRET_KEY=OLDsecret==
RESOURCE_SECRET_KEY=OLDresource==
SUPERUSER=admin
SUPERUSER_PASSWORD=OldPass@1
TMDB_API_KEY=user-configured-key
AUTH_SITE=nexusphp
ENVEOF
unset wizard_port wizard_nginx_port wizard_superuser wizard_superuser_password wizard_api_token
out="$(mp_env_bootstrap "${ENV_B}" "/vol1/@appdata/moviepilot/config")"
expect_eq "B1 覆盖安装返回 kept" "kept" "${out}"
expect_eq "B2 SECRET_KEY 未被重写" "OLDsecret==" "$(key_value "${ENV_B}" SECRET_KEY)"
expect_eq "B3 RESOURCE_SECRET_KEY 未被重写" "OLDresource==" \
    "$(key_value "${ENV_B}" RESOURCE_SECRET_KEY)"
expect_eq "B4 后端写回的用户配置保留" "user-configured-key" \
    "$(key_value "${ENV_B}" TMDB_API_KEY)"
expect_eq "B5 用户配置键 AUTH_SITE 保留" "nexusphp" "$(key_value "${ENV_B}" AUTH_SITE)"
expect_eq "B6 向导未填时密码不被清空" "OldPass@1" \
    "$(key_value "${ENV_B}" SUPERUSER_PASSWORD)"
expect_eq "B7 已有端口保留" "3011" "$(key_value "${ENV_B}" PORT)"
expect_eq "B8 缺失键补默认值" "1" "$(key_value "${ENV_B}" MP_AUTO_UPDATE)"

echo "--- C 覆盖安装 + 向导显式值 ---"
export wizard_port=3999
export wizard_superuser_password='A&b|c/d'
out="$(mp_env_bootstrap "${ENV_B}" "/vol1/@appdata/moviepilot/config")"
expect_eq "C1 向导端口覆盖生效" "3999" "$(key_value "${ENV_B}" PORT)"
expect_eq "C2 含 & | / 的值按原样写入（不走 sed 拼接）" 'A&b|c/d' \
    "$(key_value "${ENV_B}" SUPERUSER_PASSWORD)"
expect_eq "C3 多次引导密钥仍不变" "OLDsecret==" "$(key_value "${ENV_B}" SECRET_KEY)"
expect_eq "C4 键不重复写入" "1" "$(key_count "${ENV_B}" SUPERUSER_PASSWORD)"
unset wizard_port wizard_superuser_password

echo "--- D mp_env_ensure / mp_env_ensure_secret ---"
ENV_D="${WORK}/d.env"
printf 'PORT=3002\n' > "${ENV_D}"
mp_env_ensure "${ENV_D}" "PORT" "9999"
mp_env_ensure "${ENV_D}" "MP_UPDATE_DEPS" "1"
expect_eq "D1 ensure 不覆盖已有值" "3002" "$(key_value "${ENV_D}" PORT)"
expect_eq "D2 ensure 补齐缺失键" "1" "$(key_value "${ENV_D}" MP_UPDATE_DEPS)"
mp_env_ensure_secret "${ENV_D}" "SECRET_KEY"
v1="$(key_value "${ENV_D}" SECRET_KEY)"
mp_env_ensure_secret "${ENV_D}" "SECRET_KEY"
expect_eq "D3 密钥只生成一次" "${v1}" "$(key_value "${ENV_D}" SECRET_KEY)"
expect_eq "D4 密钥非空" "yes" "$([ -n "${v1}" ] && echo yes || echo no)"
expect_eq "D5 密钥可被 Fernet 接受（44 字符 base64）" "44" \
    "$(printf '%s' "${v1}" | wc -c | tr -d ' ')"
mp_env_ensure_secret "${WORK}/nope.env" "SECRET_KEY"
expect_eq "D6 文件不存在时不创建" "no" \
    "$([ -f "${WORK}/nope.env" ] && echo yes || echo no)"

echo "--- E mp_env_upsert ---"
ENV_E="${WORK}/e.env"
printf 'A=1\nB=2\n' > "${ENV_E}"
mp_env_upsert "${ENV_E}" "A" "x/y&z|w"
mp_env_upsert "${ENV_E}" "C" "3"
expect_eq "E1 upsert 覆盖已有键" "x/y&z|w" "$(key_value "${ENV_E}" A)"
expect_eq "E2 upsert 追加缺失键" "3" "$(key_value "${ENV_E}" C)"
expect_eq "E3 upsert 不影响其它键" "2" "$(key_value "${ENV_E}" B)"
mp_env_upsert "${ENV_E}" "A" "again"
expect_eq "E4 重复 upsert 不新增行" "1" "$(key_count "${ENV_E}" A)"
expect_eq "E5 重复 upsert 生效" "again" "$(key_value "${ENV_E}" A)"

echo "--- F 权限位不被 mv 破坏 ---"
# 断言语义：upsert 前后权限"不变"。若实现退回 mv（临时文件由重定向创建，权限来自
# umask），600 的 app.env 会变成 644/666 —— Windows/Git Bash 上模拟权限可能都是
# 777，此时该断言自然通过，不会误报。
ENV_F="${WORK}/f.env"
printf 'PORT=3002\n' > "${ENV_F}"
chmod 600 "${ENV_F}"
mode_before="$(stat -c '%a' "${ENV_F}" 2>/dev/null || stat -f '%Lp' "${ENV_F}" 2>/dev/null || echo '?')"
mp_env_upsert "${ENV_F}" "PORT" "3003"
mode_after="$(stat -c '%a' "${ENV_F}" 2>/dev/null || stat -f '%Lp' "${ENV_F}" 2>/dev/null || echo '?')"
expect_eq "F1 upsert 不改变文件权限（用 cat 覆盖而非 mv）" "${mode_before}" "${mode_after}"
expect_eq "F2 upsert 结果正确" "3003" "$(key_value "${ENV_F}" PORT)"

exit ${fail}
"""


def behavior_checks() -> None:
    if shutil.which("bash") is None:
        print("SKIP: 未找到 bash，跳过行为层检查（不计为失败）")
        return
    SANDBOX.mkdir(parents=True, exist_ok=True)
    script = SANDBOX / "_cmd_env_behavior.sh"
    # newline="\n"：Windows 上默认会把 \n 翻成 \r\n，bash 会因 CR 报 "command not found"
    script.write_text(_BEHAVIOR_SH, encoding="utf-8", newline="\n")
    r = subprocess.run(
        ["bash", script.relative_to(ROOT).as_posix(), ".",
         SANDBOX.relative_to(ROOT).as_posix()],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(ROOT),
    )
    out = (r.stdout or "") + (r.stderr or "")
    for line in out.strip().splitlines():
        print("      " + line)
    check("A-H 行为层全部通过", r.returncode == 0 and "BAD" not in out,
          f"rc={r.returncode}")


# ---------------------------------------------------------------------------
# 契约层：静态断言
# ---------------------------------------------------------------------------
def contract_checks() -> None:
    install_cb = read(CMD / "install_callback")
    lib = read(CMD / "lib.sh")
    main = read(CMD / "main")
    config_cb = read(CMD / "config_callback")

    check("I. install_callback 走 mp_env_bootstrap（不再自己重建 app.env）",
          "mp_env_bootstrap" in install_cb and 'cat > "${ENV_FILE}"' not in install_cb)
    check("I. install_callback 不再自行生成密钥",
          'SECRET_KEY="$(head -c' not in install_cb)
    check("I. 整份模板只存在于 lib.sh 的 fresh 分支",
          'cat > "${env_file}"' in lib)
    sed_cmds = [ln.strip() for ln in config_cb.splitlines()
                if ln.lstrip().startswith("sed ")]
    check("J. config_callback 不再用 sed 写 app.env（含 & 的值会写坏）",
          not sed_cmds, sed_cmds)
    check("J. config_callback 通过 mp_env_upsert 写值",
          config_cb.count("mp_env_upsert") >= 3)
    check("K. cmd/main 启动前兜底补密钥",
          "start)" in main and "mp_env_ensure_secret" in main)


def main() -> int:
    print("== cmd app.env 引导冒烟测试 ==\n")
    for name in ("lib.sh", "install_callback", "config_callback", "main"):
        check(f"cmd/{name} 存在", (CMD / name).is_file())

    print()
    behavior_checks()

    print()
    contract_checks()

    print()
    if FAILS:
        print(f"== 结果: {len(FAILS)} 项失败 ==")
        for f in FAILS:
            print("   - " + f)
        return 1
    print("== 结果: 全部通过 ==")
    return 0


if __name__ == "__main__":
    sys.exit(main())
