"""
job_store.py
------------
Thread-safe in-memory job state with TTL-based cleanup.
"""

import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

JOB_TTL_SECONDS = 30 * 60   # 30 minutes
CLEANUP_INTERVAL = 5 * 60   # run cleanup every 5 minutes


@dataclass
class Job:
    job_id: str
    status: str = "pending"       # pending | running | done | error
    progress: float = 0.0         # 0–100
    message: str = ""
    output_path: Optional[str] = None
    downloaded: bool = False
    created_at: float = field(default_factory=time.time)


class JobStore:
    def __init__(self):
        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}
        self._start_cleanup_thread()

    # ── Public API ─────────────────────────────────────────────────────────

    def create(self) -> Job:
        job = Job(job_id=str(uuid.uuid4()))
        with self._lock:
            self._jobs[job.job_id] = job
        return job

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def update(self, job_id: str, **kwargs) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job:
                for k, v in kwargs.items():
                    setattr(job, k, v)

    def mark_downloaded(self, job_id: str) -> None:
        self.update(job_id, downloaded=True)

    # ── Cleanup ────────────────────────────────────────────────────────────

    def _start_cleanup_thread(self):
        t = threading.Thread(target=self._cleanup_loop, daemon=True)
        t.start()

    def _cleanup_loop(self):
        while True:
            time.sleep(CLEANUP_INTERVAL)
            self._cleanup()

    def _cleanup(self):
        now = time.time()
        to_delete = []
        with self._lock:
            for job_id, job in self._jobs.items():
                age = now - job.created_at
                expired = age > JOB_TTL_SECONDS
                # also clean up downloaded jobs after 5 minutes
                downloaded_old = job.downloaded and age > 5 * 60
                if expired or downloaded_old:
                    to_delete.append(job_id)

        for job_id in to_delete:
            with self._lock:
                job = self._jobs.pop(job_id, None)
            if job and job.output_path:
                try:
                    Path(job.output_path).unlink(missing_ok=True)
                except OSError:
                    pass


# Singleton used across the app
store = JobStore()
