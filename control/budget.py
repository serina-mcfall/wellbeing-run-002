"""Metered spend accounting and threshold policy.

Only incremental metered OpenRouter spend (Jev + companion) counts against the
configured ceiling. Subscription usage for Claude, Codex and Grok is recorded
separately and never inferred as a dollar cost.

Reported provider cost is `actual`. Anything we compute ourselves is `estimated`
and is never allowed to masquerade as actual.

C-19 adds a third quantity, distinct from both: a RESERVATION. A reservation is
a conservative allowance committed to durable state BEFORE a paid request is
sent, and it is admission control, not reporting. It answers "may this call be
made at all", which `spent_usd` cannot: spend is only known after the money is
gone, and a provider that never reports a cost would otherwise spend past the
ceiling with the gate still open.

`estimated_usd` is deliberately NOT reused for this. It has no production
writer - `supervisor.consult_jev` is the only caller of `record()` and passes
`None` - so no historical estimate exists to reinterpret, and its meaning is
different in kind: an estimate is a REPORTING figure that is never retired,
while a reservation is released when the request provably never left, and is
settled against the real charge when one arrives. Folding one into the other
would make a released reservation look like erased spend.
"""

from __future__ import annotations

import math
import os
import uuid

from . import proc

# What a reservation has learned about whether money was spent.
#
# NOT_SENT is an ALLOW-LIST outcome: it is claimed only where the transport
# proves nothing left this machine. Anything else - a server that answered with
# an error, a timeout, a malformed body, a response with no cost - is UNKNOWN,
# and UNKNOWN keeps its exposure rather than releasing it.
BILLED = "BILLED"
NOT_SENT = "NOT_SENT"
UNKNOWN = "UNKNOWN"

RESERVATION_OPEN = "OPEN"
RESERVATION_UNRESOLVED = "UNRESOLVED"
# In flight, and nobody is left who could settle it. Distinct from UNRESOLVED,
# which was settled: there the call completed its lifecycle and reported an
# unknown cost. ABANDONED means we never learned whether the request was even
# sent, and asking again would buy the same answer a second time.
RESERVATION_ABANDONED = "ABANDONED"

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
         "thresholds_crossed": [], "hard_stop": False, "reservations": {}},
    )
    bucket["total_usd"] = float(total_usd)
    bucket.setdefault("spent_usd", 0.0)
    bucket.setdefault("estimated_usd", 0.0)
    bucket.setdefault("thresholds_crossed", [])
    bucket.setdefault("hard_stop", False)
    bucket.setdefault("reservations", {})


def valid_usd(value) -> float | None:
    """A dollar amount we are willing to do arithmetic with, else None.

    `bool` is rejected explicitly: it is an `int` subclass, so `True` would
    otherwise silently become $1.00. NaN and infinity are rejected because they
    poison every comparison they touch, and a negative cost is not a cost.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or number < 0:
        return None
    return number


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


def reservations(doc: dict) -> dict:
    """The durable reservation map, created on first use."""
    return doc["budget"].setdefault("reservations", {})


def outstanding_exposure_usd(doc: dict) -> float:
    """Money that may already be owed but has not been reported as spent.

    A reservation counts here in BOTH states. OPEN means the outcome is not
    known yet - including because the process that made it died mid-flight, in
    which case nothing will ever settle it and it must keep counting.
    UNRESOLVED means the request went out and no usable cost came back.

    A record whose amount is unreadable contributes the WHOLE ceiling rather
    than zero, so tampering or corruption closes the gate instead of opening
    it.
    """
    bucket = doc["budget"]
    ceiling = valid_usd(bucket.get("total_usd")) or 0.0
    exposure = 0.0
    for record_ in reservations(doc).values():
        amount = valid_usd((record_ or {}).get("amount_usd"))
        exposure += ceiling if amount is None else amount
    return round(exposure, 6)


def abandoned_reservations(doc: dict) -> list[str]:
    """Reservations whose call was lost in flight. A pure read."""
    return [key for key, entry in reservations(doc).items()
            if (entry or {}).get("status") == RESERVATION_ABANDONED]


def duplicate_blocked(doc: dict, purpose: str) -> bool:
    """Has a request with this exact purpose already been lost in flight?

    `purpose` is the LOGICAL REQUEST identity - the question and what it is
    about, not the attempt. Re-sending one whose outcome was never learned may
    pay twice for an answer we may already have bought.

    Scoped deliberately. A blanket refusal of all metered spend would also
    stop genuinely new consultations - a different task, a different question -
    which were never duplicates of anything, and would turn one lost sub-cent
    request into the loss of Jev for the whole run.
    """
    return any((entry or {}).get("status") == RESERVATION_ABANDONED
               and (entry or {}).get("purpose") == purpose
               for entry in reservations(doc).values())


def sweep_abandoned(doc: dict) -> list[str]:
    """Mark every in-flight reservation nobody can settle. Returns their ids.

    Only the in-process call that created a reservation ever settles it, and a
    caller settles before it reserves again. So an OPEN reservation seen here
    belongs either to a process that is gone, or to this process, which lost
    it between sending and settling. Both are the same fact: the outcome of a
    request that may already have been billed will never be learned.

    The one exception is a genuinely concurrent caller - preflight running
    beside the Supervisor. Its reservation is left alone while its process is
    PROVABLY the one that made it. Anything less than proof is treated as
    abandoned, because the failure that matters costs money twice.
    """
    pid = os.getpid()
    ticks = proc.start_ticks(pid)
    promoted: list[str] = []
    for key, entry in reservations(doc).items():
        entry = entry or {}
        if entry.get("status") != RESERVATION_OPEN:
            continue
        mine = (entry.get("owner_pid") == pid
                and entry.get("owner_start_ticks") == ticks)
        if not mine and proc.verified_alive(entry.get("owner_pid"),
                                            entry.get("owner_start_ticks")) is True:
            continue
        entry["status"] = RESERVATION_ABANDONED
        promoted.append(key)
    return promoted


def metered_call_allowed(doc: dict) -> bool:
    """Whether any further paid OpenRouter/Jev call may be made at all.

    At 100 percent, further paid calls need human approval. Outstanding
    exposure counts too: unresolved calls that may already be owed cannot be
    spent a second time. `percent()`, `thresholds_crossed` and `hard_stop`
    remain actual-spend only, so an unresolved reservation never fabricates a
    threshold crossing.

    This answers "is there money" only. Whether a PARTICULAR request may be
    re-sent is `duplicate_blocked`, which `reserve` checks separately: an
    abandoned request must not be repeated even with the whole ceiling free,
    and must not stop a different request that is not a repeat of anything.
    """
    bucket = doc["budget"]
    if bucket["hard_stop"]:
        return False
    ceiling = valid_usd(bucket.get("total_usd"))
    spent = valid_usd(bucket.get("spent_usd"))
    if ceiling is None or ceiling <= 0 or spent is None:
        return False
    return round(spent + outstanding_exposure_usd(doc), 6) < round(ceiling, 6)


def reserve(doc: dict, provider: str, amount_usd, *, purpose: str,
            at: str | None = None) -> str | None:
    """Commit a conservative allowance for one paid request, or refuse it.

    Returns the reservation id, which the caller must settle. Returns None when
    the call may not be made - the caller sends nothing.

    This must be called inside the state transaction that persists it, and the
    transaction must commit BEFORE the request is sent: the whole point is that
    a crash after sending still leaves the exposure on disk.
    """
    if provider not in METERED_PROVIDERS:
        return None
    amount = valid_usd(amount_usd)
    if amount is None or amount <= 0:
        return None
    # Before admitting a new call, settle the question of whether an earlier
    # one was lost in flight. Same transaction, so the promotion is durable
    # whether or not this reservation is granted.
    sweep_abandoned(doc)
    if duplicate_blocked(doc, purpose):
        return None
    if not metered_call_allowed(doc):
        return None
    bucket = doc["budget"]
    ceiling = valid_usd(bucket.get("total_usd")) or 0.0
    spent = valid_usd(bucket.get("spent_usd"))
    if spent is None:
        return None
    if round(spent + outstanding_exposure_usd(doc) + amount, 6) > round(ceiling, 6):
        return None
    reservation_id = uuid.uuid4().hex[:12]
    owner_pid = os.getpid()
    reservations(doc)[reservation_id] = {
        "provider": provider,
        "amount_usd": amount,
        "status": RESERVATION_OPEN,
        "purpose": purpose,
        "created_at": at,
        # (pid, start_ticks) identifies one process for the lifetime of a
        # boot, so a reused PID cannot make an orphan look settleable.
        "owner_pid": owner_pid,
        "owner_start_ticks": proc.start_ticks(owner_pid),
    }
    return reservation_id


def settle(doc: dict, reservation_id: str | None, *, provider: str,
           billing_state: str, actual_cost_usd=None) -> list[str]:
    """Close out a reservation once the request's outcome is known.

    Returns threshold names newly crossed, exactly like `record()`.

    - A valid provider cost settles it as ACTUAL spend and removes the
      reservation, so the two are never counted together. This happens even
      when the answer was unusable: the money went either way.
    - A request that provably never left releases the reservation entirely.
    - Anything else keeps the exposure, marked UNRESOLVED. Only a human
      reconciling against the provider's billing can clear it.
    """
    held = reservations(doc)
    cost = valid_usd(actual_cost_usd)
    if billing_state == BILLED and cost is not None:
        held.pop(reservation_id, None)
        return record(doc, provider, cost, None)
    if billing_state == NOT_SENT:
        held.pop(reservation_id, None)
        return []
    entry = held.get(reservation_id)
    if entry is not None:
        entry["status"] = RESERVATION_UNRESOLVED
    return []


def summary(doc: dict) -> dict:
    bucket = doc["budget"]
    return {
        "total_usd": bucket["total_usd"],
        "spent_usd": bucket["spent_usd"],
        "estimated_usd": bucket["estimated_usd"],
        "outstanding_exposure_usd": outstanding_exposure_usd(doc),
        "abandoned_reservations": len(abandoned_reservations(doc)),
        "percent": round(percent(doc), 2),
        "hard_stop": bucket["hard_stop"],
        "thresholds_crossed": list(bucket["thresholds_crossed"]),
    }
