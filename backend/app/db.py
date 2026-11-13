"""SQLite persistence: unified measurement table, config versions, dedup."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS measurement_files (
    id INTEGER PRIMARY KEY,
    filename TEXT NOT NULL,
    stored_path TEXT NOT NULL UNIQUE,
    sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    uploaded_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS measurement_datasets (
    id INTEGER PRIMARY KEY,
    file_id INTEGER NOT NULL REFERENCES measurement_files(id),
    dataset_path TEXT NOT NULL,
    shape TEXT NOT NULL,
    dtype TEXT,
    channel_names TEXT NOT NULL,           -- JSON list
    status TEXT NOT NULL DEFAULT 'active', -- active | paused
    issues TEXT,                           -- JSON list of blocking problems
    UNIQUE(file_id, dataset_path)
);

CREATE TABLE IF NOT EXISTS import_configs (
    id INTEGER PRIMARY KEY,
    dataset_id INTEGER NOT NULL REFERENCES measurement_datasets(id),
    version INTEGER NOT NULL,
    config_json TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(dataset_id, config_hash),
    UNIQUE(dataset_id, version)
);

CREATE TABLE IF NOT EXISTS import_jobs (
    id INTEGER PRIMARY KEY,
    config_id INTEGER NOT NULL REFERENCES import_configs(id),
    dataset_id INTEGER NOT NULL REFERENCES measurement_datasets(id),
    index_start INTEGER NOT NULL,
    index_stop INTEGER NOT NULL,
    channels_json TEXT NOT NULL,
    status TEXT NOT NULL,                 -- duplicate | imported | updated | failed
    inserted INTEGER NOT NULL DEFAULT 0,
    updated INTEGER NOT NULL DEFAULT 0,
    skipped_duplicates INTEGER NOT NULL DEFAULT 0,
    diff_json TEXT,                       -- differences vs previous config
    error TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS measurement_readings (
    id INTEGER PRIMARY KEY,
    dataset_id INTEGER NOT NULL REFERENCES measurement_datasets(id),
    channel_index INTEGER NOT NULL,
    channel_name TEXT NOT NULL,
    source_file_id INTEGER NOT NULL REFERENCES measurement_files(id),
    source_dataset_path TEXT NOT NULL,
    array_index INTEGER NOT NULL,
    timestamp_utc TEXT,                   -- ISO-8601 UTC, NULL when time missing
    raw_value REAL,
    value REAL,
    is_missing INTEGER NOT NULL DEFAULT 0,
    config_id INTEGER NOT NULL REFERENCES import_configs(id),
    config_version INTEGER NOT NULL,
    UNIQUE(dataset_id, channel_index, array_index)
);

CREATE INDEX IF NOT EXISTS idx_readings_lookup
    ON measurement_readings (source_file_id, source_dataset_path, array_index);
CREATE INDEX IF NOT EXISTS idx_readings_time
    ON measurement_readings (dataset_id, timestamp_utc);
"""


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        # FastAPI runs sync endpoints in a worker thread pool; access is
        # serialized by the GIL plus short transactions, so allow cross-thread
        # use rather than one connection per request.
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # -- files --------------------------------------------------------------

    def insert_file(self, filename: str, stored_path: str,
                    sha256: str, size_bytes: int) -> int:
        cur = self.conn.execute(
            "INSERT INTO measurement_files(filename, stored_path, sha256, "
            "size_bytes, uploaded_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(stored_path) DO UPDATE SET filename=excluded.filename "
            "RETURNING id",
            (filename, stored_path, sha256, size_bytes, utcnow_iso()),
        )
        return int(cur.fetchone()[0])

    def get_file(self, file_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM measurement_files WHERE id=?", (file_id,)
        ).fetchone()

    def list_files(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM measurement_files ORDER BY id"
        ).fetchall()

    # -- datasets -----------------------------------------------------------

    def upsert_dataset(self, file_id: int, dataset_path: str, shape: list[int],
                       dtype: str, channel_names: list[str], status: str,
                       issues: list[str]) -> int:
        cur = self.conn.execute(
            "INSERT INTO measurement_datasets(file_id, dataset_path, shape, dtype, "
            "channel_names, status, issues) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(file_id, dataset_path) DO UPDATE SET "
            "shape=excluded.shape, dtype=excluded.dtype, "
            "channel_names=excluded.channel_names, status=excluded.status, "
            "issues=excluded.issues RETURNING id",
            (file_id, dataset_path, json.dumps(shape), dtype,
             json.dumps(channel_names, ensure_ascii=False), status,
             json.dumps(issues, ensure_ascii=False)),
        )
        return int(cur.fetchone()[0])

    def get_dataset(self, dataset_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM measurement_datasets WHERE id=?", (dataset_id,)
        ).fetchone()

    def find_dataset(self, file_id: int, dataset_path: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM measurement_datasets WHERE file_id=? AND dataset_path=?",
            (file_id, dataset_path),
        ).fetchone()

    def list_datasets(self, file_id: int | None = None) -> list[sqlite3.Row]:
        if file_id is None:
            return self.conn.execute(
                "SELECT * FROM measurement_datasets ORDER BY id"
            ).fetchall()
        return self.conn.execute(
            "SELECT * FROM measurement_datasets WHERE file_id=? ORDER BY id",
            (file_id,),
        ).fetchall()

    # -- configs ------------------------------------------------------------

    @staticmethod
    def normalize_config(config: dict[str, Any]) -> dict[str, Any]:
        """Stable, comparable representation of an import configuration."""
        keys = (
            "mode", "array_path", "unit", "timezone", "start", "interval",
            "fill_value", "scale", "offset",
        )
        norm = {k: config.get(k) for k in keys if config.get(k) is not None}
        # booleans must survive even when False
        if "use_scale" in config:
            norm["use_scale"] = bool(config["use_scale"])
        norm["channels"] = sorted(int(c) for c in config.get("channels", []))
        return norm

    @staticmethod
    def hash_config(norm: dict[str, Any]) -> str:
        blob = json.dumps(norm, sort_keys=True, ensure_ascii=False,
                          separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def get_or_create_config(self, dataset_id: int,
                             config: dict[str, Any]) -> tuple[int, int, bool]:
        """Return (config_id, version, created)."""
        norm = self.normalize_config(config)
        h = self.hash_config(norm)
        existing = self.conn.execute(
            "SELECT id, version FROM import_configs WHERE dataset_id=? AND "
            "config_hash=?", (dataset_id, h)
        ).fetchone()
        if existing:
            return int(existing["id"]), int(existing["version"]), False
        row = self.conn.execute(
            "SELECT COALESCE(MAX(version), 0) AS v FROM import_configs "
            "WHERE dataset_id=?", (dataset_id,)
        ).fetchone()
        version = int(row["v"]) + 1
        cur = self.conn.execute(
            "INSERT INTO import_configs(dataset_id, version, config_json, "
            "config_hash, created_at) VALUES (?,?,?,?,?)",
            (dataset_id, version, json.dumps(norm, ensure_ascii=False),
             h, utcnow_iso()),
        )
        return int(cur.lastrowid), version, True

    def list_configs(self, dataset_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM import_configs WHERE dataset_id=? ORDER BY version",
            (dataset_id,),
        ).fetchall()

    def previous_config(self, dataset_id: int) -> sqlite3.Row | None:
        """Second-newest config — the one the new version is replacing."""
        return self.conn.execute(
            "SELECT * FROM import_configs WHERE dataset_id=? ORDER BY version "
            "DESC LIMIT 1 OFFSET 1", (dataset_id,)
        ).fetchone()

    def latest_config(self, dataset_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM import_configs WHERE dataset_id=? ORDER BY version "
            "DESC LIMIT 1", (dataset_id,)
        ).fetchone()

    # -- import jobs --------------------------------------------------------

    def insert_job(self, **kw: Any) -> int:
        kw.setdefault("created_at", utcnow_iso())
        cols = ("config_id", "dataset_id", "index_start", "index_stop",
                "channels_json", "status", "inserted", "updated",
                "skipped_duplicates", "diff_json", "error", "created_at")
        values = [kw.get(c) for c in cols]
        cur = self.conn.execute(
            f"INSERT INTO import_jobs({','.join(cols)}) VALUES "
            f"({','.join('?' * len(cols))})", values
        )
        return int(cur.lastrowid)

    def list_jobs(self, dataset_id: int | None = None) -> list[sqlite3.Row]:
        if dataset_id is None:
            return self.conn.execute(
                "SELECT * FROM import_jobs ORDER BY id DESC LIMIT 100"
            ).fetchall()
        return self.conn.execute(
            "SELECT * FROM import_jobs WHERE dataset_id=? ORDER BY id DESC",
            (dataset_id,),
        ).fetchall()

    def find_identical_job(self, config_id: int, start: int, stop: int,
                           channels: list[int]) -> sqlite3.Row | None:
        """A previous successful job with the same slice AND same config means
        re-importing must add zero new readings."""
        rows = self.conn.execute(
            "SELECT * FROM import_jobs WHERE config_id=? AND index_start=? "
            "AND index_stop=? AND status != 'failed' ORDER BY id DESC",
            (config_id, start, stop),
        ).fetchall()
        ch_sorted = json.dumps(sorted(channels))
        for row in rows:
            if json.dumps(sorted(json.loads(row["channels_json"]))) == ch_sorted:
                return row
        return None

    # -- readings -----------------------------------------------------------

    def upsert_reading(self, *, dataset_id: int, channel_index: int,
                       channel_name: str, source_file_id: int,
                       source_dataset_path: str, array_index: int,
                       timestamp_utc: str | None, raw_value: float | None,
                       value: float | None, is_missing: bool,
                       config_id: int, config_version: int) -> str:
        """Insert or refresh one reading. Returns 'inserted' | 'updated' |
        'unchanged'."""
        existing = self.conn.execute(
            "SELECT id, value, is_missing, config_version FROM "
            "measurement_readings WHERE dataset_id=? AND channel_index=? "
            "AND array_index=?",
            (dataset_id, channel_index, array_index),
        ).fetchone()
        missing_int = 1 if is_missing else 0
        if existing is None:
            self.conn.execute(
                "INSERT INTO measurement_readings(dataset_id, channel_index, "
                "channel_name, source_file_id, source_dataset_path, array_index, "
                "timestamp_utc, raw_value, value, is_missing, config_id, "
                "config_version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (dataset_id, channel_index, channel_name, source_file_id,
                 source_dataset_path, array_index, timestamp_utc, raw_value,
                 value, missing_int, config_id, config_version),
            )
            return "inserted"
        if (existing["value"] == value
                and existing["is_missing"] == missing_int):
            return "unchanged"
        self.conn.execute(
            "UPDATE measurement_readings SET channel_name=?, timestamp_utc=?, "
            "raw_value=?, value=?, is_missing=?, config_id=?, config_version=? "
            "WHERE id=?",
            (channel_name, timestamp_utc, raw_value, value, missing_int,
             config_id, config_version, existing["id"]),
        )
        return "updated"

    def query_readings(self, dataset_id: int | None = None,
                       limit: int = 500, offset: int = 0) -> list[sqlite3.Row]:
        if dataset_id is None:
            return self.conn.execute(
                "SELECT * FROM measurement_readings ORDER BY id LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return self.conn.execute(
            "SELECT * FROM measurement_readings WHERE dataset_id=? "
            "ORDER BY array_index, channel_index LIMIT ? OFFSET ?",
            (dataset_id, limit, offset),
        ).fetchall()

    def count_readings(self, dataset_id: int | None = None) -> int:
        if dataset_id is None:
            row = self.conn.execute(
                "SELECT COUNT(*) c FROM measurement_readings"
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT COUNT(*) c FROM measurement_readings WHERE dataset_id=?",
                (dataset_id,),
            ).fetchone()
        return int(row["c"])

    def commit(self) -> None:
        self.conn.commit()

    def rollback(self) -> None:
        self.conn.rollback()
