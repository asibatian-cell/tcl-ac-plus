"""指令闸门：去重 + 限速 + 熔断。

空调每收到一条控制指令就会"滴"一声。上层（巴法云 / 米家 / 自动化）如果因为
状态回读不一致而反复下发，就会变成连续滴滴响。这个模块是纯逻辑、不依赖
Home Assistant，便于离线单测。

三道闸：
1. **去重**——同一条指令在 same_window 秒内只放行一次；
2. **限速**——同一设备两次放行至少间隔 min_interval 秒；
3. **熔断**——任意 burst_window 秒内超过 burst_limit 条，直接锁死该设备
   burst_cooldown 秒。这是最后一道保险：即使上游出现病态循环，也绝不可能
   让空调一直响下去。
"""
from __future__ import annotations

import json
import time
from typing import Any


class CommandGate:
    """按设备维度的指令去重、限速与熔断闸门。"""

    def __init__(
        self,
        min_interval: float = 2.0,
        same_window: float = 10.0,
        burst_limit: int = 6,
        burst_window: float = 60.0,
        burst_cooldown: float = 60.0,
    ) -> None:
        self.min_interval = max(0.0, float(min_interval))
        self.same_window = max(0.0, float(same_window))
        self.burst_limit = max(1, int(burst_limit))
        self.burst_window = max(1.0, float(burst_window))
        self.burst_cooldown = max(0.0, float(burst_cooldown))

        self._last_time: dict[str, float] = {}
        self._last_key: dict[str, str] = {}
        self._last_key_time: dict[str, float] = {}
        self._history: dict[str, list[float]] = {}
        self._locked_until: dict[str, float] = {}

        self._blocked: dict[str, int] = {
            "duplicate": 0,
            "too_frequent": 0,
            "burst_locked": 0,
            "identical_state": 0,
        }

    # ------------------------------------------------------------ 内部
    @staticmethod
    def _signature(params: dict[str, Any]) -> str:
        try:
            return json.dumps(params, sort_keys=True, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return str(sorted(params.items(), key=lambda kv: str(kv[0])))

    def _trim_history(self, device_id: str, now: float) -> list[float]:
        stamps = [t for t in self._history.get(device_id, []) if now - t <= self.burst_window]
        self._history[device_id] = stamps
        return stamps

    # ------------------------------------------------------------ 判定
    def allow(
        self,
        device_id: str,
        params: dict[str, Any],
        now: float | None = None,
    ) -> bool:
        """返回 True 表示可以下发。"""
        if not params:
            self._blocked["identical_state"] += 1
            return False

        now = time.monotonic() if now is None else float(now)

        # 闸 3：熔断锁定中
        locked_until = self._locked_until.get(device_id)
        if locked_until is not None:
            if now < locked_until:
                self._blocked["burst_locked"] += 1
                return False
            self._locked_until.pop(device_id, None)
            # 解锁时清空历史，给设备一次干净的重启机会
            self._history[device_id] = []

        # 闸 2：最小间隔
        last_time = self._last_time.get(device_id)
        if last_time is not None and (now - last_time) < self.min_interval:
            self._blocked["too_frequent"] += 1
            return False

        # 闸 1：同值去重
        signature = self._signature(params)
        if self._last_key.get(device_id) == signature:
            key_time = self._last_key_time.get(device_id, 0.0)
            if (now - key_time) < self.same_window:
                self._blocked["duplicate"] += 1
                return False

        # 熔断计数：超限直接锁死
        stamps = self._trim_history(device_id, now)
        if len(stamps) >= self.burst_limit:
            self._locked_until[device_id] = now + self.burst_cooldown
            self._blocked["burst_locked"] += 1
            return False
        stamps.append(now)

        self._last_time[device_id] = now
        self._last_key[device_id] = signature
        self._last_key_time[device_id] = now
        return True

    def forget(self, device_id: str) -> None:
        """设备重载时清空历史，避免旧签名挡住新会话的第一条指令。"""
        self._last_time.pop(device_id, None)
        self._last_key.pop(device_id, None)
        self._last_key_time.pop(device_id, None)
        self._history.pop(device_id, None)
        self._locked_until.pop(device_id, None)

    def is_locked(self, device_id: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else float(now)
        locked_until = self._locked_until.get(device_id)
        return locked_until is not None and now < locked_until

    # ------------------------------------------------------------ 观测
    @property
    def blocked_counts(self) -> dict[str, int]:
        return dict(self._blocked)

    def stats(self) -> dict[str, Any]:
        return {
            "min_interval": self.min_interval,
            "same_window": self.same_window,
            "burst_limit": self.burst_limit,
            "burst_window": self.burst_window,
            "devices": len(self._last_time),
            "locked": [d for d in self._locked_until],
            "blocked": dict(self._blocked),
        }
