"""设备运行时状态（纯逻辑，不依赖 Home Assistant，便于离线测试）。

这里放两件与「空调滴滴响个不停」直接相关的事：

1. **乐观值覆盖**：刚下发的参数在云端回读确认前优先级最高。
   否则会出现「写入 → 轮询/推送拿回旧值 → 状态回跳 → 上层再次下发」
   的抖动环，每绕一圈空调就滴一声。
2. **重复指令判定**：比较目标值与当前已知值，值没变就不下发。
"""
from __future__ import annotations

import time
from typing import Any

CLOUD_OK = "ok"
CLOUD_AUTH_ERROR = "auth_error"
CLOUD_OFFLINE = "offline"


def same_value(left: Any, right: Any) -> bool:
    """宽松比较：1 与 "1" 与 1.0 视为相同。"""
    if left is right:
        return True
    if left == right:
        return True
    try:
        return float(left) == float(right)
    except (TypeError, ValueError):
        return str(left).strip() == str(right).strip()


class DeviceState:
    """单台设备的运行时状态。"""

    # 乐观值有效期（秒）。超时仍未确认就让位于云端真实值，避免长期失真。
    OPTIMISTIC_TTL = 30.0

    def __init__(self, raw: dict[str, Any], model: Any) -> None:
        self.raw = raw or {}
        self.model = model
        self.snapshot: dict[str, Any] = {}
        self.last_update: float = 0.0
        self.cloud_state: str = CLOUD_OK
        self.available: bool = str(self.raw.get("isOnline", 1)) not in (
            "0",
            "False",
            "false",
            "",
        )
        self._optimistic: dict[str, tuple[Any, float]] = {}

    # ------------------------------------------------------------ 基础属性
    @property
    def device_id(self) -> str:
        return str(self.raw.get("deviceId", ""))

    @property
    def name(self) -> str:
        return str(self.raw.get("nickName") or self.device_id)

    @property
    def product_key(self) -> str:
        return str(self.raw.get("productKey") or "")

    @property
    def sw_version(self) -> str | None:
        version = self.raw.get("firmwareVersion") or self.raw.get("version")
        return str(version) if version else None

    # ------------------------------------------------------------ 乐观值
    def set_optimistic(self, params: dict[str, Any], ttl: float | None = None) -> None:
        now = time.monotonic()
        expires = now + (self.OPTIMISTIC_TTL if ttl is None else float(ttl))
        for key, value in (params or {}).items():
            self._optimistic[str(key)] = (value, expires)

    def clear_optimistic(self) -> None:
        self._optimistic.clear()

    @property
    def optimistic_keys(self) -> list[str]:
        return sorted(self._optimistic)

    def _overlay(self, incoming: dict[str, Any], trusted: bool) -> dict[str, Any]:
        """把尚未过期、且未被云端确认的乐观值盖到 incoming 上。

        trusted 表示 incoming 是否来自云端（轮询 / MQTT 推送）。只有云端
        数据才允许撤销乐观值——本地写入的回显不能自己把自己撤销掉，
        否则紧接着的旧值轮询就能把状态打回原样，白白引发一次重复下发。
        """
        if not self._optimistic:
            return incoming

        now = time.monotonic()
        merged = dict(incoming)
        for key, (value, expires) in list(self._optimistic.items()):
            if now > expires:
                self._optimistic.pop(key, None)
                continue
            if trusted and key in incoming and same_value(incoming[key], value):
                # 云端已确认，撤掉乐观值
                self._optimistic.pop(key, None)
                continue
            merged[key] = value
        return merged

    # ------------------------------------------------------------ 快照
    def apply_snapshot(
        self, new_data: dict[str, Any], *, replace: bool, trusted: bool = True
    ) -> bool:
        """写入快照。trusted=False 表示这是本地写入的回显，不作确认。"""
        if not new_data:
            return False
        payload = self._overlay(dict(new_data), trusted)
        if replace:
            self.snapshot = payload
        else:
            self.snapshot.update(payload)
        self.last_update = time.time()
        return True

    def get(self, key: str) -> Any:
        return self.snapshot.get(key)

    # ------------------------------------------------------------ 去重判定
    def would_change(self, params: dict[str, Any]) -> bool:
        """这批参数里是否有任意一项与当前已知值不同。

        全部相同 → 说明指令是多余的，下发只会让空调白滴一声。
        """
        if not params:
            return False
        for key, value in params.items():
            if key not in self.snapshot:
                return True
            if not same_value(self.snapshot[key], value):
                return True
        return False
