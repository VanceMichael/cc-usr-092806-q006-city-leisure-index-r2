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
CREATE TABLE IF NOT EXISTS lr_cities (
    city_code TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS lr_city_grants (
    grant_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL,
    city_code TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    revoked_at TEXT,
    UNIQUE(organization_id, city_code)
);
CREATE TABLE IF NOT EXISTS lr_periods (
    period TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    review_at TEXT,
    closed_at TEXT,
    closed_by TEXT
);
CREATE TABLE IF NOT EXISTS lr_batches (
    batch_no TEXT PRIMARY KEY,
    period TEXT NOT NULL,
    city_code TEXT NOT NULL,
    payload_digest TEXT NOT NULL,
    status TEXT NOT NULL,
    record_count INTEGER NOT NULL,
    submitted_by TEXT NOT NULL,
    submitted_at TEXT NOT NULL,
    processed_at TEXT,
    result_ids_json TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS lr_batch_conflicts (
    conflict_id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_no TEXT NOT NULL,
    period TEXT NOT NULL,
    city_code TEXT NOT NULL,
    existing_digest TEXT NOT NULL,
    incoming_digest TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS lr_baselines (
    baseline_id TEXT PRIMARY KEY,
    period TEXT NOT NULL,
    city_code TEXT NOT NULL,
    scope TEXT NOT NULL,
    version_seq INTEGER NOT NULL,
    population REAL NOT NULL,
    unit TEXT NOT NULL,
    coverage_start TEXT NOT NULL,
    coverage_end TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    UNIQUE(period, city_code, scope, version_seq)
);
CREATE INDEX IF NOT EXISTS lr_baselines_active ON lr_baselines(period, city_code, scope, status);
CREATE TABLE IF NOT EXISTS lr_observations (
    observation_id TEXT PRIMARY KEY,
    period TEXT NOT NULL,
    city_code TEXT NOT NULL,
    dimension TEXT NOT NULL,
    metric_code TEXT NOT NULL,
    basis TEXT NOT NULL,
    raw_value REAL NOT NULL,
    unit TEXT NOT NULL,
    normalized_value REAL NOT NULL,
    normalized_unit TEXT NOT NULL,
    denominator_scope TEXT NOT NULL,
    baseline_id TEXT NOT NULL,
    coverage_start TEXT NOT NULL,
    coverage_end TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    batch_no TEXT NOT NULL,
    status TEXT NOT NULL,
    superseded_by TEXT,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS lr_obs_active ON lr_observations(period, city_code, dimension, status);
CREATE TABLE IF NOT EXISTS lr_weights (
    weight_id TEXT PRIMARY KEY,
    period TEXT NOT NULL,
    city_code TEXT NOT NULL,
    dimension TEXT NOT NULL,
    metric_code TEXT NOT NULL,
    weight REAL NOT NULL,
    version_seq INTEGER NOT NULL,
    source_proposal_id TEXT,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    UNIQUE(period, city_code, dimension, metric_code, version_seq)
);
CREATE INDEX IF NOT EXISTS lr_weights_active ON lr_weights(period, city_code, dimension, metric_code, status);
CREATE TABLE IF NOT EXISTS lr_proposals (
    proposal_id TEXT PRIMARY KEY,
    period TEXT NOT NULL,
    city_code TEXT,
    kind TEXT NOT NULL,
    dimension TEXT NOT NULL,
    metric_code TEXT NOT NULL,
    before_json TEXT NOT NULL,
    after_json TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL,
    proposed_by TEXT NOT NULL,
    confirmed_by TEXT,
    created_at TEXT NOT NULL,
    confirmed_at TEXT
);
CREATE INDEX IF NOT EXISTS lr_proposals_apply ON lr_proposals(period, city_code, dimension, metric_code, status);
CREATE TABLE IF NOT EXISTS lr_results (
    result_id TEXT PRIMARY KEY,
    period TEXT NOT NULL,
    city_code TEXT NOT NULL,
    dimension TEXT NOT NULL,
    score REAL NOT NULL,
    inputs_digest TEXT NOT NULL,
    weight_digest TEXT NOT NULL,
    calc_batch_no TEXT,
    status TEXT NOT NULL,
    post_close INTEGER NOT NULL DEFAULT 0,
    lineage_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    published_at TEXT,
    published_by TEXT
);
CREATE INDEX IF NOT EXISTS lr_results_current ON lr_results(period, city_code, dimension, status);
CREATE TABLE IF NOT EXISTS lr_rankings (
    ranking_id TEXT PRIMARY KEY,
    period TEXT NOT NULL,
    city_code TEXT NOT NULL,
    rank_no INTEGER NOT NULL,
    total_score REAL NOT NULL,
    result_ids_json TEXT NOT NULL,
    dimension_weights_json TEXT NOT NULL,
    published_at TEXT NOT NULL,
    published_by TEXT NOT NULL,
    UNIQUE(period, city_code)
);
CREATE TABLE IF NOT EXISTS lr_errata (
    erratum_id TEXT PRIMARY KEY,
    period TEXT NOT NULL,
    city_code TEXT NOT NULL,
    ranking_id TEXT NOT NULL,
    old_rank INTEGER NOT NULL,
    new_rank INTEGER NOT NULL,
    old_score REAL NOT NULL,
    new_score REAL NOT NULL,
    linked_result_ids_json TEXT NOT NULL,
    reason TEXT NOT NULL,
    issued_by TEXT NOT NULL,
    issued_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS lr_errata_ranking ON lr_errata(period, ranking_id);
CREATE TABLE IF NOT EXISTS lr_recalc_jobs (
    job_id TEXT PRIMARY KEY,
    period TEXT NOT NULL,
    city_code TEXT NOT NULL,
    dimensions_json TEXT NOT NULL,
    reason TEXT NOT NULL,
    triggered_by TEXT NOT NULL,
    status TEXT NOT NULL,
    last_error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    processed_at TEXT,
    result_ids_json TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS lr_recalc_pending ON lr_recalc_jobs(status, created_at);
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
