"""E1: notification event coverage (control/notify.py callers in
control/supervisor.py). Focused on Step 2's complete_task() enrichment:
the completion notification must show the real reviewed/merged SHAs when
observed, explicit "not observed"/"unavailable" wording when not, the
literal "No human action required.", and must never infer a merged SHA
from the reviewed head.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from control import clock, config, notify, routing, state, supervisor as supervisor_mod  # noqa: E402

TZ = "Pacific/Auckland"
PR = 7
BRANCH = "task/task-001"
REVIEWED_SHA = "a" * 40
MERGED_SHA = "b" * 40


def _doc(reviewed_head=None, merged_sha=None):
    doc = state.initial_document("run-002", "v2.0")
    task = state.add_task(doc, "TASK-001", "Foundation", [], "feature", False, TZ)
    task["state"] = "REVIEW"
    task["branch"] = BRANCH
    task["pr"] = PR
    task["title"] = "Foundation"
    record = routing.blank_pr_record(PR, "TASK-001", BRANCH)
    record["reviewed_head"] = reviewed_head
    record["merged_sha"] = merged_sha
    doc["prs"][str(PR)] = record
    return doc


class CompleteTaskNotificationCase(unittest.TestCase):
    def setUp(self):
        cfg = config.load()
        with mock.patch.object(supervisor_mod, "ledger_mod"), \
                mock.patch.object(supervisor_mod, "notify"), \
                mock.patch.object(supervisor_mod, "telemetry"), \
                mock.patch.object(supervisor_mod, "jev"):
            self.sup = supervisor_mod.Supervisor(cfg)
        self.sup.log = mock.Mock()
        self.sup.notify_out = mock.Mock(return_value={"ok": True})

    def complete(self, doc):
        with mock.patch.object(supervisor_mod.workers, "close_worker") as close:
            self.sup.complete_task(doc, doc["tasks"]["TASK-001"], PR)
        return close

    def notification_body(self):
        self.sup.notify_out.assert_called_once()
        args, kwargs = self.sup.notify_out.call_args
        # notify_out(doc, severity, title, body)
        return args[3] if len(args) > 3 else kwargs.get("body", args[-1])


class TestMergeShaAvailable(CompleteTaskNotificationCase):
    def test_body_shows_the_real_reviewed_and_merged_shas(self):
        doc = _doc(reviewed_head=REVIEWED_SHA, merged_sha=MERGED_SHA)
        self.complete(doc)
        body = self.notification_body()
        self.assertIn(REVIEWED_SHA, body)
        self.assertIn(MERGED_SHA, body)
        self.assertIn("No human action required.", body)

    def test_exactly_one_notification_is_sent(self):
        doc = _doc(reviewed_head=REVIEWED_SHA, merged_sha=MERGED_SHA)
        self.complete(doc)
        self.sup.notify_out.assert_called_once()

    def test_task_still_reaches_complete(self):
        doc = _doc(reviewed_head=REVIEWED_SHA, merged_sha=MERGED_SHA)
        self.complete(doc)
        self.assertEqual(doc["tasks"]["TASK-001"]["state"], "COMPLETE")


class TestMergeShaUnavailable(CompleteTaskNotificationCase):
    def test_body_explicitly_says_not_observed_rather_than_a_fake_value(self):
        doc = _doc(reviewed_head=REVIEWED_SHA, merged_sha=None)
        self.complete(doc)
        body = self.notification_body()
        self.assertIn("not observed", body)
        self.assertIn("No human action required.", body)

    def test_merged_sha_is_never_inferred_from_reviewed_head(self):
        doc = _doc(reviewed_head=REVIEWED_SHA, merged_sha=None)
        self.complete(doc)
        body = self.notification_body()
        # The reviewed SHA legitimately appears once, as the reviewed SHA -
        # it must not ALSO be presented as if it were the merged SHA.
        self.assertEqual(body.count(REVIEWED_SHA), 1)

    def test_completion_still_proceeds_despite_missing_merge_sha(self):
        """A missing SHA observation must not strand the task - see the
        same reasoning in attempt_merge (Step 1)."""
        doc = _doc(reviewed_head=REVIEWED_SHA, merged_sha=None)
        self.complete(doc)
        self.assertEqual(doc["tasks"]["TASK-001"]["state"], "COMPLETE")

    def test_missing_reviewed_head_says_unavailable_not_blank(self):
        doc = _doc(reviewed_head=None, merged_sha=None)
        self.complete(doc)
        body = self.notification_body()
        self.assertIn("unavailable", body)
        self.assertIn("not observed", body)


def _pr_view_result(ok=True, state=None, merge_commit_oid=None):
    if not ok:
        return None
    # C-18 stage 1 binds an observation to the pull request it was requested
    # for, and real `gh pr view` always returns number - PR_FIELDS asks for it.
    data = {"number": PR}
    if state is not None:
        data["state"] = state
    if merge_commit_oid is not None:
        data["mergeCommit"] = {"oid": merge_commit_oid}
    return data


class ExternalMergeDetectionCase(unittest.TestCase):
    """E1 Step 3: route_prs()'s externally-detected-merge branch, using
    the same single gh.pr_view() response as its only observation."""

    def setUp(self):
        cfg = config.load()
        with mock.patch.object(supervisor_mod, "ledger_mod"), \
                mock.patch.object(supervisor_mod, "notify"), \
                mock.patch.object(supervisor_mod, "telemetry"), \
                mock.patch.object(supervisor_mod, "jev"):
            self.sup = supervisor_mod.Supervisor(cfg)
        # C-14.2 reads durable merge evidence before treating a merged-but-
        # unmerged-in-state PR as external. A real, empty ledger states the
        # premise of these tests explicitly: no local merge was ever claimed,
        # so every case below really is an ordinary external merge.
        import tempfile
        from pathlib import Path as _Path
        from control import ledger as _ledger_mod
        self._ledger_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._ledger_tmp.cleanup)
        self.sup.ledger = _ledger_mod.Ledger(
            path=_Path(self._ledger_tmp.name) / "ledger.jsonl", tz=TZ,
            experiment_id="run-002")
        self.events: list[tuple] = []
        self.sup.log = mock.Mock(side_effect=lambda e, **k: self.events.append((e, k)))
        self.sup.notify_out = mock.Mock(return_value={"ok": True})

    def event_types(self) -> list[str]:
        return [name for name, _ in self.events]

    def doc_with_pr_task(self, reviewed_head=REVIEWED_SHA):
        doc = _doc(reviewed_head=reviewed_head, merged_sha=None)
        doc["tasks"]["TASK-001"]["pr"] = PR
        # Production guarantees this entry for any task carrying a PR:
        # attach_pr() assigns task["pr"] and transitions to PR_OPEN in the
        # same committed operation. The hand-built fixture has to say so, or
        # it describes a state the control plane cannot actually produce.
        doc["tasks"]["TASK-001"]["history"].append(
            {"at": "2026-09-26T09:00:00.000+12:00", "from": "ACTIVE",
             "to": "PR_OPEN", "reason": "PR opened"})
        return doc

    def run_route_prs(self, doc, *, pr_view_result):
        with mock.patch.object(supervisor_mod.gh, "pr_view", return_value=pr_view_result), \
                mock.patch.object(supervisor_mod.workers, "close_worker"):
            # No open PRs at all - forces the pr is None branch for every task.
            # C-18 stage 1: the view is observed before the transaction and
            # handed in, which is what tick() does.
            observations = self.sup.observe_closed_prs(doc, [])
            self.sup.route_prs(doc, None, [], observations)

    def test_merged_with_sha_completes_with_correct_evidence(self):
        doc = self.doc_with_pr_task()
        self.run_route_prs(doc, pr_view_result=_pr_view_result(
            ok=True, state="MERGED", merge_commit_oid=MERGED_SHA))

        self.assertEqual(doc["tasks"]["TASK-001"]["state"], "COMPLETE")
        record = doc["prs"][str(PR)]
        self.assertTrue(record["merged"])
        self.assertEqual(record["merged_sha"], MERGED_SHA)
        self.assertTrue(record["merge_sha_observed"])

        merged_events = [k for name, k in self.events if name == "MERGED"]
        self.assertEqual(len(merged_events), 1)
        self.assertEqual(merged_events[0]["metadata_redacted"]["merged_sha"], MERGED_SHA)
        self.assertEqual(merged_events[0]["metadata_redacted"]["reviewed_head"], REVIEWED_SHA)
        self.assertTrue(merged_events[0]["metadata_redacted"]["merge_sha_observed"])
        self.assertTrue(merged_events[0]["metadata_redacted"]["detected_externally"])

        self.sup.notify_out.assert_called_once()

    def test_merged_without_sha_is_honest_not_inferred(self):
        doc = self.doc_with_pr_task()
        self.run_route_prs(doc, pr_view_result=_pr_view_result(
            ok=True, state="MERGED", merge_commit_oid=None))

        self.assertEqual(doc["tasks"]["TASK-001"]["state"], "COMPLETE")
        record = doc["prs"][str(PR)]
        self.assertIsNone(record["merged_sha"])
        self.assertTrue(record["merge_sha_observed"])  # GitHub answered, just no oid

        merged_events = [k for name, k in self.events if name == "MERGED"]
        self.assertIsNone(merged_events[0]["metadata_redacted"]["merged_sha"])

        args, _ = self.sup.notify_out.call_args
        body = args[3]
        self.assertIn("not observed", body)
        self.assertNotIn(REVIEWED_SHA + REVIEWED_SHA, body)  # sanity: no duplication/inference

    def test_failed_observation_does_not_fabricate_a_merge(self):
        doc = self.doc_with_pr_task()
        self.run_route_prs(doc, pr_view_result=_pr_view_result(ok=False))

        self.assertEqual(doc["tasks"]["TASK-001"]["state"], "REVIEW")
        self.assertFalse(doc["prs"][str(PR)]["merged"])
        self.assertEqual(self.event_types(), [])
        self.sup.notify_out.assert_not_called()

    def test_not_yet_merged_is_not_completed(self):
        doc = self.doc_with_pr_task()
        self.run_route_prs(doc, pr_view_result=_pr_view_result(ok=True, state="OPEN"))

        self.assertEqual(doc["tasks"]["TASK-001"]["state"], "REVIEW")
        self.assertFalse(doc["prs"][str(PR)]["merged"])
        self.assertEqual(self.event_types(), [])
        self.sup.notify_out.assert_not_called()

    def test_exactly_one_merged_event_and_one_notification(self):
        doc = self.doc_with_pr_task()
        self.run_route_prs(doc, pr_view_result=_pr_view_result(
            ok=True, state="MERGED", merge_commit_oid=MERGED_SHA))

        self.assertEqual(self.event_types().count("MERGED"), 1)
        self.sup.notify_out.assert_called_once()


class OrderedCallCase(unittest.TestCase):
    """Base for tests that must prove ledger-before-Discord ORDER, not just
    that both eventually happened. self.calls records every self.sup.log
    and self.sup.notify_out invocation in the exact order they occurred."""

    def setUp(self):
        cfg = config.load()
        with mock.patch.object(supervisor_mod, "ledger_mod"), \
                mock.patch.object(supervisor_mod, "notify"), \
                mock.patch.object(supervisor_mod, "telemetry"), \
                mock.patch.object(supervisor_mod, "jev"):
            self.sup = supervisor_mod.Supervisor(cfg)
        self.calls: list[tuple] = []

        def fake_log(event_type, **fields):
            self.calls.append(("log", event_type, fields))
            return {"event_type": event_type, **fields}

        def fake_notify(doc, severity, title, body="", **kwargs):
            self.calls.append(("notify", severity, title, body))
            return {"ok": True}

        self.sup.log = mock.Mock(side_effect=fake_log)
        self.sup.notify_out = mock.Mock(side_effect=fake_notify)

    def notify_calls(self) -> list[tuple]:
        return [c for c in self.calls if c[0] == "notify"]

    def index_of_log(self, event_type: str):
        for i, c in enumerate(self.calls):
            if c[0] == "log" and c[1] == event_type:
                return i
        return None

    def index_of_notify(self, title_or_body_substring: str):
        for i, c in enumerate(self.calls):
            if c[0] == "notify" and (title_or_body_substring in c[2]
                                     or title_or_body_substring in c[3]):
                return i
        return None


class TestPrOpenedNotification(OrderedCallCase):
    def _doc(self):
        doc = state.initial_document("run-002", "v2.0")
        task = state.add_task(doc, "TASK-001", "Foundation", [], "feature", False, TZ)
        task["state"] = "ACTIVE"
        task["branch"] = BRANCH
        return doc, task

    def test_exactly_one_info_notification(self):
        doc, task = self._doc()
        self.sup.attach_pr(doc, task, PR)
        notifies = self.notify_calls()
        self.assertEqual(len(notifies), 1)
        self.assertEqual(notifies[0][1], notify.INFO)

    def test_content_names_task_pr_and_branch(self):
        doc, task = self._doc()
        self.sup.attach_pr(doc, task, PR)
        _, _, title, body = self.notify_calls()[0]
        self.assertIn("TASK-001", title)
        self.assertIn(str(PR), title)
        self.assertIn(BRANCH, body)

    def test_pr_opened_ledger_event_recorded_before_discord(self):
        doc, task = self._doc()
        self.sup.attach_pr(doc, task, PR)
        log_index = self.index_of_log("PR_OPENED")
        notify_index = self.index_of_notify("opened")
        self.assertIsNotNone(log_index)
        self.assertIsNotNone(notify_index)
        self.assertLess(log_index, notify_index)


class TestRecoverableReviewFailNotification(OrderedCallCase):
    def _doc(self):
        doc = state.initial_document("run-002", "v2.0")
        task = state.add_task(doc, "TASK-001", "Foundation", [], "feature", False, TZ)
        task["state"] = "REVIEW"
        task["branch"] = BRANCH
        task["pr"] = PR
        record = routing.blank_pr_record(PR, "TASK-001", BRANCH)
        doc["prs"][str(PR)] = record
        return doc

    def run_finished(self, doc, review):
        meta = {"task_id": "TASK-001", "pr": PR}
        status = {"outcome": "SUCCESS"}
        with mock.patch.object(supervisor_mod.routing, "parse_review", return_value=review), \
                mock.patch.object(supervisor_mod.routing, "touches_ui", return_value=False), \
                mock.patch.object(supervisor_mod, "providers"), \
                mock.patch.object(self.sup, "dispatch_fixer") as dispatch_fixer:
            self.sup.on_reviewer_finished(doc, "task-001-reviewer", meta, status)
        return dispatch_fixer

    def test_exactly_one_info_notification_for_the_failure(self):
        doc = self._doc()
        review = routing.Review(verdict=routing.REVIEW_FAIL,
                                findings=[{"id": "F1", "severity": "P2", "summary": "x"}])
        dispatch_fixer = self.run_finished(doc, review)
        notifies = self.notify_calls()
        self.assertEqual(len(notifies), 1)
        self.assertEqual(notifies[0][1], notify.INFO)
        dispatch_fixer.assert_called_once()

    def test_review_result_ledger_event_recorded_before_discord(self):
        """REVIEW_RESULT is the primary business event for any verdict,
        logged unconditionally before the verdict-specific branches run -
        this is what makes ledger-before-Discord true here, since the
        REVIEW_FAIL branch itself has no separate log call of its own."""
        doc = self._doc()
        review = routing.Review(verdict=routing.REVIEW_FAIL,
                                findings=[{"id": "F1", "severity": "P2", "summary": "x"}])
        self.run_finished(doc, review)
        log_index = self.index_of_log("REVIEW_RESULT")
        notify_index = self.index_of_notify("review found issues")
        self.assertIsNotNone(log_index)
        self.assertIsNotNone(notify_index)
        self.assertLess(log_index, notify_index)

    def test_distinct_from_fixer_dispatch_itself(self):
        """The notification is about the review failing, not about the
        fixer - dispatch_fixer is invoked separately, after, as its own
        step (and is covered by its own notification test group below)."""
        doc = self._doc()
        review = routing.Review(verdict=routing.REVIEW_FAIL,
                                findings=[{"id": "F1", "severity": "P2", "summary": "x"}])
        dispatch_fixer = self.run_finished(doc, review)
        notify_index = self.index_of_notify("review found issues")
        # dispatch_fixer is a Mock standing in for the real call; the real
        # FIXER_DISPATCHED notification lives inside it and is exercised by
        # TestFixerDispatchedNotification, not here.
        self.assertIsNotNone(notify_index)
        dispatch_fixer.assert_called_once_with(
            doc, doc["tasks"]["TASK-001"], PR, doc["prs"][str(PR)]["pending_findings"])


class TestFixerDispatchedNotification(OrderedCallCase):
    def _doc_in_review(self):
        doc = state.initial_document("run-002", "v2.0")
        task = state.add_task(doc, "TASK-001", "Foundation", [], "feature", False, TZ)
        task["state"] = "REVIEW"
        task["branch"] = BRANCH
        task["pr"] = PR
        record = routing.blank_pr_record(PR, "TASK-001", BRANCH)
        doc["prs"][str(PR)] = record
        return doc

    def dispatch(self, doc, findings):
        """C-18 stage 6 drives the three phases; the notification is queued
        in the commit phase, with the repair_cycles it reports."""
        builder_worktree = Path("/tmp/run-002-test/task-001-builder")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.sup.store = state.Store(path=Path(tmp.name) / "state.json", tz=TZ)
        self.sup._dispatch_plans = []
        with mock.patch.object(supervisor_mod.workers, "worktree_for_branch",
                               return_value=builder_worktree), \
                mock.patch.object(supervisor_mod.workers, "create_worker"), \
                mock.patch.object(supervisor_mod.workers, "worktree_path",
                                  return_value=builder_worktree), \
                mock.patch.object(supervisor_mod.workers, "write_job",
                                  return_value=Path("/tmp/job.json")), \
                mock.patch.object(supervisor_mod.workers, "start_job",
                                  return_value=mock.Mock(ok=True, stderr="")), \
                mock.patch.object(supervisor_mod.prompts, "fixer", return_value="p"), \
                mock.patch.object(supervisor_mod.prompts, "write",
                                  return_value=Path("/tmp/p.md")):
            self.sup.dispatch_fixer(doc, doc["tasks"]["TASK-001"], PR, findings)
            self.sup.store._write(doc)          # T1 commits
            results = self.sup.execute_dispatches(self.sup._dispatch_plans,
                                                  snapshot=doc)
        for result in results:
            self.sup.apply_dispatch_result(doc, result)

    def test_exactly_one_info_notification_after_successful_dispatch(self):
        doc = self._doc_in_review()
        self.dispatch(doc, [{"id": "F1", "severity": "P1", "summary": "x"}])
        notifies = self.notify_calls()
        self.assertEqual(len(notifies), 1)
        self.assertEqual(notifies[0][1], notify.INFO)

    def test_fix_dispatched_ledger_event_recorded_before_discord(self):
        doc = self._doc_in_review()
        self.dispatch(doc, [{"id": "F1", "severity": "P1", "summary": "x"}])
        log_index = self.index_of_log("FIX_DISPATCHED")
        notify_index = self.index_of_notify("fixer dispatched")
        self.assertIsNotNone(log_index)
        self.assertIsNotNone(notify_index)
        self.assertLess(log_index, notify_index)

    def test_repair_cycle_information_is_correct(self):
        doc = self._doc_in_review()
        self.dispatch(doc, [{"id": "F1", "severity": "P1", "summary": "x"}])
        self.assertEqual(doc["prs"][str(PR)]["repair_cycles"], 1)
        body = self.notify_calls()[0][3]
        self.assertIn("Repair cycle 1", body)


class TestReviewPassNotification(OrderedCallCase):
    def _doc(self):
        doc = state.initial_document("run-002", "v2.0")
        task = state.add_task(doc, "TASK-001", "Foundation", [], "feature", False, TZ)
        task["state"] = "REVIEW"
        task["branch"] = BRANCH
        task["pr"] = PR
        record = routing.blank_pr_record(PR, "TASK-001", BRANCH)
        doc["prs"][str(PR)] = record
        return doc

    def run_finished(self, doc):
        meta = {"task_id": "TASK-001", "pr": PR}
        status = {"outcome": "SUCCESS"}
        review = routing.Review(verdict=routing.REVIEW_PASS, findings=[], gates={})
        with mock.patch.object(supervisor_mod.routing, "parse_review", return_value=review), \
                mock.patch.object(supervisor_mod.routing, "touches_ui", return_value=False), \
                mock.patch.object(supervisor_mod, "providers"):
            self.sup.on_reviewer_finished(doc, "task-001-reviewer", meta, status)

    def test_exactly_one_info_notification(self):
        doc = self._doc()
        self.run_finished(doc)
        notifies = self.notify_calls()
        self.assertEqual(len(notifies), 1)
        self.assertEqual(notifies[0][1], notify.INFO)
        self.assertEqual(doc["prs"][str(PR)]["review_verdict"], routing.REVIEW_PASS)

    def test_review_pass_recorded_ledger_event_before_discord(self):
        doc = self._doc()
        self.run_finished(doc)
        log_index = self.index_of_log("REVIEW_PASS_RECORDED")
        notify_index = self.index_of_notify("review passed")
        self.assertIsNotNone(log_index)
        self.assertIsNotNone(notify_index)
        self.assertLess(log_index, notify_index)


class TestDiscordFailureDoesNotEraseTheLedgerEvent(unittest.TestCase):
    """The primary business event (PR_OPENED here, standing in for the
    shared notify_out mechanism every E1 notification uses) must survive a
    real Discord send failure - this exercises the REAL notify_out method,
    unlike every test above, which stubs it out to inspect call content.

    C-18 stage 2 moved the send itself out of the caller's transaction, so
    the failure can no longer be injected here: notify_out only queues. What
    this case still proves is that the business event and the state change
    are durable and the notification is not lost. The end-to-end version -
    a delivery that actually fails during the drain, against a real store,
    rolling nothing back and leaving the intent retryable - lives in
    tests/test_c18_stage2_notification_queue.py.
    """

    def setUp(self):
        cfg = config.load()
        with mock.patch.object(supervisor_mod, "ledger_mod"), \
                mock.patch.object(supervisor_mod, "notify"), \
                mock.patch.object(supervisor_mod, "telemetry"), \
                mock.patch.object(supervisor_mod, "jev"):
            self.sup = supervisor_mod.Supervisor(cfg)
        self.events: list[tuple] = []
        self.sup.log = mock.Mock(side_effect=lambda e, **k: self.events.append((e, k)))
        # notify_out is left real; only the network boundary is forced to fail.
        self.sup.notifier.send = mock.Mock(return_value={"ok": False, "reason": "TEST_BLOCKED"})

    def test_primary_business_event_survives_a_discord_send_failure(self):
        doc = state.initial_document("run-002", "v2.0")
        task = state.add_task(doc, "TASK-001", "Foundation", [], "feature", False, TZ)
        task["state"] = "ACTIVE"
        task["branch"] = BRANCH

        self.sup.attach_pr(doc, task, PR)

        # Nothing is delivered from inside the caller's transaction any more.
        self.assertEqual(self.sup.notifier.send.call_count, 0)
        # The notification is not lost by not being sent here: it is durable,
        # queued alongside the state change, and awaiting the drain.
        intents = list(notify.queue(doc).values())
        self.assertEqual(len(intents), 1)
        self.assertEqual(intents[0]["status"], notify.PENDING)

        event_types = [name for name, _ in self.events]
        self.assertIn("PR_OPENED", event_types)
        # The underlying state change itself must also survive - a Discord
        # failure must never roll anything back.
        self.assertEqual(doc["tasks"]["TASK-001"]["state"], "PR_OPEN")


if __name__ == "__main__":
    unittest.main()
