# MoviePilot 飞牛 fnOS 原生应用

将 [MoviePilot](https://github.com/jxxghp/MoviePilot)（NAS 媒体库自动化管理工具）开发为 **fnOS 原生应用（Native 应用）**，非 Docker 版。

## 特性

- **原生运行**：不依赖 Docker，直接在 fnOS 上以 Python + Node.js 运行
- **统一网关接入**：通过 fnOS 统一网关 + 反向代理嵌入桌面 iframe，自动校验 NAS 登录态
- **SQLite 数据库**：默认使用 SQLite，无需额外安装 PostgreSQL 等中间件
- **前后端分离**：Python FastAPI 后端 + Vue3 前端（官方预编译产物）
- **生命周期管理**：安装向导、启停控制、升级数据备份/恢复、卸载数据保留/删除

## 技术栈与架构

| 组件 | 说明 |
|------|------|
| 后端 | MoviePilot V3（FastAPI + Python 3.12），监听 `127.0.0.1:3001` |
| 前端 | MoviePilot-Frontend（Vue3 预编译 `dist.zip`），Express 静态服务监听 `127.0.0.1:3000`，并代理 `/api`、`/cookiecloud` 到后端 |
| 网关代理 | `app/bin/gateway-proxy.py`，Unix Socket → 前端端口，完成前缀剥离、JS polyfill、WebSocket 隧道 |
| 运行时 | fnOS `python312` + `nodejs_v24`（`manifest` 声明依赖） |
| 数据库 | SQLite（默认），配置存于 `TRIM_PKGVAR/config` |

```
用户浏览器 (fnOS 桌面 iframe)
    │  /app/moviepilot/...
    ▼
fnOS 统一网关（校验登录态）
    ▼  转发到 moviepilot.sock
gateway-proxy.py  (app/bin/gateway-proxy.py)
    ▼  剥离前缀 + JS polyfill + WS 隧道
Node 前端  frontend-server.js  (127.0.0.1:3000)
    │  静态资源 + SPA 回退
    └──  /api, /cookiecloud 反向代理
        ▼
Python 后端  app/main.py  (127.0.0.1:3001)
```

## 目录结构

```
├── app/
│   ├── bin/
│   │   ├── gateway-proxy.py     # 网关反向代理（核心）
│   │   └── frontend-server.js   # Node 前端服务 + API 代理
│   ├── mp/                      # MoviePilot V3 源码（build 时下载）
│   ├── frontend/                # 前端 dist（build 时下载）
│   └── ui/
│       ├── config               # 桌面入口（统一网关）
│       └── images/              # 入口图标
├── cmd/                         # 生命周期脚本
│   ├── main                     # start / stop / status
│   ├── install_init/callback
│   ├── config_init/callback
│   ├── upgrade_init/callback
│   └── uninstall_init/callback
├── config/
│   ├── privilege                # run-as=package
│   └── resource                 # data-share
├── wizard/                      # 安装/配置/卸载向导
├── manifest
├── build.py                    # 跨平台构建脚本（推荐）
├── build.ps1 / build.sh        # 构建脚本（备选）
├── ICON.PNG / ICON_256.PNG
└── README.md
```

## 构建

**推荐使用跨平台 `build.py`**（Windows / Linux / macOS 通用）：

```bash
# 下载源码+前端并打包
python build.py

# 常用参数
python build.py --force        # 强制重新下载外部资源
python build.py --clean        # 构建前清理 .local-build
python build.py --skip-mp      # 跳过下载后端源码
python build.py --skip-fe      # 跳过下载前端
python build.py --with-venv    # 把 Python 依赖 venv 一起打进包（安装时完全不联网，仅 Linux/macOS）
```

备选脚本：

```bash
# Windows
./build.ps1                # 默认 amd64
./build.ps1 -Arch arm64

# Linux / macOS
./build.sh                 # 默认 amd64
ARCH=arm64 ./build.sh
```

构建脚本会自动：
1. 从 GitHub 下载 MoviePilot V3 源码到 `.local-build/mp`（打包内置）
2. 从 GitHub Releases 下载前端 `dist.zip` 到 `.local-build/frontend`（打包内置）
3. （可选 `--with-venv`）构建 Python 依赖 venv 到 `.local-build/venv`（打包内置）
4. 在 `.local-build/pkg/` 组装干净的应用目录树（仓库源码 + 构建产物），下载 fnpack 到 `.local-build/tools` 并打包生成 `moviepilot-<version>.fpk`

**所有下载/解压/构建产物统一收敛到 `.local-build/`（已 gitignore，不入库）**，项目根目录不残留任何构建产物。

> 下载策略：**先直连 GitHub，直连不通再自动切换到 `gh-proxy.com` / `ghfast.top` 加速代理**，避免 GitHub 被限时卡死。

### GitHub Actions 自动构建（分架构 + 内嵌 venv）

仓库已内置 `.github/workflows/build-and-release.yml`，可在 CI 上自动分架构打包：

- **触发方式**：
  - 手动触发：Actions 页点 `workflow_dispatch`
  - 打标签发布：`git tag v1.1.3104 && git push origin v1.1.3104`（自动生成 Release 并附带 changelog）
- **分架构矩阵**：`amd64`（ubuntu-latest）、`arm64`（ubuntu-24.04-arm）
- **内嵌 venv**：每个架构的 runner 上执行 `python build.py --with-venv`，把该架构的 Python 依赖 venv 打进包，安装时**完全无需联网**
- **产物命名**：`moviepilot-<version>-<arch>.fpk`（如 `moviepilot-1.1.3104-amd64.fpk`）

> 说明：打包前会在 `.local-build/pkg/` 组装干净的应用目录树（只含该进包的内容），再调用 fnpack 打包，因此包内不会混入任何构建缓存/临时文件。

## 安装

在 fnOS 应用中心「手动安装」上传 `.fpk`，或使用 `appcenter-cli`：

```bash
appcenter-cli install-fpk moviepilot-<version>-amd64.fpk
```

安装向导会收集：
- 超级管理员用户名 / 密码
- 后端端口（默认 3001）、前端端口（默认 3000）

安装时会自动：
1. 准备 Python 环境（优先使用 `--with-venv` 构建时打包的 venv）
2. 初始化 SQLite 数据库并创建超级管理员

> - **构建时用了 `--with-venv`**：安装时完全不联网，开箱即用。
> - **未用 `--with-venv`**（如 Windows 构建）：安装时自动 `pip install` 依赖（走国内加速源），需 NAS 联网，首次安装可能耗时数分钟。

## 使用

安装完成后，在 fnOS 桌面点击 MoviePilot 图标即可打开 Web 界面（嵌入 iframe）。数据存储于：

| 用途 | 位置 |
|------|------|
| 配置 / 数据库 | `TRIM_PKGVAR/config`（持久） |
| 下载目录 | `TRIM_DATA_SHARE_PATHS` 共享目录 |
| 媒体库目录 | 用户在应用设置中授权 |

可在应用设置中修改端口、重置超级管理员密码。

## 已知假设与限制

- 本包为**原生 Native 应用**。后端 Python 依赖（较多，含 langchain、Rust 扩展等）可通过 `--with-venv` 构建时打入包（安装时不联网）；否则在安装时在线 pip 安装
- `--with-venv` 需要在 **Linux/macOS** 构建机上执行（Windows 无法交叉编译 Linux venv）；且构建机架构需与目标 NAS 架构一致（amd64/arm64 分开构建）
- 默认使用 **SQLite**；如需 PostgreSQL，可在 `app.env` 中设置 `DB_TYPE=postgresql` 并配置连接（需 fnOS 安装 PostgreSQL）
- 后端源码、前端产物、可选打包的 venv、fnpack 工具、下载缓存等**所有构建产物全部收敛在 `.local-build/`，不纳入 git**，由构建脚本生成；拉取仓库后需先执行构建脚本，项目根目录不残留任何构建产物
- 首个登录使用安装向导设置的管理员账号

## 免责声明

MoviePilot 遵循 GPL-3.0 许可证，仅限学习交流。本应用包仅为第三方封装，请自行评估合规与使用风险。
