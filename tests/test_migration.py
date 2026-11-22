"""Existing databases must gain split support without re-importing data."""

from __future__ import annotations

import sqlite3

from app.db import connect, init_db
from app.reconcile import reconcile
from app.repository import latest_submission, load_snapshot, split_batch

OLD_SCHEMA = """
CREATE TABLE submissions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('import', 'revision')),
    note TEXT NOT NULL DEFAULT '');
CREATE TABLE batches (
    submission_id INTEGER NOT NULL REFERENCES submissions(id) ON DELETE CASCADE,
    code TEXT NOT NULL, revision INTEGER NOT NULL,
    PRIMARY KEY (submission_id, code));
CREATE TABLE sheets (
    submission_id INTEGER NOT NULL REFERENCES submissions(id) ON DELETE CASCADE,
    code TEXT NOT NULL, total_pages INTEGER NOT NULL,
    PRIMARY KEY (submission_id, code));
CREATE TABLE sheet_entries (
    submission_id INTEGER NOT NULL, sheet_code TEXT NOT NULL,
    batch_code TEXT NOT NULL, revision INTEGER NOT NULL,
    PRIMARY KEY (submission_id, sheet_code, batch_code),
    FOREIGN KEY (submission_id, sheet_code)
        REFERENCES sheets(submission_id, code) ON DELETE CASCADE,
    FOREIGN KEY (submission_id, batch_code)
        REFERENCES batches(submission_id, code) ON DELETE CASCADE);
"""


def build_legacy_db(path: str) -> None:
    raw = sqlite3.connect(path)
    try:
        raw.executescript(OLD_SCHEMA)
        raw.execute(
            "INSERT INTO submissions VALUES (1, '2026-01-01T00:00:00+00:00', 'import', 'old')"
        )
        raw.executemany(
            "INSERT INTO batches (submission_id, code, revision) VALUES (1, ?, ?)",
            [("B01", 2), ("B02", 2)],
        )
        raw.execute("INSERT INTO sheets VALUES (1, 'S1', 100)")
        raw.executemany(
            "INSERT INTO sheet_entries VALUES (1, 'S1', ?, ?)",
            [("B01", 2), ("B02", 2)],
        )
        raw.commit()
    finally:
        raw.close()


def test_legacy_database_migrates_and_keeps_data(tmp_path):
    path = str(tmp_path / "legacy.sqlite3")
    build_legacy_db(path)

    conn = connect(path)
    init_db(conn)  # adds batch_lineage and rebuilds sheet_entries in place
    try:
        snap = load_snapshot(conn, 1)
        assert [b.code for b in snap.batches] == ["B01", "B02"]
        assert snap.sheets[0].entries == (("B01", 2), ("B02", 2))
        assert snap.lineage == ()

        new_id = split_batch(conn, 1, "B01", "B01A", "B01B")
        result = reconcile(
            load_snapshot(conn, new_id), ["B01A", "B01B", "B02"]
        )
        assert result.status == "ok"
        assert result.total == 100
        assert latest_submission(conn).id == new_id

        # Migration must be idempotent across restarts.
        init_db(conn)
        assert reconcile(
            load_snapshot(conn, new_id), ["B01A", "B01B", "B02"]
        ).status == "ok"
    finally:
        conn.close()


def test_migrated_database_has_no_batch_fk_on_entries(tmp_path):
    path = str(tmp_path / "legacy2.sqlite3")
    build_legacy_db(path)
    conn = connect(path)
    init_db(conn)
    try:
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='sheet_entries'"
        ).fetchone()["sql"]
        assert "REFERENCES batches" not in sql
    finally:
        conn.close()
