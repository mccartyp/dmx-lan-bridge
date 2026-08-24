"""HTTP API server for device and mapping management."""

from __future__ import annotations

import asyncio
import inspect
import string
import time
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional

from fastapi import Depends, FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
import uvicorn

from .capabilities import NormalizedCapabilities, validate_command_payload
from .config import Config, ManualDevice
from .devices import DeviceStateUpdate, DeviceStore
from .events import (
    EVENT_DEVICE_DISABLED,
    EVENT_DEVICE_ENABLED,
    EVENT_MAPPING_CREATED,
    EVENT_MAPPING_DELETED,
    EVENT_MAPPING_UPDATED,
)
from .health import HealthMonitor
from .logging import get_logger, redact_mapping
from .metrics import (
    METRICS_CONTENT_TYPE,
    latest_metrics,
    observe_request,
)


def _build_auth_dependency(config: Config) -> Callable[[Request], None]:
    async def _auth_guard(request: Request) -> None:
        if not config.api_key and not config.api_bearer_token:
            return
        api_key_header = request.headers.get("X-API-Key")
        auth_header = request.headers.get("Authorization")
        if config.api_key and api_key_header == config.api_key:
            return
        if config.api_key and auth_header and auth_header.lower().startswith("apikey "):
            if auth_header.split(" ", 1)[1] == config.api_key:
                return
        if config.api_bearer_token and auth_header and auth_header.startswith("Bearer "):
            if auth_header.split(" ", 1)[1] == config.api_bearer_token:
                return
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return _auth_guard


class DeviceCreate(BaseModel):
    """Payload for creating a manual device."""

    model_config = ConfigDict(populate_by_name=True)
    id: str
    ip: str
    protocol: str = "govee"
    model_number: Optional[str] = Field(
        default=None, alias="model"
    )
    device_type: Optional[str] = None
    description: Optional[str] = None
    capabilities: Optional[Any] = None
    length_meters: Optional[float] = None
    led_count: Optional[int] = None
    led_density_per_meter: Optional[float] = None
    has_zones: Optional[bool] = None
    zone_count: Optional[int] = None
    enabled: bool = True

    @field_validator("protocol")
    @classmethod
    def validate_protocol(cls, v: str) -> str:
        """Validate that protocol is supported."""
        from .protocol import get_supported_protocols
        supported = get_supported_protocols()
        if v not in supported:
            raise ValueError(
                f"Unsupported protocol '{v}'. Supported protocols: {', '.join(supported)}"
            )
        return v


class DeviceUpdate(BaseModel):
    """Partial update payload for a device."""

    model_config = ConfigDict(populate_by_name=True)
    ip: Optional[str] = None
    name: Optional[str] = None
    model_number: Optional[str] = Field(
        default=None, alias="model"
    )
    device_type: Optional[str] = None
    description: Optional[str] = None
    capabilities: Optional[Any] = None
    length_meters: Optional[float] = None
    led_count: Optional[int] = None
    led_density_per_meter: Optional[float] = None
    has_zones: Optional[bool] = None
    zone_count: Optional[int] = None
    enabled: Optional[bool] = None


class DeviceOut(BaseModel):
    """Device response model."""

    model_config = ConfigDict(populate_by_name=True)
    id: str
    ip: Optional[str]
    protocol: str
    name: Optional[str] = None
    model_number: Optional[str]
    model: Optional[str] = None
    device_type: Optional[str] = None
    length_meters: Optional[float] = None
    led_count: Optional[int] = None
    led_density_per_meter: Optional[float] = None
    has_zones: Optional[bool] = None
    zone_count: Optional[str] = None
    description: Optional[str]
    capabilities: Optional[Any]
    manual: bool
    discovered: bool
    configured: bool
    enabled: bool
    stale: bool
    offline: bool
    poll_health: str = "healthy"
    last_seen: Optional[str]
    first_seen: Optional[str]
    poll_last_success_at: Optional[str] = None
    poll_last_failure_at: Optional[str] = None
    poll_failure_count: int = 0
    poll_state: Optional[Any] = None
    poll_state_updated_at: Optional[str] = None
    mapping_count: int = 0
    created_at: str
    updated_at: str

    @model_validator(mode="after")
    def _backfill_model(self) -> "DeviceOut":
        if self.model is None:
            self.model = self.model_number
        if self.model_number is None and self.model is not None:
            self.model_number = self.model
        return self


class MappingCreate(BaseModel):
    """Payload for creating a mapping.

    Note: sACN (E1.31) universes are 1–63999. Art-Net supports universe 0.
    Universe 0 is Art-Net-only in this application; universes 1+ are mergeable across protocols.
    """

    device_id: str
    universe: int = Field(default=1, ge=0, description="DMX universe (0=Art-Net only, 1+=multi-protocol)")
    channel: Optional[int] = Field(default=None, gt=0)
    start_channel: Optional[int] = Field(default=None, gt=0)
    length: Optional[int] = Field(default=1, gt=0)
    mapping_type: Optional[str] = Field(default="range")
    field: Optional[str] = None
    allow_overlap: bool = False
    template: Optional[str] = None


class MappingUpdate(BaseModel):
    """Partial update payload for a mapping."""

    device_id: Optional[str] = None
    universe: Optional[int] = Field(default=None, ge=0)
    channel: Optional[int] = Field(default=None, gt=0)
    length: Optional[int] = Field(default=None, gt=0)
    mapping_type: Optional[str] = None
    field: Optional[str] = None
    allow_overlap: bool = False


class MappingOut(BaseModel):
    """Mapping response model."""

    id: int
    device_id: str
    universe: int
    channel: int
    length: int
    mapping_type: str
    field: Optional[str]
    fields: List[str]
    created_at: str
    updated_at: str


class ChannelMapEntry(BaseModel):
    """Channel map details for a single mapping slot."""

    id: int
    device_id: str
    universe: int
    channel: int
    length: int
    mapping_type: str
    fields: List[str]
    device_description: Optional[str]
    device_ip: Optional[str]
    field: Optional[str] = None


class TestAction(BaseModel):
    """Test payload to enqueue to a device."""

    payload: Any


class DeviceCommand(BaseModel):
    """Command payload for simple device control."""

    on: bool = False
    off: bool = False
    brightness: Optional[int] = Field(default=None, ge=0, le=255)
    color: Optional[str] = None
    kelvin: Optional[int] = Field(default=None, ge=0, le=255)

    @field_validator("color")
    @classmethod
    def _validate_color(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        normalized = value.strip()
        if normalized.startswith("#"):
            normalized = normalized[1:]
        if len(normalized) == 3:
            normalized = "".join(ch * 2 for ch in normalized)
        if len(normalized) != 6 or any(ch not in string.hexdigits for ch in normalized):
            raise ValueError("Color must be an RGB hex string like ff3366.")
        return normalized.lower()

    @model_validator(mode="after")
    def _validate_actions(self) -> "DeviceCommand":
        if self.on and self.off:
            raise ValueError("Choose either on or off, not both.")
        if not any(
            [
                self.on,
                self.off,
                self.brightness is not None,
                self.color,
                self.kelvin is not None,
            ]
        ):
            raise ValueError("At least one action is required.")
        return self


def _parse_hex_color(value: str) -> Mapping[str, int]:
    normalized = value.strip().lower()
    if normalized.startswith("#"):
        normalized = normalized[1:]
    if len(normalized) == 3:
        normalized = "".join(ch * 2 for ch in normalized)
    if len(normalized) != 6 or any(ch not in string.hexdigits for ch in normalized):
        raise ValueError("Color must be an RGB hex string like ff3366.")
    return {
        "r": int(normalized[0:2], 16),
        "g": int(normalized[2:4], 16),
        "b": int(normalized[4:6], 16),
    }


def _scale_color_temp(value: int, capabilities: NormalizedCapabilities) -> int:
    low, high = capabilities.color_temp_range or (2000, 9000)
    scaled = low + (high - low) * (max(0, min(255, value)) / 255.0)
    return int(round(scaled))


def _build_command_payload(
    command: DeviceCommand, capabilities: NormalizedCapabilities
) -> tuple[Mapping[str, Any], list[str]]:
    payload: Dict[str, Any] = {}
    if command.brightness is not None:
        payload["brightness"] = command.brightness
    if command.color:
        payload["color"] = _parse_hex_color(command.color)
    if command.kelvin is not None:
        payload["color_temp"] = _scale_color_temp(command.kelvin, capabilities)
    if not payload:
        return {}, []
    sanitized, warnings = validate_command_payload(payload, capabilities)

    # Build separate Govee commands based on what's in the sanitized payload
    # Following govee-discovery patterns: brightness cmd, colorwc cmd, etc.
    wrapped_payloads: list[Mapping[str, Any]] = []

    # Brightness command
    if "brightness" in sanitized and "color" not in sanitized and "color_temp" not in sanitized:
        wrapped_payloads.append({
            "msg": {
                "cmd": "brightness",
                "data": {"value": sanitized["brightness"]}
            }
        })

    # Color/colorwc command (may include color_temp for combined commands)
    if "color" in sanitized or "color_temp" in sanitized:
        data: Dict[str, Any] = {}
        if "color" in sanitized:
            data["color"] = sanitized["color"]
        if "color_temp" in sanitized:
            data["colorTemInKelvin"] = sanitized["color_temp"]
        wrapped_payloads.append({
            "msg": {
                "cmd": "colorwc",
                "data": data
            }
        })
        # If brightness was also specified with color, send it as a separate command
        if "brightness" in sanitized:
            wrapped_payloads.append({
                "msg": {
                    "cmd": "brightness",
                    "data": {"value": sanitized["brightness"]}
                }
            })

    # Return the first wrapped payload (or combined logic)
    # For now, return all wrapped payloads as a single combined one if multiple
    if len(wrapped_payloads) == 1:
        return wrapped_payloads[0], warnings
    elif len(wrapped_payloads) > 1:
        # Return as a list indicator - we'll need to handle this
        return {"_multiple": wrapped_payloads}, warnings

    return {}, warnings


def _build_turn_payload(command: DeviceCommand) -> Optional[Mapping[str, Any]]:
    if command.on:
        return {"msg": {"cmd": "turn", "data": {"value": 1}}}
    if command.off:
        return {"msg": {"cmd": "turn", "data": {"value": 0}}}
    return None


def _overall_status(subsystems: Mapping[str, Mapping[str, Any]]) -> str:
    if any(state.get("status") != "ok" for state in subsystems.values()):
        return "degraded"
    return "ok"


def create_app(
    config: Config,
    store: DeviceStore,
    health: Optional[HealthMonitor] = None,
    reload_callback: Optional[Callable[[], Awaitable[None]]] = None,
    log_buffer: Optional[Any] = None,
    event_bus: Optional[Any] = None,
    sender_provider: Optional[Callable[[], Any]] = None,
) -> FastAPI:
    """Create and configure a FastAPI application."""

    logger = get_logger("artnet.api")
    request_logger = get_logger("artnet.api.middleware")
    auth_dependency = _build_auth_dependency(config)
    app = FastAPI(
        title="ArtNet LAN Bridge API",
        description="Multi-protocol ArtNet to LAN device bridge (Govee, LIFX, and more)",
        docs_url="/docs" if config.api_docs else None,
        redoc_url="/redoc" if config.api_docs else None,
        openapi_url="/openapi.json" if config.api_docs else None,
    )

    @app.middleware("http")
    async def _logging_middleware(request: Request, call_next: Callable[..., Any]) -> JSONResponse:
        start = time.perf_counter()
        path_template = getattr(request.scope.get("route"), "path", request.url.path)
        redacted_headers = redact_mapping(dict(request.headers))
        try:
            response = await call_next(request)
        except HTTPException as exc:
            duration_seconds = time.perf_counter() - start
            observe_request(request.method, path_template, exc.status_code, duration_seconds)
            request_logger.warning(
                "API error",
                extra={
                    "method": request.method,
                    "path": path_template,
                    "status": exc.status_code,
                    "duration_ms": round(duration_seconds * 1000, 2),
                    "client": request.client.host if request.client else None,
                    "headers": redacted_headers,
                },
            )
            raise
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("Unhandled API error")
            raise HTTPException(status_code=500, detail="Internal server error") from exc
        duration_seconds = time.perf_counter() - start
        observe_request(
            request.method,
            path_template,
            response.status_code,
            duration_seconds,
        )
        request_logger.info(
            "Handled request",
            extra={
                "method": request.method,
                "path": path_template,
                "status": response.status_code,
                "duration_ms": round(duration_seconds * 1000, 2),
                "client": request.client.host if request.client else None,
                "headers": redacted_headers,
            },
        )
        return response

    @app.exception_handler(HTTPException)
    async def _http_exc_handler(request: Request, exc: HTTPException) -> JSONResponse:
        request_logger.warning(
            "API error",
            extra={"path": request.url.path, "status": exc.status_code, "detail": exc.detail},
        )
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        request_logger.warning(
            "Validation error",
            extra={"path": request.url.path, "errors": exc.errors()},
        )
        return JSONResponse(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, content={"detail": exc.errors()})

    @app.get("/health", dependencies=[Depends(auth_dependency)])
    async def health_status() -> dict[str, Any]:
        subsystems = dict(await health.snapshot()) if health else {}
        if sender_provider:
            sender = sender_provider()
            subsystems["sender"] = (
                await sender.health_details()
                if sender is not None
                else {"status": "failed", "reason": "sender worker was never started"}
            )
        return {"status": _overall_status(subsystems), "subsystems": subsystems}

    async def _enqueue_command(update: DeviceStateUpdate) -> None:
        """Queue through the live sender when application wiring supplies one."""
        try:
            if sender_provider is None:
                raise RuntimeError("sender service was not wired into the API")
            sender = sender_provider()
            if sender is None:
                raise RuntimeError("sender service is unavailable")
            await sender.enqueue(update)
        except Exception as exc:
            logger.exception(
                "Command queue insertion failed",
                extra={"device_id": update.device_id, "context_id": update.context_id},
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Sender queue unavailable",
            ) from exc

    @app.get("/status", dependencies=[Depends(auth_dependency)])
    async def status_view() -> dict[str, Any]:
        payload: dict[str, Any] = dict(await store.stats())
        payload.update(await store.polling_stats())
        payload["device_polling_enabled"] = config.device_poll_enabled
        payload["protocols"] = await store.protocol_stats()
        return payload

    @app.get("/metrics")
    async def metrics() -> Response:
        return Response(content=latest_metrics(), media_type=METRICS_CONTENT_TYPE)

    @app.get("/devices", dependencies=[Depends(auth_dependency)], response_model=list[DeviceOut])
    async def list_devices() -> list[DeviceOut]:
        rows = await store.devices()
        return [DeviceOut(**row.__dict__) for row in rows]

    @app.post("/devices", dependencies=[Depends(auth_dependency)], response_model=DeviceOut, status_code=status.HTTP_201_CREATED)
    async def create_device(payload: DeviceCreate) -> DeviceOut:
        manual = ManualDevice(
            id=payload.id,
            ip=payload.ip,
            model_number=payload.model_number,
            device_type=payload.device_type,
            description=payload.description,
            capabilities=payload.capabilities,
            length_meters=payload.length_meters,
            led_count=payload.led_count,
            led_density_per_meter=payload.led_density_per_meter,
            has_zones=payload.has_zones,
            zone_count=payload.zone_count,
        )
        device = await store.create_manual_device(manual)
        if payload.enabled is not None and not payload.enabled:
            device = await store.update_device(device.id, enabled=False) or device
        return DeviceOut(**device.__dict__)

    @app.get("/devices/{device_id}", dependencies=[Depends(auth_dependency)], response_model=DeviceOut)
    async def get_device(device_id: str) -> DeviceOut:
        device = await store.device(device_id)
        if not device:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Device not found")
        return DeviceOut(**device.__dict__)

    @app.patch("/devices/{device_id}", dependencies=[Depends(auth_dependency)], response_model=DeviceOut)
    async def update_device(device_id: str, payload: DeviceUpdate) -> DeviceOut:
        updated = await store.update_device(
            device_id,
            ip=payload.ip,
            name=payload.name,
            model_number=payload.model_number,
            device_type=payload.device_type,
            length_meters=payload.length_meters,
            led_count=payload.led_count,
            led_density_per_meter=payload.led_density_per_meter,
            has_zones=payload.has_zones,
            zone_count=payload.zone_count,
            description=payload.description,
            capabilities=payload.capabilities,
            enabled=payload.enabled,
        )
        if not updated:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Device not found")

        # Emit device enabled/disabled event if enabled state was changed
        if payload.enabled is not None and event_bus:
            event_type = EVENT_DEVICE_ENABLED if payload.enabled else EVENT_DEVICE_DISABLED
            await event_bus.publish(event_type, {"device_id": device_id, "enabled": payload.enabled})

        return DeviceOut(**updated.__dict__)

    @app.post("/devices/{device_id}/test", dependencies=[Depends(auth_dependency)], status_code=status.HTTP_202_ACCEPTED)
    async def test_device(device_id: str, payload: TestAction) -> dict[str, str]:
        device = await store.device(device_id)
        if not device:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Device not found")
        capabilities = await store.normalized_capabilities(device_id)
        if capabilities is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Device not found")
        try:
            sanitized, warnings = validate_command_payload(payload.payload, capabilities)
            update = DeviceStateUpdate(device_id=device_id, payload=sanitized)
            await _enqueue_command(update)
            response: dict[str, str] = {"status": "queued"}
            if warnings:
                response["detail"] = "; ".join(warnings)
            return response
        except HTTPException:
            raise
        except Exception as exc:  # pragma: no cover - defensive
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post(
        "/devices/{device_id}/command",
        dependencies=[Depends(auth_dependency)],
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def command_device(device_id: str, payload: DeviceCommand) -> dict[str, Any]:
        device = await store.device(device_id)
        if not device:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Device not found")
        capabilities = await store.normalized_capabilities(device_id)
        if capabilities is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Device not found")
        try:
            turn_payload = _build_turn_payload(payload)
            state_payload, warnings = _build_command_payload(payload, capabilities)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        payloads: list[Mapping[str, Any]] = []
        if turn_payload:
            payloads.append(turn_payload)
        if state_payload:
            # Check if state_payload contains multiple wrapped payloads
            if isinstance(state_payload, dict) and "_multiple" in state_payload:
                payloads.extend(state_payload["_multiple"])
            else:
                payloads.append(state_payload)
        if not payloads:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No actions to enqueue")
        logger.debug(
            "Command payload generated",
            extra={"device_id": device_id, "protocol": device.protocol, "command_type": "manual", "payload": payloads},
        )
        for entry in payloads:
            update = DeviceStateUpdate(device_id=device_id, payload=entry, context_id="command")
            await _enqueue_command(update)
        response: dict[str, Any] = {"status": "queued", "payloads": payloads}
        if warnings:
            response["detail"] = "; ".join(warnings)
        return response

    @app.get("/mappings", dependencies=[Depends(auth_dependency)], response_model=list[MappingOut])
    async def list_mappings() -> list[MappingOut]:
        rows = await store.mapping_rows()
        return [MappingOut(**row.__dict__) for row in rows]

    @app.get(
        "/channel-map",
        dependencies=[Depends(auth_dependency)],
        response_model=Dict[int, List[ChannelMapEntry]],
    )
    async def channel_map() -> Dict[int, List[ChannelMapEntry]]:
        result = await store.channel_map()
        return {int(universe): [ChannelMapEntry(**entry) for entry in entries] for universe, entries in result.items()}

    @app.post(
        "/mappings",
        dependencies=[Depends(auth_dependency)],
        response_model=MappingOut | list[MappingOut],
        status_code=status.HTTP_201_CREATED,
    )
    async def create_mapping(payload: MappingCreate) -> MappingOut | list[MappingOut]:
        try:
            if payload.template:
                start_channel = payload.start_channel or payload.channel
                if start_channel is None:
                    raise ValueError("Start channel is required when using a template")
                rows = await store.create_template_mappings(
                    device_id=payload.device_id,
                    universe=payload.universe,
                    start_channel=start_channel,
                    template=payload.template,
                    allow_overlap=payload.allow_overlap,
                )
                result = [MappingOut(**row.__dict__) for row in rows]
                # Publish event for each created mapping
                if event_bus:
                    for row in rows:
                        await event_bus.publish(EVENT_MAPPING_CREATED, {"mapping_id": row.id, "universe": row.universe, "channel": row.channel, "device_id": row.device_id, "field": row.field, "fields": list(row.fields)})
                return result
            channel = payload.channel or payload.start_channel
            if channel is None:
                raise ValueError("Channel is required")
            length = payload.length if payload.length is not None else 1

            # Infer mapping type: if field is provided, default to discrete; otherwise range
            mapping_type = payload.mapping_type
            if mapping_type is None:
                mapping_type = "discrete" if payload.field else "range"

            row = await store.create_mapping(
                device_id=payload.device_id,
                universe=payload.universe,
                channel=channel,
                length=length,
                mapping_type=mapping_type,
                field=payload.field,
                allow_overlap=payload.allow_overlap,
            )
            result = MappingOut(**row.__dict__)
            # Publish event after successfully creating mapping
            if event_bus:
                await event_bus.publish(EVENT_MAPPING_CREATED, {"mapping_id": row.id, "universe": row.universe, "channel": row.channel, "device_id": row.device_id, "field": row.field, "fields": list(row.fields)})
            return result
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    @app.get("/mappings/{mapping_id}", dependencies=[Depends(auth_dependency)], response_model=MappingOut)
    async def get_mapping(mapping_id: int) -> MappingOut:
        row = await store.mapping_by_id(mapping_id)
        if not row:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Mapping not found")
        return MappingOut(**row.__dict__)

    @app.put("/mappings/{mapping_id}", dependencies=[Depends(auth_dependency)], response_model=MappingOut)
    async def update_mapping(mapping_id: int, payload: MappingUpdate) -> MappingOut:
        try:
            row = await store.update_mapping(
                mapping_id,
                device_id=payload.device_id,
                universe=payload.universe,
                channel=payload.channel,
                length=payload.length,
                mapping_type=payload.mapping_type,
                field=payload.field,
                allow_overlap=payload.allow_overlap,
            )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        if not row:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Mapping not found")
        result = MappingOut(**row.__dict__)
        # Publish event after successfully updating mapping
        if event_bus:
            await event_bus.publish(EVENT_MAPPING_UPDATED, {"mapping_id": row.id, "universe": row.universe})
        return result

    @app.delete("/mappings/{mapping_id}", dependencies=[Depends(auth_dependency)], status_code=status.HTTP_204_NO_CONTENT)
    async def delete_mapping(mapping_id: int):
        # Get mapping details before deletion for event publishing
        mapping = await store.mapping_by_id(mapping_id)
        if not mapping:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Mapping not found")

        deleted = await store.delete_mapping(mapping_id)
        if not deleted:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Mapping not found")

        # Publish event after successfully deleting mapping
        if event_bus:
            await event_bus.publish(EVENT_MAPPING_DELETED, {"mapping_id": mapping_id, "universe": mapping.universe})

    @app.post("/reload", dependencies=[Depends(auth_dependency)], status_code=status.HTTP_202_ACCEPTED)
    async def reload_config() -> dict[str, str]:
        if not reload_callback:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Reload handler unavailable",
            )
        result = reload_callback()
        if inspect.isawaitable(result):
            await result
        return {"status": "reload_requested"}

    @app.get("/logs", dependencies=[Depends(auth_dependency)])
    async def get_logs(
        lines: int = 100,
        level: Optional[str] = None,
        logger: Optional[str] = None,
        offset: int = 0,
    ) -> dict[str, Any]:
        """
        Get recent log entries from buffer.

        Args:
            lines: Number of log lines to return (default: 100, max: 10000)
            level: Filter by log level (DEBUG, INFO, WARNING, ERROR)
            logger: Filter by logger name (e.g., 'govee.discovery')
            offset: Skip first N lines (for pagination)

        Returns:
            Dictionary with total count, offset, lines, and log entries
        """
        if log_buffer is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Log buffer not available (log_buffer_enabled=false in config)",
            )

        # Validate parameters
        if lines < 1 or lines > 10000:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="lines must be between 1 and 10000",
            )
        if offset < 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="offset must be non-negative",
            )

        # Query log buffer
        entries, total = await log_buffer.query(
            lines=lines,
            level=level,
            logger=logger,
            offset=offset,
        )

        return {
            "total": total,
            "offset": offset,
            "lines": len(entries),
            "logs": [entry.to_dict() for entry in entries],
        }

    @app.get("/logs/search", dependencies=[Depends(auth_dependency)])
    async def search_logs(
        pattern: str,
        lines: int = 100,
        regex: bool = False,
        case_sensitive: bool = False,
    ) -> dict[str, Any]:
        """
        Search logs by pattern.

        Args:
            pattern: Search pattern (string or regex if regex=true)
            lines: Max results to return (default: 100, max: 10000)
            regex: Use regex matching
            case_sensitive: Case-sensitive search

        Returns:
            Dictionary with count and matching log entries
        """
        if log_buffer is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Log buffer not available (log_buffer_enabled=false in config)",
            )

        # Validate parameters
        if lines < 1 or lines > 10000:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="lines must be between 1 and 10000",
            )

        # Search log buffer
        entries = await log_buffer.search(
            pattern=pattern,
            regex=regex,
            case_sensitive=case_sensitive,
            max_results=lines,
        )

        return {
            "count": len(entries),
            "pattern": pattern,
            "regex": regex,
            "case_sensitive": case_sensitive,
            "logs": [entry.to_dict() for entry in entries],
        }

    @app.websocket("/logs/stream")
    async def stream_logs(websocket: WebSocket) -> None:
        """
        Stream logs in real-time via WebSocket.

        Client can send filter updates:
        {"level": "INFO", "logger": "govee.discovery"}

        Server sends log entries:
        {
            "timestamp": "2025-12-26T10:30:45.123Z",
            "level": "INFO",
            "logger": "govee.discovery",
            "message": "Device discovered",
            "extra": {...}
        }
        """
        if log_buffer is None:
            await websocket.close(code=1011, reason="Log buffer not available")
            return

        await websocket.accept()

        # Track filters set by client
        level_filter: Optional[str] = None
        logger_filter: Optional[str] = None

        # Subscriber callback
        async def send_log(entry: Any) -> None:
            # Apply filters
            if level_filter and entry.level != level_filter:
                return
            if logger_filter and not entry.logger.startswith(logger_filter):
                return

            try:
                await websocket.send_json(entry.to_dict())
            except Exception:
                # Client disconnected, will be handled by main loop
                pass

        # Subscribe to log buffer
        unsubscribe = await log_buffer.subscribe(send_log)

        try:
            while True:
                # Wait for client messages (filter updates or ping)
                try:
                    message = await asyncio.wait_for(websocket.receive_json(), timeout=30.0)

                    # Update filters if provided
                    if "level" in message:
                        level_filter = message["level"]
                    if "logger" in message:
                        logger_filter = message["logger"]

                except asyncio.TimeoutError:
                    # Send ping to keep connection alive
                    await websocket.send_json({"type": "ping"})

        except WebSocketDisconnect:
            logger.info("Log stream client disconnected")
        except Exception as exc:
            logger.warning("Log stream error", extra={"error": str(exc)})
        finally:
            unsubscribe()

    @app.websocket("/events/stream")
    async def stream_events(websocket: WebSocket) -> None:
        """
        Stream system events in real-time via WebSocket.

        Server sends events:
        {
            "event": "device_discovered",
            "timestamp": "2025-12-26T10:30:45.123Z",
            "data": {...}
        }
        """
        if event_bus is None:
            await websocket.close(code=1011, reason="Event bus not available")
            return

        await websocket.accept()

        # Subscriber callback
        async def send_event(event: Any) -> None:
            try:
                await websocket.send_json(event.to_dict())
            except Exception:
                # Client disconnected, will be handled by main loop
                pass

        # Subscribe to all events (wildcard)
        unsubscribe = await event_bus.subscribe("*", send_event)

        try:
            while True:
                # Wait for client messages (ping/pong)
                try:
                    message = await asyncio.wait_for(websocket.receive_json(), timeout=30.0)
                    # Echo back pings
                    if message.get("type") == "ping":
                        await websocket.send_json({"type": "pong"})

                except asyncio.TimeoutError:
                    # Send ping to keep connection alive
                    await websocket.send_json({"type": "ping"})

        except WebSocketDisconnect:
            logger.info("Event stream client disconnected")
        except Exception as exc:
            logger.warning("Event stream error", extra={"error": str(exc)})
        finally:
            unsubscribe()

    return app


class ApiService:
    """Lifecycle wrapper for the FastAPI/uvicorn server."""

    def __init__(
        self,
        config: Config,
        store: DeviceStore,
        health: Optional[HealthMonitor] = None,
        reload_callback: Optional[Callable[[], Awaitable[None]]] = None,
        log_buffer: Optional[Any] = None,
        event_bus: Optional[Any] = None,
        sender_provider: Optional[Callable[[], Any]] = None,
    ) -> None:
        self.config = config
        self.store = store
        self.health = health
        self._reload_callback = reload_callback
        self.log_buffer = log_buffer
        self.event_bus = event_bus
        self.sender_provider = sender_provider
        self.logger = get_logger("artnet.api")
        self._server: Optional[uvicorn.Server] = None
        self._server_task: Optional[Any] = None

    async def start(self) -> None:
        if self._server:
            return
        app = create_app(
            self.config,
            self.store,
            self.health,
            reload_callback=self._reload_callback,
            log_buffer=self.log_buffer,
            event_bus=self.event_bus,
            sender_provider=self.sender_provider,
        )
        uvicorn_config = uvicorn.Config(
            app,
            host="0.0.0.0",
            port=self.config.api_port,
            log_config=None,
            loop="asyncio",
        )
        self._server = uvicorn.Server(config=uvicorn_config)
        self._server_task = asyncio.create_task(self._server.serve())
        if self.health:
            await self.health.record_success("api")
        self.logger.info("API server starting", extra={"port": self.config.api_port})

    async def stop(self) -> None:
        if not self._server:
            return
        self.logger.info("Stopping API server")
        self._server.should_exit = True
        if self._server_task:
            await self._server_task
        self._server = None
        self._server_task = None
