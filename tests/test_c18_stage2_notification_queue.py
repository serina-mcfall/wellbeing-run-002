"""C-18 stage 2: notification delivery moves out of the state transaction.

C-18 names it directly: "every T1 path reaching self.notify_out performs a
synchronous outbound notifier.send". A hanging Discord call therefore stalled
the whole state transaction, and with it every task in the run.

Stage 2 makes notify_out record a durable INTENT, committed with the state
change that produced it, and delivers it afterwards in a bounded drain that
holds no lock. The lifecycle is the one watchdog.annunciate_orphans already
established - PENDING -> ATTEMPTING -> DELIVERED behind a durable pre-send
fence - with the governed additions from this stage:

  * retry ONLY after a durably recorded failure; an ambiguous outcome stays
    suppressed, because delivering twice is worse than stale bookkeeping;
  * the drain is bounded twice, by wall clock AND by attempt count, and each
    send gets only the budget that remains;
  * nothing is ever dropped, so notification coverage cannot be lost.

The guarantee is AT MOST ONCE per durably authorised attempt. It is
deliberately not exactly-once: a Discord webhook accepts no idempotency key,
so nothing on this side could honour that claim, and these tests assert the
weaker, true property rather than the stronger, false one.

Every send here is mocked. Nothing in this file reaches the network.
"""

from __future__ import annotations

import datetime
import fcntl
import io
import json
import os
import socket
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from control import (  # noqa: E402
    clock,
    config,
    http as http_mod,
    ledger as ledger_mod,
    notify,
    providers,
    state as state_mod,
    supervisor as supervisor_mod,
)
import declared_phases  # noqa: E402

TZ = "Pacific/Auckland"
PR = 7
BRANCH = "task/task-001"


def lock_is_held(lock_path: Path) -> bool:
    """Whether Store.transaction's exclusive flock is held right now.

    flock conflicts between distinct open file descriptions even inside one
    process, so an independent open() plus LOCK_NB tests the real lock rather
    than a flag the code under test kindly sets for us.
    """
    if not lock_path.exists():
        return False
    with open(lock_path, "r+", encoding="utf-8") as probe:
        try:
            fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(probe.fileno(), fcntl.LOCK_UN)
        return False


class QueueHarness(unittest.TestCase):
    """A real Supervisor over a real Store and a real Ledger.

    The ledger must be real: convergence after a lost state commit reads its
    durable NOTIFICATION events, and a mocked ledger would make that repair
    untestable - it is the whole crash-window story.
    """

    def setUp(self):
        self.cfg = config.load()
        with mock.patch.object(supervisor_mod, "ledger_mod"), \
                mock.patch.object(supervisor_mod, "telemetry"), \
                mock.patch.object(supervisor_mod, "jev"):
            self.sup = supervisor_mod.Supervisor(self.cfg)

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = state_mod.Store(path=self.root / "state.json", tz=TZ)
        self.sup.store = self.store
        self.ledger_path = self.root / "ledger.jsonl"
        self.sup.ledger = ledger_mod.Ledger(
            path=self.ledger_path, tz=TZ, experiment_id="run-002")

        self.sent: list[dict] = []
        self.lock_states: list[bool] = []
        self.sup.notifier = mock.Mock()
        self.sup.notifier.send = mock.Mock(side_effect=self._record_send)
        self.send_result = {"ok": True}
        self.send_raises = None

    def _record_send(self, severity, title, body="", **kwargs):
        self.lock_states.append(lock_is_held(self.store.lock_path))
        self.sent.append({"severity": severity, "title": title, "body": body,
                          **kwargs})
        if self.send_raises is not None:
            raise self.send_raises
        return self.send_result

    def _budget_clock(self, cost_per_send: float):
        """A monotonic clock that only advances when a send happens, so the
        budget is exhausted deterministically rather than by real waiting."""
        state = {"t": 0.0}
        original = self.sup.notifier.send.side_effect

        def send(*args, **kwargs):
            state["t"] += cost_per_send
            return original(*args, **kwargs)

        self.sup.notifier.send.side_effect = send
        return mock.patch.object(supervisor_mod.time, "monotonic",
                                 side_effect=lambda: state["t"])

    # ------------------------------------------------------------ fixtures

    def seed(self, *, notifications=True) -> dict:
        doc = state_mod.initial_document("run-002", "2.0")
        doc["started_at"] = clock.iso(clock.now(TZ))
        providers.ensure(doc)
        task = state_mod.add_task(doc, "TASK-001", "Foundation", [], "feature",
                                  False, TZ)
        task.update({"state": "ACTIVE", "branch": BRANCH})
        if not notifications:
            # A document written before this stage existed.
            doc.pop("notifications", None)
        self.store._write(doc)
        return doc

    def enqueue(self, severity=notify.INFO, title="a title", body="a body",
                **kwargs) -> str:
        with self.store.transaction() as doc:
            result = self.sup.notify_out(doc, severity, title, body, **kwargs)
        return result.get("intent_id")

    # ------------------------------------------------------------- helpers

    def durable(self) -> dict:
        return json.loads(self.store.path.read_text(encoding="utf-8"))

    def intents(self) -> dict:
        return self.durable().get("notifications", {})

    def intent(self, intent_id: str) -> dict:
        return self.intents()[intent_id]

    def events(self, event_type=None) -> list[dict]:
        if not self.ledger_path.exists():
            return []
        out = []
        for line in self.ledger_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event_type is None or event.get("event_type") == event_type:
                out.append(event)
        return out

    def outcomes(self, event_type="NOTIFICATION") -> list[str]:
        return [e.get("outcome") for e in self.events(event_type)]


# ------------------------------------------------------- the lock boundary


class NothingIsDeliveredUnderTheStateLock(QueueHarness):

    def test_the_lock_probe_can_actually_detect_a_held_lock(self):
        """Without this, every assertion below could pass vacuously."""
        self.seed()
        self.assertFalse(lock_is_held(self.store.lock_path))
        with self.store.transaction():
            self.assertTrue(lock_is_held(self.store.lock_path))
        self.assertFalse(lock_is_held(self.store.lock_path))

    def test_notify_out_inside_a_transaction_sends_nothing(self):
        self.seed()
        self.enqueue()
        self.sup.notifier.send.assert_not_called()

    def test_every_send_in_a_drain_happens_with_the_lock_free(self):
        self.seed()
        for index in range(3):
            self.enqueue(title=f"title {index}")
        self.sup.drain_notifications()
        self.assertEqual(len(self.lock_states), 3,
                         "the test proves nothing if nothing was sent")
        self.assertEqual(self.lock_states, [False, False, False])

    def test_a_whole_tick_delivers_only_outside_the_lock(self):
        """The end-to-end path, not just the drain in isolation."""
        self.seed()
        with self.store.transaction() as doc:
            self.sup.notify_out(doc, notify.HUMAN_REQUIRED, "needs a human",
                                "please look")
        with mock.patch.object(supervisor_mod.gh, "list_open_prs",
                               return_value=[]), \
                mock.patch.object(supervisor_mod.workers, "read_status",
                                  return_value=None), \
                mock.patch.object(supervisor_mod.config, "HEARTBEAT_PATH",
                                  self.root / "heartbeat.json"), \
                mock.patch.object(
                    self.sup, "run_declared",
                    side_effect=declared_phases.provider_phases_suppressed):
            self.sup.tick()
        self.assertTrue(self.lock_states, "the tick delivered nothing")
        self.assertEqual(self.lock_states, [False] * len(self.lock_states))


# --------------------------------------------------- commit and roll back


class CommitBoundary(QueueHarness):

    def test_a_committed_intent_is_delivered_by_the_drain(self):
        self.seed()
        intent_id = self.enqueue(title="committed")
        self.assertEqual(self.intent(intent_id)["status"], notify.PENDING)
        self.sup.drain_notifications()
        self.assertEqual([s["title"] for s in self.sent], ["committed"])
        self.assertEqual(self.intent(intent_id)["status"], notify.DELIVERED)

    def test_a_rolled_back_transaction_leaves_no_deliverable_intent(self):
        self.seed()

        class Injected(RuntimeError):
            pass

        with self.assertRaises(Injected):
            with self.store.transaction() as doc:
                self.sup.notify_out(doc, notify.INFO, "never happened", "body")
                raise Injected("the state change did not stick")

        self.assertEqual(self.intents(), {})
        self.sup.drain_notifications()
        self.sup.notifier.send.assert_not_called()

    def test_content_survives_the_round_trip_unchanged(self):
        self.seed()
        self.enqueue(severity=notify.ATTENTION, title="a precise title",
                     body="a precise body", no_human_action_needed=True)
        self.sup.drain_notifications()
        sent = self.sent[0]
        self.assertEqual(sent["severity"], notify.ATTENTION)
        self.assertEqual(sent["title"], "a precise title")
        self.assertEqual(sent["body"], "a precise body")
        self.assertTrue(sent["no_human_action_needed"])

    def test_the_clock_label_is_the_one_from_when_the_event_happened(self):
        """Captured at enqueue, not recomputed at delivery - the label
        belongs to the event, not to whenever delivery got around to it."""
        self.seed()
        with self.store.transaction() as doc:
            expected = self.sup.label(doc)
            self.sup.notify_out(doc, notify.INFO, "t", "b")
        self.sup.drain_notifications()
        self.assertEqual(self.sent[0]["clock_label"], expected)


# ------------------------------------------- failure, retry and restart


class DeliveryFailure(QueueHarness):

    def test_a_failed_delivery_keeps_the_intent_for_retry(self):
        self.seed()
        self.send_result = {"ok": False, "reason": "TEST_REFUSED"}
        intent_id = self.enqueue()
        self.sup.drain_notifications()

        stored = self.intent(intent_id)
        self.assertEqual(stored["status"], notify.PENDING)
        self.assertEqual(stored["attempt"], 1)
        self.assertIsNotNone(stored["next_attempt_at"])
        self.assertIn("FAILED", self.outcomes())

    def test_a_failed_delivery_rolls_back_no_committed_state(self):
        self.seed()
        self.send_result = {"ok": False}
        with self.store.transaction() as doc:
            doc["tasks"]["TASK-001"]["state"] = "PR_OPEN"
            self.sup.notify_out(doc, notify.INFO, "pr opened", "body")
        self.sup.drain_notifications()
        self.assertEqual(self.durable()["tasks"]["TASK-001"]["state"], "PR_OPEN")

    def test_backoff_prevents_an_immediate_second_attempt(self):
        self.seed()
        self.send_result = {"ok": False}
        self.enqueue()
        self.sup.drain_notifications()
        self.assertEqual(self.sup.notifier.send.call_count, 1)
        self.sup.drain_notifications()          # same instant, still backed off
        self.assertEqual(self.sup.notifier.send.call_count, 1)

    def test_it_is_retried_once_the_backoff_has_elapsed(self):
        self.seed()
        self.send_result = {"ok": False}
        intent_id = self.enqueue()
        self.sup.drain_notifications()

        with self.store.transaction() as doc:
            notify.queue(doc)[intent_id]["next_attempt_at"] = clock.iso(
                clock.now(TZ) - datetime.timedelta(seconds=1))
        self.send_result = {"ok": True}
        self.sup.drain_notifications()

        self.assertEqual(self.sup.notifier.send.call_count, 2)
        self.assertEqual(self.intent(intent_id)["status"], notify.DELIVERED)

    def test_backoff_grows_and_is_capped(self):
        self.assertEqual(notify.backoff_seconds(1), notify.BACKOFF_BASE_SECONDS)
        self.assertEqual(notify.backoff_seconds(2),
                         notify.BACKOFF_BASE_SECONDS * 2)
        self.assertEqual(notify.backoff_seconds(99), notify.BACKOFF_MAX_SECONDS)
        self.assertEqual(notify.backoff_seconds(0), 0)

    def test_a_delivered_intent_is_never_sent_again(self):
        self.seed()
        self.enqueue()
        self.sup.drain_notifications()
        self.sup.drain_notifications()
        self.sup.drain_notifications()
        self.assertEqual(self.sup.notifier.send.call_count, 1)

    def test_nothing_is_ever_dropped(self):
        """Coverage is governed; a notification that cannot be delivered
        stays queued rather than disappearing."""
        self.seed()
        self.send_result = {"ok": False}
        ids = [self.enqueue(title=f"t{i}") for i in range(3)]
        self.sup.drain_notifications()
        self.assertEqual(sorted(self.intents()), sorted(ids))


class TheCrashWindowBetweenSendingAndRecording(QueueHarness):
    """The state commit that records an outcome can be lost while the ledger
    append that proves it survives. The ledger is the authority."""

    def test_a_lost_delivered_commit_is_repaired_from_the_ledger(self):
        self.seed()
        intent_id = self.enqueue()
        with mock.patch.object(self.sup, "_record_notification_outcome"):
            self.sup.drain_notifications()
        # The send happened and the ledger knows; state still says ATTEMPTING.
        self.assertEqual(self.intent(intent_id)["status"], notify.ATTEMPTING)
        self.assertIn("DELIVERED", self.outcomes())

        self.sup.drain_notifications()
        self.assertEqual(self.intent(intent_id)["status"], notify.DELIVERED)
        self.assertEqual(self.sup.notifier.send.call_count, 1,
                         "a lost commit must never cause a resend")

    def test_a_lost_failed_commit_is_repaired_and_becomes_retryable(self):
        self.seed()
        self.send_result = {"ok": False}
        intent_id = self.enqueue()
        with mock.patch.object(self.sup, "_record_notification_outcome"):
            self.sup.drain_notifications()
        self.assertEqual(self.intent(intent_id)["status"], notify.ATTEMPTING)

        self.sup.drain_notifications()      # converge from the ledger
        stored = self.intent(intent_id)
        self.assertEqual(stored["status"], notify.PENDING)
        self.assertIsNotNone(stored["next_attempt_at"])

    def test_an_ambiguous_outcome_is_suppressed_not_retried(self):
        """The send began and its result is unknown. Under the established
        watchdog policy the intent stays ATTEMPTING - we accept possible
        loss over possible duplication, because the destination offers no
        idempotency key that would let us have both."""
        self.seed()
        self.send_raises = RuntimeError("connection reset mid-flight")
        intent_id = self.enqueue()
        self.sup.drain_notifications()

        self.assertEqual(self.intent(intent_id)["status"], notify.ATTEMPTING)
        self.assertNotIn("FAILED", self.outcomes())
        self.assertIn("AMBIGUOUS", self.outcomes())

        self.send_raises = None
        self.sup.drain_notifications()
        self.sup.drain_notifications()
        self.assertEqual(self.sup.notifier.send.call_count, 1,
                         "an ambiguous attempt must never be resent")

    def test_a_restart_resumes_a_queue_left_by_a_previous_process(self):
        """Nothing about the queue lives in memory: a fresh Supervisor picks
        up exactly what the last one committed."""
        self.seed()
        intent_id = self.enqueue(title="survives a restart")

        with mock.patch.object(supervisor_mod, "ledger_mod"), \
                mock.patch.object(supervisor_mod, "telemetry"), \
                mock.patch.object(supervisor_mod, "jev"):
            fresh = supervisor_mod.Supervisor(self.cfg)
        fresh.store = self.store
        fresh.ledger = ledger_mod.Ledger(path=self.ledger_path, tz=TZ,
                                         experiment_id="run-002")
        fresh.notifier = mock.Mock()
        fresh.notifier.send = mock.Mock(return_value={"ok": True})

        fresh.drain_notifications()
        fresh.notifier.send.assert_called_once()
        self.assertEqual(fresh.notifier.send.call_args.args[1],
                         "survives a restart")
        self.assertEqual(self.intent(intent_id)["status"], notify.DELIVERED)


# --------------------------------------------------------- bounded drain


class TheDrainIsBounded(QueueHarness):

    def test_the_attempt_count_cap_binds(self):
        self.seed()
        for index in range(self.sup.DRAIN_MAX_ATTEMPTS + 5):
            self.enqueue(title=f"t{index}")
        self.sup.drain_notifications()
        self.assertEqual(self.sup.notifier.send.call_count,
                         self.sup.DRAIN_MAX_ATTEMPTS)
        still_pending = [i for i in self.intents().values()
                         if i["status"] == notify.PENDING]
        self.assertEqual(len(still_pending), 5)

    def test_the_rest_of_the_backlog_is_delivered_on_later_drains(self):
        self.seed()
        for index in range(self.sup.DRAIN_MAX_ATTEMPTS + 5):
            self.enqueue(title=f"t{index}")
        self.sup.drain_notifications()
        self.sup.drain_notifications()
        self.assertEqual(self.sup.notifier.send.call_count,
                         self.sup.DRAIN_MAX_ATTEMPTS + 5)
        self.assertTrue(all(i["status"] == notify.DELIVERED
                            for i in self.intents().values()))

    def test_the_wall_clock_budget_binds_before_the_count_cap(self):
        self.seed()
        for index in range(self.sup.DRAIN_MAX_ATTEMPTS):
            self.enqueue(title=f"t{index}")
        # Four sends exactly consume a ten-second budget.
        with self._budget_clock(self.sup.DRAIN_BUDGET_SECONDS / 4):
            self.sup.drain_notifications()
        self.assertEqual(self.sup.notifier.send.call_count, 4)
        self.assertLess(4, self.sup.DRAIN_MAX_ATTEMPTS)

    def test_an_intent_the_budget_refused_to_start_is_not_left_suppressed(self):
        """A fence without a send would be ATTEMPTING for ever under the
        ambiguity rule. Intents are fenced one at a time, so an intent the
        budget never reached is untouched: still PENDING, still attempt 0."""
        self.seed()
        for index in range(self.sup.DRAIN_MAX_ATTEMPTS):
            self.enqueue(title=f"t{index}")
        with self._budget_clock(self.sup.DRAIN_BUDGET_SECONDS / 4):
            self.sup.drain_notifications()
        untouched = [i for i in self.intents().values()
                     if i["status"] == notify.PENDING]
        self.assertTrue(untouched)
        for intent in untouched:
            self.assertEqual(intent["attempt"], 0)
            self.assertIsNone(intent["next_attempt_at"])

    def test_each_send_is_given_only_the_budget_that_remains(self):
        """A 20-second default HTTP timeout against a 10-second drain budget
        would make the budget decorative."""
        self.seed()
        for index in range(4):
            self.enqueue(title=f"t{index}")
        with self._budget_clock(1.0):
            self.sup.drain_notifications()
        timeouts = [call.kwargs["timeout"]
                    for call in self.sup.notifier.send.call_args_list]
        self.assertTrue(timeouts)
        for value in timeouts:
            self.assertGreater(value, 0)
            self.assertLessEqual(value, self.sup.DRAIN_BUDGET_SECONDS)
        self.assertEqual(timeouts, sorted(timeouts, reverse=True),
                         "the remaining budget must shrink, not reset")

    def test_a_drain_on_an_empty_queue_does_nothing(self):
        self.seed()
        summary = self.sup.drain_notifications()
        self.sup.notifier.send.assert_not_called()
        self.assertEqual(summary["attempted"], 0)

    def test_a_drain_without_a_state_document_is_a_no_op(self):
        summary = self.sup.drain_notifications()
        self.assertEqual(summary["attempted"], 0)
        self.sup.notifier.send.assert_not_called()


class TheBacklogIsReported(QueueHarness):

    def test_the_summary_reports_pending_count_and_oldest_age(self):
        self.seed()
        self.send_result = {"ok": False}
        for index in range(3):
            self.enqueue(title=f"t{index}")
        summary = self.sup.drain_notifications()
        self.assertEqual(summary["attempted"], 3)
        self.assertEqual(summary["failed"], 3)
        self.assertEqual(summary["pending"], 3)
        self.assertIsNotNone(summary["oldest_pending_age_seconds"])
        self.assertGreaterEqual(summary["oldest_pending_age_seconds"], 0)

    def test_a_backlog_is_visible_in_the_ledger(self):
        self.seed()
        self.send_result = {"ok": False}
        self.enqueue()
        self.sup.drain_notifications()
        drains = self.events("NOTIFICATION_DRAIN")
        self.assertEqual(len(drains), 1)
        self.assertEqual(drains[0]["outcome"], "BACKLOG")
        self.assertEqual(drains[0]["metadata_redacted"]["pending"], 1)

    def test_a_cleared_queue_reports_no_backlog(self):
        self.seed()
        self.enqueue()
        summary = self.sup.drain_notifications()
        self.assertEqual(summary["pending"], 0)
        self.assertIsNone(summary["oldest_pending_age_seconds"])
        self.assertEqual(self.events("NOTIFICATION_DRAIN")[0]["outcome"],
                         "DRAINED")


# ------------------------------------------- identity and no duplication


class IntentsHaveStableIdentities(QueueHarness):

    def test_each_enqueue_mints_a_distinct_identity(self):
        self.seed()
        ids = {self.enqueue(title=f"t{i}") for i in range(5)}
        self.assertEqual(len(ids), 5)

    def test_the_identity_is_stable_across_ticks_and_drains(self):
        self.seed()
        self.send_result = {"ok": False}
        intent_id = self.enqueue()
        for _ in range(3):
            self.sup.drain_notifications()
        self.assertEqual(list(self.intents()), [intent_id])

    def test_repeated_drains_create_no_new_intents(self):
        self.seed()
        self.enqueue()
        before = set(self.intents())
        for _ in range(3):
            self.sup.drain_notifications()
        self.assertEqual(set(self.intents()), before)

    def test_repeated_ticks_over_unchanged_state_queue_nothing_new(self):
        self.seed()
        with mock.patch.object(supervisor_mod.gh, "list_open_prs",
                               return_value=[]), \
                mock.patch.object(supervisor_mod.workers, "read_status",
                                  return_value=None), \
                mock.patch.object(supervisor_mod.config, "HEARTBEAT_PATH",
                                  self.root / "heartbeat.json"), \
                mock.patch.object(
                    self.sup, "run_declared",
                    side_effect=declared_phases.provider_phases_suppressed):
            self.sup.tick()
            first = set(self.intents())
            self.sup.tick()
            self.sup.tick()
        self.assertEqual(set(self.intents()), first)

    def test_the_ledger_records_the_identity_on_both_sides(self):
        """Queue and delivery must name the same intent, or convergence
        after a lost commit could never match them up."""
        self.seed()
        intent_id = self.enqueue()
        self.sup.drain_notifications()
        queued = self.events("NOTIFICATION_QUEUED")[0]
        delivered = self.events("NOTIFICATION")[0]
        self.assertEqual(queued["metadata_redacted"]["intent_id"], intent_id)
        self.assertEqual(delivered["metadata_redacted"]["intent_id"], intent_id)


# --------------------------- coverage, ledger and human-intervention rules


class ExistingNotificationSemanticsArePreserved(QueueHarness):

    def test_human_required_increments_the_counter_exactly_once(self):
        self.seed()
        with self.store.transaction() as doc:
            self.sup.notify_out(doc, notify.HUMAN_REQUIRED, "t", "b")
        self.assertEqual(self.durable()["counters"]["human_interventions"], 1)
        self.sup.drain_notifications()
        self.sup.drain_notifications()
        self.assertEqual(self.durable()["counters"]["human_interventions"], 1)

    def test_the_counter_moves_with_the_state_change_not_with_delivery(self):
        """Accounting must not depend on a network call succeeding."""
        self.seed()
        self.send_result = {"ok": False}
        with self.store.transaction() as doc:
            self.sup.notify_out(doc, notify.HUMAN_REQUIRED, "t", "b")
        self.assertEqual(self.durable()["counters"]["human_interventions"], 1)
        self.sup.drain_notifications()
        self.assertEqual(self.durable()["counters"]["human_interventions"], 1)

    def test_a_rolled_back_change_takes_its_counter_increment_with_it(self):
        self.seed()

        class Injected(RuntimeError):
            pass

        with self.assertRaises(Injected):
            with self.store.transaction() as doc:
                self.sup.notify_out(doc, notify.HUMAN_REQUIRED, "t", "b")
                raise Injected("rolled back")
        self.assertEqual(self.durable()["counters"]["human_interventions"], 0)

    def test_non_human_required_severities_do_not_move_the_counter(self):
        self.seed()
        for severity in (notify.INFO, notify.ATTENTION, notify.CRITICAL):
            with self.store.transaction() as doc:
                self.sup.notify_out(doc, severity, "t", "b")
        self.assertEqual(self.durable()["counters"]["human_interventions"], 0)

    def test_the_business_event_is_durable_before_any_delivery(self):
        """Protocol v2 §Discord: ledger append succeeds before Discord
        notification. Queuing puts a whole commit between them."""
        self.seed()
        with self.store.transaction() as doc:
            self.sup.log("PR_OPENED", task_id="TASK-001", pr_id=PR,
                         activity_class="BUILD", outcome="PR_OPEN")
            self.sup.notify_out(doc, notify.INFO, "pr opened", "b")
        self.sup.drain_notifications()
        names = [e["event_type"] for e in self.events()]
        self.assertLess(names.index("PR_OPENED"), names.index("NOTIFICATION_QUEUED"))
        self.assertLess(names.index("NOTIFICATION_QUEUED"),
                        names.index("NOTIFICATION"))

    def test_a_secret_shaped_body_is_redacted_before_it_becomes_durable(self):
        """New obligation: the body is durable now. The send path scrubbed on
        the way out, but nothing previously had to stop a raw secret being
        written to state.json, because nothing was written."""
        self.seed()
        with self.store.transaction() as doc:
            result = self.sup.notify_out(
                doc, notify.INFO, "leak", "token ghp_" + "a" * 32)
        self.assertTrue(result["queued"])

        blob = self.store.path.read_text(encoding="utf-8")
        self.assertNotIn("ghp_" + "a" * 32, blob)
        self.assertIn("<redacted:github-token>", blob)

        self.sup.drain_notifications()
        self.assertNotIn("ghp_" + "a" * 32, self.sent[0]["body"])

    def test_new_intent_refuses_text_that_survives_scrubbing(self):
        """notify.new_intent's own guard, tested where no ledger is involved
        - patching redact globally would also trip the ledger's identical
        defence-in-depth check and prove the wrong thing."""
        with mock.patch.object(notify.redact, "contains_secret",
                               return_value=True):
            with self.assertRaises(ValueError) as caught:
                notify.new_intent(severity=notify.INFO, title="t", body="b",
                                  no_human_action_needed=False,
                                  clock_label="T+1", created_at="2026-10-01")
        self.assertEqual(str(caught.exception), "BLOCKED_SECRET_IN_NOTIFICATION")

    def test_a_secret_that_survives_scrubbing_is_never_persisted_at_all(self):
        """The refusal path in the supervisor. Only the finite reason is
        recorded - never the offending text."""
        self.seed()
        with mock.patch.object(supervisor_mod.notify, "new_intent",
                               side_effect=ValueError(
                                   "BLOCKED_SECRET_IN_NOTIFICATION")):
            with self.store.transaction() as doc:
                result = self.sup.notify_out(doc, notify.INFO, "leak",
                                             "unscrubbable")
        self.assertFalse(result["queued"])
        self.assertEqual(result["reason"], "BLOCKED_SECRET_IN_NOTIFICATION")
        self.assertEqual(self.intents(), {})
        self.assertNotIn("unscrubbable",
                         self.store.path.read_text(encoding="utf-8"))

        blocked = self.events("NOTIFICATION_BLOCKED")
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0]["outcome"], "BLOCKED_SECRET_IN_NOTIFICATION")
        self.assertNotIn("unscrubbable", json.dumps(blocked[0]))

        self.sup.drain_notifications()
        self.sup.notifier.send.assert_not_called()

    def test_notify_out_does_not_claim_delivery_it_cannot_make(self):
        self.seed()
        with self.store.transaction() as doc:
            result = self.sup.notify_out(doc, notify.INFO, "t", "b")
        self.assertTrue(result["queued"])
        self.assertNotIn("ok", result)


# ------------------------------------------------------- back-compatibility


class DocumentsWrittenBeforeThisStageStillWork(QueueHarness):

    def test_a_document_with_no_queue_key_can_still_enqueue(self):
        self.seed(notifications=False)
        self.assertNotIn("notifications", self.durable())
        intent_id = self.enqueue(title="added later")
        self.assertIn(intent_id, self.intents())

    def test_a_document_with_no_queue_key_drains_cleanly(self):
        self.seed(notifications=False)
        summary = self.sup.drain_notifications()
        self.assertEqual(summary["attempted"], 0)
        self.assertEqual(summary["pending"], 0)
        self.sup.notifier.send.assert_not_called()

    def test_a_tick_over_a_pre_queue_document_does_not_fail(self):
        self.seed(notifications=False)
        with mock.patch.object(supervisor_mod.gh, "list_open_prs",
                               return_value=[]), \
                mock.patch.object(supervisor_mod.workers, "read_status",
                                  return_value=None), \
                mock.patch.object(supervisor_mod.config, "HEARTBEAT_PATH",
                                  self.root / "heartbeat.json"), \
                mock.patch.object(
                    self.sup, "run_declared",
                    side_effect=declared_phases.provider_phases_suppressed):
            self.sup.tick()
        self.assertIn("notifications", self.durable())

    def test_queue_helper_never_mutates_a_document_it_only_reads(self):
        doc = {"tasks": {}}
        self.assertEqual(notify.queue(doc), {})
        self.assertEqual(notify.backlog(doc, clock.now(TZ))["pending"], 0)


class LateTickWorkIsAlsoDrained(QueueHarness):
    """The provider work after the first drain has its own transaction, and
    a crossed budget threshold there raises HUMAN_REQUIRED. That escalation
    must not wait for the next tick behind an Observer run."""

    def test_an_intent_queued_after_the_first_drain_still_goes_out(self):
        self.seed()

        def late_provider_work(what, bound_seconds, action):
            if what in declared_phases.PROVIDER_PHASES and what != "jev":
                return None
            if what != "jev":
                return action()
            with self.store.transaction() as doc:
                self.sup.notify_out(doc, notify.HUMAN_REQUIRED,
                                    "budget hard stop", "stop spending")

        with mock.patch.object(supervisor_mod.gh, "list_open_prs",
                               return_value=[]), \
                mock.patch.object(supervisor_mod.workers, "read_status",
                                  return_value=None), \
                mock.patch.object(supervisor_mod.config, "HEARTBEAT_PATH",
                                  self.root / "heartbeat.json"), \
                mock.patch.object(self.sup, "run_declared",
                                  side_effect=late_provider_work):
            self.sup.tick()

        self.assertEqual([s["title"] for s in self.sent], ["budget hard stop"])
        self.assertEqual(self.lock_states, [False])
        self.assertTrue(all(i["status"] == notify.DELIVERED
                            for i in self.intents().values()))


# ================================================ stage 2 corrections
#
# Three findings from the stage 2 report, each fixed and pinned here.


class TheTickLimitsAreSharedAcrossBothDrains(QueueHarness):
    """The governed limits are 10 seconds and 20 attempts PER TICK. A tick
    drains twice, so each drain taking a fresh allowance would silently
    double both."""

    def test_two_drains_share_one_attempt_cap(self):
        self.seed()
        for index in range(self.sup.DRAIN_MAX_ATTEMPTS + 10):
            self.enqueue(title=f"t{index}")
        allowance = self.sup.new_drain_allowance()
        self.sup.drain_notifications(allowance)
        self.sup.drain_notifications(allowance)
        self.assertEqual(self.sup.notifier.send.call_count,
                         self.sup.DRAIN_MAX_ATTEMPTS)

    def test_two_drains_share_one_time_budget(self):
        self.seed()
        for index in range(self.sup.DRAIN_MAX_ATTEMPTS):
            self.enqueue(title=f"t{index}")
        with self._budget_clock(self.sup.DRAIN_BUDGET_SECONDS / 4):
            allowance = self.sup.new_drain_allowance()
            self.sup.drain_notifications(allowance)
            first = self.sup.notifier.send.call_count
            self.sup.drain_notifications(allowance)
            total = self.sup.notifier.send.call_count
        self.assertEqual(first, 4)
        self.assertEqual(total, 4, "the second drain must not refill the clock")

    def test_a_tick_mints_exactly_one_allowance_and_reuses_it(self):
        self.seed()
        real = self.sup.new_drain_allowance
        minted = []

        def counting():
            allowance = real()
            minted.append(allowance)
            return allowance

        seen = []
        real_drain = self.sup.drain_notifications

        def watched(allowance=None):
            seen.append(allowance)
            return real_drain(allowance)

        with mock.patch.object(supervisor_mod.gh, "list_open_prs",
                               return_value=[]), \
                mock.patch.object(supervisor_mod.workers, "read_status",
                                  return_value=None), \
                mock.patch.object(supervisor_mod.config, "HEARTBEAT_PATH",
                                  self.root / "heartbeat.json"), \
                mock.patch.object(
                    self.sup, "run_declared",
                    side_effect=declared_phases.provider_phases_suppressed), \
                mock.patch.object(self.sup, "new_drain_allowance",
                                  side_effect=counting), \
                mock.patch.object(self.sup, "drain_notifications",
                                  side_effect=watched):
            self.sup.tick()

        self.assertEqual(len(minted), 1, "one allowance per tick")
        self.assertEqual(len(seen), 2, "two drain points per tick")
        self.assertIs(seen[0], minted[0])
        self.assertIs(seen[1], minted[0])

    def test_a_standalone_drain_is_bounded_independently(self):
        """ctl freeze has no tick to share with, so it gets its own."""
        self.seed()
        for index in range(self.sup.DRAIN_MAX_ATTEMPTS + 3):
            self.enqueue(title=f"t{index}")
        allowance = self.sup.new_drain_allowance()
        allowance.attempts_left = 0                     # a spent tick
        self.sup.drain_notifications(allowance)
        self.assertEqual(self.sup.notifier.send.call_count, 0)

        self.sup.drain_notifications()                  # standalone: fresh
        self.assertEqual(self.sup.notifier.send.call_count,
                         self.sup.DRAIN_MAX_ATTEMPTS)


class TheAllowanceIsRecomputedAfterTheFence(QueueHarness):
    """The fence is a state transaction that waits on the lock, so it can
    itself consume the remainder. Timing the send from before it would hand
    that send more time than the tick actually has."""

    def _clock(self):
        state = {"t": 0.0}
        return state, mock.patch.object(supervisor_mod.time, "monotonic",
                                        side_effect=lambda: state["t"])

    def test_the_send_timeout_is_measured_after_the_fence(self):
        self.seed()
        self.enqueue()
        state, patch = self._clock()
        real_fence = self.sup._fence_next_notification

        def slow_fence():
            state["t"] += 6.0          # the fence eats most of the budget
            return real_fence()

        with patch, mock.patch.object(self.sup, "_fence_next_notification",
                                      side_effect=slow_fence):
            self.sup.drain_notifications()

        timeout = self.sup.notifier.send.call_args.kwargs["timeout"]
        self.assertAlmostEqual(timeout, self.sup.DRAIN_BUDGET_SECONDS - 6.0,
                               places=3)

    def test_a_fence_that_consumes_the_allowance_sends_nothing(self):
        self.seed()
        self.enqueue()
        state, patch = self._clock()
        real_fence = self.sup._fence_next_notification

        def devouring_fence():
            state["t"] += self.sup.DRAIN_BUDGET_SECONDS + 1
            return real_fence()

        with patch, mock.patch.object(self.sup, "_fence_next_notification",
                                      side_effect=devouring_fence):
            summary = self.sup.drain_notifications()

        self.sup.notifier.send.assert_not_called()
        self.assertEqual(summary["attempted"], 0)
        self.assertEqual(summary["skipped_no_budget"], 1)

    def test_an_unsent_intent_is_released_not_suppressed(self):
        self.seed()
        intent_id = self.enqueue()
        state, patch = self._clock()
        real_fence = self.sup._fence_next_notification

        def devouring_fence():
            state["t"] += self.sup.DRAIN_BUDGET_SECONDS + 1
            return real_fence()

        with patch, mock.patch.object(self.sup, "_fence_next_notification",
                                      side_effect=devouring_fence):
            self.sup.drain_notifications()

        stored = self.intent(intent_id)
        self.assertEqual(stored["status"], notify.PENDING)
        self.assertIsNone(stored["next_attempt_at"],
                          "nothing failed, so nothing earns a backoff")
        self.assertIn("NOT_ATTEMPTED", self.outcomes())
        self.assertNotIn("FAILED", self.outcomes())
        self.assertNotIn("AMBIGUOUS", self.outcomes())

    def test_it_is_delivered_on_the_very_next_drain(self):
        self.seed()
        intent_id = self.enqueue()
        state, patch = self._clock()
        real_fence = self.sup._fence_next_notification

        def devouring_fence():
            state["t"] += self.sup.DRAIN_BUDGET_SECONDS + 1
            return real_fence()

        with patch, mock.patch.object(self.sup, "_fence_next_notification",
                                      side_effect=devouring_fence):
            self.sup.drain_notifications()
        self.sup.drain_notifications()

        self.assertEqual(self.sup.notifier.send.call_count, 1)
        self.assertEqual(self.intent(intent_id)["status"], notify.DELIVERED)

    def test_a_lost_release_commit_is_repaired_from_the_ledger(self):
        """Restart safety: the NOT_ATTEMPTED append lands before the state
        write, so convergence releases the intent even if that write is
        lost."""
        self.seed()
        intent_id = self.enqueue()
        state, patch = self._clock()
        real_fence = self.sup._fence_next_notification

        def devouring_fence():
            state["t"] += self.sup.DRAIN_BUDGET_SECONDS + 1
            return real_fence()

        with patch, \
                mock.patch.object(self.sup, "_fence_next_notification",
                                  side_effect=devouring_fence), \
                mock.patch.object(self.sup, "_release_unsent_notification",
                                  side_effect=lambda fenced: self.sup.log(
                                      "NOTIFICATION",
                                      activity_class="ORCHESTRATION",
                                      outcome="NOT_ATTEMPTED",
                                      metadata_redacted={
                                          "intent_id": fenced["intent_id"],
                                          "attempt": fenced["attempt"],
                                          "reason": "DRAIN_BUDGET_EXHAUSTED"})):
            self.sup.drain_notifications()

        self.assertEqual(self.intent(intent_id)["status"], notify.ATTEMPTING)
        self.sup.drain_notifications()
        self.assertEqual(self.intent(intent_id)["status"], notify.DELIVERED)
        self.assertEqual(self.sup.notifier.send.call_count, 1)


class RealTransportAmbiguity(QueueHarness):
    """The REAL Notifier over the REAL http wrapper, with only urllib mocked.

    Making notifier.send raise proves nothing about production: in
    production it never raises - http._send catches everything and returns
    ok=False. The question is whether an uncertain outcome is distinguishable
    from a rejection once it has been through that wrapper.
    """

    # Deliberately NOT webhook-shaped. `Notifier` never validates the URL's
    # shape and this test replaces `urlopen`, so the host is irrelevant to
    # what is under test - and `.invalid` (RFC 2606) can never resolve, so no
    # refactor can turn this into a real request. A Discord-shaped literal
    # here would also trip the RED secret guardrail, which is not a check to
    # buy an exemption from.
    WEBHOOK = "https://notify.invalid/run-002-stage2-test-sink"

    def setUp(self):
        super().setUp()
        # A genuine Notifier, not a Mock - this is the whole point.
        self.sup.notifier = notify.Notifier("run-002")

    def drain_with(self, urlopen_side_effect):
        with mock.patch.dict(os.environ,
                             {"DISCORD_WEBHOOK_URL": self.WEBHOOK}), \
                mock.patch.object(supervisor_mod.notify.http.urllib.request,
                                  "urlopen",
                                  side_effect=urlopen_side_effect) as opened:
            self.sup.drain_notifications()
        return opened

    @staticmethod
    def _http_error(code=500):
        return urllib.error.HTTPError(
            url="https://discord.example/x", code=code, msg="server error",
            hdrs=None, fp=io.BytesIO(b"nope"))

    # ---------------------------------------------------------- ambiguous

    def test_a_read_timeout_is_ambiguous_and_is_never_resent(self):
        self.seed()
        intent_id = self.enqueue()
        self.drain_with(TimeoutError("read timed out"))

        self.assertEqual(self.intent(intent_id)["status"], notify.ATTEMPTING)
        self.assertIn("AMBIGUOUS", self.outcomes())
        self.assertNotIn("FAILED", self.outcomes())

        opened = self.drain_with(TimeoutError("read timed out"))
        self.assertEqual(opened.call_count, 0,
                         "an ambiguous attempt must never be resent")

    def test_a_connect_timeout_is_ambiguous(self):
        self.seed()
        intent_id = self.enqueue()
        self.drain_with(urllib.error.URLError(TimeoutError("connect timeout")))
        self.assertEqual(self.intent(intent_id)["status"], notify.ATTEMPTING)
        self.assertIn("AMBIGUOUS", self.outcomes())

    def test_a_reset_connection_is_ambiguous(self):
        self.seed()
        intent_id = self.enqueue()
        self.drain_with(urllib.error.URLError(ConnectionResetError("reset")))
        self.assertEqual(self.intent(intent_id)["status"], notify.ATTEMPTING)
        self.assertIn("AMBIGUOUS", self.outcomes())

    # ------------------------------------------------------ known failures

    def test_a_refused_connection_is_a_known_failure_and_is_retried(self):
        """Nothing left the machine, so suppressing the retry would lose the
        notification for the whole of an ordinary outage."""
        self.seed()
        intent_id = self.enqueue()
        self.drain_with(urllib.error.URLError(ConnectionRefusedError("refused")))

        stored = self.intent(intent_id)
        self.assertEqual(stored["status"], notify.PENDING)
        self.assertIsNotNone(stored["next_attempt_at"])
        self.assertIn("FAILED", self.outcomes())
        self.assertNotIn("AMBIGUOUS", self.outcomes())

    def test_a_dns_failure_is_a_known_failure_and_is_retried(self):
        self.seed()
        intent_id = self.enqueue()
        self.drain_with(urllib.error.URLError(socket.gaierror("no such host")))
        self.assertEqual(self.intent(intent_id)["status"], notify.PENDING)
        self.assertIn("FAILED", self.outcomes())

    def test_a_server_rejection_is_a_known_failure_and_is_retried(self):
        self.seed()
        intent_id = self.enqueue()
        self.drain_with(self._http_error(500))
        self.assertEqual(self.intent(intent_id)["status"], notify.PENDING)
        self.assertIn("FAILED", self.outcomes())
        self.assertNotIn("AMBIGUOUS", self.outcomes())

    def test_a_rate_limit_is_a_known_failure_and_backs_off(self):
        self.seed()
        intent_id = self.enqueue()
        self.drain_with(self._http_error(429))
        self.assertEqual(self.intent(intent_id)["status"], notify.PENDING)
        self.assertIsNotNone(self.intent(intent_id)["next_attempt_at"])

    # ------------------------------------------------------------ success

    def test_a_real_success_is_delivered_and_not_ambiguous(self):
        self.seed()
        intent_id = self.enqueue()

        class FakeResponse:
            status = 204

            def read(self):
                return b""

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        self.drain_with(lambda *a, **k: FakeResponse())
        self.assertEqual(self.intent(intent_id)["status"], notify.DELIVERED)
        self.assertIn("DELIVERED", self.outcomes())
        self.assertNotIn("AMBIGUOUS", self.outcomes())

    def test_the_send_timeout_actually_reaches_urllib(self):
        self.seed()
        self.enqueue()
        opened = self.drain_with(TimeoutError("x"))
        self.assertLessEqual(opened.call_args.kwargs["timeout"],
                             self.sup.DRAIN_BUDGET_SECONDS)


class TheTransportClassifiesOutcomesItself(unittest.TestCase):
    """http.Response.ambiguous, at the level that decides it."""

    def send(self, side_effect):
        with mock.patch.object(http_mod.urllib.request, "urlopen",
                               side_effect=side_effect):
            return http_mod.post_json("https://example.invalid/x", {"a": 1},
                                      timeout=0.1)

    def test_a_timeout_is_marked_ambiguous(self):
        self.assertTrue(self.send(TimeoutError("t")).ambiguous)

    def test_a_refused_connection_is_not_ambiguous(self):
        response = self.send(urllib.error.URLError(ConnectionRefusedError()))
        self.assertFalse(response.ambiguous)
        self.assertFalse(response.ok)

    def test_a_dns_failure_is_not_ambiguous(self):
        self.assertFalse(
            self.send(urllib.error.URLError(socket.gaierror())).ambiguous)

    def test_an_http_error_is_not_ambiguous(self):
        response = self.send(urllib.error.HTTPError(
            url="u", code=503, msg="m", hdrs=None, fp=io.BytesIO(b"")))
        self.assertFalse(response.ambiguous)
        self.assertEqual(response.status, 503)

    def test_an_unforeseen_failure_defaults_to_ambiguous(self):
        """Fail towards a suppressed retry, never towards a duplicate."""
        self.assertTrue(self.send(RuntimeError("something new")).ambiguous)

    def test_ambiguous_defaults_false_for_every_existing_caller(self):
        self.assertFalse(http_mod.Response(True, 200, "body").ambiguous)

    def test_the_notifier_never_reports_a_delivered_send_as_ambiguous(self):
        result = notify.Notifier("run-002").send("INFO", "t", "b")
        self.assertFalse(result.get("ambiguous"),
                         "no webhook configured is a known failure")


if __name__ == "__main__":
    unittest.main()
