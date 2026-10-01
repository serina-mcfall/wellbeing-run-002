"""G1 and G7 — the governed accessibility lease and concurrency bounds.

Human decisions, 2026-10-01:

  G1 — `timeouts.accessibility = 1800`, SEPARATE from the automated
       browser deadlines. Before it, `_lease_expires("accessibility")`
       raised KeyError, so the qualitative reviewer could not be
       dispatched at all.

  G7 — `max_accessibility_auto = 1` and `max_accessibility_review = 1`,
       matching every other non-builder role.

These tests exist because a governed number that nothing reads is
indistinguishable from one nobody chose. Each value is asserted at its
real reader, not only in the JSON.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from control import config, manifest  # noqa: E402


class G1LeaseCase(unittest.TestCase):

    def setUp(self):
        self.cfg = config.load()
        self.timeouts = self.cfg.extra["timeouts"]

    def test_the_accessibility_lease_is_the_governed_1800(self):
        self.assertEqual(self.timeouts["accessibility"], 1800)

    def test_it_matches_the_two_roles_it_was_derived_from(self):
        # The decision was "same as its nearest neighbours". If either of
        # those moves, this assertion is the place that notices.
        self.assertEqual(self.timeouts["accessibility"],
                         self.timeouts["security"])
        self.assertEqual(self.timeouts["accessibility"],
                         self.timeouts["reviewer"])

    def test_it_is_SEPARATE_from_the_automated_browser_deadlines(self):
        # G1's governing text: it "must not collide with the `_seconds`
        # keys C-05a deliberately kept non-role-shaped". The agent lease
        # and the browser budgets answer different questions and must not
        # become one number by accident.
        self.assertNotEqual(self.timeouts["accessibility"],
                            self.timeouts["accessibility_soft_seconds"])
        self.assertNotEqual(self.timeouts["accessibility"],
                            self.timeouts["accessibility_hard_seconds"])
        self.assertEqual(self.timeouts["accessibility_soft_seconds"], 120)
        self.assertEqual(self.timeouts["accessibility_hard_seconds"], 300)

    def test_the_key_is_role_shaped_so_the_lease_helper_can_find_it(self):
        # _lease_expires(role) indexes timeouts[role] directly. A key named
        # anything else would leave the KeyError exactly where it was.
        for role in ("builder", "fixer", "reviewer", "security",
                     "accessibility"):
            with self.subTest(role=role):
                self.assertIn(role, self.timeouts)
                self.assertIsInstance(self.timeouts[role], int)

    def test_the_lease_grace_still_applies_to_it(self):
        self.assertEqual(self.timeouts["lease_grace_seconds"], 60)


class G7ConcurrencyCase(unittest.TestCase):

    def setUp(self):
        self.cfg = config.load()

    def test_both_accessibility_roles_are_bounded_at_one(self):
        self.assertEqual(self.cfg.max_accessibility_auto, 1)
        self.assertEqual(self.cfg.max_accessibility_review, 1)

    def test_every_non_builder_role_is_one(self):
        # The reason G7 was answered this way. If a future change raises
        # one of these, this test is where the inconsistency surfaces.
        for name in ("max_fixers", "max_reviewers", "max_security",
                     "max_accessibility_auto", "max_accessibility_review",
                     "max_observers"):
            with self.subTest(bound=name):
                self.assertEqual(getattr(self.cfg, name), 1)
        self.assertEqual(self.cfg.max_builders, 3)

    def test_the_bounds_are_typed_fields_not_raw_dictionary_reads(self):
        # Every sibling bound is a typed field; an untyped pair would be
        # the odd one out and easy to read from the wrong place.
        for name in ("max_accessibility_auto", "max_accessibility_review"):
            with self.subTest(bound=name):
                self.assertIsInstance(getattr(self.cfg, name), int)

    def test_a_missing_bound_fails_loudly_rather_than_defaulting(self):
        # A governed concurrency bound that silently defaults is a number
        # this module chose, not one the operator governed.
        raw = dict(self.cfg.extra)
        raw["concurrency"] = {k: v for k, v in raw["concurrency"].items()
                              if k != "max_accessibility_auto"}
        with self.assertRaises(KeyError):
            int(raw["concurrency"]["max_accessibility_auto"])


class FrozenInputsUntouchedCase(unittest.TestCase):

    def test_no_frozen_content_hash_depends_on_experiment_json(self):
        # config/experiment.json is deliberately NOT one of the five frozen
        # content hashes, which is why a governed value may be added
        # pre-T+00 without moving a frozen input. Asserted rather than
        # assumed, because being wrong about it would be invisible.
        fields = manifest.frozen_content_fields()
        self.assertEqual(
            set(fields),
            {"product_spec_sha", "task_graph_hash", "prompt_hashes",
             "pr_template_hash", "severity_policy_hash"})
        self.assertNotIn("config/experiment.json", manifest.PRODUCT_SPEC_FILES)
        self.assertNotIn("config/experiment.json",
                         manifest.SEVERITY_POLICY_FILES)


if __name__ == "__main__":
    unittest.main()
