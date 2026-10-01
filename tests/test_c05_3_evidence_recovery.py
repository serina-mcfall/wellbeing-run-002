"""C-05.3a Step 6a: the state-only claim, and what a restart makes of it.

A Supervisor can die at any instant. The dangerous instant is between
spawning a security reviewer and committing the fact that it did, because
a restart then finds a claim that says PLANNED and a provider review that
is actually running. Dispatching a second one costs real money and
produces two verdicts for one head.

So the classifier's whole job is to refuse to guess. It may authorise a
new attempt only when the previous one is PROVEN not running, and proven
means three things at once:

  * the /proc scan SUCCEEDED - a failed enumeration proves nothing;
  * the claimed worker was ABSENT from that successful scan;
  * the governed lease has EXPIRED (now >= lease_expires_at).

Miss any one and the answer is "not yet observed", which is not "gone".

The claim itself is written in the state transaction and nowhere else,
and the ORDINAL is chosen there - under the exclusive lock, which is what
stops two ticks both deciding "attempt 3". Phase B creates no directory,
writes no job file, runs no git, calls no GitHub and spawns nothing; the
observations it classifies were gathered before the lock was taken.
"""

from __future__ import annotations

import dataclasses
import json
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from control import (  # noqa: E402
    clock,
    ledger as ledger_mod,
    notify,
    providers,
    routing,
    security_contract,
    state as state_mod,
    supervisor as supervisor_mod,
)

TZ = "Pacific/Auckland"
SHA = "a" * 40
OTHER_SHA = "b" * 40
R = routing


def observe(**over):
    base = dict(head_sha=SHA, now=clock.iso(clock.now(TZ)), scan_ok=True,
                worker_live=False, attempt_dir_exists=True,
                job_file_exists=True, outcome_present=False)
    base.update(over)
    return R.SecurityObservation(**base)


def a_claim(*, ordinal=1, sha=SHA, claim_state="SPAWNED", lease_offset=1800,
            **over):
    """One claim, optionally already expired.

    An expired claim cannot be built directly: security_claim refuses
    lease_expires_at <= claimed_at, and rightly so. So it is built valid
    and then AGED - claimed_at moved into the past with the lease still
    strictly after it, but both now behind the clock. That is exactly the
    shape a real expired claim has on disk.
    """
    now = clock.now(TZ)
    if lease_offset > 0:
        claimed_at, expires_at = now, now + timedelta(seconds=lease_offset)
    else:
        # lease_offset <= 0 means the lease has already run out. It cannot
        # simply be moved back: security_claim refuses lease <= claimed,
        # and lease_offset == 0 would make them equal. So the whole claim
        # moves into the past, keeping the lease strictly after its claim.
        expires_at = now + timedelta(seconds=lease_offset)
        claimed_at = expires_at - timedelta(seconds=1800)
    claim = R.security_claim(
        task_id="TASK-001", sha=sha, ordinal=ordinal,
        claimed_at=clock.iso(claimed_at),
        lease_expires_at=clock.iso(expires_at))
    claim["claim_state"] = claim_state
    claim.update(over)
    return claim


class RecoveryTableCase(unittest.TestCase):
    """One row per approved classification."""

    def test_no_claim_at_all(self):
        self.assertEqual(R.security_recovery_state(None, observe()),
                         R.SECURITY_RECOVERY_NO_CLAIM)

    def test_a_claim_for_a_different_head_is_no_claim(self):
        # A prior SHA's evidence never carries forward. The old claim still
        # exists; it simply says nothing about this head.
        self.assertEqual(
            R.security_recovery_state(a_claim(sha=OTHER_SHA), observe()),
            R.SECURITY_RECOVERY_NO_CLAIM)

    def test_a_durable_outcome_outranks_everything(self):
        # Disk is authoritative, state is a cache. Whatever claim_state
        # says, a published outcome means the attempt finished.
        for claim_state, over in (("PLANNED", {}), ("SPAWNED", {}),
                                  ("COMPLETE",
                                   {"verdict": security_contract.SECURITY_PASS})):
            with self.subTest(claim_state=claim_state):
                # A COMPLETE claim must carry a verdict or a finite reason
                # to be a claim at all - one with neither is malformed, and
                # is covered by MalformedClaimCase instead.
                self.assertEqual(
                    R.security_recovery_state(
                        a_claim(claim_state=claim_state, **over),
                        observe(outcome_present=True, worker_live=True,
                                scan_ok=False)),
                    R.SECURITY_RECOVERY_COMPLETE)

    def test_a_failed_proc_scan_is_unknown(self):
        self.assertEqual(
            R.security_recovery_state(a_claim(), observe(scan_ok=False)),
            R.SECURITY_RECOVERY_UNKNOWN)

    def test_planned_with_no_directory_is_not_materialized(self):
        self.assertEqual(
            R.security_recovery_state(
                a_claim(claim_state="PLANNED"),
                observe(attempt_dir_exists=False, job_file_exists=False)),
            R.SECURITY_RECOVERY_NOT_MATERIALIZED)

    def test_planned_with_a_directory_but_no_job_file(self):
        self.assertEqual(
            R.security_recovery_state(
                a_claim(claim_state="PLANNED"),
                observe(job_file_exists=False)),
            R.SECURITY_RECOVERY_MATERIALIZED_NOT_SPAWNED)

    def test_planned_with_a_job_file_is_treated_as_spawned(self):
        # The job file is written immediately before the spawn, so a crash
        # between the two leaves exactly this. Reading it as "not spawned"
        # would dispatch a second review over a live one.
        self.assertEqual(
            R.security_recovery_state(a_claim(claim_state="PLANNED"),
                                      observe(worker_live=True)),
            R.SECURITY_RECOVERY_RUNNING)
        self.assertEqual(
            R.security_recovery_state(a_claim(claim_state="PLANNED"),
                                      observe(worker_live=False)),
            R.SECURITY_RECOVERY_INDETERMINATE)

    def test_a_live_worker_is_running(self):
        self.assertEqual(
            R.security_recovery_state(a_claim(), observe(worker_live=True)),
            R.SECURITY_RECOVERY_RUNNING)

    def test_absent_but_within_the_lease_is_indeterminate(self):
        self.assertEqual(
            R.security_recovery_state(a_claim(lease_offset=3600), observe()),
            R.SECURITY_RECOVERY_INDETERMINATE)

    def test_absent_and_past_the_lease_is_proven_not_running(self):
        self.assertEqual(
            R.security_recovery_state(a_claim(lease_offset=-1), observe()),
            R.SECURITY_RECOVERY_PROVEN_NOT_RUNNING)

    def test_a_complete_claim_with_no_outcome_on_disk_is_unknown(self):
        # The cache says finished while the evidence is gone. Neither
        # re-dispatching nor trusting it is safe.
        self.assertEqual(
            R.security_recovery_state(a_claim(claim_state="COMPLETE"),
                                      observe()),
            R.SECURITY_RECOVERY_UNKNOWN)

    def test_every_answer_is_a_finite_token(self):
        for claim in (None, a_claim(), a_claim(claim_state="PLANNED"),
                      a_claim(sha=OTHER_SHA), a_claim(claim_state="COMPLETE"),
                      {}, {"sha": SHA}, {"sha": SHA, "claim_state": "NOPE"}):
            for obs in (observe(), observe(scan_ok=False),
                        observe(outcome_present=True),
                        observe(worker_live=True),
                        observe(attempt_dir_exists=False)):
                with self.subTest(claim=str(claim)[:40], obs=str(obs)[:40]):
                    self.assertIn(R.security_recovery_state(claim, obs),
                                  R.SECURITY_RECOVERY_STATES)


def malformed_claims():
    """Same-head claims this control plane would never have written.

    Each is refused by routing.security_claim_is_valid, so none may be
    trusted for its ordinal, attempt id or worker name - and none may be
    quietly replaced either, because attempt 1 may already exist on disk
    with a review running against it.
    """
    good = a_claim(ordinal=3, lease_offset=-1)
    planned = a_claim(ordinal=3, claim_state="PLANNED")
    return {
        "missing claim_state": {k: v for k, v in good.items()
                                if k != "claim_state"},
        "claim_state garbage": dict(good, claim_state="NOPE"),
        "missing attempt_id": {k: v for k, v in planned.items()
                               if k != "attempt_id"},
        "missing worker": {k: v for k, v in planned.items() if k != "worker"},
        "missing ordinal": {k: v for k, v in good.items() if k != "ordinal"},
        "ordinal is a string": dict(good, ordinal="3"),
        "ordinal is True": dict(good, ordinal=True),
        "attempt id disagrees": dict(good, attempt_id="security-attempt-0099"),
        "worker traversing": dict(planned, worker="../escape"),
        "timestamps garbled": dict(good, lease_expires_at="nope"),
        "extra key": dict(good, attacker="x"),
        "empty dict same head": {"sha": SHA},
    }


class MalformedClaimCase(unittest.TestCase):
    """A persisted claim that is not one this control plane wrote."""

    def test_no_malformed_claim_can_authorize_dispatch(self):
        for label, claim in malformed_claims().items():
            for obs in (observe(), observe(job_file_exists=False),
                        observe(attempt_dir_exists=False,
                                job_file_exists=False),
                        observe(worker_live=True)):
                with self.subTest(claim=label, obs=str(obs)[:40]):
                    got = R.security_recovery_state(claim, obs)
                    self.assertNotIn(got, R.SECURITY_RECOVERY_DISPATCHABLE,
                                     f"{label} authorised dispatch as {got}")

    def test_a_malformed_claim_classifies_as_unknown(self):
        for label, claim in malformed_claims().items():
            with self.subTest(claim=label):
                self.assertFalse(R.security_claim_is_valid(claim)[0])
                self.assertEqual(R.security_recovery_state(claim, observe()),
                                 R.SECURITY_RECOVERY_UNKNOWN)

    def test_the_classifier_never_raises_on_a_malformed_claim(self):
        for label, claim in malformed_claims().items():
            with self.subTest(claim=label):
                try:
                    R.security_recovery_state(claim, observe())
                except Exception as exc:                 # noqa: BLE001
                    self.fail(f"{label} raised {type(exc).__name__}: {exc}")

    def test_a_valid_claim_is_still_classified_normally(self):
        # The validity gate must not swallow the ordinary cases.
        self.assertEqual(
            R.security_recovery_state(a_claim(lease_offset=-1), observe()),
            R.SECURITY_RECOVERY_PROVEN_NOT_RUNNING)
        self.assertEqual(
            R.security_recovery_state(a_claim(), observe(worker_live=True)),
            R.SECURITY_RECOVERY_RUNNING)

    def test_a_legacy_record_without_the_field_still_claims_normally(self):
        legacy = routing.blank_pr_record(12, "TASK-001", "feat/x")
        del legacy["security_evidence"]
        self.assertEqual(
            R.security_recovery_state(legacy.get("security_evidence"),
                                      observe()),
            R.SECURITY_RECOVERY_NO_CLAIM)


class PersistedClaimCategoryCase(unittest.TestCase):
    """Four categories of persisted value, and only one of them may claim.

    The ordering matters. Reading the SHA before validating would let a
    string, a list or an empty object answer "not this head" and so be
    classified NO_CLAIM - which authorises a fresh ordinal-1 claim over
    the top of an attempt 1 that may already exist with a review running
    against it.
    """

    def malformed_values(self):
        good = a_claim(ordinal=3, lease_offset=-1)
        stale = dict(good, sha=OTHER_SHA)
        return {
            "string": "garbage",
            "int": 42,
            "float": 1.5,
            "bool": True,
            "list": [good],
            "tuple": (good,),
            "empty dict": {},
            "missing sha": {k: v for k, v in good.items() if k != "sha"},
            "sha not hex": dict(good, sha="nope"),
            "sha None": dict(good, sha=None),
            "malformed different head": {k: v for k, v in stale.items()
                                         if k != "ordinal"},
        }

    def test_absence_is_the_only_legitimate_no_claim(self):
        self.assertEqual(R.security_recovery_state(None, observe()),
                         R.SECURITY_RECOVERY_NO_CLAIM)
        legacy = routing.blank_pr_record(12, "TASK-001", "feat/x")
        del legacy["security_evidence"]
        self.assertEqual(
            R.security_recovery_state(legacy.get("security_evidence"),
                                      observe()),
            R.SECURITY_RECOVERY_NO_CLAIM)

    def test_every_other_invalid_value_is_unknown_not_no_claim(self):
        for label, value in self.malformed_values().items():
            with self.subTest(value=label):
                got = R.security_recovery_state(value, observe())
                self.assertEqual(got, R.SECURITY_RECOVERY_UNKNOWN, label)
                self.assertNotIn(got, R.SECURITY_RECOVERY_DISPATCHABLE)

    def test_a_valid_claim_for_another_head_is_the_stale_head_rule(self):
        # Intact, just about a different commit. A prior SHA's evidence
        # never carries forward, so a fresh claim is correct here.
        self.assertEqual(
            R.security_recovery_state(a_claim(sha=OTHER_SHA), observe()),
            R.SECURITY_RECOVERY_NO_CLAIM)

    def test_a_valid_same_head_claim_reaches_the_recovery_table(self):
        self.assertEqual(
            R.security_recovery_state(a_claim(lease_offset=-1), observe()),
            R.SECURITY_RECOVERY_PROVEN_NOT_RUNNING)

    def test_each_category_has_a_finite_diagnostic(self):
        self.assertEqual(R.security_claim_diagnostic(None), "")
        self.assertEqual(R.security_claim_diagnostic(a_claim()), "")
        self.assertEqual(R.security_claim_diagnostic(a_claim(sha=OTHER_SHA)), "")
        for label, value in self.malformed_values().items():
            with self.subTest(value=label):
                diagnostic = R.security_claim_diagnostic(value)
                self.assertTrue(diagnostic.startswith("CLAIM_"), label)
                # Finite vocabulary, never an attempt failure reason: a
                # bookkeeping fault is not evidence about a review.
                self.assertNotIn(diagnostic,
                                 security_contract.SECURITY_FAILURE_REASONS)

    def test_the_classifier_never_raises_on_any_persisted_value(self):
        for label, value in self.malformed_values().items():
            with self.subTest(value=label):
                try:
                    R.security_recovery_state(value, observe())
                    R.security_claim_diagnostic(value)
                    R.security_spawn_uncommitted(value, observe())
                except Exception as exc:                 # noqa: BLE001
                    self.fail(f"{label} raised {type(exc).__name__}: {exc}")


class SpawnUncommittedCase(unittest.TestCase):
    """The crash window between writing the job file and committing the
    spawn. The approved table names it SPAWN_UNCOMMITTED with the action
    'treat as SPAWNED - fail closed'; the condition stays observable."""

    def test_the_condition_is_detected(self):
        self.assertTrue(R.security_spawn_uncommitted(
            a_claim(claim_state="PLANNED"), observe()))

    def test_it_is_false_for_every_other_shape(self):
        for label, claim, obs in (
                ("no claim", None, observe()),
                ("already spawned", a_claim(), observe()),
                ("different head", a_claim(sha=OTHER_SHA, claim_state="PLANNED"),
                 observe()),
                ("no job file yet", a_claim(claim_state="PLANNED"),
                 observe(job_file_exists=False)),
                ("not materialized", a_claim(claim_state="PLANNED"),
                 observe(attempt_dir_exists=False, job_file_exists=False))):
            with self.subTest(case=label):
                self.assertFalse(R.security_spawn_uncommitted(claim, obs))

    def test_it_still_recovers_through_the_spawned_rows(self):
        # Folding must not strand the task: an expired uncommitted spawn is
        # still recoverable once the lease runs out.
        planned = a_claim(claim_state="PLANNED", lease_offset=-1)
        self.assertTrue(R.security_spawn_uncommitted(planned, observe()))
        self.assertEqual(R.security_recovery_state(planned, observe()),
                         R.SECURITY_RECOVERY_PROVEN_NOT_RUNNING)


class ProvenNotRunningCase(unittest.TestCase):
    """The three-condition rule, one condition at a time."""

    def test_all_three_conditions_are_required(self):
        expired = a_claim(lease_offset=-1)
        self.assertEqual(R.security_recovery_state(expired, observe()),
                         R.SECURITY_RECOVERY_PROVEN_NOT_RUNNING)
        for label, obs in (
                ("scan failed", observe(scan_ok=False)),
                ("worker still live", observe(worker_live=True))):
            with self.subTest(missing=label):
                self.assertNotEqual(
                    R.security_recovery_state(expired, obs),
                    R.SECURITY_RECOVERY_PROVEN_NOT_RUNNING)
        with self.subTest(missing="lease unexpired"):
            self.assertNotEqual(
                R.security_recovery_state(a_claim(lease_offset=1), observe()),
                R.SECURITY_RECOVERY_PROVEN_NOT_RUNNING)

    def test_the_expiry_boundary_is_inclusive(self):
        # now >= lease_expires_at. Exactly at the boundary the lease has
        # run out, so the attempt is proven finished.
        claim = a_claim(lease_offset=0)          # lease expires exactly now
        expiry = clock.parse(claim["lease_expires_at"])
        self.assertEqual(
            R.security_recovery_state(claim, observe(now=clock.iso(expiry))),
            R.SECURITY_RECOVERY_PROVEN_NOT_RUNNING)
        one_earlier = clock.iso(expiry - timedelta(seconds=1))
        self.assertEqual(
            R.security_recovery_state(claim, observe(now=one_earlier)),
            R.SECURITY_RECOVERY_INDETERMINATE)

    def test_an_unreadable_lease_or_clock_is_unknown(self):
        for label, claim, obs in (
                ("lease garbled", a_claim(lease_expires_at="nope"), observe()),
                ("lease naive", a_claim(lease_expires_at="2026-09-30T08:00:00"),
                 observe()),
                ("now garbled", a_claim(), observe(now="nope"))):
            with self.subTest(case=label):
                self.assertEqual(R.security_recovery_state(claim, obs),
                                 R.SECURITY_RECOVERY_UNKNOWN)

    def test_only_four_classifications_may_lead_to_new_work(self):
        self.assertEqual(
            R.SECURITY_RECOVERY_DISPATCHABLE,
            frozenset({R.SECURITY_RECOVERY_NO_CLAIM,
                       R.SECURITY_RECOVERY_NOT_MATERIALIZED,
                       R.SECURITY_RECOVERY_MATERIALIZED_NOT_SPAWNED,
                       R.SECURITY_RECOVERY_PROVEN_NOT_RUNNING}))
        for held in (R.SECURITY_RECOVERY_RUNNING,
                     R.SECURITY_RECOVERY_INDETERMINATE,
                     R.SECURITY_RECOVERY_UNKNOWN,
                     R.SECURITY_RECOVERY_COMPLETE):
            with self.subTest(classification=held):
                self.assertNotIn(held, R.SECURITY_RECOVERY_DISPATCHABLE)


class ClassifierPurityCase(unittest.TestCase):

    def test_it_touches_no_filesystem_network_or_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            before = sorted(Path(tmp).rglob("*"))
            with mock.patch("builtins.open",
                            side_effect=AssertionError("opened a file")):
                for claim_state in ("PLANNED", "SPAWNED", "COMPLETE"):
                    R.security_recovery_state(a_claim(claim_state=claim_state),
                                              observe())
            self.assertEqual(sorted(Path(tmp).rglob("*")), before)

    def test_the_same_inputs_always_give_the_same_answer(self):
        claim, obs = a_claim(), observe()
        answers = {R.security_recovery_state(claim, obs) for _ in range(25)}
        self.assertEqual(len(answers), 1)


class PlanSecurityCase(unittest.TestCase):
    """Phase B: the claim, written under the lock and nowhere else."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.root = root
        self.store = state_mod.Store(path=root / "state.json", tz=TZ)
        self.store.initialise("run-002", "v2.0")
        self.ledger = ledger_mod.Ledger(path=root / "ledger.jsonl", tz=TZ,
                                        experiment_id="run-002")
        self.sv = supervisor_mod.Supervisor.__new__(supervisor_mod.Supervisor)
        self.sv.cfg = SimpleNamespace(
            timezone=TZ, github_repo="o/r", max_security=1,
            # C-05.3b: route_evidence plans the QUALITATIVE accessibility
            # leg as well now, and that path reads its own governed
            # limit. Zero, so these security-focused cases keep planning
            # exactly what they always did - the limit is what is being
            # completed here, not an assertion.
            max_accessibility_review=0,
            roles={"security": SimpleNamespace(provider="codex", model=None,
                                               effort="medium")},
            extra={"timeouts": {"security": 1800, "lease_grace_seconds": 60}})
        self.sv.stopping = False
        self.sv.tz = TZ
        self.sv.ledger = self.ledger
        self.sv.store = self.store
        self.sv.notifier = mock.Mock(spec=notify.Notifier)

    def doc_with_pr(self, task_state="PR_OPEN", claim=None):
        doc = state_mod.initial_document("run-002", "v2.0")
        # tick calls providers.ensure before any routing, so a document that
        # reaches plan_security always has the provider bucket populated.
        providers.ensure(doc)
        task = state_mod.add_task(doc, "TASK-001", "T", [], "feature", False, TZ)
        task["state"] = task_state
        task["pr"] = 12
        doc["prs"]["12"] = routing.blank_pr_record(12, "TASK-001", "feat/x")
        if claim is not None:
            doc["prs"]["12"]["security_evidence"] = claim
        return doc, task

    def plan(self, doc, task, obs):
        return self.sv.plan_security(doc, task, 12, SHA, obs)

    def events(self, event_type):
        lines = Path(self.ledger.path).read_text(encoding="utf-8").splitlines()
        return [json.loads(l) for l in lines if l and
                json.loads(l).get("event_type") == event_type]

    # ---------------------------------------------------------- claiming

    def test_a_fresh_claim_starts_at_ordinal_one(self):
        doc, task = self.doc_with_pr()
        result = self.plan(doc, task, observe(attempt_dir_exists=False,
                                              job_file_exists=False))
        claim = doc["prs"]["12"]["security_evidence"]
        self.assertEqual(claim["ordinal"], 1)
        self.assertEqual(claim["attempt_id"], "security-attempt-0001")
        expected_worker = f"task-001-security-{SHA}-0001"
        self.assertEqual(claim["worker"], expected_worker)
        self.assertEqual(claim["claim_state"], "PLANNED")
        self.assertEqual(R.security_claim_is_valid(claim), (True, ""))
        self.assertEqual(
            (result.task_id, result.pr, result.sha, result.attempt_id,
             result.worker),
            ("TASK-001", 12, SHA, "security-attempt-0001", expected_worker))

    def test_it_transitions_pr_open_to_waiting_evidence(self):
        doc, task = self.doc_with_pr()
        self.plan(doc, task, observe(attempt_dir_exists=False,
                                     job_file_exists=False))
        self.assertEqual(doc["tasks"]["TASK-001"]["state"], "WAITING_EVIDENCE")

    def test_it_returns_identifiers_not_live_objects(self):
        # C-14.1: the task, record and claim belong to this transaction's
        # document and are stale the moment it commits.
        doc, task = self.doc_with_pr()
        result = self.plan(doc, task, observe(attempt_dir_exists=False,
                                              job_file_exists=False))
        self.assertIsInstance(result, routing.SecurityPlan)
        for field in dataclasses.fields(result):
            self.assertIsInstance(getattr(result, field.name), (str, int))
        # Frozen, so nothing downstream can mutate what T1 decided.
        with self.assertRaises(dataclasses.FrozenInstanceError):
            result.sha = "0" * 40

    def test_the_lease_comes_from_governed_config(self):
        doc, task = self.doc_with_pr()
        self.plan(doc, task, observe(attempt_dir_exists=False,
                                     job_file_exists=False))
        claim = doc["prs"]["12"]["security_evidence"]
        span = (clock.parse(claim["lease_expires_at"])
                - clock.parse(claim["claimed_at"])).total_seconds()
        self.assertAlmostEqual(span, 1800 + 60, delta=2)

    def test_a_stale_head_claim_is_replaced_and_restarts_at_one(self):
        # The attempt namespace is scoped per (task, SHA); a new head is a
        # new namespace, not a continuation.
        doc, task = self.doc_with_pr(claim=a_claim(sha=OTHER_SHA, ordinal=7))
        self.plan(doc, task, observe(attempt_dir_exists=False,
                                     job_file_exists=False))
        claim = doc["prs"]["12"]["security_evidence"]
        self.assertEqual(claim["ordinal"], 1)
        self.assertEqual(claim["sha"], SHA)

    def test_a_proven_dead_attempt_advances_the_ordinal(self):
        doc, task = self.doc_with_pr(
            task_state="WAITING_EVIDENCE",
            claim=a_claim(ordinal=3, lease_offset=-1))
        result = self.plan(doc, task, observe())
        claim = doc["prs"]["12"]["security_evidence"]
        self.assertEqual(claim["ordinal"], 4)
        self.assertEqual(claim["attempt_id"], "security-attempt-0004")
        self.assertEqual(result.attempt_id, "security-attempt-0004")
        self.assertEqual(len(self.events("SECURITY_ATTEMPT_ABANDONED")), 1)
        self.assertEqual(
            self.events("SECURITY_ATTEMPT_ABANDONED")[0]["outcome"],
            security_contract.SPAWN_OR_RUN_INCOMPLETE)

    def test_an_unfinished_claim_is_reused_never_reallocated(self):
        # NOT_MATERIALIZED / MATERIALIZED_NOT_SPAWNED mean the work outside
        # the lock has not finished. Allocating a second ordinal would
        # abandon a directory that may already hold evidence.
        for label, obs in (
                ("not materialized",
                 observe(attempt_dir_exists=False, job_file_exists=False)),
                ("no job file yet", observe(job_file_exists=False))):
            with self.subTest(case=label):
                doc, task = self.doc_with_pr(
                    task_state="WAITING_EVIDENCE",
                    claim=a_claim(ordinal=5, claim_state="PLANNED"))
                result = self.plan(doc, task, obs)
                claim = doc["prs"]["12"]["security_evidence"]
                self.assertEqual(claim["ordinal"], 5)
                self.assertEqual(result.attempt_id, "security-attempt-0005")

    # ------------------------------------------------------------ holding

    def test_nothing_is_dispatched_while_work_may_be_running(self):
        for label, claim, obs in (
                ("live worker", a_claim(), observe(worker_live=True)),
                ("lease unexpired", a_claim(lease_offset=3600), observe()),
                ("scan failed", a_claim(), observe(scan_ok=False)),
                ("outcome present", a_claim(), observe(outcome_present=True)),
                ("complete without evidence", a_claim(claim_state="COMPLETE"),
                 observe())):
            with self.subTest(case=label):
                doc, task = self.doc_with_pr(task_state="WAITING_EVIDENCE",
                                             claim=claim)
                before = json.dumps(doc["prs"]["12"]["security_evidence"])
                self.assertIsNone(self.plan(doc, task, obs))
                self.assertEqual(
                    json.dumps(doc["prs"]["12"]["security_evidence"]), before,
                    "the claim was mutated while holding")

    def test_a_held_task_is_not_transitioned(self):
        doc, task = self.doc_with_pr(claim=a_claim())
        self.plan(doc, task, observe(worker_live=True))
        self.assertEqual(doc["tasks"]["TASK-001"]["state"], "PR_OPEN")

    def test_an_exhausted_ordinal_refuses_rather_than_overflowing(self):
        # security_claim would refuse ordinal 10000 anyway, so the return
        # value alone cannot tell the two apart. The ceiling exists to name
        # the condition: a namespace runaway is an operational signal, not
        # an ordinary validation refusal, and the ledger has to say which.
        doc, task = self.doc_with_pr(
            task_state="WAITING_EVIDENCE",
            claim=a_claim(ordinal=9999, lease_offset=-1))
        self.assertIsNone(self.plan(doc, task, observe()))
        self.assertEqual(
            doc["prs"]["12"]["security_evidence"]["ordinal"], 9999)
        held = self.events("SECURITY_EVIDENCE_HELD")
        self.assertEqual([e["outcome"] for e in held], ["ORDINAL_EXHAUSTED"])
        self.assertEqual(held[0]["metadata_redacted"]["ordinal"], 10000)

    def test_a_missing_pr_record_is_not_a_crash(self):
        doc, task = self.doc_with_pr()
        del doc["prs"]["12"]
        self.assertIsNone(self.plan(doc, task, observe()))

    # -------------------------------------------------- transaction purity

    def test_phase_b_performs_no_external_work(self):
        doc, task = self.doc_with_pr()
        with tempfile.TemporaryDirectory() as tmp:
            before = sorted(Path(tmp).rglob("*"))
            with mock.patch.object(supervisor_mod, "gh") as gh_mock, \
                    mock.patch.object(supervisor_mod, "workers") as w_mock, \
                    mock.patch.object(supervisor_mod, "proc") as proc_mock:
                self.plan(doc, task, observe(attempt_dir_exists=False,
                                             job_file_exists=False))
                gh_mock.assert_not_called()
                self.assertEqual(gh_mock.method_calls, [])
                self.assertEqual(w_mock.method_calls, [])
                self.assertEqual(proc_mock.method_calls, [])
            self.assertEqual(sorted(Path(tmp).rglob("*")), before)

    def test_no_evidence_directory_is_created(self):
        doc, task = self.doc_with_pr()
        self.plan(doc, task, observe(attempt_dir_exists=False,
                                     job_file_exists=False))
        self.assertEqual(list(self.root.glob("**/security-attempt-*")), [])

    def test_no_raw_exception_prose_reaches_the_ledger(self):
        doc, task = self.doc_with_pr()
        with mock.patch.object(routing, "security_claim",
                               side_effect=ValueError("/home/serina/secret")):
            self.assertIsNone(self.plan(doc, task,
                                        observe(attempt_dir_exists=False,
                                                job_file_exists=False)))
        blob = Path(self.ledger.path).read_text(encoding="utf-8")
        self.assertNotIn("/home/serina/secret", blob)
        self.assertIn("CLAIM_REFUSED", blob)

    def test_a_malformed_claim_never_reaches_the_claim_writing_paths(self):
        # Before the validity gate these raised KeyError on a missing
        # attempt_id or worker, TypeError on a string ordinal, and -
        # worst - silently minted a fresh ordinal-1 claim when ordinal was
        # absent, over the top of an attempt 1 that may already exist.
        for label, claim in malformed_claims().items():
            for obs in (observe(), observe(job_file_exists=False),
                        observe(attempt_dir_exists=False,
                                job_file_exists=False)):
                with self.subTest(claim=label, obs=str(obs)[:36]):
                    doc, task = self.doc_with_pr(
                        task_state="WAITING_EVIDENCE", claim=claim)
                    before = json.dumps(doc["prs"]["12"]["security_evidence"],
                                        sort_keys=True)
                    try:
                        result = self.plan(doc, task, obs)
                    except Exception as exc:             # noqa: BLE001
                        self.fail(f"{label} raised {type(exc).__name__}")
                    self.assertIsNone(result, label)
                    self.assertEqual(
                        json.dumps(doc["prs"]["12"]["security_evidence"],
                                   sort_keys=True), before,
                        f"{label} mutated the claim")
                    self.assertEqual(
                        doc["tasks"]["TASK-001"]["state"], "WAITING_EVIDENCE")

    def test_malformed_persisted_evidence_holds_and_is_diagnosed(self):
        # Fail closed, leave everything alone, and say which rule broke.
        for label, value in (("string", "garbage"), ("int", 42),
                             ("list", [1]), ("empty dict", {}),
                             ("sha not hex", dict(a_claim(), sha="nope"))):
            with self.subTest(value=label):
                doc, task = self.doc_with_pr(task_state="WAITING_EVIDENCE",
                                             claim=value)
                before = json.dumps(doc["prs"]["12"]["security_evidence"],
                                    sort_keys=True, default=str)
                self.assertIsNone(self.plan(doc, task, observe()))
                self.assertEqual(
                    json.dumps(doc["prs"]["12"]["security_evidence"],
                               sort_keys=True, default=str), before,
                    "malformed evidence was replaced")
                self.assertEqual(doc["tasks"]["TASK-001"]["state"],
                                 "WAITING_EVIDENCE")
                held = self.events("SECURITY_EVIDENCE_HELD")[-1]
                self.assertEqual(held["outcome"], R.SECURITY_RECOVERY_UNKNOWN)
                diagnostic = held["metadata_redacted"]["claim_diagnostic"]
                self.assertTrue(diagnostic.startswith("CLAIM_"), diagnostic)

    def test_a_legacy_record_still_claims_normally(self):
        doc, task = self.doc_with_pr()
        del doc["prs"]["12"]["security_evidence"]
        result = self.plan(doc, task, observe(attempt_dir_exists=False,
                                              job_file_exists=False))
        self.assertIsNotNone(result)
        self.assertEqual(
            doc["prs"]["12"]["security_evidence"]["ordinal"], 1)

    def test_a_head_mismatch_refuses_rather_than_crossing_commits(self):
        # Classifying one SHA's evidence and then allocating for another
        # would attribute a review to a commit it never looked at.
        doc, task = self.doc_with_pr()
        result = self.sv.plan_security(
            doc, task, 12, OTHER_SHA,
            observe(head_sha=SHA, attempt_dir_exists=False,
                    job_file_exists=False))
        self.assertIsNone(result)
        self.assertIsNone(doc["prs"]["12"]["security_evidence"])
        self.assertEqual(doc["tasks"]["TASK-001"]["state"], "PR_OPEN")
        held = self.events("SECURITY_EVIDENCE_HELD")
        self.assertEqual([e["outcome"] for e in held], ["HEAD_MISMATCH"])

    def test_matching_heads_proceed_normally(self):
        doc, task = self.doc_with_pr()
        self.assertIsNotNone(self.sv.plan_security(
            doc, task, 12, SHA,
            observe(head_sha=SHA, attempt_dir_exists=False,
                    job_file_exists=False)))

    def test_the_spawn_uncommitted_condition_is_recorded(self):
        doc, task = self.doc_with_pr(task_state="WAITING_EVIDENCE",
                                     claim=a_claim(claim_state="PLANNED"))
        self.assertIsNone(self.plan(doc, task, observe(worker_live=True)))
        held = self.events("SECURITY_EVIDENCE_HELD")[0]
        self.assertEqual(held["outcome"], R.SECURITY_RECOVERY_RUNNING)
        self.assertIs(held["metadata_redacted"]["spawn_uncommitted"], True)

    def test_it_works_inside_a_real_state_transaction(self):
        # Step 6a's caller is not wired yet - tick integration is Step 6b -
        # so this proves the method behaves correctly under the EXISTING
        # exclusive-lock mechanism rather than asserting production
        # integration that does not exist.
        self.store.initialise("run-002", "v2.0")
        with self.store.transaction() as doc:
            providers.ensure(doc)
            task = state_mod.add_task(doc, "TASK-001", "T", [], "feature",
                                      False, TZ)
            task["state"] = "PR_OPEN"
            task["pr"] = 12
            doc["prs"]["12"] = routing.blank_pr_record(12, "TASK-001", "feat/x")
            result = self.sv.plan_security(
                doc, task, 12, SHA, observe(attempt_dir_exists=False,
                                            job_file_exists=False))
        self.assertIsNotNone(result)
        committed = self.store.read()
        claim = committed["prs"]["12"]["security_evidence"]
        self.assertEqual(claim["ordinal"], 1)
        self.assertEqual(R.security_claim_is_valid(claim), (True, ""))
        self.assertEqual(committed["tasks"]["TASK-001"]["state"],
                         "WAITING_EVIDENCE")

    def test_no_duplicate_claim_across_repeated_ticks(self):
        doc, task = self.doc_with_pr()
        first = self.plan(doc, task, observe(attempt_dir_exists=False,
                                             job_file_exists=False))
        # A second tick, now seeing the job file and a live worker.
        second = self.plan(doc, task, observe(worker_live=True))
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(
            doc["prs"]["12"]["security_evidence"]["ordinal"], 1)


if __name__ == "__main__":
    unittest.main()
