-- Read-only examples after a package has been completed with stage 7.

-- Completed packages and their validation decisions.
SELECT p.package_id, p.completed_at, r.run_id, r.project_id, r.status,
       p.files_count, p.total_bytes, p.package_path
FROM packages AS p
JOIN validation_runs AS r ON r.run_id = p.run_id
ORDER BY p.completed_at DESC;

-- The manifest's own fingerprint is kept outside the manifest, in SQL.
SELECT package_id, manifest_sha256
FROM packages
ORDER BY completed_at DESC;

-- Number of completed packages associated with each run, including zero.
SELECT r.run_id, r.status, COUNT(p.package_id) AS completed_packages
FROM validation_runs AS r
LEFT JOIN packages AS p ON p.run_id = r.run_id
GROUP BY r.run_id, r.status
ORDER BY r.finished_at DESC;
