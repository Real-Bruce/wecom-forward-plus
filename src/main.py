"""Entry point: load configuration and start one WeCom client per group."""

from __future__ import annotations

import asyncio
import logging
import logging.handlers
import sys
from pathlib import Path

from dotenv import load_dotenv

from .config import Config, ConfigError, load_config
from .dify_client import DifyClient
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


def main() -> None:
    load_dotenv()

    try:
        config = load_config()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        sys.exit(1)

    setup_logging()
    logger.info(
        "wecom-forward-plus starting with %d group(s)", len(config.groups)
    )

    try:
        asyncio.run(_run(config))
    except KeyboardInterrupt:
        logger.info("Interrupted; shutting down")


if __name__ == "__main__":
    main()