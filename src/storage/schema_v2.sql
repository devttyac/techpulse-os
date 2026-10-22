CREATE TABLE operation_events (
    event_id TEXT PRIMARY KEY NOT NULL,
    operation_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    phase TEXT NOT NULL CHECK (phase IN ('intent', 'result', 'reconciliation')),
    component TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    fields_json TEXT NOT NULL,
    context_json TEXT NOT NULL
);

CREATE INDEX operation_events_by_operation
    ON operation_events (operation_id, observed_at, event_id);

CREATE TABLE effect_intents (
    operation_id TEXT PRIMARY KEY NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'resolved')),
    effect_status TEXT NOT NULL,
    intent_event_id TEXT NOT NULL REFERENCES operation_events(event_id),
    resolution_event_id TEXT REFERENCES operation_events(event_id)
);

CREATE TABLE diagnostic_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT UNIQUE NOT NULL,
    observed_at TEXT NOT NULL,
    event_json TEXT NOT NULL,
    byte_length INTEGER NOT NULL CHECK (byte_length >= 0)
);
