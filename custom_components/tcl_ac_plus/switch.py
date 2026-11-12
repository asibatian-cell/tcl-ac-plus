"""switch 平台：辅助开关（ECO / 睡眠 / 灯光 / 提示音 / 电辅热 / 清洁等）。"""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import get_hub
from .const import CONF_EXPOSE_ALL, DEFAULT_EXPOSE_ALL
from .entities import build_aux_entities

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add: AddEntitiesCallback) -> None:
    hub = get_hub(hass)
    expose_all = entry.options.get(CONF_EXPOSE_ALL, DEFAULT_EXPOSE_ALL)
    entities = []
    for state in hub.devices.values():
        if state.model.is_air_conditioner:
            entities.extend(build_aux_entities(hub, state, expose_all, platform="switch"))
    _LOGGER.info("生成 switch 实体 %d 个", len(entities))
    add(entities)
