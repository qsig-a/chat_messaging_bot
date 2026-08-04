"""Entry point. Runs the chat adapter and the webhook server on one event loop."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

import httpx
import uvicorn

from .config import Config, ConfigError, load
from .delivery import Delivery
from .server import create_app
from .signalwire import SignalWire
from .store import Store

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
)
log = logging.getLogger("bridge")


def build_adapter(config: Config):
    if config.platform == "discord":
        from .chat.discord import DiscordAdapter

        return DiscordAdapter(config)
    if config.platform == "slack":
        from .chat.slack import SlackAdapter

        return SlackAdapter(config)
    raise ConfigError(f"no adapter for platform {config.platform!r}")


async def heartbeat_loop(http: httpx.AsyncClient, url: str) -> None:
    while True:
        try:
            await http.get(url, timeout=10)
        except Exception as exc:  # noqa: BLE001
            log.warning("heartbeat failed: %s", exc)
        await asyncio.sleep(300)


async def run(config: Config) -> None:
    http = httpx.AsyncClient()
    store = Store(config.db_path)
    store.prune()

    signalwire = SignalWire(config, http)
    adapter = build_adapter(config)
    queue: asyncio.Queue = asyncio.Queue()
    delivery = Delivery(config, adapter, store, signalwire)

    app = create_app(config, signalwire, store, delivery, queue, adapter)
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=config.bind_host,
            port=config.bind_port,
            log_level="warning",
            access_log=False,
        )
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # e.g. Windows
            pass

    tasks: list[asyncio.Task] = []

    log.info("platform=%s", config.platform)
    tasks.append(asyncio.create_task(server.serve()))
    log.info(
        "webhook listening on %s:%s (public: %s)",
        config.bind_host,
        config.bind_port,
        config.public_base_url,
    )
    tasks.append(asyncio.create_task(delivery.run_worker(queue)))
    if config.heartbeat_url:
        tasks.append(asyncio.create_task(heartbeat_loop(http, config.heartbeat_url)))

    async def start_chat() -> None:
        await adapter.start(delivery.handle_outbound)

    async def check_when_ready() -> None:
        # adapter.start blocks for the lifetime of the gateway connection, so the
        # access check waits on readiness rather than on start() returning.
        for _ in range(60):
            if adapter.is_ready():
                await adapter.check_access()
                return
            await asyncio.sleep(1)
        log.warning("adapter never became ready; skipping the startup access check")

    tasks.append(asyncio.create_task(start_chat()))
    tasks.append(asyncio.create_task(check_when_ready()))

    await stop.wait()
    log.info("shutdown signal received, closing")
    server.should_exit = True
    await adapter.close()
    await http.aclose()
    store.close()


def main() -> None:
    try:
        config = load()
    except ConfigError as exc:
        sys.exit(str(exc))
    try:
        asyncio.run(run(config))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
