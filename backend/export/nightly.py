"""
nightly.py — Nightly full export to CSV + JSON, committed to a shared private Git repo.

This is the 20-year survival strategy. If the app dies tomorrow, the data
survives in a human-readable, app-independent format that all co-owners
have locally cloned.

The export covers every table in `backend/db/schema.sql` and produces both
formats per table:
  - CSV  : easy diff in PR review, easy import into spreadsheets
  - JSON : preserves nested types (JSONB metadata), easier programmatic re-ingest

The cron schedule and Git remote are configured via env vars in
`backend/.env.example` (EXPORT_GIT_REPO_PATH, EXPORT_GIT_REMOTE).

Implemented in Session 8.
"""

from __future__ import annotations

from pathlib import Path


async def export_all_tables(
    db: object,
    output_dir: Path,
) -> list[Path]:
    """
    Dump every table to `output_dir` as `<table>.csv` and `<table>.json`.

    Returns the list of files written.

    TODO Session 8: implement.
        - Iterate the table list from schema.sql (or pg_class).
        - For each table:
            * SELECT * FROM <table> ORDER BY created_at, id  (deterministic order)
            * Write <output_dir>/<table>.csv   (UTF-8, RFC4180)
            * Write <output_dir>/<table>.json  (JSON Lines, one row per line)
        - Verify event-row count matches a checksum computed independently.
    """
    raise NotImplementedError("TODO Session 8: implement nightly table export")


def commit_and_push_export(
    repo_path: Path,
    output_dir: Path,
) -> None:
    """
    Stage `output_dir` inside `repo_path`, commit with a deterministic
    message, and push to the configured remote.

    TODO Session 8: implement.
        - Move/copy `output_dir` contents into `repo_path/exports/<YYYY-MM-DD>/`.
        - `git -C <repo_path> add ...`
        - `git -C <repo_path> commit -m "Nightly export: <YYYY-MM-DD>"`
        - `git -C <repo_path> push origin main`
        - On any failure, write a `LAST_EXPORT_FAILED` marker file so the
          dashboard surface can flag it.
    """
    raise NotImplementedError("TODO Session 8: implement git commit + push")


async def run_nightly_export(
    db: object,
    config: dict | None = None,
) -> None:
    """
    Orchestrator called by cron / a scheduled task runner.

    Reads the EXPORT_GIT_REPO_PATH and related env vars, calls
    `export_all_tables` into a temp directory under that repo, then
    `commit_and_push_export`. Logs success / failure for the dashboard.

    TODO Session 8: implement.
    """
    raise NotImplementedError("TODO Session 8: implement nightly export orchestration")
