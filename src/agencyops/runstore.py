"""Run registry and approval dispatcher.

Holds paused runs so a human can review staged effects and release them over
HTTP. In-memory here with a JSON trace written to disk; the interface is
narrow enough that swapping in Redis or Postgres is a single-class change.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

from .connectors import ConnectorBundle
from .connectors.base import Effect
from .observability import RunTrace


@dataclass
class StoredRun:
    run_id: str
    workflow: str
    state: dict[str, Any]
    bundle: ConnectorBundle
    trace: RunTrace
    label: str = ""
    artifacts: dict[str, Any] = field(default_factory=dict)

    @property
    def effects(self) -> list[Effect]:
        return self.state.get("effects", [])

    @property
    def pending_approval(self) -> bool:
        return bool(self.state.get("approval_required")) and any(
            e.status == "proposed" for e in self.effects
        )

    def summary(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "workflow": self.workflow,
            "label": self.label,
            "status": self.trace.status,
            "started_at": self.trace.started_at,
            "pending_approval": self.pending_approval,
            "effects": [
                {
                    "index": i,
                    "connector": e.connector,
                    "action": e.action,
                    "summary": e.summary,
                    "reversible": e.reversible,
                    "status": e.status,
                    "result": e.result,
                }
                for i, e in enumerate(self.effects)
            ],
            "errors": self.state.get("errors", []),
        }


class RunStore:
    def __init__(self) -> None:
        self._runs: dict[str, StoredRun] = {}
        self._lock = threading.Lock()

    def add(self, run: StoredRun) -> StoredRun:
        with self._lock:
            self._runs[run.run_id] = run
        run.trace.persist()
        return run

    def get(self, run_id: str) -> StoredRun:
        if run_id not in self._runs:
            raise KeyError(run_id)
        return self._runs[run_id]

    def list(self) -> list[StoredRun]:
        return list(self._runs.values())

    # ------------------------------------------------------------------
    def decide(
        self, run_id: str, decision: str, effect_indexes: list[int] | None = None
    ) -> dict[str, Any]:
        """Approve or reject staged effects, dispatching the approved ones.

        Omitting effect_indexes applies the decision to every proposed effect.
        """
        run = self.get(run_id)
        targets = (
            [run.effects[i] for i in effect_indexes]
            if effect_indexes is not None
            else [e for e in run.effects if e.status == "proposed"]
        )

        if decision == "reject":
            for e in targets:
                e.status = "rejected"
            run.trace.finish("rejected")
            run.trace.persist()
            return {"dispatched": 0, "rejected": len(targets)}

        dispatched, failed = 0, 0
        import time as _time

        started = _time.time()
        for e in targets:
            if e.status not in ("proposed", "approved"):
                continue
            try:
                e.result = run.bundle.writer(e.connector).execute(e)
                e.status = "executed"
                dispatched += 1
            except Exception as exc:  # noqa: BLE001
                e.status = "failed"
                e.result = {"error": str(exc)}
                failed += 1

        run.trace.record(
            "human_approval",
            f"approved and dispatched {dispatched} effect(s), {failed} failed",
            started,
            decision=decision,
        )
        run.trace.finish("completed" if not failed else "completed_with_errors")
        run.trace.persist()
        return {"dispatched": dispatched, "failed": failed, "rejected": 0}


STORE = RunStore()
