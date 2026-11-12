"""sensor 平台：只读传感器（故障码、滤芯寿命、运行时长等）。"""
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
            entities.extend(build_aux_entities(hub, state, expose_all, platform="sensor"))
    _LOGGER.info("生成 sensor 实体 %d 个", len(entities))
    add(entities)
