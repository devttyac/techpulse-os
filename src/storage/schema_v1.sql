CREATE TABLE artifacts (
    artifact_id TEXT PRIMARY KEY NOT NULL,
    kind TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    byte_length INTEGER NOT NULL CHECK (byte_length >= 0),
    metadata_json TEXT NOT NULL
);

CREATE TABLE episodes (
    episode_id TEXT PRIMARY KEY NOT NULL,
    metadata_json TEXT NOT NULL
);

CREATE TABLE episode_artifacts (
    episode_id TEXT NOT NULL REFERENCES episodes(episode_id),
    artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id),
    position INTEGER NOT NULL CHECK (position >= 0),
    PRIMARY KEY (episode_id, artifact_id),
    UNIQUE (episode_id, position)
);

CREATE TABLE coverage (
    coverage_key TEXT PRIMARY KEY NOT NULL,
    episode_id TEXT NOT NULL REFERENCES episodes(episode_id)
);

CREATE TABLE control_revisions (
    namespace TEXT NOT NULL,
    control_key TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision > 0),
    value BLOB NOT NULL,
    PRIMARY KEY (namespace, control_key)
);

CREATE TABLE operation_journal (
    operation_id TEXT PRIMARY KEY NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'completed', 'interrupted', 'failed')),
    metadata_json TEXT NOT NULL
);
