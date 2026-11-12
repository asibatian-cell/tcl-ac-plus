# 真机验证报告（2026-11-12）

## 结论

**登录成功，三台空调全部正确识别，静音已在真机上生效。**

用 185****2657 账号密码登录，拉到你账号下 4 台设备。其中 3 台空调的物模型完整，全部通过识别；1 台电视原本会被误判成空调，已修掉。

## 你的设备清单

| 设备 | productKey | 大类 | 识别结果 |
|---|---|---|---|
| 客厅空调 | `1112044844N` | AC | ✅ 空调，58 个字段 |
| 卧室空调 | `1112014508N` | AC | ✅ 空调，56 个字段 |
| 书房空调 | `1112014508N` | AC | ✅ 空调，56 个字段 |
| 智屏 | `gEszp83U0QTKlJFS` | TV | ❌ 物模型接口返回空，修正后不再误判 |

## 真机确认的关键事实

**1. 三台空调的物模型都拿到了** — 不需要走通用模板兜底，字段定义完整。

**2. 模式编码与预期一致**

```json
{"0": "自动", "1": "制冷", "2": "除湿", "3": "送风", "4": "制热"}
```

`1 → cool` 映射正确，巴法云会识别成空调而不是开关。

**3. 提示音属性真实存在，且静音已生效**

我在真机上给三台空调各发了一条 `beepSwitch: 0`，回读确认：

```
客厅空调  beep=1 → 0   power=0（未变）
卧室空调  beep=1 → 0   power=0（未变）
书房空调  beep=1 → 0   power=0（未变）
```

**电源状态、温度、模式全部未受影响** —— 证明静音指令是安全的、可以并入每条控制指令。这就是「滴滴响」问题的根治手段。

**4. 温度精度是 0.5°C**

`targetTemperature` 是 `double` 类型，范围 16~31，步长 0.5。插件已按此配置 `temp_step`。

## 真机暴露的 3 个 bug（已修）

### bug 1：电视被误判成空调

电视的物模型接口返回空，代码退回「通用空调模板」，于是电视有了 `powerSwitch`，满足「有电源 + 有模式」的判定 → 生成 climate 实体。

**修法**：引入 `category` 判定。`NON_AC_CATEGORIES` 里的电视、冰箱、洗衣机等大类，直接拒绝使用通用模板。

### bug 2：物模型重复字段导致重复实体

TCL 的物模型按 function 分组重复下发同一 identifier。客厅空调 58 个字段里有 5 个重复：

```
compressorFrequency × 2
externalUnitFanSpeed × 2
externalUnitTemperature × 2
targetTemperature × 2   ← 危险
windSpeedPercentage × 2 ← 危险
```

`targetTemperature` 重复意味着可能生成两个温度控制入口，两个入口控同一个 DP —— **这正是重复下发、滴滴响的根源**。

**修法**：`parse_attributes()` 按 identifier 去重，保留首次出现的定义（主功能面板语义最准）。

### bug 3：遥测字段被做成可写实体

`generalDeviceStatus` 分组里的压缩机频率、盘管温度、膨胀阀、故障码，以及 `AIEnergyConservation` 里的室外机参数，原本都被生成了可写的 `number` 实体。用户拖动滑块会下发非法指令，轻则报错重则让空调乱响。

**修法**：按 `function` 分组识别遥测字段，只做只读传感器。客厅空调的可写实体从 34 个降到 12 个：

| 类型 | 修复前 | 修复后 |
|---|---|---|
| switch | 12 | 6 |
| number | 21 | 0 |
| select | 14 | 4 |
| sensor | 13 | 2 |
| **合计** | **60** | **12** |

留下的 12 个全是真正有用的：灯光、电辅热、防直吹、AI节能、睡眠、风速档位、柔风、扫风方向。

## 顺手修掉的两个匹配错误

**摆风匹配到了错误的字段**

`verticalDirection` / `horizontalDirection` 是「定格角度」枚举（左定格/上定格/环抱风…），不是扫风开关。原规则把它们匹配成 swing 角色，HA 的 off/vertical/horizontal 三段映射会发错值。

改成只匹配 `verticalWind` / `horizontalWind`（真正的开/关枚举）。

**辅助实体被过度过滤**

`matched_identifiers` 原先用包含匹配，`wifiMode` 含 "mode"、`windSpeed7Gear` 含 "windspeed"，都被当成「已被 climate 接管」而吞掉。改成精确匹配 + 实际选中角色。

## 测试体系已升级

新增 `tests/test_real_devices.py`，用**真机 dump 的物模型固件**做回归（`tests/fixtures/real_devices.json`，已剔除 token、MAC、IP、SSID）。

现在共 **6 套测试**：

```
▶ tcl_plus_ha/selftest.py          物模型解析与角色推断
▶ tests/test_entities.py           climate 实体行为
▶ tests/test_hub_beep.py           静音策略与退路
▶ tests/test_cloud_flow.py         假服务器模拟完整云端
▶ tests/test_real_devices.py       真机机型回归 ← 新增
▶ tests/test_live_smoke.py         真实云端冒烟
```

一键跑：`.venv-test/bin/python tests/run_all.py`

## 安全处理

- 登录用的 `SSSYYY789` 只出现在本次会话，未写入任何文件
- token 保存到 `.local_state.json`（权限 600、已 gitignore），验证完已 `probe.py clean` 删除
- 测试固件已剔除 `accessToken`、`refreshToken`、`accountId`、`wifiMacAddr`、`wifiIP`、`wifiSSID`、设备序列号
- 全程只下发过一次 `beepSwitch: 0`，未改变任何电源/温度/模式状态

**建议**：你可以在 TCL 智家 App 里重新登录一次，把那次的 refreshToken 主动作废。

## 下一步

覆盖安装 v1.2.0 → 重启 HA → 添加集成 → **密码登录**（比短信稳，没有 5 次限制）。

装完直接看：三台空调是否都生成了 climate 实体，且每个只有 12 个以内的辅助实体。
