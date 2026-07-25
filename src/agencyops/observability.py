"""Run tracing.

An agency handing a workflow to a non-technical team needs to answer
"what did the robot do and why" months later. Every node appends a step to
the trace; the trace is persisted per run and exposed over the API.

Deliberately dependency-free - no vendor tracing SDK - so the artefact is a
plain JSON file the agency owns.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

RUNS_DIR = Path(__file__).resolve().parents[2] / "runs"


@dataclass
class TraceStep:
    node: str
    started_at: str
    duration_ms: float
    summary: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class RunTrace:
    workflow: str
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    steps: list[TraceStep] = field(default_factory=list)
    status: str = "running"

    def record(self, node: str, summary: str, started: float, **detail: Any) -> None:
        self.steps.append(
            TraceStep(
                node=node,
                started_at=datetime.fromtimestamp(started, tz=timezone.utc).isoformat(),
                duration_ms=round((time.perf_counter() - started) * 1000, 2)
                if started < 1e9
                else round((time.time() - started) * 1000, 2),
                summary=summary,
                detail=detail,
            )
        )

    def finish(self, status: str = "completed") -> None:
        self.status = status

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "workflow": self.workflow,
            "started_at": self.started_at,
            "status": self.status,
            "steps": [asdict(s) for s in self.steps],
        }

    def persist(self, directory: Path | None = None) -> Path:
        directory = directory or RUNS_DIR
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{self.workflow}-{self.run_id}.json"
        path.write_text(json.dumps(self.to_dict(), indent=2))
        return path

    def render(self) -> str:
        lines = [f"run {self.run_id}  [{self.workflow}]  status={self.status}"]
        for i, s in enumerate(self.steps, 1):
            lines.append(f"  {i:>2}. {s.node:<22} {s.duration_ms:>7.1f}ms  {s.summary}")
        return "\n".join(lines)


class timed:
    """Context manager that records a trace step on exit."""

    def __init__(self, trace: RunTrace, node: str) -> None:
        self.trace = trace
        self.node = node
        self.summary = ""
        self.detail: dict[str, Any] = {}

    def __enter__(self) -> "timed":
        self._start = time.time()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.trace.record(self.node, self.summary or "ok", self._start, **self.detail)
