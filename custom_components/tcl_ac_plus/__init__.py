"""TCL 空调 Plus —— 集成入口。

目标：一次安装、用 TCL 账号登录（密码 / 短信 / 直接填 token），
自动生成标准 climate 实体，巴法云与 HassLife 都能识别成空调。
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady

from .api import TclApiError, TclAuthError, TclNetworkError, Tokens
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_ACCOUNT_ID,
    CONF_BEEP_MUTE,
    CONF_POLL_INTERVAL,
    CONF_POWER_INVERTED,
    CONF_REFRESH_TOKEN,
    DEFAULT_BEEP_MUTE,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_POWER_INVERTED,
    DOMAIN,
    PLATFORMS,
)
from .hub import TclHub

_LOGGER = logging.getLogger(__name__)


def get_hub(hass: HomeAssistant) -> TclHub:
    return hass.data[DOMAIN]["hub"]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    tokens = Tokens(
        access_token=entry.data.get(CONF_ACCESS_TOKEN, ""),
        refresh_token=entry.data.get(CONF_REFRESH_TOKEN, ""),
        account_id=str(entry.data.get(CONF_ACCOUNT_ID, "")),
    )
    poll_interval = int(entry.options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL))
    power_inverted = bool(entry.options.get(CONF_POWER_INVERTED, DEFAULT_POWER_INVERTED))
    beep_mute = bool(entry.options.get(CONF_BEEP_MUTE, DEFAULT_BEEP_MUTE))

    hub = TclHub(hass, tokens, poll_interval, power_inverted, beep_mute)
    hass.data.setdefault(DOMAIN, {})["hub"] = hub

    def _save_tokens(new_tokens: Tokens) -> None:
        data = dict(entry.data)
        data[CONF_ACCESS_TOKEN] = new_tokens.access_token
        data[CONF_REFRESH_TOKEN] = new_tokens.refresh_token
        data[CONF_ACCOUNT_ID] = new_tokens.account_id
        hass.config_entries.async_update_entry(entry, data=data)

    hub.on_tokens_changed = _save_tokens

    try:
        await hub.async_load_devices()
    except TclAuthError as err:
        # 唯一会打扰用户的场景：凭证彻底失效，必须重新登录
        _LOGGER.warning("TCL 凭证已失效，需要重新登录：%s", err)
        raise ConfigEntryAuthFailed("TCL 登录已失效，请在集成里重新登录") from err
    except TclNetworkError as err:
        # 网络问题不抛异常给用户看，降级为「稍后自动重试」
        _LOGGER.warning("暂时连不上 TCL 云端，稍后自动重试：%s", err)
        raise ConfigEntryNotReady("网络未就绪，稍后自动重试") from err
    except TclApiError as err:
        _LOGGER.warning("TCL 云端返回业务错误，稍后自动重试：%s", err)
        raise ConfigEntryNotReady("云端暂时不可用，稍后自动重试") from err

    await hub.async_start()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    async def _shutdown(_event: Any) -> None:
        await hub.async_stop()

    entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _shutdown))
    entry.async_on_unload(entry.add_update_listener(_async_reload))

    _LOGGER.info(
        "TCL 空调 Plus 启动完成：%d 台设备，%d 台识别为空调",
        len(hub.devices),
        sum(1 for state in hub.devices.values() if state.model.is_air_conditioner),
    )
    return True


async def _async_reload(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hub: TclHub | None = hass.data.get(DOMAIN, {}).get("hub")
        if hub is not None:
            await hub.async_stop()
        hass.data.pop(DOMAIN, None)
    return unloaded
