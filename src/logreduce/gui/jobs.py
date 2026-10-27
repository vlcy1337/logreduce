from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


_TERMINAL_STATES = {"succeeded", "failed", "canceled"}
_MAX_EVENTS = 500


class JobBusyError(RuntimeError):
    """Raised when another GUI job is already active."""


@dataclass
class Job:
    job_id: str
    log_dir: str
    config_path: str
    workers: int | None
    force: bool
    dry_run: bool
    state: str = "starting"
    phase: str = "starting"
    completed_tasks: int = 0
    total_tasks: int = 0
    input_bytes: int = 0
    output_bytes: int = 0
    error: str | None = None
    error_kind: str | None = None
    exit_code: int | None = None
    summary: dict[str, Any] | None = None
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    cancel_requested: bool = False
    process: subprocess.Popen[str] | None = field(default=None, repr=False)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "id": self.job_id,
                "state": self.state,
                "phase": self.phase,
                "dry_run": self.dry_run,
                "force": self.force,
                "workers": self.workers,
                "log_dir": self.log_dir,
                "config_path": self.config_path,
                "completed_tasks": self.completed_tasks,
                "total_tasks": self.total_tasks,
                "input_bytes": self.input_bytes,
                "output_bytes": self.output_bytes,
                "error": self.error,
                "error_kind": self.error_kind,
                "exit_code": self.exit_code,
                "summary": self.summary,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "events": list(self.events),
            }


class JobManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}
        self._active: Job | None = None

    def start(
        self,
        *,
        log_dir: str,
        config_path: str,
        workers: int | None,
        force: bool,
        dry_run: bool,
    ) -> Job:
        with self._lock:
            if self._active is not None and self._active.state not in _TERMINAL_STATES:
                raise JobBusyError("another LogReduce GUI job is already running")
            self._discard_old_jobs()
            job = Job(
                job_id=uuid.uuid4().hex,
                log_dir=log_dir,
                config_path=config_path,
                workers=workers,
                force=force,
                dry_run=dry_run,
            )
            self._jobs[job.job_id] = job
            self._active = job

            command = [
                sys.executable,
                "-m",
                "logreduce.gui.worker",
                "--log-dir",
                log_dir,
                "--config",
                config_path,
            ]
            if workers is not None:
                command.extend(["--workers", str(workers)])
            if force:
                command.append("--force")
            if dry_run:
                command.append("--dry-run")

            environment = os.environ.copy()
            source_root = Path(__file__).resolve().parents[2]
            python_path = environment.get("PYTHONPATH")
            environment["PYTHONPATH"] = (
                str(source_root)
                if not python_path
                else os.pathsep.join((str(source_root), python_path))
            )
            try:
                job.process = subprocess.Popen(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    start_new_session=True,
                    env=environment,
                )
            except OSError as exc:
                job.state = "failed"
                job.phase = "failed"
                job.error = str(exc)
                job.error_kind = "startup"
                job.exit_code = 1
                job.finished_at = time.time()
                self._active = None
                raise RuntimeError(f"cannot start processing worker: {exc}") from exc
            job.state = "running"
            job.phase = "starting"

        threading.Thread(
            target=self._monitor,
            args=(job,),
            name=f"logreduce-job-{job.job_id[:8]}",
            daemon=True,
        ).start()
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def cancel(self, job_id: str) -> Job | None:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            return None
        with job.lock:
            if job.state in _TERMINAL_STATES:
                return job
            process = job.process
            if process is None or process.poll() is not None:
                return job
            job.cancel_requested = True
            job.state = "canceling"
            job.phase = "canceling"
            _append_event_locked(
                job,
                {
                    "message": "已请求取消任务，正在等待 worker 退出。",
                    "phase": "canceling",
                    "timestamp": time.time(),
                },
            )
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        return job

    def shutdown(self) -> None:
        with self._lock:
            active = self._active
        if active is not None:
            self.cancel(active.job_id)

    def _monitor(self, job: Job) -> None:
        process = job.process
        if process is None or process.stdout is None:
            self._finish(job, 1, "worker process was not created")
            return

        try:
            for line in process.stdout:
                text = line.rstrip("\r\n")
                if not text:
                    continue
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError:
                    _append_event(job, text, phase=job.phase)
                    continue
                if isinstance(payload, dict):
                    self._handle_payload(job, payload)
        finally:
            process.stdout.close()

        return_code = process.wait()
        with job.lock:
            failed = job.error is not None
            succeeded = job.summary is not None
            canceled = job.cancel_requested
        if canceled:
            self._finish(job, return_code, "任务已取消。", state="canceled")
        elif return_code == 0 and succeeded and not failed:
            self._finish(job, return_code, state="succeeded")
        elif not failed:
            self._finish(
                job,
                return_code,
                f"worker 异常退出，退出码为 {return_code}。",
            )
        else:
            self._finish(job, return_code)

    def _handle_payload(self, job: Job, payload: dict[str, Any]) -> None:
        payload_type = payload.get("type")
        if payload_type == "progress":
            phase = str(payload.get("phase", "processing"))
            message = str(payload.get("message", ""))
            with job.lock:
                job.phase = phase
                job.completed_tasks = _integer(payload.get("completed_tasks"))
                job.total_tasks = _integer(payload.get("total_tasks"))
                job.input_bytes = _integer(payload.get("input_bytes"))
                job.output_bytes = _integer(payload.get("output_bytes"))
            _append_event(job, message, phase=phase)
        elif payload_type == "result":
            with job.lock:
                job.summary = {
                    key: value
                    for key, value in payload.items()
                    if key != "type"
                }
                job.input_bytes = _integer(payload.get("input_bytes"))
                job.output_bytes = _integer(payload.get("output_bytes"))
                job.completed_tasks = _integer(payload.get("task_count"))
                job.total_tasks = _integer(payload.get("task_count"))
        elif payload_type == "error":
            with job.lock:
                job.error = str(payload.get("message", "unknown worker error"))
                job.error_kind = str(payload.get("kind", "processing"))
                job.exit_code = _integer(payload.get("exit_code")) or 1
                job.phase = "failed"
            _append_event(job, job.error, phase="failed")
        else:
            _append_event(job, json.dumps(payload, ensure_ascii=False), phase=job.phase)

    def _finish(
        self,
        job: Job,
        return_code: int,
        error: str | None = None,
        *,
        state: str = "failed",
    ) -> None:
        with job.lock:
            if job.finished_at is not None:
                return
            job.state = state
            job.phase = state
            job.exit_code = return_code
            job.finished_at = time.time()
            if error is not None and job.error is None:
                job.error = error
                job.error_kind = "worker"
            if state == "succeeded":
                job.error = None
                job.error_kind = None
            _append_event_locked(
                job,
                {
                    "message": (
                        "任务完成。"
                        if state == "succeeded"
                        else error or job.error or "任务失败。"
                    ),
                    "phase": state,
                    "timestamp": time.time(),
                },
            )
        with self._lock:
            if self._active is job:
                self._active = None

    def _discard_old_jobs(self) -> None:
        if len(self._jobs) < 10:
            return
        terminal = [
            job
            for job in self._jobs.values()
            if job.state in _TERMINAL_STATES
        ]
        remove_count = max(1, len(self._jobs) - 9)
        for job in sorted(terminal, key=lambda item: item.started_at)[:remove_count]:
            self._jobs.pop(job.job_id, None)


def _append_event(job: Job, message: str, *, phase: str) -> None:
    with job.lock:
        _append_event_locked(
            job,
            {
                "message": message,
                "phase": phase,
                "timestamp": time.time(),
            },
        )


def _append_event_locked(job: Job, event: dict[str, Any]) -> None:
    job.events.append(event)
    if len(job.events) > _MAX_EVENTS:
        del job.events[: len(job.events) - _MAX_EVENTS]


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
