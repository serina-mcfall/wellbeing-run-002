"""G2 — claim-owned product-server ports. Approved 2026-10-01.

The automated accessibility half runs a product server. C-09 knew two
owners of a port — a committed worker record, and a job file written
before spawn — and the scan's server is neither, so without this a Builder
could be handed a port a scan is still bound to, and reconciliation would
report that scan's own listener as a foreign orphan.

The operator's conditions, each with its own case below:

  * durable, and exclusive across builders AND accessibility processes;
  * recognised by reconciliation as OWNED, not as an orphan;
  * on completion, failure, cancellation or lease expiry: stop the owned
    process and CONFIRM the listener is gone before releasing the port;
  * **lease expiry alone must not make a still-listening port reusable**;
  * finite diagnostics preserved.

The sharpest of those is the fourth, and it is the one a plausible
implementation gets wrong: it is tempting to treat an expired lease as
"the claim is over, reclaim the port". The claim's port is held until
`port_released` is True, and only an observed-gone listener sets it.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from control import reconcile, routing, workers  # noqa: E402

HEAD = "a" * 40
PORT = 3200
RANGE = (3200, 3299)


def auto_claim(**over):
    claim = {
        "sha": HEAD,
        "attempt_id": "attempt-0001",
        "claim_state": "SPAWNED",
        "claimed_at": "2026-10-01T00:00:00+13:00",
        "port": PORT,
        "port_released": False,
        "verdict": None,
        "reason": "",
    }
    claim.update(over)
    return claim


def doc_with(claim, *, workers_map=None):
    return {
        "prs": {"7": {"number": 7, "accessibility_auto": claim}},
        "workers": workers_map or {},
    }


class OwnershipCase(unittest.TestCase):

    def test_an_active_claim_owns_its_port(self):
        held = workers.claimed_product_server_ports(doc_with(auto_claim()))
        self.assertEqual(held, {PORT: "accessibility_auto:pr-7"})

    def test_a_released_claim_owns_nothing(self):
        self.assertEqual(
            workers.claimed_product_server_ports(
                doc_with(auto_claim(port_released=True))), {})

    def test_ownership_survives_every_terminal_outcome_until_release(self):
        # Completion, failure and cancellation all leave the server
        # possibly still bound. None of them is a release.
        for state, verdict, reason in (
                ("COMPLETE", routing.ACCESSIBILITY_AUTO_PASS, ""),
                ("COMPLETE", routing.ACCESSIBILITY_AUTO_FAIL, ""),
                ("COMPLETE", None, "TIMED_OUT"),
                ("SPAWNED", None, ""),
                ("PLANNED", None, "")):
            with self.subTest(state=state, verdict=verdict, reason=reason):
                claim = auto_claim(claim_state=state, verdict=verdict,
                                   reason=reason)
                self.assertIn(PORT, workers.claimed_product_server_ports(
                    doc_with(claim)))

    def test_a_malformed_claim_owns_nothing_rather_than_raising(self):
        for claim in (None, [], "claim", 7, {}, {"port": "3200"},
                      {"port": True}):
            with self.subTest(claim=repr(claim)):
                self.assertEqual(
                    workers.claimed_product_server_ports(doc_with(claim)), {})

    def test_a_document_without_prs_is_handled(self):
        for doc in ({}, {"prs": None}, {"prs": {}}, {"prs": {"7": None}}):
            with self.subTest(doc=repr(doc)):
                self.assertEqual(workers.claimed_product_server_ports(doc), {})


class ExclusiveAcrossBuildersCase(unittest.TestCase):
    """The live call site: selection is what dispatch actually uses."""

    def select(self, doc):
        with mock.patch.object(workers, "_reserved_job_file_ports",
                               return_value=set()):
            return workers.select_port_candidates(doc, *RANGE)

    def test_a_claimed_port_is_not_offered_to_a_builder(self):
        candidates, _ = self.select(doc_with(auto_claim()))
        self.assertNotIn(PORT, candidates)
        self.assertIn(PORT + 1, candidates, "the range itself is wrong")

    def test_a_released_port_is_offered_again(self):
        candidates, _ = self.select(doc_with(auto_claim(port_released=True)))
        self.assertIn(PORT, candidates)

    def test_the_exhaustion_reason_counts_claimed_ports(self):
        # Finite diagnostics: an operator reading "no bindable unowned
        # port" must be able to see that claims are why.
        _, why = self.select(doc_with(auto_claim()))
        self.assertIn("claimed=1", why)

    def test_worker_ownership_and_claim_ownership_both_exclude(self):
        doc = doc_with(auto_claim(),
                       workers_map={"task-001-builder": {"port": PORT + 1}})
        candidates, _ = self.select(doc)
        self.assertNotIn(PORT, candidates)
        self.assertNotIn(PORT + 1, candidates)

    def test_selection_still_performs_no_network_operation(self):
        # C-18 stage 3's boundary: this half may run under the state lock,
        # so it must not bind. G2 must not have smuggled a probe in.
        with mock.patch.object(workers.socket, "socket") as sock:
            self.select(doc_with(auto_claim()))
        sock.assert_not_called()


class ReleaseRequiresAnObservedGoneListenerCase(unittest.TestCase):

    def test_a_still_listening_port_is_not_releasable(self):
        ok, why = workers.port_release_permitted(PORT, {PORT})
        self.assertFalse(ok)
        self.assertEqual(why, workers.PORT_STILL_LISTENING)

    def test_a_failed_observation_is_not_an_absence(self):
        ok, why = workers.port_release_permitted(PORT, None)
        self.assertFalse(ok)
        self.assertEqual(why, workers.PORT_OBSERVATION_FAILED)

    def test_an_observed_gone_listener_permits_release(self):
        self.assertEqual(workers.port_release_permitted(PORT, set()), (True, ""))
        self.assertEqual(
            workers.port_release_permitted(PORT, {PORT + 1}), (True, ""))

    def test_a_non_port_is_refused_with_its_own_diagnostic(self):
        for value in (None, "3200", True, 3200.0, {}, []):
            with self.subTest(port=repr(value)):
                ok, why = workers.port_release_permitted(value, set())
                self.assertFalse(ok)
                self.assertEqual(why, workers.PORT_NOT_CLAIMED)

    def test_release_mutates_the_claim_only_on_success(self):
        claim = auto_claim()
        ok, why = workers.release_claimed_port(claim, {PORT})
        self.assertFalse(ok)
        self.assertEqual(why, workers.PORT_STILL_LISTENING)
        self.assertFalse(claim["port_released"],
                         "a refused release still freed the port")

        ok, why = workers.release_claimed_port(claim, set())
        self.assertTrue(ok)
        self.assertEqual(why, "")
        self.assertTrue(claim["port_released"])

    def test_a_refused_release_leaves_the_port_excluded(self):
        claim = auto_claim()
        workers.release_claimed_port(claim, {PORT})
        self.assertIn(PORT,
                      workers.claimed_product_server_ports(doc_with(claim)))


class ExpiryAloneDoesNotFreeAPortCase(unittest.TestCase):
    """The condition most likely to be implemented wrongly."""

    def test_an_ancient_claim_still_owns_a_listening_port(self):
        # The claim is long past any plausible lease. The listener is still
        # there. Reclaiming the port here would hand a Builder a socket
        # another process is bound to.
        ancient = auto_claim(claimed_at="2020-01-01T00:00:00+13:00",
                             claim_state="COMPLETE",
                             verdict=routing.ACCESSIBILITY_AUTO_FAIL)
        self.assertIn(PORT,
                      workers.claimed_product_server_ports(doc_with(ancient)))
        ok, why = workers.release_claimed_port(ancient, {PORT})
        self.assertFalse(ok)
        self.assertEqual(why, workers.PORT_STILL_LISTENING)

    def test_nothing_in_the_claim_shape_encodes_an_expiry_that_frees_it(self):
        # There is deliberately no lease field on the automated claim whose
        # passing would release the port. If one is added later, this test
        # is where the consequence has to be thought about again.
        self.assertNotIn("lease_expires_at",
                         routing.ACCESSIBILITY_AUTO_CLAIM_KEYS)
        self.assertIn("port_released", routing.ACCESSIBILITY_AUTO_CLAIM_KEYS)


class ReconciliationRecognisesTheOwnerCase(unittest.TestCase):

    def detect(self, doc, listening):
        with mock.patch.object(reconcile.hostcheck,
                               "read_candidate_port_range",
                               return_value=RANGE), \
             mock.patch.object(reconcile.proc, "listening_ports",
                               return_value=listening), \
             mock.patch.object(reconcile.proc, "worker_entry_processes",
                               return_value={}), \
             mock.patch.object(reconcile.gate_evidence, "scan_sidecars",
                               return_value=([], True)):
            findings, _ = reconcile.detect_orphans(doc)
        return findings

    def codes(self, findings):
        return [(f.check_id, f.resource_id) for f in findings]

    def test_a_claimed_listening_port_is_not_a_foreign_orphan(self):
        findings = self.detect(doc_with(auto_claim()), {PORT})
        self.assertNotIn(("FOREIGN_OR_ORPHAN_LISTENER", str(PORT)),
                         self.codes(findings))

    def test_an_unclaimed_listening_port_is_still_a_foreign_orphan(self):
        # The control. Without it the test above could pass because the
        # orphan check stopped working entirely.
        findings = self.detect(doc_with(auto_claim()), {PORT, PORT + 5})
        self.assertIn(("FOREIGN_OR_ORPHAN_LISTENER", str(PORT + 5)),
                      self.codes(findings))

    def test_a_released_claim_stops_shielding_its_port(self):
        findings = self.detect(doc_with(auto_claim(port_released=True)),
                               {PORT})
        self.assertIn(("FOREIGN_OR_ORPHAN_LISTENER", str(PORT)),
                      self.codes(findings))

    def test_a_claim_and_a_worker_on_one_port_is_a_conflict(self):
        # Recovery evidence: two owners for one port is exactly the state
        # that must reach a human, and it was invisible while claims were
        # not in the ownership map at all.
        doc = doc_with(auto_claim(),
                       workers_map={"task-001-builder": {"port": PORT}})
        findings = self.detect(doc, {PORT})
        self.assertIn(("PORT_ASSIGNMENT_CONFLICT", str(PORT)),
                      self.codes(findings))


class ClaimValidatorCase(unittest.TestCase):

    def test_a_non_bool_release_marker_is_refused(self):
        # The exclusion keys on `is True`, so a truthy non-bool would read
        # as "not released" to the allocator while looking released to a
        # human reading the record.
        for value in ("true", 1, 0, None, {}, []):
            with self.subTest(port_released=repr(value)):
                ok, why = routing.accessibility_auto_claim_is_valid(
                    auto_claim(claim_state="COMPLETE",
                               verdict=routing.ACCESSIBILITY_AUTO_PASS,
                               port_released=value))
                self.assertFalse(ok)
                self.assertEqual(why, "CLAIM_PORT_RELEASED_INVALID")

    def test_a_truthy_non_bool_does_not_release_the_port(self):
        claim = auto_claim(port_released="yes")
        self.assertIn(PORT,
                      workers.claimed_product_server_ports(doc_with(claim)))


if __name__ == "__main__":
    unittest.main()
