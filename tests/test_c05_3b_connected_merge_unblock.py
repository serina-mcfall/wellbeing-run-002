"""Handover §41.5 item 3: the tail of the connected lifecycle, in Python.

`tests/test_c05_3b_connected_lifecycle.py` proves the evidence half — failed
evidence, repair, fresh evidence, changed-head invalidation, restart
recovery, cleanup, and the path to REVIEW. It stops at REVIEW.

**Merge confirmation and dependency unblocking were demonstrated only in the
JavaScript fixture** (`apparatus/fixture-preflight/scenario.js`'s
`runAutonomousLifecycle` and `unblockDependents`, asserted in
`lifecycle.test.js` and `production-lifecycle.test.js`). That fixture proves
the *scenario*. It cannot prove the *runtime*, for two independent reasons:

  1. Its merge gate is `apparatus/pr-evidence/live-gate.js`, and nothing in
     `control/` calls it. The Supervisor calls `routing.evaluate_merge`.
  2. Its unblocking rule is the fixture's own `unblockDependents`, a
     JavaScript reimplementation reading `config/tasks.json` directly. The
     runtime's rule is `state.dependencies_met` + `Supervisor.dispatchable`,
     and the two have never been shown to agree.

So this file runs the whole chain once, through the real objects:

    WAITING_EVIDENCE
      -> tick 1: the real tick gathers the accessibility_auto leg
                 (route_evidence -> execute_accessibility ->
                  commit_accessibility) with services injected
      -> REVIEW
      -> tick 2: the real `routing.evaluate_merge` opens, the real
                 `attempt_merge` calls `gh.merge`, and the merge is CONFIRMED
                 by reading the pull request back
      -> COMPLETE
      -> a task whose dependency was the merged one is now returned by the
         real `Supervisor.dispatchable`

WHAT IS INJECTED, AND WHY THAT IS CORRECT. There is no product, and no
network is permitted. Every external is injected: `gh` (merge, pr_view,
list_open_prs), the accessibility services (install, server, scan, teardown),
and the Codex reviewer's verdict. What is NOT injected is the thing under
test — the Supervisor's own sequencing, the merge gate, and the dependency
predicate all run for real against a real `Store` on a real temporary file.

WHAT IS NOT SEEDED. The `accessibility_auto` leg that opens the merge gate is
absent at seed time and is produced by the production code during tick 1.
`test_the_merge_gate_was_opened_by_evidence_this_chain_produced` asserts
exactly that, because a seeded leg would make this a merge test wearing a
lifecycle test's name.

THE DEPENDENCY GRAPH IS THE COMMITTED ONE. `depends_on` is read from
`config/tasks.json`, the same file the JavaScript fixture resolves against,
rather than hand-written here — so the two halves cannot drift into proving
different graphs. `test_the_committed_graph_still_has_the_shape_this_file_needs`
fails loudly if that file changes underneath the test, rather than letting it
quietly stop proving anything.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from control import clock, config, providers, routing  # noqa: E402
from control import state as state_mod  # noqa: E402
from control import supervisor as supervisor_mod  # noqa: E402
from mergeable_evidence import (  # noqa: E402
    accessibility_review_leg, security_leg)
from test_merge_boundary import (  # noqa: E402
    BRANCH_FMT, DIFF_HASH, MERGED_SHA, REVIEWED_HEAD, MergeBoundaryCase, TZ)

# The task that merges, the task that must unblock when it does, and a task
# that must NOT — TASK-006 needs TASK-002 and TASK-004, so one merge is not
# enough. TASK-004 is seeded too, so TASK-006 is blocked by a dependency that
# is PRESENT and incomplete rather than by one that is simply absent.
MERGING = "TASK-001"
UNBLOCKS = ("TASK-002", "TASK-004")
STAYS_BLOCKED = "TASK-006"
PR = 100
# A commit the pull request no longer points at, used to express staleness.
STALE_HEAD = "9" * 40


def committed_graph() -> dict[str, list[str]]:
    """`depends_on` for every task, read from the committed task graph."""
    path = config.REPO_ROOT / "config" / "tasks.json"
    spec = json.loads(path.read_text(encoding="utf-8"))
    return {task["id"]: list(task["depends_on"]) for task in spec["tasks"]}


def passing_scan() -> list[dict]:
    """A PASS for every automated check the contract requires, at the head.

    Driven off `routing.AUTOMATED_CHECK_IDS` rather than a literal list: if
    a check is added to the contract, this scan keeps satisfying all of them
    instead of silently leaving the new one unanswered.
    """
    return [{"check_id": check_id, "sha": REVIEWED_HEAD,
             "artifact_reference": "evidence/a11y", "result": "PASS"}
            for check_id in routing.AUTOMATED_CHECK_IDS]


class Services:
    """The accessibility externals, injected. Mirrors the shape
    `tests/test_c05_3b_connected_lifecycle.py::Services` injects, because
    `accessibility_evidence.run_attempt` is the real consumer of both.

    Nothing here installs, builds, binds a port, spawns a browser or scans.
    """

    def __init__(self):
        self.stopped = 0

    def exists(self, path):
        return True

    def install_build(self, bound):
        return (True, None)

    def start_server(self, port, bound):
        return "handle"

    def await_ready(self, port, bound):
        return True

    def scan(self, port, sha, bound):
        return passing_scan()

    def stop_server(self, handle, bound):
        self.stopped += 1

    def listening_ports(self):
        return set()


class ConnectedMergeUnblockCase(MergeBoundaryCase):
    """The real Supervisor, real Store, real tick — extended past REVIEW.

    `MergeBoundaryCase` already supplies a genuine `Store` on a temporary
    path and a `run_tick` that stubs only the outbound GitHub edges and wraps
    `routing.evaluate_merge` in a recording mock around the REAL function.
    This subclass adds the accessibility services and the committed
    dependency graph; it changes nothing about how the merge is decided.
    """

    def setUp(self):
        super().setUp()
        self.graph = committed_graph()
        self.services = Services()
        self.sup.accessibility_services_factory = lambda plan: self.services
        root = Path(self.tmp.name)
        # Evidence and worker artefacts belong to this test, not to
        # .runtime/. Patched on both the module and the Supervisor's view of
        # it, because the evidence helpers reach for `config.` directly.
        for module in (config, supervisor_mod.config):
            self.enterContext(mock.patch.object(
                module, "EVIDENCE_DIR", root / "evidence"))
            self.enterContext(mock.patch.object(
                module, "WORKER_LOG_DIR", root / "workers"))

    # ------------------------------------------------------------ fixtures

    def seed_graph(self, *, drop_leg: str | None = None):
        """`MERGING` awaiting evidence, its dependents queued behind it.

        Deliberately NOT `MergeBoundaryCase.seed`: that seeds a task already
        in REVIEW with complete evidence, which is the state this chain is
        supposed to *reach*. Here the task starts in WAITING_EVIDENCE with
        no accessibility_auto leg at all.

        `drop_leg` omits one injected evidence class, which is how
        `test_a_missing_evidence_class_never_even_reaches_review` expresses
        a task that must never become a merge candidate at all.
        """
        doc = state_mod.initial_document("run-002", "2.0")
        doc["started_at"] = clock.iso(clock.now(TZ))
        providers.ensure(doc)

        branch = BRANCH_FMT.format(MERGING.lower())
        task = state_mod.add_task(doc, MERGING, MERGING, self.graph[MERGING],
                                  "feature", False, TZ)
        task.update({"state": "WAITING_EVIDENCE", "branch": branch,
                     "pr": PR, "worker": None})

        for task_id in (*UNBLOCKS, STAYS_BLOCKED):
            state_mod.add_task(doc, task_id, task_id, self.graph[task_id],
                               "feature", False, TZ)

        record = routing.blank_pr_record(PR, MERGING, branch)
        # The two legs this chain does not produce. The security worker and
        # the accessibility reviewer are provider calls; injecting their
        # finished claims is the same allowance every suite here makes.
        # `accessibility_auto` is absent ON PURPOSE — tick 1 must produce it.
        legs = {"security_evidence": security_leg(REVIEWED_HEAD, "task-001"),
                "accessibility_review": accessibility_review_leg(
                    REVIEWED_HEAD, "task-001")}
        legs.pop(drop_leg, None)
        record.update(legs)
        doc["prs"][str(PR)] = record

        self.store._write(doc)
        return doc

    def approve(self):
        """The Codex reviewer's verdict, injected.

        Dispatching a real reviewer spends a provider call and spawns a
        worker; neither is permitted here. Only the verdict is injected —
        `evaluate_merge` still has to decide whether that verdict, the head,
        the diff, CI and every evidence class agree.
        """
        with self.store.transaction() as doc:
            doc["prs"][str(PR)].update({
                "review_verdict": routing.REVIEW_PASS,
                "approval_current": True,
                "reviewed_head": REVIEWED_HEAD,
                "reviewed_diff_hash": DIFF_HASH,
                "review_cycles": 1,
                "accepted_findings": [],
            })

    def gather_evidence_tick(self):
        """Tick 1. The real tick gathers the automated accessibility leg."""
        self.run_tick()
        return self.store.read()

    def merge_tick(self):
        """Tick 2. The real tick decides, merges and confirms.

        `MergeBoundaryCase.run_tick` wraps `routing.evaluate_merge` in a
        recording Mock whose `side_effect` is resolved at construction time,
        so installing a recorder on the module attribute first captures each
        DECISION as well as each call. The real function still decides —
        `real` is bound before the patch is applied.
        """
        self.events.clear()
        self.decisions = []
        real = routing.evaluate_merge

        def recording(*args, **kwargs):
            decision = real(*args, **kwargs)
            self.decisions.append(decision)
            return decision

        with mock.patch.object(routing, "evaluate_merge", recording):
            self.run_tick()
        return self.store.read()

    def run_the_chain(self, *, drop_leg: str | None = None):
        """Seed, gather, approve, merge. Returns the durable document."""
        self.seed_graph(drop_leg=drop_leg)
        self.gather_evidence_tick()
        self.approve()
        return self.merge_tick()

    # --------------------------------------------------------- the helpers

    def dispatchable_ids(self, doc=None):
        """What the REAL selector offers a builder right now."""
        doc = self.store.read() if doc is None else doc
        return [task["id"] for task in self.sup.dispatchable(
            doc, self.sup.clock_state(doc))]

    def record(self, doc=None):
        return (doc or self.store.read())["prs"][str(PR)]

    def task_state_of(self, task_id, doc=None):
        return (doc or self.store.read())["tasks"][task_id]["state"]


# ------------------------------------------------------- the graph itself


class TheGraphUnderTestCase(ConnectedMergeUnblockCase):
    """A fixture that silently stops matching the committed graph proves
    nothing. These assert the shape every case below depends on."""

    def test_the_committed_graph_still_has_the_shape_this_file_needs(self):
        self.assertEqual(self.graph[MERGING], [],
                         f"{MERGING} is supposed to be a root of the graph")
        for task_id in UNBLOCKS:
            self.assertEqual(
                self.graph[task_id], [MERGING],
                f"{task_id} is supposed to depend on {MERGING} alone")
        self.assertEqual(
            sorted(self.graph[STAYS_BLOCKED]), sorted(UNBLOCKS),
            f"{STAYS_BLOCKED} is supposed to need both of {UNBLOCKS}, which "
            "is what makes it the still-blocked control")

    def test_only_COMPLETE_satisfies_a_dependency(self):
        # The predicate the whole file turns on. MERGED is a one-statement
        # waypoint on the way to COMPLETE; if a task ever stopped there its
        # dependents would block forever, so "merged" is not enough.
        doc = self.seed_graph()
        dependent = doc["tasks"][UNBLOCKS[0]]
        for state in ("REVIEW", "MERGED"):
            doc["tasks"][MERGING]["state"] = state
            self.assertFalse(
                state_mod.dependencies_met(doc, dependent),
                f"a dependency in {state} was treated as satisfied")
        doc["tasks"][MERGING]["state"] = "COMPLETE"
        self.assertTrue(state_mod.dependencies_met(doc, dependent))


# ------------------------------------------------- the chain, end to end


class TheWholeChainCase(ConnectedMergeUnblockCase):
    """One chain: evidence gathered, gate opened, merge confirmed, dependent
    unblocked. Each link asserted separately so a failure names itself."""

    def test_the_dependent_is_blocked_before_any_of_this_happens(self):
        # The control for every assertion below. If the dependent were
        # already dispatchable at seed time, "it became dispatchable" would
        # be true for reasons that have nothing to do with the merge.
        self.seed_graph()
        self.assertNotIn(UNBLOCKS[0], self.dispatchable_ids())
        self.assertEqual(self.task_state_of(UNBLOCKS[0]), "QUEUED")

    def test_tick_one_gathers_the_accessibility_leg_and_reaches_review(self):
        self.seed_graph()
        self.assertIsNone(self.record().get("accessibility_auto"))

        doc = self.gather_evidence_tick()

        leg = self.record(doc)["accessibility_auto"]
        self.assertEqual(leg["verdict"], routing.ACCESSIBILITY_AUTO_PASS)
        self.assertEqual(leg["sha"], REVIEWED_HEAD)
        self.assertTrue(leg["port_released"])
        self.assertEqual(self.services.stopped, 1,
                         "the product server was not torn down")
        self.assertEqual(self.task_state_of(MERGING, doc), "REVIEW")

    def test_the_merge_gate_was_opened_by_evidence_this_chain_produced(self):
        # The difference between this file and a merge test with seeded
        # evidence. The leg that satisfies `review_gate_fires` did not exist
        # when the document was written; the production code made it.
        self.seed_graph()
        self.assertIsNone(self.record().get("accessibility_auto"))
        self.assertFalse(routing.review_gate_fires(self.record(),
                                                   REVIEWED_HEAD))

        self.gather_evidence_tick()
        self.approve()

        self.assertTrue(
            routing.review_gate_fires(self.record(), REVIEWED_HEAD),
            "the gate still refuses after the chain gathered its evidence")
        self.merge_tick()
        self.assertEqual(self.merged_numbers(), [PR])

    def test_the_merge_went_through_the_real_evaluate_merge(self):
        # `self.evaluate` wraps the REAL `routing.evaluate_merge`. A merge
        # that reached `gh.merge` without it would mean the orchestration
        # had stopped consulting the gate.
        self.run_the_chain()
        self.assertEqual(self.merged_numbers(), [PR])
        self.assertTrue(self.evaluate.called,
                        "gh.merge was reached without consulting the gate")
        self.assertEqual(len(self.decisions), 1)
        decision = self.decisions[0]
        self.assertTrue(decision.allowed, decision.reason)
        self.assertEqual(decision.condition, routing.MERGE_OK)

    def test_the_merge_is_CONFIRMED_not_merely_attempted(self):
        # The fixture's rule: confirm by reading the forge back, never by
        # trusting the call. `attempt_merge` re-observes the pull request
        # after merging, and that observation is what must be durable.
        doc = self.run_the_chain()
        record = self.record(doc)
        self.assertTrue(record["merged"])
        self.assertTrue(record["merge_sha_observed"],
                        "the merge was recorded without being read back")
        self.assertEqual(record["merged_sha"], MERGED_SHA)
        self.assertEqual(self.task_state_of(MERGING, doc), "COMPLETE")
        self.assertIn("MERGED", self.event_types())

    def test_the_dependent_actually_becomes_dispatchable(self):
        # §41.5 item 3, the half that existed only in JavaScript.
        doc = self.run_the_chain()
        self.assertEqual(self.task_state_of(MERGING, doc), "COMPLETE")
        offered = self.dispatchable_ids(doc)
        for task_id in UNBLOCKS:
            self.assertIn(
                task_id, offered,
                f"{MERGING} merged and completed but {task_id}, which "
                f"depends on it, is still not dispatchable; offered={offered}")

    def test_a_dependent_with_an_unmet_second_dependency_stays_blocked(self):
        # Unblocking must be per-dependency, not "something merged, release
        # everything". TASK-006 needs TASK-002 as well, and TASK-002 has not
        # even been built yet.
        doc = self.run_the_chain()
        self.assertNotIn(STAYS_BLOCKED, self.dispatchable_ids(doc))
        self.assertFalse(state_mod.dependencies_met(
            doc, doc["tasks"][STAYS_BLOCKED]))

    def test_the_runtime_predicate_agrees_with_the_fixture_rule(self):
        # `unblockDependents` in apparatus/fixture-preflight/scenario.js uses
        # two predicates: `deps.some(d => merged)` selects the candidates
        # that are dependents of what just merged, and `deps.every(d =>
        # merged)` promotes them. The runtime's rule is
        # `state.dependencies_met`. Over every dependent of this merge in the
        # committed graph, the two must name the same tasks — otherwise the
        # JavaScript proof and the Python runtime describe different systems.
        doc = self.run_the_chain()
        merged_ids = {MERGING}
        candidates = [task_id for task_id, deps in self.graph.items()
                      if any(dep in merged_ids for dep in deps)]
        self.assertTrue(candidates, "the merged task has no dependents")

        compared = 0
        for task_id in candidates:
            task = doc["tasks"].get(task_id)
            if task is None:              # not seeded into this subgraph
                continue
            fixture_rule = all(dep in merged_ids
                               for dep in self.graph[task_id])
            runtime_rule = state_mod.dependencies_met(doc, task)
            self.assertEqual(
                fixture_rule, runtime_rule,
                f"{task_id}: the fixture rule says {fixture_rule} and "
                f"state.dependencies_met says {runtime_rule}")
            compared += 1
        self.assertEqual(compared, len(UNBLOCKS),
                         "the comparison silently covered nothing")


# ------------------------------------------------------ negative controls


class NoMergeMeansNoUnblockCase(ConnectedMergeUnblockCase):
    """The link that makes the chain evidence rather than coincidence.

    If the dependent became dispatchable whether or not the merge happened,
    `test_the_dependent_actually_becomes_dispatchable` would pass for the
    wrong reason — ticks alone would be doing the work.
    """

    def reach_review_then_stale_an_evidence_leg(self):
        """The whole chain to an approved REVIEW, then one leg goes stale.

        This is how a refusal is driven all the way INTO
        `routing.evaluate_merge`. Dropping a leg before tick 1 cannot do it:
        the task then never leaves WAITING_EVIDENCE, so no merge candidate
        is ever queued and the gate is never asked — correct, fail-closed,
        and covered separately below, but it proves a different thing.

        Re-binding the accessibility review to a superseded commit is the
        realistic shape: a complete, passing, structurally valid claim that
        simply belongs to the wrong head.
        """
        self.seed_graph()
        self.gather_evidence_tick()
        self.approve()
        with self.store.transaction() as doc:
            doc["prs"][str(PR)]["accessibility_review"] = \
                accessibility_review_leg(STALE_HEAD, "task-001")
        return self.merge_tick()

    def test_a_merge_the_gate_refuses_leaves_the_dependent_blocked(self):
        doc = self.reach_review_then_stale_an_evidence_leg()

        self.assertEqual(self.merged_numbers(), [],
                         "a pull request whose accessibility review belongs "
                         "to a superseded commit was merged")
        self.assertEqual(self.task_state_of(MERGING, doc), "REVIEW")
        for task_id in UNBLOCKS:
            self.assertNotIn(
                task_id, self.dispatchable_ids(doc),
                f"{task_id} became dispatchable without its dependency "
                "merging; ticks alone are unblocking it")

    def test_the_refusal_reached_the_gate_and_is_durably_recorded(self):
        # Blocked for the stated reason, not by accident of some other
        # guard — and readable afterwards as a finite condition code.
        self.reach_review_then_stale_an_evidence_leg()

        self.assertEqual(len(self.decisions), 1,
                         "the merge gate was never consulted")
        self.assertFalse(self.decisions[0].allowed)
        self.assertEqual(self.decisions[0].condition,
                         routing.MERGE_EVIDENCE_INCOMPLETE)
        conditions = [kwargs.get("metadata_redacted", {}).get("condition")
                      for event, kwargs in self.events
                      if event == "MERGE_BLOCKED"]
        self.assertIn(routing.MERGE_EVIDENCE_INCOMPLETE, conditions,
                      f"MERGE_BLOCKED conditions seen: {conditions}")

    def test_a_missing_evidence_class_never_even_reaches_review(self):
        # The earlier, stronger refusal. Without every class the task holds
        # in WAITING_EVIDENCE, so no merge candidate is queued at all —
        # and the dependent stays blocked a step further back.
        doc = self.run_the_chain(drop_leg="accessibility_review")

        self.assertEqual(self.task_state_of(MERGING, doc), "WAITING_EVIDENCE")
        self.assertEqual(self.merged_numbers(), [])
        self.assertEqual(self.decisions, [],
                         "a task that never reached REVIEW was offered to "
                         "the merge gate")
        for task_id in UNBLOCKS:
            self.assertNotIn(task_id, self.dispatchable_ids(doc))

    def test_an_unapproved_merge_leaves_the_dependent_blocked(self):
        # The same control through a different refusal, so the dependent's
        # fate is tied to the merge and not to one particular gate.
        self.seed_graph()
        self.gather_evidence_tick()
        doc = self.merge_tick()                 # no approve() — no verdict

        self.assertEqual(self.merged_numbers(), [])
        self.assertEqual(self.task_state_of(MERGING, doc), "REVIEW")
        self.assertNotIn(UNBLOCKS[0], self.dispatchable_ids(doc))


if __name__ == "__main__":
    unittest.main()
