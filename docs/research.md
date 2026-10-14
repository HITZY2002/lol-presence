# LOL 国服数据源调研报告（2026-08-29）

> 调研方法：GitHub 项目源码审读（含直接抓取 raw 源码与生产环境 JS bundle）+
> 域名/接口实测（本机直连）。凡未经有效凭证实测的接口均标注"无法完全验证"。
> 主要信息源：LeagueAkari（4068★，2026-08-29 当天有 push）、Kroomax/LOLHistory
> （2026-06 实测文档）、rainzee/WegameApiReversing、rainzee/TencentRiotApiReversing、
> WJZ-P/sona、hh-lol-prophet、wnzzer/rank-analysis、LoL-DIY-Programs、
> WeGame 助手页生产 JS（wegame.gtimg.com frame.*.js）。
>
> **2026-08-30 补充：在真机（国服 16.17.812.0490 PublicTencent 客户端）上完成
> 全链路实测，见文末"真机实测补充"。**

---

## 真机实测补充（2026-08-30，重要）

### 2026 国服新客户端架构

- 启动链：WeGame → TCLS → **Riot Client**（RiotClientServices.exe）→ LeagueClient + LeagueClientUx
- **LCU lockfile 为空文件（0 字节）**，客户端日志明确报
  `ERROR| Lockfile contents invalid, length: 0` —— 经典 lockfile 认证已废弃！
- Riot Client lockfile 改用 **5 字段新格式**：`Riot Client:<pid>:<port>:<password>:<protocol>`
  （pid 与 RiotClientServices 进程号一致，port 为其 API 端口，Basic auth `riot:<password>`）
- LeagueClient 自身 cmdline 仍带 `--riotclient-auth-token/--riotclient-app-port`，
  但进程是提权启动的，普通权限读不到 cmdline —— **可用通道是 UX 日志**：
  `<安装目录>/LeagueClient/*_LeagueClientUx.log` 的
  `Command line arguments:` 行含完整 `--app-port` 与 `--remoting-auth-token`，普通权限可读
- 安装目录可从注册表取：`HKCU\SOFTWARE\Tencent\LoL\InstallPath`
  （例：`D:\Program Files (x86)\英雄联盟(26)`）

→ 插件已实现发现链：**进程参数(需权限) → 注册表安装目录 → Riot Client lockfile(新格式) → UX 日志解析**，
真机验证自动发现成功并连上 LCU。

### SGP 真机验证

- 客户端登录后 LCU 的两个 token 均可取，`/leagues-ledge/v2/rankedStats/puuid/{puuid}`
  与 `/match-history-query/v1/products/lol/player/{puuid}/SUMMARY` 实测返回真实数据
  （用观察号 puuid 拉到 1 条真实对局）。账号实际大区由登录参数决定
  （本机为 NJ100，`--rso_platform_id=NJ100`），SGP server_id 需与之匹配。
- 国服新客户端 puuid 为 Riot 风格 UUID（如 `8429f5bc-...`）。

### 观察号资源占用优化（实测）

| 状态 | 内存 | 进程数 | 监控链路 |
| --- | --- | --- | --- |
| 客户端正常（UI 开） | 912 MB | 17 | ✓ |
| `POST /riotclient/kill-ux` 后（仅后端） | **286 MB** | **2** | ✓ REST/WSS/SGP 全部正常 |

- `POST /riotclient/kill-ux` 是客户端**官方端点**（"Kills the ux process"），
  关闭全部 CEF 界面进程，后端继续持有 XMPP 好友 presence 与 SGP token。无需提权。
- 另有 `POST /product-launcher/v1/default-product/minimize`（Riot Client API，204 实测通过）可最小化窗口。
- 进程优先级修改对提权启动的客户端默认 `拒绝访问`（需同等权限），插件会尝试并静默降级。
- 界面可随时通过 WeGame 重新打开；重连后插件自动重发现。
- 插件内置 `resource_saver`（默认关闭）：连接后延迟 N 秒自动 kill-ux，
  带护栏——仅当 gameflow-phase ∈ {None, Lobby} 时执行，绝不在选人/对局中触发。

---

## 核心问题的直接回答

> **2026 年 8 月当前，英雄联盟腾讯国服是否存在无需本地 League Client、
> 仅通过 QQ / WeGame 云端认证即可查询其他用户实时 LOL Presence 的稳定方法？**

### 答：不能（无公开稳定方法）

| 尝试过的路径 | 结论 | 证据 |
| --- | --- | --- |
| 旧 TGP `get_battle_topbar_info`（含 `online_status` 字段） | **legacy / unusable**。字段存在证明腾讯云端曾有在线状态，但凭据体系随"掌上 WeGame"App 停运（2022-09-08）断链 | WegameApiReversing README；2026-08-29 实测接口返回 `code 500 req error` |
| `query_by_nick` / `do_search_user` | **legacy / unusable**（同上凭据断链） | 同上 |
| 新 WeGame pallas（`wegame.pallas.game.LolBattle/*`） | 2026-06 实测可用，但**只能查 Cookie 属主自己**的战绩，无 Presence | Kroomax/LOLHistory `方法总结.md`："2026年6月实测…客户端都不用开" |
| WeGame 助手页 tcall `[2,{qquin,area_id}]` | 可查**任意 QQ 的角色 basic_info**（QQ→LOL 映射），生产 JS 在用；但无 Presence | 2026-08-29 抓取 `wegame.gtimg.com/.../frame.*.js` 生产代码 |
| 掌上英雄联盟 App 好友在线 | 官方功能存在（2025 年仍在运营），**接口无公开逆向复现** → 无法验证 | 掌盟 JSAPI 文档不含好友关系链 |
| SGP GSM `/gsm/v1/ledge/region/{sub}/puuid/{puuid}` | 可点查**任意玩家**的"是否在游戏中"（含阵容/英雄），但 token 必须由**客户端登录链**派生 → 不是纯云端 | LeagueAkari `sgp/gsm.ts`；2026-08-29 实测服务器在线（401=缺 token） |
| Riot XMPP / 国服 ejabberd 好友 presence | 仅客户端聊天链路可用 | `hn4-idc-ejabberd.lol.qq.com` 等域名 |

---

## 数据能力矩阵（根据实际验证填写）

| 能力 | 云端 WeGame | SGP | 本地 LCU | 是否需要好友 |
| --- | --- | --- | --- | --- |
| QQ → LOL角色 | ⚠️ tcall basic_info（实验，需 Cookie，置信中） | ✗ | ✗（好友列表有 puuid 但无 QQ） | ✗ |
| PUUID | ✗（tcall 返回结构未验证含 puuid） | ✓（aliases 按名字查） | ✓（/lol-summoner/v1/summoners） | 战绩/段位✗ 好友✓ |
| 段位 | ✗ | ✓ `/leagues-ledge/v2/rankedStats/puuid/{puuid}`（不能跨区） | ✓ `/lol-ranked/v1/ranked-stats/{puuid}` | ✗ |
| 战绩 | ⚠️ 仅 Cookie 属主自己 | ✓ `match-history-query/v1/products/lol/player/{puuid}/SUMMARY` | ⚠️ 旧接口，多次受限 | ✗ |
| 当前在线 | ✗ | ✗（GSM 只知"是否在游戏中"） | ✓ 好友 presence（availability + lol.gameStatus） | **✓（必须好友）** |
| 当前游戏中 | ✗ | ✓ GSM 点查任意 puuid | ✓ 好友 presence `inGame` + champion/queue | GSM✗ / LCU✓ |
| 当前英雄 | ✗ | ✓ GSM / 战绩 | ✓ presence `lol.championId` | GSM✗ / LCU✓ |
| 当前队列 | ✗ | ✗ | ✓ presence `lol.queueId` | ✓ |
| 游戏开始事件 | ✗ | ✓ 可由 GSM 轮询推导（404→有局） | ✓ WSS presence 事件（实时） | GSM✗ / LCU✓ |
| 游戏结束事件 | ✗ | ✓ 同上 + 战绩出现 | ✓ presence 回到 outOfGame | 同上 |

**SGP token 依赖说明**：SGP 的两类 JWT 均从运行中的 League Client 取得——
`GET /entitlements/v1/token`（entitlements）与
`GET /lol-league-session/v1/league-session-token`（league-session，响应体即 JWT）。
JWT 有效期约 1 小时级，需客户端续期。**纯云端（QQ ticket → RSO 登录链）无公开复现，
这是云端化唯一的未突破环节。**

---

## 2026 当前确认可用

1. **LCU 好友 Presence（实时层，唯一稳定的"上线"来源）**
   - REST：`GET /lol-chat/v1/friends`
   - WSS：`OnJsonApiEvent_lol-chat_v1_friends`
   - 字段：`puuid / gameName / availability / lol.gameStatus / lol.gameId /
     lol.championId / lol.queueId / lol.rankedLeagueTier|Division`
   - `gameStatus` 枚举：`outOfGame / inQueue / lobby / matchmaking / readyCheck /
     champSelect / inGame / awake / mobileLobby`
   - 证据：LeagueAkari `subscribed-lcu-endpoints.ts`、hh-lol-prophet、LoL-DIY-Programs
   - **约束：好友对象才可见 → 需要观察号，群友须加观察号为好友**

2. **SGP 战绩/段位（战绩层）**
   - 服务器：`{hn1-k8s,hn10-k8s,tj100,tj101,nj100,gz100,cq100,bgp2-k8s}-sgp.lol.qq.com:21019`
   - 战绩：`/match-history-query/v1/products/lol/player/{puuid}/SUMMARY`（entitlements Bearer）
     响应 `{games:[{metadata:{match_id,timestamp},json:{gameId,queueId,gameDuration,participants:[{puuid,championId,kills,deaths,assists,win}]}}]}`
   - 段位：`/leagues-ledge/v2/rankedStats/puuid/{puuid}`（league-session Bearer，不能跨区）
   - 改名追踪：`https://prod-rso.lol.qq.com:3001/aliases/v1/aliases?gameName=X&tagLine=Y`（entitlements）
   - 证据：LeagueAkari 源码 + `2026-05-16-tencent-hn10` 测试快照；sona README
     （2026-08 仍在更新）"战绩查询优先使用 SGP 接口"

3. **WeGame pallas 战绩（仅自己）**
   - `POST www.wegame.com.cn/api/v1/wegame.pallas.game.LolBattle/GetBattleList`
     Cookie：`puin/pkey/tgp_biz_ticket/tgp_id`（mitmproxy 抓 WeGame PC）
   - 2026-06 实测可用（LOLHistory）；count≤10、offset<500、Cookie 会过期
   - 对本插件增益有限（查不了任意绑定群友）→ 不默认接入

## 需要本地 LCU

- **Presence 全部能力**（好友在线/lobby/游戏中/英雄/队列）
- SGP 的两个 token（entitlements + league-session）
- LCU 旧战绩接口 `/lol-match-history/v1/products/lol/{puuid}/matches`（兜底，可能 404/403）
- LCU 段位 `/lol-ranked/v1/ranked-stats/{puuid}`（兜底）

## 可以纯服务器（不启动客户端）

- WeGame pallas 自查战绩（Cookie 抓取 + 续期是现实成本）
- tcall basic_info 的 QQ→角色映射（实验性）
- **注意：以上都不含 Presence。纯服务器部署 = 只能做战绩机器人，不能做上线播报。**

## 已失效 / 不采用

| 接口 | 状态 | 原因 |
| --- | --- | --- |
| `m.wegame.com.cn/.../query_by_nick` | legacy / unusable | 凭据链断（掌上 WeGame 停运）；2026-08 实测 code 500 |
| `m.wegame.com.cn/.../get_battle_topbar_info`（online_status） | legacy / unusable | 同上 |
| `GetBattleList`（旧 trpc 路径） | 不采用 | 服务活着（code 508）但无公开可用凭据；新 pallas 已替代 |
| `www.ziyou.cn` / `lol.qqq.com` / `jiazhang100.com` | 死 | 2026-08-29 实测无法访问/已易主 |
| 本机 `gameflow-phase` 判群友状态 | 禁用 | 只反映观察号自己的流程，不反映好友 |

---

## 最终架构决策

```text
第 1 优先级（实时层）：LCU 观察号好友 Presence
    WSS OnJsonApiEvent_lol-chat_v1_friends（秒级）
    + 每 5 min 全量对账（防丢事件）
    + 断线指数退避重连（1s..60s），重连后静默封口

第 2 优先级（战绩层）：SGP（token 来自观察号客户端）→ LCU 旧接口兜底
    开局：presence 事件直接播报
    结算：IN_GAME → 退避 10/20/40/60s 拉最新战绩，出数据才显示 KDA

云端 WeGame：仅保留实验性 QQ→角色解析（tcall），Presence 标记为不可用
    ——将来若"QQ ticket → RSO"脱机登录链被公开复现，
    只需实现新的 PresenceProvider 挂进 auto 链路，业务层零改动。
```

## 部署形态结论

- **Linux/Docker 服务器 + 同网段一台 Windows 观察机（运行 LOL 客户端 + 本插件
  远程连 LCU）**：推荐形态。观察号上线即具备实时播报 + SGP 战绩。
- **AstrBot 与客户端同机（Windows）**：最简形态，lockfile/进程参数自动发现。
- **纯 Linux 服务器、无任何客户端**：绑定/缓存查询可用，**上线播报不可用**（插件会
  明确提示，不伪造）。

## QQ 自动绑定的现实边界

国服账号体系无公开的"QQ → puuid"直查：
- 观察号好友列表有 puuid+gameName，但不含 QQ 号；
- tcall basic_info（qquin+area_id）是最接近的云端链路，实验性接入；
- 默认绑定方式为 `/lol 绑定 游戏名#TAG`（LCU aliases + 召唤师名双通道解析）。
