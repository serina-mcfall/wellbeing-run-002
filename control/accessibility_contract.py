"""The accessibility evidence contract vocabulary, and nothing else.

A leaf on purpose, in the exact shape of control/security_contract.py and
for the same reason. Accessibility evidence has TWO producers - an
in-process automated browser run and a leased qualitative reviewer - and
three consumers: control/routing.py (parsing and the composite REVIEW
gate), control/gate_evidence.py (the attempt lifecycle), and claim
validation on the PR record. The tokens therefore cannot live in any one
of those modules without one importing another, or without a second copy
appearing somewhere and drifting.

So they are defined HERE, once each, and every other module re-exports
rather than restates.

ONE IMPORT, DELIBERATE. Five process-failure reasons - TIMED_OUT,
EXIT_NONZERO, OUTPUT_MISSING, PROVIDER_FAILURE, SPAWN_OR_RUN_INCOMPLETE -
already have their single definition in security_contract, and
gate_evidence already aliases exactly those five from there. They describe
how a bounded subprocess ended, which is not a security fact; restating
them here would be the second literal this file exists to prevent. They
are aliased, not redefined. security_contract imports nothing at all, so
this import can never create a cycle.

It reads slightly oddly - accessibility importing from a module named
security. The alternative is extracting a neutral attempt_contract leaf
and re-exporting from both, which is cleaner naming at the cost of
refactoring landed, tested code. Alias now; extract only if a third
evidence class ever arrives.

Dependency-light by contract otherwise: no state, routing, gate_evidence,
workers, GitHub, provider or process module. Anything may depend on it.
Nothing here reads a file, opens a socket, spawns a process or touches
durable state. It is a vocabulary.
"""

from __future__ import annotations

from . import security_contract

# ------------------------------------------------------------- verdicts
#
# TWO verdict families, because there are two producers and collapsing
# them would let one half's answer be read as the whole gate's.
#
# The QUALITATIVE reviewer's verdict. ACCESSIBILITY_UNPARSEABLE is the
# parser's answer when no block carrying a verdict could be read at all -
# it is never a verdict a reviewer emitted, and never a pass.
ACCESSIBILITY_PASS = "ACCESSIBILITY_PASS"
ACCESSIBILITY_FAIL = "ACCESSIBILITY_FAIL"
ACCESSIBILITY_UNPARSEABLE = "ACCESSIBILITY_UNPARSEABLE"

ACCESSIBILITY_VERDICTS = frozenset({ACCESSIBILITY_PASS, ACCESSIBILITY_FAIL})

# The AUTOMATED half's verdict, over apparatus/accessibility/run.js's nine
# machine checks. Distinct tokens rather than a shared pair: the composite
# REVIEW gate requires BOTH halves at the same head, and a single
# ACCESSIBILITY_PASS token would make a record carrying only the automated
# half indistinguishable from one carrying both. There is no
# ACCESSIBILITY_AUTO_UNPARSEABLE - an unreadable automated run is an
# apparatus failure with a reason (RESULT_UNREADABLE and its siblings
# below), not a verdict.
ACCESSIBILITY_AUTO_PASS = "ACCESSIBILITY_AUTO_PASS"
ACCESSIBILITY_AUTO_FAIL = "ACCESSIBILITY_AUTO_FAIL"

ACCESSIBILITY_AUTO_VERDICTS = frozenset({
    ACCESSIBILITY_AUTO_PASS, ACCESSIBILITY_AUTO_FAIL,
})

# -------------------------------------------------- adjudication reasons
#
# Why a parsed QUALITATIVE review is not a coherent contract. Produced
# while deciding, not while running.
#
# The first five are ALIASES. Their single literal is in
# security_contract, they mean exactly the same thing about an
# accessibility review as about a security one, and a parallel set of
# identical strings under a different owner is the drift this file exists
# to prevent.
OUTPUT_UNPARSEABLE = security_contract.OUTPUT_UNPARSEABLE
VERDICT_UNRECOGNISED = security_contract.VERDICT_UNRECOGNISED
FINDING_FIELDS_INVALID = security_contract.FINDING_FIELDS_INVALID
PASS_WITH_BLOCKING_FINDINGS = security_contract.PASS_WITH_BLOCKING_FINDINGS
FAIL_WITHOUT_BLOCKING_FINDINGS = security_contract.FAIL_WITHOUT_BLOCKING_FINDINGS

# SURFACES_INCOMPLETE is deliberately ABSENT and must not be aliased in.
# prompts/accessibility.md has no surfaces block; completeness here is
# about the ten accessibilityCheck values, so the reason is
# CHECKS_INCOMPLETE below. Reusing the security name would make a reader
# believe a twelve-surface grid exists for accessibility. It does not.

# control/severity.py::apply_severity_policy returned {"valid": False}.
# Security has no equivalent because a security finding's severity is
# self-describing; an accessibility finding's is not - it is derived from
# classification, unmet_requirement and a requirement registry, and six
# distinct contradictions can make it underivable. Every one of them must
# fail the review closed rather than be read as a lesser finding
# (agents/ACCESSIBILITY.md:15, protocol/SEVERITY-POLICY.md).
CLASSIFICATION_INVALID = "CLASSIFICATION_INVALID"

# Fewer than the required set of check_ids was graded. Evaluated against
# the UNION of the nine automated results and the qualitative
# COGNITIVE_SENSORY_REVIEW, so neither producer can satisfy it alone -
# that is the whole point, and it is why this is one reason rather than
# two. agents/ACCESSIBILITY.md:8: "Automated checks do not replace this
# qualitative review; neither replaces the other."
CHECKS_INCOMPLETE = "CHECKS_INCOMPLETE"

# ------------------------------------------- apparatus reasons, automated
#
# Why an AUTOMATED attempt is not usable evidence. These describe the
# apparatus, not the product: none of them may route a task to
# FIX_REQUIRED, because a run that did not happen is not evidence that the
# page is inaccessible (control/gate_evidence.py's stated rule 2).
RESULT_MISSING = "RESULT_MISSING"
RESULT_UNREADABLE = "RESULT_UNREADABLE"
RESULT_EMPTY = "RESULT_EMPTY"
SHA_MISMATCH = "SHA_MISMATCH"

# The product server did not answer within the governed readiness budget.
PRODUCT_SERVER_UNREADY = "PRODUCT_SERVER_UNREADY"

# ----------------------------------------- G3's blocking product states
#
# Approved 2026-10-01: "Missing product files, failed builds and
# unavailable servers remain blocking; no NOT_APPLICABLE route." Each
# needs its own finite diagnostic, because "the app would not build" and
# "the app has no package.json" send a human to different places.
#
# These are ATTEMPT reasons, not accessibility findings. An attempt that
# could not produce evidence is not a judgement that the product is
# inaccessible - it is the absence of a judgement, which blocks.
PRODUCT_ENTRYPOINT_MISSING = "PRODUCT_ENTRYPOINT_MISSING"
PRODUCT_LOCKFILE_MISSING = "PRODUCT_LOCKFILE_MISSING"
PRODUCT_INSTALL_FAILED = "PRODUCT_INSTALL_FAILED"
PRODUCT_BUILD_FAILED = "PRODUCT_BUILD_FAILED"
# The scan finished but the owned server could not be confirmed gone.
# G2: the port stays claimed, so this is evidence of a cleanup that must
# be retried, never a licence to reuse the port.
PRODUCT_SERVER_NOT_RELEASED = "PRODUCT_SERVER_NOT_RELEASED"

# --------------------------------------------- apparatus reasons, shared
#
# Aliased, never redefined - see the module docstring.
TIMED_OUT = security_contract.TIMED_OUT
EXIT_NONZERO = security_contract.EXIT_NONZERO
OUTPUT_MISSING = security_contract.OUTPUT_MISSING
PROVIDER_FAILURE = security_contract.PROVIDER_FAILURE
SPAWN_OR_RUN_INCOMPLETE = security_contract.SPAWN_OR_RUN_INCOMPLETE

# The complete finite set of reasons an accessibility ATTEMPT - either
# half - is not completed evidence. Twenty-two: the original seventeen
# plus G3's five blocking product states, approved 2026-10-01.
#
# These describe an ATTEMPT's outcome. They are not claim-validation
# diagnostics: why a claim RECORD is malformed is a different question
# with a different vocabulary, and conflating the two would let a
# bookkeeping bug be filed as evidence about the product's accessibility.
ACCESSIBILITY_FAILURE_REASONS = frozenset({
    OUTPUT_UNPARSEABLE,
    VERDICT_UNRECOGNISED,
    FINDING_FIELDS_INVALID,
    PASS_WITH_BLOCKING_FINDINGS,
    FAIL_WITHOUT_BLOCKING_FINDINGS,
    CLASSIFICATION_INVALID,
    CHECKS_INCOMPLETE,
    RESULT_MISSING,
    RESULT_UNREADABLE,
    RESULT_EMPTY,
    SHA_MISMATCH,
    PRODUCT_SERVER_UNREADY,
    PRODUCT_ENTRYPOINT_MISSING,
    PRODUCT_LOCKFILE_MISSING,
    PRODUCT_INSTALL_FAILED,
    PRODUCT_BUILD_FAILED,
    PRODUCT_SERVER_NOT_RELEASED,
    TIMED_OUT,
    EXIT_NONZERO,
    OUTPUT_MISSING,
    PROVIDER_FAILURE,
    SPAWN_OR_RUN_INCOMPLETE,
})

# The subset an AUTOMATED attempt can produce. gate_evidence's
# _adjudicate emits the first four plus TIMED_OUT and EXIT_NONZERO today;
# CHECKS_INCOMPLETE comes from normalising its checks against the
# required set, and PRODUCT_SERVER_UNREADY from the server lifecycle that
# has not been built. The qualitative-only reasons are excluded so a
# machine run can never be recorded as having failed a judgement it never
# made.
ACCESSIBILITY_AUTO_FAILURE_REASONS = frozenset({
    RESULT_MISSING,
    RESULT_UNREADABLE,
    RESULT_EMPTY,
    SHA_MISMATCH,
    CHECKS_INCOMPLETE,
    PRODUCT_SERVER_UNREADY,
    PRODUCT_ENTRYPOINT_MISSING,
    PRODUCT_LOCKFILE_MISSING,
    PRODUCT_INSTALL_FAILED,
    PRODUCT_BUILD_FAILED,
    PRODUCT_SERVER_NOT_RELEASED,
    TIMED_OUT,
    EXIT_NONZERO,
    SPAWN_OR_RUN_INCOMPLETE,
})
