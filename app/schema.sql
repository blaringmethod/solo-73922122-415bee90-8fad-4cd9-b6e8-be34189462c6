PRAGMA foreign_keys = ON;

-- Every import or revision update is one immutable, committed snapshot.
-- Reconciliation always reads one submission id for both the batch list
-- and the summary sheets, which is what makes "same committed state" true.
CREATE TABLE IF NOT EXISTS submissions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT NOT NULL,
    source      TEXT NOT NULL CHECK (source IN ('import', 'revision')),
    note        TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS batches (
    submission_id INTEGER NOT NULL REFERENCES submissions(id) ON DELETE CASCADE,
    code          TEXT NOT NULL CHECK (length(trim(code)) > 0),
    revision      INTEGER NOT NULL CHECK (revision > 0),
    PRIMARY KEY (submission_id, code)
);

CREATE TABLE IF NOT EXISTS sheets (
    submission_id INTEGER NOT NULL REFERENCES submissions(id) ON DELETE CASCADE,
    code          TEXT NOT NULL CHECK (length(trim(code)) > 0),
    total_pages   INTEGER NOT NULL CHECK (total_pages > 0),
    PRIMARY KEY (submission_id, code)
);

CREATE TABLE IF NOT EXISTS sheet_entries (
    submission_id INTEGER NOT NULL,
    sheet_code    TEXT NOT NULL,
    batch_code    TEXT NOT NULL,
    revision      INTEGER NOT NULL CHECK (revision > 0),
    PRIMARY KEY (submission_id, sheet_code, batch_code),
    FOREIGN KEY (submission_id, sheet_code)
        REFERENCES sheets(submission_id, code) ON DELETE CASCADE
    -- No FK on batch_code: after a split a sheet entry may keep naming the
    -- original batch, which survives only as a batch_lineage parent node.
);

-- Parent/child batch links inside one snapshot. A split clones the snapshot,
-- replaces one leaf batch with two children and extends this table. Summary
-- sheets keep the *original* batch codes and revisions; reconciliation walks
-- this table to expand an original batch into its current leaf descendants.
-- An empty table (legacy snapshots) means every batch is its own leaf root.
CREATE TABLE IF NOT EXISTS batch_lineage (
    submission_id INTEGER NOT NULL REFERENCES submissions(id) ON DELETE CASCADE,
    parent_code   TEXT NOT NULL,
    child_code    TEXT NOT NULL,
    PRIMARY KEY (submission_id, parent_code, child_code)
);

CREATE INDEX IF NOT EXISTS idx_batches_submission
    ON batches(submission_id);
CREATE INDEX IF NOT EXISTS idx_sheets_submission
    ON sheets(submission_id);
CREATE INDEX IF NOT EXISTS idx_entries_submission
    ON sheet_entries(submission_id);
CREATE INDEX IF NOT EXISTS idx_lineage_submission
    ON batch_lineage(submission_id);

-- Hard limits from the acceptance rules, enforced even if code is bypassed.
CREATE TRIGGER IF NOT EXISTS trg_batches_limit
BEFORE INSERT ON batches
WHEN (SELECT COUNT(*) FROM batches WHERE submission_id = NEW.submission_id) >= 16
BEGIN
    SELECT RAISE(ABORT, 'a submission may contain at most 16 batches');
END;

CREATE TRIGGER IF NOT EXISTS trg_sheets_limit
BEFORE INSERT ON sheets
WHEN (SELECT COUNT(*) FROM sheets WHERE submission_id = NEW.submission_id) >= 40
BEGIN
    SELECT RAISE(ABORT, 'a submission may contain at most 40 sheets');
END;
