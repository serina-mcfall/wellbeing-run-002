"""The security review contract vocabulary, and nothing else.

A leaf on purpose. control/routing.py produces the adjudication reasons
while checking a parsed contract; control/gate_evidence.py produces the
apparatus reasons while running the review; and claim validation on the
PR record needs both vocabularies at once. The tokens therefore cannot
live in either module without one importing the other, or without a
second copy appearing somewhere and drifting.

So they are defined HERE, once each, and every other module re-exports
rather than restates. There is exactly one string literal per token in
this repository, and it is in this file.

Dependency-light by contract: this module imports nothing from control/
at all - not state, routing, gate_evidence or workers, and no GitHub,
provider or process module. Anything may depend on it; it depends on
nothing, so adding it can never create a cycle.

Nothing here reads a file, opens a socket, spawns a process or touches
durable state. It is a vocabulary.
"""

from __future__ import annotations

# ------------------------------------------------------------- verdicts
#
# What a security review concluded. SECURITY_UNPARSEABLE is the parser's
# answer when no block carrying a verdict could be read at all - it is
# never a verdict a reviewer emitted, and never a pass.
SECURITY_PASS = "SECURITY_PASS"
SECURITY_FAIL = "SECURITY_FAIL"
SECURITY_UNPARSEABLE = "SECURITY_UNPARSEABLE"

SECURITY_VERDICTS = frozenset({SECURITY_PASS, SECURITY_FAIL})

# -------------------------------------------------- adjudication reasons
#
# Produced by routing.security_is_consistent while deciding whether a
# parsed review is a coherent C-05b contract.
OUTPUT_UNPARSEABLE = "OUTPUT_UNPARSEABLE"
VERDICT_UNRECOGNISED = "VERDICT_UNRECOGNISED"
SURFACES_INCOMPLETE = "SURFACES_INCOMPLETE"
FINDING_FIELDS_INVALID = "FINDING_FIELDS_INVALID"
PASS_WITH_BLOCKING_FINDINGS = "PASS_WITH_BLOCKING_FINDINGS"
FAIL_WITHOUT_BLOCKING_FINDINGS = "FAIL_WITHOUT_BLOCKING_FINDINGS"

# ----------------------------------------------------- apparatus reasons
#
# Produced by gate_evidence while running the review: these describe the
# process, not the judgement.
TIMED_OUT = "TIMED_OUT"
EXIT_NONZERO = "EXIT_NONZERO"
OUTPUT_MISSING = "OUTPUT_MISSING"
PROVIDER_FAILURE = "PROVIDER_FAILURE"
SPAWN_OR_RUN_INCOMPLETE = "SPAWN_OR_RUN_INCOMPLETE"

# The complete finite set of reasons a security ATTEMPT is not a completed
# review. Exactly eleven.
#
# PASS_WITH_FINDINGS is deliberately absent and must never be added back:
# governance decision C-05b (2026-09-30) settled that P2/P3 findings are
# non-blocking security debt which MAY accompany a SECURITY_PASS, so a
# code meaning "a pass carried findings" would encode the rule C-05b
# reverses.
#
# These describe an ATTEMPT's outcome. They are not claim-validation
# diagnostics: why a claim RECORD is malformed is a different question
# with a different vocabulary, and conflating the two would let a
# bookkeeping bug be filed as evidence about a review.
SECURITY_FAILURE_REASONS = frozenset({
    OUTPUT_UNPARSEABLE,
    VERDICT_UNRECOGNISED,
    SURFACES_INCOMPLETE,
    FINDING_FIELDS_INVALID,
    PASS_WITH_BLOCKING_FINDINGS,
    FAIL_WITHOUT_BLOCKING_FINDINGS,
    TIMED_OUT,
    EXIT_NONZERO,
    OUTPUT_MISSING,
    PROVIDER_FAILURE,
    SPAWN_OR_RUN_INCOMPLETE,
})
