"""物模型解析与角色推断。

这个模块解决原版 tcl-ha 最致命的问题：它靠硬编码的四个驼峰 key
（powerSwitch / workMode / targetTemperature / windSpeedPercentage）判断
「这台设备是不是空调」，机型命名一变就整个 climate 实体消失，只剩零散
switch/number，于是巴法云只能把它当开关。

这里改成三层兜底：
1. 云端 rn-panel-config 返回的物模型；
2. 本地缓存的物模型；
3. 内置通用空调模板 + 设备快照里实际出现的 key，按关键词归一化后匹配角色。
任何一层命中，climate 实体都会生成。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from .const import MODE_WORDS, NON_AC_CATEGORIES, ROLE_PATTERNS, normalize_key

_LOGGER = logging.getLogger(__name__)

# 这些 function 分组的字段全部是设备上报的遥测，不可写。
# 真机验证（1112044844N / 1112014508N）确认：
#   generalDeviceStatus = 四通阀、压缩机频率、盘管温度、故障码、膨胀阀…
#   AIEnergyConservation 里除 AIECOSwitch 外全是室外机运行参数
READONLY_FUNCTIONS = {
    "generalDeviceStatus",
    "AIEnergyConservation",
    "timer",
}

# 整组只读，但组内个别字段其实是可写的开关/数值，需要放行
READONLY_EXCEPTIONS = {
    "AIECOSwitch",
    "localTimer",
}

# 纯诊断性质、暴露出来只会干扰界面的字段（Wi-Fi 信号、版本号、协议元数据）
TELEMETRY_HINTS = (
    "wifi",
    "rssi",
    "macaddr",
    "ip",
    "ssid",
    "packet",
    "tsl",
    "version",
    "volume",
    "querytime",
    "aisctrl",
    "local timer",
)

# 通用空调物模型：云端拿不到配置时的最后兜底。
# identifier 命名按 TCL Plus 小程序的主流写法，matching 时会被归一化。
GENERIC_AC_TEMPLATE: list[dict[str, Any]] = [
    {"identifier": "powerSwitch", "title": "电源开关", "type": "bool"},
    {"identifier": "targetTemperature", "title": "温度", "type": "int",
     "specs": {"min": 16, "max": 31, "step": 0.5, "unit": "℃"}},
    {"identifier": "workMode", "title": "模式", "type": "enum",
     "specs": {"0": "自动", "1": "制冷", "2": "除湿", "3": "送风", "4": "制热"}},
    {"identifier": "windSpeedPercentage", "title": "风速", "type": "int",
     "specs": {"min": 1, "max": 100, "step": 1, "unit": "%"}},
    {"identifier": "windSpeedAutoSwitch", "title": "风速自动", "type": "bool"},
    {"identifier": "verticalWind", "title": "上下扫风", "type": "bool"},
    {"identifier": "horizontalWind", "title": "左右扫风", "type": "bool"},
    {"identifier": "currentTemperature", "title": "室内温度", "type": "int",
     "specs": {"min": -30, "max": 60, "step": 1, "unit": "℃"}},
    {"identifier": "ECO", "title": "节能", "type": "bool"},
    {"identifier": "sleep", "title": "睡眠模式", "type": "enum",
     "specs": {"0": "关", "1": "标准", "2": "老人", "3": "儿童"}},
    {"identifier": "PTC", "title": "电辅热", "type": "bool"},
    {"identifier": "screen", "title": "灯光", "type": "bool"},
    {"identifier": "beepSwitch", "title": "提示音", "type": "bool"},
    {"identifier": "selfClean", "title": "蒸发器清洁", "type": "bool"},
    {"identifier": "antiMoldew", "title": "干燥", "type": "bool"},
    {"identifier": "softWind", "title": "柔风", "type": "bool"},
]


@dataclass
class AttrSpec:
    """单个属性的规范化描述。"""

    identifier: str
    title: str
    raw_type: str
    specs: dict[str, Any] = field(default_factory=dict)
    norm: str = ""
    function: str = ""
    # True 表示该字段是从设备快照补出来的，物模型里没有声明，可写性未知。
    # 这种情况默认只当只读传感器，避免误发非法指令。
    from_snapshot: bool = False

    def __post_init__(self) -> None:
        if not self.norm:
            self.norm = normalize_key(self.identifier)

    @property
    def is_bool(self) -> bool:
        return "bool" in self.raw_type

    @property
    def is_enum(self) -> bool:
        return "enum" in self.raw_type

    @property
    def is_number(self) -> bool:
        return any(t in self.raw_type for t in ("int", "double", "float"))

    @property
    def is_readonly_telemetry(self) -> bool:
        """是否是纯遥测字段（压缩机频率、盘管温度、故障码…）。

        TCL 物模型用 `function` 给字段分组。generalDeviceStatus 整组都是设备
        运行状态上报，不能写；AI节能 组除少数开关外也是遥测。把这些暴露成
        number 实体，用户拖动滑块会下发非法指令，轻则报错重则让空调乱响。
        """
        if self.identifier in READONLY_EXCEPTIONS:
            return False
        if self.from_snapshot:
            # 快照补出来的字段无法判断可写性，一律当只读
            return True
        if self.function in READONLY_FUNCTIONS:
            return True
        return any(hint in self.norm for hint in TELEMETRY_HINTS)

    @property
    def numeric_range(self) -> tuple[float, float, float]:
        try:
            low = float(self.specs.get("min", 0))
        except (TypeError, ValueError):
            low = 0.0
        try:
            high = float(self.specs.get("max", 100))
        except (TypeError, ValueError):
            high = 100.0
        try:
            step = float(self.specs.get("step", 1))
        except (TypeError, ValueError):
            step = 1.0
        return low, high, step or 1.0

    def enum_map(self) -> dict[str, str]:
        """返回 {原始值字符串: 文案}。"""
        out: dict[str, str] = {}
        for key, value in (self.specs or {}).items():
            if isinstance(value, (str, int, float)):
                out[str(key)] = str(value)
        return out

    def bool_on_value(self) -> int:
        """推断 bool 属性的「开」值。

        多数 TCL 机型 1=开，但个别机型 specs 写成 {"0": "开机", "1": "关机"}，
        这种要反过来发 0 才是开机。
        """
        specs = self.specs or {}
        zero_text = str(specs.get("0", ""))
        one_text = str(specs.get("1", ""))
        zero_is_on = any(word in zero_text for word in ("开机", "打开", "开启", "开"))
        one_is_off = any(word in one_text for word in ("关机", "关闭", "关"))
        if zero_is_on and one_is_off:
            return 0
        return 1

    def bool_off_value(self) -> int:
        return 0 if self.bool_on_value() == 1 else 1

    def bool_is_on(self, value: Any) -> bool | None:
        """把上报的原始值翻译成「是否开启」。"""
        if value is None:
            return None
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in ("on", "open", "true", "开机", "打开", "开启"):
            return True
        if text in ("off", "close", "closed", "false", "关机", "关闭"):
            return False
        if text in ("0", "1"):
            return int(text) == self.bool_on_value()
        return None


@dataclass
class DeviceModel:
    """一台设备的物模型 + 角色索引。"""

    product_key: str
    attributes: list[AttrSpec]
    roles: dict[str, AttrSpec]
    source: str  # cloud / cache / generic
    category: str = ""
    blocked_reason: str = ""

    @property
    def is_air_conditioner(self) -> bool:
        """是否当作空调处理。

        三条约束，任一条不满足都不生成 climate：
        1. 大类不是明确的家电/影音设备（电视、冰箱、洗衣机…）
        2. 认得电源
        3. 认得温度或模式

        第 1 条是必须的：电视缺物模型时会退回通用空调模板，于是
        powerSwitch 就位、骨架齐全，被误判成空调。必须靠 category 拦住。
        """
        if self.category and self.category.upper() in NON_AC_CATEGORIES:
            return False
        if self.blocked_reason:
            return False

        has_power = "power" in self.roles
        has_core = "target_temp" in self.roles or "mode" in self.roles
        return has_power and has_core

    def attr(self, role: str) -> AttrSpec | None:
        return self.roles.get(role)


def _coerce_type(raw: dict[str, Any]) -> str:
    value = raw.get("type")
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return ".".join(str(k) for k in value.keys())
    return "string"


def parse_attributes(raw_attributes: list[dict[str, Any]] | None) -> list[AttrSpec]:
    """解析物模型，按 identifier 去重。

    TCL 的物模型会按功能分组重复下发同一 identifier（例如 AI节能 分组里又出现
    一次 targetTemperature / windSpeedPercentage），不去重会导致同一字段生成
    多个实体，两个入口控同一个 DP —— 直接后果就是重复下发、空调连续滴滴响。
    保留首次出现的定义（首个分组通常是主功能面板，语义最准）。
    """
    result: list[AttrSpec] = []
    seen: set[str] = set()

    for item in raw_attributes or []:
        if not isinstance(item, dict):
            continue
        identifier = item.get("identifier")
        if not identifier:
            continue
        key = str(identifier)
        if key in seen:
            continue
        seen.add(key)

        specs = item.get("specs")
        result.append(
            AttrSpec(
                identifier=key,
                title=str(item.get("title") or item.get("name") or key),
                raw_type=_coerce_type(item),
                specs=specs if isinstance(specs, dict) else {},
                function=str(item.get("function") or ""),
            )
        )
    return result


# 目标温度不可能是"当前/室内/环境"这类只读温度。加负向词，避免把
# currentTemperature 误判成可设定温度。
TARGET_TEMP_NEGATIVE = ("current", "indoor", "room", "ambient", "outdoor", "环境", "室内", "当前")


def resolve_roles(attributes: list[AttrSpec]) -> dict[str, AttrSpec]:
    """按归一化关键词匹配语义角色。

    优先级：完全相等 > 包含匹配。每个角色只取一个属性，已被占用的属性不再
    分配给其他角色，避免 windSpeedPercentage 与 windSpeedAutoSwitch 互相覆盖。
    """
    roles: dict[str, AttrSpec] = {}
    used: set[str] = set()

    for role, patterns in ROLE_PATTERNS.items():
        candidates = [a for a in attributes if a.identifier not in used]

        if role == "target_temp":
            candidates = [
                a for a in candidates
                if not any(word in a.norm for word in TARGET_TEMP_NEGATIVE)
            ]

        exact: AttrSpec | None = None
        partial: AttrSpec | None = None
        for attr in candidates:
            if attr.norm in patterns:
                exact = attr
                break
            if partial is None and any(
                attr.norm.endswith(p) or p in attr.norm for p in patterns
            ):
                partial = attr

        chosen = exact or partial
        if chosen is not None:
            roles[role] = chosen
            used.add(chosen.identifier)

    # fan_auto 是 bool，若匹配到了百分比属性就丢弃
    fan_auto = roles.get("fan_auto")
    if fan_auto is not None and not fan_auto.is_bool:
        roles.pop("fan_auto", None)

    # 风速属性必须是数值，否则交给 fan_auto
    fan = roles.get("fan")
    if fan is not None and not fan.is_number:
        roles.pop("fan", None)

    # 提示音属性必须是 bool
    beep = roles.get("beep")
    if beep is not None and not beep.is_bool:
        roles.pop("beep", None)

    return roles


def merge_snapshot_keys(attributes: list[AttrSpec], snapshot: dict[str, Any]) -> list[AttrSpec]:
    """用设备快照里实际出现的 key 补全物模型。

    有些机型的物模型接口只返回面板组件、不含全部属性；快照里能拿到真实
    上报的 key，用它补齐能让角色匹配更可靠。
    """
    known = {attr.norm for attr in attributes}
    extra: list[AttrSpec] = []
    for key in snapshot or {}:
        norm = normalize_key(key)
        if norm in known:
            continue
        value = snapshot[key]
        if isinstance(value, bool):
            raw_type = "bool"
        elif isinstance(value, int):
            raw_type = "int"
        elif isinstance(value, float):
            raw_type = "double"
        elif isinstance(value, str) and value.isdigit():
            raw_type = "int"
        else:
            raw_type = "string"
        extra.append(
            AttrSpec(
                identifier=str(key),
                title=str(key),
                raw_type=raw_type,
                from_snapshot=True,
            )
        )
        known.add(norm)
    return attributes + extra


def matched_identifiers(
    attributes: list[AttrSpec], roles: dict[str, AttrSpec] | None = None
) -> set[str]:
    """返回「已被 climate 主实体接管」的属性 identifier。

    判定 = 精确命中角色关键词 ∪ 实际被选中的角色属性。

    只用精确匹配，不做包含匹配：`wifiMode` / `InDoorRunMode` 都含 "mode"，
    `windSpeed7Gear` 含 "windspeed"，若算作已接管，这些有用的辅助实体
    就全被吞掉，界面上反而看不到。

    同时覆盖同义字段：若一台设备同时上报 `powerSwitch` 和 `Power`，
    两个都精确命中 power 关键词，两个都会被排除，避免出现两个开关入口
    控同一个 DP（那正是重复下发、滴滴响的来源）。
    """
    exact_patterns = {p for patterns in ROLE_PATTERNS.values() for p in patterns}
    claimed = {attr.identifier for attr in attributes if attr.norm in exact_patterns}
    if roles:
        claimed |= {attr.identifier for attr in roles.values()}
    return claimed


def build_model(
    product_key: str,
    raw_attributes: list[dict[str, Any]] | None,
    snapshot: dict[str, Any] | None,
    source: str,
    category: str = "",
) -> DeviceModel:
    """构建物模型。

    重要：只有确认是空调大类时才允许用通用模板兜底。否则电视/冰箱
    缺物模型时也会套上空调骨架，被误识别成空调。
    """
    category_norm = (category or "").upper()
    blocked = ""
    attributes = parse_attributes(raw_attributes)

    if not attributes:
        if category_norm and category_norm in NON_AC_CATEGORIES:
            blocked = f"大类 {category_norm} 明确不是空调，不使用通用模板"
            _LOGGER.info("设备 productKey=%s %s", product_key, blocked)
        else:
            _LOGGER.warning(
                "设备 productKey=%s 未取到物模型（来源 %s，category=%s），改用内置通用空调模板",
                product_key, source, category or "未知",
            )
            attributes = parse_attributes(GENERIC_AC_TEMPLATE)
            source = "generic"

    if snapshot:
        attributes = merge_snapshot_keys(attributes, snapshot)

    roles = resolve_roles(attributes)

    model = DeviceModel(
        product_key=product_key,
        attributes=attributes,
        roles=roles,
        source=source,
        category=category_norm,
        blocked_reason=blocked,
    )
    if not model.is_air_conditioner:
        _LOGGER.warning(
            "设备 productKey=%s（category=%s）未识别为空调，已识别角色=%s；"
            "不会生成 climate 实体",
            product_key, category or "未知", sorted(roles.keys()),
        )
    else:
        _LOGGER.info(
            "设备 productKey=%s 角色匹配完成（来源 %s，category=%s）：%s",
            product_key, source, category or "未知",
            {k: v.identifier for k, v in roles.items()},
        )
    return model


def mode_options(mode_attr: AttrSpec | None) -> dict[str, str]:
    """返回 {原始模式值: HVACMode}。

    优先按中文/英文文案识别，认不出的文案按位置兜底，
    保证至少能给出 cool/dry/fan_only/heat/auto 里的一部分。
    """
    if mode_attr is None:
        return {}
    mapping: dict[str, str] = {}
    texts = mode_attr.enum_map()
    if not texts:
        # 纯数值枚举没给文案时，按 TCL 常见编码兜底
        mapping.update({"0": "auto", "1": "cool", "2": "dry", "3": "fan_only", "4": "heat"})
        return mapping

    for raw_value, text in texts.items():
        lowered = str(text).lower()
        hvac = None
        for word, mode in MODE_WORDS.items():
            if word in lowered:
                hvac = mode
                break
        if hvac:
            mapping[raw_value] = hvac
    return mapping


def fan_percent_to_preset(percent: float | None, has_auto: bool) -> str | None:
    if percent is None:
        return None
    if has_auto and percent <= 0:
        return "auto"
    if percent <= 30:
        return "low"
    if percent <= 55:
        return "medium"
    if percent <= 80:
        return "high"
    return "max"


def preset_to_fan_percent(preset: str) -> int:
    return {"auto": 0, "low": 25, "medium": 50, "high": 75, "max": 100}.get(preset, 50)
