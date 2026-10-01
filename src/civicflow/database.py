"""SQLite 连接、事务和数据库初始化。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = r"""
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS entities (
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    state TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    updated_by TEXT NOT NULL,
    PRIMARY KEY(entity_type, entity_id)
);
CREATE TABLE IF NOT EXISTS entity_versions (
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    state TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    request_key TEXT NOT NULL,
    PRIMARY KEY(entity_type, entity_id, version)
);
CREATE INDEX IF NOT EXISTS entity_versions_asof ON entity_versions(entity_type, entity_id, valid_from, version);
CREATE TABLE IF NOT EXISTS idempotency_keys (
    scope TEXT NOT NULL,
    request_key TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(scope, request_key)
);
CREATE TABLE IF NOT EXISTS audit_entries (
    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    detail_json TEXT NOT NULL,
    previous_digest TEXT NOT NULL,
    entry_digest TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS inbox_messages (
    source TEXT NOT NULL,
    source_key TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    payload_digest TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    status TEXT NOT NULL,
    PRIMARY KEY(source, source_key, sequence)
);
CREATE TABLE IF NOT EXISTS inbox_conflicts (
    conflict_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    source_key TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    existing_digest TEXT NOT NULL,
    incoming_digest TEXT NOT NULL,
    received_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outbox_messages (
    message_id TEXT PRIMARY KEY,
    topic TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    available_at TEXT NOT NULL,
    lease_until TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    delivered_at TEXT
);
CREATE INDEX IF NOT EXISTS outbox_ready ON outbox_messages(status, available_at, lease_until);
CREATE TABLE IF NOT EXISTS journal_entries (
    entry_id TEXT PRIMARY KEY,
    journal_key TEXT NOT NULL,
    account TEXT NOT NULL,
    currency TEXT NOT NULL,
    amount_minor INTEGER NOT NULL,
    direction TEXT NOT NULL,
    reference TEXT NOT NULL,
    reversed_entry_id TEXT,
    occurred_at TEXT NOT NULL,
    posted_by TEXT NOT NULL,
    FOREIGN KEY(reversed_entry_id) REFERENCES journal_entries(entry_id)
);
CREATE INDEX IF NOT EXISTS journal_reference ON journal_entries(journal_key, reference, occurred_at);
CREATE TABLE IF NOT EXISTS resource_reservations (
    reservation_id TEXT PRIMARY KEY,
    resource_id TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    start_at TEXT NOT NULL,
    end_at TEXT NOT NULL,
    status TEXT NOT NULL,
    version INTEGER NOT NULL,
    created_by TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS reservation_window ON resource_reservations(resource_id, start_at, end_at, status);
CREATE TABLE IF NOT EXISTS scheduled_jobs (
    job_id TEXT PRIMARY KEY,
    job_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    run_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 0,
    lease_until TEXT,
    last_error TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS jobs_due ON scheduled_jobs(status, run_at, lease_until);

-- 城市休闲化指数复核：城市、合作机构授权与统计期
CREATE TABLE IF NOT EXISTS leisure_cities (
    city_id TEXT PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS leisure_city_grants (
    org_id TEXT NOT NULL,
    city_id TEXT NOT NULL,
    granted_by TEXT NOT NULL,
    granted_at TEXT NOT NULL,
    PRIMARY KEY(org_id, city_id)
);
CREATE TABLE IF NOT EXISTS leisure_periods (
    period_id TEXT PRIMARY KEY,
    year INTEGER NOT NULL UNIQUE,
    label TEXT NOT NULL,
    coverage_start TEXT NOT NULL,
    coverage_end TEXT NOT NULL,
    state TEXT NOT NULL,
    closed_by TEXT,
    closed_at TEXT
);
-- 报送批次：同一批次编号返回既有结果；编号相同内容不一致时记冲突且不入库
CREATE TABLE IF NOT EXISTS leisure_batches (
    batch_id TEXT PRIMARY KEY,
    period_id TEXT NOT NULL,
    city_id TEXT NOT NULL,
    payload_digest TEXT NOT NULL,
    status TEXT NOT NULL,
    result_json TEXT NOT NULL DEFAULT '{}',
    recorded_by TEXT NOT NULL,
    received_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS leisure_batch_conflicts (
    conflict_id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL,
    existing_digest TEXT NOT NULL,
    incoming_digest TEXT NOT NULL,
    received_at TEXT NOT NULL
);
-- 按城市、统计期、指标保存的观测数据（人口基准/休闲资源/覆盖/密度输入与样本出处），带版本
CREATE TABLE IF NOT EXISTS leisure_obs (
    city_id TEXT NOT NULL,
    period_id TEXT NOT NULL,
    metric TEXT NOT NULL,
    current_version INTEGER NOT NULL,
    value REAL NOT NULL,
    unit TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    recorded_by TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY(city_id, period_id, metric)
);
CREATE TABLE IF NOT EXISTS leisure_obs_versions (
    city_id TEXT NOT NULL,
    period_id TEXT NOT NULL,
    metric TEXT NOT NULL,
    version INTEGER NOT NULL,
    value REAL NOT NULL,
    unit TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    source_detail TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    recorded_by TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY(city_id, period_id, metric, version)
);
-- 指标权重按统计期版本化
CREATE TABLE IF NOT EXISTS leisure_weights (
    period_id TEXT NOT NULL,
    dimension TEXT NOT NULL,
    weight REAL NOT NULL,
    version INTEGER NOT NULL,
    adjustment_id TEXT,
    changed_by TEXT NOT NULL,
    changed_at TEXT NOT NULL,
    PRIMARY KEY(period_id, dimension)
);
-- 换算或权重调整：提出需说明原因，经另一名成员确认后才影响待发布结果
CREATE TABLE IF NOT EXISTS leisure_adjustments (
    adjustment_id TEXT PRIMARY KEY,
    period_id TEXT NOT NULL,
    city_id TEXT,
    kind TEXT NOT NULL,
    target TEXT NOT NULL,
    from_value TEXT NOT NULL,
    to_value TEXT NOT NULL,
    factor REAL,
    reason TEXT NOT NULL,
    state TEXT NOT NULL,
    proposed_by TEXT NOT NULL,
    proposed_at TEXT NOT NULL,
    confirmed_by TEXT,
    confirmed_at TEXT
);
-- 每次计算固定单位、覆盖期和分母版本
CREATE TABLE IF NOT EXISTS leisure_computations (
    computation_id TEXT PRIMARY KEY,
    period_id TEXT NOT NULL,
    city_id TEXT NOT NULL,
    scope_dimensions TEXT NOT NULL,
    trigger TEXT NOT NULL,
    reason TEXT NOT NULL,
    parent_computation_id TEXT,
    status TEXT NOT NULL,
    basis_json TEXT NOT NULL,
    scores_json TEXT NOT NULL,
    total REAL NOT NULL,
    inputs_digest TEXT NOT NULL,
    computed_by TEXT NOT NULL,
    computed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS leisure_computation_lookup ON leisure_computations(period_id, city_id, status);
-- 正式发布的年度榜单（不可变）与带关联关系的勘误版本
CREATE TABLE IF NOT EXISTS leisure_rankings (
    ranking_id TEXT PRIMARY KEY,
    period_id TEXT NOT NULL,
    title TEXT NOT NULL,
    entries_json TEXT NOT NULL,
    digest TEXT NOT NULL,
    published_by TEXT NOT NULL,
    published_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS leisure_errata (
    erratum_id TEXT PRIMARY KEY,
    ranking_id TEXT NOT NULL,
    period_id TEXT NOT NULL,
    city_id TEXT NOT NULL,
    dimension TEXT NOT NULL,
    before_computation_id TEXT NOT NULL,
    after_computation_id TEXT NOT NULL,
    before_value REAL NOT NULL,
    after_value REAL NOT NULL,
    change_summary TEXT NOT NULL,
    reason TEXT NOT NULL,
    issued_by TEXT NOT NULL,
    issued_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS leisure_errata_ranking ON leisure_errata(ranking_id);
"""


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(SCHEMA)

    @contextmanager
    def transaction(self, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
