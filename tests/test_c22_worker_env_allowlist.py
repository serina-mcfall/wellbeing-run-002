"""C-22 — the worker child environment allow-list.

WHAT THIS COVERS AND WHAT IT DOES NOT. `worker_child_env` is the derivation
that approval-package action 6 would switch on. It is built and tested here;
it is NOT yet used by `run_job`, which still passes `dict(os.environ)`
through. `NotYetWiredCase` pins that gap deliberately, so the day the call
site changes, this file fails and the change is a decision rather than a
drift.

THE PROPERTY THAT MATTERS. It is an ALLOW-LIST. A credential name invented
after this file was written is dropped without anyone remembering to drop
it - which is the whole reason a deny-list was refused. The §2 names in
experiment/github-app/env-var-names.md are tested as absent not because
they are special, but because they are the ones whose leak would hand a
worker the identity that merges its own pull request.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from control import worker_entry

JOB = {"role": "builder", "task_id": "TASK-001", "provider": "claude"}

# Every name env-var-names.md §2 proposes for run002-sup. Not one of these
# may reach a worker: the key PATHS are as good as the keys to a process
# that can read them, and GH_TOKEN IS the identity `gh` acts as.
SUPERVISOR_ONLY = (
    "RUN002_GATE_APP_ID", "RUN002_GATE_APP_INSTALLATION_ID",
    "RUN002_GATE_APP_KEY_PATH", "RUN002_SUPERVISOR_APP_ID",
    "RUN002_SUPERVISOR_APP_INSTALLATION_ID", "RUN002_SUPERVISOR_APP_KEY_PATH",
    "RUN002_WORKER_APP_ID", "RUN002_WORKER_APP_INSTALLATION_ID",
    "RUN002_WORKER_APP_KEY_PATH", "RUN002_TRUSTED_GATE_PATH",
    "RUN002_TRUSTED_GATE_SHA",
)


class AllowListCase(unittest.TestCase):

    def test_the_supervisors_github_app_names_never_reach_a_worker(self):
        parent = {name: "simulated-not-a-real-value" for name in SUPERVISOR_ONLY}
        parent["PATH"] = "/usr/bin"
        env = worker_entry.worker_child_env(parent, JOB)
        for name in SUPERVISOR_ONLY:
            self.assertNotIn(name, env, f"{name} leaked into the worker")

    def test_an_unknown_name_is_dropped_rather_than_passed(self):
        """The allow-list property itself, stated as a test.

        A name nobody anticipated is the case a deny-list gets wrong.
        """
        env = worker_entry.worker_child_env(
            {"PATH": "/usr/bin", "SOME_FUTURE_CREDENTIAL": "x"}, JOB)
        self.assertNotIn("SOME_FUTURE_CREDENTIAL", env)
        self.assertEqual(env["PATH"], "/usr/bin")

    def test_the_names_a_child_needs_to_run_at_all_survive(self):
        parent = {"PATH": "/usr/bin", "HOME": "/home/run002-wrk",
                  "USER": "run002-wrk", "LANG": "en_NZ.UTF-8",
                  "TERM": "xterm", "TZ": "Pacific/Auckland"}
        env = worker_entry.worker_child_env(parent, JOB)
        for name, value in parent.items():
            self.assertEqual(env[name], value)

    def test_the_workers_own_gh_identity_survives(self):
        """GH_TOKEN passes - but it is the WORKER's token.

        The allow-list cannot tell one token from another; what guarantees
        this is the worker's is that the process is spawned by a supervisor
        that minted it. That is an isolation property, not a filter
        property, and it is why C-22 needs the UID split and not just this.
        """
        env = worker_entry.worker_child_env(
            {"GH_TOKEN": "simulated", "GH_CONFIG_DIR": "/home/run002-wrk/.gh"},
            JOB)
        self.assertEqual(env["GH_TOKEN"], "simulated")
        self.assertEqual(env["GH_CONFIG_DIR"], "/home/run002-wrk/.gh")

    def test_a_product_secret_passes_only_when_explicitly_named(self):
        parent = {"OPENROUTER_API_KEY": "simulated", "PATH": "/usr/bin"}
        self.assertNotIn("OPENROUTER_API_KEY",
                         worker_entry.worker_child_env(parent, JOB))
        passed = worker_entry.worker_child_env(
            parent, JOB, secrets=("OPENROUTER_API_KEY",))
        self.assertEqual(passed["OPENROUTER_API_KEY"], "simulated")

    def test_a_named_secret_that_is_absent_is_not_invented(self):
        env = worker_entry.worker_child_env(
            {"PATH": "/usr/bin"}, JOB, secrets=("SUPABASE_SECRET_KEY",))
        self.assertNotIn("SUPABASE_SECRET_KEY", env)


class PerJobIdentityCase(unittest.TestCase):

    def test_the_job_names_are_set_from_the_job_not_inherited(self):
        """A stale parent value must not become this job's identity."""
        env = worker_entry.worker_child_env(
            {"RUN_001_ROLE": "reviewer", "RUN_001_TASK": "TASK-009",
             "PORT": "9999"},
            {"role": "builder", "task_id": "TASK-001"})
        self.assertEqual(env["RUN_001_ROLE"], "builder")
        self.assertEqual(env["RUN_001_TASK"], "TASK-001")
        self.assertNotIn("PORT", env)

    def test_a_task_less_job_gets_an_empty_task_not_a_none_string(self):
        env = worker_entry.worker_child_env({}, {"role": "observer"})
        self.assertEqual(env["RUN_001_TASK"], "")

    def test_the_governed_port_is_set_when_the_job_carries_one(self):
        env = worker_entry.worker_child_env(
            {}, {"role": "builder", "task_id": "TASK-001", "port": 4310})
        self.assertEqual(env["PORT"], "4310")


class NotYetWiredCase(unittest.TestCase):
    """The honest status of action 6, pinned so it cannot drift silently."""

    def test_main_still_passes_the_parent_environment_through(self):
        source = Path(worker_entry.__file__).read_text(encoding="utf-8")
        body = source.split("def main(", 1)[1]
        self.assertIn("env = dict(os.environ)", body,
                      "main() no longer passes the parent environment "
                      "through. If that is deliberate, action 6 has been "
                      "taken: update this test and the approval package "
                      "together - do not delete this assertion to make a "
                      "green run.")


if __name__ == "__main__":
    unittest.main()
