"""气候实体。

两个决定性约束（决定巴法云能否识别成空调）：
1. hvac_modes 必须包含 HVACMode.COOL。bemfa_cloud 的筛选条件是
   `if HVACMode.COOL not in hvac_modes: continue`，缺了 cool 只会被当成
   温控器(010)或退化成开关(006)。
2. supported_features 显式声明 TARGET_TEMPERATURE / FAN_MODE / SWING_MODE，
   否则巴法云拿不到风速与摆风，米家卡片会缺控件。
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.climate import (
    ATTR_HVAC_MODE,
    SWING_BOTH,
    SWING_HORIZONTAL,
    SWING_OFF,
    SWING_VERTICAL,
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo

from .const import DOMAIN, TEMP_MAX, TEMP_MIN, TEMP_STEP
from .hub import update_signal
from .state import DeviceState
from .model import DeviceModel, fan_percent_to_preset, mode_options, preset_to_fan_percent

_LOGGER = logging.getLogger(__name__)

# 模式文案缺失时的兜底编码（TCL Plus 小程序主流取值）
FALLBACK_MODE_RAW = "1"
FAN_PRESETS = ["auto", "low", "medium", "high", "max"]


class TclClimate(ClimateEntity):
    """TCL 空调主实体。"""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_target_temperature_step = TEMP_STEP

    def __init__(self, hub, state: DeviceState) -> None:
        self._hub = hub
        self._state = state
        self._model: DeviceModel = state.model

        self._attr_unique_id = f"{DOMAIN}_{state.device_id}_climate"
        self._attr_should_poll = False
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, state.device_id)},
            name=state.name,
            manufacturer="TCL",
            model=state.product_key,
            sw_version=state.sw_version,
        )

        self._mode_map: dict[str, HVACMode] = {}
        self._reverse_mode: dict[HVACMode, str] = {}
        self._build_mode_map()

        # 温度范围优先取物模型的取值范围
        temp_attr = self._model.attr("target_temp")
        if temp_attr is not None:
            low, high, _ = temp_attr.numeric_range
            self._attr_min_temp = low if low < high else TEMP_MIN
            self._attr_max_temp = high if high > low else TEMP_MAX
        else:
            self._attr_min_temp = TEMP_MIN
            self._attr_max_temp = TEMP_MAX

        self._attr_fan_modes = (
            list(FAN_PRESETS)
            if self._model.attr("fan") is not None or self._model.attr("fan_auto") is not None
            else []
        )
        self._swing_attr_available = (
            self._model.attr("vswing") is not None or self._model.attr("hswing") is not None
        )
        self._attr_swing_modes = (
            [SWING_OFF, SWING_VERTICAL, SWING_HORIZONTAL, SWING_BOTH]
            if self._swing_attr_available
            else []
        )

        # 只声明设备真的支持的 feature，避免米家/巴法云出现无法操作的控件
        features = ClimateEntityFeature.TURN_ON | ClimateEntityFeature.TURN_OFF
        if temp_attr is not None:
            features |= ClimateEntityFeature.TARGET_TEMPERATURE
        if self._attr_fan_modes:
            features |= ClimateEntityFeature.FAN_MODE
        if self._swing_attr_available:
            features |= ClimateEntityFeature.SWING_MODE
        self._attr_supported_features = features

        self._last_on_mode = HVACMode.COOL

    # ------------------------------------------------------------ 模式映射
    def _build_mode_map(self) -> None:
        mapping = mode_options(self._model.attr("mode"))
        self._mode_map = dict(mapping)
        self._reverse_mode = {mode: raw for raw, mode in mapping.items()}

        # 保证 cool 一定存在，否则巴法云会说「这不是空调」
        if HVACMode.COOL not in self._reverse_mode:
            self._mode_map.setdefault(FALLBACK_MODE_RAW, HVACMode.COOL)
            self._reverse_mode[HVACMode.COOL] = FALLBACK_MODE_RAW

        modes: list[HVACMode] = [HVACMode.OFF]
        for mode in (HVACMode.COOL, HVACMode.HEAT, HVACMode.DRY, HVACMode.FAN_ONLY, HVACMode.AUTO):
            if mode in self._reverse_mode and mode not in modes:
                modes.append(mode)
        self._attr_hvac_modes = modes

    # ------------------------------------------------------------ 状态读取
    @property
    def available(self) -> bool:
        return self._state.available

    def _is_powered_on(self) -> bool | None:
        attr = self._model.attr("power")
        if attr is None:
            return None
        raw = self._state.get(attr.identifier)
        if raw is None:
            return None
        if self._hub.power_inverted and str(raw) in ("0", "1"):
            return str(raw) == "0"
        return attr.bool_is_on(raw)

    def _target_temp(self) -> float | None:
        attr = self._model.attr("target_temp")
        if attr is None:
            return None
        raw = self._state.get(attr.identifier)
        try:
            return float(raw) if raw is not None else None
        except (TypeError, ValueError):
            return None

    def _current_temp(self) -> float | None:
        attr = self._model.attr("current_temp")
        if attr is None:
            return None
        raw = self._state.get(attr.identifier)
        try:
            return float(raw) if raw is not None else None
        except (TypeError, ValueError):
            return None

    @property
    def hvac_mode(self) -> HVACMode | None:
        powered = self._is_powered_on()
        if powered is False:
            return HVACMode.OFF
        if powered is None:
            return None

        mode_attr = self._model.attr("mode")
        if mode_attr is None:
            return self._last_on_mode

        raw = self._state.get(mode_attr.identifier)
        if raw is None:
            return self._last_on_mode
        return self._mode_map.get(str(raw), self._last_on_mode)

    @property
    def hvac_action(self) -> HVACAction | None:
        mode = self.hvac_mode
        if mode == HVACMode.OFF:
            return HVACAction.OFF
        if mode == HVACMode.FAN_ONLY:
            return HVACAction.FAN
        return None

    @property
    def target_temperature(self) -> float | None:
        return self._target_temp()

    @property
    def current_temperature(self) -> float | None:
        return self._current_temp()

    @property
    def fan_mode(self) -> str | None:
        fan_attr = self._model.attr("fan")
        if fan_attr is None:
            auto_attr = self._model.attr("fan_auto")
            if auto_attr is not None and auto_attr.bool_is_on(self._state.get(auto_attr.identifier)):
                return "auto"
            return None
        raw = self._state.get(fan_attr.identifier)
        try:
            percent = float(raw) if raw is not None else None
        except (TypeError, ValueError):
            percent = None
        preset = fan_percent_to_preset(percent, self._model.attr("fan_auto") is not None)
        return preset if preset in self._attr_fan_modes else None

    @property
    def swing_mode(self) -> str | None:
        if not self._swing_attr_available:
            return None
        v_attr = self._model.attr("vswing")
        h_attr = self._model.attr("hswing")
        v_on = bool(v_attr and v_attr.bool_is_on(self._state.get(v_attr.identifier)))
        h_on = bool(h_attr and h_attr.bool_is_on(self._state.get(h_attr.identifier)))
        if v_on and h_on:
            return SWING_BOTH
        if v_on:
            return SWING_VERTICAL
        if h_on:
            return SWING_HORIZONTAL
        return SWING_OFF

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "tcl_device_id": self._state.device_id,
            "tcl_product_key": self._state.product_key,
            "model_source": self._model.source,
            "cloud_state": self._state.cloud_state,
        }

    # ------------------------------------------------------------ 控制
    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        power_attr = self._model.attr("power")
        mode_attr = self._model.attr("mode")
        params: dict[str, Any] = {}

        if hvac_mode == HVACMode.OFF:
            if power_attr is not None:
                params[power_attr.identifier] = power_attr.bool_off_value()
                await self._hub.async_send(self._state.device_id, params)
                self.async_write_ha_state()
            return

        if power_attr is not None and self._is_powered_on() is not True:
            params[power_attr.identifier] = power_attr.bool_on_value()

        raw_mode = self._reverse_mode.get(hvac_mode)
        if mode_attr is not None and raw_mode is not None:
            params[mode_attr.identifier] = int(raw_mode) if raw_mode.isdigit() else raw_mode
            self._last_on_mode = hvac_mode

        if params:
            await self._hub.async_send(self._state.device_id, params)
            self.async_write_ha_state()

    async def async_turn_on(self) -> None:
        target = self._last_on_mode if self._last_on_mode != HVACMode.OFF else HVACMode.COOL
        await self.async_set_hvac_mode(target)

    async def async_turn_off(self) -> None:
        if self.hvac_mode and self.hvac_mode != HVACMode.OFF:
            self._last_on_mode = self.hvac_mode
        await self.async_set_hvac_mode(HVACMode.OFF)

    async def async_set_temperature(self, **kwargs: Any) -> None:
        temp = kwargs.get(ATTR_TEMPERATURE)
        hvac_mode = kwargs.get(ATTR_HVAC_MODE)

        if hvac_mode is not None and hvac_mode != self.hvac_mode:
            await self.async_set_hvac_mode(hvac_mode)

        if temp is None:
            return

        attr = self._model.attr("target_temp")
        if attr is None:
            _LOGGER.warning("设备 %s 未识别到温度属性，无法设置温度", self._state.name)
            return

        low, high, step = attr.numeric_range
        value: float = max(low, min(high, float(temp)))
        if step and step < 1:
            value = round(value / step) * step
        if "int" in attr.raw_type and "double" not in attr.raw_type:
            value = int(round(value))

        await self._hub.async_send(self._state.device_id, {attr.identifier: value})
        self.async_write_ha_state()

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        fan_attr = self._model.attr("fan")
        auto_attr = self._model.attr("fan_auto")
        params: dict[str, Any] = {}

        if fan_attr is not None:
            params[fan_attr.identifier] = preset_to_fan_percent(fan_mode)
        if auto_attr is not None:
            params[auto_attr.identifier] = (
                auto_attr.bool_on_value() if fan_mode == "auto" else auto_attr.bool_off_value()
            )

        if not params:
            _LOGGER.warning("设备 %s 不支持风速设置", self._state.name)
            return
        await self._hub.async_send(self._state.device_id, params)
        self.async_write_ha_state()

    async def async_set_swing_mode(self, swing_mode: str) -> None:
        v_attr = self._model.attr("vswing")
        h_attr = self._model.attr("hswing")
        params: dict[str, Any] = {}

        want_v = swing_mode in (SWING_VERTICAL, SWING_BOTH)
        want_h = swing_mode in (SWING_HORIZONTAL, SWING_BOTH)

        if v_attr is not None:
            params[v_attr.identifier] = v_attr.bool_on_value() if want_v else v_attr.bool_off_value()
        if h_attr is not None:
            params[h_attr.identifier] = h_attr.bool_on_value() if want_h else h_attr.bool_off_value()

        if not params:
            return
        await self._hub.async_send(self._state.device_id, params)
        self.async_write_ha_state()

    # ------------------------------------------------------------ 更新
    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(self.hass, update_signal(self._state.device_id), self._handle_update)
        )


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    """climate 平台入口。"""
    from . import get_hub

    hub = get_hub(hass)
    entities = [TclClimate(hub, state) for state in hub.devices.values() if state.model.is_air_conditioner]
    _LOGGER.info("生成 climate 实体 %d 个（巴法云需要 hvac_modes 含 cool）", len(entities))
    async_add_entities(entities)
