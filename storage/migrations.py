"""数据库迁移说明。

当前 Schema 版本：1（见 storage/database.py 中 _MIGRATIONS）。

## v1
初始表结构：
- bindings      QQ↔PUUID 绑定（主键 qq_id+puuid，展示名仅缓存）
- groups        群播报配置（含 umo，用于主动消息）
- presence_state 最近一次 Presence 快照缓存（重启恢复用）
- notification_state 播报冷却/去重状态（按 puuid+group）
- matches       战绩原始 JSON 缓存（按 game_id+puuid）

后续新增字段时，在 database.py 的 _MIGRATIONS 追加
`(2, "ALTER TABLE ...")` 并把 SCHEMA_VERSION 提升到 2。
"""
