CREATE TABLE lifecycle_episode_status (
    episode_id TEXT PRIMARY KEY NOT NULL REFERENCES episodes(episode_id),
    status TEXT NOT NULL CHECK (status IN ('retired'))
);

CREATE TABLE lifecycle_leases (
    lease_id TEXT PRIMARY KEY NOT NULL,
    artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id),
    acquired_at TEXT NOT NULL
);

CREATE TABLE lifecycle_tombstones (
    operation_id TEXT PRIMARY KEY NOT NULL REFERENCES effect_intents(operation_id),
    episode_id TEXT NOT NULL REFERENCES episodes(episode_id),
    artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id),
    content_sha256 TEXT NOT NULL,
    byte_length INTEGER NOT NULL,
    file_device INTEGER NOT NULL,
    file_inode INTEGER NOT NULL,
    file_mtime_ns INTEGER NOT NULL,
    safe_path_digest TEXT NOT NULL,
    prior_reference_count INTEGER NOT NULL,
    prior_lease_count INTEGER NOT NULL,
    intended_effect TEXT NOT NULL CHECK (intended_effect = 'unlink_owned_main_audio')
);
