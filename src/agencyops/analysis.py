"""Deterministic performance analysis.

Kept out of the LLM on purpose. Arithmetic, thresholds and ranking are exactly
the things a language model should not be doing in a client-facing report -
they must be reproducible and auditable. The LLM writes prose *about* these
numbers; it never computes them.
"""
from __future__ import annotations

from typing import Any

from .connectors.base import AdMetrics, RetainerStatus

# Movement below this is treated as noise, not signal.
MATERIALITY_PCT = 10.0
# Spend floor below which a campaign's percentage swings are not meaningful.
MIN_SPEND_FOR_FINDING = 2000.0


def aggregate(rows: list[AdMetrics]) -> dict[str, Any]:
    spend = sum(r.spend for r in rows)
    revenue = sum(r.revenue for r in rows)
    conversions = sum(r.conversions for r in rows)
    clicks = sum(r.clicks for r in rows)
    impressions = sum(r.impressions for r in rows)
    return {
        "campaign_count": len(rows),
        "spend": round(spend, 2),
        "revenue": round(revenue, 2),
        "conversions": conversions,
        "clicks": clicks,
        "impressions": impressions,
        "roas": round(revenue / spend, 4) if spend else 0.0,
        "cpa": round(spend / conversions, 2) if conversions else 0.0,
        "ctr": round(clicks / impressions * 100, 4) if impressions else 0.0,
        "currency": rows[0].currency if rows else "AED",
    }


def _pct_change(new: float, old: float) -> float:
    if not old:
        return 0.0
    return round((new - old) / old * 100, 2)


def compare(current: list[AdMetrics], previous: list[AdMetrics]) -> dict[str, Any]:
    cur, prev = aggregate(current), aggregate(previous)
    return {
        f"{k}_pct": _pct_change(cur[k], prev[k])
        for k in ("spend", "revenue", "conversions", "roas", "cpa", "ctr")
    }


def find_signals(
    current: list[AdMetrics],
    previous: list[AdMetrics],
    retainer: RetainerStatus | None = None,
) -> list[dict[str, Any]]:
    """Surface material, action-worthy movements. Ranked by revenue impact."""
    prev_by_id = {r.campaign_id: r for r in previous}
    findings: list[dict[str, Any]] = []

    for row in current:
        old = prev_by_id.get(row.campaign_id)
        if old is None:
            findings.append(
                {
                    "type": "new_campaign",
                    "severity": "info",
                    "campaign": row.campaign_name,
                    "headline": f"{row.campaign_name} launched, {row.currency} {row.spend:,.0f} spent at {row.roas:.2f} ROAS",
                    "impact": row.revenue,
                    "recommendation": "Hold for a full week before judging; scale only after CPA stabilises.",
                }
            )
            continue

        if row.spend < MIN_SPEND_FOR_FINDING:
            continue

        roas_delta = _pct_change(row.roas, old.roas)
        cpa_delta = _pct_change(row.cpa, old.cpa)
        revenue_delta = row.revenue - old.revenue

        if roas_delta <= -MATERIALITY_PCT:
            findings.append(
                {
                    "type": "roas_decline",
                    "severity": "high" if roas_delta <= -25 else "medium",
                    "campaign": row.campaign_name,
                    "headline": (
                        f"{row.campaign_name} ROAS fell {abs(roas_delta):.0f}% to "
                        f"{row.roas:.2f} while CPA rose {cpa_delta:.0f}% to "
                        f"{row.currency} {row.cpa:,.0f}"
                    ),
                    "impact": abs(revenue_delta),
                    "recommendation": (
                        "Cap spend at current level and refresh creative before "
                        "further budget; the decline tracks creative fatigue, not audience saturation."
                    ),
                }
            )
        elif roas_delta >= MATERIALITY_PCT:
            findings.append(
                {
                    "type": "roas_improvement",
                    "severity": "info",
                    "campaign": row.campaign_name,
                    "headline": (
                        f"{row.campaign_name} ROAS improved {roas_delta:.0f}% to "
                        f"{row.roas:.2f} on {row.currency} {row.spend:,.0f} spend"
                    ),
                    "impact": abs(revenue_delta),
                    "recommendation": "Increase budget 20% week-on-week while CPA holds.",
                }
            )

        if row.roas < 1.5 and row.spend >= MIN_SPEND_FOR_FINDING:
            findings.append(
                {
                    "type": "unprofitable",
                    "severity": "high",
                    "campaign": row.campaign_name,
                    "headline": (
                        f"{row.campaign_name} is below break-even at {row.roas:.2f} ROAS "
                        f"on {row.currency} {row.spend:,.0f} spend"
                    ),
                    "impact": row.spend,
                    "recommendation": (
                        "Reallocate this budget to the top-performing campaign unless "
                        "it is deliberately funded as an awareness line."
                    ),
                }
            )

    if retainer and retainer.over_budget:
        findings.append(
            {
                "type": "retainer_overrun",
                "severity": "medium",
                "campaign": None,
                "headline": (
                    f"Retainer at {retainer.utilisation:.0f}% - "
                    f"{retainer.used_hours - retainer.contracted_hours:.1f}h over contract"
                ),
                "impact": 0.0,
                "recommendation": "Flag to account lead before the next billing cycle.",
            }
        )

    severity_rank = {"high": 0, "medium": 1, "info": 2}
    findings.sort(key=lambda f: (severity_rank[f["severity"]], -f["impact"]))
    return findings
