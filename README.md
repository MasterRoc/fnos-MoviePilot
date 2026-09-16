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
| Python 运行时 | **自带 CPython 3.14.7**（python-build-standalone，随包分发，安装时不联网）。`manifest` 已不再声明 fnOS 的 `python312` —— 版本不够且用不上，无需安装 |
| Node 运行时 | fnOS `nodejs_v24`（`manifest` 的 `install_dep_apps` 声明依赖，前端静态服务需要） |
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
│   │   ├── mp_updater.py        # 上游自更新器（重启即升级，纯标准库）
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
│   ├── main                     # start / stop / status / update / update-check / rollback
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
├── tools/
│   └── updater_smoke.py        # 自更新器离线冒烟测试（沙箱，Windows 亦可跑）
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
python build.py --with-runtime --no-build
                                    # 严格模式：禁止源码构建，只用预编译 wheel
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
3. 同步 MoviePilot-Resources 资源包到后端源码的站点资源目录（V3 必需；目录名随上游重构变过，`app/helper` → `app/application/site`，构建时按源码结构自动定位，缺失会报 `No module named 'app.application.site.sites'`）
4. （可选 `--with-runtime`）下载 CPython 3.14.7 到 `.local-build/python`，用 `uv` 按 `uv.lock` 装依赖，并做三道自检（依赖自检 / wheel glibc 审计 / 关键路径断言）
5. 在 `.local-build/pkg/` 组装干净的应用目录树（仓库源码 + 构建产物），下载 fnpack 到 `.local-build/tools` 并打包生成 `moviepilot-<version>.fpk`

**所有下载/解压/构建产物统一收敛到 `.local-build/`（已 gitignore，不入库）**，项目根目录不残留任何构建产物。

> 下载策略：**先直连 GitHub，直连不通再自动切换到 `gh-proxy.com` / `ghfast.top` 加速代理**，避免 GitHub 被限时卡死。

### GitHub Actions 自动构建（分架构 + 自带 Python 运行时）

仓库已内置 `.github/workflows/build-and-release.yml`，可在 CI 上自动分架构打包：

- **触发方式**：
  - 手动触发：Actions 页点 `workflow_dispatch`（只构建，不发 Release）
  - 打标签发布：`git tag v1.1.3104 && git push origin v1.1.3104`（自动生成 Release 并附带 changelog）
- 推分支**不会**触发这个 workflow —— 单架构十几分钟、产物 250 MB，每次提交都跑不划算。
  推 `master` 或提 PR 时只跑 `lint.yml`（Python/Shell/Node 语法 + manifest 版本一致性，秒级返回）。

> 打 tag 前先确认 `manifest` 的 `version` 已同步改动：CI 会校验 tag 与 manifest 版本一致，
> 不一致直接失败（避免发出"标题 v1.0.2、附件却是 moviepilot-1.0.1.fpk"的 Release）。
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
> - **未用 `--with-runtime`**（如 Windows 本地构建）：安装时尝试用 fnOS `python312` 建 venv 并 `pip install` 依赖（走国内加速源）。但 `manifest` 已不再声明 `python312`，且依赖要求 `requires-python >= 3.14`，这条路径实际上装不上 —— 它只是给本地开发留的降级分支，**请使用 CI 产物或 Linux/macOS 上带 `--with-runtime` 构建**。

## 使用

安装完成后，在 fnOS 桌面点击 MoviePilot 图标即可打开 Web 界面（嵌入 iframe）。数据存储于：

| 用途 | 位置 |
|------|------|
| 配置 / 数据库 | `TRIM_PKGVAR/config`（持久） |
| 下载目录 | `TRIM_DATA_SHARE_PATHS` 共享目录 |
| 媒体库目录 | 用户在应用设置中授权 |

可在应用设置中修改端口、重置超级管理员密码。

## 上游自动更新（重启即升级）

上游 MoviePilot 发布新版本时**不需要重新构建 fpk**：应用每次启动（含重启）前会
自动检查上游 Release，有新版本就下载并就地替换后端源码与前端 dist，然后用新代码启动。

### 为什么不用 MoviePilot 自带的更新功能

上游 V3 确实有 `SystemUpdateManager`（界面里的"检查更新/下载/安装"），但它在 fnOS 上
三步里断了后两步（v3.0.3 源码核实）：

| 阶段 | 上游实现 | 在 fnOS 包里 |
|------|----------|--------------|
| 检查 | 定时任务，受 `MOVIEPILOT_AUTO_UPDATE` 控制（默认关闭，官方注释：只提示、不自动下载安装） | 可用 |
| 下载 | 界面点"下载"，但非 Docker 时 `_prepare_local_backend_ref()` 要求程序目录是 **git 仓库** | 不可用（fpk 解包无 `.git`） |
| 安装 | 非 Docker 时 `apply_prepared_update()` 直接返回"当前运行环境不是 Docker"（`is_docker()` 只看 `/.dockerenv`），本地路径还要 `git` + `uv` 重建 venv | 不可用 |

且"重启后应用已下载包"这一步挂在 CLI 的 `start`/`restart` 里，本应用由 supervisor
直接 `python app/main.py` 启动，压根不经过 CLI。所以改为自研更新器：
**下载 Release 压缩包 + 目录级替换**，只依赖标准库，不需要 git / uv。

### 工作流程

1. 读本地版本（`mp/version.py`）→ 查上游最新版本 → 版本更高才继续（无更新时只做一次 API 查询，秒级返回）
   版本发现是**四级降级**：`api.github.com` **直连**（不走加速）→ 网页 `releases/latest` → `releases.atom` → 分支 `version.py`（raw 文件型 URL）。后三级都落在 `github.com` / `raw.githubusercontent.com` 上、可走加速前缀 —— 加速镜像普遍只转发「文件」型 URL（实测 gh-proxy 对 releases 网页直接 403/404，对归档与 raw 正常），所以「下载得动」的通道一定也「查得到版本」
2. 下载后端 zip → 校验结构（有 `app/`、`version.py` 与 tag 一致）→ 解析出 `FRONTEND_VERSION`
3. 依赖预检：用新 `uv.lock` 对比已装环境，缺什么补什么（`pip`，走国内镜像）
4. 备份 → 替换 `app/ config/ database/ scripts/ skills/ moviepilot/` 与 `version.py` 等 → **回填资源包文件**（sites 二进制来自独立仓库，上游 zip 里没有）→ 校验资源在位（缺失直接判更新失败并回滚）
5. 前端 dist 整体替换
6. 自检（关键依赖 import + 源码语法编译），**失败即整树回滚**
7. 写状态文件，保留最近 2 代备份

另外：如果 MoviePilot 界面已经下载过更新包（`config/temp/movietpilot-update/`），
更新器会**直接复用**它，不重复下载。

### 开关（`app.env`，也可在应用设置里切换）

| 变量 | 默认 | 说明 |
|------|------|------|
| `MP_AUTO_UPDATE` | `1` | 总开关，应用设置里有下拉可选 |
| `MP_UPDATE_CHANNEL` | `release` | `release` 仅正式版 / `prerelease` 含测试版 / `off` 关闭 |
| `MP_UPDATE_INTERVAL` | `21600` | 检查间隔（秒），避免每次重启都查 GitHub |
| `MP_UPDATE_DEPS` | `1` | 是否同步 Python 依赖；关闭且新版本有新增依赖时会**拒绝更新**（避免装出起不来的版本） |
| `GITHUB_PROXY` | `https://gh-proxy.com/` | 加速前缀，**只作用于 `github.com` 系 URL**（归档下载、网页兜底版本发现）；`api.github.com` 一律直连，不受它影响 |
| `GITHUB_PROXY_MIRRORS` | 空 | 额外加速前缀，逗号分隔（如 `https://a/,https://b/`）。内置镜像失效时可不动代码换一批 |

> 版本发现失败时不占用整个检查间隔：只冷却 15 分钟就重试（网络抖动不该让自动更新停摆半天）。
> 四级降级带时间预算（API 直连 15s、单次请求 10s、整体 120s），所以更新检查不会把启动拖成几分钟。

### 手动操作与回退

```bash
# 立即检查并更新（忽略冷却）；应用运行中会先停后启
/var/apps/moviepilot/target/cmd/main update
# 只看有没有新版本，不动任何文件
/var/apps/moviepilot/target/cmd/main update-check
# 回滚到更新前的版本（备份在 <应用目录>/.mp-backup/）
/var/apps/moviepilot/target/cmd/main rollback
# 修复站点资源（sites 模块错位/缺失导致后端起不来时）
/var/apps/moviepilot/target/cmd/main repair
```

更新日志：`TRIM_PKGVAR/update.log`；状态文件：`TRIM_PKGVAR/config/mp_update.json`。

> 注意：通过 fpk 重新安装/升级应用时，应用目录会被整包替换，自更新的内容随之回到
> 安装包自带的版本（这是预期行为，也让"应用包升级"始终是可靠的回退路径）。

## 已知假设与限制

- 本包为**原生 Native 应用**。MoviePilot V3 要求 `requires-python >= 3.14`，因此正常产物通过 `--with-runtime` 自带 CPython 3.14 与全部依赖（含 langchain、Rust 扩展等），安装时不联网，也**不需要 fnOS 的 python312**（`manifest` 已不再声明）
- Node.js（`nodejs_v24`）是唯一仍需 fnOS 提供的运行时，用于前端静态服务
- `--with-runtime` 需要在 **Linux/macOS** 构建机上执行（Windows 无法交叉准备 Linux 运行时）；且构建机架构需与目标 NAS 架构一致（amd64/arm64 分开构建，CI 用对应的原生 runner）
- 依赖**优先使用预编译 wheel**，缺失时才源码构建 —— `anitopy`、`pinyin2hanzi` 是纯 Python 的 sdist-only 依赖（PyPI 上无 wheel），禁止构建会直接解析失败，因此不能一刀切 `--no-build`。风险由两道审计兜底：wheel 的 manylinux 基线不得高于 glibc 2.36（fnOS / Debian 12），且凡是本地编译出原生 `.so` 的一律终止构建（它链接的是构建机的 glibc 2.39）
- 产物体积较大（自带解释器 + 全部依赖），预期在数百 MB 量级；这是「安装时零联网」的代价
- MoviePilot 的插件依赖是在运行时用自身解释器的 pip 安装到 `app/python/` 的 `site-packages`。**升级会整包替换应用目录，插件依赖需要重新安装**
- 自更新只替换后端源码与前端静态文件，不替换自带 Python 运行时（`app/python/`）；新版本引入新依赖时由更新器用 pip 增量补装（需要联网，走国内镜像）
- 自更新要求应用目录可写（`TRIM_APPDEST`）。目录只读时会跳过更新并记日志，此时只能通过重新安装应用包升级
- 默认使用 **SQLite**；如需 PostgreSQL，可在 `app.env` 中设置 `DB_TYPE=postgresql` 并配置连接（需 fnOS 安装 PostgreSQL）
- 后端源码、前端产物、自带 Python 运行时、fnpack 工具、下载缓存等**所有构建产物全部收敛在 `.local-build/`，不纳入 git**，由构建脚本生成；拉取仓库后需先执行构建脚本，项目根目录不残留任何构建产物
- 首个登录使用安装向导设置的管理员账号

## 感谢与上游仓库

本应用是在以下优秀开源项目之上进行的 fnOS 原生封装，特此致谢：

| 上游仓库 | 说明 |
|----------|------|
| [jxxghp/MoviePilot](https://github.com/jxxghp/MoviePilot) | 核心后端（FastAPI 媒体库自动化管理，本包使用的 V3 源码） |
| [jxxghp/MoviePilot-Frontend](https://github.com/jxxghp/MoviePilot-Frontend) | Vue3 前端（官方预编译 `dist.zip`） |
| [jxxghp/MoviePilot-Resources](https://github.com/jxxghp/MoviePilot-Resources) | 站点资源（sites 模块 / 索引数据，由独立仓库分发） |
| [astral-sh/python-build-standalone](https://github.com/astral-sh/python-build-standalone) | 自带可重定位的 CPython 3.14 运行时（随包分发，安装零联网） |
| [astral-sh/uv](https://github.com/astral-sh/uv) | 依赖锁定与安装（构建期按 `uv.lock` 装配 `site-packages`） |
| [fnOS 应用中心](https://www.fnnas.com/) | 飞牛 fnOS 原生应用运行环境、统一网关与 Node.js 运行时 |

下载加速依赖 [gh-proxy.com](https://gh-proxy.com/) / [ghfast.top](https://ghfast.top/) 等 GitHub 镜像（直连不通时自动回退）。

## 免责声明

MoviePilot 遵循 GPL-3.0 许可证，仅限学习交流。本应用包仅为第三方封装，请自行评估合规与使用风险。
