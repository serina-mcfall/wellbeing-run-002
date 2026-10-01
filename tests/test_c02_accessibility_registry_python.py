"""C-02's requirement registry, read from the Python control plane.

The registry landed as a JavaScript module (C-02) and C-05.3b's
accessibility adjudication runs in Python, so until a Python reader
existed every accessibility FAILURE rated INVALID no matter what it
cited - fail-closed, but a gate that could never open. That is
C05-3a-SESSION-HANDOVER.md section 36.5 decision D2.

These tests hold three properties:

1. The reader reports exactly what the generated artefact contains, and
   the artefact agrees with the frozen product specification. The
   JS-to-JSON link is pinned on the Node side
   (requirement-registry-json.test.js); the link checked HERE is
   JSON-to-frozen-source, re-derived independently of both, so the
   Python control plane does not take the artefact's word for its own
   provenance.
2. A malformed or missing artefact raises rather than yielding an empty
   registry, because an empty registry shuts the gate silently.
3. A real ACC-* citation now rates through the UNMODIFIED severity
   policy, and an invented one still fails closed.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from control import accessibility_registry, config, severity  # noqa: E402

SOURCE_DOC = config.REPO_ROOT / "product" / "ACCESSIBILITY.md"


def phrases_under(heading: str) -> list[str]:
    """Re-parse one "<Heading>: a, b, c." line of the frozen document.

    Deliberately independent of the registry and of the JS test's parser:
    a derivation nobody checks from the other side is not a derivation.
    """
    text = SOURCE_DOC.read_text(encoding="utf-8")
    line = next((l for l in text.split("\n") if l.startswith(heading + ":")), None)
    assert line is not None, f"product/ACCESSIBILITY.md has no '{heading}:' line"
    body = line[len(heading) + 1:].rstrip()
    if body.endswith("."):
        body = body[:-1]
    return [p.strip() for p in body.split(",") if p.strip()]


class ProvenanceCase(unittest.TestCase):
    """The identifiers Python reads trace back to the frozen document."""

    def test_every_entry_quotes_a_phrase_that_is_literally_in_the_source(self):
        text = SOURCE_DOC.read_text(encoding="utf-8")
        for req in accessibility_registry.REQUIREMENTS:
            with self.subTest(id=req["id"]):
                self.assertIn(req["source_phrase"], text)

    def test_the_registry_covers_both_source_lists_and_nothing_else(self):
        from_source = sorted(phrases_under("Definition of done")
                             + phrases_under("Cognitive accessibility"))
        from_registry = sorted(r["source_phrase"]
                               for r in accessibility_registry.REQUIREMENTS)
        self.assertEqual(from_registry, from_source)

    def test_the_two_groups_partition_the_registry(self):
        dod = {r["source_phrase"] for r in accessibility_registry.REQUIREMENTS
               if r["group"] == "definition-of-done"}
        cog = {r["source_phrase"] for r in accessibility_registry.REQUIREMENTS
               if r["group"] == "cognitive"}
        self.assertEqual(dod, set(phrases_under("Definition of done")))
        self.assertEqual(cog, set(phrases_under("Cognitive accessibility")))
        self.assertEqual(len(dod) + len(cog),
                         len(accessibility_registry.REQUIREMENTS))

    def test_identifiers_are_unique_and_shaped_as_the_canonical_registry_says(self):
        ids = [r["id"] for r in accessibility_registry.REQUIREMENTS]
        self.assertEqual(len(set(ids)), len(ids))
        for ident in ids:
            with self.subTest(id=ident):
                self.assertRegex(ident, r"^ACC-(DOD|COG)-[A-Z0-9_]+$")
                # Positional identifiers silently re-point if the source
                # list is reordered, which would make filed evidence cite
                # a different requirement than it did when written.
                self.assertNotRegex(ident, r"-\d+$")

    def test_requirement_ids_is_immutable_so_a_caller_cannot_widen_the_gate(self):
        self.assertIsInstance(accessibility_registry.REQUIREMENT_IDS, frozenset)
        self.assertFalse(hasattr(accessibility_registry.REQUIREMENT_IDS, "add"))
        self.assertEqual(accessibility_registry.requirement_ids(),
                         accessibility_registry.REQUIREMENT_IDS)


class MembershipCase(unittest.TestCase):

    def test_a_real_identifier_is_recognised(self):
        for ident in ("ACC-DOD-VISIBLE_FOCUS", "ACC-COG-PREDICTABLE_NAVIGATION"):
            with self.subTest(id=ident):
                self.assertTrue(
                    accessibility_registry.is_known_requirement(ident))

    def test_an_invented_or_malformed_identifier_is_not(self):
        for ident in ("ACC-DOD-ANYTHING_I_LIKE", "visible-focus-indicator",
                      "acc-dod-visible_focus", "", "ACC-DOD-", None, 7,
                      {}, [], object()):
            with self.subTest(id=repr(ident)):
                self.assertFalse(
                    accessibility_registry.is_known_requirement(ident))

    def test_the_pre_c02_kebab_case_assumption_is_not_silently_honoured(self):
        # Section 36.3: the design assumed kebab-case identifiers and the
        # registry landed SCREAMING_SNAKE. Anything still written against
        # the assumed form must fail closed rather than be quietly
        # normalised into a match.
        self.assertFalse(
            accessibility_registry.is_known_requirement("visible-focus"))
        self.assertTrue(
            accessibility_registry.is_known_requirement("ACC-DOD-VISIBLE_FOCUS"))


class FailClosedLoudlyCase(unittest.TestCase):
    """A broken artefact raises; it never degrades to an empty registry."""

    def _load(self, payload: str):
        path = Path(self.enterContext(__import__("tempfile").TemporaryDirectory()))
        artefact = path / "requirement-registry.json"
        artefact.write_text(payload, encoding="utf-8")
        return accessibility_registry._load(artefact)

    def test_a_missing_artefact_raises(self):
        missing = config.REPO_ROOT / "apparatus" / "accessibility" / "no-such.json"
        with self.assertRaises(accessibility_registry.RegistryUnavailable):
            accessibility_registry._load(missing)

    def test_unparseable_json_raises(self):
        with self.assertRaises(accessibility_registry.RegistryUnavailable):
            self._load("{not json")

    def test_an_empty_registry_raises_rather_than_shutting_the_gate_silently(self):
        with self.assertRaises(accessibility_registry.RegistryUnavailable):
            self._load(json.dumps({"requirements": []}))

    def test_a_missing_requirements_array_raises(self):
        for payload in ("{}", "[]", '{"requirements": {}}', '"text"', "null"):
            with self.subTest(payload=payload):
                with self.assertRaises(
                        accessibility_registry.RegistryUnavailable):
                    self._load(payload)

    def test_an_off_shape_identifier_raises(self):
        for ident in ("visible-focus", "", "DOD-VISIBLE_FOCUS", 7, None):
            with self.subTest(id=repr(ident)):
                with self.assertRaises(
                        accessibility_registry.RegistryUnavailable):
                    self._load(json.dumps({"requirements": [
                        {"id": ident, "group": "definition-of-done",
                         "source_phrase": "visible focus"}]}))

    def test_a_duplicate_identifier_raises(self):
        entry = {"id": "ACC-DOD-VISIBLE_FOCUS", "group": "definition-of-done",
                 "source_phrase": "visible focus"}
        with self.assertRaises(accessibility_registry.RegistryUnavailable):
            self._load(json.dumps({"requirements": [entry, dict(entry)]}))


class SeverityPolicyIntegrationCase(unittest.TestCase):
    """The registry is the argument severity.py already takes.

    control/severity.py is a frozen-hash input
    (manifest.SEVERITY_POLICY_FILES) and is NOT modified by this work.
    These tests prove the registry makes the existing policy usable
    without changing it.
    """

    def _finding(self, **over):
        finding = {
            "classification": "FAILURE",
            "jev_severity": "P3",
            "unmet_requirement": "ACC-DOD-VISIBLE_FOCUS",
        }
        finding.update(over)
        return finding

    def test_a_real_citation_is_recognised_and_the_p1_floor_applies(self):
        rated = severity.apply_severity_policy(
            self._finding(),
            known_requirement_ids=accessibility_registry.requirement_ids())
        self.assertTrue(rated["valid"])
        # SEVERITY-POLICY.md: accessibility failures are P1 unless more
        # severe. The floor is severity.py's to apply; this asserts the
        # registry did not interfere with it.
        self.assertEqual(rated["severity"], "P1")
        self.assertTrue(rated["floor_applied"])
        self.assertTrue(rated["merge_blocked"])

    def test_an_unknown_citation_still_fails_closed_as_invalid(self):
        rated = severity.apply_severity_policy(
            self._finding(unmet_requirement="ACC-DOD-ANYTHING_I_LIKE"),
            known_requirement_ids=accessibility_registry.requirement_ids())
        self.assertFalse(rated["valid"])
        self.assertIsNone(rated["severity"])
        self.assertTrue(rated["merge_blocked"])

    def test_the_assumed_kebab_case_citation_fails_closed(self):
        # Section 36.3's falsified assumption, at the gate rather than at
        # the registry: evidence written against the pre-C-02 identifier
        # form is INVALID, not quietly matched.
        rated = severity.apply_severity_policy(
            self._finding(unmet_requirement="visible-focus"),
            known_requirement_ids=accessibility_registry.requirement_ids())
        self.assertFalse(rated["valid"])
        self.assertTrue(rated["merge_blocked"])

    def test_without_a_registry_every_citation_still_fails_closed(self):
        # The pre-existing behaviour, unchanged: supplying no registry
        # cannot be a way to get a citation accepted.
        rated = severity.apply_severity_policy(self._finding(),
                                               known_requirement_ids=None)
        self.assertFalse(rated["valid"])
        self.assertTrue(rated["merge_blocked"])

    def test_a_more_severe_finding_is_not_lowered_to_the_floor(self):
        rated = severity.apply_severity_policy(
            self._finding(jev_severity="P0"),
            known_requirement_ids=accessibility_registry.requirement_ids())
        self.assertTrue(rated["valid"])
        self.assertEqual(rated["severity"], "P0")
        self.assertFalse(rated["floor_applied"])
        self.assertTrue(rated["merge_blocked"])

    def test_every_registry_identifier_is_accepted_as_a_citation(self):
        # Not one representative id: all seventeen. A registry the gate
        # only half-accepts would stall exactly the tasks citing the other
        # half, and nothing else would report why.
        for ident in sorted(accessibility_registry.REQUIREMENT_IDS):
            with self.subTest(id=ident):
                rated = severity.apply_severity_policy(
                    self._finding(unmet_requirement=ident),
                    known_requirement_ids=(
                        accessibility_registry.requirement_ids()))
                self.assertTrue(rated["valid"], ident)
                self.assertEqual(rated["severity"], "P1")


if __name__ == "__main__":
    unittest.main()
