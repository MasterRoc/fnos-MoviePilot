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

# 让 venv 对应用运行用户可写。MoviePilot 会在运行时用 pip 向自身解释器环境安装
# 插件依赖（app/adapters/external/market.py 用 sys.executable 对应的 pip），
# venv 若为 root 独占，插件安装会因 PermissionError 失败。
mp_secure_venv() {
    local venv_dir="$1" user
    if [ -z "${venv_dir}" ] || [ ! -d "${venv_dir}" ]; then
        return 0
    fi
    if user="$(mp_app_user)"; then
        chown -R "${user}:${user}" "${venv_dir}" 2>/dev/null || true
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
