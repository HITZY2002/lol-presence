# astrbot_plugin_lol_presence

英雄联盟国服 QQ 群上线提醒插件（AstrBot）。群友上号 / 开局 / 结算时自动向群内播报，并提供一整套 `/lol` 查询指令。

> **必读**：`docs/research.md` 记录了 2026-08 对国服数据源的完整调研结论。
> 核心结论：**国服不存在"纯云端查他人实时在线状态"的可用接口**。
> 实时 Presence 层需要一台长期登录的 LOL 客户端（观察号）；
> 战绩/段位层走 SGP（token 同样来自客户端）+ LCU 兜底。

## 功能

- 🟢 **上线播报**：群友打开 LOL 客户端时自动播报（`🟢 张三 上号了 / 艾欧尼亚 · 张三#1234`）
- 🎮 **开局播报**：进入游戏时播报英雄与模式（英雄信息缺失时不猜测）
- 🏁 **结算播报**：游戏结束后按 10/20/40/60s 指数退避等待战绩，出数据才显示 KDA
- 📋 **查询指令**：在线列表 / 谁在玩 / 车队（可开黑名单）/ 战绩 / 段位 / 今日战绩
- 🔒 **隐私**：仅监控已绑定的群友；不携带任何个人绑定数据或登录凭据。启用监控前应征得成员同意。
- 📊 **近100场统计**：战绩查询同时显示平均 K/D/A、KDA 与胜率；实际不足100场时按可取得的场次统计。
- 🧩 **Provider 架构**：数据源可替换（wegame / lcu / sgp / mock），业务层零耦合

## 指令

```
/lol                  查询自己的状态
/lol 我的              查询自己的状态
/lol 在线              本群群友在线状态
/lol 谁在玩            正在游戏中的群友
/lol 车队              在线可开黑的群友（排除游戏中/离线）
/lol 状态 @某人        查询某人状态
/lol 战绩 @某人        最近战绩（默认 5 局）及近100场统计
/lol 最近 10           最近 N 局
/lol 段位 @某人        排位段位
/lol 今日              今日战绩汇总
/lol 绑定 游戏名#TAG   绑定 LOL 账号（PUUID 主键，改名不受影响）
/lol 绑定 @群友 游戏名#TAG  管理员为群友代绑定
/lol 自动绑定          QQ 自动解析绑定（需 WeGame Cookie，实验性）
/lol 解绑              解除绑定
/lol 播报 on/off       群播报总开关（管理员/群管）
/lol 上线播报 on/off   （管理员/群管）
/lol 开局播报 on/off   （管理员/群管）
/lol 结算播报 on/off   （管理员/群管）
/lol 帮助
```

## 安装

1. 在 AstrBot 插件管理中通过仓库地址安装：`https://github.com/HITZY2002/astrbot_plugin_lol_presence`，或将本目录放入 `AstrBot/data/plugins/astrbot_plugin_lol_presence/`
2. 重启 AstrBot（或在 WebUI 插件页重载）
3. Python 依赖见 `requirements.txt`；AstrBot 通常已安装这些依赖

## Presence 数据源配置（重要）

WebUI → 插件 → LOL 上线提醒 → 配置：

| 模式 | 说明 | 要求 |
| --- | --- | --- |
| `auto`（默认） | 云端 wegame → 本地 LCU 依次探测 | 见下 |
| `lcu` | 强制 LCU 观察号 | 与客户端同机（Windows）或远程配置端口+密码 |
| `mock` | 测试模拟源（不发真实数据） | 无 |

**观察号部署形态**（当前唯一能实现"上线播报"的方式）：

1. 准备一个小号（观察号）在一台 Windows 机器上登录 LOL 国服客户端
2. 需要实时监控的群友把观察号加为游戏好友。未加好友也可以先绑定，查询会说明未加好友；添加后自动对齐身份。
3. AstrBot 与该客户端同机部署（自动探测 LCU），或在插件配置中手动填
   `host`（如 `192.168.1.10:58528`）与 `password`（`remoting-auth-token`）
4. 客户端保持登录即可，无需进入对局；插件通过好友 Presence 实时感知状态

> **部分国服客户端版本注意**：可能出现 lockfile 为空的情况。
> 插件自动发现链为：进程参数 → 注册表安装目录（`HKCU\SOFTWARE\Tencent\LoL`）
> → Riot Client lockfile（新 5 字段格式）→ **UX 日志解析**（无需提权）。
> 远程部署时 `password` 从观察机的 UX 日志里取
> （`<安装目录>\LeagueClient\*_LeagueClientUx.log` 中 `--remoting-auth-token=`）。

### 观察号后台运行 / 资源压到最低（实测数据）

| 状态 | 内存 | 进程数 | 好友监控 |
| --- | --- | --- | --- |
| 客户端界面正常 | 912 MB | 17 | ✓ |
| 关闭界面仅留后端 | **286 MB** | **2** | ✓ 完全正常 |

- 插件内置 **资源守护**（配置 `resource_saver.enabled=true`）：连接客户端后自动
  调用客户端官方接口 `POST /riotclient/kill-ux` 关闭全部界面进程（无需提权），
  内存直降约 69%，GPU 渲染归零；好友监听（XMPP）与 SGP 战绩 token 全部保留。
- 安全护栏：仅在 gameflow 处于大厅/空闲时执行，绝不在选人/对局中触发；
  界面可随时经 WeGame 重开，重连后插件自动重新发现并再次收起。
- 进程优先级限制（BelowNormal）会被 UAC 拒绝（除非 AstrBot 以管理员运行），
  插件会尝试并静默降级；Windows 没有对单进程硬性内存/GPU 预算的通用机制，
  关闭 UI 是最有效的手段。

战绩/段位（P1 功能）同样需要观察号：SGP 的 Bearer token 取自客户端会话
（`/entitlements/v1/token` + `/lol-league-session/v1/league-session-token`），
客户端关闭时战绩查询自动降级，恢复后自动重连。

纯 Linux 服务器（无任何客户端）可运行本插件，绑定与缓存查询可用，
但**上线播报不可用**——插件会明确提示，不会伪造数据。

## 播报逻辑

- 状态机：`OFFLINE / ONLINE / LOBBY / MATCHMAKING / CHAMP_SELECT / IN_GAME`
- 只有真实 transition 才播报；插件启动与断线重连后的首批全量快照**静默封口**，绝不刷屏
- 同一玩家上线播报默认 30 分钟冷却（群可覆盖，防断线抖动刷屏）
- 同一局 `gameId` 的开局/结算各自只播报一次
- 结算时战绩未出则只播报"打完了"，**不编造 KDA**

## 数据库

SQLite（`data/plugin_data/astrbot_plugin_lol_presence/lol_presence.db`）：

- `bindings` — QQ↔PUUID 绑定（主键 `qq_id+puuid`，改名安全）
- `groups` — 群播报配置（含 `unified_msg_origin`，主动消息用）
- `presence_state` — 最近 Presence 快照（重启恢复 old_state）
- `notification_state` — 播报冷却与开局/结算去重
- `matches` — 战绩缓存

## 开发

```bash
python -m pip install -r requirements.txt pytest pytest-asyncio
python -m pytest tests/ -q
# 集成测试必须另开进程运行，并在真实 AstrBot 环境内执行：
# 如 AstrBot 未安装在当前环境，可设置 ASTRBOT_SOURCE_DIR 为其源码目录。
python -m pytest integration/ -q -o addopts=""
```

- `tests/` — 单元测试（使用 `tests/stubs/` 中的 AstrBot 桩，秒级完成）
- `integration/` — 集成测试（真实 AstrBot 框架全生命周期验证）
- `docs/research.md` — 数据源调研报告（能力矩阵、失效接口清单、架构决策依据）

## 目录结构

```
astrbot_plugin_lol_presence/
├── main.py                  # Star 插件入口 + /lol 指令路由
├── commands/                # 指令实现（bind / status / match）
├── services/
│   ├── presence_service.py  # 状态机（transition 判定、首启封口）
│   ├── player_service.py    # 绑定与玩家解析
│   ├── match_service.py     # 战绩/段位 + 结算退避等待
│   └── notification_service.py  # 事件→群播报（冷却/去重/主动消息）
├── providers/
│   ├── base.py              # PresenceProvider / MatchProvider 抽象 + 能力声明
│   ├── lcu.py               # 本地客户端：好友 Presence (REST+WSS) + 兜底战绩
│   ├── sgp.py               # 腾讯 SGP：战绩/段位/GSM（token 取自 LCU）
│   ├── wegame.py            # WeGame 云端：实验性 QQ→角色解析（无 Presence）
│   └── mock.py              # 测试模拟源
├── storage/database.py      # SQLite + migrations
├── models/                  # Binding / PlayerPresence / MatchSummary
└── utils/                   # 英雄表(腾讯CDN) / 队列映射 / 消息格式化
```

## 已知限制

1. 上线检测依赖"群友添加观察号为好友"；未加好友的玩家可以预绑定，但无法确认其实时在线状态。
2. 国服 Riot ID 体系特殊：绑定主要靠召唤师名/好友列表解析，`#TAG` 兼容但不保证全大区可查
3. SGP 战绩 token 随客户端会话，客户端离线时战绩/段位指令不可用（实时播报不受影响，恢复后自动续期）
4. WeGame 云端（`wegame.py`）当前仅实验性 QQ→角色解析，云端 Presence 经调研确认不存在可用接口

## 隐私与安全

- 不要求 QQ 密码。可选的 WeGame Cookie、LCU 凭据与掌盟 Webhook 密钥在用户自己的配置中管理，禁止提交到公开仓库。
- 日志脱敏：token/skey/ticket 一律不输出，PUUID 截断显示，群号掩码
- 已绑定成员默认参与播报，群播报开关由管理员控制；当前版本不提供 `/lol 我的播报` 命令。
- 掌盟 Webhook 属于实验功能，会在本地记录通知原文；必须配置鉴权密钥，不要直接暴露无鉴权接口。
- 仅供个人学习与合法使用，请遵守平台规则并取得被监控成员的同意。本项目不是 Riot Games、腾讯或 AstrBot 的官方产品。

## 许可证

MIT License，见 [LICENSE](LICENSE)。依赖组件按各自的许可证分发。
