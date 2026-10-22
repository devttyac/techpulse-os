CREATE TABLE schedule_revisions (
    revision INTEGER PRIMARY KEY NOT NULL CHECK (revision > 0),
    cron TEXT NOT NULL,
    timezone_id TEXT NOT NULL,
    rule_bytes BLOB NOT NULL,
    rule_sha256 TEXT NOT NULL,
    rule_provider TEXT NOT NULL,
    effective_from_utc TEXT NOT NULL,
    cursor_utc TEXT NOT NULL,
    history_known INTEGER NOT NULL CHECK (history_known IN (0, 1))
);

CREATE TABLE schedule_slots (
    schedule_revision INTEGER NOT NULL REFERENCES schedule_revisions(revision),
    nominal_local TEXT NOT NULL,
    resolved_utc TEXT NOT NULL,
    disposition TEXT NOT NULL CHECK (disposition IN ('on_time', 'gap', 'fold_first')),
    status TEXT NOT NULL CHECK (status IN ('due', 'missed', 'handled', 'skipped_active', 'superseded')),
    run_id TEXT,
    PRIMARY KEY (schedule_revision, nominal_local)
);

CREATE INDEX schedule_slots_by_status ON schedule_slots(status, resolved_utc);

CREATE TABLE run_claims (
    run_id TEXT PRIMARY KEY NOT NULL,
    nonce TEXT NOT NULL,
    boot_id TEXT NOT NULL,
    process_id INTEGER NOT NULL,
    process_start TEXT NOT NULL,
    context_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'completed', 'failed', 'interrupted', 'cancelled')),
    claimed_utc TEXT NOT NULL,
    finished_utc TEXT
);

CREATE UNIQUE INDEX one_active_run ON run_claims(status) WHERE status = 'active';
