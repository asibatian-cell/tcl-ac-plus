# TCL 空调 Plus（Home Assistant 自定义集成）

用 TCL 账号登录 Home Assistant，直接生成**标准 climate 实体**，让巴法云 / HassLife / 米家能把它当空调识别，而不是一个开关。

## v1.1 重点：不再滴滴响、不再刷报错

空调每收一条控制指令就「滴」一声。之前巴法云会因为状态回读不一致反复下发同一条指令，于是变成连续滴滴响。v1.1 加了四道闸：

| 机制 | 作用 |
|---|---|
| 值比较 | 目标值与当前值相同 → 直接丢弃 |
| 同值去重 | 同一条指令 10 秒内只放行一次 |
| 最小间隔 | 两次下发至少间隔 2 秒 |
| 熔断 | 60 秒内超 6 条 → 锁死该设备 60 秒 |
| 乐观值 | 刚写入的值在云端确认前不被旧快照覆盖，阻断「回跳 → 重发」环 |
| 主动静音 | 指令附带关闭 `beepSwitch`，机型不支持时自动退回原始指令 |

日志侧：指令失败、MQTT 鉴权失败、轮询失败全部降级为 debug，不再刷 warning。只有 refreshToken 彻底失效才提示重新登录。

## 为什么需要它

巴法云的 `bemfa_cloud` 集成有一个硬性判定：

```python
# sync_climate.py
hvac_modes = state.attributes.get(ATTR_HVAC_MODES, [])
if HVACMode.COOL not in hvac_modes:
    continue          # 不收录为空调(005)
```

只要 climate 实体缺 `cool`，或者设备根本没有 climate 实体，巴法云就只会把它归到温控器(010)或退化成开关(006)。原版 `ndwzy/tcl-ha` 用四个硬编码驼峰 key 判断「是不是空调」，机型命名一变就整个 climate 实体消失，于是只剩零散 switch/number —— 这就是「只能用开关」的根因。

本集成做了三件事：

1. **角色推断而非硬编码**：用归一化关键词（power / mode / target_temp / current_temp / fan / vswing / hswing）匹配物模型，云端拿不到配置时退回内置通用空调模板，再拿设备快照补 key。三层兜底，保证 climate 一定生成。
2. **保证 hvac_modes 含 cool**：模式映射按中英文文案识别，即使机型枚举文案异常，也会兜底注入 `cool`。
3. **修掉原版的断联缺陷**：token 原地刷新、不重载集成；只有明确的 401/403 才判定凭证失效，网络错误与 5xx 不因此清掉会话；MQTT 重连时取最新 token，不会拿过期 token 进死循环。

## 安装

### 方式一：手动复制（推荐，最快）

解压后把 `custom_components/tcl_ac_plus` 整个目录丢进 HA 配置目录的 `custom_components/`：

```bash
# 原生 OS / Supervised
cp -r custom_components/tcl_ac_plus <HA配置目录>/custom_components/
```

Docker 部署：

```bash
docker cp custom_components/tcl_ac_plus homeassistant:/config/custom_components/
docker restart homeassistant
```

**必须重启 HA**，新增集成不会热加载。

### 方式二：HACS 自定义存储库

把本项目推到自己的 GitHub 仓库后：HACS → 右上角菜单 → 自定义存储库 → 填仓库地址 → 类型选「集成」→ 添加 → 搜索 `TCL 空调 Plus` → 下载 → 重启。

> HACS 要求仓库根目录就有 `custom_components/<domain>/`，本项目结构已符合。

## 配置

1. 设置 → 设备与服务 → 添加集成 → 搜索 `TCL`
2. 选择登录方式：
   - **短信验证码登录**（推荐）：填手机号 → 收验证码 → 提交
   - **密码登录**：TCL 智家 / TCL+ 的账号密码
   - **Token 登录**：已有 `accountId` + `refreshToken`（抓包拿到）
3. 保存后自动拉设备列表与物模型，生成实体

> 账号只用于首次换取 token，密码不会落盘。落盘的是 accessToken / refreshToken / accountId。

## 生成的实体

| 平台 | 内容 |
|---|---|
| `climate` | 电源、模式、目标温度、风速、摆风 |
| `switch` | 节能、睡眠、灯光、提示音、电辅热、自清洁、柔风等 |
| `number` | 可写数值（新风风速、房间面积等） |
| `select` | 睡眠模式、上下/左右送风方向等 |
| `sensor` | 故障码、滤芯寿命、运行时长等只读量 |

climate 实体的 `hvac_modes` 一定包含 `cool`，`supported_features` 只声明设备真实支持的项。

## 接到巴法云 / 米家

1. HA 里安装 `bemfa/bemfa_cloud_ha`（HACS 搜 Bemfa Cloud）
2. 设置 → 设备与服务 → 添加集成 → 搜索 `Bemfa Cloud` → 选「私钥」→ 填巴法云 uid
3. 设备列表里现在能看到 climate 实体，勾选保存
4. 米家 → 我的 → 连接三方平台 → 添加 → 巴法 → 登录授权 → 同步设备

同步后米家会出现完整空调卡片（模式、温度、风速），不是开关。

> HassLife 同理：HA 装 HassLife 集成 → 网站设备管理里添加 → 米家绑 HassLife。HassLife 的 climate 支持模式/温度/风速。

## 选项

设置 → 设备与服务 → TCL 空调 Plus → 配置：

| 选项 | 说明 |
|---|---|
| 轮询间隔（秒） | MQTT 推送为主，轮询仅兜底，默认 60 |
| 电源状态取反 | 开关状态与实机相反时打开（部分机型 0=开机） |
| 把只读数值也暴露为传感器 | 默认只暴露可写数值 |

## 排查

**设备没识别成空调**

看日志关键字：

```
grep -i "tcl_ac_plus" home-assistant.log | grep -E "未识别为空调|角色匹配"
```

日志会打印实际识别到的属性 identifier 列表。把这段发出来就能定位是哪个 key 命名不同。

**实体里 climate 有但巴法云看不到**

确认 `hvac_modes` 含 cool。开发者工具 → 模板：

```jinja
{{ state_attr('climate.你的空调', 'hvac_modes') }}
```

若输出里没有 `cool`，把结果发出来。

**token 反复失效**

先在 HA 日志里确认错误类型，再对症处理：

- `refreshToken 已失效，需要重新登录` → 真失效，重新登录一次即可
- `暂时连不上 TCL 云端` → 网络问题，会自动重试，不用干预
- 频繁出现第一条 → 检查是不是同一账号被多个程序同时用（例如同时开着旧版 tcl-ha 和本集成）

> 关于「HA 登录会不会踢掉手机 App」：实测**不会**。见下方「多设备同时登录」。

## 多设备同时登录

实测结论：**同一账号可以在多个设备/多个程序上同时登录，互不影响。**

验证方式（2026-11-12 真机测试）：

| 场景 | 结果 |
|---|---|
| 手机登录后 HA 再登录 | 两者 token 都有效 |
| HA 登录后手机再登录 | 两者 token 都有效 |
| HA 连续登录两次（重装集成） | 新旧 token 都有效 |
| 同一账号用不同 appId（官方 App 凭据 vs 小程序凭据） | 互不影响 |
| HA 执行 token 刷新后，手机执行自己的刷新 | 互不影响 |

技术原因：TCL 的登录是**按 deviceId 独立签发**的。每次登录生成一对独立的 accessToken / refreshToken，刷新时只轮换自己那一对，不会作废其他设备的凭证。

所以：**HA 里登录不会让你手机 App 掉线，手机重新登录也不会让 HA 掉线。**

## 什么时候真的会让 HA 掉线

- 你在 TCL 智家 App 里**修改了密码** → 旧 token 全部失效，需要在 HA 里重新登录
- 你在 TCL 账号安全设置里**主动注销所有设备** → 同上
- TCL 主动风控（异常登录、更换 IP、频繁请求）→ 概率低，遇到等一会再试

## 已知限制

- 依赖 TCL 云端（`io.zx.tcljd.com`），TCL 改接口会失效，届时需要更新。
- 物模型接口对部分 productKey 返回空，此时走内置通用模板，实体可用但可能缺机型特有属性。
- 用的是 TCL 小程序公开凭据，与官方 App 无关联，TCL 单方面调整风控即会受影响。

## 免责

非官方集成。TCL、TCL+ 商标归其权利人所有，本项目仅用于识别兼容设备。
