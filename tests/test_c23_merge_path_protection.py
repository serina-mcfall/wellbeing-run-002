"""C-23a: the trusted-CI protected-path rule, WIRED INTO THE MERGE PATH.

The operator's approved rule, verbatim:

    "Ordinary product PRs cannot change trusted CI or its validation
     machinery; those changes require a separately reviewed apparatus
     amendment."

`tests/test_c23_ci_protected_paths.py` already proves what the rule's
vocabulary refuses and permits over a list of strings, and that the set is
DERIVED from what `.github/workflows/ci.yml` executes rather than chosen by
taste. It is not repeated here. This file proves the three things that were
missing once the rule was approved:

  * the changed-file list can be OBTAINED, and every way of failing to
    obtain it produces an incomplete answer rather than a short one;
  * the merge path REACHES the refusal, before the gate, through the real
    `supervisor.attempt_merge`;
  * the amendment escape hatch opens for a real authorisation and for
    nothing else.

WHAT THIS FILE CANNOT PROVE, SAID HERE RATHER THAN DISCOVERED LATER. No
real `gh` call is made anywhere in this repository, so the exact shape of a
GitHub compare response is UNVERIFIED. Every test below drives the parser
with a payload this file wrote. That is why the parser requires each field
to be the shape it uses and reports INCOMPLETE for anything else: the tests
can prove the parser's reaction to a shape, and only the parser's
fail-closed direction protects the gate from a shape nobody predicted.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from control import ci_protected_paths as protected  # noqa: E402
from control import ledger as ledger_mod  # noqa: E402
from control import routing  # noqa: E402
from control import supervisor as supervisor_mod  # noqa: E402

from test_merge_boundary import (  # noqa: E402
    BRANCH_FMT, REVIEWED_HEAD, MergeBoundaryCase)

HEAD = "a" * 40
OTHER_HEAD = "b" * 40
WORKFLOW = ".github/workflows/ci.yml"


def ok_result(payload):
    """A `gh.Result`-shaped success. Only `ok` and `json()` are read."""
    return SimpleNamespace(ok=True, json=lambda: payload, stdout="", stderr="")


def failed_result():
    return SimpleNamespace(ok=False, json=lambda: None, stdout="", stderr="")


def compare(*files):
    return {"files": list(files)}


def entry(filename, status="modified", previous=None):
    out = {"filename": filename, "status": status}
    if previous is not None:
        out["previous_filename"] = previous
    return out


def changed(paths, head=HEAD, complete=True, error=None):
    return routing.ChangedPaths(head, tuple(paths), complete, error)


def inspection(events=(), complete=True):
    return ledger_mod.Inspection(tuple(events), complete, 0, None)


# =====================================================================
# Obtaining the list. The runner is INJECTED in every test here, so no
# `gh` subprocess is ever started and no network is ever reached.
# =====================================================================


class TheFetchCase(unittest.TestCase):

    def test_the_request_names_the_head_sha_so_the_answer_is_about_it(self):
        """Head binding is structural, not a race.

        `gh pr diff` answers for whatever the head happens to be when the
        call lands. This URL names the commit, so a force-push between the
        observation and the call changes the ANSWER's subject rather than
        silently re-pointing it.
        """
        seen = []

        def runner(args):
            seen.append(args)
            return ok_result(compare(entry("src/app/page.tsx")))

        result = routing.changed_paths_for_head("o/r", "main", HEAD,
                                                runner=runner)
        self.assertTrue(result.complete, result.error)
        self.assertEqual(result.head_sha, HEAD)
        self.assertEqual(len(seen), 1)
        argv = seen[0]
        self.assertEqual(argv[0], "gh")
        self.assertIn(f"repos/o/r/compare/main...{HEAD}", argv)

    def test_an_unusable_head_asks_github_nothing_at_all(self):
        runner = mock.Mock()
        for bad in (None, "", "not-a-sha", 7, HEAD.upper(), "a" * 39):
            with self.subTest(head=bad):
                result = routing.changed_paths_for_head("o/r", "main", bad,
                                                        runner=runner)
                self.assertFalse(result.complete)
                self.assertEqual(result.error,
                                 routing.CHANGED_PATHS_HEAD_UNUSABLE)
        runner.assert_not_called()

    def test_an_unusable_base_ref_asks_github_nothing_at_all(self):
        """The base ref is interpolated into a URL path. A value carrying
        whitespace, `..` or a leading dash would make `{base}...{head}`
        parse as a comparison nobody asked for."""
        runner = mock.Mock()
        for bad in (None, "", "main branch", "a..b", "-main", 42,
                    "x" * 300):
            with self.subTest(base=bad):
                result = routing.changed_paths_for_head("o/r", bad, HEAD,
                                                        runner=runner)
                self.assertFalse(result.complete)
        runner.assert_not_called()

    def test_a_failed_call_is_incomplete_not_empty(self):
        result = routing.changed_paths_for_head(
            "o/r", "main", HEAD, runner=lambda args: failed_result())
        self.assertFalse(result.complete)
        self.assertEqual(result.paths, ())
        self.assertEqual(result.error, routing.CHANGED_PATHS_FETCH_FAILED)

    def test_the_head_it_was_asked_for_is_carried_even_on_failure(self):
        """A failed answer still has a subject. Without it the caller
        cannot tell "no list for this head" from "no list at all"."""
        result = routing.changed_paths_for_head(
            "o/r", "main", HEAD, runner=lambda args: failed_result())
        self.assertEqual(result.head_sha, HEAD)


class TheParseCase(unittest.TestCase):
    """Every refusal reachable without a subprocess."""

    def parse(self, payload):
        return routing._changed_paths_from_compare(HEAD, payload)

    def test_a_non_dict_payload_is_unparseable(self):
        for payload in (None, [], "", 7, True):
            with self.subTest(payload=payload):
                self.assertEqual(self.parse(payload).error,
                                 routing.CHANGED_PATHS_UNPARSEABLE)

    def test_an_absent_files_array_is_unparseable_not_an_unchanged_tree(self):
        """GitHub omits `files` when the comparison is too large to
        enumerate - precisely the case where something could be hiding in
        it. Reading the omission as "nothing changed" would clear the
        largest pull requests automatically."""
        for payload in ({}, {"files": None}, {"files": "src/x"},
                        {"files": {}}):
            with self.subTest(payload=payload):
                self.assertEqual(self.parse(payload).error,
                                 routing.CHANGED_PATHS_UNPARSEABLE)

    def test_an_empty_files_array_is_unknown_not_clean(self):
        self.assertEqual(self.parse(compare()).error,
                         routing.CHANGED_PATHS_EMPTY)
        self.assertFalse(self.parse(compare()).complete)

    def test_a_list_at_the_cap_is_treated_as_truncated(self):
        """There is no truncation flag to read, so the cap is the signal.
        A pull request large enough to reach it needs a human, because a
        list that may be hiding a workflow edit proves nothing about one."""
        files = [entry(f"src/f{i}.ts") for i in range(routing.CHANGED_PATHS_CAP)]
        result = self.parse(compare(*files))
        self.assertFalse(result.complete)
        self.assertEqual(result.error, routing.CHANGED_PATHS_TRUNCATED)

    def test_one_below_the_cap_is_still_complete(self):
        """The boundary, from the other side - or the test above would
        pass for a parser that refused every list."""
        files = [entry(f"src/f{i}.ts")
                 for i in range(routing.CHANGED_PATHS_CAP - 1)]
        self.assertTrue(self.parse(compare(*files)).complete)

    def test_an_unreadable_entry_makes_the_WHOLE_list_incomplete(self):
        """Not "skip the bad one". A gate that drops the entries it could
        not parse clears a pull request on the strength of the files that
        happened to be well-formed."""
        for bad in (None, "src/x.ts", {}, {"filename": None},
                    {"filename": ""}, {"filename": "   "}, {"filename": 7}):
            with self.subTest(bad=bad):
                result = self.parse(compare(entry("src/ok.ts"), bad))
                self.assertFalse(result.complete)
                self.assertEqual(result.error,
                                 routing.CHANGED_PATHS_UNPARSEABLE)
                self.assertEqual(result.paths, ())


class AdditionsDeletionsAndRenamesCase(unittest.TestCase):
    """(c) A rename out of a protected path is a DELETION from it; a rename
    into one is an ADDITION to it. Both sides are judged."""

    def parse(self, payload):
        return routing._changed_paths_from_compare(HEAD, payload)

    def test_an_addition_to_a_protected_path_is_caught(self):
        result = self.parse(compare(entry(WORKFLOW, "added")))
        self.assertTrue(result.complete)
        self.assertEqual(
            routing.ci_paths_clear_for_merge(result, HEAD)[1],
            routing.MERGE_CI_PATHS_PROTECTED)

    def test_a_deletion_from_a_protected_path_is_caught(self):
        result = self.parse(compare(entry("control/routing.py", "removed")))
        self.assertTrue(result.complete)
        self.assertEqual(
            routing.ci_paths_clear_for_merge(result, HEAD)[1],
            routing.MERGE_CI_PATHS_PROTECTED)

    def test_a_rename_OUT_of_a_protected_path_is_caught(self):
        """THE ONE A ONE-PATH-PER-FILE LISTING CANNOT BE TRUSTED WITH.

        A `--name-only` listing gives one path per changed file, and for a
        rename that path is the destination - so this change could report
        only `src/moved.py`, leaving the deletion from `control/`
        invisible. Whether `gh` really behaves that way is NOT verified
        here and is not relied on; the compare response carries
        `previous_filename`, the parser reads it, and both sides reach the
        rule regardless.
        """
        payload = compare(entry("src/moved.py", "renamed",
                                previous="control/routing.py"))
        result = self.parse(payload)
        self.assertIn("control/routing.py", result.paths)
        self.assertIn("src/moved.py", result.paths)
        clear, condition = routing.ci_paths_clear_for_merge(result, HEAD)
        self.assertFalse(clear)
        self.assertEqual(condition, routing.MERGE_CI_PATHS_PROTECTED)

    def test_the_destination_alone_would_have_cleared_it(self):
        """The counter-proof for the test above. Judging only `filename`
        really does clear a file moved out of the protected tree, so the
        previous-filename read is load-bearing and not decoration."""
        self.assertFalse(protected.touches_protected(["src/moved.py"])[0])

    def test_a_rename_INTO_a_protected_path_is_caught(self):
        payload = compare(entry("control/evil.py", "renamed",
                                previous="src/evil.py"))
        result = self.parse(payload)
        self.assertFalse(routing.ci_paths_clear_for_merge(result, HEAD)[0])

    def test_a_rename_that_will_not_say_where_from_is_incomplete(self):
        """It says it moved and withholds the origin. A path we know exists
        and cannot read is the definition of an incomplete inspection, so
        it refuses rather than judging the half it can see."""
        for status in ("renamed", "copied", "RENAMED"):
            with self.subTest(status=status):
                result = self.parse(compare(entry("src/x.ts", status)))
                self.assertFalse(result.complete)
                self.assertEqual(result.error,
                                 routing.CHANGED_PATHS_RENAME_UNRESOLVED)

    def test_a_rename_between_two_ordinary_paths_still_merges(self):
        """Or the rule would refuse every refactor that moves a component."""
        payload = compare(entry("src/b.tsx", "renamed", previous="src/a.tsx"))
        result = self.parse(payload)
        self.assertTrue(result.complete)
        self.assertTrue(routing.ci_paths_clear_for_merge(result, HEAD)[0])

    def test_an_ordinary_modification_needs_no_previous_filename(self):
        """A `modified` entry carries no `previous_filename`, and demanding
        one would refuse every ordinary pull request."""
        result = self.parse(compare(entry("src/app/page.tsx", "modified")))
        self.assertTrue(result.complete)
        self.assertEqual(result.paths, ("src/app/page.tsx",))


# =====================================================================
# The pure predicate.
# =====================================================================


class WhatThePredicatePermitsCase(unittest.TestCase):
    """(a) A rule that refuses everything stops the factory instead of
    protecting it. `test_c23_ci_protected_paths.WhatItPermitsCase` proves
    the vocabulary permits ordinary work; this proves the WIRED predicate
    does, which is a different claim."""

    def test_ordinary_product_work_merges(self):
        clear, condition = routing.ci_paths_clear_for_merge(
            changed(["src/app/page.tsx", "src/components/Timer.tsx",
                     "package.json", "public/icon.png", "README.md"]), HEAD)
        self.assertTrue(clear, condition)
        self.assertEqual(condition, "")

    def test_an_evidence_package_merges(self):
        """`evidence/` holds SUBMITTED packages - the thing CI judges, not
        part of what does the judging. A product pull request adding one is
        the normal case and must not need an amendment."""
        clear, _ = routing.ci_paths_clear_for_merge(
            changed(["evidence/TASK-001-R1.json", "src/app/page.tsx"]), HEAD)
        self.assertTrue(clear)


class WhatThePredicateRefusesCase(unittest.TestCase):
    """(b) with a finite, distinct condition."""

    def test_an_apparatus_change_is_refused(self):
        for path in (WORKFLOW, "apparatus/pr-evidence/validate.js",
                     "control/routing.py", "tests/test_anything.py",
                     "scripts/check_no_secrets.py",
                     "protocol/PR-EVIDENCE-V2.schema.json"):
            with self.subTest(path=path):
                clear, condition = routing.ci_paths_clear_for_merge(
                    changed([path, "src/app/page.tsx"]), HEAD)
                self.assertFalse(clear)
                self.assertEqual(condition,
                                 routing.MERGE_CI_PATHS_PROTECTED)

    def test_the_two_conditions_are_distinct_and_finite(self):
        self.assertNotEqual(routing.MERGE_CI_PATHS_PROTECTED,
                            routing.MERGE_CI_PATHS_UNVERIFIABLE)
        codes = [value for name, value in vars(routing).items()
                 if name.startswith("MERGE_") and isinstance(value, str)]
        for code in (routing.MERGE_CI_PATHS_PROTECTED,
                     routing.MERGE_CI_PATHS_UNVERIFIABLE):
            self.assertEqual(codes.count(code), 1,
                             f"{code} is not a distinct condition")

    def test_one_protected_file_among_many_product_ones_still_refuses(self):
        paths = [f"src/f{i}.ts" for i in range(40)] + [WORKFLOW]
        self.assertFalse(routing.ci_paths_clear_for_merge(
            changed(paths), HEAD)[0])


class AnIncompleteInspectionRefusesCase(unittest.TestCase):
    """(d) THE IMPORTANT ONE.

    Modelled on `ledger_attests_merge`'s treatment of an incomplete
    inspection: "we could not read all of it" must never become "it is not
    there". Every way of not knowing what changed lands on
    CI_PATHS_UNVERIFIABLE, and none of them reaches the clear branch.
    """

    def assert_unverifiable(self, value, head=HEAD):
        clear, condition = routing.ci_paths_clear_for_merge(value, head)
        self.assertFalse(clear)
        self.assertEqual(condition, routing.MERGE_CI_PATHS_UNVERIFIABLE)

    def test_no_list_at_all_refuses(self):
        self.assert_unverifiable(None)

    def test_an_incomplete_list_refuses_even_when_what_it_HAS_is_clean(self):
        """The whole trap. The visible half is ordinary product work; the
        invisible half is unknown. Judging the visible half is how a
        workflow edit merges."""
        self.assert_unverifiable(
            changed(["src/app/page.tsx"], complete=False,
                    error=routing.CHANGED_PATHS_TRUNCATED))

    def test_every_fetch_failure_mode_refuses(self):
        for error in (routing.CHANGED_PATHS_FETCH_FAILED,
                      routing.CHANGED_PATHS_UNPARSEABLE,
                      routing.CHANGED_PATHS_TRUNCATED,
                      routing.CHANGED_PATHS_EMPTY,
                      routing.CHANGED_PATHS_RENAME_UNRESOLVED,
                      routing.CHANGED_PATHS_HEAD_UNUSABLE):
            with self.subTest(error=error):
                self.assert_unverifiable(changed([], complete=False,
                                                 error=error))

    def test_a_complete_but_EMPTY_list_refuses(self):
        """`_changed_paths_from_compare` cannot produce this, but a
        hand-built or future value could, and "complete and empty" must not
        become the one way to clear this gate without naming a file."""
        self.assert_unverifiable(changed([]))

    def test_a_list_whose_paths_are_not_a_sequence_refuses(self):
        for paths in (None, "src/app/page.tsx", 7, {"a": 1}):
            with self.subTest(paths=paths):
                self.assert_unverifiable(
                    routing.ChangedPaths(HEAD, paths, True, None))

    def test_an_object_that_is_not_a_changed_path_record_refuses(self):
        """A stub, a Mock or a corrupted reader can hand this anything, and
        a predicate whose contract is to return a finite reason must never
        raise one instead."""
        for value in (object(), SimpleNamespace(), mock.Mock(spec=[]),
                      "complete", 0, []):
            with self.subTest(value=value):
                clear, condition = routing.ci_paths_clear_for_merge(value, HEAD)
                self.assertFalse(clear)
                self.assertEqual(condition,
                                 routing.MERGE_CI_PATHS_UNVERIFIABLE)

    def test_an_unusable_head_refuses_before_anything_is_read(self):
        clean = changed(["src/app/page.tsx"])
        for bad in (None, "", "not-a-sha", 7, HEAD.upper()):
            with self.subTest(head=bad):
                self.assert_unverifiable(clean, head=bad)

    def test_an_unreadable_path_inside_a_complete_list_still_refuses(self):
        """`touches_protected` treats a path it cannot read as offending.
        The wired predicate must inherit that, not route around it."""
        clear, condition = routing.ci_paths_clear_for_merge(
            routing.ChangedPaths(HEAD, ("src/ok.ts", None), True, None), HEAD)
        self.assertFalse(clear)
        self.assertEqual(condition, routing.MERGE_CI_PATHS_PROTECTED)


class TheListIsBoundToOneHeadCase(unittest.TestCase):
    """(e) A list is evidence about one commit and about no other."""

    def test_a_list_for_an_older_head_does_not_satisfy_a_new_one(self):
        stale = changed(["src/app/page.tsx"], head=OTHER_HEAD)
        clear, condition = routing.ci_paths_clear_for_merge(stale, HEAD)
        self.assertFalse(clear)
        self.assertEqual(condition, routing.MERGE_CI_PATHS_UNVERIFIABLE)

    def test_the_same_list_at_its_own_head_does_satisfy_it(self):
        """The other side of the boundary, or the test above would pass for
        a predicate that refused every list."""
        self.assertTrue(routing.ci_paths_clear_for_merge(
            changed(["src/app/page.tsx"], head=OTHER_HEAD), OTHER_HEAD)[0])

    def test_a_list_carrying_no_head_does_not_satisfy_any_head(self):
        self.assertFalse(routing.ci_paths_clear_for_merge(
            routing.ChangedPaths(None, ("src/app/page.tsx",), True, None),
            HEAD)[0])


# =====================================================================
# (f) The amendment escape hatch.
# =====================================================================


def amendment_record(code, *, iid="INT-0123456789abcdef", task_id="TASK-001",
                     status="RESOLVED", resolution="NO_ACTION",
                     resolved_by="operator", type_=None):
    return {
        "id": iid,
        "type": (routing.CI_AMENDMENT_INTERVENTION_TYPE
                 if type_ is None else type_),
        "scope": "task",
        "task_id": task_id,
        "condition_code": code,
        # `intervention.request` reads this key unguardedly while scanning
        # for a dedup match, so a record without it is not a record any
        # real run produces. Spelled exactly as `_dedup_key` builds it.
        "dedup_key": f"task:{task_id}:{code}",
        "status": status,
        "resolution": resolution,
        "resolved_by": resolved_by,
        "resolved_at": "2026-10-02T10:00:00+13:00",
    }


def amendment_event(code, *, iid="INT-0123456789abcdef", task_id="TASK-001",
                    human=True, resolution="NO_ACTION", type_=None,
                    event_type=None):
    return {
        "event_type": (routing.CI_AMENDMENT_RESOLVED_EVENT
                       if event_type is None else event_type),
        "task_id": task_id,
        "human_intervention": human,
        "outcome": resolution,
        "metadata_redacted": {
            "intervention_id": iid,
            "intervention_type": (routing.CI_AMENDMENT_INTERVENTION_TYPE
                                  if type_ is None else type_),
            "condition_code": code,
            "resolution": resolution,
            "resolved_by": "operator",
        },
    }


class TheAmendmentCodeCase(unittest.TestCase):

    def test_it_is_a_legal_intervention_condition_code(self):
        """Because the whole declaration rides in a field
        `intervention.py` already validates - no new field is introduced."""
        from control import intervention
        code = routing.ci_amendment_condition_code(HEAD, [WORKFLOW])
        self.assertRegex(code, intervention.CONDITION_CODE_RE)

    def test_it_binds_the_head_so_an_authorisation_expires_on_a_push(self):
        self.assertNotEqual(
            routing.ci_amendment_condition_code(HEAD, [WORKFLOW]),
            routing.ci_amendment_condition_code(OTHER_HEAD, [WORKFLOW]))

    def test_it_binds_the_paths_so_an_authorisation_cannot_widen(self):
        self.assertNotEqual(
            routing.ci_amendment_condition_code(HEAD, [WORKFLOW]),
            routing.ci_amendment_condition_code(
                HEAD, [WORKFLOW, "control/routing.py"]))

    def test_the_order_github_lists_files_in_cannot_change_it(self):
        a = routing.ci_amendment_condition_code(
            HEAD, [WORKFLOW, "control/routing.py"])
        b = routing.ci_amendment_condition_code(
            HEAD, ["control/routing.py", WORKFLOW, "control/routing.py"])
        self.assertEqual(a, b)


class TheAmendmentOpensTheGateCase(unittest.TestCase):

    def setUp(self):
        self.code = routing.ci_amendment_condition_code(HEAD, [WORKFLOW])
        self.changed = changed([WORKFLOW, "src/app/page.tsx"])

    def authorised(self, records, events, complete=True, task_id="TASK-001"):
        return routing.authorised_ci_amendments(
            {r["id"]: r for r in records}, inspection(events, complete),
            task_id)

    def test_a_valid_amendment_merges_the_protected_change(self):
        codes = self.authorised([amendment_record(self.code)],
                                [amendment_event(self.code)])
        self.assertEqual(codes, frozenset({self.code}))
        clear, condition = routing.ci_paths_clear_for_merge(
            self.changed, HEAD, codes)
        self.assertTrue(clear, condition)

    def test_no_amendment_at_all_does_not(self):
        self.assertFalse(routing.ci_paths_clear_for_merge(
            self.changed, HEAD, frozenset())[0])

    def test_the_state_record_ALONE_does_not_authorise(self):
        """THE FORGERY THIS RAISES THE COST OF. `.runtime/state.json` is
        writable by every worker - they share this UID - so a record with
        no durable event behind it is exactly what a worker produces by
        writing one file."""
        self.assertEqual(
            self.authorised([amendment_record(self.code)], []), frozenset())

    def test_the_LEDGER_EVENT_alone_does_not_authorise(self):
        """The mirror image: an appended event with no resolved record."""
        self.assertEqual(
            self.authorised([], [amendment_event(self.code)]), frozenset())

    def test_the_two_sources_must_agree_about_WHICH_intervention(self):
        self.assertEqual(
            self.authorised([amendment_record(self.code, iid="INT-aaa")],
                            [amendment_event(self.code, iid="INT-bbb")]),
            frozenset())

    def test_the_mapping_key_must_agree_with_the_record_it_holds(self):
        """Otherwise the pair matched against the ledger proves nothing
        about the record that was actually read."""
        record = amendment_record(self.code)
        codes = routing.authorised_ci_amendments(
            {"INT-somethingelse": record},
            inspection([amendment_event(self.code)]), "TASK-001")
        self.assertEqual(codes, frozenset())

    def test_an_amendment_for_another_HEAD_does_not_travel(self):
        stale = routing.ci_amendment_condition_code(OTHER_HEAD, [WORKFLOW])
        codes = self.authorised([amendment_record(stale)],
                                [amendment_event(stale)])
        self.assertEqual(codes, frozenset({stale}))
        self.assertFalse(routing.ci_paths_clear_for_merge(
            self.changed, HEAD, codes)[0],
            "an authorisation for a different head cleared this one")

    def test_an_amendment_for_FEWER_paths_does_not_cover_more(self):
        """Authorising the workflow does not authorise `control/` smuggled
        into the same pull request."""
        narrow = routing.ci_amendment_condition_code(HEAD, [WORKFLOW])
        codes = self.authorised([amendment_record(narrow)],
                                [amendment_event(narrow)])
        wider = changed([WORKFLOW, "control/routing.py"])
        self.assertFalse(routing.ci_paths_clear_for_merge(
            wider, HEAD, codes)[0])

    def test_an_amendment_for_another_TASK_does_not_travel(self):
        self.assertEqual(
            self.authorised(
                [amendment_record(self.code, task_id="TASK-999")],
                [amendment_event(self.code, task_id="TASK-999")],
                task_id="TASK-001"),
            frozenset())

    def test_an_UNRESOLVED_intervention_authorises_nothing(self):
        """Requesting an amendment is not being granted one."""
        for status in ("OPEN", "ACKNOWLEDGED", "", None, "resolved"):
            with self.subTest(status=status):
                self.assertEqual(
                    self.authorised(
                        [amendment_record(self.code, status=status)],
                        [amendment_event(self.code)]),
                    frozenset())

    def test_an_amendment_nobody_signed_authorises_nothing(self):
        for by in (None, "", "   ", 7):
            with self.subTest(by=by):
                self.assertEqual(
                    self.authorised(
                        [amendment_record(self.code, resolved_by=by)],
                        [amendment_event(self.code)]),
                    frozenset())

    def test_another_intervention_TYPE_authorises_nothing(self):
        """A resolved `builder_attempts_exhausted` is a human decision
        about something else entirely."""
        self.assertEqual(
            self.authorised(
                [amendment_record(self.code, type_="HUMAN_VERIFICATION")],
                [amendment_event(self.code, type_="HUMAN_VERIFICATION")]),
            frozenset())

    def test_another_resolution_outcome_authorises_nothing(self):
        for outcome in ("RETRY", "FAIL", "CLEAR_GUARDRAIL", None):
            with self.subTest(outcome=outcome):
                self.assertEqual(
                    self.authorised(
                        [amendment_record(self.code, resolution=outcome)],
                        [amendment_event(self.code, resolution=outcome)]),
                    frozenset())

    def test_an_event_that_does_not_claim_a_human_authorises_nothing(self):
        """`is not True`, not falsiness: a hand-written `"true"` must not
        satisfy a flag that is supposed to mean a human was present."""
        for human in (False, None, "true", 1, 0):
            with self.subTest(human=human):
                self.assertEqual(
                    self.authorised([amendment_record(self.code)],
                                    [amendment_event(self.code, human=human)]),
                    frozenset())

    def test_another_event_TYPE_authorises_nothing(self):
        self.assertEqual(
            self.authorised(
                [amendment_record(self.code)],
                [amendment_event(self.code,
                                 event_type="HUMAN_INTERVENTION_REQUESTED")]),
            frozenset())

    def test_an_UNREADABLE_LEDGER_authorises_nothing(self):
        """Same rule as `ledger_attests_merge`: a truncated ledger could be
        hiding the line that contradicts this one."""
        self.assertEqual(
            self.authorised([amendment_record(self.code)],
                            [amendment_event(self.code)], complete=False),
            frozenset())

    def test_garbage_in_either_source_authorises_nothing_and_never_raises(self):
        bad_inspections = (None, SimpleNamespace(),
                           SimpleNamespace(complete=True, events="nope"),
                           inspection(["not-a-dict", 7, None]))
        for insp in bad_inspections:
            with self.subTest(inspection=insp):
                self.assertEqual(
                    routing.authorised_ci_amendments(
                        {"INT-x": amendment_record(self.code, iid="INT-x")},
                        insp, "TASK-001"),
                    frozenset())
        for interventions in (None, [], "x", {"INT-x": "not-a-dict"},
                              {"INT-x": None}):
            with self.subTest(interventions=interventions):
                self.assertEqual(
                    routing.authorised_ci_amendments(
                        interventions, inspection([amendment_event(self.code)]),
                        "TASK-001"),
                    frozenset())
        for task_id in (None, "", 7):
            with self.subTest(task_id=task_id):
                self.assertEqual(
                    routing.authorised_ci_amendments(
                        {}, inspection(), task_id), frozenset())

    def test_an_unreadable_amendment_container_refuses_rather_than_raising(self):
        for container in (None, "a-string", 7, object()):
            with self.subTest(container=container):
                clear, condition = routing.ci_paths_clear_for_merge(
                    self.changed, HEAD, container)
                self.assertFalse(clear)
                self.assertEqual(condition,
                                 routing.MERGE_CI_PATHS_PROTECTED)

    def test_an_amendment_does_not_rescue_an_UNREADABLE_LIST(self):
        """The escape hatch is from the protected-path rule, not from the
        requirement to know what changed. An amendment covering the paths
        we could not see would be an authorisation for nothing."""
        codes = self.authorised([amendment_record(self.code)],
                                [amendment_event(self.code)])
        clear, condition = routing.ci_paths_clear_for_merge(
            changed([WORKFLOW], complete=False,
                    error=routing.CHANGED_PATHS_TRUNCATED), HEAD, codes)
        self.assertFalse(clear)
        self.assertEqual(condition, routing.MERGE_CI_PATHS_UNVERIFIABLE)


class TheLimitIsRecordedCase(unittest.TestCase):
    """The honest statement of what the escape hatch does NOT achieve.

    Both durable sources are written by the UID that runs the workers and
    neither is hash-chained, so a worker that writes the RESOLVED record
    AND appends the matching ledger event authorises itself. That is C-22,
    C-22 is open, and a docstring claiming otherwise would be the most
    dangerous sentence in this change. These assert the admission is in the
    code where the next reader will meet it - not only in a report.
    """

    def test_the_self_assertion_limit_is_stated_at_the_function(self):
        doc = routing.authorised_ci_amendments.__doc__ or ""
        self.assertIn("C-22", doc)
        self.assertIn("same UID", doc)
        self.assertIn("does not prevent", doc)

    def test_the_two_source_requirement_is_stated_as_cost_not_prevention(self):
        doc = routing.authorised_ci_amendments.__doc__ or ""
        self.assertIn("NOT NON-SELF-ASSERTING", doc.upper())


# =====================================================================
# Reachability: the REAL supervisor merge path.
# =====================================================================


class ThroughTheRealSupervisorCase(MergeBoundaryCase):
    """A predicate nothing calls is documentation.

    These drive the real `execute_merges` over a real `Store` and a real
    `Ledger`, with `routing.changed_paths_for_head` - and only that -
    stubbed, exactly as every other outbound GitHub edge in this harness
    already is.
    """

    PROTECTED_CHANGE = routing.ChangedPaths(
        REVIEWED_HEAD, (WORKFLOW, "src/app/page.tsx"), True, None)
    UNREADABLE_CHANGE = routing.ChangedPaths(
        REVIEWED_HEAD, (), False, routing.CHANGED_PATHS_TRUNCATED)

    def blocked(self):
        return [fields for name, fields in self.events if name == "MERGE_BLOCKED"]

    def test_the_seeded_product_change_still_merges(self):
        """Or nothing below means anything."""
        self.seed()
        self.run_tick()
        self.assertEqual(self.merged_numbers(), [100])

    def test_an_apparatus_change_does_not_merge(self):
        self.seed()
        self.run_tick(changed_paths=self.PROTECTED_CHANGE)
        self.assertEqual(self.merged_numbers(), [],
                         "a pull request editing .github/workflows/ci.yml "
                         "reached gh.merge")

    def test_the_refusal_names_its_condition_and_the_offending_path(self):
        self.seed()
        self.run_tick(changed_paths=self.PROTECTED_CHANGE)
        blocked = self.blocked()
        self.assertEqual(len(blocked), 1)
        meta = blocked[0]["metadata_redacted"]
        self.assertEqual(meta["condition"], routing.MERGE_CI_PATHS_PROTECTED)
        self.assertEqual(meta["offending_paths"], [WORKFLOW])
        self.assertEqual(meta["head"], REVIEWED_HEAD)

    def test_the_refusal_happens_BEFORE_the_merge_gate_is_consulted(self):
        """Ordering, behaviourally. A gate that never saw the file list
        must not be the thing that decided this pull request."""
        self.seed()
        self.run_tick(changed_paths=self.PROTECTED_CHANGE)
        self.evaluate.assert_not_called()

    def test_an_unreadable_change_list_does_not_merge(self):
        self.seed()
        self.run_tick(changed_paths=self.UNREADABLE_CHANGE)
        self.assertEqual(self.merged_numbers(), [])
        meta = self.blocked()[0]["metadata_redacted"]
        self.assertEqual(meta["condition"],
                         routing.MERGE_CI_PATHS_UNVERIFIABLE)
        self.assertEqual(meta["detail"], routing.CHANGED_PATHS_TRUNCATED)
        self.assertEqual(meta["offending_paths"], [])

    def test_a_list_computed_for_a_STALE_head_does_not_merge(self):
        """(e) through the real path. The pull request is ordinary product
        work - but the list describes a different commit."""
        self.seed()
        self.run_tick(changed_paths=routing.ChangedPaths(
            OTHER_HEAD, ("src/app/page.tsx",), True, None))
        self.assertEqual(self.merged_numbers(), [])
        self.assertEqual(self.blocked()[0]["metadata_redacted"]["condition"],
                         routing.MERGE_CI_PATHS_UNVERIFIABLE)

    def test_the_refusal_does_not_invalidate_the_approval(self):
        """Which files a pull request touches says nothing about whether
        its review was sound. Burning a review cycle would punish the task
        for a governance condition a human has to clear either way."""
        self.seed()
        self.run_tick(changed_paths=self.PROTECTED_CHANGE)
        record = self.record()
        self.assertTrue(record["approval_current"])
        self.assertEqual(record["review_verdict"], routing.REVIEW_PASS)
        self.assertEqual(self.task_state(), "REVIEW")

    def test_the_refusal_raises_the_intervention_the_operator_must_resolve(self):
        """The escape hatch has to be REACHABLE. Nothing else in this
        process mints an intervention record, so without this the operator
        has nothing to resolve and the rule is a permanent ban."""
        self.seed()
        self.run_tick(changed_paths=self.PROTECTED_CHANGE)
        interventions = self.durable().get("interventions") or {}
        self.assertEqual(len(interventions), 1)
        record = next(iter(interventions.values()))
        self.assertEqual(record["type"],
                         routing.CI_AMENDMENT_INTERVENTION_TYPE)
        self.assertEqual(record["status"], "OPEN")
        self.assertEqual(
            record["condition_code"],
            routing.ci_amendment_condition_code(REVIEWED_HEAD, [WORKFLOW]))

    def test_an_unreadable_list_raises_NO_intervention(self):
        """A transient fetch failure is not a governance decision, and
        asking a human to authorise an amendment for files nobody can name
        would teach them to rubber-stamp it."""
        self.seed()
        self.run_tick(changed_paths=self.UNREADABLE_CHANGE)
        self.assertEqual(self.durable().get("interventions") or {}, {})

    # ------------------------------------------------- the escape hatch

    def authorise(self, head=REVIEWED_HEAD, paths=(WORKFLOW,),
                  task_id="TASK-001", iid="INT-00112233445566aa"):
        """Both durable halves of a real amendment, written the way the
        operator's `ctl human-resolve` writes them."""
        code = routing.ci_amendment_condition_code(head, list(paths))
        doc = self.store.read()
        doc.setdefault("interventions", {})[iid] = amendment_record(
            code, iid=iid, task_id=task_id)
        self.store._write(doc)
        self.sup.ledger.append(
            routing.CI_AMENDMENT_RESOLVED_EVENT,
            task_id=task_id, outcome=routing.CI_AMENDMENT_RESOLUTION,
            activity_class="ESCALATION", human_intervention=True,
            metadata_redacted={
                "intervention_id": iid,
                "intervention_type": routing.CI_AMENDMENT_INTERVENTION_TYPE,
                "condition_code": code,
                "resolution": routing.CI_AMENDMENT_RESOLUTION,
                "resolved_by": "operator",
            })
        return code

    def test_a_protected_change_WITH_a_valid_amendment_merges(self):
        self.seed()
        self.authorise()
        self.run_tick(changed_paths=self.PROTECTED_CHANGE)
        self.assertEqual(self.merged_numbers(), [100],
                         f"a validly amended apparatus change was refused: "
                         f"{self.blocked()}")

    def test_an_amendment_for_the_WRONG_HEAD_does_not_merge(self):
        self.seed()
        self.authorise(head=OTHER_HEAD)
        self.run_tick(changed_paths=self.PROTECTED_CHANGE)
        self.assertEqual(self.merged_numbers(), [])

    def test_an_amendment_for_the_WRONG_PATHS_does_not_merge(self):
        self.seed()
        self.authorise(paths=("control/routing.py",))
        self.run_tick(changed_paths=self.PROTECTED_CHANGE)
        self.assertEqual(self.merged_numbers(), [])

    def test_a_FORGED_amendment_written_only_into_state_does_not_merge(self):
        """The forgery a worker reaches by writing `state.json` and nothing
        else - the same shape C-04c reproduced for the evidence legs."""
        self.seed()
        code = routing.ci_amendment_condition_code(REVIEWED_HEAD, [WORKFLOW])
        doc = self.store.read()
        doc["interventions"] = {
            "INT-forged0000000": amendment_record(code,
                                                  iid="INT-forged0000000")}
        self.store._write(doc)
        self.run_tick(changed_paths=self.PROTECTED_CHANGE)
        self.assertEqual(self.merged_numbers(), [],
                         "a state-only amendment merged an apparatus change")

    def test_an_amendment_resolved_for_ANOTHER_TASK_does_not_merge(self):
        self.seed()
        self.authorise(task_id="TASK-999")
        self.run_tick(changed_paths=self.PROTECTED_CHANGE)
        self.assertEqual(self.merged_numbers(), [])


class TheFetchIsNeverReachedFromTestsCase(unittest.TestCase):
    """A standing guard. `changed_paths_for_head` shells out to `gh`, and a
    harness that forgets to stub it would make the unit suite talk to
    GitHub. Every merge harness stubs it today; this records WHY, and
    `routing.changed_paths_for_head` is the one name to grep for if that
    ever stops being true."""

    def test_the_fetch_is_the_only_network_edge_this_change_adds(self):
        source = (Path(__file__).resolve().parents[1]
                  / "control" / "routing.py").read_text(encoding="utf-8")
        start = source.index("def changed_paths_for_head(")
        end = source.index("def _changed_paths_from_compare(")
        body = source[start:end]
        self.assertIn("runner if runner is not None else gh.run", body,
                      "the runner is no longer injectable, so a test can no "
                      "longer drive this without a subprocess")
        # The parser half touches no I/O at all, which is what lets every
        # refusal above be reached without one. Prose is stripped first, or
        # this asserts something about the comments rather than the code.
        parser = source[end:source.index("CI_AMENDMENT_INTERVENTION_TYPE =")]
        code_only = "\n".join(
            line for line in parser.splitlines()
            if line.strip() and not line.strip().startswith(("#", '"""', "*")))
        for forbidden in ("gh.", "runner", "run("):
            self.assertNotIn(forbidden, code_only,
                             f"the parser reaches {forbidden}")


if __name__ == "__main__":
    unittest.main()
