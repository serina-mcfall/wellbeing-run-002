# GitHub App and worker-isolation proposal

Status: **PROPOSAL — DECISION-READY. NOTHING CREATED, NOTHING CHANGED.**
Written 2026-10-01 against `wip/c05-1-persistence` at `1387275`.
Expands handover §41.7, which is a summary. Where this document and §41.7
disagree, the disagreement is marked and the evidence is cited.

No App was created. No credential was created, read or printed. No
protection, permission, setting, account or OS user was changed. The only
GitHub calls made while writing this were read-only `gh api` GETs against
already-authenticated ambient credentials, listed in §0.2.

---

## 0. Summary for the operator

### 0.1 The headline: §41.7's permission set is not sufficient

§41.7 states the App needs **"Commit statuses read/write; Contents read;
Pull requests read; nothing else."** Cross-checked against what
`apparatus/pr-evidence/live-gate.js`, the four adapters in
`apparatus/adapters/`, and the Python call sites that drive them actually
invoke, **that set is wrong in both directions**:

| Finding | Severity | Evidence |
|---|---|---|
| **F1. `Checks: read` is missing and the gate cannot function without it.** The CI adapter reads the *check-run* surface, which "Commit statuses" does not grant. | **BLOCKER** | `apparatus/adapters/ci-result.js:69` |
| **F2. There is no principal for the Supervisor's own GitHub writes** — merge, mark-ready, update-branch, comment. The gate App is read-only plus statuses; the worker credential is explicitly denied status access. §41.7 proposes two credentials for a three-principal problem. | **BLOCKER** | `control/gh.py:119,124,134,144`; `control/supervisor.py:1708,4264,4297` |
| **F3. `Administration: read` is missing.** A REQUIRED preflight gate reads branch protection. | **BLOCKER** (blocks `ctl start`) | `control/preflight.py:161`; `control/gh.py:149` |
| **F4. `Actions: read` is missing.** A preflight gate lists workflow runs. | **HIGH** | `control/preflight.py:545`; `control/gh.py:154` |
| **F5. Adding `run-002/independent-review` as a required context deadlocks the publisher.** GitHub reports `BLOCKED` until the status is posted; `live-gate.js` denies on `BLOCKED` and requires `CLEAN`; so the gate can never say ELIGIBLE, so the status is never posted. | **BLOCKER — design circularity not recorded anywhere** | `apparatus/pr-evidence/live-gate.js:516-536,544-554` |
| **F6. `Contents: read` is not needed by any adapter**, and the *real* Contents-write credential today is an SSH key `§41.7` never mentions. `origin` is `git@github.com:…`. | **HIGH — the credential boundary as drafted governs the wrong credential** | `git remote -v`; `ls -ld ~/.ssh` |
| **F7. Nothing in the repository posts a commit status, and `live-gate.js` has no production caller.** The App's `statuses: write` is for a publisher that does not exist. | **HIGH — scope honesty** | grep for `statuses/` returns nothing outside this document; only `apparatus/fixture-preflight/production-gate.js:53` requires `live-gate.js` |

§41.7 is **correct** on: the target repo and branch; `main` being
`4eeaa7c`; neither `live-gate.js` nor `control/accessibility_registry.py`
existing on `main`; every current protection value; the ledger being mode
644; and four of its five OS-isolation claims. Those confirmations are in
§3.1 and §4.1.

### 0.2 Read-only verification performed while writing this

All against already-present ambient auth; no new authentication, no
mutation, no credential value read or printed.

```
gh auth status                                                   # identity + scopes only
gh api repos/serina-mcfall/wellbeing-run-002/branches/main/protection
gh api repos/serina-mcfall/wellbeing-run-002                      # visibility/permissions
gh api repos/serina-mcfall/wellbeing-run-002/rulesets             # -> []
gh api repos/serina-mcfall/wellbeing-run-002/commits/main         # -> sha
gh api repos/.../commits/<sha>/check-runs?per_page=100
gh api repos/.../commits/<sha>/status
ls -ld ~/.ssh ~/.config/gh ; ls -l ~/.config/gh                   # names and modes ONLY
```

The ambient identity is `serina-mcfall`, token type `gho_`, scopes `gist,
project, read:org, repo, workflow`, with `admin: true` on the target
repository. No token value was displayed beyond `gh`'s own masking.

### 0.3 Recommendation

**Do not provision the App as §41.7 specifies it.** Three of its blockers
(F1, F2, F5) mean the resulting system would be strictly worse than today:
a merge gate wired to a credential that cannot read CI, a Supervisor that
cannot merge, and a required context nothing can ever satisfy — all three
failing closed, which is safe, but a permanently closed gate at T+00 is an
outage, not a control.

The corrected design in §1–§4 is executable. Whether to execute it is the
operator's decision, and §6 is ordered so that the OS boundary (§3) can be
built and falsified **before** any GitHub object is created.

---

## 1. The App definition

### 1.1 Identity and scope

| Field | Value | Note |
|---|---|---|
| App name | `run-002-independent-review` | Must be globally unique on GitHub. Reserve at creation time. |
| Homepage URL | `https://github.com/serina-mcfall/wellbeing-run-002` | Required field; any valid URL. |
| Webhook | **Disabled** | See §1.3. |
| Owner | `serina-mcfall` (user-owned) | VERIFIED: repo owner, `admin: true`. |
| Installation scope | **Only on `serina-mcfall/wellbeing-run-002`** — "Only select repositories", one repository. | Never "All repositories". Run 001 and every unrelated repo must be out of reach by construction, per `AGENTS.md:6-10`. |
| Where the private key lives | A file readable **only** by the Supervisor OS user (§3). Never in the repo, never in `~/.config/run-002/secrets.env` alongside worker-visible values, never in any process environment a worker inherits. | |
| Installation-token lifetime | 1 hour, minted on demand by the Supervisor. | A leaked installation token therefore expires; a leaked **private key** does not. The key is the asset. |

### 1.2 Permissions — corrected set, with justification per line

Legend: **§41.7** = in the handover's list. **ADDED** = this document adds
it. **REMOVED** = this document removes it.

| Permission | Level | Status | Why it is needed | What breaks without it |
|---|---|---|---|---|
| **Metadata** | Read | implicit | Mandatory for every App; `gh repo view` needs it. | `preflight.gate_remote` (`control/preflight.py:150`) fails. |
| **Checks** | **Read** | **ADDED — F1** | `apparatus/adapters/ci-result.js:69` calls `GET /repos/{repo}/commits/{sha}/check-runs`. The one required check, `ci`, is a **check run**, not a commit status — VERIFIED live (§1.4). | `ghCheckRuns` raises → `resolveCiResult` returns `CI_FETCH_FAILED` / `COULD_NOT_VERIFY` (`ci-result.js:106`) → `live-gate.js:448` denies `CI_UNVERIFIED` on **every** PR, forever. The gate can never open. |
| **Commit statuses** | Read / **Write** | §41.7 | Write: post `run-002/independent-review`. Read: `gh pr view --json statusCheckRollup` (`control/gh.py:64`) merges statuses and check runs. | Without write, no status is ever published and the required context is never satisfied. Without read, `statusCheckRollup` is incomplete and `gh.checks_state` (`control/gh.py:87`) misjudges. |
| **Pull requests** | Read | §41.7 | `gh pr list` / `gh pr view` (`control/gh.py:70,76`) supply `prView` to `live-gate.js:420,491`, and `routing.py:619` takes the single pre-lock observation. | `live-gate.js:493` denies `PR_UNOBSERVED`; `supervisor.route_prs` sees no PRs. |
| **Administration** | **Read** | **ADDED — F3** | `control/gh.py:149` `GET /repos/{repo}/branches/{branch}/protection`, called by the REQUIRED gate `gate_branch_protection` (`control/preflight.py:161`). | `gh.protection` returns `None` → `preflight.py:163` emits `github_main_protection` FAIL → `ctl start` refuses. |
| **Actions** | **Read** | **ADDED — F4** | `control/gh.py:154` `gh run list --json status,conclusion,…`, called from `control/preflight.py:545`. | That preflight gate cannot observe the latest workflow run; it fails or degrades to `NONE/NONE`. |
| **Contents** | Read | **REMOVED — F6** | No adapter needs it. `apparatus/adapters/git-head.js:95,150` resolves the trusted head from the **local** checkout via `git rev-parse`; `task-record.js:22` reads local `config/tasks.json`; `reviewer-identity.js:219,239` reads local `.runtime/`. `origin` is **SSH** (`git@github.com:serina-mcfall/wellbeing-run-002.git`), so git transport does not use this App at all. | Nothing. Keep it **only** if the operator also decides to move `origin` to HTTPS (§1.5), in which case it becomes required for `git fetch`. |

**Total corrected gate-App permission set:** Metadata read (implicit),
**Checks read**, **Commit statuses read+write**, **Pull requests read**,
**Administration read**, **Actions read**. Six, not four, and one of
§41.7's four drops out.

### 1.3 Events subscribed: **none**

Subscribe to **zero** webhook events; leave the webhook disabled.

Every fact the gate consumes is **pulled** at decision time against the
trusted head: `live-gate.js`'s header (lines 11-17) makes the trusted head
SHA the anchor and refuses to take any fact from the submitted package. A
webhook would introduce a push-time fact with its own delivery ordering
and retry semantics, for which no handling code exists. There is no
listener process in this repository and none is proposed.

### 1.4 The F1 cross-check, shown rather than asserted

On `serina-mcfall/wellbeing-run-002` at `main` =
`4eeaa7ce76aa82a0168236cf4e4ae08d9edc2477`, read-only, 2026-10-01:

```
GET /commits/<sha>/check-runs  -> total_count 1
                                  { name: "ci", status: "completed",
                                    conclusion: "success",
                                    app: "github-actions", app_id: 15368 }
GET /commits/<sha>/status      -> { state: "pending", total_count: 0,
                                    statuses: [] }
```

The required check lives **entirely** on the check-run surface. The commit
has **zero** commit statuses. "Commit statuses: read" therefore returns an
empty set where `ci-result.js` expects `ci`, and the adapter's own
fail-closed behaviour turns that into a permanent denial. This single
observation is the clearest demonstration that §41.7's list was written
against the protection UI's vocabulary rather than against the code.

### 1.5 Two open questions this section cannot settle

1. **Does the gate App hold the write permissions too, or is there a third
   credential?** See §2.2. This document recommends a third principal, but
   the operator may prefer to widen the gate App — at the cost of the
   publisher being able to merge what it blessed.
2. **Does `origin` move from SSH to HTTPS?** Today it does not use any
   token (F6). Leaving it on SSH means the strongest write credential in
   the system — Serina's personal SSH key — remains outside the scheme
   entirely, which is a larger hole than the one the App closes.

---

## 2. The worker credential

### 2.1 What §41.7 proposes, and what the code requires of it

§41.7: workers get "a separate credential with `Contents: write` +
`Pull requests: write` and **no** status access."

What workers are actually told to run, from the frozen prompts:

| Prompt | Command | Permission implied |
|---|---|---|
| `prompts/builder.md:60` | `gh pr create --repo … --base … --head …` | Pull requests: write |
| `prompts/reviewer.md:15-16` | `gh pr diff`, `gh pr view` | Pull requests: read |
| `prompts/security.md:21-22` | `gh pr diff`, `gh pr view` | Pull requests: read |
| `prompts/accessibility.md:31-32` | `gh pr diff`, `gh pr view` | Pull requests: read |
| `prompts/observer.md:13` | `gh pr list` | Pull requests: read |
| builder push of its branch | `git push` | Contents: write — **today via SSH, not via any token (F6)** |

`Pull requests: write` subsumes read, so §41.7's worker set **is
sufficient for the prompts as written** — provided the credential is
actually what git and `gh` use, which today it is not.

**Corrected worker credential:** a second GitHub App installation
(`run-002-worker`) on the same single repository, with **Contents: write**
and **Pull requests: write**, and **no Checks, no Commit statuses, no
Administration, no Actions**. Issued per worker as a short-lived
installation token, injected into the worker process explicitly, and
**never** present in the Supervisor's own environment.

### 2.2 The three principals — the gap §41.7 leaves open (F2)

§41.7 names two credentials. The code needs three, because the Supervisor
performs GitHub **writes** that belong to neither:

| Call | Site | Needs |
|---|---|---|
| `gh pr merge --squash --delete-branch` | `control/gh.py:134`, from `control/supervisor.py:4297` | Pull requests: write **and** Contents: write (merge commit on base; branch deletion) |
| `gh pr ready` | `control/gh.py:124`, from `control/supervisor.py:1708` | Pull requests: write |
| `gh pr update-branch` | `control/gh.py:119`, from `control/supervisor.py:4264` | Pull requests: write **and** Contents: write |
| `gh pr comment` | `control/gh.py:144` | Pull requests: write (PR comments are issue comments; "Issues: write" may be required instead — confirm on the App screen) |

Under §41.7's two-credential model, the Supervisor holds the gate App,
which has no write permission at all → **every merge 403s**. The only way
to make it work with two credentials is to give the Supervisor the
*worker* credential, which reunites the publisher and the merger and
destroys the separation the proposal exists to create.

**Proposed third principal — `run-002-supervisor`:** Contents write, Pull
requests write, and **no Commit statuses permission at all**.

### 2.3 Why the gate credential and the worker credential must not be the same principal

Not a stylistic preference — four concrete reasons, each traceable:

1. **Protocol v2 forbids it.** `live-gate.js:1-17` and
   `reviewer-identity.js:5-8` both quote the PR contract: *"Agents cannot
   satisfy independent boxes by self-attestation."* A credential that can
   both produce the work and publish the verdict on it is self-attestation
   regardless of what the code in between does.
2. **It is reachable today.** `control/gh.py:33` runs `gh` with **no
   explicit token** and no `env=` argument, so it inherits ambient host
   auth; `control/worker_entry.py:113` does `env = dict(os.environ)` with
   no scrub before `subprocess.Popen(..., env=env)` at line 130. A worker
   therefore runs with **the same GitHub identity as the Supervisor** —
   verified ambient identity `serina-mcfall`. §37.10 recorded this; it is
   still true at `1387275`.
3. **App-pinning is the only thing that makes the context unforgeable.**
   `required_status_checks.checks[].app_id` makes GitHub ignore a status
   with the right context posted by the wrong app. That protection is
   worth exactly nothing if the thing being judged holds the pinned app's
   key.
4. **Blast radius.** A compromised or merely confused worker holding
   statuses:write can mark its own commit independently reviewed. A worker
   holding only Contents+PR write can at worst open a PR nobody blessed —
   which the gate then refuses.

**The boundary is only real if the key is unreachable, not merely
unreferenced.** See §3: under one UID, "not in the environment" is not
"not reachable".

---

## 3. OS-level isolation

### 3.1 Verifying §41.7's five claims against the actual checkout

§41.7: *"Under one UID a worker can read `~/.config/gh/`, invoke `gh`
itself, use a credential helper, edit the gate code it is authored
against, and write `.runtime/ledger.jsonl` — which is mode 644 and
append-only only by convention."*

| # | Claim | Verdict | Evidence |
|---|---|---|---|
| 1 | Worker can read `~/.config/gh/` | **CONFIRMED** | `ls -l ~/.config/gh` → `hosts.yml` is `-rw-------`, owner `serina`; dir is `drwxr-x--x`. Workers run as `uid=1000(serina)` (`id`). Mode 600 is no defence against the **same** UID. **Contents never read.** Note the corollary: those modes *do* already exclude a different UID — which is precisely why a different UID is the fix. |
| 2 | Worker can invoke `gh` itself | **CONFIRMED** | `/usr/bin/gh` on PATH; `control/workers.py:665` lists `gh` among required binaries; four frozen prompts instruct workers to run `gh` (§2.1). |
| 3 | Worker can use a credential helper | **NOT CONFIRMED — §41.7 overstates this one** | `git config --get-all credential.helper` and the `--global` form both return nothing. No helper is configured. The *equivalent* capability exists via `gh auth token`, but the specific mechanism §41.7 names is absent. |
| 4 | Worker can edit the gate code it is authored against | **CONFIRMED** | Worker worktrees are `<workspace>/.worktrees/<name>` (`config/isolation.json` `worktree_dir: ".worktrees"`), siblings of the main checkout. `apparatus/pr-evidence/live-gate.js` is `-rw-r--r--` owner `serina` in both the worktree and the main checkout. A same-UID worker can edit either, by relative path. |
| 5 | `.runtime/ledger.jsonl` is mode 644, append-only by convention only | **CONFIRMED BY DIRECT OBSERVATION** | `ls -l /home/serina/wellbeing-agent-experiment/agent-run-002/.runtime` → `-rw-r--r-- serina serina … ledger.jsonl`. `control/ledger.py:124` uses `open(self.path, "a")` with no `chmod` anywhere in the module; the mode is the umask default. The lock (`fcntl`) and `fsync` (`ledger.py:129`) protect concurrent appends — they do **not** prevent the owner truncating or rewriting the file. |

**What could not be checked:** whether a GitHub App installation already
exists on the repository (needs App-authenticated API); whether
`~/.config/run-002/secrets.env` would be readable by a future worker UID
(the file does not exist — §41.9); the behaviour of `claude` and `codex`
under a different UID, which has never been exercised here.

### 3.2 Two things §41.7 understates

**3.2a — `.runtime/state.json` is as load-bearing as the ledger, and
§41.7 omits it.** `apparatus/adapters/reviewer-identity.js:239` reads
`state.json` → `doc.prs[n].reviewed_head`, and `resolveReviewerIdentity`
fails `REVIEW_SHA_MISMATCH` or passes on that value alone
(`reviewer-identity.js:178-192`). Observed mode is `-rw-------` (600) —
tighter than the ledger, and equally irrelevant: the owner is the worker.
Any isolation design must cover `state.json`, `ledger.jsonl`, and the
whole of `.runtime/`.

**3.2b — builders and fixers are not merely same-UID, they are
deliberately unsandboxed.** `control/worker_entry.py:44` launches them as
`claude --print --permission-mode bypassPermissions`. The reviewer gets
`codex exec --sandbox read-only` (`worker_entry.py:53`), which is a
partial mitigation inside Codex's own model and not an OS boundary. This
makes §41.7's argument **stronger** than it states: there is no sandbox to
scrub around.

### 3.3 The proposed design

**Recommendation: separate OS users, not containers.**

Rationale, in the spirit of "simple is the requirement, not the
fallback": the assets to protect are all filesystem objects with owners
(`~/.ssh`, `~/.config/gh`, the App private keys, `.runtime/`, the
apparatus source). POSIX ownership answers that directly. A container adds
an image build, a volume-mount map, a `claude`/`codex`/Playwright/Chromium
install inside the image, and tmux-across-namespaces — new machinery whose
failure modes nobody here has exercised, to enforce the same thing
`chown`/`chmod` already enforces.

Three accounts:

| Account | Runs | Owns |
|---|---|---|
| `serina` (existing, uid 1000) | nothing in the run; the human operator | the repository working tree, `~/.ssh`, `~/.config/gh` |
| **`run002-sup`** (new) | the Supervisor, the watchdog, `ctl`, the gate | `.runtime/`, the gate App key, the supervisor App key |
| **`run002-wrk`** (new) | every product worker: builder, fixer, reviewer, security, accessibility, observer | `<workspace>/.worktrees/` only |

Plus one group, **`run002`**, containing both new accounts, for the few
paths that must be shared read-only.

#### What each must and must not reach

| Path / capability | `run002-sup` | `run002-wrk` | Mechanism |
|---|---|---|---|
| `~serina/.ssh`, `~serina/.config/gh` | **no** | **no** | already `0700` / `0600` + `0751` dir, owned by `serina`. Neither new UID can read them. **This is already true today and needs no change** — it only becomes meaningful once workers stop running as `serina`. |
| Gate App private key | read | **no** | `0400`, owner `run002-sup` |
| Supervisor App private key | read | **no** | `0400`, owner `run002-sup` |
| Worker App private key | read (to mint per-worker tokens) | **no** | `0400`, owner `run002-sup`. The worker receives a **minted 1-hour token**, never the key. |
| `.runtime/**` (ledger, state, locks, evidence) | read+write | **no** | `.runtime/` `0700`, owner `run002-sup` |
| Apparatus and control source (`apparatus/`, `control/`, `bin/`, `config/`, `prompts/`, `protocol/`) | read | read | owner `run002-sup`, group `run002`, mode `0750` dirs / `0640` files. **Worker cannot write.** |
| `<workspace>/.worktrees/<its own>` | read+write | read+write | `0700` per worktree, owner `run002-wrk` |
| Another worker's worktree | read+write | read+write (same UID) | **NOT isolated between workers.** Accepted residual risk — §7.R3. |
| `gh` binary | yes | yes | both need it; the *credential*, not the binary, is the boundary |
| `gh` auth state | its own `GH_CONFIG_DIR` under `run002-sup` | its own `GH_CONFIG_DIR` under `run002-wrk`, holding only the minted worker token | `GH_CONFIG_DIR` set explicitly per role; default `~/.config/gh` is empty for both |
| Posting any commit status | yes (gate App token) | **no** (worker App has no Commit statuses permission) | GitHub-side, not OS-side — the two layers must agree |

#### How the gate executes from a trusted revision

Today there is no answer to this, because **there is no production caller
at all**: grep finds `live-gate.js` required only by
`apparatus/fixture-preflight/production-gate.js:53`, a test harness.
§41.11 says so and it remains true at `1387275`.

The existing precedent to follow is accessibility: `control/gate_evidence.py:209`
pins `RUN_JS = config.REPO_ROOT / "apparatus" / "accessibility" / "run.js"`
and `node_command` (`gate_evidence.py:639`) invokes that absolute path —
never a path inside the PR's worktree. **But `REPO_ROOT` is not a trusted
revision**: it is a mutable working tree, writable today by the same UID
as the worker (§3.1 claim 4). Pinning to `REPO_ROOT` is necessary and not
sufficient.

Proposed, smallest thing that works:

1. A **read-only export** of the trusted revision at a fixed path, created
   once by `run002-sup`:
   `git -C <workspace> worktree add --detach /opt/run-002/gate-<SHA> <SHA>`,
   then `chown -R run002-sup:run002 /opt/run-002/gate-<SHA>` and
   `chmod -R a-w /opt/run-002/gate-<SHA>`.
2. The gate invoker uses **only** that path, exactly as `RUN_JS` does.
3. The Supervisor records the gate revision SHA in the ledger alongside
   every decision, so a decision can be attributed to the code that made
   it.
4. The exported revision is **pinned by full 40-hex SHA**, never by branch
   name — a branch moves.

**Note on §41.7's pin:** it names the trusted apparatus revision as
`4ae1488` on `wip/c05-1-persistence`. That branch tip is now `1387275`
(the handover commit itself). The pin is already one commit stale. A pin
must be a SHA the operator states deliberately, and the operator should
state which one.

#### tmux across two UIDs — an engineering cost §41.7 does not mention

Workers are launched by `tmux send-keys` into a pane
(`control/workers.py:152`), on socket name `run-002`
(`config/isolation.json`). tmux sockets live under `/tmp/tmux-<uid>/`, so
`run002-sup` cannot `send-keys` into a server owned by `run002-wrk`
without one of:

- `sudo -u run002-wrk tmux -L run-002 …` with a narrowly scoped sudoers
  rule (recommended — one line, auditable); or
- a shared socket directory with group permissions (weaker: anything that
  can write the socket can inject into any pane); or
- the existing detached fallback at `control/workers.py:159`
  (`subprocess.Popen(["bash", entry, job_path], …)`) wrapped in
  `sudo -u run002-wrk`, accepting the loss of tmux pane management.

This is real work, not configuration. It must be scheduled, not assumed.

---

## 4. Branch protection: current vs proposed

### 4.1 Current state — VERIFIED LIVE 2026-10-01, read-only

`GET /repos/serina-mcfall/wellbeing-run-002/branches/main/protection`:

| Field | Current value |
|---|---|
| `required_status_checks.strict` | `true` |
| `required_status_checks.contexts` | `["ci"]` |
| `required_status_checks.checks` | `[{"context":"ci","app_id":15368}]` |
| `required_pull_request_reviews.required_approving_review_count` | `1` |
| `required_pull_request_reviews.dismiss_stale_reviews` | `false` |
| `required_pull_request_reviews.require_last_push_approval` | `false` |
| `required_pull_request_reviews.require_code_owner_reviews` | `false` |
| `enforce_admins.enabled` | `false` |
| `required_signatures.enabled` | `false` |
| `required_linear_history.enabled` | `false` |
| `allow_force_pushes.enabled` | `false` |
| `allow_deletions.enabled` | `false` |
| `block_creations.enabled` | `false` |
| `required_conversation_resolution.enabled` | `false` |
| `lock_branch.enabled` | `false` |
| `allow_fork_syncing.enabled` | `false` |
| `restrictions` | **absent from the response** — no push restrictions configured |
| Rulesets | `GET /rulesets` → `[]` — classic protection only, nothing layered on top |
| Repository | `visibility: public`, `private: false`, caller `admin: true` |

This matches §37.10 exactly and adds the five fields §37.10 omitted
(`required_signatures`, `block_creations`, `lock_branch`,
`allow_fork_syncing`, `restrictions`). **Nothing is UNVERIFIED in this
table.**

### 4.2 The literal payload

`PUT /repos/serina-mcfall/wellbeing-run-002/branches/main/protection`
**replaces the entire protection object.** Every field omitted is cleared.
The payload below therefore restates every current value, including the
ones that do not change.

```json
{
  "required_status_checks": {
    "strict": true,
    "checks": [
      { "context": "ci", "app_id": 15368 },
      { "context": "run-002/independent-review", "app_id": <GATE_APP_ID> }
    ]
  },
  "enforce_admins": true,
  "required_pull_request_reviews": {
    "dismiss_stale_reviews": false,
    "require_code_owner_reviews": false,
    "required_approving_review_count": 0,
    "bypass_pull_request_allowances": { "users": [], "teams": [], "apps": [] }
  },
  "restrictions": null,
  "required_linear_history": false,
  "allow_force_pushes": false,
  "allow_deletions": false,
  "block_creations": false,
  "required_conversation_resolution": false,
  "lock_branch": false,
  "allow_fork_syncing": false
}
```

`<GATE_APP_ID>` is the numeric App ID, known only after the App is
created. **It is not the installation ID.** It is readable, without
authenticating as the App, from any status the App has posted
(`statuses[].creator`) or from the App's own settings page.

Field-by-field diff:

| Field | Before | After | Note |
|---|---|---|---|
| `required_status_checks.strict` | `true` | `true` | unchanged |
| `…checks` | `ci` @ 15368 | `ci` @ 15368 **+ `run-002/independent-review` @ GATE_APP_ID** | `ci` byte-identical; the new context is app-pinned, which is the only thing making it unforgeable |
| `…contexts` | `["ci"]` | **omitted** | deprecated alias for `checks`. Sending both is an error. The GET will continue to echo a derived `contexts` list. |
| `required_approving_review_count` | `1` | **`0`** | the Supervisor cannot satisfy 1: PR author and authenticated identity are both `serina-mcfall`, and GitHub does not let an author approve their own PR (§37.10) |
| `required_pull_request_reviews` (the object) | present | **present, with count 0** | **Critical, F-level:** it must NOT be sent as `null`. `control/preflight.py:174` computes `ok = bool(reviews is not None and …)`. Deleting the block makes the REQUIRED gate `github_main_protection` FAIL and `ctl start` refuse. Count 0 keeps the object. |
| `dismiss_stale_reviews` | `false` | `false` | unchanged — governs nothing at count 0 |
| `require_last_push_approval` | `false` | **omitted (= false)** | **WITHDRAWN**, per §41.7. With zero required approvals there is no approval to invalidate. Decision E stays enforced by the control plane: `routing.evaluate_merge`'s head comparison and `live-gate.js:401,427,475-485`. |
| `require_code_owner_reviews` | `false` | `false` | unchanged |
| `bypass_pull_request_allowances` | absent | explicitly empty | belt and braces: no user, team or app may bypass |
| `enforce_admins` | `false` | **`true`** | closes §37.10 finding 2. **Operational consequence the operator must accept:** Serina will no longer be able to merge past a stuck gate by hand. The documented escape is `enforce_admins: false` → merge → `true` again, which is two auditable API calls, not a silent bypass. |
| `restrictions` | absent | **`null`** | the PUT schema requires the key; `null` preserves today's no-restrictions state |
| everything else | `false` | `false` | restated so the PUT does not clear them |

### 4.3 Two configuration consequences nobody has written down

**4.3a — `run-002/independent-review` must NOT be added to
`config/experiment.json` → `github.required_checks`.** That list is
currently `["ci"]`. `apparatus/adapters/ci-result.js:155-165` iterates it
and demands each name appear in the **check-run** response.
`run-002/independent-review` is a commit **status**, which never appears
there, so adding it yields `REQUIRED_CHECK_MISSING` / `VERIFIED_FALSE` on
every PR — a permanent, silent deny. `control/preflight.py:173`
(`required_present = all(c in contexts …)`) is satisfied either way, so
nothing would catch the mistake.

**4.3b — F5, the publisher deadlock.** This is the most serious design
problem in §41.7 and it is not recorded anywhere.

Once `run-002/independent-review` is required and unposted, GitHub reports
`mergeStateStatus: BLOCKED`. `live-gate.js:516-528` denies
`PR_BLOCKED_BY_BRANCH_PROTECTION` on exactly that value, and `:529-536`
denies anything that is not `CLEAN`; the final assertion at `:544-554`
requires `reasons.length === 0`. So:

> the status is posted only if the gate says ELIGIBLE
> → the gate says ELIGIBLE only if `mergeStateStatus` is `CLEAN`
> → `mergeStateStatus` is `CLEAN` only once the status is posted.

The gate can never open. Two candidate resolutions, both **code changes
requiring their own authorisation and NOT covered by this proposal**:

- **(i)** Split the decision: the publisher consults an
  evidence-eligibility verdict that excludes step 8 (mergeability), and
  the Supervisor's merge path consults the full verdict. Cleanest
  separation, largest change.
- **(ii)** Extend the existing `blockedOnlyByDraft` pattern
  (`live-gate.js:556,566`) with an analogous
  `blockedOnlyByPendingIndependentReview`, computed from the combined
  status showing `run-002/independent-review` as the only unsatisfied
  required context. **Recommended** — it reuses a pattern the codebase
  already has, for exactly the same class of problem: a condition that
  must be visible and distinguishable rather than collapsed into a generic
  denial.

Either way, **the protection change must not be applied before the chosen
resolution is built and tested**, or every product PR deadlocks at T+00.

---

## 5. Verification plan — the falsification test

Run on a **throwaway repository** (`serina-mcfall/run-002-isolation-probe`
or similar), never on `wellbeing-run-002`. Creating that repository is
itself a human action requiring authorisation (§6 step 2).

Every step below must **FAIL**. A step that succeeds falsifies the design
and the rollout stops there.

| # | Command (run as the stated UID) | Expected failure | Who runs it | Authorisation needed |
|---|---|---|---|---|
| V1 | `sudo -u run002-wrk cat /home/serina/.config/gh/hosts.yml` | `Permission denied` — **and the command must be `test -r`, not `cat`, so no value can reach a transcript even on unexpected success.** Use: `sudo -u run002-wrk test -r /home/serina/.config/gh/hosts.yml; echo $?` → expect `1` | human | sudo; no GitHub auth |
| V1b | `sudo -u run002-wrk test -r /home/serina/.ssh/id_ed25519; echo $?` | `1` | human | sudo |
| V1c | `sudo -u run002-wrk test -r <gate App key path>; echo $?` | `1` | human | sudo |
| V2 | `sudo -u run002-wrk env GH_CONFIG_DIR=/home/serina/.config/gh gh auth status` | non-zero exit; "not logged in" or a permission error. **Never print the token line** — assert on exit code only: `… gh auth status >/dev/null 2>&1; echo $?` → expect non-zero | human | sudo |
| V2b | `sudo -u run002-wrk gh auth status >/dev/null 2>&1; echo $?` (worker's own `GH_CONFIG_DIR`, holding the worker token) | **0** — this one must SUCCEED; the worker is meant to have its own, weaker credential | human | sudo |
| V3 | As `run002-wrk`, with the worker token: `gh api -X POST repos/<throwaway>/statuses/<sha> -f state=success -f context=run-002/independent-review` | **HTTP 403** — the worker App has no Commit statuses permission | human | sudo + the throwaway repo |
| V3b | As `run002-sup`, with the gate App token, same POST | **HTTP 201** — the gate App can post. Confirms the permission split is real in both directions, not just restrictive | human | sudo + throwaway repo |
| V3c | With the **ambient `serina-mcfall` token**, post the same context, then read `GET /commits/<sha>/status` and the PR's `mergeStateStatus` | the status appears, but the pinned required check is **still unsatisfied** — proves `app_id` pinning works and a user token cannot forge the context | human | throwaway repo only |
| V4 | `sudo -u run002-wrk touch /opt/run-002/gate-<SHA>/apparatus/pr-evidence/live-gate.js` | `Permission denied` | human | sudo |
| V4b | `sudo -u run002-wrk sh -c 'echo x >> <workspace>/apparatus/pr-evidence/live-gate.js'` | `Permission denied` (the mutable checkout must also be unwritable to workers) | human | sudo |
| V5 | `sudo -u run002-wrk sh -c 'echo x >> <workspace>/.runtime/ledger.jsonl'` | `Permission denied` | human | sudo |
| V5b | `sudo -u run002-wrk test -r <workspace>/.runtime/state.json; echo $?` | `1` — **§41.7 omits this one; `reviewer-identity.js:239` makes it load-bearing** | human | sudo |
| V6 | `sudo -u run002-wrk git -C <worktree> push origin HEAD` with `origin` on SSH | should **fail** if the SSH key is excluded. If it **succeeds**, F6 is proven live and the SSH key must be addressed before launch | human | sudo; throwaway repo |
| V7 | As `run002-wrk`: `gh api -X PUT repos/<throwaway>/pulls/1/merge` | **HTTP 403** — the worker App cannot merge | human | sudo; throwaway repo |
| V8 | As `run002-sup` with the **gate** App token: `gh api -X PUT repos/<throwaway>/pulls/1/merge` | **HTTP 403** — the publisher must not be the merger (§2.3). Only the supervisor App may merge | human | sudo; throwaway repo |
| V9 | Enable `enforce_admins: true` on the throwaway, then attempt an admin merge with a required context unsatisfied | **refused** — records whether the admin bypass is genuinely closed | human | throwaway repo admin |
| V10 | On the throwaway, with the protection payload applied: open a PR, let `ci` pass, do **not** post the independent-review status, and observe `mergeStateStatus` | **`BLOCKED`** — this is the direct, cheap reproduction of the F5 deadlock, and it must be reproduced *before* any code is written to resolve it | human | throwaway repo |

**A guard nobody has watched fail is not a guard.** V3b, V2b and V3c exist
precisely so that the suite proves the boundary *admits* what it should as
well as refusing what it should — a suite where every step fails proves
only that the credentials are broken.

---

## 6. Execution runbook

Ordered so that everything reversible and local happens before anything is
created on GitHub, and so the F5 deadlock is resolved before it can bite.

| # | Step | Who | Authorisation | Rollback |
|---|---|---|---|---|
| 0 | **Operator decides** on the three open questions: the three-principal model (§2.2), SSH vs HTTPS for `origin` (§1.5), and which SHA is the trusted gate revision (§3.3). | human | — | n/a; nothing done |
| 1 | Create OS users `run002-sup`, `run002-wrk` and group `run002`. Set ownership and modes per §3.3. | human | root | `userdel run002-sup run002-wrk; groupdel run002`; restore ownership to `serina` |
| 2 | Create the throwaway probe repository. | human | GitHub account | delete the repository |
| 3 | Run V1, V1b, V4, V4b, V5, V5b, V6 — the **filesystem** half of §5. No GitHub objects needed. | human | sudo | n/a (read-only assertions) |
| 4 | **Gate: if any of step 3 passes where it must fail, STOP.** Fix the OS boundary; do not proceed to GitHub. | human | — | — |
| 5 | Create the three GitHub Apps (`…-independent-review`, `…-worker`, `…-supervisor`) with the permissions in §1.2 and §2, webhook disabled. Install each on the **throwaway repo only**, to begin with. | human | GitHub account | delete each App (removes its installations and invalidates its tokens) |
| 6 | Store the three private keys `0400` owned by `run002-sup`. | human | sudo | `shred` the key files; regenerate from the App settings page if needed |
| 7 | Run V2, V2b, V3, V3b, V3c, V7, V8, V9 against the throwaway. | human | sudo + throwaway | n/a |
| 8 | **Gate: if any permission boundary is wrong, STOP and fix the App definition.** | human | — | — |
| 9 | Apply the §4.2 payload to the **throwaway's** default branch; run V10 and **reproduce the F5 deadlock deliberately**. | human | throwaway admin | `PUT` the prior payload back, or `DELETE …/protection` |
| 10 | Build the F5 resolution — §4.3b option (ii) recommended — with tests, including a mutation proving the new condition is detected. | agent | a separate, explicit change authorisation; **not granted by this document** | revert the commit |
| 11 | Build the status publisher: the code that turns an ELIGIBLE verdict into `POST /statuses/{sha}`, sourced **only** from the SHA-bound `REVIEW_RESULT` ledger event plus the gate verdict, never from the submitted package (§37.10). **No such code exists today (F7).** | agent | separate change authorisation | revert the commit |
| 12 | Change `control/gh.py:33` to pass an explicit per-role token instead of inheriting ambient auth, and `control/worker_entry.py:113` to construct the child environment from an allow-list rather than `dict(os.environ)`. | agent | separate change authorisation | revert the commit |
| 13 | Re-run the whole §5 suite against the throwaway with the real code paths. | human + agent | sudo + throwaway | n/a |
| 14 | Install the three Apps on `serina-mcfall/wellbeing-run-002`. | human | GitHub account | uninstall each installation |
| 15 | Capture the current protection payload verbatim to a file as the rollback artefact, then apply §4.2 to `main`. | human | repo admin | `PUT` the captured payload back — the exact bytes, from §4.1 |
| 16 | Re-run `ctl preflight`; confirm `github_main_protection` still PASSes with `required_approving_review_count: 0` (§4.2, the `reviews is not None` trap). | agent | existing | step 15's rollback |
| 17 | Delete the throwaway repository. | human | GitHub account | n/a |

Steps 1-4 are independent of every GitHub step and can start immediately
once step 0 is decided. Steps 10-12 are the real engineering cost and are
on the critical path; nothing in §41.10's "shortest path to launch"
accounts for them.

---

## 7. What this proposal does NOT cover, and residual risk

**R1. The F5 resolution is not designed here, only diagnosed.** §4.3b
offers two options and recommends one. Neither is built, neither is
tested, and the recommended one changes the semantics of a gate that
currently defaults deny. That change deserves its own adversarial review.

**R2. The status publisher does not exist.** Nothing in the repository
posts a commit status (F7), and `live-gate.js` has no production caller —
only `apparatus/fixture-preflight/production-gate.js:53`, a test harness.
Every permission in §1.2 marked "post the status" is therefore
provisioning for code that has not been written. The App could be created
today and change nothing.

**R3. Workers are not isolated from each other.** All product workers
share `run002-wrk`, so any worker can read and write any other worker's
worktree. Per-worker UIDs would fix it and would multiply the account and
sudoers surface. **Accepted residual risk**, stated so it is a decision
rather than an oversight.

**R4. The SSH key is outside the scheme.** `origin` is
`git@github.com:…`, so `git fetch`/`push` authenticate with
`~serina/.ssh`, not with any App token (F6). Under §3.3 a worker UID
cannot read that key — but then **builders cannot push at all** until
`origin` moves to HTTPS with the worker token, or a dedicated deploy key
owned by `run002-wrk` is created. V6 is the test that forces this to the
surface. Until it is answered, the worker credential's `Contents: write`
is decorative.

**R5. `codex exec --sandbox read-only` is not re-verified under a
different UID.** Neither `claude --permission-mode bypassPermissions` nor
`codex` has ever been run as `run002-wrk` here. Agent CLIs keep state in
`$HOME`; both will need their own `$HOME`, their own auth, and their own
caches. That cost is not estimated in this document.

**R6. `enforce_admins: true` removes the human escape hatch.** Once set,
Serina cannot merge past a wedged gate without two auditable API calls to
turn it off and back on. If the run wedges at 03:00, that is the
procedure. This is the intended trade and it should be an explicit yes.

**R7. The permission *names* in §1.2 are derived from GitHub's documented
permission model and from what each endpoint operates on, not from a
quoted docs page.** An attempt to confirm `Checks: read` against
`docs.github.com/en/rest/checks/runs` returned no explicit fine-grained
mapping. **The direction is certain and empirically grounded** — check
runs and commit statuses are distinct API surfaces, verified live in §1.4,
and GitHub exposes them as distinct App permissions. The exact level for
`gh pr comment` (Issues vs Pull requests write) and for
`update-branch`/`merge` (whether Contents write is required alongside Pull
requests write) **must be confirmed on the App creation screen at step 5**
and corrected there. Marking them UNVERIFIED is deliberate.

**R8. Whether any GitHub App is already installed on the repository is
UNVERIFIED.** Reading `/repos/{repo}/installation` requires
App-authenticated access, which does not exist. Step 5 may collide with an
existing installation.

**R9. `protocol/SECURITY-THREAT-MODEL-V2.md` does not cover this at
all.** It is frozen and is scoped to the *product's* surfaces — Supabase
auth, RLS, the companion boundary, the OpenRouter key. It contains no
asset, trust boundary or invariant for the **apparatus's own** GitHub
credentials, the ledger, or worker process isolation. Everything in this
document is therefore ungoverned by the frozen threat model. Whether that
gap is closed by amending a frozen document or by a separate apparatus
threat model is a governance decision nobody has taken.

**R10. Stale comment, flagged not fixed.**
`apparatus/adapters/reviewer-identity.js:24-35` states the ledger "does
not bind a review to a SHA". That is no longer true:
`control/supervisor.py:1885` and `:3846` both emit `head_sha` on
`REVIEW_DISPATCHED` and `REVIEW_RESULT`, which is what makes
`live-gate.js:131-198` satisfiable. The code is correct; the comment will
mislead the next reader. Not changed here — this document owns no code.

**R11. None of this has been exercised end to end.** No real accessibility
scan has run, no product exists, `.worktrees/` does not exist yet, no
preflight.json exists, and `T+00` remains `NOT_STARTED`. A boundary design
validated only on a throwaway repository with synthetic PRs is a boundary
design, not a proven boundary.

---

## 8. Provenance

Every claim above is one of:

- **VERIFIED LIVE** — a read-only `gh api` GET made 2026-10-01 (§0.2).
- **VERIFIED IN CHECKOUT** — a `file:line` citation at `1387275`.
- **UNVERIFIED** — explicitly marked, in R7, R8 and §3.1 claim 3.

Where §41.7 and this document disagree, the disagreements are F1-F7, §3.1
claim 3, §3.2a, §3.2b, §3.3's note on the stale pin, §4.3a and §4.3b.
Where §41.7 is confirmed, it is confirmed in §3.1 and §4.1.

**No App, credential, token, key, account, OS user, container or
repository was created. No protection, permission, setting, group or host
user was changed. No authenticated mutating call was made. No secret was
read, printed or pattern-matched. `T+00` remains `NOT_STARTED`.**
