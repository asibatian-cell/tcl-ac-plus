"""辅助实体：开关、数值、选择、传感器。

只有「不属于 climate 主实体已经接管的核心属性」才会在这里生成，
避免与 climate 重复控制同一 DP 造成状态抖动。
"""
from __future__ import annotations

import logging
import re
from typing import Any

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.components.select import SelectEntity
from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo, Entity

from .const import DOMAIN
from .hub import update_signal
from .model import AttrSpec, matched_identifiers
from .state import DeviceState

_LOGGER = logging.getLogger(__name__)

# climate 主实体已经接管的角色（保留作文档用途，判定改用 matched_identifiers）
CORE_ROLES = ("power", "mode", "target_temp", "fan", "fan_auto", "vswing", "hswing", "current_temp")

# 只读传感器角色（不做控制）
READONLY_HINTS = ("status", "fault", "error", "level", "percentage", "time", "count", "code")


def _slug(text: str) -> str:
    cleaned = re.sub(r"[^0-9a-zA-Z_\u4e00-\u9fa5]+", "_", str(text)).strip("_").lower()
    return cleaned or "attr"


class _BaseAux(Entity):
    _attr_should_poll = False
    _attr_has_entity_name = True

    def __init__(self, hub, state: DeviceState, spec: AttrSpec, suffix: str) -> None:
        self._hub = hub
        self._state = state
        self._spec = spec
        self._attr_unique_id = f"{DOMAIN}_{state.device_id}_{suffix}"
        self._attr_name = spec.title
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, state.device_id)},
            name=state.name,
            manufacturer="TCL",
            model=state.product_key,
        )

    @property
    def available(self) -> bool:
        return self._state.available

    def _raw(self) -> Any:
        return self._state.get(self._spec.identifier)

    async def _send(self, value: Any) -> None:
        await self._hub.async_send(self._state.device_id, {self._spec.identifier: value})
        self.async_write_ha_state()

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(self.hass, update_signal(self._state.device_id), self._handle_update)
        )


class TclAuxSwitch(_BaseAux, SwitchEntity):
    PLATFORM = "switch"
    _attr_device_class = SwitchDeviceClass.SWITCH

    @property
    def is_on(self) -> bool | None:
        return self._spec.bool_is_on(self._raw())

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._send(self._spec.bool_on_value())

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._send(self._spec.bool_off_value())


class TclAuxNumber(_BaseAux, NumberEntity):
    PLATFORM = "number"
    _attr_mode = NumberMode.SLIDER

    def __init__(self, hub, state: DeviceState, spec: AttrSpec, suffix: str) -> None:
        super().__init__(hub, state, spec, suffix)
        low, high, step = spec.numeric_range
        self._attr_native_min_value = low
        self._attr_native_max_value = high
        self._attr_native_step = step
        unit = spec.specs.get("unit")
        if unit:
            self._attr_native_unit_of_measurement = str(unit)

    @property
    def native_value(self) -> float | None:
        raw = self._raw()
        try:
            return float(raw) if raw is not None else None
        except (TypeError, ValueError):
            return None

    async def async_set_native_value(self, value: float) -> None:
        _, _, step = self._spec.numeric_range
        if step and step < 1:
            value = round(value / step) * step
        if "int" in self._spec.raw_type and "double" not in self._spec.raw_type:
            value = int(round(value))
        await self._send(value)


class TclAuxSelect(_BaseAux, SelectEntity):
    PLATFORM = "select"

    @property
    def options(self) -> list[str]:
        return list(self._spec.enum_map().values())

    @property
    def current_option(self) -> str | None:
        raw = self._raw()
        if raw is None:
            return None
        return self._spec.enum_map().get(str(raw))

    def _raw_for_option(self, option: str) -> Any:
        for raw, text in self._spec.enum_map().items():
            if text == option:
                return int(raw) if raw.lstrip("-").isdigit() else raw
        return option

    async def async_select_option(self, option: str) -> None:
        await self._send(self._raw_for_option(option))


class TclAuxSensor(_BaseAux, SensorEntity):
    PLATFORM = "sensor"

    def __init__(self, hub, state: DeviceState, spec: AttrSpec, suffix: str) -> None:
        super().__init__(hub, state, spec, suffix)
        # state_class 只能给数值型，否则 HA 会拒绝该实体
        if spec.is_number or (not spec.is_enum and not spec.is_bool):
            self._attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def _is_numeric_sensor(self) -> bool:
        return self._spec.is_number

    @property
    def native_value(self) -> Any:
        raw = self._raw()
        if raw is None:
            return None
        if self._spec.is_enum:
            return self._spec.enum_map().get(str(raw), str(raw))
        if isinstance(raw, (int, float)):
            return raw
        try:
            return float(raw)
        except (TypeError, ValueError):
            return str(raw)

    @property
    def native_unit_of_measurement(self) -> str | None:
        if not self._spec.is_number:
            return None
        unit = self._spec.specs.get("unit")
        return str(unit) if unit else None


def build_aux_entities(hub, state: DeviceState, expose_all: bool, platform: str | None = None) -> list[Entity]:
    """为一台设备生成辅助实体。platform 非空时只返回该平台。

    分流规则（真机 1112044844N / 1112014508N 验证后确定）：
    - 已被 climate 接管的字段 → 不重复出实体
    - 纯遥测字段（generalDeviceStatus / AI节能 / Wi-Fi 元数据）→ 默认不生成
    - 不同 function 分组里的同义字段（PTC/PTCStatus、sleep/sleepState）→ 只留第一个
    """
    model = state.model
    # 排除所有「已被 climate 接管」的属性（精确匹配 + 实际选中的角色），
    # 否则同义重复字段（如 powerSwitch 与 Power）会各出一个开关，
    # 两个入口控同一个 DP，导致重复下发、空调滴滴响。
    claimed = matched_identifiers(model.attributes, model.roles)

    # 同义字段合并：按 function + title 去重，保留首个
    seen_titles: set[tuple[str, str]] = set()
    entities: list[Entity] = []

    def want(cls: type) -> bool:
        return platform is None or getattr(cls, "PLATFORM", None) == platform

    for spec in model.attributes:
        if spec.identifier in claimed:
            continue

        # 遥测字段默认不生成，除非用户显式打开「暴露全部」
        if spec.is_readonly_telemetry and not expose_all:
            continue

        dedup_key = (spec.function, spec.title)
        if spec.function and dedup_key in seen_titles:
            continue
        if spec.function:
            seen_titles.add(dedup_key)

        suffix = _slug(spec.identifier)

        # 遥测字段只做只读传感器，绝不生成可写的 number/select/switch，
        # 否则用户一动滑块就会下发非法指令，空调会乱响或报错。
        if spec.is_bool:
            if spec.is_readonly_telemetry:
                if want(TclAuxSensor):
                    entities.append(TclAuxSensor(hub, state, spec, suffix))
            elif want(TclAuxSwitch):
                entities.append(TclAuxSwitch(hub, state, spec, suffix))
        elif spec.is_enum:
            if not spec.enum_map():
                continue
            if spec.is_readonly_telemetry:
                if want(TclAuxSensor):
                    entities.append(TclAuxSensor(hub, state, spec, suffix))
            elif want(TclAuxSelect):
                entities.append(TclAuxSelect(hub, state, spec, suffix))
        elif spec.is_number:
            readonly = spec.is_readonly_telemetry or any(
                hint in spec.norm for hint in READONLY_HINTS
            )
            if readonly or expose_all:
                if want(TclAuxSensor):
                    entities.append(TclAuxSensor(hub, state, spec, suffix))
            elif want(TclAuxNumber):
                entities.append(TclAuxNumber(hub, state, spec, suffix))
        else:
            if want(TclAuxSensor):
                entities.append(TclAuxSensor(hub, state, spec, suffix))

    return entities
