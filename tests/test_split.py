"""Split-batch persistence and lineage behaviour."""

from __future__ import annotations

import pytest

from app.db import connect, init_db
from app.models import Batch, Sheet
from app.repository import (
    RevisionError,
    import_documents,
    latest_submission,
    list_submissions,
    load_snapshot,
    split_batch,
    update_revision,
)


@pytest.fixture()
def conn(tmp_path):
    connection = connect(str(tmp_path / "repo.sqlite3"))
    init_db(connection)
    yield connection
    connection.close()


def seed(conn):
    return import_documents(
        conn,
        (Batch("B01", 2), Batch("B02", 2)),
        (Sheet("S1", 100, (("B01", 2), ("B02", 2))),),
        note="seed",
    )


def test_split_creates_new_snapshot_with_two_children(conn):
    base = seed(conn)
    new_id = split_batch(conn, base, "B01", "B01A", "B01B")
    assert new_id != base

    old = load_snapshot(conn, base)
    new = load_snapshot(conn, new_id)
    assert [b.code for b in old.batches] == ["B01", "B02"]
    assert [(b.code, b.revision) for b in new.batches] == [
        ("B01A", 2),
        ("B01B", 2),
        ("B02", 2),
    ]
    # Children inherit the parent revision; sheets are copied verbatim.
    assert new.sheets == old.sheets
    assert set(new.lineage) == {("B01", "B01A"), ("B01", "B01B")}
    assert latest_submission(conn).id == new_id


def test_split_is_its_own_commit(conn):
    base = seed(conn)
    split_id = split_batch(conn, base, "B01", "B01A", "B01B")
    submissions = list_submissions(conn)
    assert [s.id for s in submissions] == [split_id, base]
    note = submissions[0].note
    assert "拆批" in note and "B01A" in note and "B01B" in note


def test_split_rejects_stale_base(conn):
    base = seed(conn)
    newer = split_batch(conn, base, "B01", "B01A", "B01B")
    with pytest.raises(RevisionError):
        split_batch(conn, base, "B02", "X1", "X2")
    assert latest_submission(conn).id == newer


def test_split_rejects_unknown_and_already_split_batch(conn):
    base = seed(conn)
    split_id = split_batch(conn, base, "B01", "B01A", "B01B")
    with pytest.raises(RevisionError):
        split_batch(conn, split_id, "B99", "X1", "X2")
    # B01 is now a lineage parent, not a current leaf batch.
    with pytest.raises(RevisionError):
        split_batch(conn, split_id, "B01", "X1", "X2")


def test_split_children_can_split_again(conn):
    base = seed(conn)
    first = split_batch(conn, base, "B01", "B01A", "B01B")
    second = split_batch(conn, first, "B01B", "B01B1", "B01B2")
    snap = load_snapshot(conn, second)
    assert [b.code for b in snap.batches] == ["B01A", "B01B1", "B01B2", "B02"]
    assert set(snap.lineage) == {
        ("B01", "B01A"),
        ("B01", "B01B"),
        ("B01B", "B01B1"),
        ("B01B", "B01B2"),
    }


def test_split_rejects_reused_codes_since_last_import(conn):
    base = seed(conn)
    split_id = split_batch(conn, base, "B01", "B01A", "B01B")
    # B01A was used by the first split; B01 is the original code.
    for children in (("B01A", "X9"), ("X9", "B02"), ("B01", "X9")):
        with pytest.raises(RevisionError):
            split_batch(conn, split_id, "B02", *children)


def test_split_rejects_blank_or_equal_codes(conn):
    base = seed(conn)
    with pytest.raises(RevisionError):
        split_batch(conn, base, "B01", " B01A ", "B01A")
    with pytest.raises(RevisionError):
        split_batch(conn, base, "B01", "  ", "B01B")
    assert latest_submission(conn).id == base  # nothing written


def test_split_enforces_sixteen_batch_limit(conn):
    base = import_documents(
        conn,
        tuple(Batch(f"B{i:02d}", 1) for i in range(15)),
        (),
    )
    split_id = split_batch(conn, base, "B00", "C01", "C02")  # 15 -> 16
    assert len(load_snapshot(conn, split_id).batches) == 16
    with pytest.raises(RevisionError):
        split_batch(conn, split_id, "B01", "D01", "D02")  # would be 17
    # State untouched after the rejected attempt.
    assert latest_submission(conn).id == split_id
    assert len(load_snapshot(conn, split_id).batches) == 16


def test_full_import_resets_code_history_allowing_reuse(conn):
    base = seed(conn)
    split_batch(conn, base, "B01", "B01A", "B01B")
    # A brand new full-set import starts a fresh lineage and history.
    new_base = import_documents(
        conn,
        (Batch("B01", 1),),
        (Sheet("T1", 5, (("B01", 1),)),),
    )
    snap = load_snapshot(conn, new_base)
    assert snap.lineage == ()
    # B01A, used before the import, is available again.
    split_id = split_batch(conn, new_base, "B01", "B01A", "B01C")
    children = {b.code for b in load_snapshot(conn, split_id).batches}
    assert children == {"B01A", "B01C"}


def test_child_revision_keeps_lineage_and_can_be_split_afterwards(conn):
    base = seed(conn)
    split_id = split_batch(conn, base, "B01", "B01A", "B01B")
    rev_id = update_revision(conn, split_id, "B01A", 5)
    snap = load_snapshot(conn, rev_id)
    assert set(snap.lineage) == {("B01", "B01A"), ("B01", "B01B")}
    assert dict((b.code, b.revision) for b in snap.batches)["B01A"] == 5

    again = split_batch(conn, rev_id, "B01B", "B01B1", "B01B2")
    snap2 = load_snapshot(conn, again)
    assert {b.code for b in snap2.batches} == {"B02", "B01A", "B01B1", "B01B2"}


def test_failed_split_writes_nothing(conn):
    base = seed(conn)
    before = len(list_submissions(conn))
    with pytest.raises(RevisionError):
        split_batch(conn, base, "B01", "B02", "X1")  # B02 already used
    assert len(list_submissions(conn)) == before
    assert latest_submission(conn).id == base
