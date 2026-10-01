"""C-05.3a: one authoritative definition of the security vocabulary.

Three modules need the same tokens. routing produces the adjudication
reasons, gate_evidence produces the apparatus reasons, and the PR-record
claim validator checks both. Before control/security_contract.py they were
defined in two places and composed in a third, which is one rename away
from a token that exists under two spellings - and the failure mode is
silent: a reason written by one module and refused by another looks like
malformed evidence rather than a typo.

So the literals live in security_contract and everything else aliases
them. These tests hold that line.

They deliberately do NOT compare strings with `is`. CPython interns short
string literals, so two independent definitions of "TIMED_OUT" would
almost certainly BE the same object - an identity assertion would pass
while the duplication it was meant to catch sat right there in the source.
Value equality proves the alias resolves; a source-level check proves
there is only one definition; and the frozenset, a real re-exported
object, is the one thing identity legitimately proves.
"""

from __future__ import annotations

import ast
import re
import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from control import gate_evidence, routing, security_contract as sc  # noqa: E402

CONTROL = Path(__file__).resolve().parent.parent / "control"

VERDICT_TOKENS = ("SECURITY_PASS", "SECURITY_FAIL", "SECURITY_UNPARSEABLE")
ADJUDICATION_TOKENS = ("OUTPUT_UNPARSEABLE", "VERDICT_UNRECOGNISED",
                       "SURFACES_INCOMPLETE", "FINDING_FIELDS_INVALID",
                       "PASS_WITH_BLOCKING_FINDINGS",
                       "FAIL_WITHOUT_BLOCKING_FINDINGS")
APPARATUS_TOKENS = ("TIMED_OUT", "EXIT_NONZERO", "OUTPUT_MISSING",
                    "PROVIDER_FAILURE", "SPAWN_OR_RUN_INCOMPLETE")
ALL_REASON_TOKENS = ADJUDICATION_TOKENS + APPARATUS_TOKENS


class VocabularyCase(unittest.TestCase):

    def test_the_eleven_failure_reasons_are_exactly_these(self):
        self.assertEqual(len(sc.SECURITY_FAILURE_REASONS), 11)
        self.assertEqual(
            sc.SECURITY_FAILURE_REASONS,
            frozenset(getattr(sc, name) for name in ALL_REASON_TOKENS))

    def test_pass_with_findings_does_not_exist(self):
        # C-05b reversed the rule it encoded. Its absence is the contract.
        self.assertFalse(hasattr(sc, "PASS_WITH_FINDINGS"))
        self.assertNotIn("PASS_WITH_FINDINGS", sc.SECURITY_FAILURE_REASONS)

    def test_the_verdict_set_holds_only_real_verdicts(self):
        self.assertEqual(sc.SECURITY_VERDICTS,
                         frozenset({sc.SECURITY_PASS, sc.SECURITY_FAIL}))
        # UNPARSEABLE is the parser's answer, never a verdict a reviewer
        # emitted, and never a pass.
        self.assertNotIn(sc.SECURITY_UNPARSEABLE, sc.SECURITY_VERDICTS)

    def test_every_token_is_its_own_name(self):
        for name in VERDICT_TOKENS + ALL_REASON_TOKENS:
            with self.subTest(token=name):
                self.assertEqual(getattr(sc, name), name)


class ReExportCase(unittest.TestCase):
    """Value equality, not identity - see the module docstring."""

    def test_routing_re_exports_the_verdict_and_adjudication_tokens(self):
        for name in VERDICT_TOKENS + ADJUDICATION_TOKENS:
            with self.subTest(token=name):
                self.assertEqual(getattr(routing, name), getattr(sc, name))

    def test_gate_evidence_re_exports_the_apparatus_tokens(self):
        for name in APPARATUS_TOKENS:
            with self.subTest(token=name):
                self.assertEqual(getattr(gate_evidence, name),
                                 getattr(sc, name))

    def test_the_failure_set_is_one_shared_object(self):
        # A frozenset is a real re-exported object, so identity here proves
        # what it claims to - unlike a short interned string literal.
        self.assertIs(gate_evidence.SECURITY_FAILURE_REASONS,
                      sc.SECURITY_FAILURE_REASONS)

    def test_routing_shares_the_verdict_set_object(self):
        self.assertIs(routing.SECURITY_VERDICTS, sc.SECURITY_VERDICTS)


class SingleDefinitionCase(unittest.TestCase):
    """Source-level ownership: the literal exists once, here."""

    @staticmethod
    def _assignments(module_name, token):
        """Lines of the form `TOKEN = "TOKEN"` - an independent definition,
        as opposed to `TOKEN = security_contract.TOKEN`, an alias."""
        text = (CONTROL / module_name).read_text(encoding="utf-8")
        pattern = re.compile(rf'^{token}\s*=\s*["\']', re.MULTILINE)
        return pattern.findall(text)

    def test_security_contract_defines_each_token_exactly_once(self):
        text = (CONTROL / "security_contract.py").read_text(encoding="utf-8")
        for token in VERDICT_TOKENS + ALL_REASON_TOKENS:
            with self.subTest(token=token):
                defs = re.findall(rf'^{token}\s*=\s*["\']', text, re.MULTILINE)
                self.assertEqual(len(defs), 1, f"{token} defined {len(defs)}x")

    def test_no_sibling_module_defines_the_literals_independently(self):
        for module_name in ("routing.py", "gate_evidence.py", "state.py",
                            "supervisor.py", "reconcile.py", "watchdog.py"):
            for token in VERDICT_TOKENS + ALL_REASON_TOKENS:
                with self.subTest(module=module_name, token=token):
                    self.assertEqual(
                        self._assignments(module_name, token), [],
                        f"{module_name} defines {token} independently")

    def test_the_sibling_modules_alias_from_security_contract(self):
        for module_name, tokens in (("routing.py",
                                     VERDICT_TOKENS + ADJUDICATION_TOKENS),
                                    ("gate_evidence.py", APPARATUS_TOKENS)):
            lines = [ln.strip() for ln in (CONTROL / module_name).read_text(
                encoding="utf-8").splitlines()]
            for token in tokens:
                with self.subTest(module=module_name, token=token):
                    self.assertIn(f"{token} = security_contract.{token}", lines,
                                  f"{module_name} does not alias {token}")


class LeafModuleCase(unittest.TestCase):

    def test_it_imports_nothing_from_the_control_package(self):
        # Anything may depend on this module, so it may depend on nothing:
        # a control-plane import here could reintroduce the cycle the
        # module exists to avoid.
        lines = [ln.strip() for ln in (CONTROL / "security_contract.py")
                 .read_text(encoding="utf-8").splitlines()]
        offenders = [ln for ln in lines
                     if ln.startswith(("from .", "from control", "import control"))
                     or (ln.startswith("import ") and not ln.startswith("import _"))]
        self.assertEqual(offenders, [], f"control-plane import present: {offenders}")
        for banned in ("state", "routing", "gate_evidence", "workers", "gh",
                       "proc", "supervisor", "providers", "notify"):
            with self.subTest(module=banned):
                self.assertIsNone(getattr(sc, banned, None))

    def test_it_holds_no_module_objects_at_all(self):
        for name, value in vars(sc).items():
            if name.startswith("__"):
                continue
            with self.subTest(attribute=name):
                self.assertNotIsInstance(value, types.ModuleType)

    def test_it_performs_no_io_and_defines_no_behaviour(self):
        # Parsed, not grepped. A string scan over the source would trip on
        # the module's own prose - the docstring says the module opens no
        # socket, and "socket" is then "found in the file".
        tree = ast.parse((CONTROL / "security_contract.py")
                         .read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            with self.subTest(node=type(node).__name__):
                self.assertNotIsInstance(
                    node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef,
                           ast.With, ast.Try, ast.Lambda))
            # frozenset(...) is the only call a vocabulary needs; anything
            # else executing at import time is behaviour, not a constant.
            if isinstance(node, ast.Call):
                with self.subTest(call=ast.dump(node.func)[:60]):
                    self.assertIsInstance(node.func, ast.Name)
                    self.assertEqual(node.func.id, "frozenset")
        # Every top-level statement is a docstring or a plain assignment,
        # except the __future__ import.
        for node in tree.body:
            with self.subTest(statement=type(node).__name__):
                self.assertIsInstance(
                    node, (ast.Assign, ast.Expr, ast.ImportFrom))


if __name__ == "__main__":
    unittest.main()
