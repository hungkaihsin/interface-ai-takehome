"""Structured run logging: one JSONL file per run, one event per line.

JSONL because the log is meant to be queried ("every run that hit
PERMISSION_DENIED last week"), which prose answers only by grep and luck.

Every value passes through the run's Redactor here -- this is the single point
where log data becomes durable. The run id is shared by the log, its screenshots
and the returned result.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .redaction import Redactor


def new_run_id(prefix: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{prefix}-{stamp}-{uuid.uuid4().hex[:6]}"


class RunLog:
    """Append-only structured log for a single run."""

    def __init__(
        self,
        run_id: str,
        directory: str | Path = "evidence",
        redactor: Redactor | None = None,
        echo: bool = False,
    ) -> None:
        self.run_id = run_id
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / f"{run_id}.jsonl"
        self.redactor = redactor or Redactor()
        self.echo = echo
        self._events: list[dict[str, Any]] = []

    def event(self, type_: str, **payload: Any) -> dict[str, Any]:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "run_id": self.run_id,
            "type": type_,
            **self.redactor.value(payload),
        }
        self._events.append(record)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, default=str) + "\n")
        if self.echo:
            print(f"  [{type_}] " + _summarise(record))
        return record

    @property
    def events(self) -> list[dict[str, Any]]:
        return list(self._events)


def _summarise(record: dict[str, Any]) -> str:
    """A one-line human rendering for console echo."""
    skip = {"ts", "run_id", "type"}
    bits = [f"{k}={v}" for k, v in record.items() if k not in skip]
    line = " ".join(bits)
    return line if len(line) <= 160 else line[:157] + "..."
