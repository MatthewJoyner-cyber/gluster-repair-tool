# Copyright 2026 Matthew Joyner
# SPDX-License-Identifier: GPL-2.0-only
"""Immutable, fsynced execution events and exclusive run ownership."""
from __future__ import annotations

from contextvars import ContextVar
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import threading
import uuid


_active_step = ContextVar("execution_journal_step", default=None)


class ExecutionJournalError(RuntimeError):
    """No further dispatch is safe without inspecting retained evidence."""


def _sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def check_run_reusable(run_dir: str | Path) -> None:
    root = Path(run_dir)
    for previous in (root / "attempts").glob("*"):
        if not previous.is_dir():
            raise ExecutionJournalError(f"unexpected journal entry: {previous}")
        try:
            events = [json.loads(p.read_text()) for p in sorted(previous.glob("*.json"))]
        except (OSError, ValueError) as exc:
            raise ExecutionJournalError(f"unreadable execution journal: {previous}: {exc}") from exc
        if not events or events[-1].get("kind") != "attempt-finish" or events[-1].get("state") != "complete":
            raise ExecutionJournalError(f"unresolved execution attempt: {previous}; inspect possible writes and create a fresh plan before a new run")


class ExecutionJournal:
    def __init__(self, run_dir: str | Path):
        self.root = Path(run_dir)
        self.attempt_id = uuid.uuid4().hex
        self.path = self.root / "attempts" / self.attempt_id
        self._lock = threading.Lock()
        self._sequence = 0
        self._broken = False
        self._handle = None

    def __enter__(self):
        self.root.mkdir(parents=True, exist_ok=True)
        self._handle = (self.root / ".execution.lock").open("a+")
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            check_run_reusable(self.root)
            self.path.mkdir(parents=True, exist_ok=False)
            _sync_directory(self.path.parent)
            _sync_directory(self.root)
            _sync_directory(self.root.parent)
            return self
        except BaseException:
            self._handle.close()
            self._handle = None
            raise

    def __exit__(self, *_args):
        if self._handle is not None:
            self._handle.close()

    def append(self, kind: str, **payload):
        with self._lock:
            if self._broken:
                raise ExecutionJournalError(f"execution journal failed; inspect {self.path}")
            self._sequence += 1
            event = {"schema_version": 1, "attempt_id": self.attempt_id,
                     "sequence": self._sequence, "kind": kind,
                     "recorded_at": datetime.now(timezone.utc).isoformat(), **payload}
            target = self.path / f"{self._sequence:08d}-{kind}.json"
            try:
                # Exclusive creation preserves every earlier event and attempt.
                with target.open("x", encoding="utf-8") as handle:
                    json.dump(event, handle, indent=2)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                _sync_directory(self.path)
            except BaseException:
                self._broken = True
                raise
            return self._sequence

    def execute_step(self, action_id, step, execute):
        step_record = uuid.uuid4().hex
        self.append("step-start", action_id=action_id, step_record=step_record, step=step.to_dict())
        token = _active_step.set((self, action_id, step_record))
        try:
            try:
                result = execute(step)
            except BaseException as exc:
                row = step.to_dict()
                row.update(status="unknown", message=f"{type(exc).__name__}: {exc}")
                self.append("step-finish", action_id=action_id, step_record=step_record, step=row)
                raise
            row = step.to_dict()
            row.update(status=result[0], returncode=result[1], message=result[2])
            self.append("step-finish", action_id=action_id, step_record=step_record, step=row)
            return result
        finally:
            _active_step.reset(token)


def recorded_command(command, execute):
    context = _active_step.get()
    if context is None:
        return execute()
    journal, action_id, step_record = context
    command_record = uuid.uuid4().hex
    fields = dict(action_id=action_id, step_record=step_record, command_record=command_record, command=command)
    journal.append("command-start", **fields)
    try:
        completed = execute()
    except BaseException as exc:
        journal.append("command-finish", **fields, state="unknown", error=f"{type(exc).__name__}: {exc}")
        raise
    journal.append("command-finish", **fields, state="returned", returncode=completed.returncode,
                   stdout=completed.stdout, stderr=completed.stderr)
    return completed
