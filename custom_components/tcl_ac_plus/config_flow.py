"""配置与选项流程。"""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

# 返回值类型注解在不同 HA 版本里可能来自不同模块：
# 新版是 config_entries.ConfigFlowResult，旧版只有 data_entry_flow.FlowResult。
# 这里两个都试，保证任何版本都能导入成功（导入失败会让配置向导直接 500）。
try:  # pragma: no cover - 取决于 HA 版本
    from homeassistant.config_entries import ConfigFlowResult as FlowResult
except ImportError:  # pragma: no cover
    from homeassistant.data_entry_flow import FlowResult

from .api import TclApiError, TclAuthError, TclCloudClient, TclNetworkError, Tokens
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_ACCOUNT_ID,
    CONF_BEEP_MUTE,
    CONF_EXPOSE_ALL,
    CONF_LOGIN_METHOD,
    CONF_PHONE,
    CONF_POLL_INTERVAL,
    CONF_POWER_INVERTED,
    CONF_REFRESH_TOKEN,
    DEFAULT_BEEP_MUTE,
    DEFAULT_EXPOSE_ALL,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_POWER_INVERTED,
    DOMAIN,
    LOGIN_PASSWORD,
    LOGIN_SMS,
    LOGIN_TOKEN,
    MAX_POLL_INTERVAL,
    MIN_POLL_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

ERROR_AUTH = "auth_error"


def _client(hass) -> TclCloudClient:
    return TclCloudClient(async_get_clientsession(hass), Tokens())


class TclAcPlusConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        # 只能定义自己的私有字段。
        # 注意：_reauth_entry_id / _reconfigure_entry_id / unique_id / config_entry
        # 都是 HA 基类上的**只读 property**，在这里赋值会抛
        # AttributeError: property '_reauth_entry_id' has no setter，
        # 而这段代码是在 HA 实例化 flow（config_entries.py: flow = handler()）
        # 时执行的，表现为前端「无法加载配置向导 500」。
        self._phone: str = ""

    def _reauth_entry(self) -> config_entries.ConfigEntry | None:
        """取当前 reauth 流程对应的 config entry；不是 reauth 就返回 None。

        直接读 context，不走基类的 _reauth_entry_id property（那只读不可写，
        且 source 不是 reauth 时会抛 ValueError），这样各版本 HA 都能用。
        """
        context = self.context or {}
        if context.get("source") != "reauth":
            return None
        entry_id = context.get("entry_id", "")
        if not entry_id or self.hass is None:
            return None
        return self.hass.config_entries.async_get_entry(entry_id)

    # ------------------------------------------------------------ 入口
    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is not None:
            method = user_input[CONF_LOGIN_METHOD]
            if method == LOGIN_PASSWORD:
                return await self.async_step_password()
            if method == LOGIN_SMS:
                return await self.async_step_sms_phone()
            return await self.async_step_token()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_LOGIN_METHOD, default=LOGIN_SMS): vol.In(
                        {
                            LOGIN_SMS: "短信验证码登录（推荐）",
                            LOGIN_PASSWORD: "密码登录",
                            LOGIN_TOKEN: "已有 accountId + refreshToken",
                        }
                    )
                }
            ),
        )

    # ------------------------------------------------------------ 短信
    async def async_step_sms_phone(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            phone = user_input[CONF_PHONE].strip()
            try:
                await _client(self.hass).send_sms(phone)
                self._phone = phone
                return await self.async_step_sms_code()
            except TclAuthError as err:
                _LOGGER.warning("发送验证码被拒：%s", err)
                errors["base"] = "sms_error"
            except (TclApiError, TclNetworkError) as err:
                _LOGGER.warning("发送验证码失败：%s", err)
                errors["base"] = "cannot_connect"

        return self.async_show_form(
            step_id="sms_phone",
            data_schema=vol.Schema({vol.Required(CONF_PHONE, default=self._phone): str}),
            errors=errors,
        )

    async def async_step_sms_code(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                tokens = Tokens()
                client = TclCloudClient(async_get_clientsession(self.hass), tokens)
                await client.login_by_sms(self._phone, user_input["code"].strip())
                info = await client.get_user_info()
                if info.get("account_id"):
                    tokens.account_id = info["account_id"]
                return await self._finish(tokens, info, self._phone)
            except TclAuthError as err:
                _LOGGER.warning("验证码登录失败：%s", err)
                errors["base"] = ERROR_AUTH
            except (TclApiError, TclNetworkError) as err:
                _LOGGER.warning("验证码登录网络异常：%s", err)
                errors["base"] = "cannot_connect"

        return self.async_show_form(
            step_id="sms_code",
            data_schema=vol.Schema({vol.Required("code"): str}),
            errors=errors,
            description_placeholders={"phone": self._phone},
        )

    # ------------------------------------------------------------ 密码
    async def async_step_password(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                tokens = Tokens()
                client = TclCloudClient(async_get_clientsession(self.hass), tokens)
                await client.login_by_password(user_input[CONF_PHONE].strip(), user_input["password"])
                info = await client.get_user_info()
                if info.get("account_id"):
                    tokens.account_id = info["account_id"]
                return await self._finish(tokens, info, user_input[CONF_PHONE].strip())
            except TclAuthError as err:
                _LOGGER.warning("密码登录失败：%s", err)
                errors["base"] = ERROR_AUTH
            except (TclApiError, TclNetworkError) as err:
                _LOGGER.warning("密码登录网络异常：%s", err)
                errors["base"] = "cannot_connect"

        return self.async_show_form(
            step_id="password",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_PHONE): str,
                    vol.Required("password"): str,
                }
            ),
            errors=errors,
        )

    # ------------------------------------------------------------ Token
    async def async_step_token(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            tokens = Tokens(
                access_token=user_input.get(CONF_ACCESS_TOKEN, "").strip(),
                refresh_token=user_input[CONF_REFRESH_TOKEN].strip(),
                account_id=str(user_input[CONF_ACCOUNT_ID]).strip(),
            )
            try:
                if not tokens.access_token:
                    await TclCloudClient(async_get_clientsession(self.hass), tokens).do_refresh_token()
                info = await TclCloudClient(
                    async_get_clientsession(self.hass), tokens
                ).get_user_info()
                return await self._finish(tokens, info, info.get("phone", ""))
            except TclAuthError as err:
                _LOGGER.warning("Token 登录失败：%s", err)
                errors["base"] = ERROR_AUTH
            except (TclApiError, TclNetworkError) as err:
                _LOGGER.warning("Token 登录网络异常：%s", err)
                errors["base"] = "cannot_connect"

        return self.async_show_form(
            step_id="token",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ACCOUNT_ID): str,
                    vol.Required(CONF_REFRESH_TOKEN): str,
                    vol.Optional(CONF_ACCESS_TOKEN, default=""): str,
                }
            ),
            errors=errors,
            description_placeholders={"hint": "refreshToken 可用抓包工具从 TCL 小程序登录请求中提取"},
        )

    # ------------------------------------------------------------ 收尾
    async def _finish(self, tokens: Tokens, info: dict[str, Any], phone: str) -> FlowResult:
        if not tokens.account_id:
            tokens.account_id = str(info.get("account_id", ""))

        unique_id = tokens.account_id or phone or tokens.refresh_token[-12:]
        await self.async_set_unique_id(unique_id)

        data = {
            CONF_ACCOUNT_ID: tokens.account_id,
            CONF_ACCESS_TOKEN: tokens.access_token,
            CONF_REFRESH_TOKEN: tokens.refresh_token,
            CONF_PHONE: phone,
        }

        entry = self._reauth_entry()
        if entry is not None:
            self.hass.config_entries.async_update_entry(entry, data={**entry.data, **data})
            await self.hass.config_entries.async_reload(entry.entry_id)
            return self.async_abort(reason="reauth_successful")

        title = f"TCL 空调 - {phone or tokens.account_id}"
        return self.async_create_entry(title=title, data=data)

    # ------------------------------------------------------------ 重新认证
    async def async_step_reauth(self, entry_data: dict[str, Any]) -> FlowResult:
        entry = self._reauth_entry()
        if entry is not None:
            self._phone = str(entry.data.get(CONF_PHONE, ""))
        return await self.async_step_user()

    # ------------------------------------------------------------ 选项
    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry) -> config_entries.OptionsFlow:
        return TclAcPlusOptionsFlow()


class TclAcPlusOptionsFlow(config_entries.OptionsFlow):
    """选项流程。

    新版 HA 由基类注入 self.config_entry，构造函数不再接收参数。
    """

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        options = self.config_entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_POLL_INTERVAL,
                        default=int(options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL)),
                    ): vol.All(vol.Coerce(int), vol.Range(min=MIN_POLL_INTERVAL, max=MAX_POLL_INTERVAL)),
                    vol.Required(
                        CONF_POWER_INVERTED,
                        default=bool(options.get(CONF_POWER_INVERTED, DEFAULT_POWER_INVERTED)),
                    ): bool,
                    vol.Required(
                        CONF_BEEP_MUTE,
                        default=bool(options.get(CONF_BEEP_MUTE, DEFAULT_BEEP_MUTE)),
                    ): bool,
                    vol.Required(
                        CONF_EXPOSE_ALL,
                        default=bool(options.get(CONF_EXPOSE_ALL, DEFAULT_EXPOSE_ALL)),
                    ): bool,
                }
            ),
            description_placeholders={
                "power_hint": "如果开关状态与实机相反，打开「电源取反」",
                "poll_hint": "MQTT 推送为主，轮询只是兜底，间隔越大越省流量",
                "beep_hint": "保持开启可让空调在下发指令时尽量不再「滴」声（设备支持才会生效）",
            },
        )
