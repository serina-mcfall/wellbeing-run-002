"""The gate process's environment allow-list.

WHY THIS EXISTS, AND IT WAS MEASURED RATHER THAN IMAGINED. §7 of the
approval package says the gate executes from a pinned, read-only export so
that a pull request cannot supply the code that judges it. `run_node`
originally spawned that process with no `env=`, which handed it the
Supervisor's whole environment - and two names in an environment decide
what code Node runs.

REPRODUCED 2026-10-02 against a real `git worktree add --detach` of the
pin: loading `live-gate.js` from the export fails with `Cannot find module
'ajv/dist/2020'`, and succeeds when `NODE_PATH` points at a writable
`node_modules` elsewhere. So the dependency tree of the gate could come
from outside the thing the pin covers. `NODE_OPTIONS=--require <file>`
is the same hole with a shorter path to arbitrary code.

WHAT THIS IS NOT. A product worker cannot write the Supervisor's
environment under §6, so this is defence in depth, not a live hole. It is
here because the countermeasure already existed one layer down
(`worker_entry.worker_child_env`) and because "the gate runs only pinned
code" is the claim §7 makes.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from control import gate_invoker

# The two that select code, and the reason this is an allow-list.
CODE_SELECTING = ("NODE_PATH", "NODE_OPTIONS")


class GateEnvAllowListCase(unittest.TestCase):

    def test_the_two_names_that_choose_what_code_runs_are_dropped(self):
        parent = {"PATH": "/usr/bin",
                  "NODE_PATH": "/tmp/attacker/node_modules",
                  "NODE_OPTIONS": "--require /tmp/attacker/payload.js"}
        env = gate_invoker.gate_child_env(parent)
        for name in CODE_SELECTING:
            self.assertNotIn(name, env, f"{name} reached the gate process")
        self.assertEqual(env["PATH"], "/usr/bin")

    def test_an_unknown_name_is_dropped_rather_than_passed(self):
        """The allow-list property itself.

        A name nobody anticipated is the case a deny-list gets wrong, and
        NODE_OPTIONS grows new values between Node releases.
        """
        env = gate_invoker.gate_child_env(
            {"PATH": "/usr/bin", "NODE_SOMETHING_FUTURE": "x"})
        self.assertNotIn("NODE_SOMETHING_FUTURE", env)

    def test_the_supervisors_github_credentials_do_not_reach_the_gate(self):
        """The gate reads git and GitHub through its OWN credential.

        Inheriting the Supervisor's `GH_TOKEN` would let the process that
        publishes the verdict act with the identity that merges it - the
        one property §1 exists to prevent.
        """
        env = gate_invoker.gate_child_env(
            {"GH_TOKEN": "simulated-not-a-token",
             "RUN002_SUPERVISOR_APP_KEY_PATH": "/keys/sup.pem",
             "RUN_002_GATE_APP_TOKEN": "simulated-not-a-token",
             "PATH": "/usr/bin"})
        self.assertEqual(sorted(env), ["PATH"])

    def test_the_names_a_node_process_needs_to_run_survive(self):
        parent = {"PATH": "/usr/bin", "HOME": "/home/run002-sup",
                  "LANG": "en_NZ.UTF-8", "TZ": "Pacific/Auckland"}
        self.assertEqual(gate_invoker.gate_child_env(parent), parent)

    def test_an_empty_parent_yields_an_empty_environment_not_a_crash(self):
        self.assertEqual(gate_invoker.gate_child_env({}), {})

    def test_the_parent_mapping_is_not_mutated(self):
        parent = {"PATH": "/usr/bin", "NODE_PATH": "/tmp/x"}
        gate_invoker.gate_child_env(parent)
        self.assertIn("NODE_PATH", parent,
                      "the filter mutated the environment it was reading")


class TheSpawnActuallyUsesItCase(unittest.TestCase):
    """A filter nothing calls is not a filter.

    `worker_child_env` is deliberately NOT wired, and a test pins that.
    This one is the opposite: it IS wired, and this asserts the wiring
    rather than trusting that writing the function was enough.
    """

    def test_run_node_passes_an_allow_listed_environment(self):
        source = Path(gate_invoker.__file__).read_text(encoding="utf-8")
        body = source.split("def run_node", 1)[1].split("def ", 1)[0]
        self.assertIn("env=gate_child_env(os.environ)", body,
                      "run_node no longer passes an allow-listed "
                      "environment; the gate would inherit NODE_PATH and "
                      "NODE_OPTIONS again")


if __name__ == "__main__":
    unittest.main()
