import asyncio
import hashlib
import json

import pytest
from httpx import ASGITransport, AsyncClient

from dmx_lan_bridge.api import create_app
from dmx_lan_bridge.config import Config, ManualDevice
from dmx_lan_bridge.db import apply_migrations
from dmx_lan_bridge.devices import DeviceStateUpdate, DeviceStore
from dmx_lan_bridge.sender import DeviceSenderService


async def _wait_for_drain(store: DeviceStore, timeout: float = 1.0) -> None:
    start = asyncio.get_event_loop().time()
    while True:
        pending = await store.pending_device_ids()
        if not pending:
            return
        if asyncio.get_event_loop().time() - start > timeout:
            raise TimeoutError("Queue did not drain in time")
        await asyncio.sleep(0.01)


def _fast_config(db_path) -> Config:
    return Config(
        db_path=db_path,
        dry_run=True,
        device_queue_poll_interval=0.01,
        device_idle_wait=0.01,
        device_backoff_base=0.01,
        device_backoff_factor=1.0,
        device_backoff_max=0.1,
    )


@pytest.mark.asyncio
async def test_queue_drains_when_device_missing_ip(tmp_path) -> None:
    db_path = tmp_path / "bridge.sqlite3"
    apply_migrations(db_path)
    config = _fast_config(db_path)
    store = DeviceStore(config.db_path)
    await store.create_manual_device(
        ManualDevice(id="dev-missing-ip", ip="", capabilities={"transport": "udp"})
    )
    await store.enqueue_state(
        DeviceStateUpdate(device_id="dev-missing-ip", payload={"foo": "bar"})
    )

    sender = DeviceSenderService(config, store)
    await sender.start()

    try:
        await _wait_for_drain(store)
    finally:
        await sender.stop()

    assert await store.pending_device_ids() == []
    dead_letters = await store.dead_letters("dev-missing-ip")
    assert len(dead_letters) == 1
    assert dead_letters[0].reason == "missing_ip"


@pytest.mark.asyncio
async def test_queue_drains_when_device_disabled_or_stale(tmp_path) -> None:
    db_path = tmp_path / "bridge.sqlite3"
    apply_migrations(db_path)
    config = _fast_config(db_path)
    store = DeviceStore(config.db_path)
    device = await store.create_manual_device(
        ManualDevice(id="dev-disabled", ip="127.0.0.1", capabilities={"transport": "udp"})
    )
    assert device.enabled is True
    await store.enqueue_state(
        DeviceStateUpdate(device_id="dev-disabled", payload={"foo": "bar"})
    )

    await store.update_device("dev-disabled", enabled=False)

    sender = DeviceSenderService(config, store)
    await sender.start()

    try:
        await _wait_for_drain(store)
    finally:
        await sender.stop()

    assert await store.pending_device_ids() == []
    dead_letters = await store.dead_letters("dev-disabled")
    assert len(dead_letters) == 1
    assert dead_letters[0].reason == "device_unavailable"


@pytest.mark.asyncio
async def test_rate_limit_throttles_sends(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "bridge.sqlite3"
    apply_migrations(db_path)
    config = Config(
        db_path=db_path,
        dry_run=True,
        device_queue_poll_interval=0.01,
        device_idle_wait=0.0,
        device_backoff_base=0.0,
        device_backoff_factor=1.0,
        device_backoff_max=0.1,
        device_max_send_rate=0.0,
        rate_limit_per_second=2.0,
        rate_limit_burst=1,
    )
    store = DeviceStore(config.db_path)
    await store.create_manual_device(
        ManualDevice(id="dev-rate-limit", ip="127.0.0.1", capabilities={"transport": "udp"})
    )

    send_times = []

    async def _fake_send(self, *_args, **_kwargs):
        send_times.append(asyncio.get_event_loop().time())
        return True

    monkeypatch.setattr(DeviceSenderService, "_send_with_retries", _fake_send)

    for idx in range(3):
        await store.enqueue_state(
            DeviceStateUpdate(device_id="dev-rate-limit", payload={"seq": idx})
        )

    sender = DeviceSenderService(config, store)
    await sender.start()

    try:
        await _wait_for_drain(store, timeout=2.0)
    finally:
        await sender.stop()

    assert len(send_times) == 3
    spacing = [send_times[i + 1] - send_times[i] for i in range(len(send_times) - 1)]
    assert spacing[0] >= 0.45
    assert spacing[1] >= 0.45


@pytest.mark.asyncio
async def test_rate_limit_allows_burst(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "bridge.sqlite3"
    apply_migrations(db_path)
    config = Config(
        db_path=db_path,
        dry_run=True,
        device_queue_poll_interval=0.01,
        device_idle_wait=0.0,
        device_backoff_base=0.0,
        device_backoff_factor=1.0,
        device_backoff_max=0.1,
        device_max_send_rate=0.0,
        rate_limit_per_second=10.0,
        rate_limit_burst=3,
    )
    store = DeviceStore(config.db_path)
    await store.create_manual_device(
        ManualDevice(id="dev-rate-burst", ip="127.0.0.1", capabilities={"transport": "udp"})
    )

    send_times = []

    async def _fake_send(self, *_args, **_kwargs):
        send_times.append(asyncio.get_event_loop().time())
        return True

    monkeypatch.setattr(DeviceSenderService, "_send_with_retries", _fake_send)

    for idx in range(4):
        await store.enqueue_state(
            DeviceStateUpdate(device_id="dev-rate-burst", payload={"seq": idx})
        )

    sender = DeviceSenderService(config, store)
    await sender.start()

    try:
        await _wait_for_drain(store, timeout=2.0)
    finally:
        await sender.stop()

    assert len(send_times) == 4
    spacing = [send_times[i + 1] - send_times[i] for i in range(len(send_times) - 1)]
    assert spacing[0] < 0.3
    assert spacing[1] < 0.3
    assert spacing[2] >= 0.08


@pytest.mark.asyncio
async def test_command_api_to_live_sender_to_govee_udp(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "bridge.sqlite3"
    apply_migrations(db_path)
    config = Config(db_path=db_path, dry_run=False, device_queue_poll_interval=0.01, device_idle_wait=0.01, device_backoff_base=0.01, device_backoff_factor=1.0, device_backoff_max=0.1)
    store = DeviceStore(db_path)
    device_id = "11:42:DB:C3:42:86:72:4A"
    await store.create_manual_device(ManualDevice(id=device_id, ip="10.0.5.9", capabilities={"transport": "udp"}))
    calls = []
    class FakeSocket:
        def __init__(self, *_args): pass
        def __enter__(self): return self
        def __exit__(self, *_args): return None
        def settimeout(self, _timeout): pass
        def sendto(self, payload, destination):
            calls.append((payload, destination)); return len(payload)
    monkeypatch.setattr("dmx_lan_bridge.sender.socket.socket", FakeSocket)
    sender = DeviceSenderService(config, store)
    await sender.start()
    app = create_app(config, store, sender_provider=lambda: sender)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(f"/devices/{device_id}/command", json={"off": True})
        assert response.status_code == 202
        assert response.json()["payloads"] == [{"msg": {"cmd": "turn", "data": {"value": 0}}}]
        await _wait_for_drain(store)
    finally:
        await sender.stop()
    assert calls[0][1] == ("10.0.5.9", 4003)
    assert json.loads(calls[0][0]) == {"msg": {"cmd": "turn", "data": {"value": 0}}}
    assert sender._enqueued == sender._dequeued == sender._sent == 1


@pytest.mark.asyncio
async def test_manual_command_bypasses_delivered_payload_dedup(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "bridge.sqlite3"; apply_migrations(db_path)
    config = _fast_config(db_path); store = DeviceStore(db_path)
    await store.create_manual_device(ManualDevice(id="dedup", ip="127.0.0.1"))
    await store.enqueue_state(DeviceStateUpdate(device_id="dedup", payload={"msg": {"cmd": "turn", "data": {"value": 0}}}, context_id="command"))
    state = await store.next_state("dedup"); assert state is not None
    await store.record_send_success("dedup", hashlib.sha256(state.payload.encode()).hexdigest())
    sent = []
    async def fake_send(*_args): sent.append(True); return True
    sender = DeviceSenderService(config, store); monkeypatch.setattr(sender, "_send_with_retries", fake_send)
    await sender.start()
    try: await _wait_for_drain(store)
    finally: await sender.stop()
    assert sent == [True]


@pytest.mark.asyncio
async def test_queue_failure_is_not_reported_as_queued(tmp_path) -> None:
    db_path = tmp_path / "bridge.sqlite3"; apply_migrations(db_path); store = DeviceStore(db_path)
    await store.create_manual_device(ManualDevice(id="offline-sender", ip="127.0.0.1"))
    sender = DeviceSenderService(_fast_config(db_path), store)
    app = create_app(Config(), store, sender_provider=lambda: sender)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/devices/offline-sender/command", json={"off": True})
    assert response.status_code == 503
    assert await store.pending_device_ids() == []


@pytest.mark.asyncio
async def test_sender_health_detects_failed_supervisor(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "bridge.sqlite3"; apply_migrations(db_path)
    sender = DeviceSenderService(_fast_config(db_path), DeviceStore(db_path))
    async def fail(): raise RuntimeError("supervisor exploded")
    monkeypatch.setattr(sender, "_poll_loop", fail); await sender.start(); await asyncio.sleep(0)
    details = await sender.health_details()
    assert details["status"] == "failed" and "supervisor exploded" in details["reason"]
    await sender.stop()


@pytest.mark.asyncio
async def test_invalid_govee_payload_is_quarantined_without_killing_worker(
    monkeypatch, tmp_path
) -> None:
    db_path = tmp_path / "bridge.sqlite3"
    apply_migrations(db_path)
    config = _fast_config(db_path)
    store = DeviceStore(db_path)
    await store.create_manual_device(ManualDevice(id="govee-audit", ip="127.0.0.1"))
    def insert_malformed(conn):
        conn.execute(
            "INSERT INTO state (device_id, payload, context_id) VALUES (?, ?, ?)",
            ("govee-audit", "not-json", "corrupt-test"),
        )
        conn.commit()

    await store.db.run(insert_malformed)
    await store.enqueue_state(
        DeviceStateUpdate(
            device_id="govee-audit",
            payload={"msg": {"cmd": "turn", "data": {"value": 0}}},
        )
    )
    delivered = []

    async def fake_send(*_args):
        delivered.append(True)
        return True

    sender = DeviceSenderService(config, store)
    monkeypatch.setattr(sender, "_send_with_retries", fake_send)
    await sender.start()
    try:
        await _wait_for_drain(store)
    finally:
        await sender.stop()

    dead_letters = await store.dead_letters("govee-audit")
    assert len(dead_letters) == 1
    assert dead_letters[0].reason == "invalid_payload"
    assert delivered == [True]
