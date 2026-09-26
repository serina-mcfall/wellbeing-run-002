"""C-14.2: the pure three-source classifier (control/merge_invariant.py).

No mocks and no I/O: classify() is a function of its three observations, which
is exactly what lets the Supervisor and the Watchdog reach the same verdict
from independently gathered evidence.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from control import merge_invariant as mi  # noqa: E402
from control import state as state_mod  # noqa: E402

TASK = "TASK-001"
PR = 100
SHA = "a" * 40


def gh_obs(observed=True, merged=True, sha=SHA):
    return mi.GithubObservation(observed=observed, merged=merged, merged_sha=sha)


def st(task_state="REVIEW", merged=False, debt=()):
    return mi.StateView(task_state=task_state, record_present=True,
                        record_merged=merged, debt_ids_present=frozenset(debt))


def lg(*, local=0, external=0, readable=True, claimed=(), status=None,
       failed_event=False, blocked=0, lower="2026-09-26T10:00:00+12:00"):
    event = {"metadata_redacted": {}}
    return mi.LedgerView(
        readable=readable, local_merged=tuple([event] * local),
        external_merged=tuple([event] * external),
        claimed_debt_ids=frozenset(claimed), claimed_debt_status=status,
        debt_recording_failed=failed_event, blocked_merged_in_window=blocked,
        window_lower=lower, window_lower_source="last_review_at" if lower else None,
    )


def verdict_of(github=None, state=None, ledger=None):
    return mi.classify(task_id=TASK, pr_number=PR,
                       github=github or gh_obs(), state_view=state or st(),
                       ledger_view=ledger or lg())


class TestTruthTable(unittest.TestCase):

    def test_unobserved_without_contradiction_defers_with_no_verdict(self):
        """Row 1, narrowed: a failed observation is not a violation. None is
        the answer - deliberately NOT a fifth verdict, and deliberately not
        ORDINARY_EXTERNAL, because we do not know whether it merged."""
        self.assertIsNone(verdict_of(github=gh_obs(observed=False, merged=False)))

    def test_unobserved_with_a_durable_local_claim_is_unprovable(self):
        v = verdict_of(github=gh_obs(observed=False, merged=False),
                       ledger=lg(local=1))
        self.assertEqual(v.verdict, mi.UNPROVABLE)
        self.assertTrue(v.dangerous)
        self.assertIn(mi.GITHUB_UNOBSERVED, v.evidence_codes)
        self.assertIn(mi.LOCAL_MERGE_EVIDENCE, v.evidence_codes)

    def test_unobserved_does_not_hide_a_provable_debt_divergence(self):
        v = verdict_of(github=gh_obs(observed=False, merged=False),
                       state=st(task_state="COMPLETE", merged=True),
                       ledger=lg(local=1, claimed={"d1"}, status=mi.DEBT_RECORDED))
        self.assertEqual(v.verdict, mi.DEBT_DIVERGENCE)

    def test_unobserved_with_an_unreadable_ledger_still_fails_closed(self):
        v = verdict_of(github=gh_obs(observed=False, merged=False),
                       ledger=lg(readable=False))
        self.assertEqual(v.verdict, mi.UNPROVABLE)
        self.assertIn(mi.LEDGER_INCOMPLETE, v.evidence_codes)

    def test_unobserved_deferral_does_not_apply_when_state_claims_merged(self):
        """A blocked-MERGED ambiguity is only reached once GitHub is observed
        as merged; with no observation and no contradiction we still defer."""
        self.assertIsNone(verdict_of(github=gh_obs(observed=False, merged=False),
                                     ledger=lg(blocked=1)))

    def test_github_agreeing_it_is_not_merged_is_consistent(self):
        self.assertIsNone(verdict_of(github=gh_obs(merged=False)))

    def test_unreadable_ledger_is_unprovable(self):
        v = verdict_of(ledger=lg(readable=False))
        self.assertEqual(v.verdict, mi.UNPROVABLE)
        self.assertIn(mi.LEDGER_INCOMPLETE, v.evidence_codes)

    def test_missing_window_lower_bound_is_unprovable(self):
        v = verdict_of(ledger=lg(lower=None))
        self.assertEqual(v.verdict, mi.UNPROVABLE)
        self.assertIn(mi.NO_WINDOW_LOWER_BOUND, v.evidence_codes)

    def test_blocked_merged_in_window_is_unprovable(self):
        v = verdict_of(ledger=lg(blocked=1))
        self.assertEqual(v.verdict, mi.UNPROVABLE)
        self.assertIn(mi.BLOCKED_MERGED_IN_WINDOW, v.evidence_codes)

    def test_blocked_merged_is_irrelevant_when_local_evidence_exists(self):
        v = verdict_of(ledger=lg(local=1, blocked=1))
        self.assertEqual(v.verdict, mi.LOST_LOCAL_COMMIT)

    def test_local_evidence_with_unmerged_state_is_lost_local_commit(self):
        v = verdict_of(ledger=lg(local=1))
        self.assertEqual(v.verdict, mi.LOST_LOCAL_COMMIT)
        self.assertTrue(v.dangerous)
        self.assertIn(mi.LOCAL_MERGE_EVIDENCE, v.evidence_codes)

    def test_no_local_evidence_is_ordinary_external_and_not_dangerous(self):
        v = verdict_of()
        self.assertEqual(v.verdict, mi.ORDINARY_EXTERNAL)
        self.assertFalse(v.dangerous)
        self.assertFalse(v.freezable)
        self.assertIn(mi.NO_LOCAL_MERGE_EVIDENCE, v.evidence_codes)

    def test_external_evidence_alone_is_not_local_evidence(self):
        v = verdict_of(ledger=lg(external=2))
        self.assertEqual(v.verdict, mi.ORDINARY_EXTERNAL)

    def test_recorded_debt_present_in_state_is_consistent(self):
        v = verdict_of(state=st(merged=True, debt={"d1"}),
                       ledger=lg(local=1, claimed={"d1"}, status=mi.DEBT_RECORDED))
        self.assertIsNone(v)

    def test_recorded_debt_missing_from_state_is_debt_divergence(self):
        v = verdict_of(state=st(task_state="COMPLETE", merged=True, debt=()),
                       ledger=lg(local=1, claimed={"d1", "d2"},
                                 status=mi.DEBT_RECORDED))
        self.assertEqual(v.verdict, mi.DEBT_DIVERGENCE)
        self.assertEqual(v.missing_debt_ids, ("d1", "d2"))
        self.assertIn(mi.DEBT_IDS_MISSING, v.evidence_codes)

    def test_not_required_debt_is_consistent(self):
        v = verdict_of(state=st(task_state="COMPLETE", merged=True),
                       ledger=lg(local=1, status=mi.DEBT_NOT_REQUIRED))
        self.assertIsNone(v)

    def test_failed_debt_with_its_event_is_already_annunciated(self):
        v = verdict_of(state=st(task_state="COMPLETE", merged=True),
                       ledger=lg(local=1, status=mi.DEBT_FAILED, failed_event=True))
        self.assertIsNone(v)

    def test_failed_debt_without_its_event_is_unprovable(self):
        v = verdict_of(state=st(task_state="COMPLETE", merged=True),
                       ledger=lg(local=1, status=mi.DEBT_FAILED, failed_event=False))
        self.assertEqual(v.verdict, mi.UNPROVABLE)
        self.assertIn(mi.DEBT_FAILED_WITHOUT_EVENT, v.evidence_codes)

    def test_absent_debt_claim_is_not_a_divergence(self):
        v = verdict_of(state=st(task_state="COMPLETE", merged=True),
                       ledger=lg(local=1, status=None))
        self.assertIsNone(v)

    def test_local_evidence_but_github_not_merged_is_unprovable(self):
        v = verdict_of(github=gh_obs(merged=False), ledger=lg(local=1))
        self.assertEqual(v.verdict, mi.UNPROVABLE)

    def test_only_the_four_governed_verdicts_are_reachable(self):
        seen = set()
        for github in (gh_obs(), gh_obs(merged=False), gh_obs(observed=False, merged=False)):
            for state in (st(), st(merged=True), st(task_state="COMPLETE", merged=True)):
                for ledger in (lg(), lg(local=1), lg(readable=False), lg(blocked=1),
                               lg(local=1, claimed={"d"}, status=mi.DEBT_RECORDED),
                               lg(local=1, status=mi.DEBT_FAILED)):
                    v = mi.classify(task_id=TASK, pr_number=PR, github=github,
                                    state_view=state, ledger_view=ledger)
                    if v is not None:
                        seen.add(v.verdict)
        self.assertTrue(seen <= set(mi.VERDICTS), seen)


class TestAcceptedFindingsIsNotAnInput(unittest.TestCase):

    def test_state_view_has_no_accepted_findings_member(self):
        """The C-10.2 trap, enforced structurally rather than by discipline."""
        self.assertNotIn("accepted_findings", mi.StateView.__dataclass_fields__)

    def test_debt_verdict_comes_from_the_ledger_not_from_state(self):
        v = verdict_of(state=st(task_state="COMPLETE", merged=True, debt=()),
                       ledger=lg(local=1, claimed={"d1"}, status=mi.DEBT_RECORDED))
        self.assertEqual(v.verdict, mi.DEBT_DIVERGENCE)


class TestFreezability(unittest.TestCase):

    def test_f19_freezability_is_derived_from_the_transition_table(self):
        for task_state in state_mod.TASK_STATES:
            with self.subTest(state=task_state):
                expected = "FROZEN" in state_mod.ALLOWED_TRANSITIONS[task_state]
                self.assertEqual(mi.freezable(task_state), expected)

    def test_terminal_and_merge_ready_states_are_not_freezable(self):
        for task_state in ("COMPLETE", "MERGED", "MERGE_READY"):
            self.assertFalse(mi.freezable(task_state), task_state)

    def test_review_is_freezable(self):
        self.assertTrue(mi.freezable("REVIEW"))

    def test_missing_state_is_not_freezable(self):
        self.assertFalse(mi.freezable(None))

    def test_verdict_carries_freezability_of_its_task_state(self):
        self.assertTrue(verdict_of(state=st("REVIEW"), ledger=lg(local=1)).freezable)
        self.assertFalse(verdict_of(state=st("MERGE_READY"),
                                    ledger=lg(local=1)).freezable)


class TestDetectedExternallyCompatibility(unittest.TestCase):

    def build(self, *events):
        return mi.build_ledger_view(
            merged_events=events, guardrail_events=(), debt_failed_events=(),
            readable=True, unreadable_lines=0, window_lower="2026-01-01T00:00:00+13:00",
            window_lower_source="last_review_at", window_upper="2026-12-31T00:00:00+13:00")

    def test_absent_key_counts_as_local(self):
        view = self.build({"metadata_redacted": {}})
        self.assertEqual(len(view.local_merged), 1)
        self.assertEqual(len(view.external_merged), 0)

    def test_explicit_false_counts_as_local(self):
        view = self.build({"metadata_redacted": {"detected_externally": False}})
        self.assertEqual(len(view.local_merged), 1)

    def test_explicit_true_counts_as_external(self):
        view = self.build({"metadata_redacted": {"detected_externally": True}})
        self.assertEqual(len(view.external_merged), 1)
        self.assertEqual(len(view.local_merged), 0)

    def test_debt_claims_are_read_only_from_local_events(self):
        view = self.build({"metadata_redacted": {"detected_externally": True,
                                                 "debt_ids": ["x"]}})
        self.assertEqual(view.claimed_debt_ids, frozenset())


class TestWindowBounds(unittest.TestCase):

    def test_last_review_at_is_preferred(self):
        lower, source = mi.window_lower_bound(
            {"history": [{"to": "PR_OPEN", "at": "A"}]}, {"last_review_at": "B"})
        self.assertEqual((lower, source), ("B", "last_review_at"))

    def test_pr_open_history_is_the_fallback(self):
        lower, source = mi.window_lower_bound(
            {"history": [{"to": "ACTIVE", "at": "X"}, {"to": "PR_OPEN", "at": "A"}]},
            {"last_review_at": None})
        self.assertEqual((lower, source), ("A", "pr_open_history"))

    def test_earliest_pr_open_entry_wins(self):
        lower, _ = mi.window_lower_bound(
            {"history": [{"to": "PR_OPEN", "at": "A"}, {"to": "PR_OPEN", "at": "Z"}]},
            {})
        self.assertEqual(lower, "A")

    def test_no_bound_is_reported_as_none(self):
        self.assertEqual(mi.window_lower_bound({"history": []}, {}), (None, None))

    def test_blocked_events_outside_the_window_do_not_count(self):
        view = mi.build_ledger_view(
            merged_events=(), debt_failed_events=(), readable=True, unreadable_lines=0,
            guardrail_events=(
                {"timestamp": "2026-01-01T00:00:00+13:00",
                 "metadata_redacted": {"blocked_event_type": "MERGED"}},
                {"timestamp": "2026-06-01T00:00:00+13:00",
                 "metadata_redacted": {"blocked_event_type": "MERGED"}},
            ),
            window_lower="2026-05-01T00:00:00+13:00", window_lower_source="last_review_at",
            window_upper="2026-07-01T00:00:00+13:00")
        self.assertEqual(view.blocked_merged_in_window, 1)

    def test_blocked_events_of_other_types_do_not_count(self):
        view = mi.build_ledger_view(
            merged_events=(), debt_failed_events=(), readable=True, unreadable_lines=0,
            guardrail_events=({"timestamp": "2026-06-01T00:00:00+13:00",
                               "metadata_redacted": {"blocked_event_type": "REVIEW_RESULT"}},),
            window_lower="2026-05-01T00:00:00+13:00", window_lower_source="last_review_at",
            window_upper="2026-07-01T00:00:00+13:00")
        self.assertEqual(view.blocked_merged_in_window, 0)


class TestFingerprint(unittest.TestCase):

    def base(self):
        v = verdict_of(ledger=lg(local=1))
        return v, gh_obs(), st(), lg(local=1)

    def test_identical_evidence_gives_an_identical_fingerprint(self):
        v, g, s, l = self.base()
        self.assertEqual(mi.fingerprint(v, g, s, l), mi.fingerprint(v, g, s, l))

    def test_changed_merged_sha_changes_the_fingerprint(self):
        v, g, s, l = self.base()
        self.assertNotEqual(mi.fingerprint(v, g, s, l),
                            mi.fingerprint(v, gh_obs(sha="b" * 40), s, l))

    def test_missing_debt_ids_are_part_of_the_fingerprint(self):
        _, g, s, l = self.base()
        one = mi.MergeVerdict(mi.DEBT_DIVERGENCE, True, TASK, PR, "X",
                              missing_debt_ids=("d1",))
        two = mi.MergeVerdict(mi.DEBT_DIVERGENCE, True, TASK, PR, "X",
                              missing_debt_ids=("d1", "d2"))
        self.assertNotEqual(mi.fingerprint(one, g, s, l), mi.fingerprint(two, g, s, l))

    def test_free_text_summary_is_not_part_of_the_fingerprint(self):
        _, g, s, l = self.base()
        one = mi.MergeVerdict(mi.UNPROVABLE, True, TASK, PR, "X", summary="one")
        two = mi.MergeVerdict(mi.UNPROVABLE, True, TASK, PR, "X", summary="quite other")
        self.assertEqual(mi.fingerprint(one, g, s, l), mi.fingerprint(two, g, s, l))

    def test_blocked_count_is_a_boolean_indicator_not_a_count(self):
        v, g, s, _ = self.base()
        self.assertEqual(mi.fingerprint(v, g, s, lg(local=1, blocked=1)),
                         mi.fingerprint(v, g, s, lg(local=1, blocked=7)))
        self.assertNotEqual(mi.fingerprint(v, g, s, lg(local=1, blocked=0)),
                            mi.fingerprint(v, g, s, lg(local=1, blocked=1)))

    def test_ledger_readability_is_part_of_the_fingerprint(self):
        v, g, s, _ = self.base()
        self.assertNotEqual(mi.fingerprint(v, g, s, lg(local=1, readable=True)),
                            mi.fingerprint(v, g, s, lg(local=1, readable=False)))

    def test_fingerprint_is_a_hex_sha256(self):
        v, g, s, l = self.base()
        value = mi.fingerprint(v, g, s, l)
        self.assertEqual(len(value), 64)
        int(value, 16)


class TestNotificationStatus(unittest.TestCase):

    def test_success_is_delivered_with_no_code(self):
        self.assertEqual(mi.notification_status({"ok": True}), (True, None))

    def test_existing_finite_reasons_are_reused_verbatim(self):
        self.assertEqual(
            mi.notification_status({"ok": False, "reason": "DISCORD_WEBHOOK_URL_NOT_SET"}),
            (False, "DISCORD_WEBHOOK_URL_NOT_SET"))
        self.assertEqual(
            mi.notification_status({"ok": False,
                                    "reason": "BLOCKED_SECRET_IN_NOTIFICATION"}),
            (False, "BLOCKED_SECRET_IN_NOTIFICATION"))

    def test_raw_upstream_text_collapses_to_one_finite_code(self):
        delivered, code = mi.notification_status(
            {"ok": False, "status": 502, "error": "upstream said something unbounded"})
        self.assertFalse(delivered)
        self.assertEqual(code, mi.NOTIFICATION_SEND_FAILED)

    def test_missing_result_fails_closed(self):
        self.assertEqual(mi.notification_status(None), (False, mi.NOTIFICATION_SEND_FAILED))


if __name__ == "__main__":
    unittest.main()


class TestWindowIsComparedAsInstants(unittest.TestCase):
    """Regression for the review finding: the window was compared as ISO
    STRINGS. clock.iso() writes seconds, Ledger.append() writes milliseconds,
    and Pacific/Auckland's April fall-back repeats an hour across two offsets,
    so string order and chronological order genuinely disagree - and the
    disagreement failed OPEN, turning UNPROVABLE into ORDINARY_EXTERNAL."""

    def window(self, lower, event, upper):
        return mi.build_ledger_view(
            merged_events=(), debt_failed_events=(), readable=True, unreadable_lines=0,
            guardrail_events=({"timestamp": event,
                               "metadata_redacted": {"blocked_event_type": "MERGED"}},),
            window_lower=lower, window_lower_source="last_review_at",
            window_upper=upper)

    def verdict(self, lower, event, upper):
        return mi.classify(task_id=TASK, pr_number=PR, github=gh_obs(),
                           state_view=st(), ledger_view=self.window(lower, event, upper))

    def test_auckland_fall_back_event_inside_the_window_is_counted(self):
        """The exact instants demonstrated in review. 02:15+12:00 is 45 minutes
        AFTER 02:30+13:00, but sorts before it as a string."""
        lower = "2027-04-04T02:30:00+13:00"       # 2027-04-03T13:30Z
        event = "2027-04-04T02:15:00.000+12:00"   # 2027-04-03T14:15Z - inside
        upper = "2027-04-04T05:00:00+12:00"       # 2027-04-03T17:00Z
        self.assertLess(event, lower, "premise: the strings sort the wrong way")
        self.assertEqual(self.window(lower, event, upper).blocked_merged_in_window, 1)
        v = self.verdict(lower, event, upper)
        self.assertEqual(v.verdict, mi.UNPROVABLE)
        self.assertTrue(v.dangerous)
        self.assertIn(mi.BLOCKED_MERGED_IN_WINDOW, v.evidence_codes)

    def test_mixed_seconds_and_milliseconds_precision_within_one_offset(self):
        lower = "2026-06-01T12:00:00+12:00"            # seconds, clock.iso()
        event = "2026-06-01T12:00:00.500+12:00"        # milliseconds, ledger
        upper = "2026-06-01T12:00:01+12:00"
        self.assertEqual(self.window(lower, event, upper).blocked_merged_in_window, 1)

    def test_exact_lower_bound_is_inclusive(self):
        at = "2026-06-01T12:00:00.000+12:00"
        self.assertEqual(self.window("2026-06-01T12:00:00+12:00", at,
                                     "2026-06-02T00:00:00+12:00")
                         .blocked_merged_in_window, 1)

    def test_exact_upper_bound_is_inclusive(self):
        at = "2026-06-01T12:00:00.000+12:00"
        self.assertEqual(self.window("2026-06-01T00:00:00+12:00", at,
                                     "2026-06-01T12:00:00+12:00")
                         .blocked_merged_in_window, 1)

    def test_event_genuinely_outside_the_window_stays_outside(self):
        lower, upper = "2026-06-01T00:00:00+12:00", "2026-06-02T00:00:00+12:00"
        for at in ("2026-05-31T23:59:59.999+12:00", "2026-06-02T00:00:00.001+12:00"):
            with self.subTest(at=at):
                self.assertEqual(self.window(lower, at, upper).blocked_merged_in_window, 0)
        self.assertEqual(self.verdict(lower, "2026-05-01T00:00:00.000+12:00", upper)
                         .verdict, mi.ORDINARY_EXTERNAL)

    def test_no_naive_datetime_is_ever_compared(self):
        self.assertIsNone(mi._instant("2026-06-01T12:00:00"))      # no offset
        naive = self.window("2026-06-01T00:00:00+12:00", "2026-06-01T12:00:00",
                            "2026-06-02T00:00:00+12:00")
        self.assertEqual(naive.blocked_merged_in_window, 1)        # fails closed


class TestMalformedTimestampsFailClosed(unittest.TestCase):

    def window(self, lower, event, upper):
        return mi.build_ledger_view(
            merged_events=(), debt_failed_events=(), readable=True, unreadable_lines=0,
            guardrail_events=({"timestamp": event,
                               "metadata_redacted": {"blocked_event_type": "MERGED"}},),
            window_lower=lower, window_lower_source="last_review_at",
            window_upper=upper)

    GOOD_LOW, GOOD_UP = "2026-06-01T00:00:00+12:00", "2026-06-02T00:00:00+12:00"

    def test_malformed_event_timestamp_is_counted_in_window(self):
        for bad in ("not-a-timestamp", "", None, 12345, "2026-13-45T99:99:99+12:00"):
            with self.subTest(bad=bad):
                view = self.window(self.GOOD_LOW, bad, self.GOOD_UP)
                self.assertEqual(view.blocked_merged_in_window, 1)
                v = mi.classify(task_id=TASK, pr_number=PR, github=gh_obs(),
                                state_view=st(), ledger_view=view)
                self.assertEqual(v.verdict, mi.UNPROVABLE)

    def test_malformed_lower_bound_fails_closed(self):
        view = self.window("not-a-timestamp", "2026-06-01T12:00:00.000+12:00",
                           self.GOOD_UP)
        self.assertIsNone(view.window_lower)
        self.assertIsNone(view.window_lower_source)
        v = mi.classify(task_id=TASK, pr_number=PR, github=gh_obs(),
                        state_view=st(), ledger_view=view)
        self.assertEqual(v.verdict, mi.UNPROVABLE)
        self.assertIn(mi.NO_WINDOW_LOWER_BOUND, v.evidence_codes)

    def test_malformed_upper_bound_fails_closed(self):
        view = self.window(self.GOOD_LOW, "2026-06-01T12:00:00.000+12:00",
                           "not-a-timestamp")
        self.assertIsNone(view.window_lower)
        v = mi.classify(task_id=TASK, pr_number=PR, github=gh_obs(),
                        state_view=st(), ledger_view=view)
        self.assertEqual(v.verdict, mi.UNPROVABLE)

    def test_no_parse_exception_text_is_carried_anywhere(self):
        view = self.window("not-a-timestamp", "also-bad", "still-bad")
        blob = repr(view)
        for leak in ("Invalid isoformat", "ValueError", "Traceback", "fromisoformat"):
            self.assertNotIn(leak, blob)


class TestLedgerInspection(unittest.TestCase):
    """Ledger.inspect() is the dedicated fail-closed path for invariant work.
    read()/events()/last()/count() keep their ordinary reporting semantics."""

    def setUp(self):
        import tempfile
        from control import ledger as ledger_mod
        self.ledger_mod = ledger_mod
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "ledger.jsonl"
        self.ledger = ledger_mod.Ledger(path=self.path, tz="Pacific/Auckland")

    def write(self, *lines):
        self.path.write_text("".join(l + "\n" for l in lines), encoding="utf-8")

    def test_all_lines_parse_is_complete(self):
        self.write('{"event_type":"MERGED","task_id":"T","pr_id":1}')
        r = self.ledger.inspect(task_id="T", pr_id=1, event_types=("MERGED",))
        self.assertTrue(r.complete)
        self.assertEqual(r.unreadable_lines, 0)
        self.assertEqual(len(r.events), 1)
        self.assertIsNone(r.error)

    def test_malformed_line_is_incomplete_but_keeps_parsed_evidence(self):
        self.write('{"event_type":"MERGED","task_id":"T","pr_id":1}', '{truncated')
        r = self.ledger.inspect(task_id="T", pr_id=1, event_types=("MERGED",))
        self.assertFalse(r.complete)
        self.assertEqual(r.unreadable_lines, 1)
        self.assertEqual(len(r.events), 1)

    def test_filtering_cannot_hide_incompleteness(self):
        self.write('{"event_type":"OTHER"}', '{truncated')
        r = self.ledger.inspect(task_id="NOBODY", event_types=("MERGED",))
        self.assertEqual(r.events, ())
        self.assertFalse(r.complete)

    def test_missing_file_is_incomplete_with_a_finite_code(self):
        self.path.unlink()
        r = self.ledger.inspect(event_types=("MERGED",))
        self.assertFalse(r.complete)
        self.assertEqual(r.error, self.ledger_mod.LEDGER_FILE_MISSING)
        self.assertEqual(r.events, ())

    def test_unopenable_ledger_is_incomplete_with_a_finite_code(self):
        unopenable = self.ledger_mod.Ledger.__new__(self.ledger_mod.Ledger)
        unopenable.path = Path(self.tmp.name)          # a directory
        r = self.ledger_mod.Ledger.inspect(unopenable, event_types=("MERGED",))
        self.assertFalse(r.complete)
        self.assertEqual(r.error, self.ledger_mod.LEDGER_UNREADABLE)

    def test_error_codes_are_finite_and_never_os_text(self):
        self.path.unlink()
        r = self.ledger.inspect(event_types=("MERGED",))
        self.assertIn(r.error, {self.ledger_mod.LEDGER_FILE_MISSING,
                                self.ledger_mod.LEDGER_UNREADABLE})
        self.assertNotIn("No such file", str(r.error))

    def test_ordinary_readers_keep_their_empty_ledger_semantics(self):
        self.path.unlink()
        self.assertEqual(list(self.ledger.read()), [])
        self.assertEqual(self.ledger.events("MERGED"), [])
        self.assertIsNone(self.ledger.last("MERGED"))
        self.assertEqual(self.ledger.count(), 0)

    def test_a_missing_ledger_cannot_read_as_an_ordinary_external_merge(self):
        """The negative conclusion 'no local merge evidence' requires a ledger
        we could actually read."""
        self.path.unlink()
        inspection = self.ledger.inspect(task_id="T", pr_id=1, event_types=("MERGED",))
        view = mi.build_ledger_view(
            merged_events=inspection.events, guardrail_events=(), debt_failed_events=(),
            readable=inspection.complete, unreadable_lines=inspection.unreadable_lines,
            window_lower="2026-06-01T00:00:00+12:00", window_lower_source="last_review_at",
            window_upper="2026-06-02T00:00:00+12:00")
        v = mi.classify(task_id=TASK, pr_number=PR, github=gh_obs(),
                        state_view=st(), ledger_view=view)
        self.assertEqual(v.verdict, mi.UNPROVABLE)
        self.assertIn(mi.LEDGER_INCOMPLETE, v.evidence_codes)
