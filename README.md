# wecom-forward-plus

A Python bridge between Enterprise WeChat (企业微信) robots and [Dify](https://dify.ai) apps. It connects to WeCom robots over their WebSocket long-connection channel, forwards incoming user messages to a Dify chat application, and sends the streaming reply back to WeCom.

Message path:

```
Dify  <=>  wecom-forward-plus  <=>  企业微信机器人
```

## Features

- **Multiple groups** — each group pairs one WeCom robot with one Dify app API key and can be configured independently.
- **WeCom account name as `user`** — the sender's account name is sent to Dify as `wx_<account>` (e.g. `zhangsan` → `wx_zhangsan`), so each user keeps their own conversation history.
- **File & image forwarding** — images, files (Word/PDF/…) and mixed image+text messages are downloaded from WeCom, uploaded to Dify, and sent with the `files` parameter; voice messages stay auto-transcribed.
- **Session management** — per-group session pools with a TTL (default 5 minutes of inactivity), a per-group cap (default 200), least-recently-used eviction, and keyword-triggered resets.
- **Streaming Dify replies** — uses Dify's `streaming` response mode and accumulates the full answer before replying.
- **Resilience** — automatic WeCom reconnection (exponential backoff), reply retries, and sanitized error handling (Dify failures return a friendly message instead of crashing).

## Architecture

```
src/
├── main.py               Entry point: load config, logging, start one client per group
├── config.py             Parse + validate WECOM_FORWARD_PLUS_* environment variables
├── config_store.py       Mutable in-process group registry (hot-reload seam)
├── db_config.py          PostgreSQL groups table: repository + row mapping/validation/diff
├── group_manager.py      Live WeCom client per group + database reconcile loop
├── admin_server.py       Authenticated admin web UI over the groups table
├── session_manager.py    Per-group session pools (TTL + cap + LRU eviction + reset)
├── dify_client.py        Async Dify chat-messages client (streaming) + file upload + SSE parsing
├── wecom_client.py       Wrapper around wecom-aibot-python-sdk (long connection + media download)
├── message_handler.py    Routes WeCom message -> session -> Dify -> reply text
├── attachment.py         Immutable description of one downloaded media item
├── web/static/           Admin UI single page (HTML + vanilla JS + CSS, no build step)
└── constants.py          Fixed user-facing reply strings
```

Flow of a single message:

1. WeCom delivers a frame; `wecom_client` extracts the sender account and text content (media frames are downloaded and decrypted right away — the WeCom media URLs expire after ~5 minutes).
2. `message_handler` builds `dify_user = "wx_" + from_user`.
3. If the text is a reset keyword *and* the message carries no files, the user's session is reset and the fixed reply is returned (Dify is **not** called).
4. Media attachments are size-checked locally, then uploaded via `POST /files/upload` under the same `wx_<account>` user, and referenced from the `files` array of `POST /chat-messages` (which is called with `response_mode="streaming"`).
5. The streamed answer is accumulated, the returned `conversation_id` is stored, and the answer is sent back to WeCom.

## File and image forwarding

The following WeCom message types are forwarded to Dify:

| WeCom message | Sent to Dify as |
| --- | --- |
| Image | one `files[]` entry with `type: "image"` |
| File (Word, PDF, …) | one `files[]` entry with `type: "document"` |
| Mixed (image + text) | the text as `query` plus one `files[]` entry per image |
| Voice | auto-transcribed text (as before, no file is sent) |

Details and caveats:

- **Dify prerequisites** — the target app must allow the corresponding file types in its file-upload settings (and enable vision for image understanding); otherwise the upload is rejected and the user receives the upload-failed reply.
- **Default query** — when a file arrives with no accompanying text, the fixed message 「请处理我发送的文件」 is sent as the `query` (Dify requires a non-empty query).
- **Size limits** — Dify's defaults (images 10 MB, other files 15 MB) are pre-checked locally so an oversized file gets an immediate friendly reply; a server-side `413` from a self-hosted instance with custom limits maps to the same reply.
- **User identity** — the upload and the chat request both carry the same `wx_<account>` user, which Dify requires for referencing an uploaded file.
- **Reset keywords** — a reset keyword arriving *with* an attachment still forwards the message (no reset happens).

Error replies for file messages (defined in `src/constants.py`):

| Situation | Reply |
| --- | --- |
| WeCom download/decryption failed | 文件下载失败，请稍后重试 |
| Dify upload failed | 文件上传失败，请稍后重试 |
| File exceeds the size limit | 文件超出大小限制（图片最大10MB，其他文件最大15MB），请压缩后重试 |
| Downloaded file is empty | 文件内容为空，请检查后重新发送 |

## Requirements

- Python 3.9+
- An Enterprise WeChat robot (bot id + secret from the admin console)
- A Dify application with an API key

## Installation

```bash
git clone <repo-url>
cd wecom-forward-plus

# Create and activate a virtual environment (recommended)
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS / Linux:
source .venv/bin/activate

pip install -e .
```

## Configuration

Copy the example and fill in real values:

```bash
cp .env.example .env
```

All settings are read from environment variables (loaded from `.env` via python-dotenv). Secrets live only in `.env`, which is git-ignored.

| Variable | Required | Default | Description |
| --- | --- | --- | --- |
| `WECOM_FORWARD_PLUS_DIFY_BASE_URL` | ✅ | — | Dify API base URL, e.g. `https://your-dify.example.com/v1` |
| `WECOM_FORWARD_PLUS_SESSION_TTL_SECONDS` | — | `300` | Seconds of inactivity before a conversation resets |
| `WECOM_FORWARD_PLUS_SESSION_MAX_TOTAL` | — | `200` | Global per-group cap on active sessions |
| `WECOM_FORWARD_PLUS_SESSION_RESET_KEYWORDS` | — | `["开启新对话","重置对话","新一轮对话"]` | Keywords that start a new conversation (JSON array or comma-separated) |
| `WECOM_FORWARD_PLUS_GROUP_{N}_NAME` | — | `group-{N}` | Display name / id of the group |
| `WECOM_FORWARD_PLUS_GROUP_{N}_WECOM_ROBOT_ID` | ✅ (per group) | — | WeCom robot id |
| `WECOM_FORWARD_PLUS_GROUP_{N}_WECOM_ROBOT_SECRET` | ✅ (per group) | — | WeCom robot secret |
| `WECOM_FORWARD_PLUS_GROUP_{N}_DIFY_API_KEY` | ✅ (per group) | — | Dify app API key |
| `WECOM_FORWARD_PLUS_GROUP_{N}_SESSION_MAX_TOTAL` | — | global default | Per-group override of the session cap |
| `WECOM_FORWARD_PLUS_GROUP_{N}_SESSION_TTL_SECONDS` | — | global default | Per-group override of the conversation TTL |
| `WECOM_FORWARD_PLUS_CONFIG_SOURCE` | — | `env` | Where group configuration comes from: `env` (variables) or `database` (PostgreSQL; see [below](#runtime-group-configuration-postgresql)) |
| `WECOM_FORWARD_PLUS_DATABASE_URL` | ✅ when `CONFIG_SOURCE=database` | — | PostgreSQL DSN, e.g. `postgresql://user:password@localhost:5432/wecom` (never logged) |
| `WECOM_FORWARD_PLUS_DB_RELOAD_INTERVAL_SECONDS` | — | `30` | How often the running process re-reads group configuration from the database (minimum 5) |
| `WECOM_FORWARD_PLUS_ADMIN_UI` | — | `on` | Admin web UI on/off (database mode only; see [below](#admin-web-ui)) |
| `WECOM_FORWARD_PLUS_ADMIN_PASSWORD` | ✅ when `CONFIG_SOURCE=database` and UI on | — | Admin login password (never logged) |
| `WECOM_FORWARD_PLUS_ADMIN_BIND` | — | `127.0.0.1` | Address the admin UI listens on |
| `WECOM_FORWARD_PLUS_ADMIN_PORT` | — | `8080` | Port the admin UI listens on |
| `WECOM_FORWARD_PLUS_ADMIN_COOKIE_SECURE` | — | `off` | Add the `Secure` cookie flag (enable when TLS-terminated in front) |

Group indices start at 1 and must be contiguous. Each group requires `WECOM_ROBOT_ID`, `WECOM_ROBOT_SECRET`, and `DIFY_API_KEY` together; an incomplete group fails startup with exit code 1.

With `CONFIG_SOURCE=database` the `GROUP_` variables are not parsed at all — group configuration is read from the database (see below), so stale entries left in `.env` after a migration cannot break startup.

## Runtime group configuration (PostgreSQL)

Setting `WECOM_FORWARD_PLUS_CONFIG_SOURCE=database` moves group management into a PostgreSQL table. The two sources are strictly either/or: nothing is ever imported from `.env` into the database, and `GROUP_` variables are ignored while database mode is active.

The table is created automatically on startup:

```sql
CREATE TABLE IF NOT EXISTS groups (
    id                  SERIAL PRIMARY KEY,
    name                TEXT NOT NULL UNIQUE,
    wecom_robot_id      TEXT NOT NULL,
    wecom_robot_secret  TEXT NOT NULL,
    dify_api_key        TEXT NOT NULL,
    session_max_total   INTEGER,          -- NULL = global default
    session_ttl_seconds INTEGER,          -- NULL = global default
    enabled             BOOLEAN NOT NULL DEFAULT TRUE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

Add your first group directly in SQL:

```sql
INSERT INTO groups (name, wecom_robot_id, wecom_robot_secret, dify_api_key)
VALUES ('sales', 'your-robot-id', 'your-robot-secret', 'app-your-dify-key');
```

### Hot reload semantics

The running process re-reads the table every `DB_RELOAD_INTERVAL_SECONDS` (default 30) and converges without a restart:

| Change | Effect |
| --- | --- |
| New row (`enabled = true`) | A WeCom client for that group connects within one interval |
| `enabled = false` or `DELETE` | The group's client disconnects; its sessions are dropped |
| `wecom_robot_id` / `wecom_robot_secret` changed | That group's client restarts (its sessions are kept) |
| `dify_api_key` changed | Takes effect on the next message — no reconnect |
| `session_max_total` / `session_ttl_seconds` changed | Applied in place; live sessions survive |

- An empty table is a legal state: the process starts with 0 clients and hot-loads groups as they are inserted.
- If the database becomes unreachable, the process keeps running with the last-known configuration and retries every cycle.
- If the database is unreachable at startup, the process exits with code 1 (the Docker `restart` policy then retries).
- A group whose WeCom connection keeps failing is retried on every cycle until it connects or is removed.

Global settings (`DIFY_BASE_URL`, reset keywords, the session defaults the nullable columns fall back to) remain environment-only; only per-group values live in the database.

Secrets are stored in plaintext — the process environment already holds equivalent credentials, so application-side encryption would only relocate them. Restrict the database user to this table, keep the database on a private network, and rely on the never-log rule: neither the DSN nor any credential value is ever written to logs.

See [docs/connect-postgres.md](docs/connect-postgres.md) for setup, SQL examples, and Docker Compose instructions.

## Admin web UI

In database mode the process serves a built-in admin page (no Node/npm build step, plain aiohttp) for maintaining the groups table: create, edit, delete and enable/disable groups without writing SQL. It is on by default (`ADMIN_UI=off` disables it) and requires a login with `WECOM_FORWARD_PLUS_ADMIN_PASSWORD`.

Open `http://127.0.0.1:8080` (or your `ADMIN_BIND`/`ADMIN_PORT`) and log in:

- The table shows every group: name, enabled toggle, robot id, **masked** robot secret and Dify API key, session parameters (inherited values are shown in italics), and the last update time.
- "新增配置组" opens a form with all fields; session fields left empty inherit the global defaults.
- Editing an existing group leaves the secret fields blank — **blank means "keep the stored value"**; fill one in only to rotate it.
- Toggling 启用 or saving a change takes effect immediately (within one database round-trip), not on the 30-second reload cycle.

Security notes:

- Sessions are in-memory cookies (HttpOnly, SameSite=Strict), 8h expiry, lost on restart. Failed logins are rate-limited globally (5 failures per 10 minutes → 60s lockout).
- Mutating API calls with a body require a JSON content type, which (with SameSite=Strict) blocks cross-site form posts.
- The UI speaks plain HTTP: keep the default `127.0.0.1` binding and reach it through an SSH tunnel or a TLS-terminating reverse proxy; set `ADMIN_COOKIE_SECURE=on` behind TLS.
- A port conflict at startup fails the process (exit code 1) rather than running half-broken.

## Run

```bash
# From a source checkout
python -m src.main

# Or, after `pip install -e .`
wecom-forward-plus
```

Logs are written to stdout and rotated into `logs/app.log`.

## Package for deployment

To deploy on a server without cloning the repository, build a source archive with one command:

```bash
bash scripts/package.sh
# -> dist/wecom-forward-plus-<commit>.tar.gz
```

The archive is produced by `git archive` from the current `HEAD` commit, so it contains exactly the tracked files (source, tests, `docker/`, `docs/`) — never `.env`, `.venv`, `logs/`, or other local state. The short commit hash is appended to the file name; if the working tree has uncommitted changes, the script warns that they are not included.

Transfer and unpack on the server:

```bash
scp dist/wecom-forward-plus-<commit>.tar.gz user@server:~/
ssh user@server
tar -xzf wecom-forward-plus-<commit>.tar.gz   # unpacks into ./wecom-forward-plus/
```

Then continue with [Deploy with Docker](#deploy-with-docker).

## Deploy with Docker

All Docker files live in `docker/` (`Dockerfile`, `docker-compose.yml`, `Dockerfile.dockerignore`).

The compose file also starts an optional bundled PostgreSQL 16 (for `CONFIG_SOURCE=database`): it is reachable as `postgres:5432` from the app container and published to the host on port **5772**. The `groups` table is created automatically — `docker/postgres-init.sql` runs when the data volume is first initialized, and the application itself runs `CREATE TABLE IF NOT EXISTS` on every startup. See [docs/connect-postgres.md](docs/connect-postgres.md).

```bash
# 1. Provide configuration in the repository root (never committed)
cp .env.example .env
#   …fill in real values…

# 2. Build and start in the background (run from the docker/ directory)
cd docker
docker compose up -d --build

# 3. Tail logs
docker compose logs -f

# Stop / restart
docker compose down
docker compose restart
```

Configuration is injected via compose's `env_file` (`.env` in the repository root); the image itself contains no secrets. The host directory `logs/` (repository root) is mounted at `/app/logs` so the rotating log file (`logs/app.log`) persists across container recreations. The container restarts automatically (`unless-stopped`) unless explicitly stopped.

## Tests

```bash
pip install -e ".[dev]"
pytest
```

## License

[MIT](LICENSE)