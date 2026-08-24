"""Device persistence helpers."""

from __future__ import annotations

import base64
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, MutableMapping, Optional, Sequence, Set, Tuple

from .capabilities import (
    CapabilityCache,
    CapabilityCatalog,
    CapabilityProvider,
    NormalizedCapabilities,
    validate_mapping_mode,
)
from .config import ManualDevice
from .db import DatabaseManager
from .logging import get_logger
from .metrics import (
    set_offline_devices,
    set_poll_health_devices,
    set_queue_depth,
    set_total_queue_depth,
)

# Import EventBus type for type hints (avoid circular import at runtime)
if False:  # TYPE_CHECKING
    from .events import EventBus


def _now_iso() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


def _serialize_capabilities(value: Any) -> Optional[str]:
    if value is None:
        return None
    # Handle binary payloads (e.g., LIFX binary packets)
    if isinstance(value, bytes):
        # Encode as base64 with a special prefix for detection
        encoded = base64.b64encode(value).decode('ascii')
        return f"base64:{encoded}"
    if isinstance(value, (dict, list)):
        try:
            return json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(value)
    return str(value)


def _deserialize_capabilities(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _serialize_fields(fields: Iterable[str]) -> str:
    return json.dumps([field for field in fields])


def _deserialize_fields(value: Any) -> Tuple[str, ...]:
    if value is None:
        return tuple()
    data: Any = value
    if isinstance(value, str):
        try:
            data = json.loads(value)
        except json.JSONDecodeError:
            return tuple()
    if isinstance(data, Iterable) and not isinstance(data, (str, bytes)):
        result = []
        for item in data:
            if isinstance(item, str) and item.strip():
                result.append(item.strip().lower())
        return tuple(result)
    return tuple()


def _merge_capability_mappings(current: Any, incoming: Any) -> Any:
    """Merge capability mappings while preserving protocol-specific extensions."""
    if not isinstance(current, Mapping):
        return incoming
    if not isinstance(incoming, Mapping):
        return incoming if incoming is not None else current

    merged: Dict[str, Any] = dict(current)
    for key, value in incoming.items():
        if key == "lifx" and isinstance(value, Mapping):
            existing = merged.get(key)
            if isinstance(existing, Mapping):
                nested = dict(existing)
                nested.update(value)
                merged[key] = nested
                continue
        merged[key] = value
    return merged


def wrap_govee_command(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    """Wrap a device state payload in the Govee LAN API message format.

    Uses specific Govee command types based on payload contents:
    - "brightness" cmd for brightness-only changes
    - "colorwc" cmd for color/color_temp changes
    - "turn" cmd for power on/off changes
    - For combined color+brightness, returns multiple commands via "_multiple" key

    Transforms:
        {"color": {"r": 154, "g": 0, "b": 0}}
    Into:
        {"msg": {"cmd": "colorwc", "data": {"color": {"r": 154, "g": 0, "b": 0}}}}

    For combined updates:
        {"color": {"r": 154, "g": 0, "b": 0}, "brightness": 200}
    Into:
        {"_multiple": [
            {"msg": {"cmd": "colorwc", "data": {"color": {"r": 154, "g": 0, "b": 0}}}},
            {"msg": {"cmd": "brightness", "data": {"value": 200}}}
        ]}
    """
    # If already wrapped, return as-is
    if "msg" in payload:
        return payload

    # Determine the appropriate command type based on payload contents
    has_color = "color" in payload
    has_color_temp = "color_temp" in payload or "colorTemInKelvin" in payload
    has_brightness = "brightness" in payload
    has_turn = "turn" in payload

    # Handle power/turn commands
    if has_turn:
        # Convert "on"/"off" to 1/0 as required by Govee API
        turn_value = 1 if payload["turn"] == "on" else 0
        turn_cmd = {
            "msg": {
                "cmd": "turn",
                "data": {"value": turn_value}
            }
        }

        # When turning off, only send the turn command
        if turn_value == 0:
            return turn_cmd

        # When turning on, send additional commands alongside turn
        additional_cmds = []

        # Add color/colorwc command if present
        if has_color or has_color_temp:
            data: Dict[str, Any] = {}
            if has_color:
                data["color"] = payload["color"]
            if "color_temp" in payload:
                data["colorTemInKelvin"] = payload["color_temp"]
            elif "colorTemInKelvin" in payload:
                data["colorTemInKelvin"] = payload["colorTemInKelvin"]
            additional_cmds.append({
                "msg": {
                    "cmd": "colorwc",
                    "data": data
                }
            })

        # Add brightness command if present
        if has_brightness:
            additional_cmds.append({
                "msg": {
                    "cmd": "brightness",
                    "data": {"value": payload["brightness"]}
                }
            })

        # Return turn with additional commands if any
        if additional_cmds:
            return {"_multiple": [turn_cmd] + additional_cmds}
        return turn_cmd

    # Brightness-only command (no color or color_temp)
    if has_brightness and not has_color and not has_color_temp:
        return {
            "msg": {
                "cmd": "brightness",
                "data": {"value": payload["brightness"]}
            }
        }

    # Color/colorwc command
    if has_color or has_color_temp:
        data: Dict[str, Any] = {}
        if has_color:
            data["color"] = payload["color"]
        if "color_temp" in payload:
            data["colorTemInKelvin"] = payload["color_temp"]
        elif "colorTemInKelvin" in payload:
            data["colorTemInKelvin"] = payload["colorTemInKelvin"]

        colorwc_cmd = {
            "msg": {
                "cmd": "colorwc",
                "data": data
            }
        }

        # If brightness is also present, send it as a separate command
        if has_brightness:
            brightness_cmd = {
                "msg": {
                    "cmd": "brightness",
                    "data": {"value": payload["brightness"]}
                }
            }
            return {"_multiple": [colorwc_cmd, brightness_cmd]}

        return colorwc_cmd

    # Fallback for any other payload (shouldn't normally happen)
    return {
        "msg": {
            "cmd": "devControl",
            "data": dict(payload)
        }
    }


def _coerce_mode_for_mapping(capabilities: Any, length: int) -> str:
    default_mode = "rgbw" if length >= 4 else "rgb" if length >= 3 else "brightness"
    if isinstance(capabilities, Mapping):
        mode = str(capabilities.get("mode", default_mode)).lower()
        if mode in {"rgb", "rgbw", "brightness", "custom"}:
            return mode
    return default_mode


def _coerce_order_for_mapping(capabilities: Any, mode: str) -> Tuple[str, ...]:
    def _normalize_entry(entry: str) -> Optional[str]:
        value = entry.strip().lower()
        if value in {"r", "g", "b", "dimmer"}:
            return value
        return None

    default_orders: Dict[str, Tuple[str, ...]] = {
        "rgb": ("r", "g", "b"),
        "rgbw": ("r", "g", "b"),
        "brightness": ("dimmer",),
    }
    if isinstance(capabilities, Mapping):
        order_value = capabilities.get("order") or capabilities.get("channel_order")
        if isinstance(order_value, str):
            parsed = tuple(
                entry for entry in (_normalize_entry(ch) for ch in order_value) if entry
            )
            if parsed:
                return parsed
        if isinstance(order_value, Iterable) and not isinstance(order_value, (str, bytes)):
            parsed_list = []
            for item in order_value:
                if not isinstance(item, str):
                    continue
                normalized = _normalize_entry(item)
                if normalized:
                    parsed_list.append(normalized)
            if parsed_list:
                return tuple(parsed_list)
    return default_orders.get(mode, default_orders["brightness"])


def _required_channels(capabilities: Any, length: int) -> int:
    mode = _coerce_mode_for_mapping(capabilities, length)
    order = _coerce_order_for_mapping(capabilities, mode)
    return len(order) if mode != "custom" else length


def _coerce_optional_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_optional_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_optional_bool(value: Any) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    return bool(value)


_METADATA_FIELDS = (
    "device_type",
    "length_meters",
    "led_count",
    "led_density_per_meter",
    "has_zones",
    "zone_count",
)


def _extract_metadata(source: Any) -> Dict[str, Any]:
    metadata: Dict[str, Any] = {}
    if source is None:
        return metadata
    if hasattr(source, "metadata") and isinstance(getattr(source, "metadata"), Mapping):
        metadata.update(_extract_metadata(getattr(source, "metadata")))

    def _assign(key: str, value: Any) -> None:
        if value is None:
            return
        if key in {"length_meters", "led_density_per_meter"}:
            coerced = _coerce_optional_float(value)
        elif key in {"led_count", "zone_count"}:
            coerced = _coerce_optional_int(value)
        elif key == "has_zones":
            coerced = _coerce_optional_bool(value)
        else:
            coerced = str(value)
        if coerced is not None:
            metadata[key] = coerced

    if isinstance(source, Mapping):
        for key in _METADATA_FIELDS:
            if key in source or key in {"lengthMeters", "ledCount", "ledDensityPerMeter", "hasZones", "zoneCount"}:
                lookup = {
                    "length_meters": source.get("length_meters", source.get("lengthMeters")),
                    "led_count": source.get("led_count", source.get("ledCount")),
                    "led_density_per_meter": source.get(
                        "led_density_per_meter", source.get("ledDensityPerMeter")
                    ),
                    "has_zones": source.get("has_zones", source.get("hasZones")),
                    "zone_count": source.get("zone_count", source.get("zoneCount")),
                    "device_type": source.get("device_type"),
                }
                _assign(key, lookup.get(key))
    else:
        for key in _METADATA_FIELDS:
            if hasattr(source, key):
                _assign(key, getattr(source, key))
    return metadata


def _merge_metadata(*sources: Any) -> Dict[str, Any]:
    merged: Dict[str, Any] = {}
    for source in sources:
        merged.update(_extract_metadata(source))
    return merged


def _coerce_metadata_for_db(metadata: Mapping[str, Any]) -> Dict[str, Any]:
    db_values: Dict[str, Any] = {}
    for key in _METADATA_FIELDS:
        if key not in metadata:
            continue
        value = metadata.get(key)
        if value is None:
            continue
        if key == "device_type":
            db_values[key] = str(value)
        elif key in {"length_meters", "led_density_per_meter"}:
            coerced = _coerce_optional_float(value)
            if coerced is not None:
                db_values[key] = coerced
        elif key in {"led_count", "zone_count"}:
            coerced = _coerce_optional_int(value)
            if coerced is not None:
                db_values[key] = coerced
        elif key == "has_zones":
            coerced_bool = _coerce_optional_bool(value)
            if coerced_bool is not None:
                db_values[key] = 1 if coerced_bool else 0
    return db_values


SUPPORTED_FIELDS: Set[str] = {"r", "g", "b", "dimmer", "ct", "power"}

# Field aliases for user convenience
FIELD_ALIASES: Dict[str, str] = {
    "red": "r",
    "green": "g",
    "blue": "b",
    "color_temp": "ct",
}


@dataclass(frozen=True)
class TemplateSegment:
    """Describes a single segment of a template expansion."""

    kind: str  # "range" or "discrete"
    fields: Tuple[str, ...]

    @property
    def length(self) -> int:
        return len(self.fields)


_TEMPLATE_CATALOGUE: Dict[str, Tuple[TemplateSegment, ...]] = {
    "RGB": (TemplateSegment("range", ("r", "g", "b")),),
    "RGBCT": (
        TemplateSegment("range", ("r", "g", "b")),
        TemplateSegment("discrete", ("ct",)),
    ),
    "DIMRGB": (
        TemplateSegment("discrete", ("dimmer",)),
        TemplateSegment("range", ("r", "g", "b")),
    ),
    "DIMMER_RGB": (
        TemplateSegment("discrete", ("dimmer",)),
        TemplateSegment("range", ("r", "g", "b")),
    ),
    "DIMRGBCT": (
        TemplateSegment("discrete", ("dimmer",)),
        TemplateSegment("range", ("r", "g", "b")),
        TemplateSegment("discrete", ("ct",)),
    ),
    "DIMCT": (
        TemplateSegment("discrete", ("dimmer",)),
        TemplateSegment("discrete", ("ct",)),
    ),
}


def _template_segments(name: str) -> Tuple[TemplateSegment, ...]:
    normalized = name.strip().upper()
    if normalized not in _TEMPLATE_CATALOGUE:
        raise ValueError(
            f"Unknown template '{name}'. Supported templates: {', '.join(sorted(_TEMPLATE_CATALOGUE))}."
        )
    return _TEMPLATE_CATALOGUE[normalized]


def _validate_template_support(
    segments: Tuple[TemplateSegment, ...], capabilities: NormalizedCapabilities, template_name: str
) -> None:
    needs_brightness = any("brightness" in segment.fields for segment in segments)
    needs_color = any(field in {"r", "g", "b"} for segment in segments for field in segment.fields)
    needs_color_temp = any("ct" in segment.fields for segment in segments)
    errors = []
    if needs_brightness and not capabilities.supports_brightness:
        errors.append("brightness")
    if needs_color and not capabilities.supports_color:
        errors.append("color")
    if needs_color_temp and not capabilities.supports_color_temperature:
        errors.append("color temperature")
    if errors:
        supported = capabilities.describe_support()
        raise ValueError(
            f"Template '{template_name}' is incompatible with this device "
            f"(missing {', '.join(errors)} support; supported: {supported})."
        )


def _normalize_mapping_type(mapping_type: Optional[str]) -> str:
    normalized = (mapping_type or "range").strip().lower()
    if normalized not in {"range", "discrete"}:
        raise ValueError("Mapping type must be 'range' or 'discrete'")
    return normalized


def _normalize_field_name(field: Optional[str]) -> str:
    if field is None or not str(field).strip():
        raise ValueError("Field is required for discrete mappings")
    normalized = str(field).strip().lower()
    # Apply field aliases
    normalized = FIELD_ALIASES.get(normalized, normalized)
    if normalized not in SUPPORTED_FIELDS:
        raise ValueError(
            f"Unsupported field '{field}'. Supported fields: {', '.join(sorted(SUPPORTED_FIELDS))}."
        )
    return normalized


def _validate_field_support(field: str, capabilities: NormalizedCapabilities) -> None:
    if field == "dimmer" and not capabilities.supports_brightness:
        raise ValueError("Device does not support brightness control.")
    if field in {"r", "g", "b"} and not capabilities.supports_color:
        supported = ", ".join(capabilities.supported_modes) or "none"
        raise ValueError(
            f"Device does not support color control. Supported modes: {supported}."
        )
    if field == "ct" and not capabilities.supports_color_temperature:
        supported = ", ".join(capabilities.supported_modes) or "none"
        raise ValueError(
            f"Device does not support color temperature control. Supported modes: {supported}."
        )
    # Power control is assumed to be supported by all Govee devices
    if field == "power":
        pass  # No validation needed - all devices support on/off


def _mapping_fields_for_length(
    capabilities: Mapping[str, Any],
    normalized_capabilities: NormalizedCapabilities,
    mapping_type: str,
    length: int,
    field: Optional[str],
) -> Tuple[str, ...]:
    if mapping_type == "discrete":
        normalized_field = _normalize_field_name(field)
        _validate_field_support(normalized_field, normalized_capabilities)
        return (normalized_field,)
    mode = _coerce_mode_for_mapping(capabilities, length)
    order = _coerce_order_for_mapping(capabilities, mode)
    return order


@dataclass(frozen=True)
class DiscoveryResult:
    """Parsed discovery response details."""

    id: str
    ip: str
    protocol: str = "govee"
    name: Optional[str] = None
    model_number: Optional[str] = None
    device_type: Optional[str] = None
    length_meters: Optional[float] = None
    led_count: Optional[int] = None
    led_density_per_meter: Optional[float] = None
    has_zones: Optional[bool] = None
    zone_count: Optional[int] = None
    description: Optional[str] = None
    capabilities: Any = None
    manual: bool = False


@dataclass(frozen=True)
class MappingRecord:
    """Persisted mapping between an ArtNet channel slice and a device."""

    device_id: str
    universe: int
    channel: int
    length: int
    mapping_type: str
    field: Optional[str]
    fields: Tuple[str, ...]
    capabilities: Any


@dataclass(frozen=True)
class DeviceStateUpdate:
    """Pending payload to be sent to a device."""

    device_id: str
    payload: Mapping[str, Any]
    context_id: Optional[str] = None


@dataclass(frozen=True)
class PendingState:
    """Queued state row ready for delivery."""

    id: int
    device_id: str
    payload: str
    created_at: str
    context_id: Optional[str]


@dataclass(frozen=True)
class DeadLetter:
    """State rows that could not be delivered."""

    id: int
    state_id: Optional[int]
    device_id: Optional[str]
    payload: str
    payload_hash: Optional[str]
    context_id: Optional[str]
    reason: Optional[str]
    details: Optional[str]
    state_created_at: Optional[str]
    created_at: str


@dataclass(frozen=True)
class DeviceInfo:
    """Metadata required for transport decisions and monitoring."""

    id: str
    ip: Optional[str]
    protocol: str
    capabilities: Any
    model_number: Optional[str]
    device_type: Optional[str]
    length_meters: Optional[float]
    led_count: Optional[int]
    led_density_per_meter: Optional[float]
    has_zones: Optional[bool]
    zone_count: Optional[int]
    normalized_capabilities: Optional[NormalizedCapabilities]
    offline: bool
    poll_health: str
    failure_count: int
    last_payload_hash: Optional[str]
    last_payload_at: Optional[str]
    last_failure_at: Optional[str]
    poll_last_success_at: Optional[str]
    poll_last_failure_at: Optional[str]
    poll_failure_count: int


@dataclass(frozen=True)
class DeviceRow:
    """Full device row for API exposure."""

    id: str
    ip: Optional[str]
    protocol: str
    name: Optional[str]
    model_number: Optional[str]
    device_type: Optional[str]
    length_meters: Optional[float]
    led_count: Optional[int]
    led_density_per_meter: Optional[float]
    has_zones: Optional[bool]
    zone_count: Optional[int]
    description: Optional[str]
    capabilities: Any
    manual: bool
    discovered: bool
    configured: bool
    enabled: bool
    stale: bool
    offline: bool
    poll_health: str
    last_seen: Optional[str]
    first_seen: Optional[str]
    poll_last_success_at: Optional[str]
    poll_last_failure_at: Optional[str]
    poll_failure_count: int
    poll_state: Any
    poll_state_updated_at: Optional[str]
    mapping_count: int
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class PollTarget:
    """Device metadata needed for poll requests."""

    id: str
    ip: str
    protocol: str
    port: int
    model_number: Optional[str]
    device_type: Optional[str]
    length_meters: Optional[float]
    led_count: Optional[int]
    led_density_per_meter: Optional[float]
    has_zones: Optional[bool]
    zone_count: Optional[int]
    capabilities: Any
    offline: bool
    poll_failure_count: int


@dataclass(frozen=True)
class MappingRow:
    """Mapping row with primary key for management APIs."""

    id: int
    device_id: str
    universe: int
    channel: int
    length: int
    mapping_type: str
    field: Optional[str]
    fields: Tuple[str, ...]
    created_at: str
    updated_at: str


class DeviceStore:
    """SQLite-backed persistence for device metadata."""

    def __init__(
        self,
        db_path: Path,
        event_bus: Optional[Any] = None,
        capability_catalog: Optional[CapabilityCatalog] = None,
    ) -> None:
        self.db = DatabaseManager(db_path)
        self.logger = get_logger("devices.store")
        self._event_bus = event_bus
        self._capability_catalog = capability_catalog
        # Cache capability caches per protocol
        self._capability_caches: Dict[str, CapabilityCache] = {}

    def _get_capability_cache(self, protocol: str) -> CapabilityCache:
        """Get or create capability cache for a protocol.

        Args:
            protocol: Protocol name (e.g., 'govee', 'lifx')

        Returns:
            CapabilityCache instance for this protocol
        """
        if protocol not in self._capability_caches:
            provider: Optional[CapabilityProvider] = None
            if self._capability_catalog is not None:
                class _InjectedCatalogProvider(CapabilityProvider):
                    def __init__(self, catalog: CapabilityCatalog):
                        self._catalog = catalog

                    def get_capabilities(self, model: Optional[str]) -> Optional[Mapping[str, Any]]:
                        entry = self._catalog.lookup(model) if self._catalog else None
                        return dict(entry.capabilities) if entry else None

                    def get_metadata(self, model: Optional[str]) -> Optional[Mapping[str, Any]]:
                        entry = self._catalog.lookup(model) if self._catalog else None
                        return dict(entry.metadata) if entry else None

                provider = _InjectedCatalogProvider(self._capability_catalog)

            if provider is None:
                from .protocol import get_protocol_handler
                handler = get_protocol_handler(protocol)
                provider = handler.get_capability_provider()
            self._capability_caches[protocol] = CapabilityCache(provider)
        return self._capability_caches[protocol]

    async def start(self) -> None:
        await self.db.start_integrity_checks()

    async def stop(self) -> None:
        await self.db.close()

    async def devices(self) -> List[DeviceRow]:
        return await self.db.run(self._devices)

    async def device_id_by_ip(self, ip: str) -> Optional[str]:
        """Look up a device id by its last known IP address."""
        return await self.db.run(lambda conn: self._device_id_by_ip(conn, ip))

    def _devices(self, conn: sqlite3.Connection) -> List[DeviceRow]:
        rows = conn.execute(
            """
            SELECT
                d.id,
                d.ip,
                d.protocol,
                d.name,
                d.model,
                d.model_number,
                d.device_type,
                d.length_meters,
                d.led_count,
                d.led_density_per_meter,
                d.has_zones,
                d.zone_count,
                d.description,
                d.capabilities,
                d.manual,
                d.discovered,
                d.configured,
                d.enabled,
                d.stale,
                d.offline,
                d.poll_health,
                d.last_seen,
                d.first_seen,
                d.poll_last_success_at,
                d.poll_last_failure_at,
                d.poll_failure_count,
                d.poll_state,
                d.poll_state_updated_at,
                COALESCE(COUNT(m.id), 0) as mapping_count,
                d.created_at,
                d.updated_at
            FROM devices d
            LEFT JOIN mappings m ON d.id = m.device_id
            GROUP BY d.id
            ORDER BY d.created_at ASC
            """
        ).fetchall()
        return [self._row_to_device(row) for row in rows]

    def _device_id_by_ip(self, conn: sqlite3.Connection, ip: str) -> Optional[str]:
        row = conn.execute(
            """
            SELECT id
            FROM devices
            WHERE ip = ?
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (ip,),
        ).fetchone()
        return row["id"] if row else None

    async def device(self, device_id: str) -> Optional[DeviceRow]:
        return await self.db.run(lambda conn: self._device(conn, device_id))

    def _device(self, conn: sqlite3.Connection, device_id: str) -> Optional[DeviceRow]:
        row = conn.execute(
            """
            SELECT
                d.id,
                d.ip,
                d.protocol,
                d.name,
                d.model,
                d.model_number,
                d.device_type,
                d.length_meters,
                d.led_count,
                d.led_density_per_meter,
                d.has_zones,
                d.zone_count,
                d.description,
                d.capabilities,
                d.manual,
                d.discovered,
                d.configured,
                d.enabled,
                d.stale,
                d.offline,
                d.poll_health,
                d.last_seen,
                d.first_seen,
                d.poll_last_success_at,
                d.poll_last_failure_at,
                d.poll_failure_count,
                d.poll_state,
                d.poll_state_updated_at,
                COALESCE(COUNT(m.id), 0) as mapping_count,
                d.created_at,
                d.updated_at
            FROM devices d
            LEFT JOIN mappings m ON d.id = m.device_id
            WHERE d.id = ?
            GROUP BY d.id
            """,
            (device_id,),
        ).fetchone()
        if not row:
            return None
        return self._row_to_device(row)

    async def create_manual_device(self, manual: ManualDevice) -> DeviceRow:
        return await self.db.run(lambda conn: self._create_manual_device(conn, manual))

    def _create_manual_device(self, conn: sqlite3.Connection, manual: ManualDevice) -> DeviceRow:
        self._upsert_manual(conn, manual)
        conn.commit()
        row = conn.execute(
            """
            SELECT
                id,
                ip,
                protocol,
                name,
                model,
                model_number,
                device_type,
                length_meters,
                led_count,
                led_density_per_meter,
                has_zones,
                zone_count,
                description,
                capabilities,
                manual,
                discovered,
                configured,
                enabled,
                stale,
                offline,
                poll_health,
                last_seen,
                first_seen,
                poll_last_success_at,
                poll_last_failure_at,
                poll_failure_count,
                poll_state,
                poll_state_updated_at,
                created_at,
                updated_at
            FROM devices
            WHERE id = ?
            """,
            (manual.id,),
        ).fetchone()
        if not row:
            raise ValueError("Failed to create device")
        self.logger.info("Created manual device", extra={"id": manual.id, "ip": manual.ip})
        return self._row_to_device(row)

    async def update_device(
        self,
        device_id: str,
        *,
        ip: Optional[str] = None,
        name: Optional[str] = None,
        model_number: Optional[str] = None,
        device_type: Optional[str] = None,
        length_meters: Optional[float] = None,
        led_count: Optional[int] = None,
        led_density_per_meter: Optional[float] = None,
        has_zones: Optional[bool] = None,
        zone_count: Optional[int] = None,
        description: Optional[str] = None,
        capabilities: Optional[Any] = None,
        enabled: Optional[bool] = None,
    ) -> Optional[DeviceRow]:
        return await self.db.run(
            lambda conn: self._update_device(
                conn,
                device_id,
                ip,
                name,
                model_number,
                device_type,
                length_meters,
                led_count,
                led_density_per_meter,
                has_zones,
                zone_count,
                description,
                capabilities,
                enabled,
            )
        )

    def _update_device(
        self,
        conn: sqlite3.Connection,
        device_id: str,
        ip: Optional[str],
        name: Optional[str],
        model_number: Optional[str],
        device_type: Optional[str],
        length_meters: Optional[float],
        led_count: Optional[int],
        led_density_per_meter: Optional[float],
        has_zones: Optional[bool],
        zone_count: Optional[int],
        description: Optional[str],
        capabilities: Optional[Any],
        enabled: Optional[bool],
    ) -> Optional[DeviceRow]:
        row = conn.execute(
            "SELECT * FROM devices WHERE id = ?",
            (device_id,),
        ).fetchone()
        if not row:
            return None
        # Get protocol from row, defaulting to 'govee' for backwards compatibility
        protocol = row["protocol"] if "protocol" in row.keys() else "govee"
        cache = self._get_capability_cache(protocol)

        metadata_input = {
            "device_type": device_type,
            "length_meters": length_meters,
            "led_count": led_count,
            "led_density_per_meter": led_density_per_meter,
            "has_zones": has_zones,
            "zone_count": zone_count,
        }
        model_hint = model_number or row["model_number"] or row["model"]
        normalized = None
        capabilities_source = capabilities
        if capabilities_source is None:
            capabilities_source = _deserialize_capabilities(row["capabilities"])
        has_metadata_input = any(value is not None for value in metadata_input.values())
        if capabilities_source is not None or has_metadata_input or cache.has_provider_entry(model_hint):
            normalized = cache.normalize(
                model_hint, capabilities_source, metadata=metadata_input if has_metadata_input else None
            )
        serialized_caps = (
            _serialize_capabilities(normalized.as_mapping())
            if normalized is not None
            else row["capabilities"]
        )
        merged_metadata = _merge_metadata(normalized.metadata if normalized else {}, metadata_input)
        db_metadata = _coerce_metadata_for_db(merged_metadata)
        model_value = model_number or (normalized.model_number if normalized else None)
        conn.execute(
            """
            UPDATE devices
            SET
                ip = COALESCE(?, ip),
                name = COALESCE(?, name),
                model = COALESCE(?, model),
                model_number = COALESCE(?, model_number, model),
                device_type = COALESCE(?, device_type),
                length_meters = COALESCE(?, length_meters),
                led_count = COALESCE(?, led_count),
                led_density_per_meter = COALESCE(?, led_density_per_meter),
                has_zones = COALESCE(?, has_zones),
                zone_count = COALESCE(?, zone_count),
                description = COALESCE(?, description),
                capabilities = ?,
                enabled = COALESCE(?, enabled)
            WHERE id = ?
            """,
            (
                ip,
                name,
                model_value,
                model_value,
                db_metadata.get("device_type"),
                db_metadata.get("length_meters"),
                db_metadata.get("led_count"),
                db_metadata.get("led_density_per_meter"),
                db_metadata.get("has_zones"),
                db_metadata.get("zone_count"),
                description,
                serialized_caps,
                int(enabled) if enabled is not None else None,
                device_id,
            ),
        )
        conn.commit()
        updated = conn.execute(
            """
            SELECT
                id,
                ip,
                protocol,
                name,
                model,
                model_number,
                device_type,
                length_meters,
                led_count,
                led_density_per_meter,
                has_zones,
                zone_count,
                description,
                capabilities,
                manual,
                discovered,
                configured,
                enabled,
                stale,
                offline,
                poll_health,
                last_seen,
                first_seen,
                poll_last_success_at,
                poll_last_failure_at,
                poll_failure_count,
                poll_state,
                poll_state_updated_at,
                created_at,
                updated_at
            FROM devices
            WHERE id = ?
            """,
            (device_id,),
        ).fetchone()
        if not updated:
            return None
        self.logger.info(
            "Updated device",
            extra={"id": device_id, "enabled": enabled},
        )
        return self._row_to_device(updated)

    async def sync_manual_devices(self, devices: Sequence[ManualDevice]) -> None:
        if not devices:
            return
        await self.db.run(lambda conn: self._sync_manual_devices(conn, devices))

    def _sync_manual_devices(
        self, conn: sqlite3.Connection, devices: Sequence[ManualDevice]
    ) -> None:
        for device in devices:
            self._upsert_manual(conn, device)
        conn.commit()
        self.logger.info("Synced manual devices", extra={"count": len(devices)})

    def _upsert_manual(self, conn: sqlite3.Connection, device: ManualDevice) -> None:
        now = _now_iso()
        cache = self._get_capability_cache(device.protocol)
        metadata_input = _extract_metadata(device)
        normalized = None
        if (
            device.capabilities is not None
            or metadata_input
            or cache.has_provider_entry(device.model_number)
        ):
            normalized = cache.normalize(
                device.model_number, device.capabilities, metadata=metadata_input
            )
        capabilities = _serialize_capabilities(normalized.as_mapping()) if normalized else None
        model_number = device.model_number or (normalized.model_number if normalized else None)
        metadata = _coerce_metadata_for_db(_merge_metadata(normalized.metadata if normalized else {}, device))
        conn.execute(
            """
            INSERT INTO devices (
                id, ip, protocol, name, model, model_number, device_type, length_meters, led_count,
                led_density_per_meter, has_zones, zone_count, description, capabilities, manual,
                configured, enabled, discovered, first_seen, last_seen,
                stale, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 0, 1, 0, ?, NULL, 0, datetime('now'), datetime('now'))
            ON CONFLICT(id) DO UPDATE SET
                ip=excluded.ip,
                protocol=excluded.protocol,
                name=COALESCE(excluded.name, devices.name),
                model=COALESCE(excluded.model, devices.model),
                model_number=COALESCE(excluded.model_number, devices.model_number, devices.model),
                device_type=COALESCE(excluded.device_type, devices.device_type),
                length_meters=COALESCE(excluded.length_meters, devices.length_meters),
                led_count=COALESCE(excluded.led_count, devices.led_count),
                led_density_per_meter=COALESCE(
                    excluded.led_density_per_meter, devices.led_density_per_meter
                ),
                has_zones=COALESCE(excluded.has_zones, devices.has_zones),
                zone_count=COALESCE(excluded.zone_count, devices.zone_count),
                description=COALESCE(excluded.description, devices.description),
                capabilities=COALESCE(excluded.capabilities, devices.capabilities),
                manual=1,
                configured=devices.configured,
                enabled=1
            """,
            (
                device.id,
                device.ip,
                device.protocol,
                None,
                model_number,
                model_number,
                metadata.get("device_type"),
                metadata.get("length_meters"),
                metadata.get("led_count"),
                metadata.get("led_density_per_meter"),
                metadata.get("has_zones"),
                metadata.get("zone_count"),
                device.description,
                capabilities,
                now,
            ),
        )

    async def record_discovery(self, result: DiscoveryResult) -> None:
        event_data = await self.db.run(lambda conn: self._record_discovery(conn, result))

        # Publish events if event_bus is available
        if self._event_bus and event_data:
            from .events import EVENT_DEVICE_DISCOVERED, EVENT_DEVICE_UPDATED

            is_new = event_data.get("is_new", False)
            changed_fields = event_data.get("changed_fields", [])

            if is_new:
                await self._event_bus.publish(EVENT_DEVICE_DISCOVERED, {
                    "device_id": result.id,
                    "ip": result.ip,
                    "model": event_data.get("model_number"),
                    "device_type": event_data.get("device_type"),
                    "capabilities": event_data.get("capabilities_list", []),
                    "is_new": True,
                })
            elif changed_fields:
                await self._event_bus.publish(EVENT_DEVICE_UPDATED, {
                    "device_id": result.id,
                    "changed_fields": changed_fields,
                    "ip": result.ip,
                })

    def _record_discovery(self, conn: sqlite3.Connection, result: DiscoveryResult) -> Optional[Dict[str, Any]]:
        now = _now_iso()

        # Check if device exists before upsert
        existing = conn.execute(
            "SELECT id, ip, model_number, model, capabilities FROM devices WHERE id = ?",
            (result.id,)
        ).fetchone()
        is_new = existing is None
        old_ip = existing["ip"] if existing else None

        cache = self._get_capability_cache(result.protocol)
        existing_capabilities = (
            _deserialize_capabilities(existing["capabilities"]) if existing and existing["capabilities"] else None
        )
        capabilities_input = result.capabilities
        if isinstance(existing_capabilities, Mapping) and isinstance(result.capabilities, Mapping):
            capabilities_input = _merge_capability_mappings(existing_capabilities, result.capabilities)
        elif result.capabilities is None:
            capabilities_input = existing_capabilities

        model_number_input = result.model_number or (existing["model_number"] if existing else None)
        if not model_number_input and existing and "model" in existing.keys():
            model_number_input = existing["model"]

        metadata_input = _extract_metadata(result)
        normalized = None
        if (
            capabilities_input is not None
            or metadata_input
            or cache.has_provider_entry(model_number_input)
        ):
            normalized = cache.normalize(
                model_number_input, capabilities_input, metadata=metadata_input
            )
        capabilities = _serialize_capabilities(normalized.as_mapping()) if normalized else None
        model_number = model_number_input or (normalized.model_number if normalized else None)
        metadata = _coerce_metadata_for_db(_merge_metadata(normalized.metadata if normalized else {}, result))

        conn.execute(
            """
            INSERT INTO devices (
                id, ip, protocol, name, model, model_number, device_type, length_meters, led_count,
                led_density_per_meter, has_zones, zone_count, description, capabilities, manual, discovered,
                configured, enabled, first_seen, last_seen, stale,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 0, 0, ?, ?, 0, datetime('now'), datetime('now'))
            ON CONFLICT(id) DO UPDATE SET
                ip=excluded.ip,
                protocol=excluded.protocol,
                name=COALESCE(excluded.name, devices.name),
                model=COALESCE(excluded.model, devices.model),
                model_number=COALESCE(excluded.model_number, devices.model_number, devices.model),
                device_type=COALESCE(excluded.device_type, devices.device_type),
                length_meters=COALESCE(excluded.length_meters, devices.length_meters),
                led_count=COALESCE(excluded.led_count, devices.led_count),
                led_density_per_meter=COALESCE(
                    excluded.led_density_per_meter, devices.led_density_per_meter
                ),
                has_zones=COALESCE(excluded.has_zones, devices.has_zones),
                zone_count=COALESCE(excluded.zone_count, devices.zone_count),
                description=COALESCE(excluded.description, devices.description),
                capabilities=COALESCE(excluded.capabilities, devices.capabilities),
                last_seen=excluded.last_seen,
                first_seen=COALESCE(devices.first_seen, excluded.last_seen),
                manual=excluded.manual OR devices.manual,
                discovered=1,
                configured=devices.configured,
                enabled=devices.enabled,
                stale=0
            """,
            (
                result.id,
                result.ip,
                result.protocol,
                result.name,
                model_number,
                model_number,
                metadata.get("device_type"),
                metadata.get("length_meters"),
                metadata.get("led_count"),
                metadata.get("led_density_per_meter"),
                metadata.get("has_zones"),
                metadata.get("zone_count"),
                result.description,
                capabilities,
                1 if result.manual else 0,
                now,
                now,
            ),
        )
        conn.commit()

        # Log device addition/update
        if is_new:
            self.logger.debug(
                "Added new device to database",
                extra={
                    "device_id": result.id,
                    "ip": result.ip,
                    "protocol": result.protocol,
                    "model_number": model_number,
                    "device_type": metadata.get("device_type"),
                    "manual": result.manual,
                },
            )
        else:
            self.logger.debug(
                "Updated existing device in database",
                extra={
                    "device_id": result.id,
                    "ip": result.ip,
                    "protocol": result.protocol,
                    "model_number": model_number,
                    "old_ip": old_ip,
                    "manual": result.manual,
                },
            )

        # Track what changed for event publishing
        changed_fields = []
        if not is_new and old_ip and old_ip != result.ip:
            changed_fields.append("ip")

        # Extract capabilities list for event data
        capabilities_list = []
        if normalized:
            cap_dict = normalized.as_mapping()
            if isinstance(cap_dict, dict):
                capabilities_list = [k for k, v in cap_dict.items() if v is True]

        return {
            "is_new": is_new,
            "changed_fields": changed_fields,
            "model_number": model_number,
            "device_type": metadata.get("device_type"),
            "capabilities_list": capabilities_list,
        }

    async def mark_stale(self, stale_after_seconds: float) -> None:
        await self.db.run(lambda conn: self._mark_stale(conn, stale_after_seconds))

    def _mark_stale(self, conn: sqlite3.Connection, stale_after_seconds: float) -> None:
        cursor = conn.execute(
            """
            UPDATE devices
            SET stale = 1
            WHERE last_seen IS NOT NULL
              AND stale = 0
              AND (julianday('now') - julianday(last_seen)) * 86400 > ?
            """,
            (stale_after_seconds,),
        )
        conn.commit()
        if cursor.rowcount:
            self.logger.info(
                "Marked devices stale",
                extra={"count": cursor.rowcount, "threshold": stale_after_seconds},
            )

    async def manual_probe_targets(self) -> List[Tuple[str, str]]:
        return await self.db.run(self._manual_probe_targets)

    def _manual_probe_targets(self, conn: sqlite3.Connection) -> List[Tuple[str, str]]:
        rows = conn.execute(
            """
            SELECT id, ip
            FROM devices
            WHERE manual = 1
              AND enabled = 1
              AND ip IS NOT NULL
            """
        ).fetchall()
        return [(row["id"], row["ip"]) for row in rows]

    async def poll_targets(self) -> List[PollTarget]:
        return await self.db.run(self._poll_targets)

    def _poll_targets(self, conn: sqlite3.Connection) -> List[PollTarget]:
        rows = conn.execute(
            """
            SELECT
                id,
                ip,
                protocol,
                model,
                model_number,
                device_type,
                length_meters,
                led_count,
                led_density_per_meter,
                has_zones,
                zone_count,
                capabilities,
                offline,
                poll_failure_count
            FROM devices
            WHERE enabled = 1
              AND ip IS NOT NULL
            """
        ).fetchall()
        targets: List[PollTarget] = []
        for row in rows:
            normalized = self._normalized_capabilities_obj(row)
            metadata = _merge_metadata(row, normalized.metadata)
            model_number = row["model_number"] if "model_number" in row.keys() else None
            if not model_number:
                model_number = row["model"]

            # Get protocol and determine port
            protocol = row["protocol"] if "protocol" in row.keys() else "govee"
            from .protocol import get_protocol_handler
            handler = get_protocol_handler(protocol)

            # Use protocol default port, but allow capability override
            port = handler.get_default_port()
            if isinstance(normalized.as_mapping(), Mapping):
                for key in ("port", "control_port", "device_port"):
                    if key in normalized.as_mapping():
                        try:
                            port = int(normalized.as_mapping()[key])
                            break
                        except (TypeError, ValueError):
                            continue

            targets.append(
                PollTarget(
                    id=row["id"],
                    ip=row["ip"],
                    protocol=protocol,
                    port=port,
                    model_number=model_number,
                    device_type=metadata.get("device_type"),
                    length_meters=metadata.get("length_meters"),
                    led_count=metadata.get("led_count"),
                    led_density_per_meter=metadata.get("led_density_per_meter"),
                    has_zones=metadata.get("has_zones"),
                    zone_count=metadata.get("zone_count"),
                    capabilities=normalized.as_mapping(),
                    offline=bool(row["offline"]),
                    poll_failure_count=int(row["poll_failure_count"] or 0),
                )
            )
        return targets

    async def mappings(self) -> List[MappingRecord]:
        return await self.db.run(self._mappings)

    def _mappings(self, conn: sqlite3.Connection) -> List[MappingRecord]:
        rows = conn.execute(
            """
            SELECT
                m.device_id,
                m.universe,
                m.channel,
                m.length,
                m.mapping_type,
                m.field,
                m.fields,
                d.model,
                d.model_number,
                d.capabilities,
                d.protocol
            FROM mappings m
            JOIN devices d ON d.id = m.device_id
            WHERE d.enabled = 1
              AND (d.stale = 0 OR d.stale IS NULL)
            """
        ).fetchall()
        results: List[MappingRecord] = []
        for row in rows:
            protocol = row["protocol"] if "protocol" in row.keys() else "govee"
            cache = self._get_capability_cache(protocol)
            normalized = cache.normalize(
                row["model_number"] or row["model"],
                _deserialize_capabilities(row["capabilities"]),
            )
            fields = _deserialize_fields(row["fields"])
            if not fields:
                try:
                    fields = _mapping_fields_for_length(
                        normalized.as_mapping(),
                        normalized,
                        str(row["mapping_type"]),
                        int(row["length"]),
                        row["field"],
                    )
                except ValueError:
                    continue
            results.append(
                MappingRecord(
                    device_id=row["device_id"],
                    universe=int(row["universe"]),
                    channel=int(row["channel"]),
                    length=int(row["length"]),
                    mapping_type=str(row["mapping_type"]),
                    field=row["field"],
                    fields=fields,
                    capabilities=normalized.as_mapping(),
                )
            )
        return results

    async def mapping_rows(self) -> List[MappingRow]:
        return await self.db.run(self._mapping_rows)

    def _mapping_rows(self, conn: sqlite3.Connection) -> List[MappingRow]:
        rows = conn.execute(
            """
            SELECT
                id,
                device_id,
                universe,
                channel,
                length,
                mapping_type,
                field,
                fields,
                created_at,
                updated_at
            FROM mappings
            ORDER BY universe, channel
            """
        ).fetchall()
        return [self._row_to_mapping(row) for row in rows]

    async def mapping_by_id(self, mapping_id: int) -> Optional[MappingRow]:
        return await self.db.run(lambda conn: self._mapping_by_id(conn, mapping_id))

    def _mapping_by_id(self, conn: sqlite3.Connection, mapping_id: int) -> Optional[MappingRow]:
        row = conn.execute(
            """
            SELECT
                id,
                device_id,
                universe,
                channel,
                length,
                mapping_type,
                field,
                fields,
                created_at,
                updated_at
            FROM mappings
            WHERE id = ?
            """,
            (mapping_id,),
        ).fetchone()
        if not row:
            return None
        return self._row_to_mapping(row)

    async def channel_map(self) -> Dict[int, List[Dict[str, Any]]]:
        return await self.db.run(self._channel_map)

    def _channel_map(self, conn: sqlite3.Connection) -> Dict[int, List[Dict[str, Any]]]:
        rows = conn.execute(
            """
            SELECT
                m.id,
                m.device_id,
                m.universe,
                m.channel,
                m.length,
                m.mapping_type,
                m.field,
                m.fields,
                d.description,
                d.ip,
                d.model,
                d.model_number,
                d.protocol,
                d.capabilities
            FROM mappings m
            JOIN devices d ON d.id = m.device_id
            ORDER BY m.universe, m.channel
            """
        ).fetchall()
        universes: Dict[int, List[Dict[str, Any]]] = {}
        for row in rows:
            normalized_caps = self._normalized_capabilities_obj(row)
            mapping_type = _normalize_mapping_type(row["mapping_type"])
            capabilities = normalized_caps.as_mapping()
            fields = _deserialize_fields(row["fields"])
            if not fields:
                try:
                    fields = _mapping_fields_for_length(
                        capabilities,
                        normalized_caps,
                        mapping_type,
                        int(row["length"]),
                        row["field"],
                    )
                except ValueError:
                    continue
            entry: Dict[str, Any] = {
                "id": int(row["id"]),
                "device_id": row["device_id"],
                "universe": int(row["universe"]),
                "channel": int(row["channel"]),
                "length": int(row["length"]),
                "mapping_type": mapping_type,
                "fields": list(fields),
                "device_description": row["description"],
                "device_ip": row["ip"],
            }
            if row["field"]:
                entry["field"] = row["field"]
            universes.setdefault(int(row["universe"]), []).append(entry)
        return universes

    async def create_mapping(
        self,
        *,
        device_id: str,
        universe: int,
        channel: int,
        length: int,
        mapping_type: str = "range",
        field: Optional[str] = None,
        allow_overlap: bool = False,
    ) -> MappingRow:
        return await self.db.run(
            lambda conn: self._create_mapping(
                conn, device_id, universe, channel, length, mapping_type, field, allow_overlap
            )
        )

    async def create_template_mappings(
        self,
        *,
        device_id: str,
        universe: int,
        start_channel: int,
        template: str,
        allow_overlap: bool = False,
    ) -> List[MappingRow]:
        return await self.db.run(
            lambda conn: self._create_template_mappings(
                conn,
                device_id=device_id,
                universe=universe,
                start_channel=start_channel,
                template=template,
                allow_overlap=allow_overlap,
            )
        )

    def _create_mapping(
        self,
        conn: sqlite3.Connection,
        device_id: str,
        universe: int,
        channel: int,
        length: int,
        mapping_type: str,
        field: Optional[str],
        allow_overlap: bool,
        *,
        commit: bool = True,
        normalized_capabilities: Optional[NormalizedCapabilities] = None,
    ) -> MappingRow:
        if channel <= 0 or length <= 0:
            raise ValueError("Channel and length must be positive")
        normalized_mapping_type = _normalize_mapping_type(mapping_type)
        normalized_field = _normalize_field_name(field) if normalized_mapping_type == "discrete" else None
        if normalized_mapping_type == "discrete" and length != 1:
            raise ValueError("Discrete mappings must have a length of 1")
        device_row = conn.execute(
            "SELECT model, model_number, capabilities, protocol FROM devices WHERE id = ?",
            (device_id,),
        ).fetchone()
        if not device_row:
            raise ValueError("Device not found")
        normalized = normalized_capabilities or self._normalized_capabilities_obj(device_row)
        capabilities = normalized.as_mapping()
        if normalized_mapping_type == "range":
            required = _required_channels(capabilities, length)
            if required > length:
                raise ValueError("Mapping length is shorter than required channels")
            mode = _coerce_mode_for_mapping(capabilities, length)
            validate_mapping_mode(mode, normalized)
        else:
            _validate_field_support(normalized_field or "", normalized)
        new_fields = _mapping_fields_for_length(
            capabilities,
            normalized,
            normalized_mapping_type,
            length,
            normalized_field,
        )
        self._ensure_no_field_conflicts(
            conn,
            device_id,
            universe,
            capabilities,
            normalized,
            new_fields,
            exclude_id=None,
        )
        self._ensure_no_overlap(conn, universe, channel, length, None, allow_overlap)
        cursor = conn.execute(
            """
            INSERT INTO mappings (device_id, universe, channel, length, mapping_type, field, fields)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                device_id,
                universe,
                channel,
                length,
                normalized_mapping_type,
                normalized_field,
                _serialize_fields(new_fields),
            ),
        )
        self._refresh_configured_from_mappings(conn, device_id, commit=False)
        if commit:
            conn.commit()
        mapping_id = cursor.lastrowid
        created = conn.execute(
            """
            SELECT
                id,
                device_id,
                universe,
                channel,
                length,
                mapping_type,
                field,
                fields,
                created_at,
                updated_at
            FROM mappings
            WHERE id = ?
            """,
            (mapping_id,),
        ).fetchone()
        if not created:
            raise ValueError("Failed to create mapping")
        self.logger.info(
            "Created mapping",
            extra={
                "mapping_id": mapping_id,
                "device_id": device_id,
                "universe": universe,
                "channel": channel,
                "length": length,
                "mapping_type": normalized_mapping_type,
                "field": normalized_field,
            },
        )
        return self._row_to_mapping(created)

    def _create_template_mappings(
        self,
        conn: sqlite3.Connection,
        *,
        device_id: str,
        universe: int,
        start_channel: int,
        template: str,
        allow_overlap: bool,
    ) -> List[MappingRow]:
        if start_channel <= 0:
            raise ValueError("Start channel must be positive")

        segments = _template_segments(template)
        device_row = conn.execute(
            "SELECT model, model_number, capabilities, protocol FROM devices WHERE id = ?",
            (device_id,),
        ).fetchone()
        if not device_row:
            raise ValueError("Device not found")
        normalized = self._normalized_capabilities_obj(device_row)
        _validate_template_support(segments, normalized, template)

        results: List[MappingRow] = []
        channel = start_channel
        conn.execute("BEGIN")
        try:
            for segment in segments:
                mapping_type = "discrete" if segment.kind == "discrete" else "range"
                field = segment.fields[0] if mapping_type == "discrete" else None
                results.append(
                    self._create_mapping(
                        conn,
                        device_id=device_id,
                        universe=universe,
                        channel=channel,
                        length=segment.length,
                        mapping_type=mapping_type,
                        field=field,
                        allow_overlap=allow_overlap,
                        commit=False,
                        normalized_capabilities=normalized,
                    )
                )
                channel += segment.length
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return results

    async def update_mapping(
        self,
        mapping_id: int,
        *,
        device_id: Optional[str] = None,
        universe: Optional[int] = None,
        channel: Optional[int] = None,
        length: Optional[int] = None,
        mapping_type: Optional[str] = None,
        field: Optional[str] = None,
        allow_overlap: bool = False,
    ) -> Optional[MappingRow]:
        return await self.db.run(
            lambda conn: self._update_mapping(
                conn,
                mapping_id,
                device_id,
                universe,
                channel,
                length,
                mapping_type,
                field,
                allow_overlap,
            )
        )

    def _update_mapping(
        self,
        conn: sqlite3.Connection,
        mapping_id: int,
        device_id: Optional[str],
        universe: Optional[int],
        channel: Optional[int],
        length: Optional[int],
        mapping_type: Optional[str],
        field: Optional[str],
        allow_overlap: bool,
    ) -> Optional[MappingRow]:
        existing = conn.execute(
            """
            SELECT
                id,
                device_id,
                universe,
                channel,
                length,
                mapping_type,
                field
            FROM mappings
            WHERE id = ?
            """,
            (mapping_id,),
        ).fetchone()
        if not existing:
            return None
        new_device_id = device_id or existing["device_id"]
        new_universe = universe if universe is not None else int(existing["universe"])
        new_channel = channel if channel is not None else int(existing["channel"])
        new_length = length if length is not None else int(existing["length"])
        normalized_mapping_type = _normalize_mapping_type(
            mapping_type or existing["mapping_type"]
        )
        field_value = field if field is not None else existing["field"]
        normalized_field = (
            _normalize_field_name(field_value)
            if normalized_mapping_type == "discrete"
            else None
        )
        if normalized_mapping_type == "discrete" and new_length != 1:
            raise ValueError("Discrete mappings must have a length of 1")
        if normalized_mapping_type == "range":
            normalized_field = None
        if new_channel <= 0 or new_length <= 0:
            raise ValueError("Channel and length must be positive")
        device_row = conn.execute(
            "SELECT model, model_number, capabilities, protocol FROM devices WHERE id = ?",
            (new_device_id,),
        ).fetchone()
        if not device_row:
            raise ValueError("Device not found")
        normalized = self._normalized_capabilities_obj(device_row)
        capabilities = normalized.as_mapping()
        if normalized_mapping_type == "range":
            required = _required_channels(capabilities, new_length)
            if required > new_length:
                raise ValueError("Mapping length is shorter than required channels")
            mode = _coerce_mode_for_mapping(capabilities, new_length)
            validate_mapping_mode(mode, normalized)
        else:
            _validate_field_support(normalized_field or "", normalized)
        new_fields = _mapping_fields_for_length(
            capabilities,
            normalized,
            normalized_mapping_type,
            new_length,
            normalized_field,
        )
        self._ensure_no_field_conflicts(
            conn,
            new_device_id,
            new_universe,
            capabilities,
            normalized,
            new_fields,
            exclude_id=mapping_id,
        )
        self._ensure_no_overlap(
            conn, new_universe, new_channel, new_length, mapping_id, allow_overlap
        )
        conn.execute(
            """
            UPDATE mappings
            SET device_id = ?, universe = ?, channel = ?, length = ?, mapping_type = ?, field = ?, fields = ?
            WHERE id = ?
            """,
            (
                new_device_id,
                new_universe,
                new_channel,
                new_length,
                normalized_mapping_type,
                normalized_field,
                _serialize_fields(new_fields),
                mapping_id,
            ),
        )
        if existing["device_id"] != new_device_id:
            self._refresh_configured_from_mappings(conn, existing["device_id"], commit=False)
        self._refresh_configured_from_mappings(conn, new_device_id, commit=False)
        conn.commit()
        updated = conn.execute(
            """
            SELECT
                id,
                device_id,
                universe,
                channel,
                length,
                mapping_type,
                field,
                fields,
                created_at,
                updated_at
            FROM mappings
            WHERE id = ?
            """,
            (mapping_id,),
        ).fetchone()
        if not updated:
            return None
        self.logger.info(
            "Updated mapping",
            extra={
                "mapping_id": mapping_id,
                "device_id": new_device_id,
                "universe": new_universe,
                "channel": new_channel,
                "length": new_length,
                "mapping_type": normalized_mapping_type,
                "field": normalized_field,
            },
        )
        return self._row_to_mapping(updated)

    async def delete_mapping(self, mapping_id: int) -> bool:
        return await self.db.run(lambda conn: self._delete_mapping(conn, mapping_id))

    def _delete_mapping(self, conn: sqlite3.Connection, mapping_id: int) -> bool:
        mapping_row = conn.execute(
            "SELECT device_id FROM mappings WHERE id = ?",
            (mapping_id,),
        ).fetchone()
        if not mapping_row:
            return False
        device_id = mapping_row["device_id"]
        cursor = conn.execute(
            "DELETE FROM mappings WHERE id = ?",
            (mapping_id,),
        )
        self._refresh_configured_from_mappings(conn, device_id, commit=False)
        conn.commit()
        if cursor.rowcount:
            self.logger.info("Deleted mapping", extra={"mapping_id": mapping_id})
        return cursor.rowcount > 0

    def _mapping_count(
        self, conn: sqlite3.Connection, *, device_id: str, universe: Optional[int] = None
    ) -> int:
        query = "SELECT COUNT(1) FROM mappings WHERE device_id = ?"
        params: Tuple[Any, ...] = (device_id,)
        if universe is not None:
            query += " AND universe = ?"
            params += (universe,)
        row = conn.execute(query, params).fetchone()
        return int(row[0]) if row else 0

    def _ensure_no_overlap(
        self,
        conn: sqlite3.Connection,
        universe: int,
        channel: int,
        length: int,
        exclude_id: Optional[int],
        allow_overlap: bool,
    ) -> None:
        if allow_overlap:
            return
        start = channel
        end = channel + length - 1
        query = """
            SELECT id, device_id, channel, length
            FROM mappings
            WHERE universe = ?
              AND (? BETWEEN channel AND (channel + length - 1)
                   OR (channel BETWEEN ? AND ?))
        """
        params: Tuple[Any, ...] = (universe, start, start, end)
        if exclude_id is not None:
            query += " AND id != ?"
            params += (exclude_id,)
        conflict = conn.execute(query, params).fetchone()
        if conflict:
            raise ValueError("Mapping overlaps an existing entry")

    def _ensure_no_field_conflicts(
        self,
        conn: sqlite3.Connection,
        device_id: str,
        universe: int,
        capabilities: Mapping[str, Any],
        normalized_capabilities: NormalizedCapabilities,
        new_fields: Iterable[str],
        *,
        exclude_id: Optional[int],
    ) -> None:
        existing_fields = self._existing_field_assignments(
            conn,
            device_id,
            universe,
            capabilities,
            normalized_capabilities,
            exclude_id=exclude_id,
        )
        conflicts = existing_fields.intersection(set(new_fields))
        if conflicts:
            conflict_list = ", ".join(sorted(conflicts))
            raise ValueError(
                f"Field(s) already mapped for device {device_id} on universe {universe}: {conflict_list}"
            )

    def _existing_field_assignments(
        self,
        conn: sqlite3.Connection,
        device_id: str,
        universe: int,
        capabilities: Mapping[str, Any],
        normalized_capabilities: NormalizedCapabilities,
        *,
        exclude_id: Optional[int],
    ) -> Set[str]:
        rows = conn.execute(
            """
            SELECT id, length, mapping_type, field, fields
            FROM mappings
            WHERE device_id = ? AND universe = ?
            """,
            (device_id, universe),
        ).fetchall()
        fields: Set[str] = set()
        for row in rows:
            if exclude_id is not None and int(row["id"]) == exclude_id:
                continue
            mapping_type = _normalize_mapping_type(row["mapping_type"])
            stored_fields = _deserialize_fields(row["fields"])
            try:
                if stored_fields:
                    fields.update(stored_fields)
                else:
                    fields.update(
                        _mapping_fields_for_length(
                            capabilities,
                            normalized_capabilities,
                            mapping_type,
                            int(row["length"]),
                            row["field"],
                        )
                    )
            except ValueError:
                continue
        return fields

    def _set_configured_state(
        self, conn: sqlite3.Connection, device_id: str, configured: bool, *, commit: bool = False
    ) -> None:
        conn.execute(
            """
            UPDATE devices
            SET configured = ?
            WHERE id = ?
            """,
            (1 if configured else 0, device_id),
        )
        if commit:
            conn.commit()

    def _refresh_configured_from_mappings(
        self,
        conn: sqlite3.Connection,
        device_id: str,
        *,
        universe: Optional[int] = None,
        commit: bool = False,
    ) -> None:
        count = self._mapping_count(conn, device_id=device_id, universe=universe)
        self._set_configured_state(conn, device_id, count > 0, commit=commit)

    async def update_capabilities(
        self, device_id: str, capabilities: Mapping[str, Any]
    ) -> None:
        await self.db.run(lambda conn: self._update_capabilities(conn, device_id, capabilities))

    def _update_capabilities(
        self, conn: sqlite3.Connection, device_id: str, capabilities: Mapping[str, Any]
    ) -> None:
        device_row = conn.execute(
            "SELECT model, model_number, protocol, capabilities FROM devices WHERE id = ?",
            (device_id,),
        ).fetchone()
        model = None
        protocol = "govee"
        if device_row:
            model = device_row["model_number"] or device_row["model"]
            protocol = device_row["protocol"] if "protocol" in device_row.keys() else "govee"

        # Enrich capabilities using protocol-specific handler
        from .protocol import get_protocol_handler
        handler = get_protocol_handler(protocol)

        existing_caps = _deserialize_capabilities(device_row["capabilities"]) if device_row and device_row["capabilities"] else {}
        incoming_caps = dict(capabilities) if capabilities else {}

        enriched_caps = handler.enrich_capabilities(existing_caps, incoming_caps)

        cache = self._get_capability_cache(protocol)
        normalized = cache.normalize(model, enriched_caps)
        serialized = _serialize_capabilities(normalized.as_mapping())
        metadata = _coerce_metadata_for_db(normalized.metadata)
        conn.execute(
            """
            UPDATE devices
            SET
                capabilities = ?,
                model = COALESCE(?, model),
                model_number = COALESCE(?, model_number, model),
                device_type = COALESCE(?, device_type),
                length_meters = COALESCE(?, length_meters),
                led_count = COALESCE(?, led_count),
                led_density_per_meter = COALESCE(?, led_density_per_meter),
                has_zones = COALESCE(?, has_zones),
                zone_count = COALESCE(?, zone_count)
            WHERE id = ?
            """,
            (
                serialized,
                normalized.model_number,
                normalized.model_number,
                metadata.get("device_type"),
                metadata.get("length_meters"),
                metadata.get("led_count"),
                metadata.get("led_density_per_meter"),
                metadata.get("has_zones"),
                metadata.get("zone_count"),
                device_id,
            ),
        )
        conn.commit()
        self.logger.debug(
            "Updated device capabilities",
            extra={"id": device_id},
        )

    async def enqueue_state(self, update: DeviceStateUpdate) -> None:
        await self.db.run(lambda conn: self._enqueue_state(conn, update))

    def _enqueue_state(self, conn: sqlite3.Connection, update: DeviceStateUpdate) -> None:
        # Get device protocol
        device_row = conn.execute(
            "SELECT protocol FROM devices WHERE id = ?",
            (update.device_id,)
        ).fetchone()
        protocol = device_row["protocol"] if device_row else "govee"

        # Prepare payload for protocol-specific wrapping
        payload_to_wrap = dict(update.payload)

        # For LIFX, add target MAC address from device ID
        if protocol == "lifx":
            # Device ID for LIFX is the MAC address string (e.g., "AA:BB:CC:DD:EE:FF")
            # Convert to bytes for the protocol handler
            try:
                mac_bytes = bytes.fromhex(update.device_id.replace(":", ""))
                payload_to_wrap["_target_mac"] = mac_bytes
            except (ValueError, AttributeError):
                # If MAC parsing fails, let the protocol handler handle it
                pass

        # Use protocol-specific handler to wrap payload
        from .protocol import get_protocol_handler
        handler = get_protocol_handler(protocol)
        wrapped_payload = handler.wrap_command(payload_to_wrap)

        # Handle multiple commands (e.g., color + brightness for Govee)
        if isinstance(wrapped_payload, dict) and "_multiple" in wrapped_payload:
            payloads = wrapped_payload["_multiple"]
        else:
            payloads = [wrapped_payload]

        # Enqueue each command separately
        for payload in payloads:
            serialized = _serialize_capabilities(payload) or "null"
            conn.execute(
                """
                INSERT INTO state (device_id, payload, context_id)
                VALUES (?, ?, ?)
                """,
                (update.device_id, serialized, update.context_id),
            )

        conn.commit()
        self._update_queue_metrics(conn, update.device_id)
        self.logger.debug(
            "Enqueued device update",
            extra={
                "device_id": update.device_id,
                "protocol": protocol,
                "context_id": update.context_id,
                "command_count": len(payloads)
            },
        )

    async def set_last_seen(
        self, device_ids: Iterable[str], timestamp: Optional[str] = None, mark_online: bool = True
    ) -> None:
        event_data = await self.db.run(lambda conn: self._set_last_seen(conn, device_ids, timestamp, mark_online))

        # Publish device_online event if device transitioned from offline to online
        if self._event_bus and mark_online and event_data and event_data.get("went_online"):
            from .events import EVENT_DEVICE_ONLINE

            for device_id in event_data["went_online"]:
                self.logger.info(
                    "Device came online from activity",
                    extra={
                        "device_id": device_id,
                        "previous_offline_reason": "activity",
                    },
                )
                await self._event_bus.publish(
                    EVENT_DEVICE_ONLINE,
                    {
                        "device_id": device_id,
                        "previous_offline_reason": "activity",
                    },
                )

    def _set_last_seen(
        self,
        conn: sqlite3.Connection,
        device_ids: Iterable[str],
        timestamp: Optional[str] = None,
        mark_online: bool = True,
    ) -> Optional[Dict[str, Any]]:
        ids = list(device_ids)
        if not ids:
            return None
        ts = timestamp or _now_iso()
        params = [(ts, device_id) for device_id in ids]

        # Always update last_seen/stale
        conn.executemany(
            """
            UPDATE devices
            SET last_seen = ?, stale = 0
            WHERE id = ?
            """,
            params,
        )

        if mark_online:
            placeholders = ",".join(["?"] * len(ids))
            rows = conn.execute(
                f"SELECT id, offline FROM devices WHERE id IN ({placeholders})",
                ids,
            ).fetchall()
            offline_ids = [row["id"] for row in rows if bool(row["offline"])]

            if offline_ids:
                conn.executemany(
                    """
                    UPDATE devices
                    SET
                        offline = 0,
                        poll_failure_count = 0,
                        poll_health = 'healthy',
                        poll_last_failure_at = NULL,
                        poll_last_success_at = ?,
                        failure_count = 0,
                        last_failure_at = NULL
                    WHERE id = ?
                    """,
                    [(ts, device_id) for device_id in offline_ids],
                )
                conn.commit()
                self._update_offline_metric(conn)
                return {"went_online": offline_ids}

        conn.commit()
        return None

    async def pending_device_ids(self) -> List[str]:
        return await self.db.run(self._pending_device_ids)

    async def pending_state_count(self) -> int:
        """Return the exact number of commands awaiting delivery."""
        return await self.db.run(
            lambda conn: int(conn.execute("SELECT COUNT(*) FROM state").fetchone()[0])
        )

    def _pending_device_ids(self, conn: sqlite3.Connection) -> List[str]:
        rows = conn.execute(
            """
            SELECT DISTINCT device_id
            FROM state
            """
        ).fetchall()
        return [row["device_id"] for row in rows]

    async def next_state(self, device_id: str) -> Optional[PendingState]:
        return await self.db.run(lambda conn: self._next_state(conn, device_id))

    def _next_state(self, conn: sqlite3.Connection, device_id: str) -> Optional[PendingState]:
        row = conn.execute(
            """
            SELECT id, device_id, payload, created_at, context_id
            FROM state
            WHERE device_id = ?
            ORDER BY id ASC
            LIMIT 1
            """,
            (device_id,),
        ).fetchone()
        if not row:
            return None
        return PendingState(
            id=int(row["id"]),
            device_id=row["device_id"],
            payload=row["payload"],
            created_at=row["created_at"],
            context_id=row["context_id"],
        )

    async def delete_state(self, state_id: int) -> None:
        await self.db.run(lambda conn: self._delete_state(conn, state_id))

    def _delete_state(self, conn: sqlite3.Connection, state_id: int) -> None:
        row = conn.execute(
            "SELECT device_id FROM state WHERE id = ?",
            (state_id,),
        ).fetchone()
        conn.execute(
            """
            DELETE FROM state
            WHERE id = ?
            """,
            (state_id,),
        )
        conn.commit()
        if row:
            self._update_queue_metrics(conn, row["device_id"])
        else:
            total_row = conn.execute("SELECT COUNT(*) AS total FROM state").fetchone()
            total = int(total_row["total"] if total_row else 0)
            set_total_queue_depth(total)

    async def device_info(self, device_id: str) -> Optional[DeviceInfo]:
        return await self.db.run(lambda conn: self._device_info(conn, device_id))

    async def normalized_capabilities(self, device_id: str) -> Optional[NormalizedCapabilities]:
        return await self.db.run(lambda conn: self._normalized_capabilities_by_id(conn, device_id))

    def _normalized_capabilities_by_id(
        self, conn: sqlite3.Connection, device_id: str
    ) -> Optional[NormalizedCapabilities]:
        row = conn.execute(
            "SELECT model, model_number, capabilities, protocol FROM devices WHERE id = ?",
            (device_id,),
        ).fetchone()
        if not row:
            return None
        return self._normalized_capabilities_obj(row)

    def _device_info(self, conn: sqlite3.Connection, device_id: str) -> Optional[DeviceInfo]:
        row = conn.execute(
            """
            SELECT
                id,
                ip,
                protocol,
                model,
                model_number,
                device_type,
                length_meters,
                led_count,
                led_density_per_meter,
                has_zones,
                zone_count,
                capabilities,
                offline,
                poll_health,
                failure_count,
                last_payload_hash,
                last_payload_at,
                last_failure_at,
                enabled,
                stale,
                poll_last_success_at,
                poll_last_failure_at,
                poll_failure_count
            FROM devices
            WHERE id = ?
            """,
            (device_id,),
        ).fetchone()
        if not row or not row["enabled"] or row["stale"]:
            return None
        normalized = self._normalized_capabilities_obj(row)
        metadata = _merge_metadata(row, normalized.metadata)
        model_number = row["model_number"] if "model_number" in row.keys() else None
        if not model_number:
            model_number = row["model"]
        return DeviceInfo(
            id=row["id"],
            ip=row["ip"],
            protocol=row["protocol"] or "govee",
            capabilities=normalized.as_mapping(),
            model_number=model_number,
            device_type=metadata.get("device_type"),
            length_meters=metadata.get("length_meters"),
            led_count=metadata.get("led_count"),
            led_density_per_meter=metadata.get("led_density_per_meter"),
            has_zones=metadata.get("has_zones"),
            zone_count=metadata.get("zone_count"),
            offline=bool(row["offline"]),
            poll_health=row["poll_health"] or "healthy",
            failure_count=int(row["failure_count"] or 0),
            last_payload_hash=row["last_payload_hash"],
            last_payload_at=row["last_payload_at"],
            last_failure_at=row["last_failure_at"],
            normalized_capabilities=normalized,
            poll_last_success_at=row["poll_last_success_at"],
            poll_last_failure_at=row["poll_last_failure_at"],
            poll_failure_count=int(row["poll_failure_count"] or 0),
        )

    async def record_send_success(self, device_id: str, payload_hash: str) -> None:
        event_data = await self.db.run(lambda conn: self._record_send_success(conn, device_id, payload_hash))

        # Publish device_online event if device transitioned from offline to online
        if self._event_bus and event_data and event_data.get("went_online"):
            from .events import EVENT_DEVICE_ONLINE
            self.logger.info(
                "Device came online",
                extra={
                    "device_id": device_id,
                    "previous_offline_reason": "send_failures",
                },
            )
            await self._event_bus.publish(EVENT_DEVICE_ONLINE, {
                "device_id": device_id,
                "previous_offline_reason": "send_failures",
            })

    def _record_send_success(
        self, conn: sqlite3.Connection, device_id: str, payload_hash: str
    ) -> Optional[Dict[str, Any]]:
        # Check current offline status before update
        row = conn.execute("SELECT offline FROM devices WHERE id = ?", (device_id,)).fetchone()
        was_offline = row["offline"] if row else False

        now = _now_iso()
        conn.execute(
            """
            UPDATE devices
            SET
                last_payload_hash = ?,
                last_payload_at = ?,
                failure_count = 0,
                offline = 0,
                last_failure_at = NULL
            WHERE id = ?
            """,
            (payload_hash, now, device_id),
        )
        conn.commit()
        self._update_offline_metric(conn)

        # Return event data if status changed
        return {"went_online": was_offline}

    async def record_poll_success(
        self,
        device_id: str,
        state: Optional[Mapping[str, Any]],
        *,
        ip: Optional[str] = None,
        protocol: Optional[str] = None,
        port: Optional[int] = None,
    ) -> None:
        event_data = await self.db.run(
            lambda conn: self._record_poll_success(
                conn,
                device_id,
                state,
                ip=ip,
                protocol=protocol,
                port=port,
            )
        )

        # Publish device_online event if device transitioned from offline to online
        if self._event_bus and event_data and event_data.get("went_online"):
            from .events import EVENT_DEVICE_ONLINE
            self.logger.info(
                "Device came online",
                extra={
                    "device_id": device_id,
                    "previous_offline_reason": "poll_failures",
                    "protocol": event_data.get("protocol"),
                    "ip": event_data.get("ip"),
                    "port": event_data.get("port"),
                    "poll_failure_count_before": event_data.get("poll_failure_count_before"),
                    "poll_failure_count_after": event_data.get("poll_failure_count_after"),
                    "offline_before": event_data.get("offline_before"),
                    "offline_after": event_data.get("offline_after"),
                },
            )
            await self._event_bus.publish(EVENT_DEVICE_ONLINE, {
                "device_id": device_id,
                "previous_offline_reason": "poll_failures",
            })

    def _record_poll_success(
        self,
        conn: sqlite3.Connection,
        device_id: str,
        state: Optional[Mapping[str, Any]],
        *,
        ip: Optional[str] = None,
        protocol: Optional[str] = None,
        port: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        # Get current device info for capability refinement
        device_row = conn.execute(
            """
            SELECT offline, capabilities, protocol, model, model_number, ip, poll_failure_count
            FROM devices
            WHERE id = ?
            """,
            (device_id,)
        ).fetchone()
        was_offline = device_row["offline"] if device_row else False
        failure_count_before = int(device_row["poll_failure_count"]) if device_row else 0
        protocol = protocol or (device_row["protocol"] if device_row and "protocol" in device_row.keys() else None)
        ip = ip or (device_row["ip"] if device_row and "ip" in device_row.keys() else None)

        self.logger.debug(
            "Recording poll success",
            extra={
                "device_id": device_id,
                "protocol": protocol,
                "ip": ip,
                "port": port,
                "poll_failure_count_before": failure_count_before,
                "offline_before": was_offline,
                "state_present": state is not None,
            },
        )

        # Refine capabilities additively from observed state
        refined_capabilities = None
        if state and device_row:
            from .capabilities import refine_capabilities_from_state

            current_caps = _deserialize_capabilities(device_row["capabilities"])
            protocol = device_row["protocol"] if "protocol" in device_row.keys() else "govee"
            model = device_row["model_number"] or device_row["model"]

            # Get current normalized capabilities
            cache = self._get_capability_cache(protocol)
            current_normalized = cache.normalize(model, current_caps)

            # Refine based on observed state (additive only)
            refined_raw = refine_capabilities_from_state(current_normalized.as_mapping(), state)

            # Re-normalize the refined capabilities
            refined_normalized = cache.normalize(model, refined_raw)

            # Only update if capabilities actually changed
            if refined_normalized.fingerprint != current_normalized.fingerprint:
                refined_capabilities = _serialize_capabilities(refined_normalized.as_mapping())

        now = _now_iso()
        serialized_state = _serialize_capabilities(state) if state is not None else None

        if refined_capabilities is not None:
            # Update with refined capabilities
            conn.execute(
                """
                UPDATE devices
                SET
                    poll_failure_count = 0,
                    poll_health = 'healthy',
                    poll_last_success_at = ?,
                    poll_last_failure_at = NULL,
                    poll_state = ?,
                    poll_state_updated_at = CASE
                        WHEN ? IS NOT NULL THEN ?
                        ELSE poll_state_updated_at
                    END,
                    capabilities = ?,
                    offline = 0,
                    last_seen = ?,
                    stale = 0
                WHERE id = ?
                """,
                (now, serialized_state, serialized_state, now, refined_capabilities, now, device_id),
            )
        else:
            # Update without changing capabilities
            conn.execute(
                """
                UPDATE devices
                SET
                    poll_failure_count = 0,
                    poll_health = 'healthy',
                    poll_last_success_at = ?,
                    poll_last_failure_at = NULL,
                    poll_state = ?,
                    poll_state_updated_at = CASE
                        WHEN ? IS NOT NULL THEN ?
                        ELSE poll_state_updated_at
                    END,
                    offline = 0,
                    last_seen = ?,
                    stale = 0
                WHERE id = ?
                """,
                (now, serialized_state, serialized_state, now, now, device_id),
            )

        conn.commit()
        self._update_offline_metric(conn)

        self.logger.debug(
            "Recorded poll success",
            extra={
                "device_id": device_id,
                "protocol": protocol,
                "ip": ip,
                "port": port,
                "poll_failure_count_before": failure_count_before,
                "poll_failure_count_after": 0,
                "offline_before": was_offline,
                "offline_after": False,
                "state_present": state is not None,
            },
        )

        return {
            "went_online": was_offline,
            "protocol": protocol,
            "ip": ip,
            "port": port,
            "poll_failure_count_before": failure_count_before,
            "poll_failure_count_after": 0,
            "offline_before": was_offline,
            "offline_after": False,
        }

    async def record_poll_failure(
        self,
        device_id: str,
        offline_threshold: int,
        *,
        failure_reason: Optional[str] = None,
        ip: Optional[str] = None,
        protocol: Optional[str] = None,
        port: Optional[int] = None,
    ) -> None:
        event_data = await self.db.run(
            lambda conn: self._record_poll_failure(
                conn,
                device_id,
                offline_threshold,
                failure_reason=failure_reason,
                ip=ip,
                protocol=protocol,
                port=port,
            )
        )

        # Publish device_offline event if device transitioned to offline
        if self._event_bus and event_data and event_data.get("went_offline"):
            from .events import EVENT_DEVICE_OFFLINE
            self.logger.info(
                "Device went offline",
                extra={
                    "device_id": device_id,
                    "reason": "poll_failures",
                    "failure_count": event_data.get("failure_count", 0),
                    "protocol": event_data.get("protocol"),
                    "ip": event_data.get("ip"),
                    "port": event_data.get("port"),
                    "poll_failure_count_before": event_data.get("poll_failure_count_before"),
                    "poll_failure_count_after": event_data.get("poll_failure_count_after"),
                    "offline_before": event_data.get("offline_before"),
                    "offline_after": event_data.get("offline_after"),
                    "failure_reason": event_data.get("failure_reason"),
                },
            )
            await self._event_bus.publish(EVENT_DEVICE_OFFLINE, {
                "device_id": device_id,
                "reason": "poll_failures",
                "failure_count": event_data.get("failure_count", 0),
            })

    def _record_poll_failure(
        self,
        conn: sqlite3.Connection,
        device_id: str,
        offline_threshold: int,
        *,
        failure_reason: Optional[str] = None,
        ip: Optional[str] = None,
        protocol: Optional[str] = None,
        port: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        # Check current status before update
        row = conn.execute(
            "SELECT offline, poll_failure_count, protocol, ip FROM devices WHERE id = ?",
            (device_id,)
        ).fetchone()
        was_offline = row["offline"] if row else False
        old_failure_count = row["poll_failure_count"] if row else 0
        protocol = protocol or (row["protocol"] if row and "protocol" in row.keys() else None)
        ip = ip or (row["ip"] if row and "ip" in row.keys() else None)

        self.logger.debug(
            "Recording poll failure",
            extra={
                "device_id": device_id,
                "protocol": protocol,
                "ip": ip,
                "port": port,
                "poll_failure_count_before": old_failure_count,
                "offline_before": was_offline,
                "failure_reason": failure_reason or "unknown",
            },
        )

        now = _now_iso()
        conn.execute(
            """
            UPDATE devices
            SET
                poll_failure_count = poll_failure_count + 1,
                poll_last_failure_at = ?,
                poll_health = CASE
                    WHEN (poll_failure_count + 1) >= ? THEN 'offline'
                    WHEN (poll_failure_count + 1) >= 1 THEN 'degraded'
                    ELSE poll_health
                END,
                offline = CASE
                    WHEN (poll_failure_count + 1) >= ? THEN 1
                    ELSE offline
                END
            WHERE id = ?
            """,
            (now, offline_threshold, offline_threshold, device_id),
        )
        conn.commit()
        self._update_offline_metric(conn)

        # Check if device went offline
        new_failure_count = old_failure_count + 1
        went_offline = not was_offline and new_failure_count >= offline_threshold

        offline_after = bool(was_offline or new_failure_count >= offline_threshold)

        self.logger.debug(
            "Recorded poll failure",
            extra={
                "device_id": device_id,
                "protocol": protocol,
                "ip": ip,
                "port": port,
                "poll_failure_count_before": old_failure_count,
                "poll_failure_count_after": new_failure_count,
                "offline_before": was_offline,
                "offline_after": offline_after,
                "failure_reason": failure_reason or "unknown",
            },
        )

        return {
            "went_offline": went_offline,
            "failure_count": new_failure_count,
            "protocol": protocol,
            "ip": ip,
            "port": port,
            "poll_failure_count_before": old_failure_count,
            "poll_failure_count_after": new_failure_count,
            "offline_before": was_offline,
            "offline_after": offline_after,
            "failure_reason": failure_reason or "unknown",
        }

    async def record_send_failure(
        self, device_id: str, payload_hash: str, offline_threshold: int
    ) -> None:
        event_data = await self.db.run(
            lambda conn: self._record_send_failure(conn, device_id, payload_hash, offline_threshold)
        )

        # Publish device_offline event if device transitioned to offline
        if self._event_bus and event_data and event_data.get("went_offline"):
            from .events import EVENT_DEVICE_OFFLINE
            self.logger.info(
                "Device went offline",
                extra={
                    "device_id": device_id,
                    "reason": "send_failures",
                    "failure_count": event_data.get("failure_count", 0),
                },
            )
            await self._event_bus.publish(EVENT_DEVICE_OFFLINE, {
                "device_id": device_id,
                "reason": "send_failures",
                "failure_count": event_data.get("failure_count", 0),
            })

    def _record_send_failure(
        self, conn: sqlite3.Connection, device_id: str, payload_hash: str, offline_threshold: int
    ) -> Optional[Dict[str, Any]]:
        # Check current status before update
        row = conn.execute(
            "SELECT offline, failure_count FROM devices WHERE id = ?",
            (device_id,)
        ).fetchone()
        was_offline = row["offline"] if row else False
        old_failure_count = row["failure_count"] if row else 0

        now = _now_iso()
        conn.execute(
            """
            UPDATE devices
            SET
                last_payload_hash = ?,
                last_payload_at = ?,
                failure_count = failure_count + 1,
                offline = CASE
                    WHEN (failure_count + 1) >= ? THEN 1
                    ELSE offline
                END,
                last_failure_at = ?
            WHERE id = ?
            """,
            (payload_hash, now, offline_threshold, now, device_id),
        )
        conn.commit()
        self._update_offline_metric(conn)

        # Check if device went offline
        new_failure_count = old_failure_count + 1
        went_offline = not was_offline and new_failure_count >= offline_threshold

        return {
            "went_offline": went_offline,
            "failure_count": new_failure_count,
        }

    async def stats(self) -> Mapping[str, int]:
        return await self.db.run(self._stats)

    def _stats(self, conn: sqlite3.Connection) -> Mapping[str, int]:
        device_counts = conn.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN enabled = 1 THEN 1 ELSE 0 END) AS enabled,
                SUM(CASE WHEN offline = 1 THEN 1 ELSE 0 END) AS offline,
                SUM(CASE WHEN discovered = 1 THEN 1 ELSE 0 END) AS discovered,
                SUM(CASE WHEN manual = 1 THEN 1 ELSE 0 END) AS manual,
                SUM(CASE WHEN enabled = 1 AND configured = 1 AND offline = 0 THEN 1 ELSE 0 END) AS active
            FROM devices
            """
        ).fetchone()
        mapping_counts = conn.execute(
            "SELECT COUNT(*) AS total FROM mappings"
        ).fetchone()
        set_offline_devices(int(device_counts["offline"] or 0))
        return {
            "devices_total": int(device_counts["total"] or 0),
            "devices_enabled": int(device_counts["enabled"] or 0),
            "devices_offline": int(device_counts["offline"] or 0),
            "discovered_count": int(device_counts["discovered"] or 0),
            "manual_count": int(device_counts["manual"] or 0),
            "active_count": int(device_counts["active"] or 0),
            "mappings_total": int(mapping_counts["total"] or 0),
        }

    async def polling_stats(self) -> Mapping[str, int]:
        return await self.db.run(self._polling_stats)

    def _polling_stats(self, conn: sqlite3.Connection) -> Mapping[str, int]:
        rows = conn.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN poll_last_success_at IS NOT NULL THEN 1 ELSE 0 END) AS ever_polled,
                SUM(CASE WHEN poll_last_failure_at IS NOT NULL THEN 1 ELSE 0 END) AS failures
            FROM devices
            WHERE enabled = 1
              AND ip IS NOT NULL
            """
        ).fetchone()
        return {
            "poll_targets": int(rows["total"] or 0),
            "poll_successes": int(rows["ever_polled"] or 0),
            "poll_failures": int(rows["failures"] or 0),
        }

    async def protocol_stats(self) -> Mapping[str, Mapping[str, int]]:
        """Get device counts broken down by protocol."""
        return await self.db.run(self._protocol_stats)

    def _protocol_stats(self, conn: sqlite3.Connection) -> Mapping[str, Mapping[str, int]]:
        rows = conn.execute(
            """
            SELECT
                protocol,
                COUNT(*) AS total,
                SUM(CASE WHEN enabled = 1 THEN 1 ELSE 0 END) AS enabled,
                SUM(CASE WHEN offline = 1 THEN 1 ELSE 0 END) AS offline,
                SUM(CASE WHEN discovered = 1 THEN 1 ELSE 0 END) AS discovered,
                SUM(CASE WHEN manual = 1 THEN 1 ELSE 0 END) AS manual
            FROM devices
            GROUP BY protocol
            ORDER BY protocol
            """
        ).fetchall()

        result: Dict[str, Dict[str, int]] = {}
        for row in rows:
            protocol = row["protocol"] or "unknown"
            result[protocol] = {
                "total": int(row["total"] or 0),
                "enabled": int(row["enabled"] or 0),
                "offline": int(row["offline"] or 0),
                "discovered": int(row["discovered"] or 0),
                "manual": int(row["manual"] or 0),
            }
        return result

    async def refresh_metrics(self) -> None:
        """Refresh gauges derived from the database."""

        await self.db.run(self._refresh_metrics)

    def _refresh_metrics(self, conn: sqlite3.Connection) -> None:
        self._update_offline_metric(conn)
        rows = conn.execute(
            """
            SELECT device_id, COUNT(*) AS depth
            FROM state
            GROUP BY device_id
            """
        ).fetchall()
        for row in rows:
            set_queue_depth(row["device_id"], int(row["depth"]))
        total = conn.execute("SELECT COUNT(*) AS total FROM state").fetchone()
        set_total_queue_depth(int(total["total"] if total else 0))

    def _update_queue_metrics(self, conn: sqlite3.Connection, device_id: str) -> None:
        depth_row = conn.execute(
            "SELECT COUNT(*) AS depth FROM state WHERE device_id = ?",
            (device_id,),
        ).fetchone()
        depth = int(depth_row["depth"] if depth_row else 0)
        set_queue_depth(device_id, depth)
        total_row = conn.execute("SELECT COUNT(*) AS total FROM state").fetchone()
        total = int(total_row["total"] if total_row else 0)
        set_total_queue_depth(total)

    async def quarantine_state(
        self, state: PendingState, payload_hash: str, reason: str, details: Optional[str] = None
    ) -> None:
        await self.db.run(
            lambda conn: self._quarantine_state(conn, state, payload_hash, reason, details)
        )

    def _quarantine_state(
        self,
        conn: sqlite3.Connection,
        state: PendingState,
        payload_hash: str,
        reason: str,
        details: Optional[str],
    ) -> None:
        conn.execute(
            """
            INSERT INTO dead_letters (
                state_id,
                device_id,
                payload,
                payload_hash,
                context_id,
                reason,
                details,
                state_created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                state.id,
                state.device_id,
                state.payload,
                payload_hash,
                state.context_id,
                reason,
                details,
                state.created_at,
            ),
        )
        conn.execute(
            """
            DELETE FROM state
            WHERE id = ?
            """,
            (state.id,),
        )
        conn.commit()
        self._update_queue_metrics(conn, state.device_id)

    async def dead_letters(self, device_id: Optional[str] = None) -> List[DeadLetter]:
        return await self.db.run(lambda conn: self._dead_letters(conn, device_id))

    def _dead_letters(
        self, conn: sqlite3.Connection, device_id: Optional[str] = None
    ) -> List[DeadLetter]:
        query = """
            SELECT
                id,
                state_id,
                device_id,
                payload,
                payload_hash,
                context_id,
                reason,
                details,
                state_created_at,
                created_at
            FROM dead_letters
        """
        params: Tuple[Any, ...] = ()
        if device_id is not None:
            query += " WHERE device_id = ?"
            params = (device_id,)
        query += " ORDER BY created_at ASC"
        rows = conn.execute(query, params).fetchall()
        return [
            DeadLetter(
                id=int(row["id"]),
                state_id=row["state_id"],
                device_id=row["device_id"],
                payload=row["payload"],
                payload_hash=row["payload_hash"],
                context_id=row["context_id"],
                reason=row["reason"],
                details=row["details"],
                state_created_at=row["state_created_at"],
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def _update_offline_metric(self, conn: sqlite3.Connection) -> None:
        row = conn.execute(
            "SELECT COUNT(*) AS offline FROM devices WHERE offline = 1"
        ).fetchone()
        count = int(row["offline"] if row else 0)
        set_offline_devices(count)

        health_rows = conn.execute(
            "SELECT poll_health, COUNT(*) AS count FROM devices GROUP BY poll_health"
        ).fetchall()
        counts = {
            (health_row["poll_health"] or "unknown"): int(health_row["count"] or 0)
            for health_row in health_rows
        }
        set_poll_health_devices(counts)

    def _normalized_capabilities_obj(self, row: sqlite3.Row) -> NormalizedCapabilities:
        model = None
        if "model_number" in row.keys() and row["model_number"]:
            model = row["model_number"]
        elif "model" in row.keys():
            model = row["model"]
        protocol = row["protocol"] if "protocol" in row.keys() else "govee"
        cache = self._get_capability_cache(protocol)
        raw_caps = _deserialize_capabilities(row["capabilities"])
        return cache.normalize(model, raw_caps)

    def _normalized_capabilities_from_row(self, row: sqlite3.Row) -> Any:
        return self._normalized_capabilities_obj(row).as_mapping()

    def _row_to_device(self, row: sqlite3.Row) -> DeviceRow:
        normalized = self._normalized_capabilities_obj(row)
        model_number = None
        if "model_number" in row.keys() and row["model_number"]:
            model_number = row["model_number"]
        elif "model" in row.keys():
            model_number = row["model"]
        metadata = _merge_metadata(row, normalized.metadata)
        name = row["name"] if "name" in row.keys() else None
        return DeviceRow(
            id=row["id"],
            ip=row["ip"],
            protocol=row["protocol"] if "protocol" in row.keys() else "govee",
            name=name,
            model_number=model_number,
            device_type=metadata.get("device_type"),
            length_meters=metadata.get("length_meters"),
            led_count=metadata.get("led_count"),
            led_density_per_meter=metadata.get("led_density_per_meter"),
            has_zones=metadata.get("has_zones"),
            zone_count=metadata.get("zone_count"),
            description=row["description"],
            capabilities=normalized.as_mapping(),
            manual=bool(row["manual"]),
            discovered=bool(row["discovered"]),
            configured=bool(row["configured"]),
            enabled=bool(row["enabled"]),
            stale=bool(row["stale"]),
            offline=bool(row["offline"]),
            poll_health=row["poll_health"] if "poll_health" in row.keys() else "healthy",
            last_seen=row["last_seen"],
            first_seen=row["first_seen"],
            poll_last_success_at=row["poll_last_success_at"]
            if "poll_last_success_at" in row.keys()
            else None,
            poll_last_failure_at=row["poll_last_failure_at"]
            if "poll_last_failure_at" in row.keys()
            else None,
            poll_failure_count=int(row["poll_failure_count"] or 0)
            if "poll_failure_count" in row.keys()
            else 0,
            poll_state=_deserialize_capabilities(row["poll_state"])
            if "poll_state" in row.keys()
            else None,
            poll_state_updated_at=row["poll_state_updated_at"]
            if "poll_state_updated_at" in row.keys()
            else None,
            mapping_count=int(row["mapping_count"] or 0)
            if "mapping_count" in row.keys()
            else 0,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _row_to_mapping(self, row: sqlite3.Row) -> MappingRow:
        return MappingRow(
            id=int(row["id"]),
            device_id=row["device_id"],
            universe=int(row["universe"]),
            channel=int(row["channel"]),
            length=int(row["length"]),
            mapping_type=str(row["mapping_type"]),
            field=row["field"],
            fields=_deserialize_fields(row["fields"] if "fields" in row.keys() else None),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
