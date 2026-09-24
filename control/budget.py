"""Metered spend accounting and threshold policy.

Only incremental metered OpenRouter spend (Jev + companion) counts against the
configured ceiling. Subscription usage for Claude, Codex and Grok is recorded
separately and never inferred as a dollar cost.

Reported provider cost is `actual`. Anything we compute ourselves is `estimated`
and is never allowed to masquerade as actual.
"""

from __future__ import annotations

THRESHOLDS: tuple[tuple[int, str], ...] = (
    (50, "ATTENTION"),
    (75, "RESTRICT_ESCALATION"),
    (90, "RELEASE_CRITICAL_ONLY"),
    (100, "HARD_STOP"),
)

METERED_PROVIDERS = frozenset({"openrouter"})


def ensure(doc: dict, total_usd: float) -> None:
    bucket = doc.setdefault(
        "budget",
        {"total_usd": 0.0, "spent_usd": 0.0, "estimated_usd": 0.0,
         "thresholds_crossed": [], "hard_stop": False},
    )
    bucket["total_usd"] = float(total_usd)
    bucket.setdefault("spent_usd", 0.0)
    bucket.setdefault("estimated_usd", 0.0)
    bucket.setdefault("thresholds_crossed", [])
    bucket.setdefault("hard_stop", False)


def percent(doc: dict) -> float:
    bucket = doc["budget"]
    total = bucket["total_usd"]
    if total <= 0:
        return 0.0
    return (bucket["spent_usd"] / total) * 100.0


def record(doc: dict, provider: str, actual_cost_usd: float | None,
           estimated_cost_usd: float | None) -> list[str]:
    """Add spend. Returns threshold names newly crossed.

    Estimates are tracked but never counted as actual spend against the ceiling.
    """
    bucket = doc["budget"]
    if provider in METERED_PROVIDERS:
        if actual_cost_usd is not None:
            bucket["spent_usd"] = round(bucket["spent_usd"] + float(actual_cost_usd), 6)
        if estimated_cost_usd is not None:
            bucket["estimated_usd"] = round(
                bucket["estimated_usd"] + float(estimated_cost_usd), 6
            )

    crossed: list[str] = []
    current = percent(doc)
    for level, name in THRESHOLDS:
        if current >= level and name not in bucket["thresholds_crossed"]:
            bucket["thresholds_crossed"].append(name)
            crossed.append(name)
            if name == "HARD_STOP":
                bucket["hard_stop"] = True
    return crossed


def escalation_allowed(doc: dict, *, release_critical: bool = False) -> bool:
    """Whether an expensive-model escalation is still permitted."""
    bucket = doc["budget"]
    if bucket["hard_stop"]:
        return False
    current = percent(doc)
    if current >= 90:
        return release_critical
    if current >= 75:
        return release_critical
    return True


def metered_call_allowed(doc: dict) -> bool:
    """At 100 percent, further paid OpenRouter/Jev calls need human approval."""
    return not doc["budget"]["hard_stop"]


def summary(doc: dict) -> dict:
    bucket = doc["budget"]
    return {
        "total_usd": bucket["total_usd"],
        "spent_usd": bucket["spent_usd"],
        "estimated_usd": bucket["estimated_usd"],
        "percent": round(percent(doc), 2),
        "hard_stop": bucket["hard_stop"],
        "thresholds_crossed": list(bucket["thresholds_crossed"]),
    }
