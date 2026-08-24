"""Entrypoint for the Govee Artnet LAN bridge."""

from __future__ import annotations

import asyncio
import contextlib
import signal
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, List, Mapping, Optional

from .config import Config, load_config
from .db import apply_migrations
from .devices import DeviceStore
from .api import ApiService
from .artnet import ArtNetService
from .discovery import DiscoveryService
from .health import BackoffPolicy, HealthMonitor
from .poller import DevicePollerService
from .udp_protocol import GoveeProtocolService
from .sender import DeviceSenderService
from .logging import configure_logging, get_logger


@dataclass
class RunningServices:
    """Track running service instances for state capture."""

    protocol: Optional[GoveeProtocolService] = None
    discovery: Optional[DiscoveryService] = None
    dmx_mapping: Optional[Any] = None  # DmxMappingService (imported in loop to avoid circular)
    artnet: Optional[ArtNetService] = None
    sender: Optional[DeviceSenderService] = None
    poller: Optional[DevicePollerService] = None
    api: Optional[ApiService] = None


async def _protocol_loop(
    stop_event: asyncio.Event,
    config: Config,
    services: Optional[RunningServices] = None,
) -> None:
    logger = get_logger("artnet.protocol")
    service = GoveeProtocolService(config)
    if services is not None:
        services.protocol = service
    await service.start()
    logger.info("Protocol service started")
    try:
        await stop_event.wait()
    finally:
        await service.stop()
        if services is not None:
            services.protocol = None
        logger.info("Protocol service stopped")


async def _discovery_loop(
    stop_event: asyncio.Event,
    config: Config,
    store: DeviceStore,
    health: HealthMonitor,
    protocol: Optional[GoveeProtocolService] = None,
    services: Optional[RunningServices] = None,
) -> None:
    logger = get_logger("artnet.discovery")
    backoff = BackoffPolicy(
        base=config.device_backoff_base,
        factor=config.device_backoff_factor,
        maximum=config.device_backoff_max,
    )
    proto_inst = protocol.protocol if protocol else None
    service = DiscoveryService(config, store, protocol=proto_inst)
    if services is not None:
        services.discovery = service
    start_failures = 0
    while not stop_event.is_set():
        allowed, remaining = await health.allow_attempt("discovery")
        if not allowed:
            logger.warning(
                "Discovery temporarily suppressed after repeated failures",
                extra={"cooldown_seconds": round(remaining, 2)},
            )
            await _wait_or_stop(stop_event, remaining)
            continue
        try:
            await service.start()
            await health.record_success("discovery")
            start_failures = 0
            break
        except Exception as exc:
            start_failures += 1
            logger.exception("Discovery service failed to start")
            await health.record_failure("discovery", exc)
            await _wait_or_stop(stop_event, backoff.delay(start_failures))
    else:
        return

    logger.info(
        "Discovery loop starting",
        extra={"interval": config.discovery_interval},
    )
    failures = 0
    try:
        while not stop_event.is_set():
            logger.debug("Running discovery cycle")
            try:
                await service.run_cycle()
                await health.record_success("discovery")
                failures = 0
            except Exception as exc:
                logger.exception("Discovery cycle failed")
                failures += 1
                await health.record_failure("discovery", exc)
                await _wait_or_stop(stop_event, backoff.delay(failures))
                continue
            try:
                await asyncio.wait_for(
                    stop_event.wait(), timeout=config.discovery_interval
                )
            except asyncio.TimeoutError:
                continue
    except asyncio.CancelledError:
        logger.info("Discovery loop cancelled")
        raise
    finally:
        await service.stop()
        if services is not None:
            services.discovery = None
        logger.info("Discovery loop stopped")


async def _rate_limit_monitor(stop_event: asyncio.Event, config: Config) -> None:
    logger = get_logger("artnet.rate_limit")
    logger.info(
        "Rate limit monitor starting",
        extra={
            "per_second": config.rate_limit_per_second,
            "burst": config.rate_limit_burst,
        },
    )
    try:
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=5)
            except asyncio.TimeoutError:
                logger.debug("Rate limiter heartbeat")
                continue
    except asyncio.CancelledError:
        logger.info("Rate limit monitor cancelled")
        raise
    finally:
        logger.info("Rate limit monitor stopped")


async def _dmx_mapping_loop(
    stop_event: asyncio.Event,
    config: Config,
    store: DeviceStore,
    health: HealthMonitor,
    services: Optional[RunningServices] = None,
    dmx_state: Optional[Mapping[str, Mapping[str, Any]]] = None,
    event_bus: Optional[Any] = None,
) -> None:
    """DMX mapping service loop - handles protocol-agnostic DMX→Device mapping."""
    logger = get_logger("dmx.mapping")

    # Import here to avoid circular dependency
    from .dmx import DmxMappingService

    service = DmxMappingService(config, store, initial_last_payloads=dmx_state, event_bus=event_bus)
    if services is not None:
        services.dmx_mapping = service

    backoff = BackoffPolicy(
        base=config.device_backoff_base,
        factor=config.device_backoff_factor,
        maximum=config.device_backoff_max,
    )
    failures = 0

    while not stop_event.is_set():
        allowed, remaining = await health.allow_attempt("dmx_mapping")
        if not allowed:
            logger.warning(
                "DMX mapping service suppressed after repeated failures",
                extra={"cooldown_seconds": round(remaining, 2)},
            )
            await _wait_or_stop(stop_event, remaining)
            continue

        try:
            await service.start()
            await health.record_success("dmx_mapping")
        except Exception as exc:
            failures += 1
            logger.exception("DMX mapping service failed to start; will retry")
            await health.record_failure("dmx_mapping", exc)
            await _wait_or_stop(stop_event, backoff.delay(failures))
            continue

        failures = 0
        logger.info("DMX mapping service running")

        # Service runs until stop requested
        try:
            await stop_event.wait()
        except asyncio.CancelledError:
            pass

        break

    await service.stop()
    if services is not None:
        services.dmx_mapping = None
    logger.info("DMX mapping loop stopped")


async def _artnet_loop(
    stop_event: asyncio.Event,
    config: Config,
    health: HealthMonitor,
    services: Optional[RunningServices] = None,
) -> None:
    """ArtNet input protocol loop - receives ArtNet packets and forwards to DMX mapper."""
    logger = get_logger("artnet.input")

    # Wait for DMX mapping service to be available
    while not stop_event.is_set():
        if services and services.dmx_mapping:
            break
        logger.debug("Waiting for DMX mapping service to start...")
        await asyncio.sleep(0.1)

    if stop_event.is_set():
        logger.info("ArtNet loop cancelled before start")
        return

    dmx_mapper = services.dmx_mapping if services else None
    if not dmx_mapper:
        logger.error("DMX mapping service not available, ArtNet cannot start")
        return

    service = ArtNetService(config, dmx_mapper=dmx_mapper)
    if services is not None:
        services.artnet = service

    backoff = BackoffPolicy(
        base=config.device_backoff_base,
        factor=config.device_backoff_factor,
        maximum=config.device_backoff_max,
    )
    failures = 0

    while not stop_event.is_set():
        allowed, remaining = await health.allow_attempt("artnet")
        if not allowed:
            logger.warning(
                "ArtNet input suppressed after repeated failures",
                extra={"cooldown_seconds": round(remaining, 2)},
            )
            await _wait_or_stop(stop_event, remaining)
            continue

        try:
            await service.start()
            await health.record_success("artnet")
        except Exception as exc:
            failures += 1
            logger.exception("ArtNet input failed to start; will retry")
            await health.record_failure("artnet", exc)
            await _wait_or_stop(stop_event, backoff.delay(failures))
            continue

        failures = 0
        wait_tasks = [
            asyncio.create_task(stop_event.wait()),
            asyncio.create_task(service.error_event.wait()),
        ]
        done, pending = await asyncio.wait(wait_tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()

        if stop_event.is_set():
            break

        logger.warning("ArtNet input restarting after error")
        await health.record_failure("artnet")
        failures += 1
        await _wait_or_stop(stop_event, backoff.delay(failures))

    await service.stop()
    if services is not None:
        services.artnet = None
    logger.info("ArtNet loop stopped")


async def _sacn_loop(
    stop_event: asyncio.Event,
    config: Config,
    health: HealthMonitor,
    services: Optional[RunningServices] = None,
) -> None:
    """sACN/E1.31 input protocol loop - receives sACN packets and forwards to DMX mapper."""
    logger = get_logger("sacn.input")

    # Wait for DMX mapping service to be available
    while not stop_event.is_set():
        if services and services.dmx_mapping:
            break
        logger.debug("Waiting for DMX mapping service to start...")
        await asyncio.sleep(0.1)

    if stop_event.is_set():
        logger.info("sACN loop cancelled before start")
        return

    dmx_mapper = services.dmx_mapping if services else None
    if not dmx_mapper:
        logger.error("DMX mapping service not available, sACN cannot start")
        return

    # Import here to avoid importing if not enabled
    from .sacn import SacnService

    service = SacnService(config, dmx_mapper=dmx_mapper)
    if services is not None:
        services.sacn = service  # type: ignore[attr-defined]

    backoff = BackoffPolicy(
        base=config.device_backoff_base,
        factor=config.device_backoff_factor,
        maximum=config.device_backoff_max,
    )
    failures = 0

    while not stop_event.is_set():
        allowed, remaining = await health.allow_attempt("sacn")
        if not allowed:
            logger.warning(
                "sACN input suppressed after repeated failures",
                extra={"cooldown_seconds": round(remaining, 2)},
            )
            await _wait_or_stop(stop_event, remaining)
            continue

        try:
            await service.start()
            await health.record_success("sacn")
        except Exception as exc:
            failures += 1
            logger.exception("sACN input failed to start; will retry")
            await health.record_failure("sacn", exc)
            await _wait_or_stop(stop_event, backoff.delay(failures))
            continue

        failures = 0
        wait_tasks = [
            asyncio.create_task(stop_event.wait()),
            asyncio.create_task(service.error_event.wait()),
        ]
        done, pending = await asyncio.wait(wait_tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()

        if stop_event.is_set():
            break

        logger.warning("sACN input restarting after error")
        await health.record_failure("sacn")
        failures += 1
        await _wait_or_stop(stop_event, backoff.delay(failures))

    await service.stop()
    if services is not None:
        services.sacn = None  # type: ignore[attr-defined]
    logger.info("sACN loop stopped")


async def _sender_loop(
    stop_event: asyncio.Event,
    config: Config,
    store: DeviceStore,
    health: HealthMonitor,
    services: Optional[RunningServices] = None,
) -> None:
    logger = get_logger("artnet.sender")
    service = DeviceSenderService(config, store, health=health)
    if services is not None:
        services.sender = service
    await service.start()
    try:
        await stop_event.wait()
    finally:
        await service.stop()
        logger.info("Sender loop stopped")
        if services is not None:
            services.sender = None


async def _api_loop(
    stop_event: asyncio.Event,
    config: Config,
    store: DeviceStore,
    health: HealthMonitor,
    services: Optional[RunningServices] = None,
    reload_callback: Optional[Callable[[], Awaitable[None]]] = None,
    log_buffer: Optional[Any] = None,
    event_bus: Optional[Any] = None,
) -> None:
    logger = get_logger("artnet.api")
    service = ApiService(
        config,
        store,
        health=health,
        reload_callback=reload_callback,
        log_buffer=log_buffer,
        event_bus=event_bus,
        sender_provider=(lambda: services.sender) if services is not None else None,
    )
    if services is not None:
        services.api = service
    await service.start()
    try:
        await stop_event.wait()
    finally:
        await service.stop()
        logger.info("API loop stopped")
        if services is not None:
            services.api = None


async def _poller_loop(
    stop_event: asyncio.Event,
    config: Config,
    store: DeviceStore,
    health: HealthMonitor,
    protocol: Optional[GoveeProtocolService] = None,
    services: Optional[RunningServices] = None,
) -> None:
    logger = get_logger("artnet.poller")
    proto_inst = protocol.protocol if protocol else None
    service = DevicePollerService(config, store, protocol=proto_inst, health=health)
    if services is not None:
        services.poller = service
    await service.start()
    try:
        await stop_event.wait()
    finally:
        await service.stop()
        if services is not None:
            services.poller = None
        logger.info("Poller loop stopped")


async def _stop_services(
    stop_event: asyncio.Event, tasks: Iterable[asyncio.Task[None]], logger: logging.Logger
) -> None:
    stop_event.set()
    done, pending = await asyncio.wait(set(tasks), timeout=5)
    for task in pending:
        task.cancel()
    if pending:
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.gather(*pending)
    for task in done:
        with contextlib.suppress(asyncio.CancelledError):
            await task


def _load_reloaded_config(
    cli_args: Optional[Iterable[str]],
    logger: logging.Logger,
    current_config: Config,
) -> Optional[Config]:
    try:
        new_config = load_config(cli_args)
    except Exception:
        logger.exception("Failed to reload configuration; keeping existing settings")
        return None

    if new_config.db_path != current_config.db_path:
        logger.error(
            "Config reload rejected because db_path changed; restart required",
            extra={"current_db_path": str(current_config.db_path), "new_db_path": str(new_config.db_path)},
        )
        return None
    return new_config


async def _run_async(config: Config, cli_args: Optional[Iterable[str]] = None) -> None:
    logger = get_logger("govee")
    shutdown_event = asyncio.Event()
    reload_event = asyncio.Event()

    # Initialize log buffer and event_bus BEFORE creating store
    log_buffer = None
    event_bus = None
    if config.log_buffer_enabled:
        from .log_buffer import LogBuffer
        log_buffer = LogBuffer(max_size=config.log_buffer_size)
        logger.info("Log buffer enabled", extra={"size": config.log_buffer_size})

    if config.event_bus_enabled:
        from .events import EventBus
        event_bus = EventBus()
        logger.info("Event bus enabled")

    store = DeviceStore(config.db_path, event_bus=event_bus)
    await store.start()
    await store.refresh_metrics()

    # Reconfigure logging with log buffer
    if log_buffer is not None:
        configure_logging(config, log_buffer)
        logger = get_logger("govee")  # Get logger again after reconfiguration

    def _request_shutdown(sig: Optional[int] = None) -> None:
        if not shutdown_event.is_set():
            logger.warning("Shutdown requested", extra={"signal": sig})
            shutdown_event.set()

    def _request_reload(sig: Optional[int] = None) -> None:
        logger.warning("Config reload requested", extra={"signal": sig})
        reload_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, _request_shutdown, sig.name)
    with contextlib.suppress(NotImplementedError):
        loop.add_signal_handler(signal.SIGHUP, _request_reload, "SIGHUP")

    dmx_state: Optional[Mapping[str, Mapping[str, Any]]] = None
    current_config = config

    while not shutdown_event.is_set():
        await store.sync_manual_devices(current_config.manual_devices)

        # Build subsystem list based on enabled protocols
        subsystems = ["discovery", "dmx_mapping", "sender", "api", "poller"]
        if current_config.artnet_enabled:
            subsystems.append("artnet")
        if current_config.sacn_enabled:
            subsystems.append("sacn")

        health = HealthMonitor(
            tuple(subsystems),
            failure_threshold=current_config.subsystem_failure_threshold,
            cooldown_seconds=current_config.subsystem_failure_cooldown,
            event_bus=event_bus,
        )
        services = RunningServices()
        stop_event = asyncio.Event()

        # Start protocol service first (provides shared UDP listener for devices)
        protocol_task = asyncio.create_task(_protocol_loop(stop_event, current_config, services))
        # Wait for protocol to be ready using event-based signaling
        while not services.protocol:
            await asyncio.sleep(0.01)  # Wait for service object to be set
        await services.protocol.wait_ready(timeout=5.0)
        protocol_service = services.protocol

        # Build task list (conditionally add input protocols)
        tasks: List[asyncio.Task[None]] = [
            protocol_task,
            asyncio.create_task(_discovery_loop(stop_event, current_config, store, health, protocol_service, services)),
            asyncio.create_task(_rate_limit_monitor(stop_event, current_config)),
            asyncio.create_task(_dmx_mapping_loop(stop_event, current_config, store, health, services, dmx_state, event_bus)),
        ]

        # Conditionally start input protocol services
        if current_config.artnet_enabled:
            tasks.append(asyncio.create_task(_artnet_loop(stop_event, current_config, health, services)))
        if current_config.sacn_enabled:
            tasks.append(asyncio.create_task(_sacn_loop(stop_event, current_config, health, services)))

        # Add remaining services
        tasks.extend([
            asyncio.create_task(_sender_loop(stop_event, current_config, store, health, services)),
            asyncio.create_task(_poller_loop(stop_event, current_config, store, health, protocol_service, services)),
            asyncio.create_task(_api_loop(stop_event, current_config, store, health, services, _request_reload, log_buffer, event_bus)),
        ])

        # Build logging info
        input_protocols = []
        if current_config.artnet_enabled:
            input_protocols.append(f"ArtNet:{current_config.artnet_port}")
        if current_config.sacn_enabled:
            input_protocols.append(f"sACN:{current_config.sacn_port}")

        logger.info(
            "Bridge services started",
            extra={
                "input_protocols": ", ".join(input_protocols) if input_protocols else "none",
                "api_port": current_config.api_port,
                "db_path": str(current_config.db_path),
                "dry_run": current_config.dry_run,
            },
        )

        while True:
            wait_tasks = [
                asyncio.create_task(shutdown_event.wait()),
                asyncio.create_task(reload_event.wait()),
            ]
            done, pending = await asyncio.wait(wait_tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            for task in done:
                with contextlib.suppress(asyncio.CancelledError):
                    await task

            if shutdown_event.is_set():
                await _stop_services(stop_event, tasks, logger)
                break

            reload_event.clear()
            new_config = _load_reloaded_config(cli_args, logger, current_config)
            if new_config is None:
                logger.warning("Continuing with existing configuration after failed reload")
                continue

            dmx_mapping_service = services.dmx_mapping
            await _stop_services(stop_event, tasks, logger)
            if dmx_mapping_service is not None:
                dmx_state = dmx_mapping_service.snapshot_last_payloads()
            current_config = new_config
            configure_logging(current_config, log_buffer)
            logger = get_logger("govee")  # Get logger again after reconfiguration
            logger.info("Configuration reloaded", extra={"config": current_config.logging_dict()})
            break

        if shutdown_event.is_set():
            break

    await store.stop()
    logger.info("Bridge shutdown complete")


async def _wait_or_stop(stop_event: asyncio.Event, delay: float) -> None:
    if delay <= 0:
        return
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=delay)
    except asyncio.TimeoutError:
        return


def run(cli_args: Optional[Iterable[str]] = None) -> None:
    """CLI entrypoint used by setuptools."""

    config = load_config(cli_args)
    configure_logging(config)
    logger = get_logger("govee")
    logger.info("Loaded configuration", extra={"config": config.logging_dict()})

    apply_migrations(config.db_path)
    if config.migrate_only:
        logger.info("Migrations complete; exiting per configuration.")
        return
    try:
        asyncio.run(_run_async(config, cli_args))
    except KeyboardInterrupt:
        logger.warning("Interrupted by user")


if __name__ == "__main__":
    run()
