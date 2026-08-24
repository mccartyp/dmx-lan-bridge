"""Per-device send queues and transport handling."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import socket
import contextlib
import time
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional

from .config import Config
from .devices import DeviceInfo, DeviceStateUpdate, DeviceStore, PendingState
from .health import BackoffPolicy, HealthMonitor
from .logging import get_logger
from .metrics import (
    observe_send_duration,
    record_rate_limit_wait,
    record_send_result,
    set_rate_limit_tokens,
)


@dataclass(frozen=True)
class DeviceTarget:
    """Resolved transport target for a device."""

    id: str
    ip: str
    protocol: str
    port: int
    transport: str
    capabilities: Any


def _coerce_transport(capabilities: Any, default: str) -> str:
    if isinstance(capabilities, Mapping):
        value = capabilities.get("transport") or capabilities.get("protocol")
        if isinstance(value, str):
            lowered = value.lower()
            if lowered in {"tcp", "udp"}:
                return lowered
    return default


def _coerce_port(capabilities: Any, default: int) -> int:
    if isinstance(capabilities, Mapping):
        for key in ("port", "control_port", "device_port"):
            if key in capabilities:
                try:
                    parsed = int(capabilities[key])
                    if parsed > 0:
                        return parsed
                except (TypeError, ValueError):
                    continue
    return default


def _derive_target(config: Config, device: DeviceInfo) -> Optional[DeviceTarget]:
    if not device.ip:
        return None

    # Get protocol-specific defaults
    from .protocol import get_protocol_handler
    handler = get_protocol_handler(device.protocol)

    # Use protocol handler defaults, but allow capability overrides
    transport = _coerce_transport(device.capabilities, handler.get_default_transport())
    port = _coerce_port(device.capabilities, handler.get_default_port())

    return DeviceTarget(
        id=device.id,
        ip=device.ip,
        protocol=device.protocol,
        port=port,
        transport=transport,
        capabilities=device.capabilities,
    )


class DeviceSenderService:
    """Background service draining device queues and handling retries."""

    def __init__(
        self, config: Config, store: DeviceStore, health: Optional[HealthMonitor] = None
    ) -> None:
        self.config = config
        self.store = store
        self.logger = get_logger("artnet.sender")
        self._stop_event = asyncio.Event()
        self._wake_event = asyncio.Event()
        self._poll_task: Optional[asyncio.Task[None]] = None
        self._device_tasks: Dict[str, asyncio.Task[None]] = {}
        self._dry_run = config.dry_run
        self._health = health or HealthMonitor(
            ("sender",),
            failure_threshold=config.subsystem_failure_threshold,
            cooldown_seconds=config.subsystem_failure_cooldown,
        )
        self._backoff = BackoffPolicy(
            base=config.device_backoff_base,
            factor=config.device_backoff_factor,
            maximum=config.device_backoff_max,
        )
        self._rate_tokens = float(config.rate_limit_burst)
        self._rate_last_refill = time.perf_counter()
        self._rate_lock = asyncio.Lock()
        set_rate_limit_tokens(self._rate_tokens)
        self._enqueued = 0
        self._dequeued = 0
        self._sent = 0
        self._failed = 0

    async def start(self) -> None:
        if self.is_running():
            return
        self._stop_event.clear()
        self._wake_event.clear()
        await self.store.refresh_metrics()
        self._rate_tokens = float(self.config.rate_limit_burst)
        self._rate_last_refill = time.perf_counter()
        set_rate_limit_tokens(self._rate_tokens)
        self._poll_task = asyncio.create_task(self._poll_loop())
        self._poll_task.add_done_callback(self._poll_task_done)
        if self._dry_run:
            self.logger.info("Device sender service started in dry-run mode; payloads will not be sent.")
        else:
            self.logger.info("Device sender service started")

    def _poll_task_done(self, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            self.logger.error("Sender worker failed", exc_info=(type(exc), exc, exc.__traceback__))
            asyncio.create_task(self._health.record_failure("sender", exc))

    def is_running(self) -> bool:
        return self._poll_task is not None and not self._poll_task.done()

    async def health_details(self) -> Mapping[str, Any]:
        monitor_state = dict((await self._health.snapshot()).get("sender", {}))
        reason = None
        if self._poll_task is None:
            reason = "sender worker was never started"
        elif self._poll_task.cancelled():
            reason = "sender worker was cancelled"
        elif self._poll_task.done():
            exc = self._poll_task.exception()
            reason = str(exc) if exc else "sender worker stopped"
        try:
            queue_depth = await self.store.pending_state_count()
        except Exception as exc:
            queue_depth = None
            reason = reason or f"queue inspection failed: {exc}"
        monitor_status = str(monitor_state.get("status", "ok"))
        details = {
            **monitor_state,
            "status": monitor_status if reason is None else "failed",
            "reason": reason,
            "queue_depth": queue_depth,
            "enqueued": self._enqueued,
            "dequeued": self._dequeued,
            "sent": self._sent,
            "failed": self._failed,
            "active_device_workers": sum(not task.done() for task in self._device_tasks.values()),
            "queue_name": "device-state-db",
            "queue_id": id(self.store),
        }
        if reason is None and monitor_state.get("last_error"):
            details["reason"] = monitor_state["last_error"]
        return details

    async def enqueue(self, update: DeviceStateUpdate) -> None:
        if not self.is_running():
            raise RuntimeError("sender worker is not running")
        device = await self.store.device_info(update.device_id)
        if device is None:
            raise ValueError(f"device {update.device_id!r} is unavailable")
        self.logger.debug(
            "Command about to be queued",
            extra={
                "device_id": update.device_id,
                "protocol": device.protocol,
                "command_type": update.context_id,
                "payload": update.payload,
                "queue_name": "device-state-db",
                "queue_id": id(self.store),
            },
        )
        await self.store.enqueue_state(update)
        self._enqueued += 1
        self._wake_event.set()
        self.logger.debug(
            "Command successfully queued",
            extra={
                "device_id": update.device_id,
                "protocol": device.protocol,
                "queue_depth": await self.store.pending_state_count(),
                "queue_name": "device-state-db",
                "queue_id": id(self.store),
            },
        )

    async def stop(self) -> None:
        self._stop_event.set()
        tasks = list(self._device_tasks.values())
        if self._poll_task:
            tasks.append(self._poll_task)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._poll_task = None
        self._device_tasks.clear()
        self.logger.info("Device sender service stopped")

    async def _poll_loop(self) -> None:
        while not self._stop_event.is_set():
            self._wake_event.clear()
            await self._ensure_workers()
            try:
                await asyncio.wait_for(self._wake_event.wait(), timeout=self.config.device_queue_poll_interval)
            except asyncio.TimeoutError:
                continue

    async def _ensure_workers(self) -> None:
        device_ids = await self.store.pending_device_ids()
        for device_id in device_ids:
            if device_id not in self._device_tasks:
                self._device_tasks[device_id] = asyncio.create_task(
                    self._run_device_queue(device_id)
                )
        done_ids = [device_id for device_id, task in self._device_tasks.items() if task.done()]
        for device_id in done_ids:
            task = self._device_tasks.pop(device_id)
            if task.cancelled():
                continue
            if task.exception():
                exc = task.exception()
                self.logger.error(
                    "Device send task failed",
                    extra={"device_id": device_id},
                    exc_info=(type(exc), exc, exc.__traceback__),
                )
                self._failed += 1
                await self._health.record_failure("sender", exc)

    async def _run_device_queue(self, device_id: str) -> None:
        rate_delay = 0.0
        if self.config.device_max_send_rate > 0:
            rate_delay = 1.0 / self.config.device_max_send_rate
        while not self._stop_event.is_set():
            self.logger.debug(
                "Sender waiting for command",
                extra={"device_id": device_id, "queue_name": "device-state-db", "queue_id": id(self.store)},
            )
            state = await self.store.next_state(device_id)
            if state is None:
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(), timeout=self.config.device_idle_wait
                    )
                except asyncio.TimeoutError:
                    continue
                continue
            self._dequeued += 1
            self.logger.debug(
                "Sender dequeued command",
                extra={"device_id": device_id, "context_id": state.context_id, "payload": state.payload, "queue_name": "device-state-db", "queue_id": id(self.store)},
            )
            try:
                await self._process_state(state)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._failed += 1
                self.logger.exception(
                    "Sender command processing failed; worker will continue",
                    extra={"device_id": device_id, "state_id": state.id, "context_id": state.context_id},
                )
                await self._health.record_failure("sender", exc)
                await self._sleep_with_stop(self._backoff.delay(1))
            if rate_delay:
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=rate_delay)
                except asyncio.TimeoutError:
                    continue

    async def _process_state(self, state: PendingState) -> None:
        started = time.perf_counter()
        transport_label = "none"
        context_extra = {"device_id": state.device_id, "context_id": state.context_id}

        def _finalize(result: str) -> None:
            duration = time.perf_counter() - started
            record_send_result(result)
            observe_send_duration(result, transport_label, duration)

        allowed, remaining = await self._health.allow_attempt("sender")
        if not allowed:
            self.logger.warning(
                "Send pipeline suppressed after repeated failures",
                extra={**context_extra, "cooldown_seconds": round(remaining, 2)},
            )
            await self._sleep_with_stop(remaining)
            _finalize("suppressed")
            return

        payload_hash = hashlib.sha256(state.payload.encode("utf-8")).hexdigest()
        device = await self.store.device_info(state.device_id)
        if device is None:
            self.logger.warning(
                "Skipping send for unknown or disabled device",
                extra=context_extra,
            )
            await self.store.record_send_failure(
                state.device_id, payload_hash, self.config.device_offline_threshold
            )
            await self._health.record_failure("sender", RuntimeError("unknown or disabled device"))
            await self.store.quarantine_state(
                state, payload_hash, reason="device_unavailable", details="missing, disabled, or stale"
            )
            _finalize("dead_letter")
            return

        target = _derive_target(self.config, device)
        if target is None:
            self.logger.warning(
                "Device missing IP; cannot send",
                extra=context_extra,
            )
            await self.store.record_send_failure(
                state.device_id, payload_hash, self.config.device_offline_threshold
            )
            await self._health.record_failure("sender", RuntimeError("device missing IP"))
            await self.store.quarantine_state(
                state, payload_hash, reason="missing_ip", details="device has no IP address"
            )
            _finalize("dead_letter")
            return

        if state.context_id != "command" and device.failure_count == 0 and device.last_payload_hash == payload_hash:
            self.logger.debug(
                "Dropping duplicate payload",
                extra={**context_extra, "reason": "same payload was previously delivered", "payload": state.payload},
            )
            await self.store.delete_state(state.id)
            return

        await self._acquire_rate_limit(state.device_id, state.context_id)

        # Decode binary payloads (LIFX) or encode text payloads (Govee)
        if state.payload.startswith("base64:"):
            # Binary payload - decode from base64
            payload = base64.b64decode(state.payload[7:])  # Strip "base64:" prefix
        else:
            # Text payload - encode to UTF-8 (Govee JSON)
            payload = state.payload.encode("utf-8")

        if target.protocol.lower() == "govee":
            try:
                document = json.loads(payload)
                message = document["msg"]
                if not isinstance(message.get("cmd"), str) or not isinstance(message.get("data"), Mapping):
                    raise ValueError("msg.cmd and msg.data are required")
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                self.logger.exception(
                    "Invalid Govee UDP payload; moving command to dead letter",
                    extra={**context_extra, "payload": state.payload, "protocol": target.protocol},
                )
                await self.store.quarantine_state(
                    state, payload_hash, reason="invalid_payload", details=str(exc)
                )
                await self._health.record_failure("sender", exc)
                self._failed += 1
                _finalize("dead_letter")
                return

        transport_label = target.transport
        self.logger.debug("Protocol sender selected", extra={**context_extra, "protocol": target.protocol, "transport": target.transport})
        self.logger.debug("Destination device selected", extra={**context_extra, "device_ip": target.ip, "destination_port": target.port, "protocol": target.protocol})
        try:
            success = await self._send_with_retries(target, payload, payload_hash, state.context_id)
        except Exception as exc:  # pragma: no cover - defensive
            self.logger.error(
                "Unhandled send error",
                extra=context_extra,
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            await self.store.record_send_failure(
                state.device_id, payload_hash, self.config.device_offline_threshold
            )
            await self._health.record_failure("sender", exc)
            await self._sleep_with_stop(self._backoff.delay(1))
            _finalize("error")
            return
        if success:
            await self._health.record_success("sender")
            await self.store.record_send_success(state.device_id, payload_hash)
            await self.store.set_last_seen([state.device_id], mark_online=True)
            await self.store.delete_state(state.id)
            self._sent += 1
            _finalize("success" if not self._dry_run else "dry_run")
        else:
            self._failed += 1
            await self.store.record_send_failure(
                state.device_id, payload_hash, self.config.device_offline_threshold
            )
            await self._health.record_failure("sender", RuntimeError("send failed"))
            await self._sleep_with_stop(self._backoff.delay(1))
            _finalize("failure")

    async def _send_with_retries(
        self, target: DeviceTarget, payload: bytes, payload_hash: str, context_id: Optional[str]
    ) -> bool:
        if self._dry_run:
            self.logger.info(
                "Dry-run: would send payload",
                extra={
                    "device_id": target.id,
                    "transport": target.transport,
                    "port": target.port,
                    "context_id": context_id,
                },
            )
            return True
        attempts = max(1, self.config.device_send_retries)
        delays = self._backoff.iter_delays(attempts)
        for attempt in range(1, attempts + 1):
            self.logger.debug(
                "Sender attempting delivery",
                extra={
                    "device_id": target.id,
                    "device_ip": target.ip,
                    "protocol": target.protocol,
                    "transport": target.transport,
                    "destination_port": target.port,
                    "attempt": attempt,
                    "max_attempts": attempts,
                    "context_id": context_id,
                },
            )
            if await self._send_once(target, payload, context_id):
                return True
            if attempt == attempts:
                break
            await self._sleep_with_stop(delays[attempt - 1])
        self.logger.error(
            "Exhausted retries sending payload",
            extra={
                "device_id": target.id,
                "transport": target.transport,
                "port": target.port,
                "hash": payload_hash,
                "attempts": attempts,
                "context_id": context_id,
            },
        )
        return False

    async def _send_once(
        self, target: DeviceTarget, payload: bytes, context_id: Optional[str]
    ) -> bool:
        if target.transport == "tcp":
            return await self._send_tcp(target, payload, context_id)
        return await self._send_udp(target, payload, context_id)

    async def _send_udp(
        self, target: DeviceTarget, payload: bytes, context_id: Optional[str]
    ) -> bool:
        def _send() -> int:
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                    sock.settimeout(self.config.device_send_timeout)
                    sent = sock.sendto(payload, (target.ip, target.port))
                return sent
            except OSError as exc:
                self.logger.warning(
                    "UDP send failed",
                    exc_info=(type(exc), exc, exc.__traceback__),
                    extra={"device_id": target.id, "context_id": context_id},
                )
                return 0

        try:
            self.logger.debug("UDP command about to be sent", extra={"device_id": target.id, "device_ip": target.ip, "protocol": target.protocol, "destination_port": target.port, "bytes": len(payload), "payload": payload.decode("utf-8", errors="replace"), "context_id": context_id})
            sent = await asyncio.wait_for(
                asyncio.to_thread(_send), timeout=self.config.device_send_timeout
            )
            if sent == len(payload):
                self.logger.debug("UDP send completed", extra={"device_id": target.id, "device_ip": target.ip, "destination_port": target.port, "bytes_sent": sent, "context_id": context_id})
                return True
            self.logger.warning(
                "UDP send returned an incomplete byte count",
                extra={
                    "device_id": target.id,
                    "device_ip": target.ip,
                    "destination_port": target.port,
                    "bytes_expected": len(payload),
                    "bytes_sent": sent,
                    "context_id": context_id,
                },
            )
            return False
        except (asyncio.TimeoutError, OSError) as exc:
            self.logger.warning(
                "UDP send timed out",
                exc_info=(type(exc), exc, exc.__traceback__),
                extra={"device_id": target.id, "context_id": context_id},
            )
            return False

    async def _send_tcp(
        self, target: DeviceTarget, payload: bytes, context_id: Optional[str]
    ) -> bool:
        try:
            _reader, writer = await asyncio.wait_for(
                asyncio.open_connection(target.ip, target.port),
                timeout=self.config.device_send_timeout,
            )
            writer.write(payload)
            await asyncio.wait_for(writer.drain(), timeout=self.config.device_send_timeout)
            writer.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(
                    writer.wait_closed(), timeout=self.config.device_send_timeout
                )
            return True
        except (asyncio.TimeoutError, OSError) as exc:
            self.logger.warning(
                "TCP send failed",
                exc_info=(type(exc), exc, exc.__traceback__),
                extra={"device_id": target.id, "context_id": context_id},
            )
            return False

    async def _sleep_with_stop(self, delay: float) -> None:
        if delay <= 0:
            return
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
        except asyncio.TimeoutError:
            return

    async def _acquire_rate_limit(self, device_id: str, context_id: Optional[str]) -> None:
        if self.config.rate_limit_per_second <= 0 or self.config.rate_limit_burst <= 0:
            return
        while not self._stop_event.is_set():
            async with self._rate_lock:
                now = time.perf_counter()
                elapsed = max(0.0, now - self._rate_last_refill)
                self._rate_last_refill = now
                self._rate_tokens = min(
                    float(self.config.rate_limit_burst),
                    self._rate_tokens + elapsed * self.config.rate_limit_per_second,
                )
                if self._rate_tokens >= 1.0:
                    self._rate_tokens -= 1.0
                    set_rate_limit_tokens(self._rate_tokens)
                    return
                wait_seconds = (1.0 - self._rate_tokens) / self.config.rate_limit_per_second
                set_rate_limit_tokens(self._rate_tokens)
            self.logger.debug(
                "Rate limit exceeded; delaying send",
                extra={
                    "device_id": device_id,
                    "context_id": context_id,
                    "wait_seconds": round(wait_seconds, 3),
                    "tokens": round(self._rate_tokens, 3),
                },
            )
            record_rate_limit_wait("global")
            await self._sleep_with_stop(wait_seconds)
