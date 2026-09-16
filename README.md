# wecom-forward-plus

A Python bridge between Enterprise WeChat (企业微信) robots and [Dify](https://dify.ai) apps. It connects to WeCom robots over their WebSocket long-connection channel, forwards incoming user messages to a Dify chat application, and sends the streaming reply back to WeCom.

Message path:

```
Dify  <=>  wecom-forward-plus  <=>  企业微信机器人
```

## Features

- **Multiple groups** — each group pairs one WeCom robot with one Dify app API key and can be configured independently.
- **WeCom account name as `user`** — the sender's account name is sent to Dify as `wx_<account>` (e.g. `zhangsan` → `wx_zhangsan`), so each user keeps their own conversation history.
- **Session management** — per-group session pools with a TTL (default 5 minutes of inactivity), a per-group cap (default 200), least-recently-used eviction, and keyword-triggered resets.
- **Streaming Dify replies** — uses Dify's `streaming` response mode and accumulates the full answer before replying.
- **Resilience** — automatic WeCom reconnection (exponential backoff), reply retries, and sanitized error handling (Dify failures return a friendly message instead of crashing).

## Architecture

```
src/
├── main.py               Entry point: load config, logging, start one client per group
├── config.py             Parse + validate WECOM_FORWARD_PLUS_* environment variables
├── session_manager.py    Per-group session pools (TTL + cap + LRU eviction + reset)
├── dify_client.py        Async Dify chat-messages client (streaming) + SSE parsing
├── wecom_client.py       Wrapper around wecom-aibot-python-sdk (long connection)
├── message_handler.py    Routes WeCom message -> session -> Dify -> reply text
└── constants.py          Fixed user-facing reply strings
```

Flow of a single message:

1. WeCom delivers a frame; `wecom_client` extracts the sender account and text content.
2. `message_handler` builds `dify_user = "wx_" + from_user`.
3. If the text is a reset keyword, the user's session is reset and the fixed reply is returned (Dify is **not** called).
4. Otherwise the user's session is fetched/created and `POST /chat-messages` is called with `response_mode="streaming"`.
5. The streamed answer is accumulated, the returned `conversation_id` is stored, and the answer is sent back to WeCom.

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

Group indices start at 1 and must be contiguous. Each group requires `WECOM_ROBOT_ID`, `WECOM_ROBOT_SECRET`, and `DIFY_API_KEY` together; an incomplete group fails startup with exit code 1.

## Run

```bash
# From a source checkout
python -m src.main

# Or, after `pip install -e .`
wecom-forward-plus
```

Logs are written to stdout and rotated into `logs/app.log`.

## Deploy with Docker

All Docker files live in `docker/` (`Dockerfile`, `docker-compose.yml`, `Dockerfile.dockerignore`).

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

MIT