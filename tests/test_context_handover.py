"""The context handover: occupancy, the threshold, and once per session.

WHAT THESE TESTS PROVE, AND THE LIMIT OF IT.

  COMPONENT IMPLEMENTED: occupancy is read from a transcript's `usage`
  records, the threshold fires at exactly the boundary, an unreadable
  transcript is refused rather than reported as empty, and a second run
  is suppressed by the marker.

  CONNECTED PATH, SIMULATED SERVICES: the send goes through
  `control.notify.Notifier`, the mechanism the experiment already uses -
  not a second one written here. The transport is the notifier's dry run.

  NOT A DEPLOYED PATH. No Discord message has ever been sent from this
  repository. `DISCORD_WEBHOOK_URL` is unset (`env-var-names.md`), so a
  real send returns `DISCORD_WEBHOOK_URL_NOT_SET` - asserted below rather
  than assumed. A delivered notification needs that secret, which is
  Stage 3, and the operator's authorisation.

NO TRANSCRIPT OF A REAL SESSION IS READ HERE. Every fixture is written by
this file and holds nothing but synthetic usage integers.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from control import notify  # noqa: E402
from scripts import context_handover as ch  # noqa: E402

WINDOW = 1_000_000


def _line(total: int) -> str:
    return json.dumps({"type": "assistant", "message": {
        "role": "assistant", "model": "claude-opus-5",
        "usage": {"input_tokens": total, "cache_read_input_tokens": 0,
                  "cache_creation_input_tokens": 0}}})


class OccupancyCase(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def transcript(self, *totals: int, trailing: str = "") -> Path:
        path = self.root / "t.jsonl"
        path.write_text("\n".join(_line(t) for t in totals) + trailing,
                        encoding="utf-8")
        return path

    def test_the_last_assistant_message_is_the_occupancy(self):
        """Every request re-sends the whole conversation, so the LAST
        input total is what the window holds - not the sum, and not the
        largest."""
        self.assertEqual(ch.last_input_total(self.transcript(10, 900, 400)), 400)

    def test_the_three_input_fields_are_added_together(self):
        path = self.root / "t.jsonl"
        path.write_text(json.dumps({"message": {
            "role": "assistant",
            "usage": {"input_tokens": 1, "cache_read_input_tokens": 20,
                      "cache_creation_input_tokens": 300}}}), encoding="utf-8")
        self.assertEqual(ch.last_input_total(path), 321)

    def test_output_tokens_are_not_occupancy(self):
        path = self.root / "t.jsonl"
        path.write_text(json.dumps({"message": {
            "role": "assistant",
            "usage": {"input_tokens": 5, "output_tokens": 99999}}}),
            encoding="utf-8")
        self.assertEqual(ch.last_input_total(path), 5)

    def test_a_half_written_final_line_does_not_lose_the_record(self):
        """A transcript is appended to while the session runs, so the last
        line can be a fragment. That must not read as UNREADABLE."""
        path = self.transcript(10, 400, trailing="\n{\"message\": {\"ro")
        self.assertEqual(ch.last_input_total(path), 400)

    def test_a_transcript_with_no_usage_is_UNREADABLE_not_zero(self):
        path = self.root / "t.jsonl"
        path.write_text(json.dumps({"message": {"role": "user"}}),
                        encoding="utf-8")
        self.assertEqual(ch.last_input_total(path), ch.UNREADABLE)
        self.assertEqual(ch.percent(ch.UNREADABLE, WINDOW), ch.UNREADABLE)

    def test_a_missing_transcript_is_UNREADABLE(self):
        self.assertEqual(ch.last_input_total(self.root / "nope.jsonl"),
                         ch.UNREADABLE)

    def test_an_unreadable_transcript_exits_nonzero_and_sends_nothing(self):
        path = self.root / "t.jsonl"
        path.write_text("not json at all", encoding="utf-8")
        with mock.patch.object(notify.Notifier, "send") as send:
            code = ch.main([str(path), "--window", str(WINDOW)])
        self.assertEqual(code, 2)
        send.assert_not_called()


class ThresholdCase(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.handover = self.root / "handover.txt"
        self.handover.write_text("resume with: read handover section 51\n",
                                 encoding="utf-8")
        self.marker = self.root / "marker"

    def run_at(self, total: int, *extra: str, send: bool = False):
        path = self.root / "t.jsonl"
        path.write_text(_line(total), encoding="utf-8")
        argv = [str(path), "--window", str(WINDOW),
                "--handover", str(self.handover), "--marker", str(self.marker)]
        if send:
            argv.append("--send")
        argv.extend(extra)
        with mock.patch.object(notify.Notifier, "send",
                               wraps=notify.Notifier("run-002", dry_run=True).send) as spy:
            ch.main(argv)
        return spy

    def test_below_the_threshold_nothing_is_sent(self):
        self.run_at(499_999).assert_not_called()

    def test_exactly_at_the_threshold_it_fires(self):
        """AT OR ABOVE. A boundary that only fires above 50% means the
        notification for a session sitting exactly on it never arrives."""
        self.run_at(500_000).assert_called_once()

    def test_above_the_threshold_it_fires(self):
        self.run_at(750_000).assert_called_once()

    def test_the_threshold_is_configurable_without_touching_the_code(self):
        self.run_at(300_000, "--threshold", "25").assert_called_once()

    def test_without_send_the_notifier_runs_dry(self):
        spy = self.run_at(600_000)
        self.assertTrue(spy.call_args is not None)
        self.assertFalse(self.marker.exists(),
                         "a dry run must not consume the once-per-session slot")

    def test_a_dry_run_leaves_the_marker_unwritten_so_a_real_send_can_follow(self):
        self.run_at(600_000)
        self.run_at(600_000).assert_called_once()

    def test_once_per_session_is_the_marker_and_nothing_else(self):
        self.marker.write_text("sent\n", encoding="utf-8")
        self.run_at(900_000).assert_not_called()

    def test_the_body_carries_the_handover_text_and_the_numbers(self):
        body = ch.handover_body(62.5, 625_000, WINDOW, "resume: section 51")
        self.assertIn("62.5%", body)
        self.assertIn("625,000", body)
        self.assertIn("resume: section 51", body)

    def test_the_display_never_rounds_up_to_the_threshold_it_is_under(self):
        """499,999 of 1,000,000 is 49.9999%. Printed as `50.0` next to
        `below threshold 50.0` it reads as a contradiction, and the number
        a human hands over on must not overstate the window."""
        self.assertEqual(ch.shown(ch.percent(499_999, WINDOW)), "49.9")
        self.assertEqual(ch.shown(ch.percent(500_000, WINDOW)), "50.0")

    def test_the_body_says_the_session_is_not_being_cleared(self):
        """The one thing a handover must never be mistaken for."""
        body = ch.handover_body(55.0, 550_000, WINDOW, "x")
        self.assertIn("NOT being cleared", body)


class NothingIsDeployedCase(unittest.TestCase):
    def test_a_real_send_today_refuses_for_want_of_the_webhook(self):
        """`DISCORD_WEBHOOK_URL` is a Stage 3 secret and is unset. This
        asserts the refusal rather than claiming the alert works."""
        with mock.patch.dict("os.environ", {}, clear=True):
            result = notify.Notifier("run-002", dry_run=False).send(
                notify.ATTENTION, "probe", "body")
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "DISCORD_WEBHOOK_URL_NOT_SET")

    def test_the_script_sends_through_the_shared_notifier_not_its_own_http(self):
        source = Path(ch.__file__).read_text(encoding="utf-8")
        for forbidden in ("urllib", "requests", "httpx", "socket"):
            self.assertNotIn(forbidden, source)

    def test_the_script_prints_no_transcript_content(self):
        """It may print integers it computed and the fields it names. A
        print of a message body is how a transcript's secrets escape."""
        source = Path(ch.__file__).read_text(encoding="utf-8")
        self.assertNotIn("print(record", source)
        self.assertNotIn("print(message", source)
        self.assertNotIn("print(raw", source)


if __name__ == "__main__":
    unittest.main()
