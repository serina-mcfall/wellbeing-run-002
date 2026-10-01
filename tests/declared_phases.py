"""A `run_declared` stub that suppresses only the slow provider phases.

WHY THIS EXISTS. Before C-18 stage 7, `Supervisor.run_declared` carried
exactly two things: Jev and the Observer. Several suites therefore stubbed
it with a bare `mock.Mock()` to mean "do not run the slow provider work -
it is not part of this boundary". `test_merge_boundary` says so in a
comment at the stub itself.

Stage 7 put the tick's OWN post-T1 phases through the same wrapper -
dispatch execution, security execution, accessibility execution, the
merges and both notification drains - because each can outlast whatever
is left of T1's heartbeat, and an undeclared one makes a healthy
supervisor look dead to the Watchdog.

A bare Mock now swallows those phases too, which is how 85 tests in seven
files went red on a change that altered none of their behaviour. These
helpers keep the stubs' ORIGINAL meaning rather than weakening the
assertions: the provider phases stay suppressed, every other declared
phase still runs.

Deliberately not a Mock subclass and deliberately not auto-applied: a
suite that genuinely wants every phase suppressed should keep saying so
explicitly.
"""

from __future__ import annotations

# The slow provider work that runs after the tick transaction. Only these
# two were ever meant to be suppressed by a `run_declared` stub.
PROVIDER_PHASES = frozenset({"jev", "observer"})


def provider_phases_suppressed(what: str, bound_seconds: float, action):
    """Run every declared phase except Jev and the Observer."""
    if what in PROVIDER_PHASES:
        return None
    return action()


def provider_phase_raises(exc: BaseException):
    """As above, but the provider phase raises - for the tests that prove
    a failure AFTER the merge transaction cannot roll the merge back."""
    def run(what: str, bound_seconds: float, action):
        if what in PROVIDER_PHASES:
            raise exc
        return action()
    return run
