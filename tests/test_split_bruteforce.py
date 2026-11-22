"""Differential test with lineage: expanded DP vs. brute-force enumeration."""

from __future__ import annotations

import random

from app.models import Batch, Sheet, Snapshot, Submission
from app.reconcile import expanded_sheet_views, reconcile


def brute_force(snapshot, selected):
    current = {b.code: b.revision for b in snapshot.batches}
    views = expanded_sheet_views(snapshot)
    usable = []
    for view in views:
        if view.is_stale:
            continue
        leaves = tuple(
            leaf for leaves in view.coverage.values() for leaf in leaves
        )
        usable.append((view.sheet, leaves))

    wanted = set(selected)
    if not wanted or any(code not in current for code in selected):
        return "none", None, 0
    totals = []
    for mask in range(1 << len(usable)):
        chosen = [usable[i] for i in range(len(usable)) if mask >> i & 1]
        leaves = [leaf for _, sheet_leaves in chosen for leaf in sheet_leaves]
        if len(leaves) != len(set(leaves)) or set(leaves) != wanted:
            continue
        totals.append(sum(sheet.total_pages for sheet, _ in chosen))
    if not totals:
        return "none", None, len(totals)
    if len(set(totals)) == 1:
        return "ok", totals[0], len(totals)
    return "conflict", None, len(totals)


def random_split_case(rng):
    # Build a small forest: roots may be split once into two leaves.
    root_count = rng.randint(1, 4)
    roots = [f"R{i}" for i in range(root_count)]
    batches: list[tuple[str, int]] = []
    lineage: list[tuple[str, str]] = []
    leaves_by_root: dict[str, list[str]] = {}
    for root in roots:
        revision = rng.choice([1, 2])
        if rng.random() < 0.6:
            kids = [f"{root}a", f"{root}b"]
            lineage.extend(((root, kids[0]), (root, kids[1])))
            for kid in kids:
                # Child may already have been revised independently.
                batches.append((kid, revision + (1 if rng.random() < 0.3 else 0)))
            leaves_by_root[root] = kids
        else:
            batches.append((root, revision))
            leaves_by_root[root] = [root]

    sheets = []
    batch_rev = dict(batches)
    for index in range(rng.randint(0, 7)):
        picked_roots = rng.sample(roots, rng.randint(1, min(2, len(roots))))
        entries = []
        for root in picked_roots:
            # Record the original root revision; sometimes mismatch on purpose.
            base_rev = batch_rev[leaves_by_root[root][0]]
            entries.append((root, base_rev if rng.random() < 0.75 else base_rev + 1))
        sheets.append(
            Sheet(f"S{index}", rng.randint(1, 100), tuple(sorted(entries)))
        )
    snapshot = Snapshot(
        Submission(1, "x", "revision", ""),
        tuple(Batch(code, rev) for code, rev in batches),
        tuple(sheets),
        tuple(lineage),
    )
    leaf_codes = [code for code, _ in batches]
    selected = rng.sample(leaf_codes, rng.randint(1, len(leaf_codes)))
    return snapshot, selected


def test_split_dp_matches_brute_force_on_random_cases():
    rng = random.Random(20261122)
    for trial in range(400):
        snapshot, selected = random_split_case(rng)
        expected_status, expected_total, expected_count = brute_force(
            snapshot, selected
        )
        result = reconcile(snapshot, selected)
        assert result.status == expected_status, f"trial {trial}"
        assert result.combination_count == expected_count, f"trial {trial}"
        if expected_status == "ok":
            assert result.total == expected_total, f"trial {trial}"
        if expected_status == "conflict":
            assert len(result.witnesses) == 2
            assert result.witnesses[0].total < result.witnesses[1].total
