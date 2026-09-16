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
| 后端 | MoviePilot V3（FastAPI + Python 3.14），监听 `127.0.0.1:3002` |
| 前端 | MoviePilot-Frontend（Vue3 预编译 `dist.zip`），Express 静态服务监听 `127.0.0.1:3005`，并代理 `/api`、`/cookiecloud` 到后端 |
| 网关代理 | `app/bin/gateway-proxy.py`，Unix Socket → 前端端口，完成前缀剥离、JS polyfill、WebSocket 隧道 |
| Python 运行时 | **自带 CPython 3.14.7**（python-build-standalone，随包分发，安装时不联网）；fnOS 应用中心的 `python312` 仅作在线兜底 |
| Node 运行时 | fnOS `nodejs_v24`（`manifest` 的 `install_dep_apps` 声明依赖） |
| 数据库 | SQLite（默认），配置存于 `TRIM_PKGVAR/config` |

```
用户浏览器 (fnOS 桌面 iframe)
    │  /app/moviepilot/...
    ▼
fnOS 统一网关（校验登录态）
    ▼  转发到 moviepilot.sock
gateway-proxy.py  (app/bin/gateway-proxy.py)
    ▼  剥离前缀 + JS polyfill + WS 隧道
Node 前端  frontend-server.js  (127.0.0.1:3005)
    │  静态资源 + SPA 回退
    └──  /api, /cookiecloud 反向代理
        ▼
Python 后端  app/main.py  (127.0.0.1:3002)
```

### 为什么要自带 Python 3.14

MoviePilot V3 的 `pyproject.toml` 声明 `requires-python >= 3.14`，而 fnOS 应用中心提供的运行时是 `python312` —— 版本不够。官方 Docker 镜像的处理方式同样是自带解释器（`/opt/python`）。本应用沿用同一思路：

- 构建时下载 [python-build-standalone](https://github.com/astral-sh/python-build-standalone) 的 `cpython-3.14.7`（`install_only_stripped`，arm64 约 29 MB / amd64 约 34 MB）
- 用 `uv` 按 MoviePilot 的 `uv.lock` 把依赖直接装进该解释器的 `site-packages`
- 整个 `app/python/` 随包分发，**安装时零联网、零编译**

不采用 venv 的原因：venv 的 `bin/python` 是指向构建机绝对路径的符号链接，`pyvenv.cfg` 里也写着构建机路径，打进包搬到 NAS 必然失效。python-build-standalone 的发行版是可重定位的（`sys.prefix` 由二进制位置推导），整个目录搬到哪儿都能跑。

## 目录结构

```
├── app/
│   ├── bin/
│   │   ├── gateway-proxy.py     # 网关反向代理（核心）
│   │   ├── supervisor.py        # 唯一主进程，托管后端/前端/代理
│   │   └── frontend-server.js   # Node 前端服务 + API 代理
│   ├── mp/                      # MoviePilot V3 源码（build 时下载）
│   │   └── requirements.lock.txt# 由 uv.lock 导出的锁定依赖清单（在线兜底时用）
│   ├── frontend/                # 前端 dist（build 时下载）
│   ├── python/                  # 自带 CPython 3.14 + 全部依赖（--with-runtime 时内置）
│   └── ui/
│       ├── config               # 桌面入口（统一网关）
│       └── images/              # 入口图标
├── cmd/                         # 生命周期脚本
│   ├── lib.sh                   # 公共函数（运行用户/权限收敛/密码校验/运行时解析）
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
python build.py --force             # 强制重新下载外部资源
python build.py --clean             # 构建前清理 .local-build
python build.py --skip-mp           # 跳过下载后端源码
python build.py --skip-fe           # 跳过下载前端
python build.py --arch arm64        # 显式声明目标架构（裁剪 sites 原生变体）
python build.py --with-runtime      # 自带 Python 3.14 运行时 + 全部依赖（安装时完全不联网，仅 Linux/macOS）
python build.py --with-runtime --allow-build
                                    # 同上，但允许从源码构建依赖（默认只用预编译 wheel）
```

> `--with-venv` 仍可用，是 `--with-runtime` 的兼容别名（早期版本打包的是 venv，现已改为自带解释器）。

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
3. 同步 MoviePilot-Resources 资源包到 `mp/app/helper`（V3 必需，缺失会报 `No module named 'app.helper.sites'`）
4. （可选 `--with-runtime`）下载 CPython 3.14.7 到 `.local-build/python`，用 `uv` 按 `uv.lock` 装依赖，并做三道自检（依赖自检 / wheel glibc 审计 / 关键路径断言）
5. 在 `.local-build/pkg/` 组装干净的应用目录树（仓库源码 + 构建产物），下载 fnpack 到 `.local-build/tools` 并打包生成 `moviepilot-<version>.fpk`

**所有下载/解压/构建产物统一收敛到 `.local-build/`（已 gitignore，不入库）**，项目根目录不残留任何构建产物。

> 下载策略：**先直连 GitHub，直连不通再自动切换到 `gh-proxy.com` / `ghfast.top` 加速代理**，避免 GitHub 被限时卡死。

### GitHub Actions 自动构建（分架构 + 自带 Python 运行时）

仓库已内置 `.github/workflows/build-and-release.yml`，可在 CI 上自动分架构打包：

- **触发方式**：
  - 手动触发：Actions 页点 `workflow_dispatch`
  - 打标签发布：`git tag v1.1.3104 && git push origin v1.1.3104`（自动生成 Release 并附带 changelog）
- **分架构矩阵**：`amd64`（ubuntu-latest）、`arm64`（ubuntu-24.04-arm）
- **自带运行时**：每个架构的 runner 上执行 `python build.py --with-runtime --arch <arch>`，把该架构的 CPython 3.14 与依赖打进包，安装时**完全无需联网**
- **产物校验**：打包后会嵌套解开 `app.tgz`，断言 `app/python/bin/python3`、`lib/python3.14/site-packages` 存在，并抽查 `fastapi/uvicorn/sqlalchemy/pydantic_core/orjson` —— 防止再次出现「CI 全绿但包里没有依赖」的静默失败
- **产物命名**：`moviepilot-<version>-<arch>.fpk`（如 `moviepilot-1.1.3104-amd64.fpk`）

> 说明：打包前会在 `.local-build/pkg/` 组装干净的应用目录树（只含该进包的内容），再调用 fnpack 打包，因此包内不会混入任何构建缓存/临时文件。

## 安装

在 fnOS 应用中心「手动安装」上传 `.fpk`，或使用 `appcenter-cli`：

```bash
appcenter-cli install-fpk moviepilot-<version>-amd64.fpk
```

安装向导会收集：
- 超级管理员用户名 / 密码
- 后端端口（默认 3002）、前端端口（默认 3005）

安装时会自动：
1. 准备 Python 环境（优先使用 `--with-runtime` 构建时打包的自带 CPython 3.14 + 全部依赖）
2. 初始化 SQLite 数据库并创建超级管理员

> - **构建时用了 `--with-runtime`**（CI 产物即如此）：安装时完全不联网，开箱即用。安装脚本会先自检解释器与核心依赖，不自检通过就明确失败，不会留下「安装成功但起不来」的应用。
> - **未用 `--with-runtime`**（如 Windows 构建）：安装时用 fnOS `python312` 建 venv 并 `pip install` 依赖（走国内加速源），需 NAS 联网，首次安装可能耗时数分钟。注意此路径下依赖需满足 `requires-python >= 3.14`，实际很可能装不上，仅作降级兜底。

## 使用

安装完成后，在 fnOS 桌面点击 MoviePilot 图标即可打开 Web 界面（嵌入 iframe）。数据存储于：

| 用途 | 位置 |
|------|------|
| 配置 / 数据库 | `TRIM_PKGVAR/config`（持久） |
| 下载目录 | `TRIM_DATA_SHARE_PATHS` 共享目录 |
| 媒体库目录 | 用户在应用设置中授权 |

可在应用设置中修改端口、重置超级管理员密码。

## 已知假设与限制

- 本包为**原生 Native 应用**。MoviePilot V3 要求 `requires-python >= 3.14`，因此正常产物通过 `--with-runtime` 自带 CPython 3.14 与全部依赖（含 langchain、Rust 扩展等），安装时不联网；未打包运行时的产物只能退回 fnOS `python312` 在线安装依赖
- `--with-runtime` 需要在 **Linux/macOS** 构建机上执行（Windows 无法交叉准备 Linux 运行时）；且构建机架构需与目标 NAS 架构一致（amd64/arm64 分开构建，CI 用对应的原生 runner）
- 依赖默认**只用预编译 wheel**（`--no-build`），并审计 wheel 的 manylinux 基线不高于 glibc 2.36（fnOS / Debian 12 的水平）。若某个包只有更老的基线之外的 wheel，构建会失败而不是打出跑不起来的包
- 产物体积较大（自带解释器 + 全部依赖），预期在数百 MB 量级；这是「安装时零联网」的代价
- MoviePilot 的插件依赖是在运行时用自身解释器的 pip 安装到 `app/python/` 的 `site-packages`。**升级会整包替换应用目录，插件依赖需要重新安装**
- 默认使用 **SQLite**；如需 PostgreSQL，可在 `app.env` 中设置 `DB_TYPE=postgresql` 并配置连接（需 fnOS 安装 PostgreSQL）
- 后端源码、前端产物、自带 Python 运行时、fnpack 工具、下载缓存等**所有构建产物全部收敛在 `.local-build/`，不纳入 git**，由构建脚本生成；拉取仓库后需先执行构建脚本，项目根目录不残留任何构建产物
- 首个登录使用安装向导设置的管理员账号

## 免责声明

MoviePilot 遵循 GPL-3.0 许可证，仅限学习交流。本应用包仅为第三方封装，请自行评估合规与使用风险。
