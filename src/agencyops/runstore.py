"""Run registry and approval dispatcher.

Holds paused runs so a human can review staged effects and release them over
HTTP. In-memory here with a JSON trace written to disk; the interface is
narrow enough that swapping in Redis or Postgres is a single-class change.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

from .connectors import ConnectorBundle
from .connectors.base import Effect, run_status
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
    # Serialises decisions on this run; see RunStore.decide.
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

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
        Only proposed effects can be decided. The whole request is validated
        before anything changes, and each run's decisions are serialised, so
        two concurrent approvals cannot dispatch the same effect twice.

        Raises KeyError (no such run), IndexError (index out of range) or
        ValueError (bad decision, already-decided target, nothing pending).
        """
        if decision not in ("approve", "reject"):
            raise ValueError(f"decision must be 'approve' or 'reject', not {decision!r}")
        run = self.get(run_id)
        with run.lock:
            effects = run.effects
            if effect_indexes is None:
                indexes = [i for i, e in enumerate(effects) if e.status == "proposed"]
            else:
                indexes = list(dict.fromkeys(effect_indexes))
                if any(not 0 <= i < len(effects) for i in indexes):
                    raise IndexError("effect_indexes out of range")
                settled = [i for i in indexes if effects[i].status != "proposed"]
                if settled:
                    raise ValueError(f"effect(s) {settled} already decided")
            targets = [effects[i] for i in indexes]
            if not targets:
                raise ValueError("no proposed effects to decide")

            started = time.time()
            if decision == "reject":
                for e in targets:
                    e.status = "rejected"
                dispatched, failed, rejected = 0, 0, len(targets)
                summary = f"rejected {rejected} effect(s)"
            else:
                dispatched, failed = run.bundle.dispatch(targets)
                rejected = 0
                summary = f"approved and dispatched {dispatched} effect(s), {failed} failed"
            run.trace.record(
                "human_approval",
                summary,
                started,
                decision=decision,
                effect_indexes=indexes,
            )
            run.trace.finish(run_status(effects))
            run.trace.persist()
        return {"dispatched": dispatched, "failed": failed, "rejected": rejected}

STORE = RunStore()
