"""SQLite connection and transaction helpers.

Connections run in autocommit mode and transactions are opened explicitly.
That lets read paths take one consistent SQLite snapshot across all queries
(batches, sheets and entries), which is required by the reconciliation rules.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from flask import current_app, g

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def connect(path: str) -> sqlite3.Connection:
    if path != ":memory:":
        Path(path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    # Existing databases created before batch splitting lack batch_lineage.
    # CREATE TABLE/INDEX/TRIGGER IF NOT EXISTS adds the new objects in place,
    # so archived submissions stay usable and no re-import is required.
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    _migrate_legacy(conn)


def _migrate_legacy(conn: sqlite3.Connection) -> None:
    """Best-effort upgrades for databases created by older schema versions."""
    tables = {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    if "batch_lineage" not in tables:  # pragma: no cover - defensive
        conn.executescript(
            """
            CREATE TABLE batch_lineage (
                submission_id INTEGER NOT NULL,
                parent_code   TEXT NOT NULL,
                child_code    TEXT NOT NULL,
                PRIMARY KEY (submission_id, parent_code, child_code)
            );
            CREATE INDEX idx_lineage_submission ON batch_lineage(submission_id);
            """
        )

    # The pre-split schema gave sheet_entries an FK to batches; a split keeps
    # the original batch code on the sheet even though that code stops being a
    # leaf batch, so the FK must go. Rebuild the table once, copying all rows.
    fk_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'sheet_entries'"
    ).fetchone()
    if fk_sql is not None and "REFERENCES batches" in (fk_sql["sql"] or ""):
        conn.execute("PRAGMA foreign_keys = OFF")
        try:
            conn.executescript(
                """
                CREATE TABLE sheet_entries_new (
                    submission_id INTEGER NOT NULL,
                    sheet_code    TEXT NOT NULL,
                    batch_code    TEXT NOT NULL,
                    revision      INTEGER NOT NULL CHECK (revision > 0),
                    PRIMARY KEY (submission_id, sheet_code, batch_code),
                    FOREIGN KEY (submission_id, sheet_code)
                        REFERENCES sheets(submission_id, code) ON DELETE CASCADE
                );
                INSERT INTO sheet_entries_new
                    SELECT submission_id, sheet_code, batch_code, revision
                      FROM sheet_entries;
                DROP TABLE sheet_entries;
                ALTER TABLE sheet_entries_new RENAME TO sheet_entries;
                CREATE INDEX IF NOT EXISTS idx_entries_submission
                    ON sheet_entries(submission_id);
                """
            )
        finally:
            conn.execute("PRAGMA foreign_keys = ON")


@contextmanager
def read_tx(conn: sqlite3.Connection):
    """One read snapshot. Rolls back on exit; SELECT-only is the contract."""
    conn.execute("BEGIN")
    try:
        yield conn
    finally:
        conn.execute("ROLLBACK")


@contextmanager
def write_tx(conn: sqlite3.Connection):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = connect(current_app.config["DATABASE"])
    return g.db


def close_db(_exc: BaseException | None = None) -> None:
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def database_label(path: str) -> str:
    if path == ":memory:":
        return ":memory:"
    return os.path.basename(path)
