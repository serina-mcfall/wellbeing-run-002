# C-20a(C) — the deployment procedure

**The runnable form of the approval package's twenty actions and the
proposal's fourteen V-steps.** The package decides *whether*; this decides
*in what order, with which command, and how you know it worked*.

| | |
|---|---|
| Date | 2026-10-02 |
| State | **NOTHING HERE HAS BEEN RUN. No App, no installation, no key, no export, no host user, no protection change, no commit status.** |
| Authorises | nothing. Approval is decision **A** in the package's §9, and it has not been given |
| Derived from | `experiment/C-20a-APPROVAL-PACKAGE.md` §9 (the actions), §5 (expected-head), §7 (the trusted revision and THE INVOKER TRAP), §11 (rollback); `experiment/GITHUB-APP-WORKER-ISOLATION-PROPOSAL.md` §5 (V1–V14) |
| Companion | §10 below is the **test-PR-lifecycle walkthrough and its named gaps**. Read it before starting. A procedure that reads complete and is not is worse than one that says where it stops |

**ORDER IS LOAD-BEARING AND TWO INVERSIONS ARE CATASTROPHIC.** Both have
already been found by checking rather than by reasoning:

1. **Protection (action 11) is LAST.** Applied before the publisher can
   post, every product pull request is blocked forever — defect F5.
2. **`npm ci --omit=dev` (action 7b) comes BEFORE `chown`/`chmod`
   (action 7c).** `chmod -R a-w` freezes the export; an export frozen
   without its dependencies cannot load the gate at all, and the failure
   is `Cannot find module 'ajv/dist/2020'`, reproduced.

---

## 0. What this document is not

**It is not an authorisation, and following it is not implied by reading
it.** Every step marked **DEPLOY** needs the operator's word first.

**No step here has ever been executed.** Every claim about what a command
returns is derived from GitHub's published documentation or from the
package's own prior measurements, and is labelled as such where it
matters. Nothing in this repository has made a GitHub call.

**Classification used throughout:**

| Label | Meaning |
|---|---|
| `COMPONENT` | the code exists and is unit-tested locally |
| `SIMULATED` | the connected path is exercised against injected doubles — real `node`, real throwaway git repos, no network |
| `GITHUB` | verified against real GitHub. **Nothing in this document carries this label.** |

---

## 1. Prerequisites

Before step 1, all of these must be true. Starting without them causes
harm, not merely inconvenience.

- [ ] Decision **A** given in writing (§9's approval request), and quoted
      verbatim into the repository alongside this procedure.
- [ ] Decisions **B** (the protection change), **C** (`origin` SSH →
      HTTPS) and **D** (the protected CI path set) answered. **C must be
      answered before action 5**: after 5 re-owns the checkout, declining
      it requires some other answer to how a worker pushes.
- [ ] `python3 experiment/github-app/check-templates.py` exits `OK — 0
      failing check(s)`. **`F2` must be green**, meaning nothing the gate
      executes has moved since the pin. If `F2` is red, RE-PIN before
      anything else; the export would otherwise run code the operator did
      not approve.
- [ ] `python3 -m unittest discover -s tests -q` → `OK`.
- [ ] `cd apparatus && node --test` → `0 fail`.
- [ ] `sudo` available to the operator, and the operator is a human.
      Every V-step's "who runs it" column is `human`.
- [ ] The trusted revision confirmed or re-stated **at approval time**,
      not inherited from the document:
      `47f35f50fd9ebaebf6eadbd2902d53123ef1ed44`. Below it is written
      `<PIN>`. **This line was two re-pins stale on 2026-10-02** — it
      still named `b2df44f` after the proposal's §9.5 had moved twice.
      Read the pin out of §9.5, never out of this line.

**Notation.** `<PIN>` the 40-hex trusted revision · `<THROW>` the
throwaway repository `owner/name` · `<PROD>`
`serina-mcfall/wellbeing-run-002` · `<WS>` the live workspace
`/home/serina/wellbeing-agent-experiment/agent-run-002` · `<EXPORT>`
`/opt/run-002/gate-<PIN>`.

**A standing rule for every step.** No command in this procedure may
`cat`, `grep`, `head`, `tail` or glob a private key or a token file.
Existence and mode are checked with `ls -l` or `test -r`, and nothing
else. Where a step would otherwise print a credential, it asserts on an
exit code instead.

---

## 2. Phase A — local, reversible, no GitHub object exists yet

### A0. Capture what this stage is about to change — **do this first** *(GATE)*

**ADDED 2026-10-02. Nothing else in Phase A may run before it.**

D2 captures live branch protection *before* the PUT that overwrites it,
and those bytes are what makes Stage 2 reversible. The host side had no
equivalent, and the two undos below asked for a restore from bytes nobody
had taken: A2 created two accounts without first asking whether they
already existed, and A5 re-owned a whole workspace whose previous modes
were recorded nowhere. **A rollback that cannot name the prior state is
not a rollback.**

The capture lives **outside `<WS>`**, because A5 re-owns `<WS>`.

```
mkdir -p ~/run-002-stage1-rollback
R=~/run-002-stage1-rollback

# 1. Do the identities already exist? Answered BEFORE anything creates them.
{ getent group  run002     || echo 'ABSENT group run002'
  getent passwd run002-sup || echo 'ABSENT user run002-sup'
  getent passwd run002-wrk || echo 'ABSENT user run002-wrk'
} > $R/identities-BEFORE.txt

# 2. Mode, owner and group of every path A5's `-R` reaches (~6,900 lines).
find <WS> -printf '%m %u %g %p\n' > $R/ownership-BEFORE.txt

# 3. The refs and worktrees V14a is about to commit on and push.
git -C <WS> worktree list > $R/worktrees-BEFORE.txt
git -C <WS> for-each-ref --format='%(objectname) %(refname)' refs/heads \
  > $R/refs-BEFORE.txt
```

→ **Verify:** all four files exist and are non-empty;
`wc -l $R/ownership-BEFORE.txt` is within a few of `find <WS> | wc -l`.
→ **If `identities-BEFORE.txt` does NOT say ABSENT for all three:** the
account or group is **not this stage's to create or delete**. Do not
create it, and strike it from the rollback — `userdel` on a pre-existing
account destroys someone else's home directory.
→ **This file is how A2's and A5's undos are executed.** They are no
longer "restore the previous modes"; they are a loop over these bytes.
→ **Known limit:** the restore loop below splits on whitespace, so a path
containing a newline would not round-trip. None exists in `<WS>`.

### A1. Create the throwaway repository *(action 0, DEPLOY)*

Create a repository under the operator's own account, e.g.
`serina-mcfall/run-002-isolation-probe`. Give it a `main` with one
commit and a workflow that produces a check run named `ci`.

**VISIBILITY IS A COST DECISION, NOT A DETAIL.** This step used to say
"private" with no further comment, and §13's cost row said Stage 1 has
"no billable operation". Those two cannot both be relied on: **GitHub
Actions minutes are free on public repositories and metered on private
ones**, and V10 and V12 each require "let `ci` pass" on this repository,
so Stage 1 *does* run Actions here. The account's remaining quota could
not be read from this host — `gh api /users/<owner>/settings/billing/actions`
returns 404 because the ambient token lacks the `user` scope, and
refreshing that scope is itself an authorisation change outside this
stage. So the quota is **unverified**, and the choice is the operator's:

| | |
|---|---|
| **Public** | Actions minutes are free. Nothing but one commit and a trivial workflow is ever in this repository — no product code, no secrets, no credential. Removes the question rather than bounding it |
| **Private** | metered against the account's monthly allowance, which this host cannot read. Bounded by the workflow being trivial and by the run count below |

**Whichever is chosen, the `ci` workflow here must be TRIVIAL** — a
single `ubuntu-latest` step that exits 0 — and must **not** be a copy of
`<WS>/.github/workflows/ci.yml`, which installs Playwright Chromium and
carries `timeout-minutes: 25`. The V-steps need a check run *named* `ci`
with a conclusion; they need nothing it does. Expect **under ten runs**
across V3c, V7, V8, V9, V10 and V12 — minutes, not hours.

→ **Verify:** `gh repo view <THROW> --json name,visibility` returns the
name and the visibility you chose. Then `gh run list --repo <THROW>
--limit 1` after the first PR, to see what a run actually costs in time.
→ **If failed:** nothing else has happened. Fix and retry.
→ **Undo:** `gh repo delete <THROW>`.

### A2. Create the OS users *(action 1, DEPLOY)*

```
sudo groupadd run002
sudo useradd -m -g run002 run002-sup
sudo useradd -m -g run002 run002-wrk
```

→ **Verify:** `id run002-sup` and `id run002-wrk` both resolve, both in
group `run002`. **`id` run after creation cannot tell "I made this" from
"this was already here"** — that is what A0's `identities-BEFORE.txt`
answers, and it must say ABSENT for all three before this step runs.
→ **If failed:** DO NOT PROCEED to action 5. Every ownership claim below
depends on these two identities existing and being distinct.
→ **Undo, for the identities A0 recorded as ABSENT and no others:**
`sudo userdel run002-wrk; sudo userdel run002-sup; sudo groupdel run002`.
**Without `-r`, deliberately.** This document used to say `userdel -r`
while §13's rollback said plain `userdel`; `-r` deletes the home
directory, so against an account this stage did not create it destroys
data the stage never owned. Remove the home directories by hand
afterwards if A0 shows the accounts were absent and you want them gone.

### A3. Create the three Apps *(action 2, DEPLOY — UI)*

GitHub → Settings → Developer settings → GitHub Apps → New GitHub App,
three times. Use `app-manifest-gate.json`, `app-manifest-supervisor.json`
and `app-manifest-worker.json` as the field-by-field source. For each:

- **Webhook:** uncheck *Active*. Subscribe to **zero** events.
- **Where can this app be installed:** *Only on this account*.
- **Permissions:** exactly the manifest's `default_permissions` and
  nothing more.

→ **Verify:** three Apps exist. Record each numeric **App ID** — you need
the gate's for `__GATE_APP_ID__` in step D3.
→ **If failed:** no permission has been granted to anything. Retry.
→ **Undo:** delete the App in its settings page.

**Record, do not assume.** §8 lists four permission questions the App
creation screen answers and this repository could not: whether
`Issues: write` is needed for `gh pr comment`, whether `Contents: write`
is needed alongside `Pull requests: write` for merge, and the same for the
supervisor. Write the screen's answers down as you go.

### A4. Generate and place the private keys *(action 4, DEPLOY)*

Generate one private key per App from its settings page. Place each
outside the repository, outside `~/.config/run-002/secrets.env`, and
outside any directory a worker can traverse:

```
sudo install -d -m 0700 -o run002-sup -g run002 /etc/run-002/keys
sudo install -m 0400 -o run002-sup -g run002 <downloaded.pem> /etc/run-002/keys/<role>.pem
sudo rm -f <downloaded.pem>
```

→ **Verify, WITHOUT READING THE FILE:**
`sudo ls -l /etc/run-002/keys` shows `-r-------- run002-sup run002`.
→ **If failed:** a key readable by the worker UID defeats the whole
boundary. Fix the mode before proceeding.
→ **Undo:** revoke the key **in GitHub first**, then delete the file. A
fresh key does **not** invalidate its predecessor — §11 row 3.

### A5. Re-own the checkout and `.runtime/` *(action 5, DEPLOY)*

Apply §6's asset table, **including the corrected git row**. The row as
originally written (`.git/` read-only for workers) was measured to stop
every worker commit; the corrected row is narrower.

```
sudo chown -R run002-sup:run002 <WS>
sudo chmod -R 0750 <WS>
sudo chmod 0700 <WS>/.runtime
sudo chmod -R 0750 <WS>/.git/hooks
sudo chmod 0640 <WS>/.git/config
# writable by run002-wrk, because a commit cannot happen otherwise:
sudo chmod -R 0770 <WS>/.git/objects <WS>/.git/refs/heads
```

→ **Verify:** V1, V1b, V4b, V5, V5b, V14 (§3 below). Do not proceed on
inspection alone — the measurement that produced this table was taken as
one user with `chmod`, and §6 says in terms that it establishes which
paths git must write, **not** how `run002-wrk` behaves against files
owned by `run002-sup`.
→ **If failed:** DO NOT PROCEED to GitHub. Fix the OS boundary first;
this is runbook step 4's gate.
→ **Undo — from A0's capture, not from memory:**

```
while read -r mode owner group path; do
  sudo chown "$owner:$group" "$path" && sudo chmod "$mode" "$path"
done < ~/run-002-stage1-rollback/ownership-BEFORE.txt
```

This replaces "`sudo chown -R serina:serina <WS>` and restore the
previous modes", which named a restore nobody had the bytes for —
`chmod -R 0750` over a workspace is not invertible from the after-state,
and not every path under `<WS>` was `serina:serina` to begin with.
→ **Verify the undo:** re-run A0's `find` and diff it against
`ownership-BEFORE.txt`; expect no output.

### A6. Decide `origin` SSH → HTTPS *(action 6c, DECISION)*

If **C** is yes, for the worker's worktrees only:

```
sudo -u run002-wrk git -C <worktree> remote set-url origin https://github.com/<PROD>.git
```

→ **Verify:** V6 (§3) now fails as an SSH push and succeeds as an HTTPS
push with the worker's token.
→ **If C is no:** STOP and record the alternative answer to how a worker
pushes. After A5 the worker can read neither `serina`'s SSH key nor her
`gh` config, so "the status quo" is not an available answer.
→ **Undo:** `remote set-url` back.

### A7. Switch the call sites to per-role authentication *(action 6b, DEPLOY — code)*

The code half is **built**: `control/gh.py` now takes `role=` on `run()`
and on every public wrapper, and refuses rather than falling back to
ambient auth. See §4 for the contract and the proof.

What remains is **one line per call site** in `control/routing.py`,
`control/supervisor.py` and `control/cli.py` — each `gh.<fn>(...)` call
gains `role=gh.SUPERVISOR` or `role=gh.WORKER`, whichever principal the
package's §2 derives that permission for.

→ **Verify:** `python3 -m unittest discover -s tests -q` → `OK`, then
`python3 experiment/github-app/check-templates.py` → `OK`, then re-cite
check `D`'s line numbers if any moved.
→ **If failed:** revert the call-site edits. The parameter defaults to
`None`, so an un-switched call site is exactly today's behaviour and the
system still runs.
→ **Undo:** remove the `role=` arguments.

> **THIS STEP IS NOT DONE AND IS NOT MINE TO DO.** `routing.py`,
> `supervisor.py` and `cli.py` are outside this change's ownership. §10
> gap **G1** records it.

### A8. Switch the worker spawn to the allow-list *(action 6, DEPLOY — code)*

`control/worker_entry.py:180` is still `env = dict(os.environ)`.
`worker_child_env` is built and tested beside it, and a test pins the
unwired status so taking this step is a decision rather than a drift.

→ **Verify:** a worker starts, reaches `PROMPT_ACCEPTED`, and
`sudo -u run002-wrk env | grep -c RUN002_` returns `0`.
→ **If failed:** revert the one line; the worker starts as before.
→ **Undo:** restore `dict(os.environ)`.

> **AND THE ONE LINE IS NOT SUFFICIENT.** `GH_TOKEN` is *on* the
> allow-list, because a worker needs its own token. If the Supervisor
> still holds its own `GH_TOKEN` in `os.environ` when the filter is
> switched on, every worker inherits the identity that merges. Action 6
> is the filter **plus** the UID split from A5 **plus** A7's per-role
> authentication, which is what gives the worker a *different* token.
> §10 gap **G2**.

---

## 3. Phase B — the falsification suite on the throwaway

**Every step in this phase is run by a human with `sudo`. None of it
touches `<PROD>`.** The suite is trustworthy only because some of it must
SUCCEED: V2b, V3b, V3c and V14's three worker operations. A suite in
which everything fails proves only that the credentials are broken.

### B1. Install all three Apps on the THROWAWAY first *(action 3a, DEPLOY)*

→ **Verify:** each App's installation page lists `<THROW>` and only
`<THROW>`. Record each **installation ID**.
→ **If failed:** uninstall and retry. Nothing on `<PROD>` has changed.
→ **Undo:** uninstall from `<THROW>`.

### B2. The filesystem half — V1, V1b, V1c, V4, V4b, V5, V5b

| V | Command | Expected |
|---|---|---|
| V1 | `sudo -u run002-wrk test -r /home/serina/.config/gh/hosts.yml; echo $?` | `1` |
| V1b | `sudo -u run002-wrk test -r /home/serina/.ssh/id_ed25519; echo $?` | `1` |
| V1c | `sudo -u run002-wrk test -r /etc/run-002/keys/gate.pem; echo $?` | `1` |
| V4 | `sudo -u run002-wrk touch <EXPORT>/apparatus/pr-evidence/live-gate.js` | `Permission denied` |
| V4b | `sudo -u run002-wrk sh -c 'echo x >> <WS>/apparatus/pr-evidence/live-gate.js'` | `Permission denied` |
| V5 | `sudo -u run002-wrk sh -c 'echo x >> <WS>/.runtime/ledger.jsonl'` | `Permission denied` |
| V5b | `sudo -u run002-wrk test -r <WS>/.runtime/state.json; echo $?` | `1` |

**V1, V1b and V1c use `test -r`, never `cat`.** The proposal says so in
terms: *"the command must be `test -r`, not `cat`, so no value can reach
a transcript even on unexpected success."*

→ **If any of these SUCCEEDS where it must fail:** **STOP.** The design
is falsified at the OS boundary. Do not proceed to GitHub. This is
runbook step 4.
→ **Undo:** nothing was changed; these are read-only assertions.

**V4 cannot run until Phase C has created the export.** Run it there and
come back; it is listed here because it belongs to the filesystem half.

### B3. The credential half — V2, V2b, V3, V3b, V3c, V7, V8, V9, V11

| V | Command | Expected | Must |
|---|---|---|---|
| V2 | `sudo -u run002-wrk env GH_CONFIG_DIR=/home/serina/.config/gh gh auth status >/dev/null 2>&1; echo $?` | non-zero | FAIL |
| V2b | `sudo -u run002-wrk gh auth status >/dev/null 2>&1; echo $?` (worker's OWN `GH_CONFIG_DIR`) | `0` | **SUCCEED** |
| V3 | as `run002-wrk`: `gh api -X POST repos/<THROW>/statuses/<sha> -f state=success -f context=run-002/independent-review` | **403** | FAIL |
| V3b | as `run002-sup` with the **gate** token, same POST | **201** | **SUCCEED** |
| V3c | with the ambient `serina-mcfall` token, post the same context, then `GET /repos/<THROW>/commits/<sha>/status` and the PR's `mergeStateStatus` | the status appears, the pinned required check is **still unsatisfied** | **SUCCEED** (and the check stays unmet) |
| V7 | as `run002-wrk`: `gh api -X PUT repos/<THROW>/pulls/1/merge` | **403** | FAIL |
| V8 | as `run002-sup` with the **gate** token: `gh api -X PUT repos/<THROW>/pulls/1/merge` | **403** | FAIL |
| V9 | enable `enforce_admins: true` on `<THROW>`, then attempt an admin merge with a required context unsatisfied | refused | FAIL |
| V11 | as `run002-wrk` with the worker token: `gh pr view <n> --repo <THROW>` — **no `--json`**, exactly as the frozen prompts issue it | **exit 0**, with whatever degradation the missing `Checks` permission causes, RECORDED | SUCCEED |

**V2 and V2b assert on exit codes only.** Never print `gh auth status`'s
token line.

**V8 is the one that makes the whole arrangement falsifiable.** If the
gate App can merge, the property "the principal that publishes the verdict
cannot act on it" is false and the three-principal design has bought
nothing.

**V11 has a governance consequence either way.** If it errors, either the
worker App needs `Checks: read` after all, or three frozen prompts need
amending — and a frozen-prompt amendment is its own governance act.

→ **If any permission boundary is wrong:** **STOP** and fix the App
definition. This is runbook step 8.
→ **Undo:** V9's `enforce_admins` — set it back to `false` on `<THROW>`.
V3/V3b/V3c leave commit statuses on a throwaway commit; they are
harmless and the repository is deleted at the end.

### B4. Reproduce the F5 deadlock deliberately — V10

Apply the AFTER payload to `<THROW>`'s default branch, with
`__GATE_APP_ID__` replaced by the gate App's real ID. Open a PR, let `ci`
pass, **do not post** `run-002/independent-review`, and observe.

→ **Verify:** `mergeStateStatus` is `BLOCKED`.
→ **If it is not BLOCKED:** the required context is not actually
required. Re-check the protection payload before trusting anything that
depends on it.
→ **Undo:** `PUT` the prior payload back, or
`gh api -X DELETE repos/<THROW>/branches/<default>/protection`.

### B5. The flag cannot tell its own missing context from another rule — V12

On `<THROW>`, add a **second** required context that nothing will ever
satisfy, alongside `ci` and `run-002/independent-review`. Let `ci` pass,
leave both other contexts unposted, and run the gate.

→ **Verify, BOTH HALVES:** the gate reports
`blockedOnlyByPendingIndependentReview: true`, **and the merge stays
blocked**.
→ **Why this matters:** GitHub collapses every unsatisfied protection
rule into one `BLOCKED`, and `live-gate.js:516-523` raises exactly one
reason code for it. The flag therefore cannot distinguish its own missing
context from a second required check. The thing that makes that safe is
the second half: **a true flag never produces a merge**, because
`routing.evaluate_merge` denies on `BLOCKED` without ever reading the
flag. Observing it here is what turns an argument into a measurement.
→ **If the merge goes through:** **STOP.** That is the one outcome the
whole arrangement is built to make impossible.

### B6. Run the gate from a real read-only export — V13

**This is the step that would have caught a launch-day failure, and it
must pass BEFORE action 11.** Create the export exactly as Phase C
specifies, then run the invoker against it for a real commit.

→ **Verify:** a real 40-hex SHA comes back, and the decision's reasons
contain neither `HEAD_SHA_UNVERIFIED` nor `REVIEWER_UNVERIFIED`.
→ **Why:** `git-head.js:163` and `reviewer-identity.js:252` each derive
their root from their own `__dirname`. Run from the export, the first
fails `WORKSPACE_MISMATCH` and the second finds no `.runtime/` — it is
gitignored, so a worktree export has none. Either denies every pull
request forever, and `chmod -R a-w` means neither can be patched in
place.
→ **If either reason appears:** **STOP.** Applying protection on top of a
broken gate turns a bug into a permanent deadlock. The gate program must
call the low-level `resolveTrustedHeadSha(identity, { repoRoot })` and
`loadReviewerEvidence(runtimeDir, prNumber)` with the LIVE root, never the
`resolveRun002*` wrappers.
→ **Undo:** delete the export.

### B7. The corrected git row, under the REAL identities — V14

As `run002-wrk`, after A5, in a worktree **created for this probe**:

**ON A DEDICATED BRANCH, NOT AN EXISTING ONE. CORRECTED 2026-10-02.** This
step used to say "in a worker worktree of the real checkout", and `add -A`
+ `commit` + `push origin HEAD` there would put a probe commit on whichever
real branch that worktree held — eleven of them carry unmerged apparatus
work — and push it to `<PROD>`. A probe must not leave a commit on a branch
somebody is still using, and the rollback must be able to name exactly what
it created.

```
# Created by run002-sup, because that is who creates a worker's worktree
# in the shipping arrangement - then handed over by whichever mechanism
# the deployment chose for real worktrees. `control/worker_git.py`'s "WHO
# MAKES THE CLONE" note records that the code takes no position and cannot
# chown anything itself: it is either a root helper at dispatch, or a
# setgid group-writable root. IF NO MECHANISM HAS BEEN CHOSEN, THIS IS THE
# STEP THAT FORCES THE CHOICE - and V14d is what proves it works.
sudo -u run002-sup git -C <WS> worktree add -b probe/v14 <WS>/.probe-v14
sudo chown -R run002-wrk:run002 <WS>/.probe-v14 <WS>/.git/worktrees/.probe-v14
sudo chmod -R 0700 <WS>/.probe-v14
sudo -u run002-wrk sh -c 'echo probe > <WS>/.probe-v14/V14-PROBE'
sudo -u run002-wrk git -C <WS>/.probe-v14 add V14-PROBE
sudo -u run002-wrk git -C <WS>/.probe-v14 commit -m 'v14 probe'
sudo -u run002-wrk git -C <WS>/.probe-v14 push -u origin probe/v14
sudo -u run002-wrk sh -c 'echo x > <WS>/.git/hooks/pre-commit'
sudo -u run002-wrk cat <WS>/.git/worktrees/gate-<PIN>/HEAD
```

**This pushes one branch to `<PROD>`, the production repository.** It is
the only write Stage 1 makes there. It does not touch `main`, its
protection, or any pull request, and `ci.yml` triggers on `push` only for
`branches: [main]`, so it starts no workflow run. §13's "what it does NOT
touch" row says so explicitly rather than leaving the push unmentioned.

→ **Verify:** the three worker operations **succeed**; both attempts on
the protected paths **fail** with `Permission denied`.
→ **Undo, and it is exactly four things this stage created:**
`git push origin --delete probe/v14` · `git -C <WS> worktree remove
--force <WS>/.probe-v14` · `git -C <WS> branch -D probe/v14` ·
`rm -f <WS>/.git/hooks/pre-commit` **only if A0's capture shows none was
there** — the write above is expected to be refused, but if the mode was
looser than §6 assumes it will have succeeded. Then diff
`git -C <WS> worktree list` and `git -C <WS> for-each-ref refs/heads`
against A0's `worktrees-BEFORE.txt` and `refs-BEFORE.txt` — expect no
difference. **No pre-existing branch, worktree or ref is touched.**
→ **Why the last two:** `hooks/` is code the Supervisor's own git runs,
and the export's gitdir `HEAD` is the proof
`gate_invoker.export_revision()` reads to establish the export is at the
pin. A worker that could write it could claim any revision.
→ **If a worker operation fails:** the ownership in A5 is too tight and
the factory will not run. §6's git row as originally written had exactly
this defect.
→ **If a protected-path attempt succeeds:** the ownership is too loose.
STOP.

### B8. **STOP** *(action 10b, GATE)*

Do not proceed unless **every** V-step produced its expected result —
including the four that must succeed.

---

## 4. Per-role authentication — the contract *(action 6b)*

`COMPONENT` + `SIMULATED`. Code: `control/gh.py`. Tests:
`tests/test_c24_role_auth_and_renewal.py` (34).

### What to set, per process

**One principal per process. One `GH_TOKEN` per process, never both.**

| Role constant | Token variable | Expiry variable |
|---|---|---|
| `gh.GATE` | `RUN_002_GATE_APP_TOKEN` | `RUN_002_GATE_APP_TOKEN_EXPIRES_AT` |
| `gh.SUPERVISOR` | `RUN_002_SUPERVISOR_APP_TOKEN` | `RUN_002_SUPERVISOR_APP_TOKEN_EXPIRES_AT` |
| `gh.WORKER` | `RUN_002_WORKER_APP_TOKEN` | `RUN_002_WORKER_APP_TOKEN_EXPIRES_AT` |

The gate's name is the one `control/publisher.py` and
`status_transport.py` already read, spelled identically so a token
supplied for publication is the same token `gh` authenticates with.

> **The two supervisor/worker names and all three `_EXPIRES_AT` names are
> NOT yet in `env-var-names.md`.** That file's §2 and §3 name only the
> standard `GH_TOKEN`. §10 gap **G3**.

### How a call authenticates

```python
gh.merge(repo, number, role=gh.SUPERVISOR)
gh.create_pr(repo, head, base, title, body, role=gh.WORKER)
```

The child environment is an **allow-list**: `PATH`, `HOME`, `LANG`,
`LC_ALL`, `TZ`, `GH_CONFIG_DIR`, plus `GH_TOKEN` written from that role's
own variable. `GH_TOKEN`, `GITHUB_TOKEN`, `GH_HOST`, `GH_REPO`,
`NODE_PATH` and `NODE_OPTIONS` are absent **by construction**, not by
being named.

`role=None` — the default — passes `env=None` and inherits ambient auth,
byte-for-byte today's behaviour. **No existing caller changes meaning.**

### How it fails

| Refusal | When | Exit code |
|---|---|---|
| `ROLE_UNKNOWN` | not one of the three principals | `125` |
| `ROLE_TOKEN_ABSENT` | the named variable is unset or empty | `125` |
| `ROLE_TOKEN_EXPIRY_UNRECORDED` | a token is present but nothing says when it dies | `125` |
| `ROLE_TOKEN_EXPIRED` | the recorded expiry has passed, or is inside the 60s skew window | `125` |

**None of them falls back to ambient auth, and no process is started.**
The ambient identity holds `statuses: write` *and* merge rights, so a
silent fallback would hand the weakest caller the strongest credential —
the exact hole action 6b exists to close. Proved by mutation: making the
refusal fall back to `env = None` turns six tests red.

### Proving it on the host

→ **Verify, after A7, as each role:**
```
sudo -u run002-sup env RUN_002_SUPERVISOR_APP_TOKEN=<minted> \
  RUN_002_SUPERVISOR_APP_TOKEN_EXPIRES_AT=<iso> \
  python3 -c "from control import gh; print(gh.repo_exists('<THROW>', role=gh.SUPERVISOR))"
```
→ **Expected:** `True`.
→ **If `False` with exit code 125 in the Result:** the credential is
absent or expired — read the reason token, do not retry blind.

> **UNVERIFIED, AND IT IS THE LOAD-BEARING ASSUMPTION OF THIS WHOLE
> SECTION.** That `gh` prefers `GH_TOKEN` over a credential already
> stored in `GH_CONFIG_DIR` is GitHub's documented behaviour and is **not
> asserted anywhere in this repository**. V2/V2b are the closest existing
> probes; neither tests precedence. §10 gap **G4** adds the probe.

---

## 5. Token renewal — the minter contract *(action 9 support)*

`COMPONENT` + `SIMULATED`. Code: `control/gh.py::ensure_token`.

An installation token lasts **one hour**. A Run 002 cycle lasts
twenty-four. The token in the environment is therefore replaced many
times, and nothing in this repository can mint one.

### The injected edge

```python
gh.ensure_token(gh.GATE, mint=my_minter)
```

`mint(role)` is **REQUIRED and UNDEFAULTED**, exactly as `poster` and
`http` are elsewhere. `control/gh.py` imports no `urllib`, no `httpx`, no
`socket`, no crypto library, and a test asserts each absence. **The
minter is the operator's script and lives outside this repository.**

It must return GitHub's own response shape, or a `(token, expires_at)`
pair:

```json
{"token": "...", "expires_at": "2026-10-02T13:00:00Z"}
```

That is the body of
`POST /app/installations/{installation_id}/access_tokens`, authenticated
with a short-lived JWT signed by the App's private key. **The minter is
the only thing in the arrangement that reads a private key**, which is
why it runs as `run002-sup` and never inside a worker.

### What `ensure_token` does with it

| Situation | Outcome | Effect |
|---|---|---|
| more than 5 min + 60 s skew of life left | `TOKEN_FRESH` | **nothing is minted** |
| inside that margin | `TOKEN_RENEWED` | token and expiry written to the environment |
| the minter raises | `TOKEN_MINT_FAILED` | **the old credential is left exactly as it was** |
| the answer is not token+expiry, or the expiry is naive | `TOKEN_MINT_MALFORMED` | nothing written |
| the minted token dies inside the margin | `TOKEN_MINT_EXPIRES_TOO_SOON` | nothing written |
| an unrecognised role | `ROLE_UNKNOWN` | nothing minted |

**The token lands in the environment and nowhere else.** It is never
returned, logged, or stored on the `Renewal` — that dataclass has three
fields and none of them can hold a credential.

**The four hazards, each handled and each mutation-proven:**

- **A renewal that fails** does not destroy a token with minutes left.
  Mutation: `TOKEN_MINT_FAILED` clearing the variable → red.
- **A token that expires mid-operation** cannot be used: `run(role=...)`
  refuses a token past its recorded expiry *before* starting a process.
- **Clock skew** of ±60 s is applied to every horizon, always erring
  early. Mutation: removing the skew term → red.
- **Concurrent renewal** is one lock per role with the freshness check
  re-made *inside* it. Mutation: moving the check outside → 8 mints for
  one role, red.

### The loop

→ **Call `ensure_token` on a timer, or immediately before each `gh`
call**, in each of the three processes, with that process's own role.

> **NOTHING CALLS IT.** There is no renewal daemon and no tick hook. The
> Supervisor's loop is where it belongs and `control/supervisor.py` is
> outside this change's ownership. §10 gap **G5**.

---

## 6. Phase C — the trusted export *(actions 7, 7b, 7c)*

**The order inside this phase is the whole point of the phase.**

### C1. Create the export *(action 7)*

```
sudo install -d -m 0755 -o run002-sup -g run002 /opt/run-002
sudo -u run002-sup git -C <WS> worktree add --detach <EXPORT> <PIN>
```

→ **Verify:** `cat <EXPORT>/.git` is a single `gitdir:` line, and the
file it points at has a `HEAD` holding `<PIN>` as 40 hex — **not** a
`ref:` line. `gate_invoker.export_revision()` refuses a symbolic `HEAD`
outright: a branch moves, and an export pinned to a branch is not pinned.
→ **If `HEAD` is symbolic:** you omitted `--detach`. Remove and redo.
→ **Undo:** `sudo -u run002-sup git -C <WS> worktree remove --force <EXPORT>`.

### C2. Install the runtime dependencies — **BEFORE anything is frozen** *(action 7b)*

```
cd <EXPORT>/apparatus && sudo -u run002-sup npm ci --omit=dev
```

→ **Verify:** `ls <EXPORT>/apparatus/node_modules/ajv/dist/2020.js`
exists, and `ls <EXPORT>/apparatus/node_modules | grep -c playwright`
returns `0`.
→ **If `ajv` is absent:** the gate crashes at module load with
`Cannot find module 'ajv/dist/2020'` — `validate.js:49` requires `ajv` at
module load and `live-gate.js:40` requires `validate.js` at its own. It
fails closed, so it is not a safety hole; it is a 100% outage.
→ **If playwright appears:** `--omit=dev` was dropped, and a browser
download has happened inside the export.
→ **DO NOT** symlink or bind-mount the live `apparatus/node_modules` into
the export. That reintroduces a mutable, worker-reachable dependency tree
as the code the gate executes — the exact property the export exists to
remove.
→ **Undo:** `sudo rm -rf <EXPORT>/apparatus/node_modules` and re-run.

**Installing does not disturb the pin.** The revision check reads `HEAD`,
not a tree hash.

### C3. Freeze it — **only now** *(action 7c)*

```
sudo chown -R run002-sup:run002 <EXPORT>
sudo chmod -R a-w <EXPORT>
sudo chmod 0700 <WS>/.git/worktrees/gate-<PIN>
sudo chown -R run002-sup:run002 <WS>/.git/worktrees/gate-<PIN>
```

→ **Verify:** V4 — `sudo -u run002-wrk touch
<EXPORT>/apparatus/pr-evidence/live-gate.js` → `Permission denied`. And
V14's last probe — `run002-wrk` cannot read
`<WS>/.git/worktrees/gate-<PIN>/HEAD`.
→ **If a worker can write the export:** the gate is executing code a pull
request could have supplied. STOP.
→ **Undo:** `sudo chmod -R u+w <EXPORT>` then delete it.

### C4. Point the invoker at it

```
export RUN002_TRUSTED_GATE_PATH=<EXPORT>
export RUN002_TRUSTED_GATE_SHA=<PIN>
```

→ **Verify:**
```
python3 -c "from control import gate_invoker as g; print(g.deployment_from_env()[1])"
```
→ **Expected:** `DEPLOYMENT_OK`.

| Refusal | Means |
|---|---|
| `DEPLOYMENT_EXPORT_PATH_UNSET` | `RUN002_TRUSTED_GATE_PATH` unset, empty, or relative |
| `DEPLOYMENT_PIN_UNSET` | `RUN002_TRUSTED_GATE_SHA` is not 40 lowercase hex |
| `DEPLOYMENT_ISOLATION_UNREADABLE` | `config/isolation.json` missing or unparseable in the live checkout |
| `DEPLOYMENT_WORKSPACE_MISMATCH` | the control plane is running from a tree `isolation.json` does not name |

**`DEPLOYMENT_WORKSPACE_MISMATCH` is not a formality.** It re-makes, at
the live checkout, the invariant THE INVOKER TRAP forced the gate program
to drop: the Run-002 wrapper in `git-head.js` compares its own location
to `isolation.json`'s `workspace`, which from the read-only export can
never match, so `gate-cli.js` calls the low-level form instead and the
comparison goes unmade. Made here it is both true and checkable.

→ **Then run V13 (B6).** The gate must come back with a real SHA before
you go near action 11.

---

## 7. Publisher transport — assembling the poster *(action 9)*

`COMPONENT` + `SIMULATED`. Code: `control/publisher.py`,
`experiment/github-app/status_transport.py`,
`experiment/github-app/publication_path.py`.

**All three are built. None is wired, and that is enforced rather than
promised:** no module in `control/` or `bin/` references the publisher; a
repo-wide scan allows exactly one importer of the publisher and zero
importers of the join; and `check-templates.py` check `E2` asserts
nothing in `control/`, `bin/`, `apparatus/` or `experiment/github-app/`
imports the transport.

**That last check is why no module in this repository assembles the
poster.** Writing a Python module that imports `status_transport` would
turn `E2` red, and `E2` is live evidence in the approval package that the
transport is inert. So the assembly is a **hand-run operator step**, and
the friction is deliberate.

### C5. Switch publication on

```
export RUN_002_PUBLISH_INDEPENDENT_REVIEW=1
export RUN_002_GATE_APP_TOKEN=<the gate installation token>
export RUN_002_GATE_APP_TOKEN_EXPIRES_AT=<its expires_at>
```

Exactly `"1"`. Not `"true"`, not `"yes"` — a permissive parse is how a
stray value turns a disabled publisher on.

→ **Verify:**
`python3 -c "from control import publisher as p; print(p.enabled(), p.credential_present())"`
→ **Expected:** `True True`.
→ **Undo:** `unset RUN_002_PUBLISH_INDEPENDENT_REVIEW`. That single
variable disables publication everywhere; it is checked before the gate
is even invoked, so a disabled path starts no subprocess either.

### C6. Supply the two external edges and publish

The operator loads the two hyphen-directory modules by explicit file path
and hands them an HTTP client. **This repository contains no HTTP
client**, and `status_transport.send` requires `http` with no default.

```python
import importlib.util, os, pathlib, urllib.error, urllib.request
from control import gate_invoker, publisher

def load(name, rel):
    spec = importlib.util.spec_from_file_location(name, pathlib.Path(rel))
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    return mod

transport = load("t", "experiment/github-app/status_transport.py")
join      = load("j", "experiment/github-app/publication_path.py")

class Response:
    def __init__(self, status): self.status = status

def http(method, url, headers, body):          # THE EXTERNAL EDGE
    req = urllib.request.Request(url, method=method,
                                 data=body.encode(), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return Response(r.status)
    except urllib.error.HTTPError as e:
        return Response(e.code)

# `enabled` is a CALLABLE, not a boolean, so it is re-read per call.
# The definition of "on" belongs to control/publisher.py and is injected
# rather than restated here; two definitions are how two switches drift.
poster = transport.poster_for(
    "<THROW>", http=http, environ=os.environ, enabled=publisher.enabled)

deployment, reason = gate_invoker.deployment_from_env()
assert reason == gate_invoker.DEPLOY_OK, reason

attempt = join.publish_independent_review(
    gate_request, head_sha,
    export_root=deployment.export_root,
    expected_revision=deployment.expected_revision,
    live_repo_root=deployment.live_repo_root,
    poster=poster)
print(attempt.as_dict())
```

→ **Verify:** `attempt.as_dict()["reason"] == "PUBLISHED"` and
`state == "success"`, and `GET /repos/<THROW>/commits/<sha>/status` shows
`run-002/independent-review`.
→ **If `reason` is `PUBLISHER_DISABLED`:** C5 was not applied to *this*
process.
→ **If `GATE_UNUSABLE`:** read `gate_outcome`. `GATE_EXPORT_UNTRUSTED`
means the export is not at `<PIN>`; `GATE_EXPORT_MISSING` means
`gate-cli.js` is absent; `GATE_LIVE_ROOT_INVALID` means you pointed the
gate at the export instead of the checkout.
→ **If `DECISION_HEAD_MISMATCH`:** the gate judged a different commit
than the one you asked to publish for. **Do not override this.** A gate
that judged commit X cannot bless commit Y.
→ **If `STATUS_POST_FAILED`:** a 403 means the credential lacks
`Commit statuses: write`; a 422 is the documented answer to exceeding
1000 statuses for one `(sha, context)`. The transport reports "not
posted" and does not guess which.
→ **Undo:** a commit status cannot be deleted. Post the opposite state
against the same `(sha, context)` — the latest wins — or, if the context
is already required on a protected branch, remove the requirement via the
rollback payload from D2.

> **The `http` above is an EXAMPLE, not shipped code.** It is written out
> here so the contract is unambiguous; writing it into the repository
> would put the only untested code in the chain at exactly the point
> where a mistake posts a false pass against a real commit. §10 gap
> **G6**.

---

## 8. Phase D — production *(actions 10c, 11a, 11, 12)*

**Nothing in this phase may start until B8 has passed.**

### D1. Install the three Apps on `<PROD>` *(action 10c, DEPLOY)*

→ **Verify:** each installation lists `<PROD>` and only `<PROD>`.
Record the three production installation IDs — they differ from the
throwaway's.
→ **Undo:** uninstall.

### D2. Capture the live protection — **this is the rollback artefact** *(action 11a, GATE)*

```
gh api repos/<PROD>/branches/main/protection \
  > experiment/github-app/branch-protection-ROLLBACK-$(date +%Y%m%dT%H%M%SZ).json
```

→ **Verify:** the file is non-empty and parses as JSON.
→ **If it 404s:** `main` has no protection at all, and the "restore the
previous state" path becomes `DELETE .../protection`. Record that fact in
the file's place; do not leave the question open.
→ **These bytes win over `branch-protection-BEFORE.json`**, which is a
2026-10-01 GET and is the FALLBACK, not the artefact.

### D3. Apply the AFTER payload *(action 11, DECISION + DEPLOY)*

Substitute the gate App's real numeric ID for `__GATE_APP_ID__` first —
`check-templates.py` check `B` asserts the placeholder is still unresolved
in the template, so **edit a copy, never the template**.

```
jq --argjson gate <GATE_APP_ID> \
  '.body | (.required_status_checks.checks[] | select(.app_id=="__GATE_APP_ID__") | .app_id) |= $gate' \
  experiment/github-app/branch-protection-AFTER.json > /tmp/after.json
gh api -X PUT repos/<PROD>/branches/main/protection --input /tmp/after.json
```

**This is the hinge.** It takes `main` from "a human approved this" to
"the gate approved this": `required_approving_review_count` 1 → 0, and
`enforce_admins` false → **true**. **The cost, stated:** you can no
longer merge `main` by hand if the gate stalls.

→ **Verify:** D4, both halves.
→ **If the PUT errors:** a protection PUT replaces the **whole** object,
so a rejected payload leaves the old one in place. Read the error; the
two traps the payload avoids are sending the deprecated `contexts` alias
alongside `checks`, and sending `required_pull_request_reviews` as
`null`.
→ **Undo:** `gh api -X PUT repos/<PROD>/branches/main/protection --input
<the file from D2>`.

### D4. Confirm, twice, two different ways *(action 12, GATE)*

```
bin/ctl preflight
gh api repos/<PROD>/branches/main/protection | jq '.required_status_checks.checks, .required_pull_request_reviews'
```

→ **Verify (1):** `github_main_protection` still **PASSes**.
→ **Verify (2), by eye:** `run-002/independent-review` appears in the
checks, and `required_approving_review_count` is `0`. **The preflight
gate checks neither of those**, which is why this is two commands and not
one.
→ **If `github_main_protection` FAILS:** the likely cause is named in §8
and is the one unverified claim that decides whether `ctl start` works at
all — **whether GitHub's GET still returns a
`required_pull_request_reviews` OBJECT after a PUT with count 0.**
`control/preflight.py:174` computes `reviews is not None`. If the GET
echoes `null`, the REQUIRED gate fails and `ctl start` refuses. Check
this here, before assuming the arrangement is live.
→ **If it fails:** roll back with D2's bytes. Do not force `ctl start`.

---

## 9. Completion

You are done when **all** of these are observably true:

- [ ] Three Apps exist, installed on `<PROD>` only, with §2's permissions
      and zero webhook events.
- [ ] `sudo ls -l /etc/run-002/keys` shows three `0400` files owned by
      `run002-sup`, and V1c returns `1` for each.
- [ ] Every V-step in B2, B3, B4, B5, B6 and B7 produced its expected
      result, including the four that must succeed.
- [ ] `<EXPORT>` exists at `<PIN>`, contains `node_modules/ajv`, contains
      no playwright, and is unwritable by `run002-wrk`.
- [ ] `deployment_from_env()` returns `DEPLOYMENT_OK` in the Supervisor's
      process.
- [ ] A role-bound `gh` call succeeds as each of the three principals, and
      V8 confirms the gate principal still cannot merge.
- [ ] `ensure_token` is being called on a schedule in each process, and
      each `*_EXPIRES_AT` is in the future.
- [ ] `bin/ctl preflight` passes `github_main_protection`, and the
      by-eye check in D4 agrees.
- [ ] One real pull request has gone end to end — §10.

---

## 10. The test-PR lifecycle walkthrough — **and the named gaps**

**Walked step by step against the procedure above, on the throwaway.
NONE OF IT HAS BEEN EXECUTED.** The point of the walk is to find where
the procedure does not yet say what to do. Twelve places were found.

| # | Lifecycle step | Does the procedure say what to do? |
|---|---|---|
| 1 | worker creates a branch and commits | **Yes** — A5, A6, V14 |
| 2 | worker pushes | **Partly** — see G7 |
| 3 | worker opens the PR | **Partly** — see G1 |
| 4 | CI runs and concludes `ci` | **Yes** — nothing to do |
| 5 | something assembles a gate request | **NO** — G8, the largest gap |
| 6 | the gate evaluates it | **Yes** — C4, C6, V13 |
| 7 | the independent-review status is published | **Partly** — G6, hand-run only |
| 8 | the supervisor merges | **Partly** — G9, no expected-head |
| 9 | the merge is confirmed | **Yes** — `gh.pr_view(...).mergeCommit` |

### The gaps, named

**G1 — no call site asks for a role.** `control/gh.py` takes `role=`, and
nothing passes it. Until `routing.py`, `supervisor.py` and `cli.py` are
edited (A7), every `gh` call in the running system still uses ambient
auth and §1's separation remains a property of the manifests. *Owner: the
integration owner. `routing.py`/`supervisor.py` are outside this change.*

**G2 — the worker spawn still passes `dict(os.environ)`.**
`worker_entry.py:180`. The filter is built and tested beside it; a test
pins it unwired. *Owner: the integration owner.*

**G3 — `env-var-names.md` does not name five of the six variables
per-role auth uses.** `RUN_002_SUPERVISOR_APP_TOKEN`,
`RUN_002_WORKER_APP_TOKEN` and all three `*_EXPIRES_AT` names are new.
Names only; no value may ever be written there.

**G4 — nothing proves `GH_TOKEN` beats a configured `GH_CONFIG_DIR`
credential.** The entire per-role design assumes it. Add a V-step:
*as `run002-sup`, with a `GH_CONFIG_DIR` holding the supervisor
credential and `GH_TOKEN` holding the **gate** token, attempt
`gh api -X PUT repos/<THROW>/pulls/1/merge` — expect **403**.* If it
returns `200`, the config credential won and per-role auth does not
work at all.

**G5 — nothing calls `ensure_token`.** There is no renewal daemon, no
tick hook, no systemd timer. The procedure says *when* to renew and
*what* a minter must return; it does not say what runs it, because the
loop belongs in `supervisor.py`. **Without this, every token dies one
hour into a twenty-four-hour run.**

**G6 — there is no HTTP client anywhere in this repository, and there
must not be one in `control/`, `bin/` or `experiment/github-app/`.**
Check `E2` asserts that nothing in those directories imports the
transport, and that check is the package's live evidence that F7 is
contained. Publication is therefore a hand-run operator action (C6), not
something the Supervisor can do. **Decide deliberately** whether to keep
it that way for the run, or to re-aim `E2` at a narrower claim — the same
way `E` was re-aimed rather than relaxed.

**TRACKED AS R3 FROM 2026-10-02, and it is not optional.** Phase D makes
`run-002/independent-review` a required context on `main` with
`enforce_admins: true`. From that moment GitHub refuses every merge —
including the Supervisor App's — until a human posts a status for that
exact head. The frozen acceptance property is "no human step". **G1 is
R1 and G5 is R2** in `experiment/RUN-003-BACKLOG.md`; all three are
"NOT deferred — REQUIRED RUN 002 WIRING", and none of them blocks
Stage 1.

**G7 — `gh.git()` has no role, so git operations never carry a token.**
`git push` over HTTPS authenticates through git's credential helper, not
through `GH_TOKEN` on the `gh` child. The procedure does not say to run
`gh auth setup-git` inside each role's `GH_CONFIG_DIR`, and it should,
because after A5 the worker cannot use `serina`'s SSH key. V6 detects the
symptom; nothing yet prescribes the cure.

**G8 — nothing assembles a gate request in production. This is the
largest gap.** `gate-cli.js` needs an envelope whose `request` carries
five fields:

```json
{"liveRepoRoot": "<WS>",
 "request": {"identity": {...}, "prNumber": 7, "pkg": {...},
             "prView": {...}, "checkRuns": [...]}}
```

`checkRuns` must be **the caller's own observed check-run array** — the
gate never fetches one for itself and refuses with
`GATE_CLI_CI_OBSERVATION_MISSING` if it is absent. `prView` is a
`gh pr view --json` observation. `pkg` is the submitted evidence package.
`identity` is the task identity. **The Supervisor's merge path uses
`control/routing.py::evaluate_merge` and never calls `live-gate.js` at
all**, so no code anywhere builds this. Until something does, step 5 of
the lifecycle is manual.

**G9 — the merge carries no expected head.** §5 specifies
`PUT /repos/{owner}/{repo}/pulls/{n}/merge` with `sha` set to the head
the gate judged, so GitHub refuses if the head has moved.
`control/gh.py::merge` runs `gh pr merge --squash --delete-branch` with
no such parameter, and which `gh pr merge` flag carries it could not be
verified — a local hook on this host refuses all `gh pr` invocations,
including `--help`. The REST form is specified precisely because the flag
is unverified. **Nothing implements it.**

**G10 — the "Modify" CI hole is open and the procedure cannot close
it.** A pull request whose own branch redefines `.github/workflows/ci.yml`
still produces a green check run named `ci`, and matching is by name:
`ci-result.js:155-157` is `run.name === required`, with no App id, no
workflow path and no actor. App-ID pinning does **not** fix this — a
modified workflow runs through GitHub Actions and carries `app_id 15368`
legitimately. `control/ci_protected_paths.py` is built and unwired;
wiring it is decision **D**, and enforcement also needs the PR's
changed-file list, which the merge path does not fetch today.

**G11 — another worker's worktree is reachable, and no operator
acceptance exists.** POSIX modes cannot scope write access per ref, so a
worker that can create `run-002/<its task>` can also move another task's
branch. Narrowed by the gate anchoring on the head GitHub reports —
a locally moved ref produces `HEAD_SHA_UNVERIFIED` or `PR_HEAD_MOVED` —
but not closed. The procedure has no step for it because there is none.

**G12 — V11's answer changes the procedure and is unknown.** If
`gh pr view` without `--json` errors for the worker App, either the
worker needs `Checks: read` (changing §2's table, which `check-templates`
check `C` asserts) or three frozen prompts need amending. Both are
governance acts. **Run V11 before anything depends on either answer.**

---

## 11. Recovery

| If | Then |
|---|---|
| **protection is wrong** | `PUT` **the bytes captured at D2**. `branch-protection-BEFORE.json` is a 2026-10-01 GET and is the FALLBACK, not the artefact |
| **the gate denies everything** | `unset RUN_002_PUBLISH_INDEPENDENT_REVIEW`, then remove the required context from protection using D2's bytes. **In that order** — removing the requirement first leaves a publisher live against an unprotected branch |
| **the gate cannot load (`Cannot find module`)** | C2 was skipped or run after C3. `chmod -R u+w <EXPORT>`, run `npm ci --omit=dev`, re-freeze. Do not patch the export in place for anything else |
| **V13 fails with `HEAD_SHA_UNVERIFIED` or `REVIEWER_UNVERIFIED`** | the gate program is using a `resolveRun002*` wrapper. **Do not apply protection.** Fix `gate-cli.js`, re-export, re-run V13 |
| **a credential is suspected** | revoke the App's private key in GitHub settings **BEFORE** issuing a new one — a fresh key does not invalidate the old one. Then re-mint every installation token, because the old ones stay valid for their full hour |
| **a token leaked into a transcript** | say so immediately and lead with it; state what leaked and how long it stays valid; revoke server-side **before** re-authenticating, because a global sign-out also kills any token just issued |
| **a worker cannot commit after A5** | the git row is too tight. `.git/objects/` and `.git/refs/heads/` must be writable by `run002-wrk`; only `hooks/`, `config` and the export's gitdir may be withheld. Measured, not reasoned |
| **the whole arrangement is wrong** | uninstall all three Apps, apply D2's bytes, delete the export, restore ownership and modes **from A0's `ownership-BEFORE.txt`** (not from memory), remove the V14 probe branch and worktree, and delete only the identities A0 recorded as ABSENT. No product data is involved at any point |

**No step in this procedure is irreversible**, and nothing here touches
Run 001.

---

## 12. What could not be done locally, and why

| | Why |
|---|---|
| Any `GITHUB`-classified verification | no App, no installation, no credential, no network call. Every claim above is `COMPONENT` or `SIMULATED` |
| `gh`'s `GH_TOKEN`-over-config precedence | needs a real `gh` and a real credential — G4 |
| The App creation screen's four permission answers | §8; needs the screen |
| Whether the protection GET echoes `required_pull_request_reviews` as an object after a PUT with count 0 | §8; decides whether `ctl start` works — D4 |
| Whether GitHub honours `app_id` pinning on a required context | V3c |
| Whether a `pull_request` workflow resolves from the head branch or the base | bears on G10; the name-only match records the risk regardless |
| `run002-wrk`'s real behaviour against `run002-sup`-owned files | the local measurement ran as ONE user with `chmod`. V14 is the real test |
