"""Persistence for submissions and their immutable snapshots."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from .models import Batch, Sheet, Snapshot, Submission
from .parsing import MAX_BATCHES, MAX_SHEETS, ImportError_
from .db import read_tx, write_tx


class RevisionError(ValueError):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _insert_snapshot(
    conn: sqlite3.Connection,
    *,
    source: str,
    note: str,
    batches: tuple[Batch, ...],
    sheets: tuple[Sheet, ...],
    lineage: tuple[tuple[str, str], ...] = (),
) -> int:
    if len(batches) > MAX_BATCHES:
        raise ImportError_([f"批次数 {len(batches)} 超过上限 {MAX_BATCHES}"])
    if len(sheets) > MAX_SHEETS:
        raise ImportError_([f"汇总单张数 {len(sheets)} 超过上限 {MAX_SHEETS}"])
    known = {batch.code for batch in batches}
    # Sheet entries may name a batch that was later split: it stays valid as a
    # lineage parent even though it is no longer a current leaf batch.
    known.update(parent for parent, _ in lineage)
    known.update(child for _, child in lineage)
    for sheet in sheets:
        unknown = [code for code in sheet.batches if code not in known]
        if unknown:
            raise ImportError_(
                [f"汇总单“{sheet.code}”含未知批次：{'、'.join(unknown)}"]
            )
    lineage_codes = {code for pair in lineage for code in pair}
    unknown_lineage = sorted(code for code in lineage_codes if code not in known)
    if unknown_lineage:  # defensive: repository callers always pass valid codes
        raise ImportError_(
            [f"批次谱系含未知批次：{'、'.join(unknown_lineage)}"]
        )

    cur = conn.execute(
        "INSERT INTO submissions (created_at, source, note) VALUES (?, ?, ?)",
        (_now(), source, note),
    )
    submission_id = int(cur.lastrowid)
    conn.executemany(
        "INSERT INTO batches (submission_id, code, revision) VALUES (?, ?, ?)",
        [(submission_id, b.code, b.revision) for b in batches],
    )
    conn.executemany(
        "INSERT INTO sheets (submission_id, code, total_pages) VALUES (?, ?, ?)",
        [(submission_id, s.code, s.total_pages) for s in sheets],
    )
    conn.executemany(
        """INSERT INTO sheet_entries
               (submission_id, sheet_code, batch_code, revision)
           VALUES (?, ?, ?, ?)""",
        [
            (submission_id, sheet.code, code, revision)
            for sheet in sheets
            for code, revision in sheet.entries
        ],
    )
    conn.executemany(
        """INSERT INTO batch_lineage (submission_id, parent_code, child_code)
           VALUES (?, ?, ?)""",
        [(submission_id, parent, child) for parent, child in lineage],
    )
    return submission_id


def latest_submission(conn: sqlite3.Connection) -> Submission | None:
    row = conn.execute(
        "SELECT id, created_at, source, note FROM submissions ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return Submission(**dict(row)) if row is not None else None


def load_snapshot(conn: sqlite3.Connection, submission_id: int) -> Snapshot:
    """Load one snapshot inside a single read transaction."""
    with read_tx(conn):
        return _load_snapshot_in_tx(conn, submission_id)


def _load_snapshot_in_tx(
    conn: sqlite3.Connection, submission_id: int
) -> Snapshot:
    sub_row = conn.execute(
        "SELECT id, created_at, source, note FROM submissions WHERE id = ?",
        (submission_id,),
    ).fetchone()
    if sub_row is None:
        raise LookupError(f"提交记录 {submission_id} 不存在")
    batch_rows = conn.execute(
        "SELECT code, revision FROM batches WHERE submission_id = ? ORDER BY code",
        (submission_id,),
    ).fetchall()
    sheet_rows = conn.execute(
        "SELECT code, total_pages FROM sheets WHERE submission_id = ? ORDER BY code",
        (submission_id,),
    ).fetchall()
    entry_rows = conn.execute(
        """SELECT sheet_code, batch_code, revision
             FROM sheet_entries
            WHERE submission_id = ?
            ORDER BY sheet_code, batch_code""",
        (submission_id,),
    ).fetchall()
    lineage_rows = conn.execute(
        """SELECT parent_code, child_code
             FROM batch_lineage
            WHERE submission_id = ?
            ORDER BY parent_code, child_code""",
        (submission_id,),
    ).fetchall()

    entries: dict[str, list[tuple[str, int]]] = {}
    for row in entry_rows:
        entries.setdefault(row["sheet_code"], []).append(
            (row["batch_code"], row["revision"])
        )
    sheets = tuple(
        Sheet(row["code"], row["total_pages"], tuple(entries.get(row["code"], ())))
        for row in sheet_rows
    )
    return Snapshot(
        submission=Submission(**dict(sub_row)),
        batches=tuple(Batch(row["code"], row["revision"]) for row in batch_rows),
        sheets=sheets,
        lineage=tuple((row["parent_code"], row["child_code"]) for row in lineage_rows),
    )


def latest_state(conn: sqlite3.Connection) -> tuple[Submission, Snapshot] | None:
    """Latest submission and its snapshot read from one consistent snapshot."""
    with read_tx(conn):
        latest = latest_submission(conn)
        if latest is None:
            return None
        return latest, _load_snapshot_in_tx(conn, latest.id)


def load_current(
    conn: sqlite3.Connection, expected_id: int
) -> tuple[Submission, Snapshot]:
    """Load the snapshot in one read transaction, refusing stale ids.

    Refusing an id that is no longer the newest commit is what keeps the web
    page from reconciling a batch list against sheets from a different
    committed state.
    """
    with read_tx(conn):
        latest = latest_submission(conn)
        if latest is None:
            raise RevisionError("尚未导入任何批次修订清单")
        if latest.id != expected_id:
            raise RevisionError(
                "页面状态已过期：已有新的提交，请刷新后重试"
            )
        return latest, _load_snapshot_in_tx(conn, latest.id)


def import_documents(
    conn: sqlite3.Connection, batches: tuple[Batch, ...], sheets: tuple[Sheet, ...],
    note: str = "",
) -> int:
    with write_tx(conn):
        return _insert_snapshot(
            conn, source="import", note=note, batches=batches, sheets=sheets
        )


def update_revision(
    conn: sqlite3.Connection, base_id: int, code: str, revision: int
) -> int:
    """Clone the latest snapshot with one batch's revision changed.

    Stored sheet revisions are left untouched: sheets keep the revision they
    were issued against, so the reconciliation can tell which ones went stale.
    """
    if revision <= 0:
        raise RevisionError("修订号必须为正整数")

    with write_tx(conn):
        latest = latest_submission(conn)
        if latest is None or latest.id != base_id:
            raise RevisionError(
                "页面上的清单已不是最新提交，请刷新后基于最新状态重试"
            )
        prev = _load_snapshot_in_tx(conn, base_id)
        if code not in prev.batch_by_code:
            raise RevisionError(f"批次“{code}”不在现行清单中")
        if prev.batch_by_code[code].revision == revision:
            raise RevisionError(f"批次“{code}”的现行修订号已是 r{revision}，无需更版")

        batches = tuple(
            Batch(b.code, revision if b.code == code else b.revision)
            for b in prev.batches
        )
        return _insert_snapshot(
            conn,
            source="revision",
            note=f"{code} 修订号改为 r{revision}",
            batches=batches,
            sheets=prev.sheets,
            lineage=prev.lineage,
        )


def list_submissions(conn: sqlite3.Connection, limit: int = 20) -> list[Submission]:
    rows = conn.execute(
        "SELECT id, created_at, source, note FROM submissions ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [Submission(**dict(row)) for row in rows]


def _last_full_import_id(conn: sqlite3.Connection) -> int | None:
    """Newest full-set import; lineage and code history restart from there."""
    row = conn.execute(
        "SELECT MAX(id) AS id FROM submissions WHERE source = 'import'"
    ).fetchone()
    return row["id"]


def _codes_used_since_import(conn: sqlite3.Connection, import_id: int) -> set[str]:
    rows = conn.execute(
        "SELECT DISTINCT code FROM batches WHERE submission_id >= ?",
        (import_id,),
    ).fetchall()
    return {row["code"] for row in rows}


def split_batch(
    conn: sqlite3.Connection,
    base_id: int,
    code: str,
    child_a: str,
    child_b: str,
) -> int:
    """Split one current leaf batch into two children, as a new commit.

    The children inherit the parent's revision; summary sheets are copied
    verbatim and still name the original batch, so their coverage is later
    expanded over the lineage. New codes must be distinct from every batch
    code used since the most recent full-set import. The whole operation is
    one write transaction: any failure rolls back and leaves state untouched.
    """
    child_a = (child_a or "").strip()
    child_b = (child_b or "").strip()
    code = (code or "").strip()
    if not child_a or not child_b:
        raise RevisionError("两个子批编号都必须填写")
    if child_a == child_b:
        raise RevisionError("两个子批编号必须不同")

    with write_tx(conn):
        latest = latest_submission(conn)
        if latest is None or latest.id != base_id:
            raise RevisionError(
                "页面上的清单已不是最新提交，请刷新后基于最新状态重试"
            )
        prev = _load_snapshot_in_tx(conn, base_id)
        if code not in prev.batch_by_code:
            raise RevisionError(f"批次“{code}”不在现行清单中")
        parents = {parent for parent, _ in prev.lineage}
        if code in parents:
            raise RevisionError(
                f"批次“{code}”已经拆过，只能对现行子批继续再拆"
            )
        if len(prev.batches) + 1 > MAX_BATCHES:
            raise RevisionError(
                f"拆批后批次数为 {len(prev.batches) + 1}，超过上限 {MAX_BATCHES}"
            )

        import_id = _last_full_import_id(conn)
        used = _codes_used_since_import(conn, import_id)
        reused = sorted(c for c in (child_a, child_b) if c in used)
        if reused:
            raise RevisionError(
                f"新编号自最近一次整套导入起已被使用：{'、'.join(reused)}"
            )

        parent = prev.batch_by_code[code]
        new_batches: list[Batch] = []
        for batch in prev.batches:
            if batch.code == code:
                new_batches.append(Batch(child_a, parent.revision))
                new_batches.append(Batch(child_b, parent.revision))
            else:
                new_batches.append(batch)
        new_lineage = prev.lineage + ((code, child_a), (code, child_b))
        return _insert_snapshot(
            conn,
            source="revision",
            note=f"拆批：{code} → {child_a}、{child_b}（沿用 {_rev_label(parent.revision)}）",
            batches=tuple(new_batches),
            sheets=prev.sheets,
            lineage=new_lineage,
        )


def _rev_label(revision: int) -> str:
    return f"r{revision}"
