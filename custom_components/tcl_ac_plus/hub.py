"""运行时：设备注册表、MQTT 推送、轮询兜底。

关键设计：
- 推送（MQTT）是主通道，轮询是兜底。任一通道拿到数据都写同一份快照。
- MQTT 断线由 supervisor 统一重建，重建时取 TokenManager 的当前 token，
  不会像原版那样拿着过期 token 无限重连。
- token 刷新后不重载集成，只重建 MQTT 会话，实体状态不丢。
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
import ssl
import time
from typing import Any, Callable

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .api import (
    TclApiError,
    TclAuthError,
    TclCloudClient,
    TclNetworkError,
    TokenManager,
    Tokens,
)
from .const import DOMAIN, MQTT_HOST, MQTT_PATH, MQTT_PORT
from .gate import CommandGate
from .model import DeviceModel, build_model
from .state import CLOUD_AUTH_ERROR, CLOUD_OFFLINE, CLOUD_OK, DeviceState

_LOGGER = logging.getLogger(__name__)

UPDATE_SIGNAL = f"{DOMAIN}_update_{{device_id}}"


class _MqttUnavailable(Exception):
    """paho-mqtt 不可用。推送通道缺席不影响控制，直接停掉 supervisor。"""


def update_signal(device_id: str) -> str:
    return UPDATE_SIGNAL.format(device_id=device_id)


class TclHub:
    """集成级单例，挂在 hass.data[DOMAIN]["hub"]。"""

    def __init__(
        self,
        hass: HomeAssistant,
        tokens: Tokens,
        poll_interval: int,
        power_inverted: bool,
        beep_mute: bool = True,
    ) -> None:
        self.hass = hass
        self.tokens = tokens
        self.poll_interval = max(30, int(poll_interval))
        self.power_inverted = power_inverted
        # 默认把空调提示音关掉。用户明确要听声音时可在选项里关掉这个开关。
        self.beep_mute = beep_mute
        self.devices: dict[str, DeviceState] = {}
        self.on_tokens_changed: Callable[[Tokens], None] | None = None

        self.client = TclCloudClient(async_get_clientsession(hass), tokens)
        self.token_manager = TokenManager(self.client, self._on_token_changed)

        # 指令闸门：杜绝同一条指令被反复下发（空调每次收到指令都会"滴"一声）
        self.gate = CommandGate(min_interval=2.0, same_window=10.0)

        # 记录哪些设备不接受静音字段，避免每次都白试一遍
        self._beep_unsupported: set[str] = set()

        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._models: dict[str, dict[str, Any]] = {}
        self._mqtt_generation = -1

    # ------------------------------------------------------------ 生命周期
    async def async_start(self) -> None:
        self._tasks.append(
            self.hass.async_create_background_task(self._poll_loop(), f"{DOMAIN}-poll")
        )
        # MQTT 只用来拿实时推送，拿不到也不影响控制（轮询兜底仍在跑），
        # 所以它失败时绝不升级成 HA 的 Repairs 通知或错误日志。
        self._tasks.append(
            self.hass.async_create_background_task(self._mqtt_supervisor(), f"{DOMAIN}-mqtt")
        )

    async def async_stop(self) -> None:
        self._stop.set()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()

    async def _on_token_changed(self) -> None:
        """token 换了：MQTT 下一轮用新凭证重建，并把新 token 写回 config entry。"""
        self._mqtt_generation = -1
        _LOGGER.info("TCL token 已更新，MQTT 将用新凭证重建连接")
        if self.on_tokens_changed is not None:
            self.on_tokens_changed(self.tokens)

    # ------------------------------------------------------------ 设备加载
    async def async_load_devices(self) -> list[DeviceState]:
        await self.token_manager.ensure()
        raw_devices = await self._with_auth(self.client.get_devices)
        loaded: list[DeviceState] = []

        for raw in raw_devices:
            device_id = str(raw.get("deviceId", ""))
            if not device_id:
                continue

            snapshot: dict[str, Any] = {}
            with contextlib.suppress(TclApiError, TclNetworkError, TclAuthError):
                snapshot = await self._with_auth(lambda: self.client.get_status(device_id))

            model = await self._load_model(
                str(raw.get("productKey") or ""),
                snapshot,
                str(raw.get("category") or ""),
            )
            state = DeviceState(raw, model)
            if snapshot:
                state.apply_snapshot(snapshot, replace=True)

            self.devices[device_id] = state
            loaded.append(state)

            if not model.is_air_conditioner:
                _LOGGER.warning(
                    "设备 %s（productKey=%s）未识别为空调，已识别属性：%s",
                    state.name,
                    state.product_key,
                    [attr.identifier for attr in model.attributes][:20],
                )

        _LOGGER.info(
            "TCL 设备加载完成：共 %d 台，识别为空调 %d 台",
            len(loaded),
            sum(1 for state in loaded if state.model.is_air_conditioner),
        )
        return loaded

    async def _load_model(
        self, product_key: str, snapshot: dict[str, Any], category: str = ""
    ) -> DeviceModel:
        cached = self._models.get(product_key)
        if cached is not None:
            return build_model(
                product_key,
                cached.get("attributes"),
                snapshot,
                cached.get("source", "cache"),
                category,
            )

        raw_attrs: list[dict[str, Any]] | None = None
        source = "cloud"
        try:
            raw_attrs = await self._with_auth(lambda: self.client.get_panel_config(product_key))
        except (TclApiError, TclNetworkError, TclAuthError) as err:
            _LOGGER.warning("设备 productKey=%s 拉取物模型失败：%s", product_key, err)

        if raw_attrs:
            self._models[product_key] = {"attributes": raw_attrs, "source": source}
        else:
            source = "generic"

        return build_model(product_key, raw_attrs, snapshot, source, category)

    # ------------------------------------------------------------ 鉴权包装
    async def _with_auth(self, factory: Callable[[], Any]) -> Any:
        """执行云端调用；遇鉴权错误刷新 token 后重试一次。

        入参必须是「零参可调用对象」，返回协程。协程对象只能 await 一次，
        所以失败后重试必须重新构造。
        """
        try:
            return await factory()
        except TclAuthError:
            _LOGGER.info("云端判定凭证失效，刷新 token 后重试一次")
            await self.token_manager.refresh()
        return await factory()

    # ------------------------------------------------------------ 控制
    async def async_send(self, device_id: str, params: dict[str, Any]) -> bool:
        """下发指令。返回 True 表示真的发出去了。

        全程不向上抛异常——指令失败只记一条 warning，避免在 HA 日志里刷出
        大段 traceback。用户的体感是「别报错、别乱响」，优先满足这个。
        """
        if not params:
            return False
        state = self.devices.get(device_id)
        payload = self._with_beep_policy(state, params)

        # 闸门 1：值没变就不下发。上层（巴法云 / 米家）经常因为状态回读
        # 不一致而重复下发同一条指令，每一次都会让空调滴一声。
        if state is not None and not state.would_change(payload):
            _LOGGER.debug(
                "跳过无效指令 device=%s params=%s（当前值已相同）", device_id, payload
            )
            return False

        # 闸门 2/3：同值去重、最小间隔、超量熔断
        if not self.gate.allow(device_id, payload):
            _LOGGER.debug(
                "跳过重复指令 device=%s params=%s（%s）",
                device_id, payload, self.gate.blocked_counts,
            )
            return False

        try:
            await self._with_auth(lambda: self.client.send_command(device_id, payload))
        except (TclApiError, TclNetworkError) as err:
            # 关键兜底：如果失败是因为我们擅自塞了 beep 字段（机型不认识它），
            # 必须退回原始指令重试一次，否则用户改温度会完全失效。
            if payload is not params:
                _LOGGER.debug("附带静音字段被拒（%s），退回原始指令重试", err)
                try:
                    await self._with_auth(
                        lambda: self.client.send_command(device_id, params)
                    )
                except TclAuthError as retry_err:
                    _LOGGER.debug("TCL 凭证失效，指令未下发：%s", retry_err)
                    if state is not None:
                        state.cloud_state = CLOUD_AUTH_ERROR
                        self.async_notify(state)
                    return False
                except (TclApiError, TclNetworkError) as retry_err:
                    _LOGGER.warning("TCL 云端指令失败（device=%s）：%s", device_id, retry_err)
                    if state is not None:
                        state.cloud_state = CLOUD_OFFLINE
                        self.async_notify(state)
                    return False
                # 退路成功：以后给这台设备就不再夹带 beep 字段
                self._beep_unsupported.add(device_id)
                payload = params
            else:
                _LOGGER.warning("TCL 云端指令失败（device=%s）：%s", device_id, err)
                if state is not None:
                    state.cloud_state = CLOUD_OFFLINE
                    self.async_notify(state)
                return False
        except TclAuthError as err:
            _LOGGER.debug("TCL 凭证失效，指令未下发：%s", err)
            if state is not None:
                state.cloud_state = CLOUD_AUTH_ERROR
                self.async_notify(state)
            return False

        if state is not None:
            state.cloud_state = CLOUD_OK
            # 乐观值：云端回读确认前不被旧快照覆盖，避免状态回跳引发二次下发
            state.set_optimistic(params)
            # trusted=False：这是本地写入的回显，不能用来「确认」乐观值
            state.apply_snapshot(params, replace=False, trusted=False)
            self.async_notify(state)
        return True

    def _with_beep_policy(
        self, state: DeviceState | None, params: dict[str, Any]
    ) -> dict[str, Any]:
        """指令附带关提示音。

        空调每收一条控制指令就「滴」一声，这是设备固件行为，云端改不了。
        但多数 TCL 机型有 beep/buzzer 这个 DP，把它一并置成关闭后，
        后续指令就不再响。只有设备确实上报过这个属性时才发，
        避免给不支持的机型塞未知字段导致整条指令被拒。
        """
        if not self.beep_mute or state is None:
            return params
        if state.device_id in self._beep_unsupported:
            return params

        beep_attr = state.model.attr("beep") if state.model is not None else None
        if beep_attr is None:
            return params

        payload = dict(params)
        payload.setdefault(beep_attr.identifier, beep_attr.bool_off_value())
        return payload

    # ------------------------------------------------------------ 通知
    def async_notify(self, state: DeviceState) -> None:
        async_dispatcher_send(self.hass, update_signal(state.device_id))

    # ------------------------------------------------------------ 轮询兜底
    async def _poll_loop(self) -> None:
        await asyncio.sleep(3)
        auth_failures = 0
        while not self._stop.is_set():
            for device_id in list(self.devices):
                if self._stop.is_set():
                    return
                try:
                    snapshot = await self._with_auth(lambda: self.client.get_status(device_id))
                    auth_failures = 0
                except TclAuthError as err:
                    # 只记一次，避免每轮刷屏
                    auth_failures += 1
                    if auth_failures == 1:
                        _LOGGER.debug("轮询遇凭证失效：%s", err)
                    with contextlib.suppress(Exception):
                        await self.token_manager.refresh()
                    continue
                except (TclApiError, TclNetworkError) as err:
                    _LOGGER.debug("轮询 %s 失败：%s", device_id, err)
                    continue

                state = self.devices.get(device_id)
                if state is not None and state.apply_snapshot(snapshot, replace=True):
                    state.cloud_state = CLOUD_OK
                    self.async_notify(state)

                await asyncio.sleep(0.4)

            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=self.poll_interval)

    # ------------------------------------------------------------ MQTT
    async def _mqtt_supervisor(self) -> None:
        backoff = 5
        while not self._stop.is_set():
            generation = self.token_manager.generation
            try:
                await self._run_mqtt_session()
                backoff = 5
            except asyncio.CancelledError:
                raise
            except _MqttUnavailable as err:
                # paho-mqtt 没装（manifest 不再声明该依赖，避免配置向导 500）。
                # 推送只是加速通道，轮询照样能用，所以直接退出、不无限重试。
                _LOGGER.info("MQTT 推送不可用（缺少 paho-mqtt），仅使用轮询：%s", err)
                return
            except TclAuthError as err:
                # MQTT 只是实时推送通道，失败不影响控制。降级 debug，不刷日志。
                _LOGGER.debug("MQTT 鉴权失败：%s，尝试刷新 token", err)
                with contextlib.suppress(Exception):
                    await self.token_manager.refresh()
                backoff = 5
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("MQTT 会话异常：%s", err)
                backoff = min(backoff * 2, 120)

            if self._stop.is_set():
                return
            if self.token_manager.generation != generation:
                backoff = 3
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=backoff)

    async def _run_mqtt_session(self) -> None:
        """跑一次 MQTT 会话，直到断线或取消。"""
        try:
            import paho.mqtt.client as mqtt
        except ImportError as err:
            raise _MqttUnavailable("缺少 paho-mqtt") from err

        await self.token_manager.ensure()
        config = await self._with_auth(self.client.get_mqtt_config)
        user_id = str(config.get("userId") or "")
        if not user_id:
            raise TclAuthError("云端未返回 MQTT userId")
        self.client.user_id = user_id

        loop = self.hass.loop
        disconnected = asyncio.Event()

        client_id = (
            f"{user_id}@miniprogram@"
            f"{random.randint(1000, 9999)}{int(time.time() * 1000)}"
        )

        client = _make_client(mqtt, client_id)
        client.username_pw_set(username=user_id, password=self.tokens.access_token)
        client.ws_set_options(path=MQTT_PATH)
        client.reconnect_delay_set(min_delay=1, max_delay=30)

        ctx = await self.hass.async_add_executor_job(ssl.create_default_context)
        client.tls_set_context(ctx)

        def on_connect(_c, _u, _flags, rc, _props=None):
            if rc == 0:
                for state in self.devices.values():
                    _c.subscribe(
                        f"/sys/{state.product_key}/{state.device_id}/thing/event/property/post",
                        qos=1,
                    )
                    _c.subscribe(
                        f"/sys/{state.product_key}/{state.device_id}/thing/event/+",
                        qos=1,
                    )
                _LOGGER.info("MQTT 已连接，订阅 %d 台设备", len(self.devices))
            else:
                _LOGGER.warning("MQTT 连接被拒，返回码 %s", rc)
                loop.call_soon_threadsafe(disconnected.set)

        def on_message(_c, _u, msg):
            try:
                payload = json.loads(msg.payload.decode())
            except (ValueError, UnicodeDecodeError):
                return
            parts = msg.topic.split("/")
            if len(parts) < 5:
                return
            device_id = parts[3]

            if "/thing/event/property/post" in msg.topic:
                params = payload.get("params")
                if not isinstance(params, dict):
                    return
                values = {
                    key: (val.get("value") if isinstance(val, dict) else val)
                    for key, val in params.items()
                }
                loop.call_soon_threadsafe(self._apply_push, device_id, values)
            elif "pushnotice" in msg.topic:
                value = ((payload.get("params") or {}).get("value")) or {}
                status = value.get("status")
                if status is not None:
                    loop.call_soon_threadsafe(self._apply_online, device_id, status)

        def on_disconnect(_c, _u, rc, _props=None):
            if rc != 0:
                _LOGGER.info("MQTT 断开，返回码 %s", rc)
            loop.call_soon_threadsafe(disconnected.set)

        client.on_connect = on_connect
        client.on_message = on_message
        client.on_disconnect = on_disconnect

        await self.hass.async_add_executor_job(client.connect, MQTT_HOST, MQTT_PORT, 30)
        client.loop_start()

        try:
            stop_task = asyncio.ensure_future(self._stop.wait())
            disc_task = asyncio.ensure_future(disconnected.wait())
            done, pending = await asyncio.wait(
                {stop_task, disc_task}, timeout=1800, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            if not done:
                _LOGGER.debug("MQTT 会话空闲超时，主动重连")
        finally:
            with contextlib.suppress(Exception):
                await self.hass.async_add_executor_job(client.loop_stop)
            with contextlib.suppress(Exception):
                await self.hass.async_add_executor_job(client.disconnect)

    def _apply_push(self, device_id: str, values: dict[str, Any]) -> None:
        state = self.devices.get(device_id)
        if state is None or not values:
            return
        if state.apply_snapshot(values, replace=False):
            state.cloud_state = CLOUD_OK
            state.available = True
            self.async_notify(state)

    def _apply_online(self, device_id: str, status: Any) -> None:
        state = self.devices.get(device_id)
        if state is None:
            return
        state.available = str(status).lower() in ("1", "true", "online", "on")
        self.async_notify(state)


def _make_client(mqtt, client_id: str):
    """兼容 paho-mqtt 1.x 与 2.x。"""
    try:
        return mqtt.Client(
            client_id=client_id,
            clean_session=True,
            protocol=mqtt.MQTTv311,
            transport="websockets",
            callback_api_version=mqtt.CallbackAPIVersion.VERSION1,
        )
    except (AttributeError, TypeError):
        return mqtt.Client(
            client_id=client_id,
            clean_session=True,
            protocol=mqtt.MQTTv311,
            transport="websockets",
        )
