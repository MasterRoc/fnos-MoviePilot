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
# app.env 默认值补齐
# ---------------------------------------------------------------------------
# 新增功能对应的环境变量，对"已安装过旧版本"的应用不会自动出现（app.env 只在
# 安装时生成一次）。升级脚本用它把缺失的键补成默认值，已有值一律不动 ——
# 用户手动改过的配置必须保留。
#   $1 配置文件   $2 键   $3 默认值
mp_env_ensure() {
    local env_file="$1" key="$2" value="$3"
    [ -n "${env_file}" ] || return 0
    [ -f "${env_file}" ] || return 0
    if grep -q "^${key}=" "${env_file}" 2>/dev/null; then
        return 0
    fi
    echo "${key}=${value}" >> "${env_file}"
    return 0
}

# ---------------------------------------------------------------------------
# app.env 键值 upsert（存在则替换，不存在则追加）
# ---------------------------------------------------------------------------
# 与 mp_env_ensure 的分工：mp_env_ensure 只补缺失键，用于"新增功能默认值"；
# mp_env_upsert 会覆盖已有值，用于"向导里显式填写的配置"（端口、管理员账号等）。
#
# 为什么不用 sed：待写入的值可能含 / & | * . 等字符 —— 密钥是 base64（含 + / =），
# 密码/用户名也可能带特殊符号。sed 无论选哪个分隔符都要额外转义，漏一个就会写出
# 坏配置或截断值。这里改为 awk 重写、值经环境变量传入，完全不经过 sed/正则解析。
#
# 为什么用 `cat > 文件` 而不是 mv：保留原 inode 与权限位。app.env 已被收敛为
# 600/应用用户，直接 mv 覆盖会带回默认 umask 权限。
#   $1 配置文件   $2 键   $3 值
mp_env_upsert() {
    local env_file="$1" key="$2" value="$3"
    [ -n "${env_file}" ] || return 0
    [ -n "${key}" ] || return 0
    [ -f "${env_file}" ] || return 0
    if grep -q "^${key}=" "${env_file}" 2>/dev/null; then
        MP_UPSERT_KEY="${key}" MP_UPSERT_VALUE="${value}" \
            awk 'BEGIN { k = ENVIRON["MP_UPSERT_KEY"]; v = ENVIRON["MP_UPSERT_VALUE"] }
                 index($0, k "=") == 1 { print k "=" v; next }
                 { print }' "${env_file}" > "${env_file}.tmp" 2>/dev/null || {
            rm -f "${env_file}.tmp"
            return 0
        }
        cat "${env_file}.tmp" > "${env_file}" 2>/dev/null || true
        rm -f "${env_file}.tmp"
    else
        printf '%s=%s\n' "${key}" "${value}" >> "${env_file}" 2>/dev/null || true
    fi
    return 0
}

# ---------------------------------------------------------------------------
# 密钥补齐（生成一次后永久稳定）
# ---------------------------------------------------------------------------
# 为什么必须显式补：
#   MoviePilot 的 SECRET_KEY / RESOURCE_SECRET_KEY 默认值都是
#   `secrets.token_urlsafe(32)` —— **每次进程启动重新随机生成**。官方 Docker
#   把它写进挂在 /config 卷上的 app.env 持久化，所以不受影响；而本项目的
#   app.env 由 install_callback 的 heredoc 一次性生成，模板里没有这两个键，
#   于是每次重启密钥都在变。造成两个可见故障：
#     1) RESOURCE_SECRET_KEY 变化 -> 解不开 Fernet 加密的站点索引
#        user.sites.v3.bin -> get_authsites() 返回空 -> “站点认证”页面
#        选站点时显示 "No data available"（文件在位、ABI 也对，就是解不开）；
#     2) SECRET_KEY 变化 -> 已签发的登录令牌全部作废 -> 每次重启都要重新登录。
# 上游 tests/test_security_utils.py 明确要求 RESOURCE_SECRET_KEY 变化后旧签名
# 必须作废，所以这里**只在缺失时写入一次**，绝不在每次启动时覆盖。
#
# 密钥格式：Fernet 要求 32 字节 urlsafe-base64。`head -c 32 /dev/urandom | base64`
# 得到 44 字符（含结尾 '='），解码后 32 字节，符合要求。不能用 64 位的 hex
# （解码后 32 字节但非 base64 字母表，Fernet 会拒绝）。
#   $1 配置文件   $2 键名
mp_env_ensure_secret() {
    local env_file="$1" key="$2"
    [ -n "${env_file}" ] || return 0
    [ -n "${key}" ] || return 0
    [ -f "${env_file}" ] || return 0
    if grep -q "^${key}=" "${env_file}" 2>/dev/null; then
        return 0
    fi
    local secret
    secret="$(head -c 32 /dev/urandom | base64 | tr -d '\n')"
    [ -n "${secret}" ] || return 0
    echo "${key}=${secret}" >> "${env_file}"
    return 0
}

# ---------------------------------------------------------------------------
# app.env 引导（全新安装 / 覆盖安装共用）
# ---------------------------------------------------------------------------
# 为什么这里必须区分"文件是否已存在"：
#   app.env 不只是部署参数，它同时是 MoviePilot 自己的配置落点（后端通过 dotenv
#   的 set_key 把用户设置写回这里：媒体服务器、目录、站点、通知渠道、分类策略…），
#   并承载两个密钥。无条件重建它 = 清空密钥 + 清空用户配置，实测后果：
#     - RESOURCE_SECRET_KEY 变化 -> Fernet 站点索引 user.sites.v3.bin 解不开
#       （站点页/站点认证无数据），且只认资源 Cookie 的 EventSource 接口
#       （system/message、system/logging）持续 401；
#     - SECRET_KEY 变化 -> 已签发登录令牌全部作废，前端收到 401 直接登出，
#       表现为"能登录但各页面列表为空"。
#   fnOS 的"重新安装"同样会走 install_callback，所以覆盖安装路径绝不能重建。
# 语义：
#   * 文件不存在（全新安装） -> 写整份模板，并生成两个密钥
#   * 文件已存在（覆盖安装） -> 只补齐缺失键；只有"向导里显式填写"的值才覆盖
# 向导显式值经环境变量读取（wizard_port / wizard_nginx_port / wizard_superuser /
# wizard_superuser_password / wizard_api_token），与 install_init 使用的键一致。
# stdout 输出 "fresh" / "kept"，供调用方记录日志。
#   $1 app.env 路径   $2 CONFIG_DIR
mp_env_bootstrap() {
    local env_file="$1" config_dir="$2"
    [ -n "${env_file}" ] || return 1

    local api_token port nginx_port
    api_token="${wizard_api_token:-}"
    if [ -z "${api_token}" ] || [ ${#api_token} -lt 16 ]; then
        api_token="$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n' | cut -c1-32)"
    fi
    port="${wizard_port:-3002}"
    nginx_port="${wizard_nginx_port:-3005}"

    if [ -f "${env_file}" ]; then
        # 覆盖安装：保留既有配置与密钥，只补缺失项
        mp_env_ensure_secret "${env_file}" "SECRET_KEY"
        mp_env_ensure_secret "${env_file}" "RESOURCE_SECRET_KEY"
        mp_env_ensure "${env_file}" "CONFIG_DIR" "${config_dir}"
        mp_env_ensure "${env_file}" "HOST" "127.0.0.1"
        mp_env_ensure "${env_file}" "PORT" "${port}"
        mp_env_ensure "${env_file}" "NGINX_PORT" "${nginx_port}"
        mp_env_ensure "${env_file}" "DB_TYPE" "sqlite"
        mp_env_ensure "${env_file}" "API_TOKEN" "${api_token}"
        mp_env_ensure "${env_file}" "GITHUB_PROXY" "https://gh-proxy.com/"
        mp_env_ensure "${env_file}" "MP_AUTO_UPDATE" "1"
        mp_env_ensure "${env_file}" "MP_UPDATE_CHANNEL" "release"
        mp_env_ensure "${env_file}" "MP_UPDATE_INTERVAL" "21600"
        mp_env_ensure "${env_file}" "MP_UPDATE_DEPS" "1"
        # 只有向导里真的填了才覆盖：留空表示"沿用原有配置"，
        # 早期实现会把留空的管理员密码直接写成空值（等于清掉配置）。
        if [ -n "${wizard_port:-}" ]; then
            mp_env_upsert "${env_file}" "PORT" "${wizard_port}"
        fi
        if [ -n "${wizard_nginx_port:-}" ]; then
            mp_env_upsert "${env_file}" "NGINX_PORT" "${wizard_nginx_port}"
        fi
        if [ -n "${wizard_superuser:-}" ]; then
            mp_env_upsert "${env_file}" "SUPERUSER" "${wizard_superuser}"
        fi
        if [ -n "${wizard_superuser_password:-}" ]; then
            mp_env_upsert "${env_file}" "SUPERUSER_PASSWORD" "${wizard_superuser_password}"
        fi
        if [ -n "${wizard_api_token:-}" ] && [ ${#wizard_api_token} -ge 16 ]; then
            mp_env_upsert "${env_file}" "API_TOKEN" "${wizard_api_token}"
        fi
        echo "kept"
        return 0
    fi

    local secret resource_secret
    secret="$(head -c 32 /dev/urandom | base64 | tr -d '\n')"
    resource_secret="$(head -c 32 /dev/urandom | base64 | tr -d '\n')"
    cat > "${env_file}" <<EOF
# MoviePilot fnOS 环境配置
CONFIG_DIR=${config_dir}
HOST=127.0.0.1
PORT=${port}
NGINX_PORT=${nginx_port}
DB_TYPE=sqlite
API_TOKEN=${api_token}
# 安全密钥：必须持久化，随机默认值会导致站点资源解密失败与登录态失效
SECRET_KEY=${secret}
RESOURCE_SECRET_KEY=${resource_secret}
SUPERUSER=${wizard_superuser:-admin}
SUPERUSER_PASSWORD=${wizard_superuser_password:-}
# GitHub 加速代理：后端启动时下载/更新资源包（sites.so 等）走国内镜像，避免直连超时
GITHUB_PROXY=https://gh-proxy.com/
# 自动更新：应用每次启动（重启）前检查上游 Release 并就地升级，无需重新构建安装包
MP_AUTO_UPDATE=1
# 更新通道：release=仅正式版 / prerelease=含测试版 / off=关闭
MP_UPDATE_CHANNEL=release
# 检查间隔（秒）：避免每次重启都去查一次 GitHub
MP_UPDATE_INTERVAL=21600
# 是否顺带同步 Python 依赖：新版本引入新依赖时必须开启
MP_UPDATE_DEPS=1
EOF
    echo "fresh"
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
# manifest 已不再声明 python312（本应用自带 CPython 3.14），这里只为"未打包运行时
# 的降级包"留一条在线兜底路径。正常安装用不到。
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
