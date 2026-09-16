#!/bin/bash
### MoviePilot fnOS 生命周期脚本公共函数库
### 由 cmd/* 以 `. "${CMD_DIR}/lib.sh"` 方式加载，避免同一段校验/权限逻辑在多个
### 脚本里各写一份而逐渐漂移。
### 注意：cmd/install_init 在"安装前准备"阶段执行，此时包文件尚未解压，
###       因此它不能依赖本文件，必须保持自包含。

# ---------------------------------------------------------------------------
# 运行用户
# ---------------------------------------------------------------------------
# config/privilege 声明 run-as=package，fnOS 会注入专用应用用户到 TRIM_USERNAME。
# 兜底按 manifest.appname（moviepilot）推断。
mp_app_user() {
    if [ -n "${TRIM_USERNAME:-}" ] && id -u "${TRIM_USERNAME}" >/dev/null 2>&1; then
        echo "${TRIM_USERNAME}"
        return 0
    fi
    if id -u moviepilot >/dev/null 2>&1; then
        echo "moviepilot"
        return 0
    fi
    return 1
}

# ---------------------------------------------------------------------------
# 权限收紧
# ---------------------------------------------------------------------------
# app.env 内含明文超级管理员密码与 API_TOKEN，绝不能全局可读；官方开发文档也
# 明确要求"不通过放宽权限解决路径或端口配置错误"。
# 但后端以专用用户运行、必须能写 app.env 与数据库，所以只在能确定运行用户时
# 收紧；无法确定时退回宽松权限并告警——宁可放宽，也不能让后端写不进去。
#   $1 配置目录   $2 app.env   $3 是否递归（1=递归 chown，用于安装/升级后修正
#   安装阶段由 root 创建的数据库文件属主）
mp_secure_config() {
    local dir="$1" env_file="$2" recursive="${3:-0}"
    local user
    if user="$(mp_app_user)"; then
        if [ "${recursive}" = "1" ]; then
            chown -R "${user}:${user}" "${dir}" 2>/dev/null || true
        else
            chown "${user}:${user}" "${dir}" 2>/dev/null || true
            if [ -f "${env_file}" ]; then
                chown "${user}:${user}" "${env_file}" 2>/dev/null || true
            fi
        fi
        chmod 700 "${dir}" 2>/dev/null || true
        if [ -f "${env_file}" ]; then
            chmod 600 "${env_file}" 2>/dev/null || true
        fi
        return 0
    fi
    echo "[warn] 无法确定应用运行用户，退回宽松权限（config 777 / app.env 666）"
    chmod 777 "${dir}" 2>/dev/null || true
    if [ -f "${env_file}" ]; then
        chmod 666 "${env_file}" 2>/dev/null || true
    fi
    return 0
}

# 让 Python 运行时对应用运行用户可写。MoviePilot 会在运行时用 pip 向自身解释器
# 环境安装插件依赖（app/adapters/external/market.py 用 sys.executable 对应的 pip），
# 运行时目录若为 root 独占，插件安装会因 PermissionError 失败。官方 Dockerfile
# 也把 venv 整个 chmod 777，是同一个原因。
#
# 只精确处理 pip 真正会写入的两处，不做整树 chown -R：
#   * lib/pythonX.Y/site-packages —— 装包落点
#   * bin                        —— pip 生成 console script 的落点
# 自带运行时整树约 300 MB、数万个文件，全树 chown 会让安装/升级明显变慢，而
# lib/*.so、include/、share/ 这些地方 pip 根本不会碰。
mp_secure_venv() {
    local rt="$1" user
    if [ -z "${rt}" ] || [ ! -d "${rt}" ]; then
        return 0
    fi
    if user="$(mp_app_user)"; then
        for sp in "${rt}"/lib/python3.*/site-packages; do
            [ -d "${sp}" ] && chown -R "${user}:${user}" "${sp}" 2>/dev/null || true
        done
        if [ -d "${rt}/Lib/site-packages" ]; then          # 兼容 Windows 布局（理论分支）
            chown -R "${user}:${user}" "${rt}/Lib/site-packages" 2>/dev/null || true
        fi
        [ -d "${rt}/bin" ] && chown -R "${user}:${user}" "${rt}/bin" 2>/dev/null || true
    fi
    return 0
}

# ---------------------------------------------------------------------------
# 超级管理员密码校验（与 MoviePilot 官方 _validate_superuser_password 一致）
# ---------------------------------------------------------------------------
# 通过返回 0；失败时把原因写入全局 PW_ERR 并返回 1。
# 字符集限制的原因：密码会写入 app.env，被 dotenv 解析、被 sed 重写；含
# & | # 引号 空格 等字符时会被截断或转义，表现为"设置成功却无法登录"。
PW_ERR=""
mp_validate_password() {
    local pw="$1"
    PW_ERR=""
    local len=${#pw}
    if [ "${len}" -lt 6 ] || [ "${len}" -gt 50 ]; then
        PW_ERR="超级管理员密码长度需在 6-50 位之间"
        return 1
    fi
    local has_letter=0 has_digit=0 has_special=0
    if printf '%s' "$pw" | grep -qE '[A-Za-z]'; then has_letter=1; fi
    if printf '%s' "$pw" | grep -qE '[0-9]'; then has_digit=1; fi
    if printf '%s' "$pw" | grep -qE '[^A-Za-z0-9]'; then has_special=1; fi
    if [ $((has_letter + has_digit + has_special)) -lt 2 ]; then
        PW_ERR="超级管理员密码需至少包含字母、数字、特殊字符中的两类（如：MoviePilot@2026）"
        return 1
    fi
    if printf '%s' "$pw" | grep -qE '[^A-Za-z0-9!@%^*_.:+?-]'; then
        PW_ERR="密码包含不允许的字符（仅限字母、数字及 !@%^*_.:+?）"
        return 1
    fi
    return 0
}

# ---------------------------------------------------------------------------
# Python 运行时解析
# ---------------------------------------------------------------------------
# 三种运行时，按优先级：
#   1. ${TRIM_APPDEST}/python —— 构建时打包的自带 CPython 3.14 + 全部依赖。
#      MoviePilot V3 要求 requires-python >=3.14，而 fnOS 只有 python312，
#      所以正常安装都走这个。它是可重定位的普通解释器目录（不是 venv，没有
#      pyvenv.cfg），bin/python、bin/python3、lib/python3.14/site-packages
#      一应俱全（bin/python 与 bin/python3 都是指向 bin/python3.14 的符号链接），
#      所以"解释器根目录"和原来的"venv 目录"用法完全一致。
#   2. ${TRIM_APPDEST}/venv —— 历史版本打包的 venv（升级场景兼容）。
#   3. ${TRIM_PKGVAR}/venv —— 安装时用 fnOS python312 在线创建的 venv（兜底）。
# 注意：pip 一律用 "${python}" -m pip 调用，不要直接执行 ${VENV_DIR}/bin/pip。
#       python-build-standalone 的 bin/pip 用的是 `exec "$(dirname -- "$(realpath
#       -- "$0")")/python3.14"` 这种相对路径 trampoline（并非构建机绝对路径
#       shebang，是可重定位的），但它依赖系统存在 realpath；用 -m pip 可以少
#       依赖一个外部命令，且对 venv 与自带运行时两种布局都成立。
MP_FNOS_PYTHON="/var/apps/python312/target/bin/python3"

mp_runtime_dir() {
    if [ -x "${TRIM_APPDEST:-}/python/bin/python3" ]; then
        echo "${TRIM_APPDEST}/python"
        return 0
    fi
    if [ -x "${TRIM_APPDEST:-}/venv/bin/python" ]; then
        echo "${TRIM_APPDEST}/venv"
        return 0
    fi
    if [ -x "${TRIM_PKGVAR:-}/venv/bin/python" ]; then
        echo "${TRIM_PKGVAR}/venv"
        return 0
    fi
    return 1
}

# 应用主解释器（跑 supervisor 与后端）。找不到返回 1。
# 优先 bin/python3、退回 bin/python：自带运行时两者都有，venv 里通常只有 python3，
# 历史打包的 venv 则可能只有 python。两种布局都覆盖。
mp_app_python() {
    local rt
    if rt="$(mp_runtime_dir)"; then
        if [ -x "${rt}/bin/python3" ]; then
            echo "${rt}/bin/python3"
        else
            echo "${rt}/bin/python"
        fi
        return 0
    fi
    return 1
}

# 通用解释器：优先应用运行时，其次 fnOS python312，最后 PATH 里的 python3。
# 用于跑只依赖标准库的脚本（如 gateway-proxy.py），保证运行时缺失时仍能起代理。
mp_any_python() {
    local rt
    if rt="$(mp_runtime_dir)"; then
        echo "${rt}/bin/python3"
        return 0
    fi
    if [ -x "${MP_FNOS_PYTHON}" ]; then
        echo "${MP_FNOS_PYTHON}"
        return 0
    fi
    command -v python3 2>/dev/null
}
