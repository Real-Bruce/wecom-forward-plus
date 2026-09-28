[English](README.md) | 简体中文

# wecom-forward-plus

一个连接企业微信机器人与 [Dify](https://dify.ai) 应用的 Python 桥接服务。它通过企业微信机器人的 WebSocket 长连接通道接入，把用户发来的消息转发给 Dify 聊天应用，再把流式回复发回企业微信。

消息链路：

```
Dify  <=>  wecom-forward-plus  <=>  企业微信机器人
```

## 功能特性

- **多分组**：每个分组绑定一个企业微信机器人和一个 Dify 应用 API Key，可以各自独立配置。
- **以企业微信账号名作用户标识**：发送者的账号名以 `wx_<账号名>` 的形式发给 Dify（如 `zhangsan` → `wx_zhangsan`），让每个用户保有独立的对话历史。
- **文件与图片转发**：图片、文件（Word/PDF 等）以及图文混排消息会先从企业微信下载，上传到 Dify，再通过 `files` 参数随请求发送；语音消息仍走自动转文字。
- **会话管理**：每个分组有独立的会话池，带 TTL（默认 5 分钟不活跃重置）、分组容量上限（默认 200）、LRU 淘汰，以及关键词触发的会话重置。
- **流式 Dify 回复**：使用 Dify 的 `streaming` 响应模式，累积完整答案后再回复。
- **容错**：企业微信自动重连（指数退避）、回复重试，以及清洗过的错误处理（Dify 出错时返回友好提示而不是崩溃）。

## 架构

```
src/
├── main.py               入口：加载配置、日志，为每个分组启动一个客户端
├── config.py             解析并校验 WECOM_FORWARD_PLUS_* 环境变量
├── config_store.py       进程内可变的分组注册表（热重载接缝）
├── db_config.py          SQLite groups 表：仓储 + 行映射/校验/diff
├── group_manager.py      每个分组的活跃企业微信客户端 + 数据库对账循环
├── admin_server.py       带鉴权的管理后台 Web UI，维护 groups 表
├── session_manager.py    分组级会话池（TTL + 容量上限 + LRU 淘汰 + 重置）
├── dify_client.py        异步 Dify chat-messages 客户端（流式）+ 文件上传 + SSE 解析
├── wecom_client.py       封装 wecom-aibot-python-sdk（长连接 + 媒体下载）
├── message_handler.py    路由：企业微信消息 → 会话 → Dify → 回复文本
├── attachment.py         单个已下载媒体项的不可变描述
├── web/static/           管理页单页应用（HTML + 原生 JS + CSS，无构建步骤）
└── constants.py          固定的用户可见回复文案
```

单条消息的处理流程：

1. 企业微信推送一帧消息；`wecom_client` 提取发送者账号和文本内容（媒体帧会立即下载并解密，因为企业微信的媒体 URL 约 5 分钟后失效）。
2. `message_handler` 构造 `dify_user = "wx_" + from_user`。
3. 如果文本是重置关键词且消息不带文件，就重置该用户的会话并返回固定回复（不会调用 Dify）。
4. 媒体附件先在本地做大小检查，再以同一个 `wx_<账号名>` 用户调用 `POST /files/upload` 上传，并在 `POST /chat-messages`（以 `response_mode="streaming"` 调用）的 `files` 数组里引用。
5. 累积流式返回的答案，保存返回的 `conversation_id`，把答案发回企业微信。

## 文件与图片转发

以下企业微信消息类型会被转发到 Dify：

| 企业微信消息 | 发给 Dify 的形式 |
| --- | --- |
| 图片 | 一条 `type: "image"` 的 `files[]` 条目 |
| 文件（Word、PDF 等） | 一条 `type: "document"` 的 `files[]` 条目 |
| 图文混排 | 文本作为 `query`，每张图片一条 `files[]` 条目 |
| 语音 | 自动转写后的文本（与之前一致，不发送文件） |

细节与限制：

- **Dify 前提条件**：目标应用必须在其文件上传设置里允许对应的文件类型（图片理解还需开启视觉能力），否则上传会被拒绝，用户会收到上传失败的回复。
- **默认 query**：文件不附带文字时，固定以「请处理我发送的文件」作为 `query` 发送（Dify 要求 query 非空）。
- **大小限制**：Dify 的默认限制（图片 10 MB，其他文件 15 MB）会在本地预先检查，超限文件立即收到友好回复；自建实例自定义了限制时，服务端的 `413` 也映射为同一条回复。
- **用户身份**：上传和聊天请求携带同一个 `wx_<账号名>` 用户，Dify 要求这样才能引用已上传的文件。
- **重置关键词**：带附件到达的重置关键词仍会正常转发消息（不触发重置）。

文件类消息的错误回复（定义在 `src/constants.py`）：

| 情形 | 回复 |
| --- | --- |
| 企业微信下载/解密失败 | 文件下载失败，请稍后重试 |
| Dify 上传失败 | 文件上传失败，请稍后重试 |
| 文件超出大小限制 | 文件超出大小限制（图片最大10MB，其他文件最大15MB），请压缩后重试 |
| 下载的文件为空 | 文件内容为空，请检查后重新发送 |

## 环境要求

- Python 3.9+
- 一个企业微信机器人（bot id 和 secret，来自管理后台）
- 一个已配好 API Key 的 Dify 应用

## 安装

```bash
git clone <repo-url>
cd wecom-forward-plus

# 创建并激活虚拟环境（推荐）
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS / Linux:
source .venv/bin/activate

pip install -e .
```

## 配置

复制示例文件并填入真实值：

```bash
cp .env.example .env
```

所有配置都从环境变量读取（经 python-dotenv 从 `.env` 加载）。密钥只放在 `.env` 里，该文件已被 git 忽略。

| 变量 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `WECOM_FORWARD_PLUS_DIFY_BASE_URL` | ✅ | — | Dify API base URL，如 `https://your-dify.example.com/v1` |
| `WECOM_FORWARD_PLUS_SESSION_TTL_SECONDS` | — | `300` | 多少秒不活跃后对话重置 |
| `WECOM_FORWARD_PLUS_SESSION_MAX_TOTAL` | — | `200` | 每个分组的活跃会话全局上限 |
| `WECOM_FORWARD_PLUS_SESSION_RESET_KEYWORDS` | — | `["开启新对话","重置对话","新一轮对话"]` | 触发新对话的关键词（JSON 数组或逗号分隔） |
| `WECOM_FORWARD_PLUS_GROUP_{N}_NAME` | — | `group-{N}` | 分组的显示名称 / id |
| `WECOM_FORWARD_PLUS_GROUP_{N}_WECOM_ROBOT_ID` | ✅（每个分组） | — | 企业微信机器人 id |
| `WECOM_FORWARD_PLUS_GROUP_{N}_WECOM_ROBOT_SECRET` | ✅（每个分组） | — | 企业微信机器人 secret |
| `WECOM_FORWARD_PLUS_GROUP_{N}_DIFY_API_KEY` | ✅（每个分组） | — | Dify 应用 API Key |
| `WECOM_FORWARD_PLUS_GROUP_{N}_SESSION_MAX_TOTAL` | — | 全局默认值 | 分组级会话上限覆盖 |
| `WECOM_FORWARD_PLUS_GROUP_{N}_SESSION_TTL_SECONDS` | — | 全局默认值 | 分组级对话 TTL 覆盖 |
| `WECOM_FORWARD_PLUS_CONFIG_SOURCE` | — | `env` | 分组配置来源：`env`（环境变量）或 `database`（SQLite，见[下文](#运行时分组配置sqlite)） |
| `WECOM_FORWARD_PLUS_DATABASE_PATH` | — | `data/wecom.db` | 存放 `groups` 表的 SQLite 文件（数据库模式；自动创建，含父目录） |
| `WECOM_FORWARD_PLUS_DB_RELOAD_INTERVAL_SECONDS` | — | `30` | 运行中的进程多久从数据库重读一次分组配置（最小 5） |
| `WECOM_FORWARD_PLUS_ADMIN_UI` | — | `on` | 管理后台 Web UI 开关（仅数据库模式，见[下文](#管理后台-web-ui)） |
| `WECOM_FORWARD_PLUS_ADMIN_PASSWORD` | ✅，当 `CONFIG_SOURCE=database` 且 UI 开启 | — | 管理登录密码（永不记入日志） |
| `WECOM_FORWARD_PLUS_ADMIN_BIND` | — | `127.0.0.1` | 管理页监听地址 |
| `WECOM_FORWARD_PLUS_ADMIN_PORT` | — | `8080` | 管理页监听端口 |
| `WECOM_FORWARD_PLUS_ADMIN_COOKIE_SECURE` | — | `off` | 为 Cookie 加 `Secure` 标志（前端做了 TLS 终结时开启） |

分组序号从 1 开始且必须连续。每个分组必须同时提供 `WECOM_ROBOT_ID`、`WECOM_ROBOT_SECRET` 和 `DIFY_API_KEY`；不完整的分组会导致启动失败，退出码为 1。

`CONFIG_SOURCE=database` 时，`GROUP_` 系列变量完全不被解析，分组配置改从数据库读取（见下文），迁移后残留在 `.env` 里的旧条目不会影响启动。

## 运行时分组配置（SQLite）

设置 `WECOM_FORWARD_PLUS_CONFIG_SOURCE=database` 后，分组管理迁移到一张 SQLite 表（`WECOM_FORWARD_PLUS_DATABASE_PATH`，默认 `data/wecom.db`，不需要单独的数据库服务、账号或密码）。两种来源严格二选一：永远不会把 `.env` 的内容导入数据库，数据库模式生效期间 `GROUP_` 变量也会被忽略。

文件和表在启动时自动创建：

```sql
CREATE TABLE IF NOT EXISTS groups (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    name                TEXT NOT NULL UNIQUE,
    wecom_robot_id      TEXT NOT NULL,
    wecom_robot_secret  TEXT NOT NULL,
    dify_api_key        TEXT NOT NULL,
    session_max_total   INTEGER,          -- NULL = 使用全局默认值
    session_ttl_seconds INTEGER,          -- NULL = 使用全局默认值
    enabled             INTEGER NOT NULL DEFAULT 1,
    created_at          TEXT NOT NULL DEFAULT (datetime('now', '+8 hours')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now', '+8 hours'))
);
```

`created_at`/`updated_at` 以北京时间（UTC+8）存储，SQLite 的 `datetime('now')` 单独使用时是 UTC。

可以直接用 SQL 插入第一个分组：

```sql
INSERT INTO groups (name, wecom_robot_id, wecom_robot_secret, dify_api_key)
VALUES ('sales', 'your-robot-id', 'your-robot-secret', 'app-your-dify-key');
```

### 热重载语义

运行中的进程每隔 `DB_RELOAD_INTERVAL_SECONDS`（默认 30 秒）重读这张表，无需重启即可收敛到最新状态：

| 变更 | 效果 |
| --- | --- |
| 新增行（`enabled = true`） | 一个周期内为该分组连接企业微信客户端 |
| `enabled = false` 或 `DELETE` | 该分组的客户端断开，其会话被丢弃 |
| `wecom_robot_id` / `wecom_robot_secret` 变更 | 该分组的客户端重启（其会话保留） |
| `dify_api_key` 变更 | 下一条消息生效，无需重连 |
| `session_max_total` / `session_ttl_seconds` 变更 | 原地生效，存量会话不受影响 |

- 空表是合法状态：进程以 0 个客户端启动，插入分组后热加载。
- 数据库变得不可达时，进程带着最后一次已知的配置继续运行，并在每个周期重试。
- 启动时数据库不可达，进程以退出码 1 退出（Docker 的 `restart` 策略会接着重试）。
- 企业微信连接持续失败的分组，每个周期都会重试，直到连上或被删除。

全局设置（`DIFY_BASE_URL`、重置关键词、可空列回退到的会话默认值）仍然只来自环境变量；数据库里只存分组级数值。

密钥以明文存储：进程环境里本来就持有等效凭据，应用层加密只是换个位置存放。请在文件系统层面限制数据库文件的读取权限，并依赖「永不写入日志」规则：任何凭据值都不会被写入日志。

备份、SQL 示例和 Docker Compose 说明见 [docs/config-database.md](docs/config-database.md)。

## 管理后台 Web UI

数据库模式下，进程内置一个管理页面（无需 Node/npm 构建步骤，纯 aiohttp），用来维护分组表：不写 SQL 就能创建、编辑、删除分组，以及启用/停用。默认开启（`ADMIN_UI=off` 可关闭），登录需要 `WECOM_FORWARD_PLUS_ADMIN_PASSWORD`。

打开 `http://127.0.0.1:8080`（或你设置的 `ADMIN_BIND`/`ADMIN_PORT`）并登录。Docker 下，compose 文件会把容器内监听地址覆盖为 `0.0.0.0`，并把端口发布到宿主机 `127.0.0.1` 的同一 `ADMIN_PORT`；在 `.env` 里设置 `WECOM_ADMIN_PUBLISH_HOST=0.0.0.0` 可以把它暴露到网络（请先在前端加 TLS 或反向代理）：

- 表格展示所有分组：名称、启用开关、机器人 id、打码显示的机器人 secret 和 Dify API Key、会话参数（继承来的值以斜体显示），以及最后更新时间。
- 表格上方的横幅高亮全局默认值：Dify base URL（`DIFY_BASE_URL`）、默认会话上限和默认会话 TTL。
- 「新增配置组」打开包含全部字段的表单；红色 `*` 标记的字段必填。会话字段留空则继承全局默认值。
- 编辑已有分组时密钥字段是空的，留空表示「保留已存的值」，只有要轮换密钥时才填写。已存的密钥本身永远不会发回浏览器。
- 切换启用或保存修改立即生效（一次数据库往返内），不用等 30 秒的重载周期。

安全说明：

- 会话是内存 Cookie（HttpOnly、SameSite=Strict），8 小时过期，重启后失效。登录失败做全局限流（10 分钟内 5 次失败 → 锁定 60 秒）。
- 带请求体的变更类 API 调用要求 JSON content type，配合 SameSite=Strict 可以挡住跨站表单提交。
- 管理页走纯 HTTP：保持默认的 `127.0.0.1` 监听，通过 SSH 隧道或终结 TLS 的反向代理访问；在 TLS 后面时设置 `ADMIN_COOKIE_SECURE=on`。
- 启动时端口冲突会让进程直接失败（退出码 1），而不是带病运行。

## 运行

```bash
# 源码运行
python -m src.main

# 或 pip install -e . 之后
wecom-forward-plus
```

日志输出到 stdout 并轮转到 `logs/app.log`。

## 打包部署

要在不克隆仓库的服务器上部署，一条命令就能构建源码归档：

```bash
bash scripts/package.sh
# -> dist/wecom-forward-plus-<commit>.tar.gz
```

归档由 `git archive` 从当前 `HEAD` 提交生成，只包含被 git 跟踪的文件（源码、测试、`docker/`、`docs/`），绝不会包含 `.env`、`.venv`、`logs/` 之类的本地状态。文件名会追加短提交哈希；如果工作区有未提交的改动，脚本会警告这些改动不在归档里。

传输到服务器并解压：

```bash
scp dist/wecom-forward-plus-<commit>.tar.gz user@server:~/
ssh user@server
tar -xzf wecom-forward-plus-<commit>.tar.gz   # 解压到 ./wecom-forward-plus/
```

然后继续看[使用 Docker 部署](#使用-docker-部署)。

## 使用 Docker 部署

所有 Docker 文件都在 `docker/` 目录（`Dockerfile`、`docker-compose.yml`、`Dockerfile.dockerignore`）；仓库根目录的 `compose.yaml` 只是一层薄封装，它 include 了 `docker/docker-compose.yml`，这样从仓库根目录运行 Compose 时，会加载根目录的 `.env` 做 `${VAR}` 插值（`WECOM_FORWARD_PLUS_ADMIN_PORT` / `WECOM_ADMIN_PUBLISH_HOST`）。请从仓库根目录运行，这些变量才会生效。

数据库模式下，`groups` 表存放在 SQLite 文件里，不需要单独的数据库容器。compose 文件把仓库根目录的 `data/` 挂载到 `/app/data`，与默认的 `WECOM_FORWARD_PLUS_DATABASE_PATH=data/wecom.db` 对应，重建容器后分组配置仍然保留；文件和表在启动时自动创建。见 [docs/config-database.md](docs/config-database.md)。

```bash
# 1. 在仓库根目录准备配置（不会被提交）
cp .env.example .env
#   ……填入真实值……

# 2. 构建并在后台启动（从仓库根目录运行）
docker compose up -d --build

# 3. 跟踪日志
docker compose logs -f

# 停止 / 重启
docker compose down
docker compose restart
```

配置通过 compose 的 `env_file`（仓库根目录的 `.env`）注入；镜像本身不含密钥。宿主机的 `logs/` 目录（仓库根目录）挂载到 `/app/logs`，轮转日志 `logs/app.log` 在重建容器后仍然保留。容器会自动重启（`unless-stopped`），除非被显式停止。

数据库模式下，管理页可以从宿主机的 `http://127.0.0.1:8080` 访问（compose 把容器内监听地址覆盖为 `0.0.0.0`，并把端口发布到宿主机回环地址）。要暴露到网络，在 `.env` 里设置 `WECOM_ADMIN_PUBLISH_HOST=0.0.0.0`，并在前面加 TLS 或反向代理；要改端口，设置 `WECOM_FORWARD_PLUS_ADMIN_PORT`，宿主机端口和容器端口会自动跟随。

## 测试

```bash
pip install -e ".[dev]"
pytest
```

## 许可证

[MIT](LICENSE)
