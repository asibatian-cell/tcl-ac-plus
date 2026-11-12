"""常量定义。"""
from __future__ import annotations

try:
    from homeassistant.const import Platform
except ModuleNotFoundError:  # 允许脱离 HA 运行（离线自检、真机探针）
    class Platform:  # type: ignore[no-redef]
        CLIMATE = "climate"
        SWITCH = "switch"
        NUMBER = "number"
        SELECT = "select"
        SENSOR = "sensor"
        BINARY_SENSOR = "binary_sensor"

DOMAIN = "tcl_ac_plus"
NAME = "TCL 空调 Plus"

PLATFORMS: list[Platform] = [
    Platform.CLIMATE,
    Platform.SWITCH,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
]

# ---------------------------------------------------------------- 配置项
CONF_ACCOUNT_ID = "account_id"
CONF_ACCESS_TOKEN = "access_token"
CONF_REFRESH_TOKEN = "refresh_token"
CONF_PHONE = "phone"
CONF_LOGIN_METHOD = "login_method"
CONF_POLL_INTERVAL = "poll_interval"
CONF_POWER_INVERTED = "power_inverted"
CONF_EXPOSE_ALL = "expose_all_entities"
CONF_BEEP_MUTE = "beep_mute"

LOGIN_PASSWORD = "password"
LOGIN_SMS = "sms"
LOGIN_TOKEN = "token"

DEFAULT_POLL_INTERVAL = 60
MIN_POLL_INTERVAL = 30
MAX_POLL_INTERVAL = 600

DEFAULT_POWER_INVERTED = False
DEFAULT_EXPOSE_ALL = False

# 关闭空调提示音：只在设备确实上报了 beep 类属性时才发送
DEFAULT_BEEP_MUTE = True

# 指令合并窗口（秒）。同一设备在这个窗口内的多条指令会被合并成一次请求，
# 避免「模式 + 温度 + 风速」被拆成三次下发、让空调连滴三声。
COMMAND_MERGE_DELAY = 0.35

# ---------------------------------------------------------------- 云端接口
APP_ID = "55141607047147220"
APP_TENANT_ID = "tcl"
APP_SECRET = "48980a392dc2078cbda5b3035a084a7bcee34a69cace18baa715e61d790b01d4"
APP_VERSION = "2.7.33"
APP_UUID = "TCL+"
APP_PLATFORM_TYPE = "MemberMiniProgram"
APP_ENCRYPT_VERSION = "2.0"

IOS_APP_ID = "28411606743791229"
IOS_APP_SECRET = "8f2538acdb620574bf334cd35114f7178c8e790bf216edb7ba749bd6f6d86b44"
IOS_TENANT_ID = "TCLPLUS"
IOS_APP_VERSION = "2.6.1(1344)"
IOS_PLATFORM_TYPE = "iOS"
IOS_STORE_UUID = "TCL+"
IOS_USER_AGENT = "TCLPlus/2.6.1 (iPhone; iOS 15.4.1; Scale/3.00)"
IOS_REPORT_STATE = '{"os":"iOS","osVersion":"15.4.1","appVersion":"2.6.1","deviceModel":"iPhone"}'

MINIPROGRAM_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 "
    "MicroMessenger/7.0.20.1781(0x6700143B) NetType/WIFI "
    "MiniProgramEnv/Windows WindowsWechat/WMPF WindowsWechat(0x63090a13)"
    "XWEB/13639"
)
MINIPROGRAM_REFERER = (
    "https://servicewechat.com/wxed3f11c6ee178737/243/page-frame.html"
)

AUTH_HOST = "https://cn.account.tcl.com"
IO_HOST = "https://io.zx.tcljd.com"

URL_REFRESH_TOKEN = AUTH_HOST + "/auth/auth/refershToken"
URL_LOGIN = AUTH_HOST + "/auth/auth/login"
URL_QUICK_LOGIN = AUTH_HOST + "/auth/auth/quickLogin"
URL_SMS_CAPTCHA = AUTH_HOST + "/captcha/captcha/new/smsCaptcha"
URL_USER_INFO = AUTH_HOST + "/user/user/getUserInfoByToken"
URL_DEVICES = IO_HOST + "/v1/tclplus/weChat/user/user_devices"
URL_MQTT_CONFIG = IO_HOST + "/v1/auth/service/loadBalance"
URL_CONTROL = IO_HOST + "/v1/control/property/{device_id}"
URL_STATUS = IO_HOST + "/v1/thing/status"
URL_PANEL_CONFIG = IO_HOST + "/v1/tclplus/panel/rn-panel-config"

MQTT_HOST = "iotws-prod.tcliot.com"
MQTT_PORT = 443
MQTT_PATH = "/mqtt"

# ---------------------------------------------------------------- 事件
EVENT_CLOUD_STATE = f"{DOMAIN}_cloud_state"

# ---------------------------------------------------------------- 角色识别
# 归一化后的 identifier -> 语义角色
ROLE_PATTERNS: dict[str, tuple[str, ...]] = {
    "power": (
        "powerswitch",
        "power",
        "onoff",
        "mainswitch",
        "startstop",
        "switchstatus",
    ),
    "mode": ("workmode", "runmode", "acmode", "operationmode", "mode"),
    "target_temp": (
        "targettemperature",
        "settemperature",
        "targettemp",
        "settemp",
        "temperature",
        "temp",
    ),
    "current_temp": (
        "currenttemperature",
        "indoortemperature",
        "roomtemperature",
        "ambienttemperature",
        "indoorairtemperature",
        "currenttemp",
    ),
    "fan": (
        "windspeedpercentage",
        "fanspeedpercentage",
        "windspeed",
        "fanspeed",
        "windlevel",
    ),
    "fan_auto": ("windspeedautoswitch", "fanspeedautoswitch", "windauto"),
    # 注意：不收录 verticalDirection / horizontalDirection。
    # 这两个是「定格角度」枚举（左定格/上定格/环抱风…），不是扫风开关；
    # 若混进 swing 角色，HA 的 off/vertical/horizontal 三段映射会发错值。
    "vswing": (
        "verticalwind",
        "verticalswing",
        "updownwind",
        "updownswing",
    ),
    "hswing": (
        "horizontalwind",
        "horizontalswing",
        "leftrightwind",
        "leftrightswing",
    ),
    "beep": (
        "beepswitch",
        "beep",
        "buzzer",
        "prompttone",
        "voiceswitch",
        "soundswitch",
    ),
}

# 模式值 -> HA HVACMode（按中文/英文关键词，避免依赖具体机型编码）
MODE_WORDS: dict[str, str] = {
    "制冷": "cool",
    "cool": "cool",
    "制热": "heat",
    "heating": "heat",
    "heat": "heat",
    "除湿": "dry",
    "dry": "dry",
    "送风": "fan_only",
    "fan_only": "fan_only",
    "风": "fan_only",
    "自动": "auto",
    "auto": "auto",
}

# 风速档位 -> 百分比
FAN_PRESET_PERCENT: dict[str, int] = {
    "auto": 0,
    "low": 25,
    "medium": 50,
    "high": 75,
}

TEMP_MIN = 16.0
TEMP_MAX = 31.0
TEMP_STEP = 0.5

# TCL 设备大类。只有这些才允许用「通用空调模板」兜底。
AC_CATEGORIES = {"AC", "AIRCONDITIONER", "AIRCON", "KT"}

# 明确不是空调的大类：遇到它们直接拒绝生成 climate，避免电视/冰箱
# 因为缺物模型而套上空调模板、被误识别成空调。
NON_AC_CATEGORIES = {
    "TV",
    "RF",   # 冰箱
    "WH",   # 洗衣机
    "WD",
    "DW",   # 洗碗机
    "OV",   # 烤箱
    "OTR",
    "RAC",
    "FAN",
    "LIGHT",
    "PLUG",
    "SOCKET",
    "CAMERA",
    "LOCK",
    "ROBOT",
    "VACUUM",
    "WATERHEATER",
    "HEATER",
    "PURIFIER",
    "DEHUMIDIFIER",
    "HUMIDIFIER",
}


def normalize_key(text: str) -> str:
    """identifier 归一化：小写 + 只保留字母数字。"""
    return "".join(ch for ch in str(text).lower() if ch.isalnum())
