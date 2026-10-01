"""C-05.3b: the qualitative half, connected to the tick at last.

THE DISCREPANCY THIS CLOSES. The previous handover said both
accessibility halves were "wired to the tick" and, three paragraphs
later, that the qualitative tick dispatch was unfinished. Both were true
of different things, and the summary was wrong:

  * the AUTOMATED half really was tick-connected end to end -
    route_evidence -> execute_accessibility -> commit_accessibility - but
    gated on a services factory that was None, so it never planned;
  * the QUALITATIVE half was callable and proved through the Supervisor,
    but `route_evidence` called NEITHER plan_accessibility_review NOR
    ingest_accessibility_review. Nothing dispatched it in a real run, so
    `review_gate_fires` could never see that leg pass.

These cases exercise the real chain: plan in T1, spawn outside it,
confirm in its own transaction, ingest through the ordinary reaper. Every
external edge - worktree acquisition, the job file, the spawn, the
worker's output - is injected. Nothing here spawns a process, runs git or
makes a provider call.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from control import accessibility_contract as ac  # noqa: E402
from control import accessibility_registry, clock, config  # noqa: E402
from control import notify, providers, routing  # noqa: E402
from control import state as state_mod  # noqa: E402
from control import supervisor as sv_mod  # noqa: E402
from mergeable_evidence import accessibility_auto_leg, security_leg  # noqa: E402

TZ = "Pacific/Auckland"
HEAD = "a" * 40
NEW_HEAD = "b" * 40
REAL_ID = "ACC-DOD-VISIBLE_FOCUS"


def block(payload) -> str:
    return "noise\n```json\n" + json.dumps(payload) + "\n```\n"


def finding(requirement=REAL_ID, severity="P1"):
    return {"id": "A1", "jev_severity": severity, "classification": "FAILURE",
            "unmet_requirement": requirement}


class QualitativeDispatchCase(unittest.TestCase):
    """A real Supervisor over a real Store, with the worker edges stubbed."""

    def setUp(self):
        self.cfg = config.load()
        with mock.patch.object(sv_mod, "ledger_mod"), \
                mock.patch.object(sv_mod, "telemetry"), \
                mock.patch.object(sv_mod, "jev"):
            self.sv = sv_mod.Supervisor(self.cfg)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.sv.store = state_mod.Store(path=self.root / "state.json", tz=TZ)
        self.sv.ledger = mock.Mock()
        self.sv.notifier = mock.Mock()
        self.logged: list[tuple[str, dict]] = []
        self.sv.log = mock.Mock(
            side_effect=lambda e, **k: self.logged.append((e, k)))
        self.sv.notify_out = mock.Mock(return_value={"queued": True,
                                                     "intent_id": "NTF-1"})
        self.sv._accessibility_plans = []
        self.sv._accessibility_review_plans = []
        self.written: list[tuple[str, str]] = []
        self.jobs: list[dict] = []

    # ------------------------------------------------------------ fixtures

    def seed(self, *, head=HEAD, security=True, auto=True) -> dict:
        doc = state_mod.initial_document("run-002", "2.0")
        doc["started_at"] = clock.iso(clock.now(TZ))
        providers.ensure(doc)
        task = state_mod.add_task(doc, "TASK-001", "Foundation", [], "feature",
                                  False, TZ)
        task.update({"state": "WAITING_EVIDENCE", "pr": 7,
                     "branch": "run-002/task-001"})
        record = routing.blank_pr_record(7, "TASK-001", "run-002/task-001")
        if security:
            record["security_evidence"] = security_leg(head, "task-001")
        if auto:
            record["accessibility_auto"] = accessibility_auto_leg(head)
        doc["prs"]["7"] = record
        self.sv.store._write(doc)
        return doc

    def plan(self, doc, head=HEAD):
        """Drive the real T1 planning path."""
        self.sv._accessibility_review_plans = []
        task = doc["tasks"]["TASK-001"]
        # The REAL observation type, so this harness cannot diverge from
        # what the pre-lock phase actually hands route_evidence.
        observation = routing.SecurityObservation(
            head_sha=head, now=clock.iso(clock.now(TZ)), scan_ok=True,
            worker_live=False, attempt_dir_exists=False,
            job_file_exists=False, outcome_present=False)
        self.sv.route_evidence(doc, {7: (head, observation, None, {})})
        return list(self.sv._accessibility_review_plans)

    def spawn(self, plans, *, acquire_ok=True, start_ok=True):
        """Drive the real Phase C with every worker edge stubbed."""
        def write(worker, text):
            self.written.append((worker, text))
            return self.root / f"{worker}.prompt.md"

        def acquire(name, branch, base, session, prompt_path, **kw):
            if not acquire_ok:
                return None, "WORKTREE_UNAVAILABLE"
            return self.root / name, ""

        def write_job(worker, role, provider, model, task_id, path,
                      prompt_path, tz, **kw):
            self.jobs.append({"worker": worker, "role": role,
                              "provider": provider, "task_id": task_id, **kw})
            return self.root / f"{worker}.job.json"

        with mock.patch.object(sv_mod.prompts, "write", side_effect=write), \
                mock.patch.object(sv_mod.workers, "acquire_worktree",
                                  side_effect=acquire), \
                mock.patch.object(sv_mod.workers, "write_job",
                                  side_effect=write_job), \
                mock.patch.object(sv_mod.workers, "start_job",
                                  return_value=SimpleNamespace(
                                      ok=start_ok, stderr="")):
            return self.sv.execute_accessibility_review(plans)

    def reap(self, worker, text, *, outcome="SUCCESS"):
        """Drive the real reaper into the qualitative role handler."""
        status = {"phase": "DONE", "outcome": outcome, "duration_ms": 10}
        last = self.root / f"{worker}.last.txt"
        last.write_text(text, encoding="utf-8")
        with self.sv.store.transaction() as doc:
            with mock.patch.object(sv_mod.workers, "read_status",
                                   return_value=status), \
                    mock.patch.object(sv_mod.config, "WORKER_LOG_DIR",
                                      self.root), \
                    mock.patch.object(self.sv, "release_review_worktree"), \
                    mock.patch.object(self.sv, "_retain_worktree"):
                self.sv.reap_workers(doc)
            return doc

    def kinds(self):
        return [name for name, _ in self.logged]

    # --------------------------------------------------------- the planning

    def test_route_evidence_now_plans_the_qualitative_leg(self):
        """The gap itself. Before this, route_evidence called neither
        plan_accessibility_review nor ingest_accessibility_review, so the
        leg could never be dispatched in a real run."""
        doc = self.seed()
        plans = self.plan(doc)
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0].sha, HEAD)
        self.assertEqual(plans[0].pr, 7)

    def test_the_claim_is_durable_in_the_same_transaction(self):
        doc = self.seed()
        self.plan(doc)
        claim = doc["prs"]["7"]["accessibility_review"]
        self.assertEqual(claim["sha"], HEAD)
        self.assertEqual(claim["claim_state"], "PLANNED")
        self.assertIsNone(claim["verdict"])

    def test_planning_is_idempotent_within_a_head(self):
        doc = self.seed()
        self.assertEqual(len(self.plan(doc)), 1)
        self.assertEqual(self.plan(doc), [])

    def test_the_worker_name_is_bound_to_the_sha(self):
        doc = self.seed()
        plan = self.plan(doc)[0]
        self.assertEqual(plan.worker, routing.accessibility_review_worker_name(
            "TASK-001", HEAD, 1))

    # ------------------------------------------------------- C-02a residual

    def test_the_prompt_carries_every_canonical_identifier(self):
        """C-02a amended the frozen example; it did not tell the reviewer
        the other sixteen. The vocabulary is injected through the
        {{evidence}} substitution the Supervisor already owns - no further
        frozen-file change, and no parser alias."""
        doc = self.seed()
        self.spawn(self.plan(doc))
        _worker, text = self.written[0]
        for identifier in accessibility_registry.requirement_ids():
            self.assertIn(identifier, text)

    def test_the_vocabulary_is_rendered_from_the_registry_not_typed_out(self):
        """So it cannot drift from what the severity policy will accept."""
        doc = self.seed()
        self.spawn(self.plan(doc))
        _worker, text = self.written[0]
        self.assertIn(accessibility_registry.prompt_vocabulary(), text)

    def test_the_prompt_still_contains_no_unknown_identifier(self):
        import re
        doc = self.seed()
        self.spawn(self.plan(doc))
        _worker, text = self.written[0]
        cited = re.findall(r'"unmet_requirement"\s*:\s*"([^"]+)"', text)
        self.assertTrue(cited)
        for identifier in cited:
            self.assertIn(identifier, accessibility_registry.requirement_ids())

    # ----------------------------------------------------------- the spawn

    def test_the_spawn_uses_the_accessibility_role_and_its_own_lease(self):
        doc = self.seed()
        self.spawn(self.plan(doc))
        self.assertEqual(self.jobs[0]["role"], "accessibility")
        self.assertEqual(self.jobs[0]["hard_timeout_seconds"],
                         self.cfg.extra["timeouts"]["accessibility"])

    def test_the_reviewer_never_shares_the_builders_worktree(self):
        """It is read-only, and the Builder or Fixer may be committing
        into that branch's checkout right now."""
        acquired = {}

        def acquire(name, branch, base, session, prompt_path, **kw):
            acquired.update(branch=branch, base=base, kw=kw)
            return self.root / name, ""
        doc = self.seed()
        with mock.patch.object(sv_mod.prompts, "write",
                               return_value=self.root / "p.md"), \
                mock.patch.object(sv_mod.workers, "acquire_worktree",
                                  side_effect=acquire), \
                mock.patch.object(sv_mod.workers, "write_job",
                                  return_value=self.root / "j.json"), \
                mock.patch.object(sv_mod.workers, "start_job",
                                  return_value=SimpleNamespace(ok=True, stderr="")):
            self.sv.execute_accessibility_review(self.plan(doc))
        self.assertTrue(acquired["branch"].startswith("a11y/c1/"))
        self.assertEqual(acquired["base"], HEAD)
        self.assertFalse(acquired["kw"]["reuse_if_checked_out"])

    def test_a_stopping_supervisor_spawns_nothing(self):
        doc = self.seed()
        plans = self.plan(doc)
        self.sv.stopping = True
        self.assertEqual(self.spawn(plans), [])
        self.assertIn("ACCESSIBILITY_REVIEW_HELD", self.kinds())

    def test_a_worktree_that_cannot_be_acquired_does_not_spawn(self):
        doc = self.seed()
        self.assertEqual(self.spawn(self.plan(doc), acquire_ok=False), [])
        self.assertEqual(self.jobs, [])

    def test_a_failed_spawn_is_not_reported_as_started(self):
        doc = self.seed()
        self.assertEqual(self.spawn(self.plan(doc), start_ok=False), [])

    # --------------------------------------------------------- the confirm

    def test_confirming_registers_an_ordinary_worker(self):
        """Which is what gives the qualitative reviewer liveness, lease
        expiry and orphan detection for free, rather than reinventing
        them."""
        doc = self.seed()
        plans = self.plan(doc)
        self.sv.store._write(doc)
        started = self.spawn(plans)
        self.sv.confirm_accessibility_review_spawn(started)

        fresh = self.sv.store.read()
        worker = plans[0].worker
        self.assertIn(worker, fresh["workers"])
        self.assertEqual(fresh["workers"][worker]["role"], "accessibility")
        self.assertEqual(fresh["workers"][worker]["head"], HEAD)
        self.assertEqual(fresh["workers"][worker]["worktree"],
                         started[0][1])
        # The claim keeps its governed shape. It stays PLANNED - which is
        # what "in flight" means to plan_accessibility_review - and gains
        # no new key. An earlier version set claim_state="DISPATCHED" and
        # added claim["worktree"]; both fail
        # accessibility_review_claim_is_valid's closed sets, which made
        # review_gate_fires refuse the leg forever.
        claim = fresh["prs"]["7"]["accessibility_review"]
        self.assertEqual(claim["claim_state"], "PLANNED")
        self.assertNotIn("worktree", claim)
        self.assertEqual(routing.accessibility_review_claim_is_valid(claim),
                         (True, ""))

    def test_a_claim_that_moved_while_spawning_is_discarded(self):
        """Phase C holds no lock, so the head may move under it. The
        spawn must not be attached to whatever claim is there now."""
        doc = self.seed()
        plans = self.plan(doc)
        self.sv.store._write(doc)
        started = self.spawn(plans)
        with self.sv.store.transaction() as live:
            live["prs"]["7"]["accessibility_review"]["sha"] = NEW_HEAD
        self.sv.confirm_accessibility_review_spawn(started)

        fresh = self.sv.store.read()
        self.assertNotIn(plans[0].worker, fresh["workers"])
        self.assertIn("ACCESSIBILITY_REVIEW_DISCARDED", self.kinds())

    # ---------------------------------------------------------- the ingest

    def dispatched(self, **seed_kw):
        doc = self.seed(**seed_kw)
        plans = self.plan(doc)
        self.sv.store._write(doc)
        self.sv.confirm_accessibility_review_spawn(self.spawn(plans))
        return plans[0]

    def test_a_pass_reaches_review_through_the_real_reaper(self):
        plan = self.dispatched()
        doc = self.reap(plan.worker, block({"verdict": "ACCESSIBILITY_PASS",
                                            "findings": []}))
        self.assertEqual(
            doc["prs"]["7"]["accessibility_review"]["verdict"],
            ac.ACCESSIBILITY_PASS)
        self.assertEqual(doc["tasks"]["TASK-001"]["state"], "REVIEW")

    def test_a_fail_routes_to_repair_with_its_blocking_finding(self):
        plan = self.dispatched()
        doc = self.reap(plan.worker, block({"verdict": "ACCESSIBILITY_FAIL",
                                            "findings": [finding()]}))
        self.assertEqual(doc["tasks"]["TASK-001"]["state"], "FIX_REQUIRED")
        self.assertEqual(len(doc["prs"]["7"]["pending_findings"]), 1)

    def test_an_unknown_citation_still_holds_rather_than_failing(self):
        """Fail-closed is preserved end to end, not just in the parser."""
        plan = self.dispatched()
        doc = self.reap(plan.worker, block(
            {"verdict": "ACCESSIBILITY_FAIL",
             "findings": [finding(requirement="visible-focus-indicator")]}))
        self.assertEqual(doc["prs"]["7"]["accessibility_review"]["reason"],
                         ac.CLASSIFICATION_INVALID)
        self.assertEqual(doc["tasks"]["TASK-001"]["state"], "WAITING_EVIDENCE")

    def test_a_failed_worker_is_not_a_verdict(self):
        """A reviewer that crashed said nothing about accessibility. The
        claim is released so a later tick can re-plan, and the task is
        routed nowhere."""
        plan = self.dispatched()
        doc = self.reap(plan.worker, "", outcome="FAILED")
        claim = doc["prs"]["7"]["accessibility_review"]
        self.assertEqual(claim["claim_state"], "COMPLETE")
        self.assertIsNone(claim["verdict"])
        self.assertEqual(doc["tasks"]["TASK-001"]["state"], "WAITING_EVIDENCE")
        self.assertFalse(doc["prs"]["7"].get("pending_findings"))

    def test_a_released_claim_does_not_strand_the_only_slot(self):
        """max_accessibility_review is 1. A crashed reviewer that left its
        claim in flight would hold that slot for the rest of the run."""
        plan = self.dispatched()
        doc = self.reap(plan.worker, "", outcome="FAILED")
        self.sv.store._write(doc)
        self.assertEqual(len(self.plan(doc)), 1)

    # ------------------------------------------------------- restart recovery

    def restarted(self):
        """A genuinely new Supervisor over the SAME durable store.

        Not the same object with its lists cleared - a restart is a fresh
        process reading state.json back, and anything the claim needed
        that lived only in memory would be gone.
        """
        with mock.patch.object(sv_mod, "ledger_mod"), \
                mock.patch.object(sv_mod, "telemetry"), \
                mock.patch.object(sv_mod, "jev"):
            fresh = sv_mod.Supervisor(self.cfg)
        fresh.store = state_mod.Store(path=self.root / "state.json", tz=TZ)
        fresh.ledger = mock.Mock()
        fresh.notifier = mock.Mock()
        fresh.log = mock.Mock(
            side_effect=lambda e, **k: self.logged.append((e, k)))
        fresh.notify_out = mock.Mock(return_value={"queued": True,
                                                   "intent_id": "NTF-2"})
        fresh._accessibility_plans = []
        fresh._accessibility_review_plans = []
        self.sv = fresh
        return fresh

    def test_a_restart_does_not_re_dispatch_an_in_flight_review(self):
        """The claim is durable, so a Supervisor that died between the
        spawn and the result must not spend a second provider call on the
        same commit."""
        self.dispatched()
        self.restarted()
        doc = self.sv.store.read()
        self.assertEqual(self.plan(doc), [])

    def test_a_restart_keeps_the_claim_and_its_lease(self):
        plan = self.dispatched()
        before = self.sv.store.read()["prs"]["7"]["accessibility_review"]
        self.restarted()
        after = self.sv.store.read()["prs"]["7"]["accessibility_review"]
        self.assertEqual(after, before)
        self.assertEqual(after["worker"], plan.worker)
        self.assertIsNotNone(after["lease_expires_at"])

    def test_a_restart_can_still_ingest_the_result_it_did_not_dispatch(self):
        """The head and attempt id the ingest re-verifies against travel
        on the WORKER record, not in the dead process's memory."""
        plan = self.dispatched()
        self.restarted()
        doc = self.reap(plan.worker,
                        block({"verdict": "ACCESSIBILITY_PASS",
                               "findings": []}))
        self.assertEqual(
            doc["prs"]["7"]["accessibility_review"]["verdict"],
            ac.ACCESSIBILITY_PASS)
        self.assertEqual(doc["tasks"]["TASK-001"]["state"], "REVIEW")

    def test_the_gate_needs_the_qualitative_leg_too(self):
        """A PASS on the other two classes is not enough: before this
        chain existed, nothing could ever supply this one."""
        doc = self.seed()
        task = doc["tasks"]["TASK-001"]
        self.assertFalse(routing.review_gate_fires(doc["prs"]["7"], HEAD))
        self.assertEqual(task["state"], "WAITING_EVIDENCE")


if __name__ == "__main__":
    unittest.main()
