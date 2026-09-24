# 连接 PostgreSQL：运行时组配置

当 `WECOM_FORWARD_PLUS_CONFIG_SOURCE=database` 时，配置组不再来自 `.env`，而是保存在
PostgreSQL 的 `groups` 表中。运行中的进程定期重读该表（默认 30 秒），自动启停对应
的 WeCom 长连接——新增、修改、停用配置组都不需要重新打包或重启进程。

两种配置源严格二选一：不会把 `.env` 的组导入数据库；database 模式下
`WECOM_FORWARD_PLUS_GROUP_*` 变量被完全忽略（日志会提示一次）。

## 建库与授权

```sql
CREATE DATABASE wecom;
CREATE USER wecom WITH PASSWORD '...';
GRANT CONNECT ON DATABASE wecom TO wecom;
-- 在 wecom 库中（表由应用启动时自动创建）：
GRANT SELECT, INSERT, UPDATE, DELETE ON groups TO wecom;
```

连接串（含密码，绝不写日志、绝不提交进仓库）：

```
WECOM_FORWARD_PLUS_DATABASE_URL=postgresql://wecom:<password>@<host>:5432/wecom
```

> 密码含特殊字符时需要 URL 编码，例如 `@` 写作 `%40`。

## 表结构（启动时自动创建）

```sql
CREATE TABLE IF NOT EXISTS groups (
    id                  SERIAL PRIMARY KEY,
    name                TEXT NOT NULL UNIQUE,
    wecom_robot_id      TEXT NOT NULL,
    wecom_robot_secret  TEXT NOT NULL,
    dify_api_key        TEXT NOT NULL,
    session_max_total   INTEGER,          -- NULL = 用全局默认
    session_ttl_seconds INTEGER,          -- NULL = 用全局默认
    enabled             BOOLEAN NOT NULL DEFAULT TRUE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

全局设置（`DIFY_BASE_URL`、重置关键词、会话默认值）仍只在 `.env` 中；数据库里
只有组级配置。字段为 NULL 的会话参数自动继承全局默认。

## 从零开始：创建第一批配置组

空表是合法的启动状态——进程以 0 个客户端启动，插入数据后自动上线。两种方式：

**方式一：直接 SQL（psql / 数据库客户端）**

```sql
INSERT INTO groups (name, wecom_robot_id, wecom_robot_secret, dify_api_key)
VALUES ('sales', '机器人ID', '机器人Secret', 'app-xxx');
```

**方式二：管理网页（推荐）**

database 模式默认启用内置管理界面，浏览器打开 `http://127.0.0.1:8080`，用
`WECOM_FORWARD_PLUS_ADMIN_PASSWORD` 登录后在表格里直接新建组。详见 README 的
「管理界面」章节。Docker 部署时同样在 `http://127.0.0.1:8080` 访问（见下方
「Docker Compose 部署」的端口说明）。

## 日常运维操作

| 操作 | SQL |
| --- | --- |
| 新增组 | `INSERT INTO groups (name, wecom_robot_id, wecom_robot_secret, dify_api_key) VALUES (...);` |
| 停用 / 启用组 | `UPDATE groups SET enabled = false WHERE name = 'sales';` |
| 换 Dify API Key | `UPDATE groups SET dify_api_key = 'app-new' WHERE name = 'sales';` |
| 换机器人凭证 | `UPDATE groups SET wecom_robot_secret = '...' WHERE name = 'sales';` |
| 调整会话参数 | `UPDATE groups SET session_max_total = 300, session_ttl_seconds = 600 WHERE name = 'sales';` |
| 删除组 | `DELETE FROM groups WHERE name = 'sales';` |

生效语义（无需重启进程）：

- **新增 / 重新启用**：一个重读周期内（默认 30 秒；管理界面写入即时生效）该组的
  客户端自动连接。
- **停用 / 删除**：该组客户端断开，其会话被清空。
- **机器人凭证变更**：该组客户端自动重启，用户会话保留。
- **Dify API Key 变更**：下一条消息即用新 Key，不断线重连。
- **会话参数变更**：原地生效，在线会话不丢失。
- **数据库不可达**：进程继续用最后已知配置运行并周期重试；启动时不可达则退出
  （exit code 1，配合 `restart: unless-stopped` 自动重试）。

验证方法：执行 SQL 后观察日志（`logs/app.log` 或 docker logs），应出现
`Started WeCom client for group=sales` / `Stopped WeCom client for group=sales`
等记录，然后向机器人发消息测试。

## Docker Compose 部署

`docker/docker-compose.yml` 已内置可选的 `postgres:16-alpine` 服务（带健康检查
与 `pgdata` 数据卷），应用容器等数据库健康后启动。数据库端口映射为宿主机
`5772` → 容器 `5432`：应用容器内用 `postgres:5432` 访问，宿主机上用
`127.0.0.1:5772` 连接（例如 `psql -h 127.0.0.1 -p 5772 -U wecom -d wecom`）。
`groups` 表会自动创建——`docker/postgres-init.sql` 在数据卷首次初始化时执行
（幂等的 `CREATE TABLE IF NOT EXISTS`），应用每次启动时也会执行同样的 DDL。

`.env` 中设置：

```
WECOM_FORWARD_PLUS_CONFIG_SOURCE=database
POSTGRES_PASSWORD=<设置一个密码>
WECOM_FORWARD_PLUS_DATABASE_URL=postgresql://wecom:<密码>@postgres:5432/wecom
```

如需使用外部 PostgreSQL，删掉 compose 里的 `postgres` 服务、`ports` 与
`depends_on` 块，把 `DATABASE_URL` 指向外部实例即可。

管理网页在容器内始终监听 `0.0.0.0`（compose 覆盖了 `.env` 里的
`ADMIN_BIND`），并发布到宿主机 `127.0.0.1` 的同号端口（默认
`http://127.0.0.1:8080`，跟随 `ADMIN_PORT`）。需要从外部网络访问时，在
`.env` 中设置 `WECOM_ADMIN_PUBLISH_HOST=0.0.0.0` 并置于 TLS / 反向代理之后。

## 安全注意事项

- 密钥（机器人 Secret、Dify Key、数据库密码）以明文存在 `groups` 表中——进程
  环境本就持有同级密钥，应用层加密只是换个位置。请用最小权限数据库账号、把
  数据库放在内网、必要时启用磁盘加密。
- DSN 与任何密钥值都不会出现在日志中。
- 同一数据库只允许一个进程实例连接（多实例会重复连接同一批机器人）。
