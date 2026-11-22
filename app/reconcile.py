"""Exact-cover reconciliation of page-count summary sheets.

Rules implemented here, in one place so the web layer cannot drift:

* Sheets keep the batch codes and revisions they were issued against. After a
  batch split, an original batch code is expanded along ``snapshot.lineage``
  into its current leaf descendants, so an old sheet's coverage automatically
  follows every descendant of the batches it names.
* A sheet is usable only when, for every recorded batch, *all* of its current
  leaf descendants carry the revision recorded on the sheet. If one child has
  been revised separately, the whole sheet goes stale.
* A combination of sheets is valid for the checked batches when every checked
  batch is counted exactly once and no unchecked batch appears. Because a
  split expands one sheet entry into several leaves, selecting only one child
  can never be covered by a sheet that was written for the parent: the whole
  sheet total can never be reported for just one child.
* If every valid combination has the same page total, that total is reported.
* If valid combinations disagree, two witness combinations with different
  totals are returned.
* If there is no valid combination, no number can be produced.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .models import Batch, Sheet, Snapshot


@dataclass(frozen=True)
class Witness:
    sheets: tuple[str, ...]
    total: int


@dataclass(frozen=True)
class StaleSheet:
    code: str
    details: tuple[str, ...]


@dataclass(frozen=True)
class SheetView:
    """A sheet after lineage expansion for display and reconciliation."""

    sheet: Sheet
    # Original batch code -> sorted current leaf codes it now covers.
    coverage: dict[str, tuple[str, ...]]
    # Human-readable reasons the sheet cannot be used; empty means usable.
    stale_details: tuple[str, ...]

    @property
    def is_stale(self) -> bool:
        return bool(self.stale_details)


@dataclass(frozen=True)
class CoverResult:
    status: str  # "ok" | "conflict" | "none"
    total: int | None
    witnesses: tuple[Witness, ...]
    combination_count: int
    usable_sheets: tuple[str, ...]
    stale_sheets: tuple[StaleSheet, ...]


def _children_map(snapshot: Snapshot) -> dict[str, tuple[str, ...]]:
    children: dict[str, list[str]] = {}
    for parent, child in snapshot.lineage:
        children.setdefault(parent, []).append(child)
    return {parent: tuple(sorted(kids)) for parent, kids in children.items()}


def leaf_descendants(
    code: str, children: dict[str, tuple[str, ...]]
) -> tuple[str, ...]:
    """Current leaf batches under ``code`` (itself when it was never split)."""
    if code not in children:
        return (code,)
    leaves: list[str] = []
    stack = list(children[code])
    while stack:
        node = stack.pop()
        kids = children.get(node)
        if kids:
            stack.extend(kids)
        else:
            leaves.append(node)
    return tuple(sorted(leaves))


def expanded_sheet_views(snapshot: Snapshot) -> tuple[SheetView, ...]:
    """Expand every sheet's original batches along the lineage.

    Missing current batches are reported as stale rather than raised: an
    import replaces the whole state, and sheets from the new snapshot always
    name batches of that snapshot, so this is the defensive branch.
    """
    children = _children_map(snapshot)
    current = snapshot.batch_by_code
    views: list[SheetView] = []
    for sheet in snapshot.sheets:
        coverage: dict[str, tuple[str, ...]] = {}
        details: list[str] = []
        for original, recorded_revision in sheet.entries:
            leaves = leaf_descendants(original, children)
            coverage[original] = leaves
            if not leaves or all(leaf not in current for leaf in leaves):
                details.append(f"{original} 不在现行批次清单")
                continue
            mismatched = sorted(
                leaf
                for leaf in leaves
                if leaf in current
                and current[leaf].revision != recorded_revision
            )
            if mismatched:
                shown = "、".join(mismatched)
                if tuple(mismatched) == leaves and len(leaves) == 1 and leaves[0] == original:
                    details.append(
                        f"{original} 单上修订 r{recorded_revision} ≠ "
                        f"现行 r{current[original].revision}"
                    )
                else:
                    details.append(
                        f"{original} 现覆盖 {'、'.join(leaves)}，其中 {shown} 的现行"
                        f"修订与单上 r{recorded_revision} 不一致"
                    )
        views.append(SheetView(sheet, coverage, tuple(details)))
    return tuple(views)


def stale_sheets_for(snapshot: Snapshot) -> tuple[StaleSheet, ...]:
    """Sheets whose expanded revisions do not all match the snapshot."""
    return tuple(
        StaleSheet(view.sheet.code, view.stale_details)
        for view in expanded_sheet_views(snapshot)
        if view.is_stale
    )


def _exact_covers(
    selected: tuple[str, ...],
    candidates: list[tuple[int, Sheet]],
) -> tuple[int, list[Witness]]:
    """Enumerate exact covers restricted to the selected batch set.

    Bitmask DP over at most 16 selected batches (2^16 states). Each state
    keeps a minimum-total and a maximum-total witness. Summing a constant
    sheet total preserves ordering, so these bounds are exact for the full
    state, and ``min != max`` is exactly the existence of a disagreement.
    Cover counting is done separately and exactly.
    """
    index = {code: i for i, code in enumerate(selected)}
    selected_mask = (1 << len(selected)) - 1

    by_lowest: list[list[tuple[int, Sheet]]] = [[] for _ in range(len(selected))]
    for mask, sheet in candidates:
        lowest = (mask & -mask).bit_length() - 1
        by_lowest[lowest].append((mask, sheet))

    # lo/hi[mask] = (total, witness) with the smallest/largest achievable total.
    lo: list[tuple[int, tuple[str, ...]] | None] = [None] * (selected_mask + 1)
    hi: list[tuple[int, tuple[str, ...]] | None] = [None] * (selected_mask + 1)
    counts = [0] * (selected_mask + 1)
    counts[0] = 1

    for mask in range(1, selected_mask + 1):
        lowest = (mask & -mask).bit_length() - 1
        best_lo: tuple[int, tuple[str, ...]] | None = None
        best_hi: tuple[int, tuple[str, ...]] | None = None
        for sheet_mask, sheet in by_lowest[lowest]:
            if sheet_mask & ~mask:
                continue
            rest = mask ^ sheet_mask
            counts[mask] += counts[rest]
            if rest == 0:
                pairs = [(sheet.total_pages, (sheet.code,))]
            else:
                if lo[rest] is None or hi[rest] is None:
                    continue
                pairs = [
                    (lo[rest][0] + sheet.total_pages, lo[rest][1] + (sheet.code,)),
                    (hi[rest][0] + sheet.total_pages, hi[rest][1] + (sheet.code,)),
                ]
            for total, witness in pairs:
                if best_lo is None or total < best_lo[0]:
                    best_lo = (total, witness)
                if best_hi is None or total > best_hi[0]:
                    best_hi = (total, witness)
        lo[mask] = best_lo
        hi[mask] = best_hi

    count = counts[selected_mask]
    if count == 0 or lo[selected_mask] is None or hi[selected_mask] is None:
        return count, []

    low_total, low_witness = lo[selected_mask]
    high_total, high_witness = hi[selected_mask]
    if low_total == high_total:
        return count, [Witness(tuple(sorted(low_witness)), low_total)]
    return count, [
        Witness(tuple(sorted(low_witness)), low_total),
        Witness(tuple(sorted(high_witness)), high_total),
    ]


def reconcile(snapshot: Snapshot, selected_codes: Iterable[str]) -> CoverResult:
    current = snapshot.batch_by_code
    selected_seen: list[str] = []
    for code in selected_codes:
        if code in current and code not in selected_seen:
            selected_seen.append(code)
    selected = tuple(sorted(selected_seen, key=lambda c: (c.casefold(), c)))

    requested = tuple(sorted(set(selected_codes)))
    ignored = tuple(sorted(code for code in requested if code not in current))

    views = expanded_sheet_views(snapshot)
    stale = tuple(
        StaleSheet(view.sheet.code, view.stale_details)
        for view in views
        if view.is_stale
    )
    stale_codes = {item.code for item in stale}
    usable_views = [view for view in views if not view.is_stale]
    usable_codes = tuple(view.sheet.code for view in usable_views)

    if not selected or ignored:
        return CoverResult("none", None, (), 0, usable_codes, stale)

    # A usable sheet is represented by the leaf set it now covers. Any leaf
    # outside the checked set (including a sibling child left unselected)
    # disqualifies the whole sheet, which is what forbids reporting a whole
    # sheet total for a single child batch.
    index = {code: i for i, code in enumerate(selected)}
    candidates: list[tuple[int, Sheet]] = []
    for view in usable_views:
        mask = 0
        fits = True
        seen: set[int] = set()
        for leaves in view.coverage.values():
            for leaf in leaves:
                bit = index.get(leaf)
                if bit is None or bit in seen:  # extra/duplicate leaf
                    fits = False
                    break
                seen.add(bit)
                mask |= 1 << bit
            if not fits:
                break
        if fits and mask:
            candidates.append((mask, view.sheet))

    count, witnesses = _exact_covers(selected, candidates)
    if count == 0 or not witnesses:
        return CoverResult("none", None, (), count, usable_codes, stale)
    if len(witnesses) == 1:
        return CoverResult(
            "ok", witnesses[0].total, tuple(witnesses), count,
            usable_codes, stale,
        )
    return CoverResult(
        "conflict", None, tuple(witnesses), count, usable_codes, stale
    )
