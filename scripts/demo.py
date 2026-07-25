#!/usr/bin/env python3
"""End-to-end demo. No credentials required.

    python scripts/demo.py

Walks both workflows, shows the approval gate holding the writes, then
releases them and shows what would have been sent.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agencyops.config import Settings  # noqa: E402
from agencyops.connectors import build_bundle  # noqa: E402
from agencyops.graphs.client_report import run_report  # noqa: E402
from agencyops.graphs.creative_pipeline import run_creative  # noqa: E402
from agencyops.llm import build_engine  # noqa: E402

RULE = "─" * 78


def banner(text: str) -> None:
    print(f"\n{RULE}\n  {text}\n{RULE}")


def main() -> int:
    settings = Settings(connector_mode="mock", require_human_approval=True)
    engine = build_engine(settings)

    print(f"\nAgencyOps Orchestrator — demo run")
    print(f"connectors: {settings.connector_mode}   llm: {engine.name}   "
          f"approval gate: {'ON' if settings.require_human_approval else 'OFF'}")

    # ---------------------------------------------------------------- 1 ---
    banner("WORKFLOW 1  ·  Weekly client report  ·  Nova Retail")
    bundle = build_bundle(settings)
    report = run_report("nova-retail", bundle=bundle, engine=engine, settings=settings)

    print(report["trace"].render())
    print("\n" + report["report_markdown"])

    banner("Staged effects — nothing has been sent")
    for i, e in enumerate(report["effects"]):
        print(f"  [{i}] {e.status:<9} {e.render()}")
    print(f"\n  Slack calls made so far: {len(bundle.writer('slack').log)}")
    print(f"  Trello calls made so far: {len(bundle.writer('trello').log)}")

    banner("Account lead approves effect 0 only (report yes, action cards no)")
    report["effects"][0].result = bundle.writer("slack").execute(report["effects"][0])
    report["effects"][0].status = "executed"
    for i, e in enumerate(report["effects"]):
        print(f"  [{i}] {e.status:<9} {e.render()}")
    print(f"\n  Slack calls made now: {len(bundle.writer('slack').log)}")
    print(f"  Trello calls made now: {len(bundle.writer('trello').log)}  (still held)")

    # ---------------------------------------------------------------- 2 ---
    banner("WORKFLOW 2  ·  Creative pipeline  ·  Atlas Fitness")
    bundle2 = build_bundle(settings)
    creative = run_creative(
        "atlas-fitness",
        product="Atlas Annual Membership",
        audience="first-time gym joiners in Dubai",
        key_benefit="a coached start, not a cold treadmill",
        cta="Start today",
        variant_count=6,
        bundle=bundle2,
        engine=engine,
        settings=settings,
    )
    print(creative["trace"].render())

    print(f"\n  APPROVED ({len(creative['approved_variants'])})")
    for v in creative["approved_variants"]:
        print(f"    [{v['score']:>3}]  {v['headline']}")
        print(f"           {v['body']}")

    if creative["rejected_variants"]:
        print(f"\n  REJECTED ({len(creative['rejected_variants'])}) — escalated to a human")
        for v in creative["rejected_variants"]:
            print(f"    [{v['score']:>3}]  {v['headline']!r}")
            for viol in v["violations"]:
                print(f"           ↳ {viol}")

    banner("Traces persisted")
    for t in (report["trace"], creative["trace"]):
        print(f"  {t.persist()}")

    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
