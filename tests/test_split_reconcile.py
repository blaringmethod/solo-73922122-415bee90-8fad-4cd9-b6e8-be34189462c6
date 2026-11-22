"""Reconciliation with lineage expansion after batch splits."""

from __future__ import annotations

from app.models import Batch, Sheet, Snapshot, Submission
from app.reconcile import (
    expanded_sheet_views,
    leaf_descendants,
    reconcile,
)


def make(batches, sheets, lineage=(), submission_id=1):
    return Snapshot(
        Submission(submission_id, "2026-01-01T00:00:00+00:00", "revision", ""),
        tuple(Batch(code, rev) for code, rev in batches),
        tuple(Sheet(code, pages, tuple(sorted(entries))) for code, pages, entries in sheets),
        tuple(lineage),
    )


def split_snapshot():
    # B01 split into B01A/B01B, both still r2; old sheet S1 names B01 r2.
    return make(
        [("B01A", 2), ("B01B", 2), ("B02", 2)],
        [("S1", 100, [("B01", 2), ("B02", 2)])],
        lineage=[("B01", "B01A"), ("B01", "B01B")],
    )


def test_leaf_descendants_walks_the_lineage():
    snap = make(
        [("A1", 1), ("A2", 1), ("B", 1)],
        [],
        lineage=[("P", "A"), ("A", "A1"), ("A", "A2")],
    )
    from app.reconcile import _children_map

    children = _children_map(snap)
    assert leaf_descendants("P", children) == ("A1", "A2")
    assert leaf_descendants("B", children) == ("B",)


def test_sheet_coverage_expands_to_current_leaves():
    views = expanded_sheet_views(split_snapshot())
    assert len(views) == 1
    assert views[0].coverage == {
        "B01": ("B01A", "B01B"),
        "B02": ("B02",),
    }
    assert views[0].is_stale is False


def test_old_sheet_still_covers_all_descendants_and_reports_total():
    snap = split_snapshot()
    result = reconcile(snap, ["B01A", "B01B", "B02"])
    assert result.status == "ok"
    assert result.total == 100
    assert result.usable_sheets == ("S1",)
    assert result.stale_sheets == ()


def test_selecting_one_child_cannot_use_whole_sheet_total():
    snap = split_snapshot()
    result = reconcile(snap, ["B01A"])
    assert result.status == "none"
    # The sheet itself is still valid (revisions match); it simply covers an
    # extra unselected leaf, so it cannot report a number for one child alone.
    assert result.usable_sheets == ("S1",)
    assert result.combination_count == 0


def test_selecting_both_children_but_not_other_batch_is_none():
    snap = split_snapshot()
    result = reconcile(snap, ["B01A", "B01B"])
    assert result.status == "none"  # S1 also covers B02


def test_revising_one_child_makes_old_sheet_stale():
    snap = make(
        [("B01A", 3), ("B01B", 2), ("B02", 2)],
        [("S1", 100, [("B01", 2), ("B02", 2)])],
        lineage=[("B01", "B01A"), ("B01", "B01B")],
    )
    result = reconcile(snap, ["B01A", "B01B", "B02"])
    assert result.status == "none"
    assert result.usable_sheets == ()
    assert len(result.stale_sheets) == 1
    detail = result.stale_sheets[0].details[0]
    assert "B01A" in detail and "r2" in detail


def test_revised_sheet_matching_child_revisions_is_usable():
    snap = make(
        [("B01A", 3), ("B01B", 2), ("B02", 2)],
        [
            ("S1", 100, [("B01", 2), ("B02", 2)]),  # stale
            ("S2", 60, [("B01", 3), ("B02", 2)]),   # cannot match unless all r3
        ],
        lineage=[("B01", "B01A"), ("B01", "B01B")],
    )
    # S2 requires r3 for B01 but B01B is still r2, so it is stale as well.
    result = reconcile(snap, ["B01A", "B01B", "B02"])
    assert result.status == "none"
    assert {s.code for s in result.stale_sheets} == {"S1", "S2"}

    snap2 = make(
        [("B01A", 3), ("B01B", 3), ("B02", 2)],
        [("S2", 60, [("B01", 3), ("B02", 2)])],
        lineage=[("B01", "B01A"), ("B01", "B01B")],
    )
    result = reconcile(snap2, ["B01A", "B01B", "B02"])
    assert result.status == "ok"
    assert result.total == 60


def test_nested_split_expands_transitively():
    snap = make(
        [("B01A", 2), ("B01B1", 2), ("B01B2", 2), ("B02", 2)],
        [("S1", 100, [("B01", 2), ("B02", 2)])],
        lineage=[
            ("B01", "B01A"),
            ("B01", "B01B"),
            ("B01B", "B01B1"),
            ("B01B", "B01B2"),
        ],
    )
    views = expanded_sheet_views(snap)
    assert views[0].coverage == {
        "B01": ("B01A", "B01B1", "B01B2"),
        "B02": ("B02",),
    }
    result = reconcile(snap, ["B01A", "B01B1", "B01B2", "B02"])
    assert result.status == "ok" and result.total == 100

    # One nested leaf revised -> stale.
    snap_rev = make(
        [("B01A", 2), ("B01B1", 4), ("B01B2", 2), ("B02", 2)],
        [("S1", 100, [("B01", 2), ("B02", 2)])],
        lineage=snap.lineage,
    )
    assert reconcile(snap_rev, ["B01A", "B01B1", "B01B2", "B02"]).status == "none"


def test_legacy_snapshot_without_lineage_keeps_old_behaviour():
    snap = make(
        [("B01", 2), ("B02", 2)],
        [("S1", 100, [("B01", 2), ("B02", 2)])],
    )
    views = expanded_sheet_views(snap)
    assert views[0].coverage == {"B01": ("B01",), "B02": ("B02",)}
    result = reconcile(snap, ["B01", "B02"])
    assert result.status == "ok" and result.total == 100


def test_sheet_on_split_leaves_can_be_combined_with_others():
    # S1 covers the two B01 children; S2 covers B02; together they exact-cover.
    snap = make(
        [("B01A", 2), ("B01B", 2), ("B02", 1)],
        [
            ("S1", 70, [("B01", 2)]),
            ("S2", 30, [("B02", 1)]),
        ],
        lineage=[("B01", "B01A"), ("B01", "B01B")],
    )
    result = reconcile(snap, ["B01A", "B01B", "B02"])
    assert result.status == "ok"
    assert result.total == 100
    assert result.witnesses[0].sheets == ("S1", "S2")

    # Picking only one child again cannot take S1.
    assert reconcile(snap, ["B01A", "B02"]).status == "none"
