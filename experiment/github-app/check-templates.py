#!/usr/bin/env python3
"""TEMPLATE CHECKER — reads only. Applies nothing. Deployment is not authorised.

Checks the unapplied templates in this directory against the repository as it
actually is. It makes no network call, no GitHub call, touches no credential,
and writes nothing. It is deliberately NOT part of any test suite: run it by
hand.

    python3 experiment/github-app/check-templates.py

What it proves:
  A. Every JSON template parses and carries its TEMPLATE notice.
  B. The AFTER branch-protection body preserves the existing CI requirement
     byte-for-byte, and does not fall into the two traps §4.2 and §4.3a name
     (required_pull_request_reviews sent as null; the deprecated `contexts`
     alias sent alongside `checks`).
  C. The three App manifests are a real separation: exactly one principal may
     write a commit status, and it is not a principal that may merge.
  D. Every GitHub call site the proposal attributes a permission to still
     exists in the code at the cited location — so the permission set cannot
     silently drift away from what the code calls (findings F1-F4).
  E. F7's REMAINING half (no code constructs a commit-status API call) and
     §4.3a (required_checks is still exactly ["ci"]) still hold.

     F7's OTHER half is now CLOSED and this check no longer speaks to it.
     F7 was "the permission at the centre of the proposal exists to serve
     code nobody has written"; control/publisher.py is that code, and it
     exists. What this check still proves is narrower and still worth
     proving: publisher.py reaches GitHub only through an injected
     `poster`, so no module in this repository builds the `statuses/` REST
     path. The day one does, the transport is real and the "it cannot call
     out by accident" argument needs re-making rather than assuming.
"""

import json
import pathlib
import re
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent.parent

failures: list[str] = []
notes: list[str] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        failures.append(label)


def load(name: str) -> dict:
    return json.loads((HERE / name).read_text())


# ---------------------------------------------------------------- A. parsing
TEMPLATES = [
    "app-manifest-gate.json",
    "app-manifest-supervisor.json",
    "app-manifest-worker.json",
    "branch-protection-BEFORE.json",
    "branch-protection-AFTER.json",
]
docs = {}
for name in TEMPLATES:
    try:
        docs[name] = load(name)
        parsed = True
    except Exception as exc:  # noqa: BLE001
        parsed = False
        notes.append(f"{name}: {exc}")
    check(parsed, f"A. {name} parses as JSON")
    if parsed:
        notice = docs[name].get("_TEMPLATE_NOTICE")
        ok = isinstance(notice, list) and any("TEMPLATE" in line for line in notice)
        check(ok, f"A. {name} carries a TEMPLATE notice")

if failures:
    print("\nparsing failed; stopping")
    sys.exit(1)

# ------------------------------------------------- B. branch-protection body
before = docs["branch-protection-BEFORE.json"]["body"]
after = docs["branch-protection-AFTER.json"]["body"]

ci_before = [c for c in before["required_status_checks"]["checks"] if c["context"] == "ci"]
ci_after = [c for c in after["required_status_checks"]["checks"] if c["context"] == "ci"]
check(ci_before == ci_after == [{"context": "ci", "app_id": 15368}],
      "B. AFTER preserves the existing `ci` requirement unchanged",
      f"before={ci_before} after={ci_after}")

check("contexts" not in after["required_status_checks"],
      "B. AFTER omits the deprecated `contexts` alias (sending both is an error)")

rpr = after.get("required_pull_request_reviews")
check(isinstance(rpr, dict),
      "B. AFTER sends required_pull_request_reviews as an OBJECT, not null",
      "control/preflight.py:174 computes `reviews is not None`; null fails ctl start")
check(isinstance(rpr, dict) and rpr.get("required_approving_review_count") == 0,
      "B. AFTER sets required_approving_review_count to 0")
check(after.get("enforce_admins") is True, "B. AFTER sets enforce_admins true")
check("restrictions" in after and after["restrictions"] is None,
      "B. AFTER sends restrictions: null (the PUT schema requires the key)")

new_ctx = [c for c in after["required_status_checks"]["checks"]
           if c["context"] == "run-002/independent-review"]
check(len(new_ctx) == 1 and new_ctx[0]["app_id"] == "__GATE_APP_ID__",
      "B. AFTER still carries the unresolved __GATE_APP_ID__ placeholder",
      "a real App ID in a template would mean an App exists; none does")

# Every key present in BEFORE must be present in AFTER: the PUT replaces the
# whole object, so an omitted key is a silently cleared setting.
#
# RECURSIVE, and it has to be. This compared top-level keys only until an
# independent review walked the two documents by hand and found
# `required_pull_request_reviews.require_last_push_approval` set in BEFORE and
# absent from AFTER - a nested field dropped by a check that reported
# "missing: []" and a §3 table that claimed every field was restated. The
# shallow version could not have caught it, and the protection PUT clears
# exactly this kind of field without comment.
def _paths(node, prefix=""):
    """Every dotted key path in a nested object. Lists are leaves.

    A list is a leaf because `required_status_checks.checks` is compared
    element-wise by the `ci`-preservation check above; descending into it
    here would report index paths that say nothing about a cleared setting.
    """
    out = set()
    for key, value in node.items():
        path = f"{prefix}{key}"
        out.add(path)
        if isinstance(value, dict):
            out |= _paths(value, path + ".")
    return out


missing = sorted(_paths(before) - _paths(after))
check(not missing, "B. AFTER restates every field BEFORE sets, at every depth",
      f"missing: {missing}")

# -------------------------------------------------------- C. the separation
perms = {k: docs[f"app-manifest-{k}.json"]["manifest"]["default_permissions"]
         for k in ("gate", "supervisor", "worker")}

writers = [k for k, p in perms.items() if p.get("statuses") == "write"]
check(writers == ["gate"], "C. exactly one principal may WRITE a commit status", str(writers))

check(perms["gate"].get("checks") == "read",
      "C. F1 resolved — the gate holds checks:read")
check("contents" not in perms["gate"] and perms["gate"].get("pull_requests") != "write",
      "C. the publisher cannot merge what it blessed (no contents, no PR write)")
check(perms["supervisor"].get("pull_requests") == "write"
      and perms["supervisor"].get("contents") == "write",
      "C. F2 resolved — a principal exists that can execute a merge")
check(perms["supervisor"].get("statuses") != "write",
      "C. the merger cannot publish the verdict it acts on")
check(perms["supervisor"].get("administration") == "read",
      "C. F3 resolved — administration:read present")
check(perms["supervisor"].get("actions") == "read",
      "C. F4 resolved — actions:read present")
for forbidden in ("statuses", "checks", "administration", "actions"):
    check(forbidden not in perms["worker"],
          f"C. the worker has no `{forbidden}` permission at any level")

# ------------------------------------------- D. call sites still where cited
# (file, 1-based line, substring that must be on it, who needs it)
CALL_SITES = [
    ("apparatus/adapters/ci-result.js", 69, "check-runs", "gate: checks read (F1)"),
    ("control/gh.py", 66, "statusCheckRollup", "supervisor: checks read + statuses read"),
    ("control/gh.py", 71, "pr", "worker/supervisor: pull_requests read"),
    ("control/gh.py", 77, "pr", "worker/supervisor: pull_requests read"),
    ("control/gh.py", 121, "update-branch", "supervisor: pull_requests write (F2)"),
    ("control/gh.py", 131, "ready", "supervisor: pull_requests write (F2)"),
    ("control/gh.py", 135, "merge", "supervisor: pull_requests + contents write (F2)"),
    ("control/gh.py", 140, "create", "worker: pull_requests write"),
    ("control/gh.py", 145, "comment", "supervisor: issues write (UNVERIFIED)"),
    ("control/gh.py", 150, "protection", "supervisor: administration read (F3)"),
    ("control/gh.py", 156, "run", "supervisor: actions read (F4)"),
    ("prompts/builder.md", 60, "gh pr create", "worker: pull_requests write"),
    ("prompts/reviewer.md", 15, "gh pr diff", "worker: pull_requests read"),
    ("prompts/reviewer.md", 16, "gh pr view", "worker: pull_requests read"),
    ("prompts/security.md", 21, "gh pr diff", "worker: pull_requests read"),
    ("prompts/security.md", 22, "gh pr view", "worker: pull_requests read"),
    ("prompts/accessibility.md", 33, "gh pr diff", "worker: pull_requests read"),
    ("prompts/accessibility.md", 34, "gh pr view", "worker: pull_requests read"),
    ("prompts/observer.md", 13, "gh pr list", "worker: pull_requests read"),
]
for rel, lineno, needle, who in CALL_SITES:
    path = ROOT / rel
    lines = path.read_text().splitlines()
    on_line = lineno <= len(lines) and needle in lines[lineno - 1]
    if on_line:
        check(True, f"D. {rel}:{lineno} still calls `{needle}`", who)
    else:
        found = [i + 1 for i, l in enumerate(lines) if needle in l]
        check(False, f"D. {rel}:{lineno} no longer contains `{needle}`",
              f"{who}; found at {found or 'nowhere'} — re-cite before relying on this")

# --------------------------------------------------- E. F7 and §4.3a still true
grep = subprocess.run(
    ["git", "grep", "-n", "statuses/", "--", "*.py", "*.js"],
    cwd=ROOT, capture_output=True, text=True,
)
# `git grep` prints REPO-RELATIVE paths with no leading slash, so the
# original `"/experiment/" not in l` filter matched nothing and this check
# found ITSELF the moment this file became tracked — a false positive that
# would have made the whole run untrustworthy. `git grep` only searches
# tracked files, which is exactly why committing the checker was the event
# that broke it.
hits = [l for l in grep.stdout.splitlines()
        if not l.startswith("experiment/") and "node_modules" not in l]
check(not hits, "E. no code constructs a commit-status API call",
      f"{len(hits)} hit(s): {hits[0]}" if hits
      else "control/publisher.py posts only through an injected `poster`; "
           "no module builds the statuses/ path itself")

required = json.loads((ROOT / "config" / "experiment.json").read_text())["github"]["required_checks"]
check(required == ["ci"],
      "E. §4.3a holds — config required_checks is still exactly ['ci']", str(required))
check("run-002/independent-review" not in required,
      "E. §4.3a holds — the status context was NOT added to required_checks",
      "ci-result.js iterates this list against the CHECK-RUN surface; a commit "
      "status never appears there, so adding it is a permanent silent deny")

# ------------------------------------------------------------- the pinned SHA
pin = re.search(r"TRUSTED APPARATUS REVISION[^`]*`([0-9a-f]{40})`",
                (ROOT / "experiment" / "GITHUB-APP-WORKER-ISOLATION-PROPOSAL.md").read_text())
if pin:
    sha = pin.group(1)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT,
                          capture_output=True, text=True).stdout.strip()
    # pin == HEAD was the first version of this check and it was WRONG in
    # a way that matters: it goes red the moment anything is committed,
    # including the commit that records the pin itself and every later
    # documentation commit. A check that cannot be satisfied is a check
    # people learn to ignore.
    #
    # The invariant that actually matters is narrower and stronger: the
    # pin must be REACHABLE from HEAD, and nothing the gate executes may
    # have changed since it. A documentation commit on top of the pin is
    # harmless; one line of apparatus/ or control/ is not.
    def git(*args):
        return subprocess.run(["git", *args], cwd=ROOT,
                              capture_output=True, text=True)

    reachable = git("merge-base", "--is-ancestor", sha, "HEAD").returncode == 0
    check(reachable, "F1. the pinned trusted revision is reachable from HEAD",
          f"pin={sha[:7]} head={head[:7]}"
          + ("" if reachable else " — the pin names a commit this branch "
                                  "does not contain; it cannot be exported"))
    if reachable:
        # `bin/` and `.github/` added 2026-10-02. `bin/` was protected by
        # the proposal's §6 asset table but absent from this list, so a
        # change to an entry point the gate's own run depends on would not
        # have gone red. `.github/` defines the `ci` check run that
        # ci-result.js treats as authoritative, and it is matched by NAME
        # alone - so the definition of "CI passed" is part of what the pin
        # must hold still.
        drift = git("diff", "--name-only", sha, "HEAD",
                    "--", "apparatus", "control", "protocol", "prompts",
                    "config", "bin", ".github").stdout.split()
        check(not drift,
              "F2. nothing the gate executes has changed since the pin",
              f"pin={sha[:7]}..head={head[:7]} clean across apparatus/, "
              "control/, protocol/, prompts/, config/, bin/, .github/"
              if not drift else
              f"{len(drift)} file(s) changed since the pin, including "
              f"{drift[0]} — RE-PIN before approving")
else:
    check(False, "F. the proposal states a 40-hex TRUSTED APPARATUS REVISION")

print()
if notes:
    print("notes:")
    for n in notes:
        print("  " + n)
print(f"{'FAILED' if failures else 'OK'} — {len(failures)} failing check(s)")
sys.exit(1 if failures else 0)
