"""TCL Plus 云端客户端与 Token 管理。

相比原版 tcl-ha 的三个关键修正：
1. Token 刷新是原地更新，不重载集成、不重建实体、不丢状态。
2. 只有云端明确返回 401/403 才判定凭证失效；网络错误、5xx、限流不清会话。
3. MQTT 使用 TokenManager 当前 token，token 一变就重建连接，避免拿旧 token 死循环重连。
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any

from aiohttp import ClientError, ClientResponse, ClientSession, ClientTimeout

from .const import (
    APP_ENCRYPT_VERSION,
    APP_ID,
    APP_PLATFORM_TYPE,
    APP_SECRET,
    APP_TENANT_ID,
    APP_UUID,
    APP_VERSION,
    IOS_APP_ID,
    IOS_APP_SECRET,
    IOS_TENANT_ID,
    IOS_APP_VERSION,
    IOS_PLATFORM_TYPE,
    IOS_REPORT_STATE,
    IOS_STORE_UUID,
    IOS_USER_AGENT,
    MINIPROGRAM_REFERER,
    MINIPROGRAM_UA,
    URL_CONTROL,
    URL_DEVICES,
    URL_LOGIN,
    URL_MQTT_CONFIG,
    URL_PANEL_CONFIG,
    URL_QUICK_LOGIN,
    URL_REFRESH_TOKEN,
    URL_SMS_CAPTCHA,
    URL_STATUS,
    URL_USER_INFO,
)
from .crypto import encrypt_body, encrypt_url_params, generate_nonce, generate_sign, md5_hash

_LOGGER = logging.getLogger(__name__)

REQUEST_TIMEOUT = ClientTimeout(total=25)

# TCL 业务返回码：这些视为凭证失效，需要重新登录
AUTH_ERROR_CODES = {"401", "403", "40100", "40300", "A0230", "A0401"}


class TclAuthError(Exception):
    """凭证失效，必须重新登录（只有这种情况才通知用户重新配置）。"""


class TclApiError(Exception):
    """接口业务错误，可稍后重试。"""


class TclNetworkError(Exception):
    """网络层面错误（DNS、超时、5xx），不改变会话状态。"""


@dataclass
class Tokens:
    access_token: str = ""
    refresh_token: str = ""
    account_id: str = ""

    def is_valid(self) -> bool:
        return bool(self.access_token)


class TokenManager:
    """集中管理 token 的刷新与持久化。

    并发调用 refresh 时只打一次接口（用锁串行化），避免 refreshToken 被
    多路并发消费导致互相作废。
    """

    def __init__(self, client: "TclCloudClient", on_change) -> None:
        self._client = client
        self._on_change = on_change
        self._lock = asyncio.Lock()
        self._inflight: asyncio.Task | None = None
        self._generation = 0

    @property
    def generation(self) -> int:
        """token 版本号。MQTT 侧用它判断凭证是否已更新。"""
        return self._generation

    async def ensure(self) -> None:
        """确保 token 可用；失效则刷新一次。"""
        if self._client.tokens.is_valid():
            return
        await self.refresh()

    async def refresh(self) -> None:
        """刷新 token。并发调用只打一次接口。

        refreshToken 是一次性凭证，并发刷新会导致互相作废，所以必须串行。
        """
        async with self._lock:
            existing = self._inflight
            if existing is None or existing.done():
                existing = asyncio.ensure_future(self._client.do_refresh_token())
                self._inflight = existing
            task = existing

        try:
            await asyncio.shield(task)
        except Exception:
            # 刷新失败不能让 generation 递增，否则 MQTT 会误判「token 已更新」
            raise
        else:
            self._generation += 1
            await self._on_change()


class TclCloudClient:
    """TCL 云端 API 封装。"""

    def __init__(self, session: ClientSession, tokens: Tokens) -> None:
        self._session = session
        self.tokens = tokens
        self.user_id: str | None = None

    # ------------------------------------------------------------ 请求底层
    async def _request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        json_body: Any = None,
        data: Any = None,
        expect_auth: bool = True,
    ) -> dict[str, Any]:
        try:
            async with self._session.request(
                method, url, headers=headers, json=json_body, data=data, timeout=REQUEST_TIMEOUT
            ) as resp:
                return await self._parse(resp, expect_auth)
        except (ClientError, asyncio.TimeoutError) as err:
            raise TclNetworkError(str(err)) from err

    async def _parse(self, resp: ClientResponse, expect_auth: bool) -> dict[str, Any]:
        if resp.status in (401, 403):
            raise TclAuthError(f"HTTP {resp.status}")
        if resp.status >= 500:
            raise TclNetworkError(f"HTTP {resp.status}")

        try:
            payload = await resp.json(content_type=None)
        except (ValueError, ClientError) as err:
            raise TclNetworkError(f"响应解析失败: {err}") from err

        if not isinstance(payload, dict):
            raise TclApiError(f"响应格式异常: {payload!r}")

        status = str(payload.get("status", "")).upper()
        raw_code = payload.get("code")
        code = "" if raw_code is None else str(raw_code).strip()
        # TCL 的业务码可能是负数（如 -1 表示失败），isdigit() 会把 "-1" 判成非数字，
        # 导致失败响应被当成成功往下传。这里用正则判断。
        numeric_code = bool(re.fullmatch(r"-?\d+", code)) if code else False

        # 统一取错误文案：不同接口分别用 msg / message / failCause
        reason = (
            payload.get("msg")
            or payload.get("message")
            or payload.get("failCause")
            or payload.get("errorCode")
        )

        # 登录接口成功时返回 code=1 + msg="login success!"，与"1 是通用成功码"
        # 的假设冲突。所以只要真的拿到了 token，就直接认定成功，不再看 code。
        if payload.get("accessToken"):
            return payload

        # 只有明确表示鉴权失败的信号才升级为 TclAuthError
        if expect_auth:
            cause = str(reason or "").upper()
            if status in ("FAILED", "FAIL") and any(
                token in cause
                for token in ("TOKEN", "AUTH", "LOGIN", "UNAUTHORIZED", "EXPIRE")
            ):
                raise TclAuthError(str(reason))
            if numeric_code and code in AUTH_ERROR_CODES:
                raise TclAuthError(str(reason) if reason else code)
            if code.upper() in AUTH_ERROR_CODES:
                raise TclAuthError(str(reason) if reason else code)

        # 业务失败但不是鉴权问题：抛可重试错误，并带上云端的原始文案
        if status in ("FAILED", "FAIL"):
            raise TclApiError(str(reason) if reason else "FAILED")
        if numeric_code and code not in ("200", "0"):
            raise TclApiError(str(reason) if reason else f"code={code}")

        return payload

    # ------------------------------------------------------------ 请求头
    def _io_headers(self, content_type: str = "application/x-www-form-urlencoded;charset=utf-8") -> dict[str, str]:
        return {
            "Host": "io.zx.tcljd.com",
            "accessToken": self.tokens.access_token,
            "t-app-version": APP_VERSION,
            "t-store-uuid": APP_UUID,
            "User-Agent": MINIPROGRAM_UA,
            "Content-Type": content_type,
            "xweb_xhr": "1",
            "miniGramSetup": "1",
            "t-platform-type": APP_PLATFORM_TYPE,
            "Referer": MINIPROGRAM_REFERER,
        }

    def _login_headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json;charset=UTF-8",
            "t-platform-type": IOS_PLATFORM_TYPE,
            "uid": "",
            "Accept": "*/*",
            "cid": str(uuid.uuid4()).upper(),
            "Accept-Language": "zh-Hans-CN;q=1, zh-Hant-CN;q=0.9, en-CN;q=0.8",
            "token": "",
            "EncryptVersion": "2.0",
            "t-app-version": IOS_APP_VERSION,
            "t-store-uuid": IOS_STORE_UUID,
            "Encrypt": "true",
            "User-Agent": IOS_USER_AGENT,
            "t-application-name": "",
        }

    # ------------------------------------------------------------ 认证
    async def do_refresh_token(self) -> None:
        if not self.tokens.refresh_token or not self.tokens.account_id:
            raise TclAuthError("缺少 refreshToken 或 accountId，请重新登录")

        url = (
            f"{URL_REFRESH_TOKEN}?appId={APP_ID}"
            f"&accountId={self.tokens.account_id}"
            f"&tenantId={APP_TENANT_ID}"
            f"&appSecret={APP_SECRET}"
        )
        headers = {
            "Host": "cn.account.tcl.com",
            "Content-Type": "application/json;charset=UTF-8",
            "t-platform-type": IOS_PLATFORM_TYPE,
            "TCL-Authorization": self.tokens.access_token,
            "token": self.tokens.access_token,
            "EncryptVersion": APP_ENCRYPT_VERSION,
            "t-app-version": APP_VERSION,
            "t-store-uuid": APP_UUID,
            "User-Agent": IOS_USER_AGENT,
            "Encrypt": "false",
            "refreshToken": self.tokens.refresh_token,
        }
        try:
            payload = await self._request(
                "GET", url, headers=headers, expect_auth=False
            )
        except TclApiError as err:
            # 刷新接口唯一的失败原因就是 refreshToken 不可用，
            # 这种情况只能重新登录，所以升级为鉴权错误。
            raise TclAuthError(f"refreshToken 已失效，需要重新登录：{err}") from err

        access = payload.get("accessToken")
        refresh = payload.get("refreshToken")
        if not access or not refresh:
            raise TclAuthError(f"刷新 token 失败: {payload.get('message', payload)}")
        self.tokens.access_token = access
        self.tokens.refresh_token = refresh
        _LOGGER.info("TCL token 已刷新（原地，不重载集成）")

    async def get_user_info(self) -> dict[str, Any]:
        url = f"{URL_USER_INFO}?appId={APP_ID}&tenantId={APP_TENANT_ID}&appSecret={APP_SECRET}"
        headers = {
            "Host": "cn.account.tcl.com",
            "TCL-Authorization": self.tokens.access_token,
            "xweb_xhr": "1",
            "t-app-version": APP_VERSION,
            "t-store-uuid": APP_UUID,
            "t-platform-type": APP_PLATFORM_TYPE,
            "User-Agent": MINIPROGRAM_UA,
            "Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
            "Referer": MINIPROGRAM_REFERER,
        }
        try:
            payload = await self._request("GET", url, headers=headers, expect_auth=False)
        except TclApiError as err:
            # 这个接口的唯一失败原因就是 token 不可用，统一升级为鉴权错误
            raise TclAuthError(f"获取用户信息失败：{err}") from err

        data = payload.get("data") or {}
        return {
            "account_id": str(data.get("accountId", self.tokens.account_id)),
            "phone": data.get("phone", ""),
            "username": data.get("username", ""),
        }

    async def login_by_password(self, phone: str, password: str) -> None:
        params = {
            "deviceId": str(uuid.uuid4()).upper(),
            "password": md5_hash(password),
            "channel": IOS_STORE_UUID,
            "tenantId": IOS_TENANT_ID,
            "appSecret": IOS_APP_SECRET,
            "username": phone,
            "appId": IOS_APP_ID,
            "reportState": IOS_REPORT_STATE,
        }
        url = f"{URL_LOGIN}?{encrypt_url_params(params)}"
        payload = await self._request(
            "POST", url, headers=self._login_headers(), data=encrypt_body(params), expect_auth=False
        )
        self._store_login_result(payload, phone)

    async def send_sms(self, phone: str) -> None:
        params = {
            "appId": IOS_APP_ID,
            "appSecret": IOS_APP_SECRET,
            "tenantId": IOS_TENANT_ID,
            "timestamp": str(int(time.time() * 1000)),
            "nonce": generate_nonce(),
            "mobile": phone,
            "bType": "LOGIN",
        }
        params["sign"] = generate_sign(params)
        url = f"{URL_SMS_CAPTCHA}?{encrypt_url_params(params)}"
        payload = await self._request(
            "GET", url, headers=self._login_headers(), expect_auth=False
        )
        status = payload.get("status", "")
        code = payload.get("code")
        if status != "SUCCESS" and str(code) not in ("1", "200", "0"):
            raise TclApiError(payload.get("message") or f"短信发送失败: {payload}")

    async def login_by_sms(self, phone: str, code: str) -> None:
        params = {
            "deviceId": str(uuid.uuid4()).upper(),
            "bType": "LOGIN",
            "appSecret": IOS_APP_SECRET,
            "username": phone,
            "appId": IOS_APP_ID,
            "tenantId": IOS_TENANT_ID,
            "validCode": code,
            "reportState": IOS_REPORT_STATE,
        }
        url = f"{URL_QUICK_LOGIN}?{encrypt_url_params(params)}"
        payload = await self._request(
            "POST", url, headers=self._login_headers(), data=encrypt_body(params), expect_auth=False
        )
        self._store_login_result(payload, phone)

    def _store_login_result(self, payload: dict[str, Any], phone: str) -> None:
        access = payload.get("accessToken")
        refresh = payload.get("refreshToken")
        if not access or not refresh:
            # TCL 登录失败时返回 code=-1 + msg（中文），把它提取出来，
            # 否则整坨 dict 塞进异常里没法看。
            reason = (
                payload.get("msg")
                or payload.get("message")
                or payload.get("errorCode")
                or "未知原因"
            )
            raise TclAuthError(str(reason))
        self.tokens.access_token = access
        self.tokens.refresh_token = refresh
        self.tokens.account_id = str(payload.get("accountId", self.tokens.account_id))

    # ------------------------------------------------------------ 业务接口
    async def get_devices(self) -> list[dict[str, Any]]:
        payload = await self._request("GET", URL_DEVICES, headers=self._io_headers(), expect_auth=True)
        return list(payload.get("data") or [])

    async def get_mqtt_config(self) -> dict[str, Any]:
        payload = await self._request("GET", URL_MQTT_CONFIG, headers=self._io_headers(), expect_auth=True)
        return payload.get("data") or {}

    async def get_panel_config(self, product_key: str) -> list[dict[str, Any]]:
        url = f"{URL_PANEL_CONFIG}?productKey={product_key}"
        payload = await self._request("GET", url, headers=self._io_headers(), expect_auth=True)
        data = payload.get("data") or {}
        pages = data.get("pages") or {}
        home = pages.get("home")
        return list(home or [])

    async def get_status(self, device_id: str) -> dict[str, Any]:
        headers = self._io_headers("application/json;charset=UTF-8")
        payload = await self._request(
            "POST", URL_STATUS, headers=headers, json_body={"deviceId": device_id}, expect_auth=True
        )
        data = payload.get("data") or {}
        return dict(data.get("status") or {})

    async def send_command(self, device_id: str, params: dict[str, Any]) -> None:
        url = URL_CONTROL.format(device_id=device_id)
        headers = self._io_headers("application/json;charset=UTF-8")
        body = {
            "msgId": f"miniprogram_{int(random.random() * 1e5)}_{int(time.time() * 1000)}",
            "source": "miniprogram",
            "version": "3.0.0",
            "params": [{str(k): v} for k, v in params.items()],
        }
        await self._request("POST", url, headers=headers, json_body=body, expect_auth=True)
