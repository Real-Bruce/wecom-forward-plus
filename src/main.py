"""Entry point: load configuration and start one WeCom client per group."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import logging.handlers
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from .config import PREFIX, SOURCE_DATABASE, Config, ConfigError, load_config
from .config_store import GroupStore
from .dify_client import DifyClient
from .group_manager import GroupManager, reconcile_loop
from .db_config import GroupDefaults, GroupRepository, rows_to_desired
from .admin_server import AdminServer
from .message_handler import MessageHandler
from .session_manager import SessionManager
from .wecom_client import WeComClient

logger = logging.getLogger(__name__)


def setup_logging(log_dir: Path = Path("logs")) -> None:
    """Configure stdout + rotating-file logging for the whole process."""
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(formatter)
    root.addHandler(stream)

    log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / "app.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    # Keep noisy third-party logs out of the way.
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    logging.getLogger("pyee").setLevel(logging.WARNING)


async def _run(config: Config) -> None:
    if config.config_source == SOURCE_DATABASE:
        await _run_database(config)
    else:
        await _run_env(config)


async def _run_env(config: Config) -> None:
    dify_client = DifyClient(config.dify_base_url)
    try:
        session_manager = SessionManager(config)
        message_handler = MessageHandler(config, session_manager, dify_client)

        clients = [
            WeComClient(
                group_id=group.name,
                robot_id=group.wecom_robot_id,
                robot_secret=group.wecom_robot_secret,
                handler=message_handler.handler_for(group.name),
            )
            for group in config.groups
        ]

        await asyncio.gather(*(client.connect() for client in clients))
        logger.info("Started %d WeCom client(s)", len(clients))

        # Optional background sweeper to proactively drop expired sessions.
        session_manager.start_sweeper(
            interval=max(30.0, config.session_ttl_seconds / 2)
        )

        stop_event = asyncio.Event()
        try:
            await stop_event.wait()
        finally:
            for client in clients:
                client.disconnect()
            await dify_client.close()
    except Exception:
        await dify_client.close()
        raise


def _has_env_group_vars() -> bool:
    return any(key.startswith(f"{PREFIX}GROUP_") for key in os.environ)


async def _run_database(config: Config) -> None:
    dify_client = DifyClient(config.dify_base_url)
    repo = GroupRepository(config.database_url)
    try:
        logger.info("Connecting to group configuration database")
        await repo.connect()

        if _has_env_group_vars():
            logger.warning(
                "Database config source is active; "
                "WECOM_FORWARD_PLUS_GROUP_* environment variables are ignored"
            )

        defaults = GroupDefaults(
            session_ttl_seconds=config.session_ttl_seconds,
            session_max_total=config.session_max_total,
        )
        desired = rows_to_desired(await repo.list_rows(), defaults)
        if not desired:
            logger.warning(
                "No enabled groups configured in the database yet; "
                "groups inserted later are hot-loaded automatically"
            )

        store = GroupStore(desired)
        session_manager = SessionManager(config, list(desired.values()))
        message_handler = MessageHandler(config, session_manager, dify_client, store)
        manager = GroupManager(
            handler=message_handler,
            sessions=session_manager,
            store=store,
            global_ttl=config.session_ttl_seconds,
            client_factory=WeComClient,
        )
        await manager.apply(desired)
        logger.info("Started %d WeCom client(s)", len(manager.live_group_names))

        # Optional background sweeper to proactively drop expired sessions.
        session_manager.start_sweeper(
            interval=max(30.0, config.session_ttl_seconds / 2)
        )

        stop_event = asyncio.Event()
        reload_event = asyncio.Event()
        reload_task = asyncio.create_task(
            reconcile_loop(
                repo,
                manager,
                defaults,
                config.db_reload_interval_seconds,
                stop_event,
                reload_event,
            )
        )

        admin = None
        if config.admin_ui_enabled:
            admin = AdminServer(
                repo=repo,
                password=config.admin_password,
                request_reload=reload_event.set,
                defaults=defaults,
                dify_base_url=config.dify_base_url,
                bind=config.admin_bind,
                port=config.admin_port,
                cookie_secure=config.admin_cookie_secure,
            )
            try:
                await admin.start()
            except OSError:
                logger.error(
                    "Admin UI cannot listen on %s:%d (port in use?)",
                    config.admin_bind,
                    config.admin_port,
                )
                raise
            logger.info(
                "Admin UI listening on http://%s:%d", config.admin_bind, config.admin_port
            )

        try:
            await stop_event.wait()
        finally:
            reload_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reload_task
            if admin is not None:
                await admin.stop()
            await manager.shutdown()
            await repo.close()
            await dify_client.close()
    except Exception:
        await dify_client.close()
        with contextlib.suppress(Exception):
            await repo.close()
        raise


def main() -> None:
    load_dotenv()

    try:
        config = load_config()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        sys.exit(1)

    setup_logging()
    if config.config_source == SOURCE_DATABASE:
        logger.info("wecom-forward-plus starting (config source: database)")
    else:
        logger.info(
            "wecom-forward-plus starting with %d group(s)", len(config.groups)
        )

    try:
        asyncio.run(_run(config))
    except KeyboardInterrupt:
        logger.info("Interrupted; shutting down")
    except SystemExit:
        raise
    except Exception as exc:
        # Startup failures (database unreachable, admin port in use, ...):
        # report cleanly and let the Docker restart policy retry.
        logger.error("Startup failed: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()