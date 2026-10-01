"""C-05.3a Step 6b: observe -> plan -> execute -> commit, end to end.

The phases exist to keep external work out of the state transaction. T1
claims and ingests; everything that touches git, a provider, a process or
a file happens with no lock held. Phase E is deliberately NOT a
reap_workers callback: reap_workers runs inside T1, and a callback there
that read worker output and published files would put exactly the work
C-18 documents back under the lock.

Two properties carry the most weight.

IDENTITY. At Phase D and Phase E the task association, PR number, head
SHA and exact attempt id are all re-checked against the current claim. The
SHA alone is not enough - a delayed answer from attempt 1 must not
overwrite attempt 2 for the same commit.

PUBLICATION BEFORE COMMIT. The normalized outcome becomes durable before
anything is marked COMPLETE. A failed publication changes nothing: no
verdict, no findings, no transition, no replacement attempt. The evidence
stays put so the next tick retries.

Everything here uses mocked providers, mocked processes and temporary
directories. Nothing launches an experiment or makes a paid call.
"""

from __future__ import annotations

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
    gate_evidence,
    ledger as ledger_mod,
    notify,
    providers,
    routing,
    security_contract,
    state as state_mod,
    supervisor as sv_mod,
)

TZ = "Pacific/Auckland"
SHA = "a" * 40
NEW_SHA = "c" * 40
SURFACES = {s: "PASS" for s in routing.SECURITY_SURFACES}


def finding(**over):
    base = {"id": "S1", "severity": "P1", "surface": "SECRETS",
            "file": "app/api/route.ts", "summary": "names the defect",
            "evidence": "diff quote", "required_change": "change it"}
    base.update(over)
    return base


def verdict_block(verdict="SECURITY_PASS", findings=()):
    return "prose\n```json\n" + json.dumps(
        {"verdict": verdict, "surfaces": SURFACES,
         "findings": list(findings), "summary": "s"}) + "\n```\n"


class EvidenceRoutingCase(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.evidence = self.root / "evidence"
        self.evidence.mkdir()
        self.logs = self.root / "workers"
        self.logs.mkdir()
        for target in (gate_evidence.config, sv_mod.config):
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
            # C-05.3b: route_evidence plans the QUALITATIVE accessibility
            # leg as well now, and that path reads its own governed
            # limit. Zero, so these security-focused cases keep planning
            # exactly what they always did - the limit is what is being
            # completed here, not an assertion.
            max_accessibility_review=0,
            extra={"timeouts": {"security": 1800, "lease_grace_seconds": 60}})
        self.sv.tz = TZ
        self.sv.ledger = self.ledger
        self.sv.store = self.store
        self.sv.stopping = False
        self.sv.notifier = mock.Mock(spec=notify.Notifier)

    # ------------------------------------------------------------- fixtures

    def doc(self, task_state="PR_OPEN", claim=None, sha=SHA):
        doc = state_mod.initial_document("run-002", "v2.0")
        providers.ensure(doc)
        task = state_mod.add_task(doc, "TASK-001", "T", [], "feature", False, TZ)
        task["state"] = task_state
        task["pr"] = 12
        doc["prs"]["12"] = routing.blank_pr_record(12, "TASK-001", "feat/x")
        if claim is not None:
            doc["prs"]["12"]["security_evidence"] = claim
        return doc

    def claim(self, *, ordinal=1, sha=SHA, claim_state="SPAWNED",
              lease_offset=1800, **over):
        now = clock.now(TZ)
        if lease_offset > 0:
            claimed, expires = now, now + timedelta(seconds=lease_offset)
        else:
            expires = now + timedelta(seconds=lease_offset)
            claimed = expires - timedelta(seconds=1800)
        c = routing.security_claim(task_id="TASK-001", sha=sha, ordinal=ordinal,
                                   claimed_at=clock.iso(claimed),
                                   lease_expires_at=clock.iso(expires))
        c["claim_state"] = claim_state
        c.update(over)
        return c

    def write_status(self, worker, phase="DONE", **over):
        payload = {"phase": phase, "exit_code": 0, "duration_ms": 12.0}
        payload.update(over)
        (self.logs / f"{worker}.status.json").write_text(json.dumps(payload),
                                                         encoding="utf-8")

    def write_output(self, worker, text):
        (self.logs / f"{worker}.last.txt").write_text(text, encoding="utf-8")

    def attempt_dir(self, claim, sha=SHA, *, pr=12, provenance=True):
        """The attempt directory, materialised as Phase C leaves it.

        Provenance is written by default because Phase C writes it before
        spawning, and publication refuses to attribute any worker artefact to
        an attempt without it. Pass provenance=False to build the state a
        failed worktree acquisition leaves behind.
        """
        path = gate_evidence.security_attempt_dir("TASK-001", sha,
                                                  claim["attempt_id"])
        path.mkdir(parents=True, exist_ok=True)
        if provenance:
            gate_evidence.write_security_provenance(path, {
                "task_id": "TASK-001", "pr": pr, "sha": sha,
                "attempt_id": claim["attempt_id"], "worker": claim["worker"],
                "worktree": str(self.root / "wt" / claim["worker"])})
        return path

    def plan_for(self, claim, *, task_title="T"):
        """The frozen payload T1 hands to Phase C for this claim."""
        return routing.SecurityPlan(
            task_id="TASK-001", task_title=task_title, pr=12,
            sha=claim["sha"], attempt_id=claim["attempt_id"],
            worker=claim["worker"])

    def events(self, event_type):
        lines = Path(self.ledger.path).read_text(encoding="utf-8").splitlines()
        return [json.loads(l) for l in lines if l and
                json.loads(l).get("event_type") == event_type]

    def observe(self, doc, entries=None, sha=SHA):
        return self.sv.observe_security(doc, {12: sha},
                                        {} if entries is None else entries)

    # ------------------------------------------------- Phase A eligibility

    def test_only_pr_open_and_waiting_evidence_are_observed(self):
        self.assertEqual(tuple(self.sv.EVIDENCE_STATES),
                         ("PR_OPEN", "WAITING_EVIDENCE"))
        for eligible in ("PR_OPEN", "WAITING_EVIDENCE"):
            with self.subTest(state=eligible):
                self.assertIn(12, self.observe(self.doc(eligible)))
        for ineligible in ("REVIEW", "FIX_REQUIRED", "MERGE_READY", "ACTIVE",
                           "COMPLETE", "FAILED", "HUMAN_REQUIRED"):
            with self.subTest(state=ineligible):
                self.assertEqual(self.observe(self.doc(ineligible)), {})

    def test_waiting_evidence_stays_eligible_across_ticks(self):
        # Recovery can only fire if a task already in WAITING_EVIDENCE is
        # re-observed every tick.
        doc = self.doc("WAITING_EVIDENCE", claim=self.claim())
        for _ in range(3):
            self.assertIn(12, self.observe(doc))

    def test_a_task_without_a_pr_is_never_observed(self):
        doc = self.doc()
        doc["tasks"]["TASK-001"]["pr"] = None
        self.assertEqual(self.observe(doc), {})

    def test_a_failed_proc_scan_marks_the_observation_not_the_absence(self):
        doc = self.doc("WAITING_EVIDENCE", claim=self.claim())
        observed = self.sv.observe_security(doc, {12: SHA}, None)
        self.assertFalse(observed[12][1].scan_ok)
        self.assertFalse(observed[12][1].worker_live)

    # ------------------------------------------- T1 purity and Phase C work

    def test_t1_claims_without_touching_the_filesystem(self):
        doc = self.doc()
        planned = self.sv.route_evidence(doc, self.observe(doc))
        self.assertEqual(len(planned), 1)
        self.assertEqual(doc["tasks"]["TASK-001"]["state"], "WAITING_EVIDENCE")
        self.assertEqual(list(self.evidence.glob("**/security-attempt-*")), [])
        self.assertEqual(list(self.logs.glob("*.job.json")), [])

    def test_phase_c_materialises_the_exact_claimed_attempt(self):
        doc = self.doc()
        planned = self.sv.route_evidence(doc, self.observe(doc))
        with self.store.transaction() as live:
            live.update(doc)      # T1 commits before Phase C re-reads controls
        claim = doc["prs"]["12"]["security_evidence"]
        with mock.patch.object(sv_mod.workers, "acquire_worktree",
                               return_value=(self.root / "wt", "")), \
                mock.patch.object(sv_mod.workers, "start_job",
                                  return_value=SimpleNamespace(ok=True)), \
                mock.patch.object(sv_mod.prompts, "write",
                                  return_value=self.root / "p.md"):
            spawned = self.sv.execute_security(planned)
        self.assertEqual(spawned, planned)
        made = gate_evidence.security_attempt_dir("TASK-001", SHA,
                                                  claim["attempt_id"])
        self.assertTrue(made.is_dir())
        self.assertEqual([p.name for p in self.evidence.glob("**/security-attempt-*")],
                         [claim["attempt_id"]])
        self.assertEqual([e["outcome"] for e in self.events("SECURITY_DISPATCHED")],
                         ["DISPATCHED"])

    def test_a_failed_spawn_does_not_reach_phase_d(self):
        doc = self.doc()
        planned = self.sv.route_evidence(doc, self.observe(doc))
        with self.store.transaction() as live:
            live.update(doc)      # T1 commits before Phase C re-reads controls
        with mock.patch.object(sv_mod.workers, "acquire_worktree",
                               return_value=(None, "no worktree")), \
                mock.patch.object(sv_mod.prompts, "write",
                                  return_value=self.root / "p.md"):
            self.assertEqual(self.sv.execute_security(planned), [])
        self.assertEqual(
            [e["outcome"] for e in self.events("SECURITY_EXECUTION_FAILED")],
            ["WORKTREE_UNAVAILABLE"])

    # ------------------------------------------------ Phase D identity gate

    def test_phase_d_caches_only_the_exact_attempt(self):
        claim = self.claim(claim_state="PLANNED")
        with self.store.transaction() as doc:
            doc.update(self.doc("WAITING_EVIDENCE", claim=claim))
        self.sv.confirm_security_spawn([self.plan_for(claim)])
        self.assertEqual(
            self.store.read()["prs"]["12"]["security_evidence"]["claim_state"],
            "SPAWNED")

    def test_phase_d_ignores_a_superseded_attempt(self):
        # A late Phase D from attempt 1 must not touch attempt 2.
        newer = self.claim(ordinal=2, claim_state="PLANNED")
        with self.store.transaction() as doc:
            doc.update(self.doc("WAITING_EVIDENCE", claim=newer))
        self.sv.confirm_security_spawn([self.plan_for(
            self.claim(ordinal=1, claim_state="PLANNED"))])
        after = self.store.read()["prs"]["12"]["security_evidence"]
        self.assertEqual(after["claim_state"], "PLANNED")
        self.assertEqual(after["ordinal"], 2)

    def test_phase_d_ignores_a_moved_head(self):
        claim = self.claim(claim_state="PLANNED")
        with self.store.transaction() as doc:
            doc.update(self.doc("WAITING_EVIDENCE", claim=claim))
        self.sv.confirm_security_spawn(
            [self.plan_for(self.claim(sha=NEW_SHA, claim_state="PLANNED"))])
        self.assertEqual(
            self.store.read()["prs"]["12"]["security_evidence"]["claim_state"],
            "PLANNED")

    # ------------------------------------------- Phase E publish then ingest

    def publish_for(self, doc, output, phase="DONE"):
        claim = doc["prs"]["12"]["security_evidence"]
        self.attempt_dir(claim)
        self.write_status(claim["worker"], phase)
        self.write_output(claim["worker"], output)
        self.sv.publish_security_results(doc, {})
        return claim

    def test_publication_happens_before_any_ingestion(self):
        doc = self.doc("WAITING_EVIDENCE", claim=self.claim())
        claim = self.publish_for(doc, verdict_block())
        stored = gate_evidence.read_security_outcome("TASK-001", 12, SHA,
                                                     claim["attempt_id"])
        self.assertIsNotNone(stored)
        # Nothing committed yet: publication is not ingestion.
        self.assertEqual(claim["claim_state"], "SPAWNED")
        self.assertIsNone(claim["verdict"])

    def test_a_pass_ingests_and_holds_in_waiting_evidence(self):
        doc = self.doc("WAITING_EVIDENCE", claim=self.claim())
        self.publish_for(doc, verdict_block(findings=[finding(severity="P2")]))
        self.sv.route_evidence(doc, self.observe(doc))
        claim = doc["prs"]["12"]["security_evidence"]
        self.assertEqual(claim["claim_state"], "COMPLETE")
        self.assertEqual(claim["verdict"], security_contract.SECURITY_PASS)
        self.assertEqual(doc["tasks"]["TASK-001"]["state"], "WAITING_EVIDENCE")

    def test_security_pass_alone_never_reaches_review(self):
        # Accessibility is a required evidence class and is C-05.3b.
        doc = self.doc("WAITING_EVIDENCE", claim=self.claim())
        self.publish_for(doc, verdict_block())
        for _ in range(3):
            self.sv.route_evidence(doc, self.observe(doc))
        self.assertNotEqual(doc["tasks"]["TASK-001"]["state"], "REVIEW")
        self.assertEqual(doc["tasks"]["TASK-001"]["state"], "WAITING_EVIDENCE")

    def test_a_fail_routes_only_blocking_findings_to_fix_required(self):
        doc = self.doc("WAITING_EVIDENCE", claim=self.claim())
        self.publish_for(doc, verdict_block("SECURITY_FAIL", [
            finding(id="S1", severity="P0"),
            finding(id="S2", severity="P1", surface="AUTHORIZATION",
                    file="app/auth.ts"),
            finding(id="S3", severity="P2", surface="DEPENDENCY_RISK",
                    file="package.json"),
            finding(id="S4", severity="P3", surface="ERROR_LEAKAGE",
                    file="app/log.ts")]))
        self.sv.route_evidence(doc, self.observe(doc))
        record = doc["prs"]["12"]
        self.assertEqual(doc["tasks"]["TASK-001"]["state"], "FIX_REQUIRED")
        self.assertEqual([f["id"] for f in record["pending_findings"]],
                         ["S1", "S2"])
        self.assertEqual([f["id"] for f in record["security_debt"]],
                         ["S3", "S4"])
        for f in record["pending_findings"] + record["security_debt"]:
            self.assertNotIn("evidence", f)

    def test_debt_alone_never_forces_remediation(self):
        doc = self.doc("WAITING_EVIDENCE", claim=self.claim())
        self.publish_for(doc, verdict_block(findings=[
            finding(severity="P2"), finding(id="S2", severity="P3")]))
        self.sv.route_evidence(doc, self.observe(doc))
        self.assertEqual(doc["tasks"]["TASK-001"]["state"], "WAITING_EVIDENCE")
        self.assertEqual(len(doc["prs"]["12"]["security_debt"]), 2)
        self.assertNotIn("pending_findings", doc["prs"]["12"])

    def test_ingestion_is_idempotent_across_repeated_ticks(self):
        # A PASS keeps the task in WAITING_EVIDENCE, so it stays eligible and
        # every later tick really does re-reach ingestion. A FAIL would move
        # it to FIX_REQUIRED and leave EVIDENCE_STATES, which would make the
        # loop prove nothing about idempotence.
        doc = self.doc("WAITING_EVIDENCE", claim=self.claim())
        self.publish_for(doc, verdict_block(findings=[finding(severity="P2")]))
        for _ in range(4):
            self.sv.route_evidence(doc, self.observe(doc))
            self.assertEqual(doc["tasks"]["TASK-001"]["state"],
                             "WAITING_EVIDENCE")
        self.assertEqual(len(doc["prs"]["12"]["security_debt"]), 1)
        self.assertEqual(len(self.events("SECURITY_RESULT")), 1)

    def test_a_completed_claim_is_never_ingested_twice_directly(self):
        doc = self.doc("WAITING_EVIDENCE", claim=self.claim())
        claim = self.publish_for(doc, verdict_block(
            "SECURITY_FAIL", [finding(severity="P0")]))
        outcome = gate_evidence.read_security_outcome(
            "TASK-001", 12, SHA, claim["attempt_id"])
        task = doc["tasks"]["TASK-001"]
        for _ in range(4):
            self.sv.ingest_security(doc, task, 12, SHA, outcome)
        self.assertEqual(len(doc["prs"]["12"]["pending_findings"]), 1)
        self.assertEqual(len(self.events("SECURITY_RESULT")), 1)

    def test_a_result_for_a_reassigned_pr_is_refused(self):
        # The task no longer owns this PR. Its evidence is not this task's.
        doc = self.doc("WAITING_EVIDENCE", claim=self.claim())
        claim = self.publish_for(doc, verdict_block(
            "SECURITY_FAIL", [finding(severity="P0")]))
        outcome = gate_evidence.read_security_outcome(
            "TASK-001", 12, SHA, claim["attempt_id"])
        doc["tasks"]["TASK-001"]["pr"] = 99
        self.sv.ingest_security(doc, doc["tasks"]["TASK-001"], 12, SHA, outcome)
        self.assertEqual(claim["claim_state"], "SPAWNED")
        self.assertNotIn("pending_findings", doc["prs"]["12"])
        self.assertEqual(doc["tasks"]["TASK-001"]["state"], "WAITING_EVIDENCE")

    def test_phase_d_refuses_a_reassigned_pr(self):
        claim = self.claim(claim_state="PLANNED")
        with self.store.transaction() as doc:
            doc.update(self.doc("WAITING_EVIDENCE", claim=claim))
            doc["tasks"]["TASK-001"]["pr"] = 99
        self.sv.confirm_security_spawn([self.plan_for(claim)])
        self.assertEqual(
            self.store.read()["prs"]["12"]["security_evidence"]["claim_state"],
            "PLANNED")

    def test_an_apparatus_failure_never_becomes_a_reviewer_fail(self):
        for phase, reason in (("TIMEOUT", security_contract.TIMED_OUT),
                              ("FAILED", security_contract.PROVIDER_FAILURE)):
            with self.subTest(phase=phase):
                self.setUp()
                doc = self.doc("WAITING_EVIDENCE", claim=self.claim())
                self.publish_for(doc, "", phase)
                self.sv.route_evidence(doc, self.observe(doc))
                claim = doc["prs"]["12"]["security_evidence"]
                self.assertEqual(claim["claim_state"], "COMPLETE")
                self.assertIsNone(claim["verdict"])
                self.assertEqual(claim["reason"], reason)
                self.assertEqual(doc["tasks"]["TASK-001"]["state"],
                                 "WAITING_EVIDENCE")
                self.assertNotIn("pending_findings", doc["prs"]["12"])

    def test_malformed_provider_output_is_an_unparseable_outcome(self):
        doc = self.doc("WAITING_EVIDENCE", claim=self.claim())
        self.publish_for(doc, "SECURITY_PASS, all clear\n```json\n{ broken\n```")
        self.sv.route_evidence(doc, self.observe(doc))
        claim = doc["prs"]["12"]["security_evidence"]
        self.assertIsNone(claim["verdict"])
        self.assertEqual(claim["reason"], security_contract.OUTPUT_UNPARSEABLE)

    def test_no_raw_provider_prose_is_persisted(self):
        canary = "sk-proj-" + "A" * 30
        doc = self.doc("WAITING_EVIDENCE", claim=self.claim())
        self.publish_for(doc, verdict_block("SECURITY_FAIL", [
            finding(severity="P0", evidence="leak " + canary,
                    summary="s " + canary)]))
        self.sv.route_evidence(doc, self.observe(doc))
        blob = json.dumps(doc) + Path(self.ledger.path).read_text(encoding="utf-8")
        for attempt in self.evidence.rglob("*.json"):
            blob += attempt.read_text(encoding="utf-8")
        self.assertNotIn(canary, blob)

    # --------------------------------------------- publication failure path

    def test_a_failed_publication_changes_nothing_and_retries(self):
        doc = self.doc("WAITING_EVIDENCE", claim=self.claim())
        claim = doc["prs"]["12"]["security_evidence"]
        self.attempt_dir(claim)
        self.write_status(claim["worker"])
        self.write_output(claim["worker"], verdict_block())
        with mock.patch.object(sv_mod.gate_evidence,
                               "publish_security_outcome", return_value=False):
            self.sv.publish_security_results(doc, {})
        self.sv.route_evidence(doc, self.observe(doc))
        self.assertEqual(claim["claim_state"], "SPAWNED")
        self.assertIsNone(claim["verdict"])
        self.assertEqual(doc["tasks"]["TASK-001"]["state"], "WAITING_EVIDENCE")
        self.assertEqual(
            [e["outcome"] for e in self.events("SECURITY_OUTCOME_PUBLISHED")],
            ["PUBLISH_FAILED"])
        # The next tick republishes from the evidence still on disk.
        self.sv.publish_security_results(doc, {})
        self.assertIsNotNone(gate_evidence.read_security_outcome(
            "TASK-001", 12, SHA, claim["attempt_id"]))

    def test_a_live_worker_is_never_published_even_if_status_says_done(self):
        # Liveness is the gate, not the status file. A worker still in the
        # /proc scan may be mid-exit with a stale DONE already written;
        # publishing then would race its own last write.
        doc = self.doc("WAITING_EVIDENCE", claim=self.claim())
        claim = doc["prs"]["12"]["security_evidence"]
        self.attempt_dir(claim)
        self.write_status(claim["worker"], "DONE")
        self.write_output(claim["worker"], verdict_block())
        self.sv.publish_security_results(doc, {claim["worker"]: 4242})
        self.assertIsNone(gate_evidence.read_security_outcome(
            "TASK-001", 12, SHA, claim["attempt_id"]))
        self.assertEqual(self.events("SECURITY_OUTCOME_PUBLISHED"), [])

    def test_an_unfinished_worker_is_never_published(self):
        doc = self.doc("WAITING_EVIDENCE", claim=self.claim())
        claim = doc["prs"]["12"]["security_evidence"]
        self.attempt_dir(claim)
        self.write_status(claim["worker"], "RUNNING")
        self.write_output(claim["worker"], verdict_block())
        self.sv.publish_security_results(doc, {})
        self.assertIsNone(gate_evidence.read_security_outcome(
            "TASK-001", 12, SHA, claim["attempt_id"]))

    def test_an_already_published_outcome_is_not_republished(self):
        # The publisher is write-once, so a second attempt would not corrupt
        # anything - but it would log a spurious PUBLISH_FAILED every tick
        # for the rest of the run, which reads as a fault that is not one.
        doc = self.doc("WAITING_EVIDENCE", claim=self.claim())
        self.publish_for(doc, verdict_block())
        self.assertEqual(
            [e["outcome"] for e in self.events("SECURITY_OUTCOME_PUBLISHED")],
            ["PUBLISHED"])
        for _ in range(3):
            self.sv.publish_security_results(doc, {})
        self.assertEqual(
            [e["outcome"] for e in self.events("SECURITY_OUTCOME_PUBLISHED")],
            ["PUBLISHED"], "republished over an existing outcome")

    def test_a_failed_scan_publishes_nothing(self):
        doc = self.doc("WAITING_EVIDENCE", claim=self.claim())
        claim = doc["prs"]["12"]["security_evidence"]
        self.attempt_dir(claim)
        self.write_status(claim["worker"])
        self.write_output(claim["worker"], verdict_block())
        self.sv.publish_security_results(doc, None)
        self.assertIsNone(gate_evidence.read_security_outcome(
            "TASK-001", 12, SHA, claim["attempt_id"]))

    # ------------------------------------------------- stale SHA and attempt

    def test_a_moved_head_discards_the_old_result_and_reclaims(self):
        doc = self.doc("WAITING_EVIDENCE", claim=self.claim())
        old = doc["prs"]["12"]["security_evidence"]
        self.publish_for(doc, verdict_block("SECURITY_FAIL",
                                            [finding(severity="P0")]))
        # The head moves before the result is ingested.
        planned = self.sv.route_evidence(doc, self.observe(doc, sha=NEW_SHA))
        self.assertEqual(doc["tasks"]["TASK-001"]["state"], "WAITING_EVIDENCE")
        self.assertNotIn("pending_findings", doc["prs"]["12"])
        fresh = doc["prs"]["12"]["security_evidence"]
        self.assertEqual(fresh["sha"], NEW_SHA)
        self.assertEqual(fresh["ordinal"], 1)
        self.assertEqual(len(planned), 1)
        # The old artifact survives untouched under its own SHA.
        self.assertIsNotNone(gate_evidence.read_security_outcome(
            "TASK-001", 12, SHA, old["attempt_id"]))

    def test_a_superseded_attempts_result_is_not_ingested(self):
        doc = self.doc("WAITING_EVIDENCE", claim=self.claim(ordinal=2))
        stale = {"task_id": "TASK-001", "pr": 12, "sha": SHA,
                 "attempt_id": "security-attempt-0001",
                 "status": gate_evidence.COMPLETED, "reason": "",
                 "verdict": security_contract.SECURITY_FAIL,
                 "surfaces": SURFACES, "findings": [],
                 "finding_counts": {"P0": 0, "P1": 0, "P2": 0, "P3": 0},
                 "summary": "", "provider": "codex", "model": None,
                 "exit_code": 0, "timed_out": False, "duration_ms": 1.0,
                 "stdout_name": "stdout.txt", "stderr_name": "stderr.txt"}
        self.sv.ingest_security(doc, doc["tasks"]["TASK-001"], 12, SHA, stale)
        claim = doc["prs"]["12"]["security_evidence"]
        self.assertEqual(claim["claim_state"], "SPAWNED")
        self.assertIsNone(claim["verdict"])
        self.assertEqual(doc["tasks"]["TASK-001"]["state"], "WAITING_EVIDENCE")

    # ----------------------------------------------------- crash recovery

    def test_a_crash_before_materialisation_resumes_the_same_attempt(self):
        doc = self.doc("WAITING_EVIDENCE", claim=self.claim(ordinal=4,
                                                            claim_state="PLANNED"))
        planned = self.sv.route_evidence(doc, self.observe(doc))
        self.assertEqual(planned[0].attempt_id, "security-attempt-0004")
        self.assertEqual(
            doc["prs"]["12"]["security_evidence"]["ordinal"], 4)

    def test_a_crash_after_the_job_file_never_dispatches_again(self):
        claim = self.claim(ordinal=4, claim_state="PLANNED")
        doc = self.doc("WAITING_EVIDENCE", claim=claim)
        self.attempt_dir(claim)
        (self.logs / f"{claim['worker']}.job.json").write_text("{}",
                                                              encoding="utf-8")
        self.assertEqual(self.sv.route_evidence(doc, self.observe(doc)), [])

    def test_an_expired_dead_attempt_advances_the_ordinal(self):
        claim = self.claim(ordinal=4, lease_offset=-1)
        doc = self.doc("WAITING_EVIDENCE", claim=claim)
        self.attempt_dir(claim)
        (self.logs / f"{claim['worker']}.job.json").write_text("{}",
                                                              encoding="utf-8")
        planned = self.sv.route_evidence(doc, self.observe(doc))
        self.assertEqual(planned[0].attempt_id, "security-attempt-0005")


if __name__ == "__main__":
    unittest.main()
