-- Run these read-only examples against outputs/biodataqc.sqlite.

-- 1. Number of historical runs per status.
SELECT status, COUNT(*) AS runs
FROM validation_runs
GROUP BY status;

-- 2. Most recently stored runs for the demo project.
SELECT run_id, finished_at, dataset_path, status, blocking_findings, warnings
FROM validation_runs
WHERE project_id = 'DEMO_001'
ORDER BY stored_at DESC, rowid DESC
LIMIT 10;

-- 3. Root findings from the most recently stored run.
SELECT rule_id, outcome, severity, sample_id, file_path, record_number, message, next_action
FROM issues
WHERE run_id = (
    SELECT run_id FROM validation_runs
    ORDER BY stored_at DESC, rowid DESC LIMIT 1
)
ORDER BY position;

-- 4. Required checks that could not run, with their dependencies.
SELECT run_id, check_id, message, blocked_by_json
FROM checks
WHERE required = 1 AND outcome = 'NOT_RUN'
ORDER BY run_id, position;

-- 5. The inventory and fingerprints from the latest run.
SELECT relative_path, inventory_status, size_bytes, sha256
FROM file_snapshots
WHERE run_id = (
    SELECT run_id FROM validation_runs
    ORDER BY stored_at DESC, rowid DESC LIMIT 1
)
ORDER BY relative_path;

-- 6. Exact saved metadata values; duplicate IDs remain separate CSV records.
SELECT csv_record, sample_id, values_json
FROM metadata_rows
WHERE run_id = (
    SELECT run_id FROM validation_runs
    ORDER BY stored_at DESC, rowid DESC LIMIT 1
)
ORDER BY csv_record;
