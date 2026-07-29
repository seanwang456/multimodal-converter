"""SQLite schema initialization must be safe across API/worker processes."""
from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time


API_ROOT = Path(__file__).resolve().parents[1]
INIT_SCRIPT = """
import os
import time
from app.database import init_db

start_at = float(os.environ["INIT_START_AT"])
while time.time() < start_at:
    pass
init_db()
"""


def test_init_db_is_safe_across_processes(tmp_path: Path) -> None:
    failures: list[str] = []
    final_db: Path | None = None

    for round_number in range(3):
        db_path = tmp_path / f"concurrent-{round_number}.db"
        final_db = db_path
        env = os.environ.copy()
        env.update(
            SQLITE_PATH=str(db_path),
            STORAGE_ROOT=str(tmp_path / "storage"),
            INIT_START_AT=str(time.time() + 0.4),
        )
        processes = [
            subprocess.Popen(
                [sys.executable, "-c", INIT_SCRIPT],
                cwd=API_ROOT,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for _ in range(6)
        ]
        for process in processes:
            stdout, stderr = process.communicate(timeout=20)
            if process.returncode != 0:
                failures.append(f"stdout={stdout}\nstderr={stderr}")

    assert not failures, "\n\n".join(failures)
    assert final_db is not None
    with sqlite3.connect(final_db) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert {"file_records", "conversion_jobs"} <= tables
