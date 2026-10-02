# Stage 1 — execution log

Approval recorded verbatim at `experiment/evidence/STAGE-1-approval.txt`.
Executed from 2026-10-02. **This file is appended to as steps complete; a
step with no entry here has not been run.**

Branch `wip/c05-1-persistence`. Pin `ab1ceee`.

**`<WS>` = `/home/serina/wellbeing-agent-experiment/agent-run-002`**
**`<THROW>` = `serina-mcfall/run-002-isolation-probe`**
**`<PROD>` = `serina-mcfall/wellbeing-run-002` — untouched by Stage 1**

---

## A0 — pre-change capture — **PASS** (2026-10-02)

Seven files written to `~/run-002-stage1-rollback/`. **Every expectation
the procedure recorded was confirmed, not assumed:**

| File | Result |
|---|---|
| `identities-BEFORE.txt` | **ABSENT** for group `run002`, user `run002-sup`, user `run002-wrk` — all three are this stage's to create, and therefore its to delete |
| `ownership-BEFORE.txt` | **6,925 lines**, against a `find <WS>` count of **6,925** — exact, not "within a few" |
| `worktrees-BEFORE.txt` | 12 entries — the 11 pre-existing plus main |
| `refs-BEFORE.txt` | 22 refs |
| `worktree-root-BEFORE.txt` | **ABSENT** `…/agent-run-002__worktrees`; parent `755 serina serina` — confirming the Supervisor could not have created it |
| `sharedrepo-BEFORE.txt` | **ABSENT** `core.sharedRepository` |
| `origin-url-BEFORE.txt` | the two lines **disagree**: stored `https://github.com/serina-mcfall/wellbeing-run-002.git`, resolved push `git@github.com:…` — the `insteadOf` rewrite, confirmed live |

**The rollback is now executable.** Every undo in Stage 1 names something
these bytes describe.

---

## A1 — the throwaway repository — **PASS** (2026-10-02)

**`<THROW>` = `serina-mcfall/run-002-isolation-probe`, PUBLIC**, per the
operator's approval.

**Contents — three files, one commit, built outside the repository so no
Run 002 content could reach it:**

```
.github/workflows/ci.yml
README.md
fixture.txt
```

Confirmed by reading the published tree back from GitHub, not from the
local copy. **No product code, no repository history, no runtime
evidence, no credentials** — the approval's own exclusion list, satisfied
by construction: the fixture was assembled in a scratchpad directory and
given its own fresh `git init`, so it shares no object and no commit with
`<WS>`.

**The workflow is trivial by requirement**, not by accident: one
`ubuntu-latest` step that echoes and exits 0, `timeout-minutes: 5`. It is
**not** a copy of `<WS>/.github/workflows/ci.yml`, which installs
Playwright Chromium under a 25-minute timeout.

**Verified:** `visibility PUBLIC`, `isPrivate false`, default branch
`main`; and on the head commit `40ea370`, a check run named exactly **`ci`**,
`completed`, `success`, in **6 seconds**.

**Workflow-run budget: 1 of 10 used.** Standard GitHub-hosted runner, and
free on a public repository.

---

## A2 onward — **BLOCKED ON THE OPERATOR, NOT ON THE WORK**

Measured at execution time, not assumed:

- **`sudo -n true` fails — sudo requires a password on this host.** So A2,
  A5, A6, C1–C3 and every `sudo -u run002-*` V-step cannot be run from an
  agent session.
- **GitHub Apps cannot be created without a browser.** A3 is a UI flow;
  A4's private keys are generated and downloaded through it.

Both are access controls working as intended. The exact actions are in
`STAGE-1-OPERATOR-ACTIONS.md` beside this file.

---

## THE CONSEQUENCE OF A5 THAT THE PROCEDURE DID NOT STATE

**A5 ends this session's access to the repository.** It sets
`chown -R run002-sup:run002 <WS>` and `chmod -R 0750 <WS>`, and `serina`
is not a member of `run002`. From that moment `serina` can neither read
nor write `<WS>`: no test run, no commit, no document edit, except through
`sudo`.

**Therefore everything that must be written into the repository is written
BEFORE A5 runs.** This log, the approval record and the operator actions
are committed first, deliberately.

**Optional, and the operator's call:** `sudo usermod -aG run002 serina`
restores *read* access for the operator's own account without granting
write to anything the arrangement protects — `.runtime/` stays `0700`
`run002-sup`, so it remains unreadable to the group either way. It is not
required by any V-step and is not part of the approved arrangement; it is
convenience for the human, and it widens nothing a root-capable operator
did not already have.
