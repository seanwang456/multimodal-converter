# SQLite First-Start Race Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make concurrent API/worker initialization of one empty SQLite database succeed on the first attempt without changing schema or normal database access.

**Architecture:** Serialize only `SQLModel.metadata.create_all()` with an advisory `fcntl.flock` lock file stored next to the shared SQLite file. Keep both API and worker able to initialize independently, and add Docker Compose readiness ordering so the worker waits for the API health check as defense in depth.

**Tech Stack:** Python 3.11, SQLModel/SQLAlchemy, SQLite, `fcntl` standard library, pytest, Docker Compose

## Global Constraints

- Do not change the existing SQLite schema or add a migration.
- Do not add a third-party dependency; Linux Docker and macOS are the supported runtimes for this lock.
- Preserve startup fail-fast behavior for real `create_all()` errors.
- Lock only initialization; ordinary Sessions and conversion jobs must not acquire the lock.
- Keep API and worker independently startable outside Docker Compose.
- Work only on `codex/scanned-pdf-ocr`; preserve unrelated user files and generated review artifacts.

---

### Task 1: Add a real concurrent initialization regression test

**Files:**
- Create: `apps/api/tests/test_database_init.py`

**Interfaces:**
- Consumes: `app.database.init_db() -> None`, configured at import time by `SQLITE_PATH`.
- Produces: a multiprocessing regression test that fails if any concurrent initializer exits non-zero or if core tables are missing.

- [ ] **Step 1: Write the failing integration test**

```python
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
```

- [ ] **Step 2: Run the test against current production code and verify RED**

Run:

```bash
cd apps/api
.venv/bin/pytest -q tests/test_database_init.py -x
```

Expected: FAIL because at least one subprocess exits with `sqlite3.OperationalError: table ... already exists`. If a timing round happens to pass, repeat once; the three rounds and six callers are intentionally sized from the reproduced race.

- [ ] **Step 3: Commit the regression test while it is demonstrably red**

```bash
git add apps/api/tests/test_database_init.py
git commit -m "test: reproduce concurrent SQLite initialization"
```

---

### Task 2: Serialize SQLite schema initialization

**Files:**
- Modify: `apps/api/app/database.py:1-32`
- Test: `apps/api/tests/test_database_init.py`

**Interfaces:**
- Consumes: `settings.sqlite_path: Path`, `SQLModel.metadata.create_all(engine)`.
- Produces: `_sqlite_init_lock() -> Iterator[None]` and race-safe `init_db() -> None`.

- [ ] **Step 1: Add the minimal standard-library lock context**

```python
from collections.abc import Iterator
from contextlib import contextmanager
import fcntl


@contextmanager
def _sqlite_init_lock() -> Iterator[None]:
    lock_path = settings.sqlite_path.with_name(f"{settings.sqlite_path.name}.init.lock")
    with lock_path.open("a+b") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
```

Update `init_db()` so directory creation and model import happen first, then wrap only `SQLModel.metadata.create_all(engine)`:

```python
def init_db() -> None:
    settings.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    from app import models  # noqa: F401

    with _sqlite_init_lock():
        SQLModel.metadata.create_all(engine)
```

- [ ] **Step 2: Run the focused regression test and verify GREEN**

Run:

```bash
cd apps/api
.venv/bin/pytest -q tests/test_database_init.py
```

Expected: `1 passed`; all 18 subprocesses exit zero and core tables exist.

- [ ] **Step 3: Run existing database/job tests**

Run:

```bash
cd apps/api
.venv/bin/pytest -q tests/test_jobs.py tests/test_files_validation.py tests/test_phase1_fixes.py
```

Expected: all selected tests pass with no startup exceptions.

- [ ] **Step 4: Commit the root-cause fix**

```bash
git add apps/api/app/database.py
git commit -m "fix: serialize SQLite schema initialization"
```

---

### Task 3: Add Docker Compose readiness defense

**Files:**
- Modify: `docker-compose.yml:29-32`

**Interfaces:**
- Consumes: the existing API health check at `GET /api/health`.
- Produces: worker startup dependency `api.condition = service_healthy`.

- [ ] **Step 1: Change the worker dependency condition**

```yaml
    depends_on:
      redis:
        condition: service_healthy
      api:
        condition: service_healthy
```

- [ ] **Step 2: Render and behavior-check the resolved Compose model**

Run:

```bash
docker compose config --no-env-resolution --format json > /tmp/converter-compose-config.json
python3 - <<'PY'
import json
from pathlib import Path

config = json.loads(Path('/tmp/converter-compose-config.json').read_text())
assert config['services']['worker']['depends_on']['api']['condition'] == 'service_healthy'
print('compose worker readiness: ok')
PY
```

Expected: `compose worker readiness: ok`.

- [ ] **Step 3: Commit the deployment ordering change**

```bash
git add docker-compose.yml
git commit -m "fix: wait for API health before worker startup"
```

---

### Task 4: Full verification, Docker empty-volume smoke test, and publication

**Files:**
- Verify: `apps/api/app/database.py`
- Verify: `apps/api/tests/test_database_init.py`
- Verify: `docker-compose.yml`
- Update external artifact: existing GitHub PR #1 description/checklist

**Interfaces:**
- Consumes: committed lock implementation and Compose readiness condition.
- Produces: evidence that code, tests, empty-volume containers, and published branch agree.

- [ ] **Step 1: Run the complete backend suite and compile check**

```bash
cd apps/api
.venv/bin/pytest -q
.venv/bin/python -m compileall -q app tests
```

Expected: all tests pass, with the existing optional skip allowed; compileall exits zero.

- [ ] **Step 2: Run repository hygiene checks**

```bash
git diff --check
git status -sb
```

Expected: no whitespace errors; only intentional commits on `codex/scanned-pdf-ocr`.

- [ ] **Step 3: Build a fresh API image from the feature branch**

```bash
docker build -t converter-api:sqlite-init-race-smoke apps/api
```

Expected: image build exits zero.

- [ ] **Step 4: Start Redis, API, and worker simultaneously on a brand-new named volume**

Use exact isolated names so no existing deployment is touched:

```bash
docker network create converter-sqlite-init-net-20260728
docker volume create converter-sqlite-init-storage-20260728
docker run -d --name converter-sqlite-init-redis-20260728 \
  --network converter-sqlite-init-net-20260728 --network-alias redis redis:7-alpine
docker run -d --name converter-sqlite-init-api-20260728 \
  --network converter-sqlite-init-net-20260728 \
  --env-file /Users/zhexiaow/Desktop/原圈科技/converter/.env \
  -e REDIS_URL=redis://redis:6379/0 -e STORAGE_ROOT=/app/storage \
  -e SQLITE_PATH=/app/storage/app.db \
  -v converter-sqlite-init-storage-20260728:/app/storage \
  -p 127.0.0.1:18083:8000 converter-api:sqlite-init-race-smoke
docker run -d --name converter-sqlite-init-worker-20260728 \
  --network converter-sqlite-init-net-20260728 \
  --env-file /Users/zhexiaow/Desktop/原圈科技/converter/.env \
  -e REDIS_URL=redis://redis:6379/0 -e STORAGE_ROOT=/app/storage \
  -e SQLITE_PATH=/app/storage/app.db \
  -v converter-sqlite-init-storage-20260728:/app/storage \
  converter-api:sqlite-init-race-smoke python -m app.worker
```

- [ ] **Step 5: Verify first-attempt health and absence of schema race errors**

```bash
for attempt in $(seq 1 30); do
  curl -fsS http://127.0.0.1:18083/api/health && break
  sleep 1
done
docker ps --filter name=converter-sqlite-init --format 'table {{.Names}}\t{{.Status}}'
docker logs converter-sqlite-init-api-20260728
docker logs converter-sqlite-init-worker-20260728
```

Expected: API, worker, and Redis remain `Up`; health returns `{"status":"ok"}`; neither log contains `table ... already exists` or `OperationalError`.

- [ ] **Step 6: Clean only the isolated Docker smoke resources**

```bash
docker stop converter-sqlite-init-worker-20260728 converter-sqlite-init-api-20260728 converter-sqlite-init-redis-20260728
docker container rm converter-sqlite-init-worker-20260728 converter-sqlite-init-api-20260728 converter-sqlite-init-redis-20260728
docker network rm converter-sqlite-init-net-20260728
docker volume rm converter-sqlite-init-storage-20260728
docker image rm converter-api:sqlite-init-race-smoke
```

- [ ] **Step 7: Push the branch and update PR #1**

```bash
git push origin codex/scanned-pdf-ocr
gh pr edit 1 --add-label bug
```

Update the PR body with the SQLite race root cause, no-migration compatibility note, focused/full test counts, and Docker empty-volume result. Confirm with:

```bash
gh pr view 1 --json state,isDraft,url,headRefOid
```

Expected: PR remains open on `codex/scanned-pdf-ocr`, with the new commits and verification evidence visible.
