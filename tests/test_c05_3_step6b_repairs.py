"""C-05.3a Step 6b repairs: identity, provenance, ownership, retry, exclusion.

Five defects were reproduced against the pre-repair tree. Each one gets
behavioural coverage here that fails without its repair:

D1 PROVENANCE. Worker names carried no SHA and the ordinal restarts at 1 for
   a new head, so two attempts for different commits shared a name - and every
   worker artefact is keyed on that name. SHA-A's SECURITY_FAIL was published
   as SHA-B's durable outcome and ingested to FIX_REQUIRED, with no SHA-B
   review having run.
D2 PUBLICATION RETRY. A completed review whose publication kept failing was
   abandoned at lease expiry and re-dispatched as a second PAID review, while
   its own output sat readable on disk.
D3 SUPERSEDED OWNERSHIP. A stale-head claim was overwritten while its worker
   was still live, erasing the only durable identity that attempt had.
D4 SINGLETON. Exclusion was check-then-write: 16 of 40 racing pairs both
   started as supervisor.
D5 RESOURCE OWNERSHIP. A working attempt was reported ORPHAN_WORKTREE and
   ORPHAN_WORKER_PROCESS, because ownership was read only from doc["workers"]
   and a security attempt can never have a record there.

Everything uses temporary directories, mocked providers and mocked processes.
Nothing launches an experiment and nothing makes a paid call.
"""

from __future__ import annotations

import datetime
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from control import (  # noqa: E402
    clock,
    config,
    gate_evidence,
    ledger as ledger_mod,
    notify,
    providers,
    reconcile,
    routing,
    security_contract,
    state as state_mod,
    supervisor as sv_mod,
    workers as workers_mod,
)

TZ = "Pacific/Auckland"
SHA_A = "a" * 40
SHA_B = "c" * 40
TASK_TITLE = "Build the soundscape player with offline caching"
SURFACES = {s: "PASS" for s in routing.SECURITY_SURFACES}


def finding(**over):
    base = {"id": "S1", "severity": "P0", "surface": "SECRETS",
            "file": "app/api/route.ts", "summary": "names the defect",
            "evidence": "diff quote", "required_change": "change it"}
    base.update(over)
    return base


def verdict_block(verdict="SECURITY_PASS", findings=()):
    return "prose\n```json\n" + json.dumps(
        {"verdict": verdict, "surfaces": SURFACES,
         "findings": list(findings), "summary": "s"}) + "\n```\n"


# --------------------------------------------------------------- D4: singleton

GUARD_SOURCE = textwrap.dedent("""
    import sys, time
    from pathlib import Path
    sys.path.insert(0, {repo!r})
    from unittest import mock
    from control import config, supervisor as sv

    lock_path, barrier, pid_path = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
    while not barrier.exists():
        time.sleep(0.0005)
    s = sv.Supervisor.__new__(sv.Supervisor)
    s._singleton_lock = None
    s.log = lambda *a, **k: None
    with mock.patch.object(config, "SINGLETON_LOCK_PATH", lock_path), \\
            mock.patch.object(config, "PID_PATH", pid_path):
        ok = s._acquire_singleton()
    print("PROCEEDED" if ok else "REFUSED")
    if ok:
        time.sleep(float(sys.argv[4]))
""")


class SingletonExclusionCase(unittest.TestCase):
    """The real Supervisor._acquire_singleton, in real concurrent processes."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.script = self.root / "guard.py"
        self.script.write_text(
            GUARD_SOURCE.format(repo=str(Path(__file__).resolve().parent.parent)),
            encoding="utf-8")

    def _race(self, trials: int, hold: float = 0.3):
        lock = self.root / "supervisor.lock"
        pid = self.root / "supervisor.pid"
        outcomes = []
        for _ in range(trials):
            for path in (lock, pid, self.root / "go"):
                if path.exists():
                    path.unlink()
            procs = [subprocess.Popen(
                [sys.executable, str(self.script), str(lock), str(self.root / "go"),
                 str(pid), str(hold)], stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True) for _ in range(2)]
            time.sleep(0.25)
            (self.root / "go").touch()
            outcomes.append([p.communicate()[0].strip() for p in procs])
        return outcomes

    def test_two_concurrent_starts_never_both_proceed(self):
        # The pre-repair sequence produced 16 double-starts in 40 trials.
        results = self._race(trials=20)
        doubles = [r for r in results if r.count("PROCEEDED") == 2]
        self.assertEqual(doubles, [], "two supervisors started concurrently")
        for pair in results:
            self.assertEqual(sorted(pair), ["PROCEEDED", "REFUSED"],
                             "exactly one start must win every race")

    def test_the_lock_is_released_when_the_holder_is_killed(self):
        lock = self.root / "supervisor.lock"
        pid = self.root / "supervisor.pid"
        (self.root / "go").touch()
        holder = subprocess.Popen(
            [sys.executable, str(self.script), str(lock), str(self.root / "go"),
             str(pid), "30"], stdout=subprocess.PIPE, text=True)
        time.sleep(1.0)
        holder.kill()
        holder.communicate()   # reaps and closes the pipe, not just wait()
        # A crashed holder leaves no stale lock to clean up: the kernel drops
        # it with the process, which is the whole reason flock was chosen.
        follower = subprocess.run(
            [sys.executable, str(self.script), str(lock), str(self.root / "go"),
             str(pid), "0"], capture_output=True, text=True)
        self.assertEqual(follower.stdout.strip(), "PROCEEDED")

    def test_the_lock_inode_is_not_replaced_on_acquisition(self):
        # Replacing the inode would give a second process an independent
        # lock, which is exactly how atomic exclusion stops being exclusion.
        lock = self.root / "supervisor.lock"
        lock.write_text("", encoding="utf-8")
        before = lock.stat().st_ino
        sv = sv_mod.Supervisor.__new__(sv_mod.Supervisor)
        sv._singleton_lock = None
        sv.log = lambda *a, **k: None
        with mock.patch.object(sv_mod.config, "SINGLETON_LOCK_PATH", lock):
            self.assertTrue(sv._acquire_singleton())
        self.addCleanup(sv._singleton_lock.close)
        self.assertEqual(lock.stat().st_ino, before)

    def test_the_descriptor_is_not_inherited_by_children(self):
        sv = sv_mod.Supervisor.__new__(sv_mod.Supervisor)
        sv._singleton_lock = None
        sv.log = lambda *a, **k: None
        with mock.patch.object(sv_mod.config, "SINGLETON_LOCK_PATH",
                               self.root / "supervisor.lock"):
            self.assertTrue(sv._acquire_singleton())
        self.addCleanup(sv._singleton_lock.close)
        self.assertFalse(os.get_inheritable(sv._singleton_lock.fileno()))

    def test_pid_reporting_still_happens_for_the_watchdog(self):
        # watchdog.supervisor_pid and preflight both read PID_PATH. Exclusion
        # moved; reporting must not.
        from control import watchdog as watchdog_mod
        pid_path = self.root / "supervisor.pid"
        pid_path.write_text("4242", encoding="utf-8")
        with mock.patch.object(watchdog_mod.config, "PID_PATH", pid_path):
            self.assertEqual(watchdog_mod.supervisor_pid(), 4242)


# ------------------------------------------------------ shared Step 6b harness

class Step6bCase(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.evidence = self.root / "evidence"
        self.evidence.mkdir()
        self.logs = self.root / "workers"
        self.logs.mkdir()
        self.wt_root = self.root / "run-002__worktrees"
        self.wt_root.mkdir()
        for target in (gate_evidence.config, sv_mod.config, reconcile.config):
            for name, value in (("EVIDENCE_DIR", self.evidence),
                                ("WORKER_LOG_DIR", self.logs)):
                patch = mock.patch.object(target, name, value)
                patch.start()
                self.addCleanup(patch.stop)

        self.store = state_mod.Store(path=self.root / "state.json", tz=TZ)
        self.store.initialise("run-002", "v2.0")
        self.ledger = ledger_mod.Ledger(path=self.root / "ledger.jsonl", tz=TZ,
                                        experiment_id="run-002")
        self.sv = sv_mod.Supervisor.__new__(sv_mod.Supervisor)
        self.sv.cfg = SimpleNamespace(
            timezone=TZ, github_repo="o/r", tmux_session="run-002",
            roles={"security": SimpleNamespace(provider="codex", model=None,
                                               effort="medium")},
            max_security=1,
            extra={"timeouts": {"security": 1800, "lease_grace_seconds": 60}})
        self.sv.tz = TZ
        self.sv.ledger = self.ledger
        self.sv.store = self.store
        self.sv.stopping = False
        self.sv.notifier = mock.Mock(spec=notify.Notifier)

    # ----------------------------------------------------------- fixtures

    def claim(self, *, sha=SHA_A, ordinal=1, claim_state="SPAWNED",
              lease_offset=1800, task_id="TASK-001"):
        now = clock.now(TZ)
        if lease_offset > 0:
            claimed, expires = now, now + datetime.timedelta(seconds=lease_offset)
        else:
            expires = now + datetime.timedelta(seconds=lease_offset)
            claimed = expires - datetime.timedelta(seconds=1800)
        c = routing.security_claim(task_id=task_id, sha=sha, ordinal=ordinal,
                                   claimed_at=clock.iso(claimed),
                                   lease_expires_at=clock.iso(expires))
        c["claim_state"] = claim_state
        return c

    def doc(self, claim=None, state="WAITING_EVIDENCE", title=TASK_TITLE):
        doc = state_mod.initial_document("run-002", "v2.0")
        providers.ensure(doc)
        task = state_mod.add_task(doc, "TASK-001", title, [], "feature", False, TZ)
        task["state"] = state
        task["pr"] = 12
        doc["prs"]["12"] = routing.blank_pr_record(12, "TASK-001", "feat/x")
        if claim is not None:
            doc["prs"]["12"]["security_evidence"] = claim
        return doc

    def worktree_for(self, claim):
        return self.wt_root / claim["worker"]

    def materialise(self, claim, *, sha=SHA_A, pr=12, provenance=True):
        """The attempt directory as Phase C leaves it, with or without the
        provenance a successful worktree acquisition writes."""
        path = gate_evidence.security_attempt_dir("TASK-001", sha,
                                                  claim["attempt_id"])
        path.mkdir(parents=True, exist_ok=True)
        if provenance:
            wt = self.worktree_for(claim)
            wt.mkdir(parents=True, exist_ok=True)
            self.assertTrue(gate_evidence.write_security_provenance(path, {
                "task_id": "TASK-001", "pr": pr, "sha": sha,
                "attempt_id": claim["attempt_id"], "worker": claim["worker"],
                "worktree": str(wt)}))
        return path

    def ran(self, claim, output, phase="DONE"):
        (self.logs / f"{claim['worker']}.status.json").write_text(
            json.dumps({"phase": phase, "exit_code": 0, "duration_ms": 12.0}),
            encoding="utf-8")
        (self.logs / f"{claim['worker']}.last.txt").write_text(
            output, encoding="utf-8")
        (self.logs / f"{claim['worker']}.job.json").write_text(
            "{}", encoding="utf-8")

    def observe(self, doc, *, sha=SHA_A, entries=None):
        return self.sv.observe_security(
            doc, {12: sha}, {} if entries is None else entries)

    def events(self, event_type):
        lines = Path(self.ledger.path).read_text(encoding="utf-8").splitlines()
        return [json.loads(l) for l in lines
                if l and json.loads(l).get("event_type") == event_type]


# ------------------------------------------------------------ D1: provenance

class AttemptIdentityCase(Step6bCase):

    def test_attempts_for_different_shas_never_share_a_worker_name(self):
        a = self.claim(sha=SHA_A, ordinal=1)
        b = self.claim(sha=SHA_B, ordinal=1)
        self.assertNotEqual(a["worker"], b["worker"])
        self.assertIn(SHA_A, a["worker"])
        self.assertIn(SHA_B, b["worker"])
        # The attempt id legitimately repeats - the namespace is per SHA.
        self.assertEqual(a["attempt_id"], b["attempt_id"])

    def test_the_derived_name_stays_inside_the_worker_pattern(self):
        for ordinal in (1, 42, 9999):
            with self.subTest(ordinal=ordinal):
                name = self.claim(ordinal=ordinal)["worker"]
                self.assertTrue(routing.SECURITY_WORKER_RE.match(name), name)
                self.assertLessEqual(len(name), 64)

    def test_the_task_id_length_boundary_is_fail_closed(self):
        """The full SHA costs 40 characters, so the name has a real ceiling.

        At the limit a claim is minted normally. One character beyond it the
        claim is REFUSED - a visible, fail-closed error - rather than
        producing a truncated or unsafe name that would address the wrong
        attempt's files. Every task id in this run is eight characters, well
        inside the limit.
        """
        def mint(task_id):
            now = clock.now(TZ)
            return routing.security_claim(
                task_id=task_id, sha=SHA_A, ordinal=9999,
                claimed_at=clock.iso(now),
                lease_expires_at=clock.iso(
                    now + datetime.timedelta(seconds=60)))["worker"]

        at_limit = "t" * routing.SECURITY_TASK_ID_MAX
        name = mint(at_limit)
        self.assertEqual(len(name), 64)
        self.assertTrue(routing.SECURITY_WORKER_RE.match(name), name)
        self.assertNotIn("/", name)
        with self.assertRaises(ValueError):
            mint("t" * (routing.SECURITY_TASK_ID_MAX + 1))
        # Every real task id in this run is comfortably inside it.
        self.assertLessEqual(len("TASK-001"), routing.SECURITY_TASK_ID_MAX)

    def test_the_claim_validator_binds_worker_to_its_own_sha_and_ordinal(self):
        claim = self.claim()
        self.assertEqual(routing.security_claim_is_valid(claim), (True, ""))
        forged = dict(claim, worker=f"task-001-security-{SHA_B}-0001")
        self.assertEqual(routing.security_claim_is_valid(forged)[1],
                         "CLAIM_WORKER_PROVENANCE_MISMATCH")

    # ------------------------------------------- legacy record compatibility

    def legacy_claim(self):
        claim = self.claim()
        claim["worker"] = "task-001-security-0001"   # the pre-repair form
        return claim

    def test_a_legacy_identity_is_refused_with_its_own_finite_diagnostic(self):
        ok, why = routing.security_claim_is_valid(self.legacy_claim())
        self.assertFalse(ok)
        self.assertEqual(why, "CLAIM_WORKER_LEGACY_IDENTITY")

    def test_a_legacy_claim_holds_rather_than_dispatching_or_ingesting(self):
        legacy = self.legacy_claim()
        doc = self.doc(legacy)
        planned = self.sv.route_evidence(doc, self.observe(doc))
        self.assertEqual(planned, [], "a legacy claim must not be re-dispatched")
        held = self.events("SECURITY_EVIDENCE_HELD")
        self.assertEqual([e["outcome"] for e in held], ["UNKNOWN"])
        self.assertEqual(held[0]["metadata_redacted"]["claim_diagnostic"],
                         "CLAIM_WORKER_LEGACY_IDENTITY")

    def test_a_legacy_claims_identity_is_never_rewritten(self):
        # Rewriting a name out from under a possibly-live worker would strand
        # it: unreachable, still spending budget, owned by nothing.
        legacy = self.legacy_claim()
        doc = self.doc(legacy)
        before = dict(legacy)
        self.sv.route_evidence(doc, self.observe(doc))
        self.assertEqual(doc["prs"]["12"]["security_evidence"], before)

    def test_a_legacy_claims_worker_is_still_owned_for_orphan_purposes(self):
        doc = self.doc(self.legacy_claim())
        owned = reconcile._security_claimed_workers(doc)
        self.assertIn("task-001-security-0001", owned)


class PublicationProvenanceCase(Step6bCase):

    def test_stale_output_is_never_published_for_a_new_sha(self):
        """The exact reproduced path: SHA-A finished, the head moved, and
        SHA-B materialised but never acquired a worktree."""
        old = self.claim(sha=SHA_A)
        self.materialise(old, sha=SHA_A)
        self.ran(old, verdict_block("SECURITY_FAIL", [
            finding(file="app/sha-A-only.ts", summary="only in SHA-A")]))
        self.sv.publish_security_results(self.doc(old), {})
        self.assertIsNotNone(gate_evidence.read_security_outcome(
            "TASK-001", 12, SHA_A, old["attempt_id"]))

        # SHA-B: attempt directory made, worktree acquisition failed, so no
        # provenance was written and no SHA-B worker ever ran.
        new = self.claim(sha=SHA_B, claim_state="PLANNED")
        self.materialise(new, sha=SHA_B, provenance=False)
        doc = self.doc(new)
        self.sv.publish_security_results(doc, {})

        self.assertIsNone(
            gate_evidence.read_security_outcome("TASK-001", 12, SHA_B,
                                                new["attempt_id"]),
            "a SHA-B outcome was published from a SHA-A review")
        self.sv.route_evidence(doc, self.observe(doc, sha=SHA_B))
        self.assertNotIn("pending_findings", doc["prs"]["12"])
        self.assertNotEqual(doc["tasks"]["TASK-001"]["state"], "FIX_REQUIRED")

    def test_an_attempt_without_provenance_is_never_published(self):
        """Publication is driven BY provenance, so an attempt that never
        acquired a worktree has no identity to publish under and is simply
        not a candidate. Nothing is written and nothing is claimed about it."""
        claim = self.claim()
        self.materialise(claim, provenance=False)
        self.ran(claim, verdict_block())
        self.sv.publish_security_results(self.doc(claim), {})
        self.assertIsNone(gate_evidence.read_security_outcome(
            "TASK-001", 12, SHA_A, claim["attempt_id"]))
        self.assertEqual(self.events("SECURITY_OUTCOME_PUBLISHED"), [])

    def test_a_displaced_provenance_record_is_refused(self):
        """The record must agree with the directory it sits in.

        The path encodes task, full SHA and attempt independently of the
        file's contents, so a record naming a different attempt has been
        moved, copied or corrupted. Since the record IS the identity the
        publisher attributes output to, trusting a displaced one would
        credit a review to whatever it happened to name.
        """
        claim = self.claim()
        attempt = self.materialise(claim)
        (attempt / gate_evidence.SECURITY_PROVENANCE_NAME).unlink()
        gate_evidence.write_security_provenance(attempt, {
            "task_id": "TASK-001", "pr": 12, "sha": SHA_B,   # wrong directory
            "attempt_id": claim["attempt_id"], "worker": claim["worker"],
            "worktree": str(self.worktree_for(claim))})
        records, ok = gate_evidence.scan_security_attempts()
        self.assertEqual(records, [])
        self.assertFalse(ok, "a displaced record must not leave the walk complete")
        self.ran(claim, verdict_block())
        self.sv.publish_security_results(self.doc(claim), {})
        self.assertIsNone(gate_evidence.read_security_outcome(
            "TASK-001", 12, SHA_A, claim["attempt_id"]))
        self.assertEqual(self.events("SECURITY_OUTCOME_PUBLISHED"), [])

    def test_verified_provenance_publishes_normally(self):
        claim = self.claim()
        self.materialise(claim)
        self.ran(claim, verdict_block())
        self.sv.publish_security_results(self.doc(claim), {})
        self.assertEqual(
            [e["outcome"] for e in self.events("SECURITY_OUTCOME_PUBLISHED")],
            ["PUBLISHED"])

    def test_provenance_rewrite_is_idempotent_but_refuses_a_different_record(self):
        claim = self.claim()
        attempt = self.materialise(claim)
        same = {"task_id": "TASK-001", "pr": 12, "sha": SHA_A,
                "attempt_id": claim["attempt_id"], "worker": claim["worker"],
                "worktree": str(self.worktree_for(claim))}
        # A crash between acquisition and spawn is resumed by re-running
        # Phase C for the SAME attempt, which must not be refused.
        self.assertTrue(gate_evidence.write_security_provenance(attempt, same))
        self.assertFalse(gate_evidence.write_security_provenance(
            attempt, dict(same, worker="task-001-security-" + "f" * 40 + "-0001")))

    def test_a_malformed_provenance_record_is_not_usable(self):
        claim = self.claim()
        attempt = self.materialise(claim, provenance=False)
        (attempt / gate_evidence.SECURITY_PROVENANCE_NAME).write_text(
            "{ not json", encoding="utf-8")
        self.assertIsNone(gate_evidence.read_security_provenance(attempt))


# ------------------------------------------------ D3: superseded-attempt owner

class SupersededAttemptCase(Step6bCase):

    def test_a_live_stale_head_attempt_is_not_replaced(self):
        old = self.claim(sha=SHA_A)
        doc = self.doc(old)
        self.materialise(old)
        planned = self.sv.route_evidence(
            doc, self.observe(doc, sha=SHA_B, entries={old["worker"]: 4242}))
        self.assertEqual(planned, [])
        self.assertEqual(doc["prs"]["12"]["security_evidence"], old,
                         "a live attempt's claim was overwritten")
        self.assertEqual(
            [e["outcome"] for e in self.events("SECURITY_EVIDENCE_HELD")],
            ["STALE_HEAD_ATTEMPT_LIVE"])

    def test_unknown_liveness_also_blocks_replacement(self):
        old = self.claim(sha=SHA_A)
        doc = self.doc(old)
        self.materialise(old)
        observations = self.sv.observe_security(doc, {12: SHA_B}, None)
        self.assertEqual(self.sv.route_evidence(doc, observations), [])
        self.assertEqual(doc["prs"]["12"]["security_evidence"], old)
        self.assertEqual(
            [e["outcome"] for e in self.events("SECURITY_EVIDENCE_HELD")],
            ["STALE_HEAD_ATTEMPT_LIVE"])

    def test_a_dead_stale_head_attempt_is_replaced_as_before(self):
        old = self.claim(sha=SHA_A)
        doc = self.doc(old)
        self.materialise(old)
        planned = self.sv.route_evidence(doc, self.observe(doc, sha=SHA_B))
        self.assertEqual(len(planned), 1)
        fresh = doc["prs"]["12"]["security_evidence"]
        self.assertEqual(fresh["sha"], SHA_B)
        self.assertEqual(fresh["ordinal"], 1)

    def test_a_superseded_attempts_worktree_stays_owned_after_replacement(self):
        old = self.claim(sha=SHA_A)
        doc = self.doc(old)
        self.materialise(old)
        # Tick 1 registers ownership; tick 2 replaces the dead claim.
        self.sv.route_evidence(doc, self.observe(doc))
        owned = doc["tasks"]["TASK-001"]["retained_worktrees"]
        self.assertIn(str(self.worktree_for(old)), owned)
        self.sv.route_evidence(doc, self.observe(doc, sha=SHA_B))
        self.assertIn(str(self.worktree_for(old)),
                      doc["tasks"]["TASK-001"]["retained_worktrees"],
                      "replacing the claim dropped the old worktree's owner")


# ------------------------------------------------------- D2: publication retry

class PublicationRetryCase(Step6bCase):

    def expired_completed_attempt(self):
        claim = self.claim(lease_offset=-1)
        self.materialise(claim)
        self.ran(claim, verdict_block())
        return claim

    def test_a_failed_publication_past_lease_expiry_never_replaces(self):
        claim = self.expired_completed_attempt()
        doc = self.doc(claim)
        with mock.patch.object(sv_mod.gate_evidence,
                               "publish_security_outcome", return_value=False):
            self.sv.publish_security_results(doc, {})
        observations = self.observe(doc)
        self.assertEqual(
            routing.security_recovery_state(claim, observations[12][1]),
            routing.SECURITY_RECOVERY_PUBLICATION_PENDING)
        planned = self.sv.route_evidence(doc, observations)
        self.assertEqual(planned, [], "a second PAID review was dispatched")
        self.assertEqual(doc["prs"]["12"]["security_evidence"]["ordinal"], 1)
        self.assertEqual(self.events("SECURITY_ATTEMPT_ABANDONED"), [])
        self.assertEqual(
            [e["outcome"] for e in self.events("SECURITY_EVIDENCE_HELD")],
            ["PUBLICATION_PENDING"])

    def test_the_retry_publishes_the_original_review_not_a_new_one(self):
        claim = self.expired_completed_attempt()
        doc = self.doc(claim)
        with mock.patch.object(sv_mod.gate_evidence,
                               "publish_security_outcome", return_value=False):
            self.sv.publish_security_results(doc, {})
        self.sv.publish_security_results(doc, {})     # the write recovers
        outcome = gate_evidence.read_security_outcome(
            "TASK-001", 12, SHA_A, claim["attempt_id"])
        self.assertIsNotNone(outcome)
        self.assertEqual(outcome["attempt_id"], claim["attempt_id"])
        self.sv.route_evidence(doc, self.observe(doc))
        self.assertEqual(doc["prs"]["12"]["security_evidence"]["claim_state"],
                         "COMPLETE")
        self.assertEqual(doc["prs"]["12"]["security_evidence"]["verdict"],
                         security_contract.SECURITY_PASS)

    def test_a_genuinely_dead_attempt_with_no_output_still_advances(self):
        # PUBLICATION_PENDING must not swallow the ordinary recovery path.
        claim = self.claim(lease_offset=-1)
        self.materialise(claim)
        (self.logs / f"{claim['worker']}.job.json").write_text(
            "{}", encoding="utf-8")
        doc = self.doc(claim)
        planned = self.sv.route_evidence(doc, self.observe(doc))
        self.assertEqual(len(planned), 1)
        self.assertEqual(doc["prs"]["12"]["security_evidence"]["ordinal"], 2)

    def test_apparatus_failure_stays_apparatus_failure_through_retry(self):
        claim = self.claim(lease_offset=-1)
        self.materialise(claim)
        self.ran(claim, "", phase="TIMEOUT")
        doc = self.doc(claim)
        with mock.patch.object(sv_mod.gate_evidence,
                               "publish_security_outcome", return_value=False):
            self.sv.publish_security_results(doc, {})
        self.sv.publish_security_results(doc, {})
        self.sv.route_evidence(doc, self.observe(doc))
        stored = doc["prs"]["12"]["security_evidence"]
        self.assertEqual(stored["claim_state"], "COMPLETE")
        self.assertIsNone(stored["verdict"])
        self.assertEqual(stored["reason"], security_contract.TIMED_OUT)
        self.assertNotIn("pending_findings", doc["prs"]["12"])

    def test_ingestion_stays_idempotent_and_refuses_a_delayed_result(self):
        claim = self.claim()
        self.materialise(claim)
        self.ran(claim, verdict_block(findings=[finding(severity="P2")]))
        doc = self.doc(claim)
        self.sv.publish_security_results(doc, {})
        for _ in range(4):
            self.sv.route_evidence(doc, self.observe(doc))
        self.assertEqual(len(self.events("SECURITY_RESULT")), 1)
        self.assertEqual(len(doc["prs"]["12"]["security_debt"]), 1)

        # A late answer from a superseded attempt must change nothing.
        stale = {"task_id": "TASK-001", "pr": 12, "sha": SHA_A,
                 "attempt_id": "security-attempt-0009",
                 "status": gate_evidence.COMPLETED, "reason": "",
                 "verdict": security_contract.SECURITY_FAIL,
                 "surfaces": SURFACES, "findings": [],
                 "finding_counts": {"P0": 0, "P1": 0, "P2": 0, "P3": 0},
                 "summary": "", "provider": "codex", "model": None,
                 "exit_code": 0, "timed_out": False, "duration_ms": 1.0,
                 "stdout_name": "stdout.txt", "stderr_name": "stderr.txt"}
        self.sv.ingest_security(doc, doc["tasks"]["TASK-001"], 12, SHA_A, stale)
        self.assertEqual(len(self.events("SECURITY_RESULT")), 1)
        self.assertEqual(doc["tasks"]["TASK-001"]["state"], "WAITING_EVIDENCE")


# --------------------------------------------------- D5: resource lifecycle

class ResourceOwnershipCase(Step6bCase):

    def orphan_findings(self, doc, entries):
        with mock.patch.object(reconcile.proc, "worker_entry_processes",
                               return_value=entries), \
                mock.patch.object(reconcile, "registered_worktrees",
                                  return_value=(True, [
                                      str(p) for p in self.wt_root.iterdir()])), \
                mock.patch.object(reconcile.workers_mod,
                                  "managed_worktree_root",
                                  return_value=self.wt_root), \
                mock.patch.object(reconcile.hostcheck,
                                  "read_candidate_port_range",
                                  side_effect=reconcile.hostcheck.HostCheckError("n/a")):
            findings, _ = reconcile.detect_orphans(doc, repo_root=self.root)
        return {(f.check_id, f.resource_id) for f in findings}

    def test_a_running_attempt_is_owned_not_orphaned(self):
        claim = self.claim()
        self.materialise(claim)
        doc = self.doc(claim)
        self.sv.route_evidence(doc, self.observe(doc,
                                                 entries={claim["worker"]: 1}))
        found = self.orphan_findings(doc, {claim["worker"]: 4242})
        self.assertNotIn(("ORPHAN_WORKER_PROCESS", claim["worker"]), found)
        self.assertNotIn(("ORPHAN_WORKTREE", str(self.worktree_for(claim))),
                         found)

    def test_ownership_holds_through_publication_retry(self):
        claim = self.claim()
        self.materialise(claim)
        self.ran(claim, verdict_block())
        doc = self.doc(claim)
        self.sv.route_evidence(doc, self.observe(doc))
        with mock.patch.object(sv_mod.gate_evidence,
                               "publish_security_outcome", return_value=False):
            self.sv.publish_security_results(doc, {})
        found = self.orphan_findings(doc, {})
        self.assertNotIn(("ORPHAN_WORKTREE", str(self.worktree_for(claim))),
                         found)

    def test_a_complete_claim_still_owns_a_worktree_that_exists(self):
        claim = self.claim()
        self.materialise(claim)
        self.ran(claim, verdict_block())
        doc = self.doc(claim)
        self.sv.publish_security_results(doc, {})
        self.sv.route_evidence(doc, self.observe(doc))
        self.assertEqual(doc["prs"]["12"]["security_evidence"]["claim_state"],
                         "COMPLETE")
        self.assertTrue(self.worktree_for(claim).exists())
        found = self.orphan_findings(doc, {})
        self.assertNotIn(("ORPHAN_WORKTREE", str(self.worktree_for(claim))),
                         found)
        self.assertIn(str(self.worktree_for(claim)),
                      doc["tasks"]["TASK-001"]["retained_worktrees"])

    def test_ownership_survives_a_crash_before_phase_d(self):
        # Phase C acquired and spawned; the process died before Phase D could
        # cache anything. The next tick must still recognise the resource.
        claim = self.claim(claim_state="PLANNED")
        self.materialise(claim)
        doc = self.doc(claim)
        self.assertNotIn("retained_worktrees", doc["tasks"]["TASK-001"])
        self.sv.route_evidence(doc, self.observe(doc,
                                                 entries={claim["worker"]: 1}))
        self.assertIn(str(self.worktree_for(claim)),
                      doc["tasks"]["TASK-001"]["retained_worktrees"])

    def test_an_attempt_that_never_acquired_owns_nothing(self):
        claim = self.claim(claim_state="PLANNED")
        self.materialise(claim, provenance=False)
        doc = self.doc(claim)
        self.sv.route_evidence(doc, self.observe(doc))
        self.assertEqual(doc["tasks"]["TASK-001"].get("retained_worktrees", {}),
                         {})

    # ------------------------------------------------------------ Phase F

    def completed_attempt(self):
        claim = self.claim()
        self.materialise(claim)
        self.ran(claim, verdict_block())
        doc = self.doc(claim)
        self.sv.publish_security_results(doc, {})
        self.sv.route_evidence(doc, self.observe(doc))
        with self.store.transaction() as stored:
            stored.update(doc)
        return claim, doc

    def test_release_removes_the_worktree_and_its_ownership(self):
        claim, doc = self.completed_attempt()
        with mock.patch.object(sv_mod.workers, "remove_worker",
                               return_value=SimpleNamespace(ok=True)) as rm:
            self.sv.release_security_worktrees(self.store.read(), {})
        rm.assert_called_once_with(claim["worker"])
        after = self.store.read()["tasks"]["TASK-001"]["retained_worktrees"]
        self.assertNotIn(str(self.worktree_for(claim)), after)
        self.assertEqual(
            [e["outcome"] for e in self.events("SECURITY_WORKTREE_RELEASED")],
            ["RELEASED"])

    def test_release_never_happens_while_the_worker_is_live(self):
        claim, _ = self.completed_attempt()
        with mock.patch.object(sv_mod.workers, "remove_worker") as rm:
            self.sv.release_security_worktrees(self.store.read(),
                                               {claim["worker"]: 4242})
        rm.assert_not_called()

    def test_release_never_happens_when_liveness_is_unknown(self):
        self.completed_attempt()
        with mock.patch.object(sv_mod.workers, "remove_worker") as rm:
            self.sv.release_security_worktrees(self.store.read(), None)
        rm.assert_not_called()

    def test_release_never_happens_before_the_outcome_is_durable(self):
        claim = self.claim()
        self.materialise(claim)
        self.ran(claim, verdict_block())
        doc = self.doc(claim)
        self.sv.route_evidence(doc, self.observe(doc))   # ownership only
        with self.store.transaction() as stored:
            stored.update(doc)
        self.assertIsNone(gate_evidence.read_security_outcome(
            "TASK-001", 12, SHA_A, claim["attempt_id"]))
        with mock.patch.object(sv_mod.workers, "remove_worker") as rm:
            self.sv.release_security_worktrees(self.store.read(), {})
        rm.assert_not_called()

    def test_a_vanished_outcome_blocks_release_even_when_the_claim_is_complete(self):
        """The publication precondition, isolated.

        A mutation that deleted it survived the test above, because that
        attempt was also blocked by its claim not yet being COMPLETE - the
        test proved "release refused", not "refused BY the publication
        rule". This builds the one state where only the publication rule can
        answer: the claim reached COMPLETE, and the durable outcome has since
        gone from disk.

        Refusing is the conservative answer. "Publication is durable" is the
        precondition, and once it stops being true, removing the worktree
        destroys the last physical trace of a review whose answer the control
        plane can no longer read.
        """
        claim, _ = self.completed_attempt()
        stored = self.store.read()
        self.assertEqual(
            stored["prs"]["12"]["security_evidence"]["claim_state"], "COMPLETE")
        outcome_path = (gate_evidence.security_attempt_dir(
            "TASK-001", SHA_A, claim["attempt_id"])
            / gate_evidence.SECURITY_OUTCOME_NAME)
        outcome_path.unlink()
        with mock.patch.object(sv_mod.workers, "remove_worker") as rm:
            self.sv.release_security_worktrees(self.store.read(), {})
        rm.assert_not_called()
        self.assertIn(str(self.worktree_for(claim)),
                      self.store.read()["tasks"]["TASK-001"]["retained_worktrees"])

    def test_a_superseded_unpublished_attempt_is_retained_not_released(self):
        """A known, bounded limitation, pinned so it cannot drift silently.

        The governing rule is that a worktree is released only once its
        outcome publication is durable. A superseded attempt whose
        publication never succeeded therefore keeps its worktree: the
        publisher only ever revisits the CURRENT claim, so nothing will
        publish it, and the precondition can never be met.

        What this is NOT is an unowned leak. The retained_worktrees entry
        stays, so the Watchdog never reports it as an orphan and an operator
        can see exactly what is held and why. Whether such an attempt may be
        released without a durable outcome is a governance decision, not
        something to settle by quietly relaxing the rule - see the handover.
        """
        old = self.claim(sha=SHA_A)
        self.materialise(old)
        doc = self.doc(old)
        self.sv.route_evidence(doc, self.observe(doc))             # own it
        self.sv.route_evidence(doc, self.observe(doc, sha=SHA_B))  # supersede
        with self.store.transaction() as stored:
            stored.update(doc)
        self.assertIsNone(gate_evidence.read_security_outcome(
            "TASK-001", 12, SHA_A, old["attempt_id"]))
        with mock.patch.object(sv_mod.workers, "remove_worker") as rm:
            self.sv.release_security_worktrees(self.store.read(), {})
        rm.assert_not_called()
        retained = self.store.read()["tasks"]["TASK-001"]["retained_worktrees"]
        self.assertIn(str(self.worktree_for(old)), retained)
        self.assertEqual(retained[str(self.worktree_for(old))]["why"],
                         sv_mod.SECURITY_WORKTREE_WHY)

    def test_a_failed_release_stays_owned_durable_and_retryable(self):
        claim, _ = self.completed_attempt()
        with mock.patch.object(sv_mod.workers, "remove_worker",
                               return_value=SimpleNamespace(ok=False)):
            self.sv.release_security_worktrees(self.store.read(), {})
        self.assertIn(str(self.worktree_for(claim)),
                      self.store.read()["tasks"]["TASK-001"]["retained_worktrees"])
        self.assertEqual(
            [e["outcome"] for e in self.events("SECURITY_WORKTREE_RELEASE_FAILED")],
            ["REMOVE_FAILED"])
        # The next tick retries and succeeds.
        with mock.patch.object(sv_mod.workers, "remove_worker",
                               return_value=SimpleNamespace(ok=True)):
            self.sv.release_security_worktrees(self.store.read(), {})
        self.assertNotIn(
            str(self.worktree_for(claim)),
            self.store.read()["tasks"]["TASK-001"]["retained_worktrees"])

    def test_an_incomplete_evidence_walk_releases_nothing(self):
        self.completed_attempt()
        with mock.patch.object(sv_mod.gate_evidence, "scan_security_attempts",
                               return_value=([], False)), \
                mock.patch.object(sv_mod.workers, "remove_worker") as rm:
            self.sv.release_security_worktrees(self.store.read(), {})
        rm.assert_not_called()

    def test_release_only_drops_its_own_retained_entries(self):
        claim, doc = self.completed_attempt()
        builder_path = str(self.wt_root / "task-001-builder")
        with self.store.transaction() as stored:
            stored["tasks"]["TASK-001"]["retained_worktrees"][builder_path] = {
                "retained_at": clock.iso(clock.now(TZ)), "why": "TERMINAL_REAP"}
        with mock.patch.object(sv_mod.workers, "remove_worker",
                               return_value=SimpleNamespace(ok=True)):
            self.sv.release_security_worktrees(self.store.read(), {})
        after = self.store.read()["tasks"]["TASK-001"]["retained_worktrees"]
        self.assertIn(builder_path, after)
        self.assertNotIn(str(self.worktree_for(claim)), after)

    def test_the_generic_reaper_never_touches_a_security_attempt(self):
        # A security worker has no doc["workers"] record by construction, so
        # reap_workers - which runs inside T1 - cannot drop ownership during
        # the publication-retry window.
        claim, doc = self.completed_attempt()
        self.sv.telemetry = mock.Mock()
        before = dict(doc["tasks"]["TASK-001"]["retained_worktrees"])
        self.sv.reap_workers(doc)
        self.assertEqual(doc["tasks"]["TASK-001"]["retained_worktrees"], before)
        self.assertEqual(doc["workers"], {})


# ------------------------------------------------------------- max_security

class CapacityCase(Step6bCase):

    def two_pr_doc(self):
        doc = state_mod.initial_document("run-002", "v2.0")
        providers.ensure(doc)
        for task_id, pr in (("TASK-001", 12), ("TASK-002", 13)):
            task = state_mod.add_task(doc, task_id, TASK_TITLE, [], "feature",
                                      False, TZ)
            task["state"] = "WAITING_EVIDENCE"
            task["pr"] = pr
            doc["prs"][str(pr)] = routing.blank_pr_record(pr, task_id, "feat/x")
        return doc

    def test_a_second_concurrent_attempt_is_refused_at_max_security_one(self):
        doc = self.two_pr_doc()
        doc["prs"]["12"]["security_evidence"] = self.claim()
        self.assertFalse(self.sv._security_slots_free(doc, 13))
        self.assertTrue(self.sv._security_slots_free(doc, 12))

    def test_a_stale_head_attempt_still_consumes_capacity(self):
        doc = self.two_pr_doc()
        doc["prs"]["12"]["security_evidence"] = self.claim(sha=SHA_B)
        self.assertFalse(self.sv._security_slots_free(doc, 13))

    def test_unknown_liveness_does_not_free_capacity(self):
        # The count reads claim_state, never the /proc scan, so a failed scan
        # cannot silently release a slot.
        doc = self.two_pr_doc()
        doc["prs"]["12"]["security_evidence"] = self.claim(claim_state="SPAWNED")
        observations = self.sv.observe_security(doc, {13: SHA_A}, None)
        self.assertFalse(self.sv._security_slots_free(doc, 13))
        self.assertEqual(self.sv.route_evidence(doc, observations), [])

    def test_a_completed_attempt_frees_capacity(self):
        doc = self.two_pr_doc()
        doc["prs"]["12"]["security_evidence"] = self.claim(claim_state="COMPLETE")
        self.assertTrue(self.sv._security_slots_free(doc, 13))

    def test_capacity_is_reported_and_holds_the_second_pr(self):
        doc = self.two_pr_doc()
        doc["prs"]["12"]["security_evidence"] = self.claim()
        observations = self.sv.observe_security(doc, {12: SHA_A, 13: SHA_A}, {})
        self.sv.route_evidence(doc, observations)
        held = [e for e in self.events("SECURITY_EVIDENCE_HELD")
                if e["outcome"] == "AT_CAPACITY"]
        self.assertEqual(len(held), 1)
        self.assertEqual(held[0]["metadata_redacted"]["max_security"], 1)
        self.assertIsNone(doc["prs"]["13"].get("security_evidence"))

    def test_resuming_an_existing_attempt_is_never_capacity_blocked(self):
        # One PR record holds one claim, so resuming it adds no concurrency.
        # Counting it would strand every attempt at max_security = 1.
        claim = self.claim(claim_state="PLANNED")
        doc = self.doc(claim)
        planned = self.sv.route_evidence(doc, self.observe(doc))
        self.assertEqual(len(planned), 1)
        self.assertEqual(planned[0].attempt_id, claim["attempt_id"])

    def test_the_cap_comes_from_the_validated_configuration(self):
        loaded = json.loads(
            (Path(__file__).resolve().parent.parent
             / "config" / "experiment.json").read_text(encoding="utf-8"))
        self.assertEqual(loaded["concurrency"]["max_security"], 1)
        self.assertIn("max_security",
                      config.ExperimentConfig.__dataclass_fields__)
        # It must arrive through the same validated load path as every other
        # concurrency key, not through a default nothing checks.
        self.assertEqual(config.load().max_security, 1)


# ------------------------------------------------- transaction-boundary guard

class TransactionBoundaryCase(Step6bCase):

    def test_t1_performs_no_external_work(self):
        """Every phase that touches git, a provider, a process or a file runs
        with no lock held. C-18 must not be extended by these repairs."""
        claim = self.claim(claim_state="PLANNED")
        self.materialise(claim)
        doc = self.doc(claim)
        observations = self.observe(doc)
        with mock.patch.object(sv_mod.workers, "acquire_worktree") as acquire, \
                mock.patch.object(sv_mod.workers, "start_job") as start, \
                mock.patch.object(sv_mod.workers, "write_job") as write, \
                mock.patch.object(sv_mod.workers, "remove_worker") as remove, \
                mock.patch.object(sv_mod.gate_evidence,
                                  "write_security_provenance") as prov, \
                mock.patch.object(sv_mod.gate_evidence,
                                  "publish_security_outcome") as publish:
            self.sv.route_evidence(doc, observations)
        for spy in (acquire, start, write, remove, prov, publish):
            spy.assert_not_called()

    def test_phase_f_holds_no_lock_while_removing(self):
        claim = self.claim()
        self.materialise(claim)
        self.ran(claim, verdict_block())
        doc = self.doc(claim)
        self.sv.publish_security_results(doc, {})
        self.sv.route_evidence(doc, self.observe(doc))
        with self.store.transaction() as stored:
            stored.update(doc)

        inside = []

        def watching_remove(name):
            # The state file must be writable by anyone while this runs.
            inside.append(self.store.read() is not None)
            return SimpleNamespace(ok=True)

        with mock.patch.object(sv_mod.workers, "remove_worker",
                               watching_remove):
            self.sv.release_security_worktrees(self.store.read(), {})
        self.assertEqual(inside, [True])


# ---------------------------------------- same-prefix full-SHA collision

class ShaPrefixCollisionCase(Step6bCase):
    """Two DISTINCT commits whose first twelve characters are identical.

    A twelve-character prefix is not globally unique. With the name built
    from a prefix, the attempt DIRECTORY stayed distinct (it is keyed by the
    full SHA) while the worker NAME collided - and the per-attempt provenance
    record could not catch it, because each attempt's record truthfully named
    its own full SHA. One commit's SECURITY_FAIL was published as the other's
    durable outcome. Probability is not the guarantee; the full SHA is.
    """

    PREFIX = "abc123def456"
    COLL_A = PREFIX + "0" * 28
    COLL_B = PREFIX + "1" * 28

    def setUp(self):
        super().setUp()
        self.assertNotEqual(self.COLL_A, self.COLL_B)
        self.assertEqual(self.COLL_A[:12], self.COLL_B[:12])

    def test_same_prefix_commits_get_distinct_worker_identities(self):
        a = self.claim(sha=self.COLL_A)
        b = self.claim(sha=self.COLL_B)
        self.assertNotEqual(a["worker"], b["worker"],
                            "two distinct commits share one worker identity")
        # Same attempt namespace, legitimately - it is scoped per SHA.
        self.assertEqual(a["attempt_id"], b["attempt_id"])
        self.assertNotEqual(
            gate_evidence.security_attempt_dir("TASK-001", self.COLL_A,
                                               a["attempt_id"]),
            gate_evidence.security_attempt_dir("TASK-001", self.COLL_B,
                                               b["attempt_id"]))

    def test_every_worker_artefact_path_differs_between_the_two(self):
        a = self.claim(sha=self.COLL_A)
        b = self.claim(sha=self.COLL_B)
        for suffix in (".job.json", ".status.json", ".last.txt", ".out",
                       ".entry.log"):
            with self.subTest(suffix=suffix):
                self.assertNotEqual(a["worker"] + suffix,
                                    b["worker"] + suffix)

    def test_the_older_commits_output_cannot_be_attributed_to_the_newer(self):
        old = self.claim(sha=self.COLL_A)
        self.materialise(old, sha=self.COLL_A)
        self.ran(old, verdict_block("SECURITY_FAIL", [
            finding(file="app/ONLY-IN-A.ts", summary="only in the first commit")]))

        new = self.claim(sha=self.COLL_B, claim_state="PLANNED")
        self.materialise(new, sha=self.COLL_B)     # its own dir and provenance
        doc = self.doc(new)
        self.sv.publish_security_results(doc, {})

        published = gate_evidence.read_security_outcome(
            "TASK-001", 12, self.COLL_B, new["attempt_id"])
        self.assertIsNone(
            published,
            "the first commit's review was published as the second's outcome")
        self.sv.route_evidence(doc, self.observe(doc, sha=self.COLL_B))
        self.assertNotIn("pending_findings", doc["prs"]["12"])
        self.assertNotEqual(doc["tasks"]["TASK-001"]["state"], "FIX_REQUIRED")

    def test_each_commit_publishes_its_own_answer_under_its_own_identity(self):
        outcomes = {}
        for sha, defect in ((self.COLL_A, "app/ONLY-IN-A.ts"),
                            (self.COLL_B, "app/ONLY-IN-B.ts")):
            claim = self.claim(sha=sha)
            self.materialise(claim, sha=sha)
            self.ran(claim, verdict_block("SECURITY_FAIL",
                                          [finding(file=defect)]))
            self.sv.publish_security_results(self.doc(claim), {})
            outcomes[sha] = gate_evidence.read_security_outcome(
                "TASK-001", 12, sha, claim["attempt_id"])
        self.assertEqual(
            [f["file"] for f in outcomes[self.COLL_A]["findings"]],
            ["app/ONLY-IN-A.ts"])
        self.assertEqual(
            [f["file"] for f in outcomes[self.COLL_B]["findings"]],
            ["app/ONLY-IN-B.ts"])

    def test_the_two_cannot_be_live_under_one_worker_identity(self):
        # Simultaneous use would collide on the job file and the worktree.
        # Distinct names make that impossible; the stale-head guard then
        # stops the second from even being claimed while the first runs.
        old = self.claim(sha=self.COLL_A)
        self.materialise(old, sha=self.COLL_A)
        doc = self.doc(old)
        planned = self.sv.route_evidence(
            doc, self.observe(doc, sha=self.COLL_B,
                              entries={old["worker"]: 4242}))
        self.assertEqual(planned, [])
        self.assertEqual(doc["prs"]["12"]["security_evidence"]["sha"],
                         self.COLL_A)
        self.assertEqual(
            [e["outcome"] for e in self.events("SECURITY_EVIDENCE_HELD")],
            ["STALE_HEAD_ATTEMPT_LIVE"])


# ------------------------------------- superseded publication retry (round 2)

class SupersededPublicationCase(Step6bCase):
    """A superseded attempt's answer still gets written, then cleaned up.

    Publication used to be driven by the CURRENT claim, so an attempt whose
    claim had been replaced was never revisited: its answer stayed unwritten,
    and because a worktree may only be released once publication is durable,
    its worktree was retained for the rest of the run. Retry resolves that
    rather than leaving it as permanent lifecycle behaviour.
    """

    def superseded_attempt(self):
        """SHA-A finished, its publication failed, the head then moved."""
        old = self.claim(sha=SHA_A)
        self.materialise(old)
        self.ran(old, verdict_block("SECURITY_FAIL", [
            finding(file="app/only-in-sha-a.ts", summary="only in SHA-A")]))
        doc = self.doc(old)
        with mock.patch.object(sv_mod.gate_evidence,
                               "publish_security_outcome", return_value=False):
            self.sv.publish_security_results(doc, {})
        self.sv.route_evidence(doc, self.observe(doc))             # own it
        self.sv.route_evidence(doc, self.observe(doc, sha=SHA_B))  # supersede
        with self.store.transaction() as stored:
            stored.update(doc)
        self.assertNotEqual(
            doc["prs"]["12"]["security_evidence"]["sha"], SHA_A,
            "the claim should have moved on to the new head")
        return old, doc

    def test_a_superseded_attempt_is_retried_and_published(self):
        old, doc = self.superseded_attempt()
        self.assertIsNone(gate_evidence.read_security_outcome(
            "TASK-001", 12, SHA_A, old["attempt_id"]))
        self.sv.publish_security_results(self.store.read(), {})
        outcome = gate_evidence.read_security_outcome(
            "TASK-001", 12, SHA_A, old["attempt_id"])
        self.assertIsNotNone(outcome, "a superseded attempt was never retried")
        self.assertEqual(outcome["sha"], SHA_A)
        self.assertEqual(outcome["attempt_id"], old["attempt_id"])

    def test_it_publishes_under_its_original_identity_not_the_current_one(self):
        old, doc = self.superseded_attempt()
        self.sv.publish_security_results(self.store.read(), {})
        current = self.store.read()["prs"]["12"]["security_evidence"]
        self.assertEqual(current["sha"], SHA_B)
        # Nothing was written under the CURRENT head.
        self.assertIsNone(gate_evidence.read_security_outcome(
            "TASK-001", 12, SHA_B, current["attempt_id"]))
        published = [e for e in self.events("SECURITY_OUTCOME_PUBLISHED")
                     if e["outcome"] == "PUBLISHED"]
        self.assertEqual([e["metadata_redacted"]["head"] for e in published],
                         [SHA_A])

    def test_a_stale_outcome_never_touches_the_current_task(self):
        old, doc = self.superseded_attempt()
        self.sv.publish_security_results(self.store.read(), {})
        live = self.store.read()
        before = dict(live["tasks"]["TASK-001"])
        self.sv.route_evidence(live, self.observe(live, sha=SHA_B))
        record = live["prs"]["12"]
        self.assertNotIn("pending_findings", record)
        self.assertEqual(record.get("security_debt", []), [])
        self.assertEqual(live["tasks"]["TASK-001"]["state"], before["state"])
        self.assertEqual(record["security_evidence"]["verdict"], None)
        self.assertEqual(self.events("SECURITY_RESULT"), [])

    def test_retry_survives_a_restart_and_then_cleans_up(self):
        old, doc = self.superseded_attempt()
        # A fresh Supervisor, as a restart gives: no in-memory carry-over.
        restarted = sv_mod.Supervisor.__new__(sv_mod.Supervisor)
        for attr in ("cfg", "tz", "ledger", "store", "notifier", "stopping"):
            setattr(restarted, attr, getattr(self.sv, attr))
        restarted.publish_security_results(self.store.read(), {})
        self.assertIsNotNone(gate_evidence.read_security_outcome(
            "TASK-001", 12, SHA_A, old["attempt_id"]))
        with mock.patch.object(sv_mod.workers, "remove_worker",
                               return_value=SimpleNamespace(ok=True)) as rm:
            restarted.release_security_worktrees(self.store.read(), {})
        rm.assert_any_call(old["worker"])
        self.assertNotIn(
            str(self.worktree_for(old)),
            self.store.read()["tasks"]["TASK-001"]["retained_worktrees"])

    def test_cleanup_waits_for_publication_even_when_superseded(self):
        old, _ = self.superseded_attempt()
        with mock.patch.object(sv_mod.workers, "remove_worker") as rm:
            self.sv.release_security_worktrees(self.store.read(), {})
        rm.assert_not_called()
        self.assertIn(str(self.worktree_for(old)),
                      self.store.read()["tasks"]["TASK-001"]["retained_worktrees"])

    def test_a_live_superseded_worker_is_neither_published_nor_released(self):
        old, _ = self.superseded_attempt()
        with mock.patch.object(sv_mod.workers, "remove_worker") as rm:
            self.sv.publish_security_results(self.store.read(),
                                             {old["worker"]: 4242})
            self.sv.release_security_worktrees(self.store.read(),
                                               {old["worker"]: 4242})
        self.assertIsNone(gate_evidence.read_security_outcome(
            "TASK-001", 12, SHA_A, old["attempt_id"]))
        rm.assert_not_called()

    def test_unknown_liveness_blocks_superseded_publication_and_release(self):
        old, _ = self.superseded_attempt()
        with mock.patch.object(sv_mod.workers, "remove_worker") as rm:
            self.sv.publish_security_results(self.store.read(), None)
            self.sv.release_security_worktrees(self.store.read(), None)
        self.assertIsNone(gate_evidence.read_security_outcome(
            "TASK-001", 12, SHA_A, old["attempt_id"]))
        rm.assert_not_called()

    def test_a_failed_cleanup_after_retry_stays_owned_and_retryable(self):
        old, _ = self.superseded_attempt()
        self.sv.publish_security_results(self.store.read(), {})
        with mock.patch.object(sv_mod.workers, "remove_worker",
                               return_value=SimpleNamespace(ok=False)):
            self.sv.release_security_worktrees(self.store.read(), {})
        self.assertIn(str(self.worktree_for(old)),
                      self.store.read()["tasks"]["TASK-001"]["retained_worktrees"])
        self.assertEqual(
            [e["outcome"] for e in self.events("SECURITY_WORKTREE_RELEASE_FAILED")],
            ["REMOVE_FAILED"])
        with mock.patch.object(sv_mod.workers, "remove_worker",
                               return_value=SimpleNamespace(ok=True)):
            self.sv.release_security_worktrees(self.store.read(), {})
        self.assertNotIn(
            str(self.worktree_for(old)),
            self.store.read()["tasks"]["TASK-001"]["retained_worktrees"])

    def test_retry_never_dispatches_a_replacement_paid_review(self):
        old, doc = self.superseded_attempt()
        self.sv.publish_security_results(self.store.read(), {})
        dispatched = [e for e in self.events("SECURITY_DISPATCHED")]
        self.assertEqual(dispatched, [])
        self.assertEqual(self.events("SECURITY_ATTEMPT_ABANDONED"), [])


# -------------------------------------------- existing dispatch controls

class DispatchControlCase(Step6bCase):
    """Security dispatch obeys the controls the repository already has.

    None of these is invented here: frozen_at gates dispatchable(), stopping
    is the shutdown flag run() already honours, and providers.may(doc,
    "review") is exactly what dispatch_reviewer uses for review work.
    """

    def fresh(self):
        doc = self.doc()
        return doc, self.observe(doc)

    def assert_blocked(self, doc, observations, outcome):
        planned = self.sv.route_evidence(doc, observations)
        self.assertEqual(planned, [])
        self.assertIsNone(doc["prs"]["12"].get("security_evidence"),
                          "a claim was made while dispatch was blocked")
        self.assertIn(outcome,
                      [e["outcome"] for e in self.events("SECURITY_EVIDENCE_HELD")])

    def test_a_frozen_run_makes_no_new_security_attempt(self):
        doc, observations = self.fresh()
        doc["frozen_at"] = clock.iso(clock.now(TZ))
        self.assert_blocked(doc, observations, "RUN_FROZEN")

    def test_a_stopping_supervisor_makes_no_new_security_attempt(self):
        doc, observations = self.fresh()
        self.sv.stopping = True
        self.assert_blocked(doc, observations, "SUPERVISOR_STOPPING")

    def test_review_not_permitted_blocks_new_security_attempts(self):
        doc, observations = self.fresh()
        with mock.patch.object(sv_mod.providers, "may",
                               side_effect=lambda d, c: c != "review"):
            self.assert_blocked(doc, observations, "REVIEW_NOT_PERMITTED")

    def test_an_unavailable_security_provider_blocks_new_attempts(self):
        doc, observations = self.fresh()
        with mock.patch.object(sv_mod.providers, "usable", return_value=False):
            self.assert_blocked(doc, observations, "PROVIDER_UNAVAILABLE")

    def test_codex_in_cooldown_blocks_through_the_real_state_machine(self):
        """No mocks: the configured provider really in COOLDOWN.

        It blocks at the review capability rather than the provider check,
        because codex's own paused policy sets allows_review=False - "PRs
        queue for review; no self-review is ever permitted". Asserting
        PROVIDER_UNAVAILABLE here would be asserting a reason the policy
        never reaches.
        """
        doc, observations = self.fresh()
        doc["providers"]["codex"]["state"] = providers.COOLDOWN
        self.assertFalse(providers.may(doc, "review"))
        self.assert_blocked(doc, observations, "REVIEW_NOT_PERMITTED")

    def test_the_provider_check_is_reachable_for_a_review_allowing_policy(self):
        """The usable() gate is not dead code - it depends on configuration.

        roles.security.provider is a config value. Pointed at a provider
        whose paused policy still permits review (grok: allows_review=True),
        the capability gate passes and the provider check is the one thing
        standing between a cooling-down provider and a dispatch to it.
        """
        doc, observations = self.fresh()
        self.sv.cfg.roles["security"] = SimpleNamespace(
            provider="grok", model=None, effort="medium")
        doc["providers"]["grok"]["state"] = providers.COOLDOWN
        self.assertTrue(providers.may(doc, "review"))
        self.assert_blocked(doc, observations, "PROVIDER_UNAVAILABLE")

    def test_a_stopping_supervisor_spawns_nothing_in_phase_c(self):
        doc = self.doc()
        planned = self.sv.route_evidence(doc, self.observe(doc))
        self.assertEqual(len(planned), 1)
        self.sv.stopping = True          # the signal arrives after T1 commits
        with mock.patch.object(sv_mod.workers, "acquire_worktree") as acquire, \
                mock.patch.object(sv_mod.workers, "start_job") as start:
            self.assertEqual(self.sv.execute_security(planned), [])
        acquire.assert_not_called()
        start.assert_not_called()
        self.assertEqual(
            [e["outcome"] for e in self.events("SECURITY_EXECUTION_FAILED")],
            ["SUPERVISOR_STOPPING"])

    # ---------------- evidence must keep flowing while dispatch is blocked

    def test_publication_and_ingestion_continue_while_dispatch_is_blocked(self):
        claim = self.claim()
        self.materialise(claim)
        self.ran(claim, verdict_block(findings=[finding(severity="P2")]))
        doc = self.doc(claim)
        doc["frozen_at"] = clock.iso(clock.now(TZ))
        self.sv.stopping = True
        with mock.patch.object(sv_mod.providers, "may", return_value=False):
            self.sv.publish_security_results(doc, {})
            self.sv.route_evidence(doc, self.observe(doc))
        stored = doc["prs"]["12"]["security_evidence"]
        self.assertEqual(stored["claim_state"], "COMPLETE")
        self.assertEqual(stored["verdict"], security_contract.SECURITY_PASS)
        self.assertEqual(len(doc["prs"]["12"]["security_debt"]), 1)

    def test_an_existing_claim_is_held_then_resumes_once_the_freeze_clears(self):
        """This test previously asserted the opposite, and was wrong.

        It read "recovery is not new work" and expected a frozen run to
        keep resuming a claimed attempt. But resuming a PLANNED claim
        acquires a worktree and starts a PAID worker, so it IS new work.
        The claim is a reservation, not permission to spend: it is held
        while frozen, keeps its identity, and resumes when the control
        clears - nothing is abandoned and no second ordinal is allocated.
        """
        claim = self.claim(claim_state="PLANNED")
        doc = self.doc(claim)
        doc["frozen_at"] = clock.iso(clock.now(TZ))
        self.assertEqual(self.sv.route_evidence(doc, self.observe(doc)), [])
        held = doc["prs"]["12"]["security_evidence"]
        self.assertEqual(held["ordinal"], claim["ordinal"])
        self.assertEqual(held["attempt_id"], claim["attempt_id"])
        self.assertEqual(held["claim_state"], "PLANNED")

        doc["frozen_at"] = None                      # the freeze is lifted
        planned = self.sv.route_evidence(doc, self.observe(doc))
        self.assertEqual(len(planned), 1)
        self.assertEqual(planned[0].attempt_id, claim["attempt_id"])

    def test_cleanup_still_proceeds_while_dispatch_is_blocked(self):
        claim = self.claim()
        self.materialise(claim)
        self.ran(claim, verdict_block())
        doc = self.doc(claim)
        self.sv.publish_security_results(doc, {})
        self.sv.route_evidence(doc, self.observe(doc))
        with self.store.transaction() as stored:
            stored.update(doc)
            stored["frozen_at"] = clock.iso(clock.now(TZ))
        self.sv.stopping = True
        with mock.patch.object(sv_mod.workers, "remove_worker",
                               return_value=SimpleNamespace(ok=True)) as rm:
            self.sv.release_security_worktrees(self.store.read(), {})
        rm.assert_called_once_with(claim["worker"])

    def test_no_budget_control_is_claimed_for_worker_dispatch(self):
        """A gap, recorded as a test so it cannot be quietly mis-stated.

        budget.hard_stop governs metered OpenRouter/Jev spend, and since C-19
        the Supervisor reaches it through budget.reserve - which refuses when
        hard_stop is set, and additionally when outstanding exposure leaves no
        headroom. That one Jev call site is still the Supervisor's only budget
        consumer. No control in this repository gates subscription-provider
        worker dispatch on budget - builder, fixer, reviewer and observer are
        all ungated - so security is not gated either, rather than inventing a
        threshold.
        """
        doc, observations = self.fresh()
        doc["budget"]["hard_stop"] = True
        self.assertEqual(self.sv._security_dispatch_block(doc), "")
        source = Path(sv_mod.__file__).read_text(encoding="utf-8")
        self.assertEqual(source.count("budget.reserve("), 1,
                         "the metered-spend gate gained or lost a consumer")
        self.assertEqual(source.count("budget.metered_call_allowed"), 0,
                         "metered_call_allowed is reached through budget.reserve")


# --------------------------------------------- prompt context and payload

class ExecutionPayloadCase(Step6bCase):

    def test_the_real_task_title_reaches_the_security_prompt(self):
        doc = self.doc()
        planned = self.sv.route_evidence(doc, self.observe(doc))
        with self.store.transaction() as live:
            live.update(doc)
        seen = {}

        def capture(task, pr, branch, repo, cycle, evidence=""):
            seen.update(task=task, pr=pr, branch=branch, repo=repo)
            return "PROMPT"

        with mock.patch.object(sv_mod.prompts, "security", capture), \
                mock.patch.object(sv_mod.prompts, "write",
                                  return_value=self.root / "p.md"), \
                mock.patch.object(sv_mod.workers, "acquire_worktree",
                                  return_value=(self.root / "wt", "")), \
                mock.patch.object(sv_mod.workers, "write_job",
                                  return_value=self.root / "j.json"), \
                mock.patch.object(sv_mod.workers, "start_job",
                                  return_value=SimpleNamespace(ok=True)):
            self.sv.execute_security(planned)
        self.assertEqual(seen["task"]["title"], TASK_TITLE)
        self.assertNotEqual(seen["task"]["title"], "TASK-001")
        self.assertEqual(seen["task"]["id"], "TASK-001")
        self.assertEqual(seen["pr"], 12)

    def test_the_prompt_branch_is_the_branch_the_worktree_is_on(self):
        doc = self.doc()
        planned = self.sv.route_evidence(doc, self.observe(doc))
        with self.store.transaction() as live:
            live.update(doc)
        seen = {}
        with mock.patch.object(sv_mod.prompts, "security",
                               lambda t, pr, branch, repo, cycle, evidence="":
                               seen.setdefault("branch", branch) or "P"), \
                mock.patch.object(sv_mod.prompts, "write",
                                  return_value=self.root / "p.md"), \
                mock.patch.object(sv_mod.workers, "acquire_worktree",
                                  return_value=(self.root / "wt", "")) as acquire, \
                mock.patch.object(sv_mod.workers, "write_job",
                                  return_value=self.root / "j.json"), \
                mock.patch.object(sv_mod.workers, "start_job",
                                  return_value=SimpleNamespace(ok=True)):
            self.sv.execute_security(planned)
        self.assertEqual(seen["branch"], acquire.call_args[0][1])

    def test_the_rendered_prompt_actually_carries_the_title(self):
        # Through the real template, not a mock: a payload that never reaches
        # the rendered text would satisfy the test above and help nobody.
        text = sv_mod.prompts.security(
            {"id": "TASK-001", "title": TASK_TITLE}, 12,
            "security/security-attempt-0001/" + SHA_A[:12], "o/r", 1)
        self.assertIn(TASK_TITLE, text)

    def test_the_payload_is_frozen_and_carries_no_document_reference(self):
        doc = self.doc()
        planned = self.sv.route_evidence(doc, self.observe(doc))
        plan = planned[0]
        self.assertIsInstance(plan, routing.SecurityPlan)
        for field in ("task_id", "task_title", "pr", "sha", "attempt_id",
                      "worker"):
            self.assertIsInstance(getattr(plan, field), (str, int))
        with self.assertRaises(Exception):
            plan.sha = SHA_B
        # Mutating the document after T1 cannot reach the payload.
        doc["tasks"]["TASK-001"]["title"] = "CHANGED AFTER COMMIT"
        self.assertEqual(plan.task_title, TASK_TITLE)

    def test_an_inconsistent_payload_cannot_be_built(self):
        base = dict(task_id="TASK-001", task_title=TASK_TITLE, pr=12,
                    sha=SHA_A, attempt_id="security-attempt-0001",
                    worker=routing.security_worker_name("TASK-001", SHA_A, 1))
        routing.SecurityPlan(**base)          # the consistent one is fine
        for bad in (dict(base, worker="task-001-security-0001"),
                    dict(base, sha=SHA_B),
                    dict(base, attempt_id="security-attempt-0002"),
                    dict(base, pr=0), dict(base, task_title="")):
            with self.subTest(bad=sorted(set(bad.items()) - set(base.items()))):
                with self.assertRaises(ValueError):
                    routing.SecurityPlan(**bad)


# ------------------------------- reuse-branch dispatch-control bypass

class ReuseBranchDispatchControlCase(Step6bCase):
    """An existing claim is a RESERVATION, not permission to spend.

    The dispatch block originally sat AFTER the reuse branch, on the
    reasoning that resuming a claim is "recovery, not new work". That was
    wrong: NOT_MATERIALIZED means nothing has ever run, and
    MATERIALIZED_NOT_SPAWNED means no job file was ever written - in both,
    Phase C acquires a worktree and starts a PAID worker. Reproduced: a
    PLANNED claim spawned while the run was FROZEN and codex was in
    COOLDOWN.

    What the claim legitimately preserves is IDENTITY - same ordinal, same
    attempt directory - which survives a hold unchanged.
    """

    def planned_claim(self, *, materialised):
        """A PLANNED claim in one of the two reuse-branch recovery states."""
        claim = self.claim(claim_state="PLANNED")
        if materialised:
            self.materialise(claim)          # dir exists, no job file
        return claim

    def spawn_attempt(self, doc, observations=None):
        """Run T1 then Phase C, recording any real spawn."""
        started = []
        planned = self.sv.route_evidence(
            doc, observations if observations is not None else self.observe(doc))
        with self.store.transaction() as live:
            live.update(doc)          # T1 commits before Phase C runs
        if planned:
            with mock.patch.object(sv_mod.prompts, "write",
                                   return_value=self.root / "p.md"), \
                    mock.patch.object(
                        sv_mod.workers, "acquire_worktree",
                        # The SAME path the fixture's provenance records.
                        # Provenance is write-once, so a different worktree
                        # here is correctly refused as an identity collision.
                        side_effect=lambda name, *a, **k: (
                            self.wt_root / name, "")), \
                    mock.patch.object(sv_mod.workers, "write_job",
                                      return_value=self.root / "j.json"), \
                    mock.patch.object(
                        sv_mod.workers, "start_job",
                        side_effect=lambda w, j, p: (
                            started.append(w), SimpleNamespace(ok=True))[1]):
                self.sv.execute_security(planned)
        return planned, started

    def assert_held(self, doc, claim, outcome):
        planned, started = self.spawn_attempt(doc)
        self.assertEqual(planned, [], "a blocked control still planned work")
        self.assertEqual(started, [], "a PAID worker spawned while blocked")
        self.assertIn(outcome,
                      [e["outcome"] for e in self.events("SECURITY_EVIDENCE_HELD")])
        # Identity preserved: same ordinal, same attempt, nothing abandoned.
        after = doc["prs"]["12"]["security_evidence"]
        self.assertEqual(after["ordinal"], claim["ordinal"])
        self.assertEqual(after["attempt_id"], claim["attempt_id"])
        self.assertEqual(after["worker"], claim["worker"])
        self.assertEqual(after["claim_state"], "PLANNED")
        self.assertEqual(self.events("SECURITY_ATTEMPT_ABANDONED"), [])

    # --------------------------------------- both reuse states, frozen

    def test_not_materialized_does_not_spawn_while_frozen(self):
        claim = self.planned_claim(materialised=False)
        doc = self.doc(claim)
        self.assertEqual(
            routing.security_recovery_state(
                claim, self.observe(doc)[12][1]),
            routing.SECURITY_RECOVERY_NOT_MATERIALIZED)
        doc["frozen_at"] = clock.iso(clock.now(TZ))
        self.assert_held(doc, claim, "RUN_FROZEN")

    def test_materialized_not_spawned_does_not_spawn_while_frozen(self):
        claim = self.planned_claim(materialised=True)
        doc = self.doc(claim)
        self.assertEqual(
            routing.security_recovery_state(
                claim, self.observe(doc)[12][1]),
            routing.SECURITY_RECOVERY_MATERIALIZED_NOT_SPAWNED)
        doc["frozen_at"] = clock.iso(clock.now(TZ))
        self.assert_held(doc, claim, "RUN_FROZEN")

    # ------------------------------------------------ provider controls

    def test_a_reused_claim_does_not_spawn_into_a_cooling_down_provider(self):
        claim = self.planned_claim(materialised=True)
        doc = self.doc(claim)
        doc["providers"]["codex"]["state"] = providers.COOLDOWN
        self.assert_held(doc, claim, "REVIEW_NOT_PERMITTED")

    def test_a_reused_claim_does_not_spawn_when_review_is_denied(self):
        claim = self.planned_claim(materialised=False)
        doc = self.doc(claim)
        with mock.patch.object(sv_mod.providers, "may",
                               side_effect=lambda d, c: c != "review"):
            self.assert_held(doc, claim, "REVIEW_NOT_PERMITTED")

    def test_a_reused_claim_does_not_spawn_into_an_unusable_provider(self):
        claim = self.planned_claim(materialised=True)
        doc = self.doc(claim)
        with mock.patch.object(sv_mod.providers, "usable", return_value=False):
            self.assert_held(doc, claim, "PROVIDER_UNAVAILABLE")

    def test_unreadable_provider_state_fails_closed(self):
        claim = self.planned_claim(materialised=False)
        doc = self.doc(claim)
        doc.pop("providers")
        self.assert_held(doc, claim, "CONTROLS_UNREADABLE")

    # ------------------------------ controls changing after T1 commits

    def test_shutdown_between_planning_and_execution_spawns_nothing(self):
        claim = self.planned_claim(materialised=True)
        doc = self.doc(claim)
        planned = self.sv.route_evidence(doc, self.observe(doc))
        self.assertEqual(len(planned), 1, "T1 should have planned the resume")
        self.sv.stopping = True                       # the signal lands after T1
        with mock.patch.object(sv_mod.workers, "acquire_worktree") as acquire, \
                mock.patch.object(sv_mod.workers, "start_job") as start:
            self.assertEqual(self.sv.execute_security(planned), [])
        acquire.assert_not_called()
        start.assert_not_called()

    def test_a_freeze_between_planning_and_execution_spawns_nothing(self):
        """The decision that matters is the one true when a worker would
        start, not the one true when it was planned."""
        claim = self.planned_claim(materialised=True)
        doc = self.doc(claim)
        with self.store.transaction() as stored:
            stored.update(doc)
        planned = self.sv.route_evidence(doc, self.observe(doc))
        self.assertEqual(len(planned), 1)
        # `ctl freeze` lands in durable state between T1 and Phase C.
        with self.store.transaction() as stored:
            stored["frozen_at"] = clock.iso(clock.now(TZ))
        with mock.patch.object(sv_mod.workers, "acquire_worktree") as acquire, \
                mock.patch.object(sv_mod.workers, "start_job") as start:
            self.assertEqual(self.sv.execute_security(planned), [])
        acquire.assert_not_called()
        start.assert_not_called()
        self.assertEqual(
            [e["outcome"] for e in self.events("SECURITY_EXECUTION_FAILED")],
            ["RUN_FROZEN"])

    def test_the_phase_c_recheck_holds_no_lock_and_does_no_external_work(self):
        claim = self.planned_claim(materialised=True)
        doc = self.doc(claim)
        with self.store.transaction() as stored:
            stored.update(doc)
        planned = self.sv.route_evidence(doc, self.observe(doc))
        with mock.patch.object(sv_mod.gh, "git") as git, \
                mock.patch.object(sv_mod.prompts, "write",
                                  return_value=self.root / "p.md"), \
                mock.patch.object(sv_mod.workers, "acquire_worktree",
                                  return_value=(self.root / "wt", "")), \
                mock.patch.object(sv_mod.workers, "write_job",
                                  return_value=self.root / "j.json"), \
                mock.patch.object(sv_mod.workers, "start_job",
                                  return_value=SimpleNamespace(ok=True)):
            # The state file must stay writable by anyone throughout.
            self.assertIsNotNone(self.store.read())
            self.sv.execute_security(planned)
        git.assert_not_called()

    # ----------------------------------------------- capacity semantics

    def test_a_resumed_claim_is_not_double_counted_at_max_security(self):
        """Its slot is already held; counting it again would strand every
        resumed attempt at max_security = 1."""
        claim = self.planned_claim(materialised=True)
        doc = self.doc(claim)
        self.assertEqual(self.sv.cfg.max_security, 1)
        planned, started = self.spawn_attempt(doc)
        self.assertEqual(len(planned), 1)
        self.assertEqual(started, [claim["worker"]])
        self.assertEqual(
            [e["outcome"] for e in self.events("SECURITY_EVIDENCE_HELD")
             if e["outcome"] == "AT_CAPACITY"], [])

    def test_the_block_itself_refuses_while_stopping(self):
        """Phase C carries two stopping guards now - an explicit `break` and
        the shared block. The explicit one wins first, which would let the
        block's own stopping arm rot unnoticed. Asserted directly so both
        are live."""
        doc = self.doc()
        self.assertEqual(self.sv._security_dispatch_block(doc), "")
        self.sv.stopping = True
        self.assertEqual(self.sv._security_dispatch_block(doc),
                         "SUPERVISOR_STOPPING")

    def test_the_reserved_slot_still_blocks_a_different_pr(self):
        """The reservation is real: what is refused is a SECOND slot."""
        doc = state_mod.initial_document("run-002", "v2.0")
        providers.ensure(doc)
        for task_id, pr in (("TASK-001", 12), ("TASK-002", 13)):
            task = state_mod.add_task(doc, task_id, TASK_TITLE, [], "feature",
                                      False, TZ)
            task["state"] = "WAITING_EVIDENCE"
            task["pr"] = pr
            doc["prs"][str(pr)] = routing.blank_pr_record(pr, task_id, "feat/x")
        doc["prs"]["12"]["security_evidence"] = self.claim(claim_state="PLANNED")
        self.assertFalse(self.sv._security_slots_free(doc, 13))
        self.assertTrue(self.sv._security_slots_free(doc, 12))

    # ------------------- evidence must stay available while dispatch is held

    def test_publication_ingestion_and_cleanup_survive_a_blocked_dispatch(self):
        claim = self.claim()                     # a SPAWNED attempt that ran
        self.materialise(claim)
        self.ran(claim, verdict_block(findings=[finding(severity="P2")]))
        doc = self.doc(claim)
        doc["frozen_at"] = clock.iso(clock.now(TZ))
        doc["providers"]["codex"]["state"] = providers.COOLDOWN

        self.sv.publish_security_results(doc, {})
        self.sv.route_evidence(doc, self.observe(doc))
        stored = doc["prs"]["12"]["security_evidence"]
        self.assertEqual(stored["claim_state"], "COMPLETE")
        self.assertEqual(stored["verdict"], security_contract.SECURITY_PASS)
        self.assertEqual(len(doc["prs"]["12"]["security_debt"]), 1)

        with self.store.transaction() as live:
            live.update(doc)
        with mock.patch.object(sv_mod.workers, "remove_worker",
                               return_value=SimpleNamespace(ok=True)) as rm:
            self.sv.release_security_worktrees(self.store.read(), {})
        rm.assert_called_once_with(claim["worker"])

    # --------------- a job file is not proof of a running worker

    def test_a_job_file_without_a_live_worker_does_not_authorise_a_spawn(self):
        """PLANNED + a job file is read as SPAWNED, so it leaves the reuse
        branch entirely. Within the lease it is INDETERMINATE - absent from a
        good scan is "not yet observed", not "gone" - and nothing spawns."""
        claim = self.claim(claim_state="PLANNED")
        self.materialise(claim)
        (self.logs / f"{claim['worker']}.job.json").write_text(
            "{}", encoding="utf-8")
        doc = self.doc(claim)
        self.assertEqual(
            routing.security_recovery_state(claim, self.observe(doc)[12][1]),
            routing.SECURITY_RECOVERY_INDETERMINATE)
        planned, started = self.spawn_attempt(doc)
        self.assertEqual(planned, [])
        self.assertEqual(started, [])
        self.assertEqual(doc["prs"]["12"]["security_evidence"]["ordinal"], 1)

    def test_a_job_file_with_a_live_worker_is_running_and_never_respawns(self):
        claim = self.claim(claim_state="PLANNED")
        self.materialise(claim)
        (self.logs / f"{claim['worker']}.job.json").write_text(
            "{}", encoding="utf-8")
        doc = self.doc(claim)
        observations = self.observe(doc, entries={claim["worker"]: 4242})
        self.assertEqual(
            routing.security_recovery_state(claim, observations[12][1]),
            routing.SECURITY_RECOVERY_RUNNING)
        planned, started = self.spawn_attempt(doc, observations)
        self.assertEqual(planned, [])
        self.assertEqual(started, [])


if __name__ == "__main__":
    unittest.main()
