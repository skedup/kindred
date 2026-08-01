-- Kindred SQLite schema v5
-- v5（Artifact Visibility ART1A）：+ artifact_commit committed descriptor 读模型
-- v4（Inventory I0）：+ inventory_items 运行期私有物品名册
-- v3（L3 PlaceStore）：+ place_visits 表（真实到访事件索引，从 tick.act_result.location_arrival 派生）
-- v2（earlier milestone）：+ watcher_cursor 表（「心已读书签」持久化 / 崩溃恢复；路 X / earlier milestone 后写者为 sense_io）
--
-- 来源：
-- - 02-state-system.md §3.10.1 tick 表 + state_latest / episode 视图
-- - 09-memory.md §3.2 thought 表 §4.4.4.1 episode_recall 表
-- - 14-heart-graph.md §3.5 T3.persist.write_state 同事务写 tick + episode_recall
--
-- 设计原则：
-- - 8 层 state 不平铺成 50+ 列，每层一个 JSON 列
-- - 单一真相源：tick 表存所有历史；视图不占额外存储
-- - 心嘴独立 agent 不引入外键（参考 02 §3.10.6）
-- - JSON 列天然柔韧：加新子字段不需要 ALTER TABLE（参考 02 §3.10.4）
--
-- 字段顺序契约：
--   下面 §C 8 层 JSON 列顺序与 state.State.model_fields / ticks.py::STATE_LAYERS
--   完全一致。三处中改任何一处都必须同步另两处，否则
--   tests/unit/state/test_state.py::test_field_order_matches_doc 会爆。
--   定位三处契约点：本文件 §C 列定义、state.py 搜 ``class State``、
--   ticks.py 搜 ``STATE_LAYERS``（grep ``STATE_LAYERS`` 可跳到代码侧两处）。

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = OFF;

-- ─── tick：每 tick 一行（T3.persist.write_state 唯一 INSERT 目标） ────

CREATE TABLE IF NOT EXISTS tick (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT    NOT NULL,                 -- ISO8601（next_state.time.iso）
    trigger_source  TEXT    NOT NULL,                 -- heartbeat / watcher / cold_start
    triggered_at    TEXT    NOT NULL,

    -- §C: 8 层 state 完整快照（next_state，T3 写时是 T2 末态）
    --   ↓ 列顺序 = state.State.model_fields = ticks.py::STATE_LAYERS（顺序契约）
    interior        JSON    NOT NULL,
    embodiment      JSON    NOT NULL,
    bag             JSON    NOT NULL,
    activity        JSON    NOT NULL,
    location        JSON    NOT NULL,
    time            JSON    NOT NULL,
    environment     JSON    NOT NULL,
    presence        JSON    NOT NULL,

    -- §D: T1.sense.llm 输出
    note            TEXT,
    significance    INTEGER,                          -- 1~10
    act_decision    JSON,                             -- ActDecision（含 act/kind/target/reason）

    -- §E: T2.act.llm 输出（act_decision.act == False 时为 NULL）
    act_result      JSON
);

CREATE INDEX IF NOT EXISTS idx_tick_ts            ON tick(ts);
-- 复合索引 (significance, ts)：episode 视图 `WHERE significance >= 7 ORDER BY ts DESC` 走这个。
-- 堆里先 significance 范围扫，后 ts 有序输出。
-- 不另建单列 idx_tick_significance ——单列查询走复合索引前缀就够（SQLite 会走）。
CREATE INDEX IF NOT EXISTS idx_tick_significance_ts
    ON tick(significance, ts);
-- 高频 where 用 generated column 索引（02 §3.10.1）
CREATE INDEX IF NOT EXISTS idx_tick_activity_name
    ON tick(json_extract(activity, '$.name'));

-- ─── state_latest 视图（T1.sense.io 读 prev_state） ────────────────

DROP VIEW IF EXISTS state_latest;
CREATE VIEW state_latest AS
    SELECT * FROM tick ORDER BY ts DESC LIMIT 1;

-- ─── episode 视图（significance>=7 高光闪回，09 Layer C） ──────────

DROP VIEW IF EXISTS episode;
CREATE VIEW episode AS
    SELECT * FROM tick WHERE significance >= 7 ORDER BY ts DESC;

-- ─── thought：interior.thoughts 子层的持久化镜像（09 §3.2） ────────

CREATE TABLE IF NOT EXISTS thought (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT    NOT NULL,                 -- 写入时间（同 source_tick.ts）
    description     TEXT    NOT NULL,                 -- 02 §4.1.4：thought 文本
    mood_w          INTEGER,                          -- 02 §4.1.4：对 mood 影响权重（-30 ~ +30）
    expire_at       TEXT,                             -- 02 §4.1.4：过期时间（NULL = 不过期）
    tag             TEXT    NOT NULL,                 -- 02 §4.1.4：分类
    source_tick_id  INTEGER                           -- 软关联 tick.id（不加外键）
);

CREATE INDEX IF NOT EXISTS idx_thought_ts      ON thought(ts);
CREATE INDEX IF NOT EXISTS idx_thought_tag     ON thought(tag);
CREATE INDEX IF NOT EXISTS idx_thought_expire  ON thought(expire_at) WHERE expire_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_thought_source  ON thought(source_tick_id);

-- ─── episode_recall：sig≥7 tick 的闪回冷却度表（09 §4.4.4.1 / 14 §3.5） ──
--
-- T3.persist.write_state 与 tick 同事务 INSERT（sig≥7，初始 cooldown=100）。
-- T3.persist.flush_bundle 选中后 UPDATE cooldown -= 50，0 不再闪回。
-- 软关联 tick.id，无外键（与 tick 一致的独立-agent 风格）。

CREATE TABLE IF NOT EXISTS episode_recall (
    tick_id           INTEGER PRIMARY KEY,            -- 软关联 tick.id
    cooldown          INTEGER NOT NULL,               -- 当前冷却度（满=100，0=不再闪回）
    last_recalled_ts  TEXT                            -- 最后一次被选中时间（NULL=从未）
);

CREATE INDEX IF NOT EXISTS idx_episode_recall_cooldown
    ON episode_recall(cooldown);

-- ─── main_session_messages：嘴写的 main session 消息缓存（earlier milestone / docs/13） ──
--
-- watcher 进程把 main session 的消息历史定时缓存进本表，心（子 agent）启动时
-- 读最近 N 条感知嘴正在和 partner 聊什么（spawn 模式子 session 默认看不到主会话）。
--
-- role 业务层语义（earlier milestone 去专名化，覆盖 docs/13 §9 的 kindred_voice/kindred_heart）：
--   partner  : 对面的人（user）说的话（watcher 从 protocol 层 user 映射）
--   my_voice : 嘴（main agent）说的话（watcher 从 protocol 层 assistant 映射）
--   my_heart : [legacy] 旧协议下心自产写表的行（record_outbound）。Bug2（2026-06-29）
--              退役 record_outbound 后当前路径不再产生此 role；保留仅为兼容历史数据。
-- LLM 协议层仍是 user/assistant，仅喂模型前由调用方映射。
--
-- 设计：(session_key, msg_id) 唯一键 → upsert 幂等；content 可 NULL，
-- 子 agent 只依赖 text_summary；与独立-agent 风格一致，无外键。

CREATE TABLE IF NOT EXISTS main_session_messages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_key   TEXT    NOT NULL,
    msg_id        TEXT    NOT NULL,
    seq           INTEGER,
    role          TEXT    NOT NULL,
    content       TEXT,
    text_summary  TEXT,
    ts_ms         INTEGER NOT NULL,
    cached_at     TEXT    NOT NULL,
    UNIQUE(session_key, msg_id)
);

CREATE INDEX IF NOT EXISTS idx_main_msgs_session_ts
    ON main_session_messages(session_key, ts_ms DESC);
CREATE INDEX IF NOT EXISTS idx_main_msgs_session_seq
    ON main_session_messages(session_key, seq DESC);

-- ─── watcher_cursor：「心已读书签」持久化（earlier milestone 建表 / N-4 崩溃恢复） ──
--
-- cursor = 「心已读到哪条」的单一书签。路 X / earlier milestone：运行期唯一写者是 sense_io
-- （tick 内读完 cursor 后消息即推进表）；watcher 只读表判是否唤醒，仅 prime 首启
-- 写一次 baseline。落库意义 = daemon 崩溃重启后从书签位置继续，不从头重放历史。
-- 库无书签（首次启动）→ watcher.prime 设到表尾 baseline→不重放历史（保 N-1 契约）。
--
-- 游标三元组 (ts_ms, seq, id) 与 MessageCursor 一致；seq 可能是归一哨兵 -1。
-- 一个 session_key 一行（PK）→ set 用 INSERT … ON CONFLICT 更新。独立-agent 风格无外键。

CREATE TABLE IF NOT EXISTS watcher_cursor (
    session_key   TEXT    PRIMARY KEY,
    ts_ms         INTEGER NOT NULL,
    seq           INTEGER NOT NULL,
    id            INTEGER NOT NULL,
    updated_at    TEXT    NOT NULL                     -- ISO8601
);

-- ─── place_visits：真实到访事件索引（PlaceStore，第四趴 / L3） ──────
--
-- tick 表仍是真相源；本表是从 tick.act_result.location_arrival 派生的读优化镜像
-- （episode_recall 同款副表模式）。T3 写 tick 时同事务派生 INSERT；损坏可从 tick
-- 历史重建（db/places.py::rebuild_place_visits）。
--
-- 只记录 committed 且真实到达（act_result.location_arrival）的到访：
-- 未选候选不进、未 committed 不进、停留同地不重复写（每 tick 至多一次 arrival
-- 事件 → tick_id UNIQUE；未来单 tick 多段地点意图需改 (tick_id, sequence)）。
--
-- name/address/type/source/candidate_snapshot 是展示缓存/性能冗余（arrival 事件
-- 已由 act 节点按计划归一化）；与 tick 冲突时以 tick 为准，重建修复本表。
-- 软关联 tick.id，无外键（独立-agent 风格）。

CREATE TABLE IF NOT EXISTS place_visits (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    place_key           TEXT    NOT NULL,             -- 稳定地点身份（如 baidu:uid / character_card:home:<hash>）
    tick_id             INTEGER NOT NULL UNIQUE,      -- 软关联 tick.id（一 tick 至多一次到访）
    arrived_at          TEXT    NOT NULL,             -- ISO8601，拷自 tick.location.arrived_at
    name                TEXT    NOT NULL,             -- 到访时地点名（缓存）
    address             TEXT,                         -- 地址（缓存，可空）
    type                TEXT,                         -- 粗类型（缓存，可空）
    source              TEXT    NOT NULL,             -- 候选来源（Provider / character-card 已知地点）
    activity_name       TEXT,                         -- 到访时 activity 名（缓存，可空）
    candidate_snapshot  JSON,                         -- arrival 事件的候选事实快照（性能冗余）
    created_at          TEXT    NOT NULL              -- **确定性镜像时间**（=arrived_at，
                                                      -- 非行创建墙钟：rebuild 必须逐字节
                                                      -- 复现在线写入，墙钟做不到）
);

-- pre-act exact lookup：按候选 place_key[] 批量查到访聚合（COUNT + MAX arrived_at）。
CREATE INDEX IF NOT EXISTS idx_place_visits_key_arrived
    ON place_visits(place_key, arrived_at);

-- ─── inventory_items：运行期私有物品名册（Inventory I0）──────────

CREATE TABLE IF NOT EXISTS inventory_items (
    item_key       TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    home_pool      TEXT NOT NULL,
    kind           TEXT NOT NULL,
    equip_to_json  JSON NOT NULL,
    description    TEXT
);

-- ─── artifact_commit：Host committed Artifact 读模型 ─────────────

CREATE TABLE IF NOT EXISTS artifact_commit (
    tick_id             INTEGER NOT NULL,
    artifact_ordinal    INTEGER NOT NULL,
    artifact_ref        TEXT    NOT NULL UNIQUE,
    producer            TEXT    NOT NULL,
    profile             TEXT    NOT NULL,
    activity_name       TEXT    NOT NULL,
    activity_started_at TEXT    NOT NULL,
    created_at          TEXT    NOT NULL,
    PRIMARY KEY (tick_id, artifact_ordinal)
);

CREATE INDEX IF NOT EXISTS idx_artifact_commit_activity_run
    ON artifact_commit(activity_name, activity_started_at, tick_id, artifact_ordinal);

-- ─── schema_version：迁移追踪 ─────────────────────────────────────

CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT    NOT NULL                       -- ISO8601
);
