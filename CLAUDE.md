# CLAUDE.md

Guidance for AI agents working in this repository. User-facing usage and full
configuration details live in `README.md` — do not duplicate them here.

## Project

`wecom-forward-plus` is a Python bridge between Enterprise WeChat bots and
Dify. One service process runs one WeCom long-connection client per configured
"group"; each group pairs one WeCom robot with one Dify app API key. A user's
WeCom account name is sent to Dify as `wx_<account>` to scope conversations.

## Module map (src/)

- `main.py` — entry point: load config, set up logging, start one `WeComClient` per group (env mode), or run the database reconcile loop + admin UI (database mode).
- `config.py` — parse/validate `WECOM_FORWARD_PLUS_*` env vars; defines `Config`/`GroupConfig`/`ConfigError` and `load_config()`.
- `config_store.py` — `GroupStore`, the mutable in-process group registry the message handler consults per message (hot reload seam).
- `db_config.py` — asyncpg `GroupRepository` (groups table read/write) plus pure `rows_to_desired`/`compute_group_diff`/`validate_group_fields`.
- `group_manager.py` — `GroupManager` (live WeCom client per group, apply desired state) and `reconcile_loop` (periodic/hot database reload).
- `admin_server.py` — authenticated aiohttp admin web UI over the groups table (login/rate limit/masked secrets); page assets live in `src/web/static/` (packaged via package-data).
- `session_manager.py` — `SessionManager`/`GroupPool`/`Session` (TTL, per-group cap, LRU eviction, reset, optional sweeper).
- `dify_client.py` — async `DifyClient` (streaming chat + file upload) plus pure `iter_sse_events`/`accumulate_stream`/`parse_upload_payload`; `DifyError`.
- `wecom_client.py` — thin wrapper around `wecom-aibot-python-sdk` (`WSClient`); downloads/decrypts media (image/file/mixed) into `Attachment`s.
- `message_handler.py` — routes message → session → Dify → reply; owns `handle()` and `handler_for(group_id)`; uploads attachments under the same `wx_<account>` user that references them.
- `attachment.py` — frozen `Attachment` dataclass (`filename`/`data`/`kind`) shared between wecom_client (producer) and message_handler (consumer).
- `constants.py` — `RESET_REPLY` / `ERROR_REPLY` plus the file-related replies and default file query.

## Run / test

- Run from source: `python -m src.main` (loads `.env` via python-dotenv). Config errors exit with code 1.
- Tests: `pytest` (`pytest-asyncio` auto mode; `pythonpath = ["."]` in pyproject).

## Constraints & rules

- **Secrets** — never read `.env`/`.env.local`/`secrets/`/`*.key`/`*.pem`; never git-add them; never embed real values in code or docs. Only placeholders may appear in `.env.example`. Never log `WECOM_ROBOT_ID`, `WECOM_ROBOT_SECRET`, `DIFY_API_KEY`, `WECOM_FORWARD_PLUS_DATABASE_URL` (contains DB credentials), `WECOM_FORWARD_PLUS_ADMIN_PASSWORD`, admin session tokens, or raw Dify request bodies.
- **Docs stay in sync** — every functional change must update `README.md` in the same commit. Keep `CLAUDE.md` limited to architecture / config / run / constraints; anything available in `README.md` belongs there, not here.
- **Commits** — English commit messages; one logical change per commit.
- **Tests before commit** — `pytest` must pass before committing.