# GitHub App and worker-isolation proposal

Status: **COMPLETE — ONE CONCRETE ARRANGEMENT, AWAITING THE OPERATOR'S
DECISION. NOTHING CREATED, NOTHING CHANGED.**

Written 2026-10-01 against `wip/c05-1-persistence` at `1387275`.
**Completed 2026-10-02 against `wip/c05-1-persistence` at `b66944a`** —
§9 and §10 added, §1.5, §3.3's pin note and §4.3b updated to point at the
decided arrangement. Every §0–§8 finding and verification stands; nothing
earlier was weakened or deleted. Code citations were re-checked at
`b66944a` and all still resolve (§10.2).

Expands handover §41.7, which is a summary and whose permission set is
**SUPERSEDED** by §1.2 and §9.2. Where this document and §41.7 disagree,
the disagreement is marked and the evidence is cited.

**→ The operator-facing answer is §9. Everything before it is the working.**

No App was created. No credential was created, read or printed. No
protection, permission, setting, account or OS user was changed. The only
GitHub calls made while writing this were read-only `gh api` GETs against
already-authenticated ambient credentials, listed in §0.2. **The 2026-10-02
completion pass made no GitHub call of any kind** — it used only local
`git`, local file reads, and the checker in §10.

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

### 0.4 What changed on 2026-10-02 — the arrangement, not a menu

The operator asked for the proposal to be **finished**, presenting **one
concrete arrangement** rather than a set of options. **§9 is that
arrangement**, and §10 indexes the reviewable templates now sitting
unapplied under `experiment/github-app/`.

Three things §9 does that §1–§8 did not:

1. **It decides.** The two questions §43.5(c) says the operator must settle
   are answered with a recommendation and its reasoning (§9.9), **flagged
   as pending the operator's approval** rather than left open.
2. **It pins the trusted revision** (§9.5) with the git evidence for the
   choice, including the explicit demonstration that **`main` does not
   contain the apparatus at all** and therefore cannot host the gate.
3. **It shows F1, F2 and F5 are gone rather than asserting it** — §9.3 is a
   permission-to-call-site cross-check, and
   `experiment/github-app/check-templates.py` re-runs it mechanically
   against the live checkout (§10.2).

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

**→ ANSWERED 2026-10-02 in §9.9, with reasoning, as a RECOMMENDATION
PENDING THE OPERATOR'S APPROVAL.** Both are still the operator's to decide;
§9.9 picks one of each and says why, so there is a single arrangement to
approve or reject rather than a menu to assemble.

**Note on §1.2's allocation.** §1.2 computes the permission set for a world
where the Supervisor holds the gate App — so it loads `Administration:
read` and `Actions: read` onto the gate App. Once the three-principal model
of §2.2 is adopted, those two belong to the **Supervisor** principal, not
the publisher, because the calls that need them
(`control/preflight.py:161,545`) are made by the Supervisor's own preflight
and never by the gate. §9.2 is the corrected per-principal allocation and
**supersedes §1.2's single-column table**. The total set of permissions is
unchanged; only their owner moves, and it moves strictly tighter.

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
   auth; `control/worker_entry.py:180` does `env = dict(os.environ)` with
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
`4ae1488` on `wip/c05-1-persistence`. That branch tip was `1387275` when
this section was written and is `b66944a` as of 2026-10-02 — `4ae1488` is
now **14 commits stale**. A pin must be a SHA the operator states
deliberately. **→ §9.5 states it, with the git evidence, and explains why
`main` is not a candidate.**

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

**→ DECIDED in §9.6: option (ii), with the precise safety condition that
stops it becoming a fail-open.** The ordering invariant — resolution built
and mutation-tested *before* the protection PUT — is §9.6's hard
precondition and is restated in the AFTER template's own header.

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
| **V12** | **ADDED 2026-10-02.** On the throwaway, add a SECOND required context that nothing will ever satisfy, alongside `ci` and `run-002/independent-review`. Let `ci` pass, leave both other contexts unposted, and run the gate | the gate reports `blockedOnlyByPendingIndependentReview` **true** — and the merge **stays blocked**. Both halves must be observed. GitHub collapses every unsatisfied protection rule into one `BLOCKED`, and `live-gate.js:516-523` raises ONE reason code for it, so the flag cannot tell its own missing context from a second rule. This step makes that visible instead of leaving it as an argument, and confirms the thing that actually matters: **a true flag never produces a merge** | human | throwaway repo |
| **V13** | **ADDED 2026-10-02, and it is the one that would have caught a launch-day failure.** Create the read-only export exactly as action 7 specifies (`git worktree add --detach`, `chown`, `chmod -R a-w`), then run the gate invoker against it for a real commit | **a real 40-hex SHA comes back, and the decision is not `HEAD_SHA_UNVERIFIED` or `REVIEWER_UNVERIFIED`.** `git-head.js:163` and `reviewer-identity.js:252` both derive their root from `__dirname`; from the export the first fails `WORKSPACE_MISMATCH` and the second finds no `.runtime/` (it is gitignored, so a worktree export has none). Either one denies every pull request forever, and `chmod -R a-w` means neither can be fixed in place. **Run this BEFORE action 11** — applying protection first turns a broken gate into a permanent deadlock | human | the export; no GitHub call needed for the head half |

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
| 0 | **Operator decides.** §9 is now one concrete arrangement; the two questions that remain are §9.9's, each with a recommendation: the three-principal model, and `origin` SSH → HTTPS for workers. The trusted revision is **no longer** an open question — §9.5 pins it to `2c7c6cf257054872171d6e8f34d6329368a056a6`; the operator confirms it or re-pins to the then-current HEAD. | human | — | n/a; nothing done |
| **0b** | ~~**Push `wip/c05-1-persistence`** so the pinned revision exists on `origin`.~~ **DONE 2026-10-02** — the branch is pushed, and each re-pin is pushed with it; `44e1eb5` is reachable from `origin/wip/c05-1-persistence` and the pin is independently fetchable. **Re-check this after any re-pin** — a pin that only exists locally cannot be exported by anyone else. | done | repo write | n/a — pushing an existing local commit |
| 1 | Create OS users `run002-sup`, `run002-wrk` and group `run002`. Set ownership and modes per §3.3 and §9.4. | human | root | `userdel run002-sup run002-wrk; groupdel run002`; restore ownership to `serina` |
| 2 | Create the throwaway probe repository. | human | GitHub account | delete the repository |
| 3 | Run V1, V1b, V4, V4b, V5, V5b, V6 — the **filesystem** half of §5. No GitHub objects needed. | human | sudo | n/a (read-only assertions) |
| 4 | **Gate: if any of step 3 passes where it must fail, STOP.** Fix the OS boundary; do not proceed to GitHub. | human | — | — |
| 5 | Create the three GitHub Apps from the manifests in `experiment/github-app/` — **§9.2's per-principal permissions, which supersede §1.2's single column** — webhook disabled. Install each on the **throwaway repo only**, to begin with. Confirm the UNVERIFIED mappings (§9.11 item 3) on the creation screen and correct the manifests there. | human | GitHub account | delete each App (removes its installations and invalidates its tokens) |
| 6 | Store the three private keys `0400` owned by `run002-sup`. | human | sudo | `shred` the key files; regenerate from the App settings page if needed |
| 7 | Run V2, V2b, V3, V3b, V3c, V7, V8, V9 **and V11** (§9.10) against the throwaway. | human | sudo + throwaway | n/a |
| 8 | **Gate: if any permission boundary is wrong, STOP and fix the App definition.** | human | — | — |
| 9 | Apply the §4.2 payload to the **throwaway's** default branch; run V10 and **reproduce the F5 deadlock deliberately**. | human | throwaway admin | `PUT` the prior payload back, or `DELETE …/protection` |
| 10 | Build the F5 resolution — §4.3b option (ii) recommended — with tests, including a mutation proving the new condition is detected. | agent | a separate, explicit change authorisation; **not granted by this document** | revert the commit |
| 11 | Build the status publisher: the code that turns an ELIGIBLE verdict into `POST /statuses/{sha}`, sourced **only** from the SHA-bound `REVIEW_RESULT` ledger event plus the gate verdict, never from the submitted package (§37.10). **No such code exists today (F7).** | agent | separate change authorisation | revert the commit |
| 12 | Change `control/gh.py:33` to pass an explicit per-role token instead of inheriting ambient auth, and `control/worker_entry.py:180` to construct the child environment from an allow-list rather than `dict(os.environ)`. | agent | separate change authorisation | revert the commit |
| 13 | Re-run the whole §5 suite against the throwaway with the real code paths. | human + agent | sudo + throwaway | n/a |
| 14 | Install the three Apps on `serina-mcfall/wellbeing-run-002`. | human | GitHub account | uninstall each installation |
| 15 | Capture the current protection payload verbatim to a file as the rollback artefact, then apply `experiment/github-app/branch-protection-AFTER.json` (`jq .body`) to `main`, with `__GATE_APP_ID__` replaced by the gate App's numeric ID. **Only after steps 10 and 11 — see §9.6's ordering invariant.** | human | repo admin | `PUT` the captured bytes back; `experiment/github-app/branch-protection-BEFORE.json` is the fallback if capture failed |
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

The five below were added 2026-10-02 alongside §9. They are consequences of
**deciding**, which §1–§8 did not have to face.

**R12. The publisher/merger split is a GitHub-side split, not an OS-side
one.** §9.2 gives the gate App and the supervisor App to two different
GitHub principals, but §3.3 puts **both private keys under the same OS user
`run002-sup`**. A process running as `run002-sup` can therefore read both
keys and mint both tokens. The split defends against **the worker** — which
is the threat §2.3 is actually about, because the worker is the thing being
judged — and it makes every posted status attributable to one App ID in
GitHub's audit log. It does **not** defend against a compromised
`run002-sup`. A fourth OS user owning only the gate key would close that,
at the cost of another account, another sudoers rule and another `$HOME`.
**Recommended: do not add it now.** Stated here so the separation is not
read as stronger than it is.

**R13. The pinned revision is not on `origin`.** `b66944a` is three commits
ahead of `origin/wip/c05-1-persistence` (`dbee92c`) — evidence in §9.5 —
and all three touch `apparatus/` or `control/`. Until the branch is pushed,
the trusted export can only be created from this local clone and **no third
party can fetch the pin to verify what the gate ran**. §9.5 makes pushing
the branch a precondition of the runbook rather than an afterthought.

**R14. The merge has no expected-head guard, today.**
`control/gh.py:134-136` is `gh pr merge <n> --repo <r> --squash
--delete-branch` and passes **no** expected-head argument. Between the
gate deciding on trusted head `S` and the merge executing, a push can move
the head, and the merge would take the new one. §9.7 specifies the fix;
**the fix is a code change this document does not authorise.** The window
is open now and is not created by anything proposed here.

**R15. Bare `gh pr view` under a Checks-less token is untested.**
`prompts/reviewer.md:16`, `prompts/security.md:22` and
`prompts/accessibility.md:34` run `gh pr view <n> --repo <repo>` with **no**
`--json`. Whether gh's default view requests the check-run surface — and so
whether it errors or silently degrades under the worker App, which has no
`checks` permission by design — has not been established. Verification step
V11 (§9.10) tests it on the throwaway before any worker depends on it.
**UNVERIFIED, and deliberately so: establishing it requires a token, and no
token exists.**

---

## 8. Provenance

Every claim in this document is exactly one of:

- **VERIFIED LIVE** — a read-only `gh api` GET made **2026-10-01** and
  listed in §0.2. Nothing was re-queried on 2026-10-02 and no GitHub call
  of any kind was made in the completion pass.
- **VERIFIED IN CHECKOUT** — a `file:line` citation. §0–§7 were written
  against `1387275`; **every citation §9 relies on was re-read at
  `b66944a`** and `experiment/github-app/check-templates.py` re-asserts
  twelve of them mechanically (§10.2 check `D`).
- **VERIFIED BY LOCAL GIT** — the ref, ancestry and tree-content evidence
  in §9.5, from `git rev-parse`, `git merge-base`, `git rev-list`,
  `git ls-tree` and `git cat-file -e`. Local only; no fetch, no push.
- **UNVERIFIED** — explicitly marked. The complete list is §9.11 items 3–6,
  plus §7 R7, R8, R15 and §3.1 claim 3. Nothing is marked verified that was
  not.

**One citation drifted between `1387275` and `b66944a`, and is corrected
rather than silently restated.** §2.1 cites the accessibility prompt's `gh`
commands at `prompts/accessibility.md:31-32`. That was correct at
`1387275`; commit `7f937a8` ("C-02b: the frozen prompt stops calling its
own dispatch unimplemented") edited the file, and the lines are now
**33-34**. §2.1 is left as written — it was true at its stated revision —
and §9.3 row 13 carries the corrected location. The checker now pins all
eight prompt call sites, so the next drift is reported rather than
inherited. **This is the only citation that moved**; the other eleven
re-checked sites are unchanged.

Where §41.7 and this document disagree, the disagreements are F1–F7, §3.1
claim 3, §3.2a, §3.2b, §3.3's note on the stale pin, §4.3a, §4.3b, and
§9.2's re-allocation of `Administration`/`Actions` read to the Supervisor.
Where §41.7 is confirmed, it is confirmed in §3.1 and §4.1 — including, 54
commits later, that `main` still contains neither `live-gate.js` nor
`control/accessibility_registry.py` (§9.5).

**§41.7's permission set is SUPERSEDED, not offered as an alternative.**

---

## 9. THE CONCRETE ARRANGEMENT

**One arrangement, not a menu.** Everything in §9 is a single coherent
proposal: approve it, reject it, or send back a specific line. It is
**unapplied**. Nothing in it has been created, installed, granted or
changed, and §9 by itself authorises nothing.

Where §9 and §1–§4 differ, §9 wins and the difference is called out.
Where §9 and handover §41.7 differ, **§41.7 is superseded** — §41.7's
permission set is the thing F1–F5 proved wrong, and it must not be read as
an alternative on offer.

### 9.1 Target — exactly what is governed

| | |
|---|---|
| **Repository** | `serina-mcfall/wellbeing-run-002` |
| **Target branch** | `main` |
| **Source of truth for both** | `config/experiment.json` → `github.repo` / `github.main_branch`, loaded at `control/config.py:171`. VERIFIED IN CHECKOUT at `b66944a`. |
| **Installation scope** | "Only select repositories" → **this one repository**, for all three Apps. Never "All repositories". |
| **Everything else** | out of scope. Run 001 and every unrelated repository are out of reach **by construction**, not by policy (`AGENTS.md:6-10`). |

### 9.2 The three principals

This supersedes §1.2's single-column table. §1.2 derived six permissions
for a world where one App did everything the Supervisor does; §9.2 keeps
all six and allocates each to **the principal that actually makes the
call**, which is strictly tighter. The two write permissions §2 derived
(`Contents: write`, `Pull requests: write`) and the `Issues: write` §2.2
flags as uncertain complete the table. **Nothing is granted here that §1.2
or §2 did not already derive from a call site.**

| Permission | **gate**<br>`run-002-independent-review` | **supervisor**<br>`run-002-supervisor` | **worker**<br>`run-002-worker` |
|---|---|---|---|
| Metadata | read | read | read |
| Checks | **read** | read | **— none** |
| Commit statuses | **read + WRITE** | read | **— none** |
| Pull requests | read | **write** | **write** |
| Contents | — none | **write** | **write** |
| Administration | — none | read | — none |
| Actions | — none | read | — none |
| Issues | — none | write *(UNVERIFIED, §7 R7)* | — none |
| Webhook / events | disabled, zero events | disabled, zero events | disabled, zero events |

**Responsibilities, in one line each.**

- **gate / publisher** — runs `live-gate.js` from the trusted export (§9.5)
  and, on an ELIGIBLE verdict, posts the `run-002/independent-review`
  commit status against the trusted head SHA. **It is the only principal in
  the system that may write a commit status.** It cannot merge, cannot
  push, cannot comment, cannot read branch protection.
- **supervisor / merger** — runs preflight, routes PRs, marks ready,
  updates branches, comments, and executes the merge. **It cannot post the
  status it then acts on.** Its merge is permitted by GitHub only because
  the gate already satisfied the required context.
- **worker** — pushes its branch and opens its PR. Nothing else. It is the
  thing being judged, so it holds no permission that touches the judgement.

**The reason this is three and not two** is F2: §41.7's two credentials are
the publisher and the worker, and *neither* of them can merge. See §9.3
rows 5–8.

**The minimum sets the operator asked for, read off the table:**

| Capability | Minimum permission | Principal |
|---|---|---|
| **(a) read CI / check results** | `Checks: read` (+ `Commit statuses: read` for the rollup) | gate, and supervisor |
| **(b) publish the required result/status** | `Commit statuses: write` | **gate only** |
| **(c) execute merges** | `Pull requests: write` + `Contents: write` | **supervisor only** |

### 9.3 The cross-check — shown, not asserted

Every GitHub call the repository makes, the permission it needs, and the
principal that holds it. **Each `file:line` was re-read at `b66944a`**, and
`experiment/github-app/check-templates.py` re-asserts **every row that has
a site** — eleven of the thirteen. Row 4 has no site, which is the entire
point of row 4, and row 12 is a `git` transport rather than a call in this
repository. So this table cannot silently rot (§10.2).

| # | Call | Site at `b66944a` | Permission | Principal |
|---|---|---|---|---|
| 1 | `GET /repos/{r}/commits/{sha}/check-runs` | `apparatus/adapters/ci-result.js:69` | **Checks: read** | gate |
| 2 | `gh pr view --json …statusCheckRollup…` | `control/gh.py:66,77` | Checks: read **+** Commit statuses: read | supervisor |
| 3 | `gh pr list` | `control/gh.py:71` | Pull requests: read | supervisor |
| 4 | `POST /repos/{r}/statuses/{sha}` *(does not exist yet — F7)* | **nowhere**; see §9.11 | **Commit statuses: write** | **gate** |
| 5 | `gh pr update-branch` | `control/gh.py:121` | Pull requests: write + Contents: write | supervisor |
| 6 | `gh pr ready` | `control/gh.py:131` | Pull requests: write | supervisor |
| 7 | `gh pr merge --squash --delete-branch` | `control/gh.py:135` | Pull requests: write + Contents: write | supervisor |
| 8 | `gh pr comment` | `control/gh.py:145` | Issues: write *(UNVERIFIED)* | supervisor |
| 9 | `GET /repos/{r}/branches/{b}/protection` | `control/gh.py:150` ← `control/preflight.py:161` | **Administration: read** | supervisor |
| 10 | `gh run list --json status,conclusion,…` | `control/gh.py:156` ← `control/preflight.py:545` | **Actions: read** | supervisor |
| 11 | `gh pr create` | `control/gh.py:140`; `prompts/builder.md:60` | Pull requests: write | worker |
| 12 | `git push` of a worker branch | builder prompt | Contents: write — **today via SSH, not any token (F6)** | worker, **once §9.9(2) is applied** |
| 13 | `gh pr diff` / `gh pr view` (no `--json`), `gh pr list` | `prompts/reviewer.md:15-16`, `security.md:21-22`, **`accessibility.md:33-34`**, `observer.md:13` | Pull requests: read | worker *(§7 R15 — untested without Checks)* |

**How this demonstrates the three defects are gone.**

- **F1 — missing READ.** Row 1 is the call that §41.7's set could not make.
  `Checks: read` is on the gate, which is the principal that makes it. The
  live evidence that `ci` lives **only** on the check-run surface, and that
  the commit carries **zero** commit statuses, is §1.4 — a read-only GET
  recorded 2026-10-01, not re-queried on 2026-10-02 and not needed to be.
  **Mechanical re-assertion:** `check-templates.py` check `C. F1 resolved`
  plus `D. apparatus/adapters/ci-result.js:69 still calls check-runs`.
- **F2 — nobody could merge.** Rows 5–8 are the Supervisor's writes. Under
  §41.7 they belonged to neither credential; here they belong to the
  supervisor principal, which holds `Pull requests: write` + `Contents:
  write`. **Mechanical re-assertion:** `C. F2 resolved — a principal exists
  that can execute a merge`, paired with `C. the merger cannot publish the
  verdict it acts on`, so the fix does not quietly re-merge the two roles.
- **F5 — unsatisfiable required context.** Not a permission defect and not
  fixable by one; it is an ordering and code defect, resolved in §9.6.

**What the cross-check also shows, and should be read as a warning:** row 4
is the only row with no site. The permission at the centre of this entire
proposal exists to serve code that has not been written (F7, §7 R2).

### 9.4 Worker permissions and enforceable isolation

**GitHub-side:** `Contents: write` + `Pull requests: write`, nothing else
(§9.2 column 3). Sufficient for every command the frozen prompts issue
(§2.1), and insufficient for every command they must never issue.

**OS-side — the part that makes it enforceable.** A permission the worker
cannot reach a key for is worth more than one it is merely not given.
§3.3's three accounts stand; this is the asset-by-asset statement.

| Asset | `run002-sup` | `run002-wrk` | Enforced by | Falsified by |
|---|---|---|---|---|
| `~serina/.ssh/*` | no | no | existing `0700`/`0600`, owner `serina` | V1b |
| `~serina/.config/gh/hosts.yml` | no | no | existing `0600` + `0751` dir | V1 |
| Gate App private key | read | **no** | `0400`, owner `run002-sup` | V1c |
| Supervisor App private key | read | **no** | `0400`, owner `run002-sup` | V1c |
| Worker App private key | read (to mint) | **no** | `0400`, owner `run002-sup`; the worker gets a **1-hour token**, never the key | V1c |
| Trusted gate export `/opt/run-002/gate-<SHA>` | read | read | `chmod -R a-w`, owner `run002-sup` | V4 |
| Mutable checkout `apparatus/`, `control/`, `bin/`, `config/`, `prompts/`, `protocol/` | read | **read, not write** | owner `run002-sup`, group `run002`, `0750` dirs / `0640` files | V4b |
| `.runtime/ledger.jsonl` | read+write | **no** | `.runtime/` `0700`, owner `run002-sup` | V5 |
| `.runtime/state.json` | read+write | **no** | same | V5b |
| `.runtime/evidence/`, locks, `workers/`, `observer/` | read+write | **no** | same | V5 |
| Its own worktree | read+write | read+write | `0700`, owner `run002-wrk` | — must succeed |
| Another worker's worktree | — | **reachable** | nothing | **accepted, §7 R3** |

Four things make this enforceable rather than aspirational:

1. **The modes that matter already exist.** `~/.config/gh/hosts.yml` is
   `0600` and `~/.ssh` is `0700`, both owned by `serina` — VERIFIED by
   `ls -l`, **contents never read** (§3.1 claim 1). They exclude a
   different UID today. They exclude nothing while workers run as
   `serina`, which is exactly the hole, and changing the UID is exactly
   the fix.
2. **The worker's environment is built from an allow-list.**
   `control/worker_entry.py:180` is `env = dict(os.environ)` with no scrub
   before `Popen(..., env=env)` at `:124`. Runbook step 12 replaces it.
   Names for the allow-list are in
   `experiment/github-app/env-var-names.md` §3. **Until step 12 lands, a
   worker inherits the Supervisor's whole environment and the boundary is
   not enforced** — this is the single change the GitHub side cannot
   substitute for.
3. **The two layers must agree, and are checked separately.** The worker
   App has no `statuses` permission (GitHub-side, checked by
   `check-templates.py` check `C`), *and* the worker UID cannot read the
   gate key (OS-side, checked by V1c). Either alone is a single point of
   failure.
4. **The gate does not execute from anything a worker can write** — §9.5.

### 9.5 THE TRUSTED APPARATUS REVISION

> **TRUSTED APPARATUS REVISION:
> `2c7c6cf257054872171d6e8f34d6329368a056a6`**
> on `wip/c05-1-persistence`. Full 40 hex, never a branch name.

**This supersedes §41.7's `4ae1488`, which is 16 commits stale.** It also
supersedes this document's own first pin, `b66944a`, which went stale
within the same session — see "the pin and the moving branch" below.

#### The evidence, and why `main` is not a candidate

`main` was **not** assumed to contain the apparatus. It was checked, and
then re-checked independently by the integration owner at the final pin:

```
$ git rev-parse HEAD
2c7c6cf257054872171d6e8f34d6329368a056a6
$ git rev-parse main origin/main
4eeaa7ce76aa82a0168236cf4e4ae08d9edc2477
4eeaa7ce76aa82a0168236cf4e4ae08d9edc2477
$ git merge-base main HEAD
4eeaa7ce76aa82a0168236cf4e4ae08d9edc2477
$ git rev-list --count HEAD..main     ->  0      # main is a strict ancestor
$ git rev-list --count main..HEAD     -> 61      # 61 commits of apparatus
$ git diff --shortstat main..HEAD
  154 files changed, 51645 insertions(+), 444 deletions(-)
$ git ls-tree -r --name-only main -- apparatus control | wc -l  -> 50
$ git ls-tree -r --name-only HEAD -- apparatus control | wc -l  -> 88
```

Per-file, `git cat-file -e <ref>:<path>`:

| Path the gate needs | on `main` | at `b66944a` |
|---|---|---|
| `apparatus/pr-evidence/live-gate.js` | **ABSENT** | present |
| `apparatus/adapters/ci-result.js` | **ABSENT** | present |
| `apparatus/adapters/reviewer-identity.js` | **ABSENT** | present |
| `apparatus/accessibility/requirement-registry.js` | **ABSENT** | present |
| `control/gate_evidence.py` | **ABSENT** | present |
| `control/accessibility_registry.py` | **ABSENT** | present |
| `control/accessibility_services.py` | **ABSENT** | present |
| `apparatus/adapters/git-head.js` | present | present |
| `apparatus/adapters/task-record.js` | present | present |
| `control/gh.py`, `control/routing.py`, `control/preflight.py` | present | present |

**Conclusion, stated explicitly because the assumption is the dangerous
one: `main` does not contain the gate.** Exporting `main` as the trusted
revision would produce a tree with no `live-gate.js`, no CI adapter and no
requirement registry in it. The gate would not fail closed — it would fail
to exist. §41.7 said `main` is `4eeaa7c` and contains neither
`live-gate.js` nor `control/accessibility_registry.py`; that is **confirmed
here and is still true 61 commits later**, and it is the reason `main` is
disqualified rather than merely behind.

#### Why the branch tip and not an older branch commit

`4ae1488` (§41.7) is 21 behind; `1387275` (this document's original base) is
15 behind. Both predate work the gate depends on. The pin is always the tip
of the branch that holds the apparatus.

#### The pin and the moving branch

**This document's first pin went stale inside the session that wrote it.**
It named `b66944a`; two further commits landed — `f2129c5` (C-04b, which
changes `control/routing.py`, code the gate's decisions depend on) and
`6c53540` (a test-isolation fix), so it became `6c53540`.

**RE-PINNED AGAIN 2026-10-02, to `2c7c6cf257054872171d6e8f34d6329368a056a6`.**
Three further commits moved code inside the drift set: the C-22 worker
environment allow-list (`control/worker_entry.py`), the gate invoker
(`control/gate_invoker.py`), and the sixth provenance condition
(`control/routing.py` — which, again, is code the gate's decisions depend
on). `F2` went red naming `control/worker_entry.py` and stayed red until
this line was rewritten. **That is three re-pins in two sessions, every one
of them demanded by the check rather than noticed by a person**, which is
the argument for the check. The drift set itself was widened in the same
session to include `bin/` and `.github/`, so the figure it compares is now
larger than it was for either earlier pin.

That is not an embarrassment, it is the thing the check exists to catch,
and it exposed a real defect in the FIRST version of the check, which
asserted `pin == HEAD`. That assertion goes red the moment anything is
committed — **including the commit that records the pin itself**, and every
later documentation commit. A check that cannot be satisfied is a check
people learn to ignore.

`check-templates.py` now asserts the invariant that actually matters, in
two parts:

- **F1** the pin is REACHABLE from HEAD (`git merge-base --is-ancestor`),
  so it names a commit this branch really contains and an export can be
  made from it;
- **F2** `git diff --name-only <pin> HEAD -- apparatus control protocol
  prompts config` is EMPTY — nothing the gate executes, and nothing that
  governs it, has changed since the pin.

A documentation commit on top of the pin is harmless and stays green. One
line of `apparatus/` or `control/` is not, and goes red naming the file.

#### The precondition this pin still carries

```
$ git rev-parse origin/wip/c05-1-persistence
dbee92c6db4d78eac93ed9832eaef8fa27e1851e
$ git log --oneline origin/wip/c05-1-persistence..HEAD
183959e The test suite was writing the live run state
f2129c5 C-04b: the merge gate stopped merging on absent facts
b66944a Running it found G6's composite mapping never fired
15a5bbf C-05.3c: a fixture product the real accessibility services can actually run
7f937a8 C-02b: the frozen prompt stops calling its own dispatch unimplemented
```

**When the pin was chosen it was ahead of `origin`, and the commits in
between touch `apparatus/` and `control/`.** So:

- Pinning `dbee92c` instead would export an **older**
  `control/accessibility_services.py` and an **older** `control/routing.py`
  than the ones the current tests pass against — the latter being the one
  C-04b stopped merging on absent facts. Not acceptable.
- Pinning `6c53540` is correct, but while the branch sat unpushed it was
  **not fetchable by anyone else.** A trusted revision nobody can fetch
  cannot be independently verified, which defeats half the point of
  pinning.

> **PRECONDITION, runbook step 0b — SATISFIED 2026-10-02.**
> `wip/c05-1-persistence` was pushed to `origin` at `17dc358`, which
> contains the pinned commit, so `6c53540` is now fetchable and the pin is
> independently verifiable. Pushing the working branch is ordinary work and
> was authorised. What is **not** authorised by this document is creating
> the export, the Apps, the OS users, or anything else in §6.

#### The pinning rule, which outlives this particular SHA

1. The pin is a **full 40-hex SHA**, stated deliberately by the operator at
   approval time. A branch name is never a pin; a branch moves.
2. **Re-pin at approval if the gate's own code moved.** Documentation may
   land on top of a pin; `apparatus/`, `control/`, `protocol/`, `prompts/`
   and `config/` may not. `check-templates.py` checks `F1`/`F2` enforce
   exactly that, and both **have been watched failing** — `F` on a stale
   pin during this session, which is what forced the rule above to be
   written correctly, and the original `pin == HEAD` form before the pin
   was first written in (§10.2).
3. The export is created once, read-only, by `run002-sup`:
   `git -C <workspace> worktree add --detach /opt/run-002/gate-<SHA> <SHA>`
   → `chown -R run002-sup:run002` → `chmod -R a-w`.
4. The gate invoker uses **only** that absolute path, exactly as
   `control/gate_evidence.py:209`'s `RUN_JS` does — never a path inside the
   PR's worktree, and **never `config.REPO_ROOT`**, which is a mutable tree
   (§3.3).
5. The SHA is written into the ledger beside every decision, so a decision
   is attributable to the code that made it.

### 9.6 Branch protection — before, after, and the F5 ordering

**Before and after are §4.1 and §4.2, unchanged.** §4.1 is the state
VERIFIED LIVE by read-only GET on 2026-10-01; it was **not re-queried** on
2026-10-02, deliberately, because re-querying is a GitHub call this pass
did not need to make. Both now exist as applyable request bodies:

| | File |
|---|---|
| **BEFORE** (rollback artefact) | `experiment/github-app/branch-protection-BEFORE.json` |
| **AFTER** (proposed) | `experiment/github-app/branch-protection-AFTER.json` |

**The existing CI requirement is preserved, and this is checked rather than
promised.** `check-templates.py` check **"B. AFTER preserves the existing
ci requirement unchanged"** asserts the `ci` entry is identical in both
bodies, `app_id` 15368 included; check **"B. AFTER restates every field
BEFORE sets"** asserts no current setting is silently cleared by a PUT that
replaces the whole object.

**ONE SETTING IS REDUCED, AND IT IS THE ONE TO ARGUE ABOUT.**
`required_approving_review_count` goes **1 → 0** (§4.2 row, reasoning at
§4.2's table). Read plainly: *today `main` requires a human approving
review; under this arrangement it does not.* That is a reduction in
protection and nothing else in the payload compensates for it directly —
what replaces it is the new required context
`run-002/independent-review`, which only the gate principal can write and
only on an ELIGIBLE verdict against the trusted head.

The reason it cannot simply stay at 1 is mechanical, not preference:
the PR author and the authenticated identity are both `serina-mcfall`, and
GitHub does not permit an author to approve their own pull request, so the
requirement is unsatisfiable by anything in this system (§37.10). Leaving
it at 1 does not make the run safer — it makes every product PR
unmergeable, which is F5 in a different costume.

**If the operator is not willing to take `main` from "a human approved
this" to "the gate approved this", this arrangement should be rejected
rather than trimmed.** It is the hinge of the whole design.

The three traps, all re-checked mechanically:

| Trap | Why it bites | Check |
|---|---|---|
| `required_pull_request_reviews` sent as `null` | `control/preflight.py:174` computes `bool(reviews is not None and …)`; the REQUIRED gate `github_main_protection` FAILs and `ctl start` refuses | `B. AFTER sends required_pull_request_reviews as an OBJECT, not null` |
| `contexts` sent alongside `checks` | deprecated alias; sending both is an error | **"B. AFTER omits the deprecated contexts alias"** |
| `run-002/independent-review` added to `config/experiment.json` → `required_checks` | `ci-result.js:155-165` iterates that list against the **check-run** surface, where a commit status never appears → permanent silent deny (§4.3a) | `E. §4.3a holds — the status context was NOT added` |

#### F5 — the resolution, and the ordering that is the real fix

**Resolution: §4.3b option (ii)** — extend the existing
`blockedOnlyByDraft` pattern (`live-gate.js:557,566`) with a sibling
`blockedOnlyByPendingIndependentReview`, and let the **publisher** and the
**merger** consume different fields of the same verdict:

- the **publisher** acts on `eligible || blockedOnlyByPendingIndependentReview`;
- the **Supervisor's merge path** acts on `eligible` alone, unchanged.

**The safety condition, which is the whole of it.** The new flag must be
computed exactly as `blockedOnlyByDraft` is — from the reason list being
*precisely one* reason, and that reason being the branch-protection one:

> `reasons.length === 1 && reasons[0].code === 'PR_BLOCKED_BY_BRANCH_PROTECTION'`,
> **and** `ciResult.ok === true` with `ci` concluded success.

Computing it from `mergeStateStatus === 'BLOCKED'` alone would be
**fail-open**: `BLOCKED` is also what GitHub reports for a failing `ci`, an
unsatisfied conversation requirement, or any other protection rule
(`live-gate.js:516-528` says so in its own comment). The default-deny
backstop at `live-gate.js:544-554` must keep re-reading every adapter's
`ok` flag and must not be weakened; the new flag is a **separate reported
field**, never an input to `verified`.

**Required evidence before this is believed: a mutation.** Break the
condition to `mergeState === 'BLOCKED'` and prove a PR with failing `ci`
becomes publishable; restore it and prove it does not. A guard nobody has
watched fail is not a guard.

**The ordering invariant — this is the part that actually resolves F5:**

```
build + mutation-test the resolution (runbook 10)
  → build the publisher (runbook 11)
    → reproduce the deadlock deliberately on the throwaway (V10, runbook 9)
      → ONLY THEN apply branch-protection-AFTER.json to main (runbook 15)
```

Applying the AFTER payload at any earlier point deadlocks every product PR
permanently. That precondition is restated inside the AFTER template's own
header, so it travels with the file rather than living only here.

### 9.7 Expected-head protection

Four layers, three of which exist and one of which does not:

| # | Layer | Status |
|---|---|---|
| 1 | A commit status is bound to one SHA by construction — `POST /statuses/{sha}`. A new head has no status, so the required context is unsatisfied again automatically. | inherent to GitHub |
| 2 | `required_status_checks.strict: true` — the branch must be up to date with `main`, so a moved base re-opens the gate. | **already true** (§4.1), preserved in AFTER |
| 3 | The gate anchors on the SHA **git** resolves, never on `pkg.head_sha` — `live-gate.js` header lines 11-17; the submitted SHA is only ever compared to it. Review provenance is SHA-bound: `supervisor.py:1885,3846` emit `head_sha` on `REVIEW_DISPATCHED`/`REVIEW_RESULT`. C-20a(E) makes a new head invalidate the prior review even for an identical diff. | **already built** |
| 4 | **The merge call naming the head it expects.** | **MISSING** |

Layer 4, precisely. `control/gh.py:134-136` is:

```python
def merge(repo: str, number: int, method: str = "squash") -> Result:
    return run(["gh", "pr", "merge", str(number), "--repo", repo, f"--{method}",
                "--delete-branch"])
```

No expected-head argument. Between the gate deciding on trusted head `S`
and this call executing, a push can move the head and the merge takes the
new one. Layers 1–3 make that window **small and loud** — the status would
not cover the new SHA and `strict` would likely report `BEHIND` — but
small is not closed.

**Specified fix (a code change this document does not authorise, runbook
step 12):** pass the trusted head explicitly, and refuse rather than merge
if GitHub reports a different head:

```
PUT /repos/{repo}/pulls/{n}/merge      body: { "sha": "<trusted head>", ... }
```

The REST form's `sha` parameter is the unambiguous expected-head
primitive and the recommended target. The `gh` CLI exposes an equivalent
merge flag in recent versions; **which flag, and whether this host's `gh`
has it, is UNVERIFIED — a local hook in this environment refuses `gh pr`
invocations, including `--help`, so it could not be checked here.** Confirm
before writing the code; prefer the REST form if there is any doubt.

### 9.8 Credential lifecycle

Names only. **No value, path contents or key material appears in this
document, in `experiment/github-app/`, or in any transcript.**

| Phase | Gate | Supervisor | Worker |
|---|---|---|---|
| **Create** | App via the manifest in `experiment/github-app/app-manifest-gate.json`; generate one private key | `…-supervisor.json` | `…-worker.json` |
| **Install** | "Only select repositories" → `wellbeing-run-002` only. **Throwaway repo first** (runbook 5), target repo only after §5 passes (runbook 14) | same | same |
| **Store** | key file `0400`, owner `run002-sup`, outside the repository and outside `~/.config/run-002/secrets.env` | same | same — **the key stays with `run002-sup`; the worker never receives it** |
| **Use** | `run002-sup` mints a 1-hour installation token per invocation | same | `run002-sup` mints a 1-hour token **per worker**, injected into that worker's environment via the §9.4 allow-list |
| **Rotate (routine)** | generate a new key on the App settings page, deploy it, **then delete the old key** — in that order, so there is no window with no valid key | same | same |
| **Rotate (suspected exposure)** | **delete the exposed key FIRST**, then generate. Order inverted deliberately: availability is not the priority when a key may be loose | same | same |
| **Revoke one worker** | — | — | let the 1-hour token expire, or `DELETE /installation/token`; the blast radius is bounded by the lifetime |
| **Revoke everything** | delete the App — this removes its installations and invalidates every token minted from it | same | same |
| **Audit** | every `run-002/independent-review` status carries `creator` = the gate App; a status from any other principal does not satisfy the `app_id`-pinned context (V3c) | merges appear under the supervisor App | PRs and pushes appear under the worker App |

**The key is the asset, not the token.** A leaked 1-hour installation token
expires. A leaked private key does not, and mints new tokens until it is
deleted. This is why the keys are `0400` under one UID and why the worker
receives tokens only.

**Rotation triggers**, stated so "when" is not a judgement call: any
`V1c` failure; any key file found with a mode other than `0400` or an owner
other than `run002-sup`; any key value appearing in any transcript, log or
command line; the end of the run.

**Hard rule for every agent, now and later.** Key files are checked with
`ls -l` or `test -r` and **nothing else**. Never `cat`, `grep`, `strings`,
`head`, `tail`, `awk`, `sed`, `less`, and never a glob that could sweep
them in. Short patterns are the dangerous ones — exclude credential paths
by name even when the pattern looks harmless.

### 9.9 The two decisions §43.5(c) leaves to the operator

**RECOMMENDED ANSWERS. NOT DECIDED. PENDING THE OPERATOR'S APPROVAL.**
§9 is built on both; rejecting either sends §9 back rather than leaving it
half-applicable.

#### Decision 1 — a third principal, or widen the gate App?

**RECOMMENDED: the third principal (`run-002-supervisor`).**

| | Third principal *(recommended)* | Widen the gate App |
|---|---|---|
| Can the publisher merge what it blessed? | **No** — the gate has no write permission of any kind | **Yes** |
| GitHub objects to manage | 3 Apps, 3 keys | 2 Apps, 2 keys |
| What the `app_id` pin buys | an unforgeable context posted by a principal that cannot act on it | an unforgeable context posted by the principal that then merges on it |
| Falsifiable? | **yes** — V8 asserts the gate App gets 403 on merge | no test can distinguish it from a single all-powerful credential |

**Reasoning.** §2.3's four arguments are about the *worker*, and widening
the gate App does not breach any of them — so the honest case for widening
is real: it is one fewer App and one fewer key. The case against is that
`required_status_checks.checks[].app_id` exists to make the verdict
attributable to a principal, and attribution is worth much less when that
same principal performs the act the verdict permits. The whole document
exists to separate producing work from blessing it; **having blessed it and
acted on it collapse into one credential undoes that at the last step, for
the saving of one App.**

The cost is honest and small: one more App, one more key file, one more
line in §9.8. **R12 limits the claim** — both keys sit under one OS user,
so this is a GitHub-side split that defends against the worker, not against
a compromised `run002-sup`.

#### Decision 2 — does `origin` move from SSH to HTTPS?

**RECOMMENDED: YES — move `origin` to HTTPS, for the worker only, and only
inside the worker's own worktrees.**

The fact, VERIFIED IN CHECKOUT: `git remote -v` →
`git@github.com:serina-mcfall/wellbeing-run-002.git` (fetch and push).

Why this has to be answered rather than deferred: under §3.3 the worker UID
cannot read `~serina/.ssh`. That is the boundary working **and** it means
**builders cannot push at all**. The worker App's `Contents: write` is
decorative until git actually authenticates with it (§7 R4). V6 is the test
that forces this to the surface; it has two failure modes and *both* are
findings:

- V6 **fails** → the SSH key is correctly excluded, and the builder is
  broken until this decision is applied.
- V6 **succeeds** → F6 is proven live: a worker is pushing with Serina's
  personal SSH key, a credential with far more reach than the whole App
  scheme governs.

| | HTTPS + worker token *(recommended)* | Keep SSH | Deploy key for `run002-wrk` |
|---|---|---|---|
| Worker pushes with a credential this scheme governs | **yes** | no | yes |
| Blast radius of a leak | one repo, one hour | **every repo Serina's key reaches** | one repo, indefinitely |
| Revocable without touching Serina's account | **yes** | no | yes |
| New machinery | a remote URL and `GH_TOKEN` | none | a key to generate, store and rotate |

**Reasoning.** The SSH key is the strongest write credential in the system
and sits entirely outside the scheme — a larger hole than the one the App
closes. HTTPS with the minted worker token puts it inside, with a one-hour
blast radius, revocable without touching Serina's personal account. A
dedicated deploy key also works but trades a 1-hour token for a permanent
one and adds a fourth key to the rotation table for no gain.

**Scope limit, deliberate:** change the remote **in the worker's
worktrees**, not in Serina's main checkout. Her own `git push` keeps using
SSH, unchanged, and nothing about her normal workflow moves.

**Consequence to accept:** if `origin` is HTTPS for the Supervisor too,
`Contents: read` returns to the supervisor App for `git fetch` — the
caveat §1.2's F6 row already states. Under the scope limit above, the
supervisor keeps SSH and `Contents: read` stays out.

### 9.10 Verification and rollback

**Verification is §5, unchanged**, plus one step this section adds:

| # | Step | Expected | Why added |
|---|---|---|---|
| **V11** | As `run002-wrk` with the worker token: `gh pr view <n> --repo <throwaway>` — **no `--json`**, exactly as the frozen prompts issue it | **exit 0**, with whatever degradation the missing `checks` permission causes, recorded | §7 R15: the worker App deliberately has no `Checks` permission, and three frozen prompts run this command. If it errors, either the worker needs `Checks: read` after all or the prompts need amending — and a frozen prompt amendment is its own governance act |

§5's existing design property holds and is why the suite is trustworthy:
**V2b, V3b and V3c must SUCCEED.** A suite in which every step fails proves
only that the credentials are broken.

**Rollback, by layer, each reversible on its own:**

| Layer | Rollback | Reverses |
|---|---|---|
| Branch protection | `gh api -X PUT …/protection --input <(jq .body experiment/github-app/branch-protection-BEFORE.json)` — **or**, in preference, the bytes captured live at runbook step 15 if they differ | the only change visible to anyone outside this host |
| Installations | uninstall each App from the repository | all three principals' access, at once |
| Apps | delete each App — removes installations and invalidates every token | the credentials themselves |
| Keys | `shred` the key files | local key material |
| Trusted export | `git worktree remove /opt/run-002/gate-<SHA>` | the pinned gate |
| OS users | `userdel run002-sup run002-wrk; groupdel run002`; restore ownership to `serina` | the OS boundary |
| Code (runbook 10-12) | revert the commits | the F5 resolution, publisher and token plumbing |

**The BEFORE file is reconstructed from §4.1's recorded GET, not from a
fresh query.** If the live state has drifted since 2026-10-01, the bytes
captured at runbook step 15 win over the file. That is why step 15 captures
before it applies, and the file says so in its own header.

### 9.11 What §9 settles, and what it does not

**Settled** — target and branch; the three principals and their minimum
permissions; who publishes and who merges; worker permissions and the
asset-by-asset isolation map; the trusted revision with its git evidence;
before/after protection with the CI requirement preserved and mechanically
checked; the F5 resolution and the ordering that makes it safe; the
expected-head gap and its specified fix; the credential lifecycle; the
rollback.

**Not settled, and not settleable here:**

| # | Item | Why |
|---|---|---|
| 1 | **Both §9.9 decisions** | the operator's, by §43.5(c). Recommendations given; approval pending |
| 2 | **The pin, at approval time** | if commits land first, re-pin to the then-current HEAD (§9.5 rule 2) |
| 3 | `Issues: write` for `gh pr comment`; whether `Contents: write` is required alongside `Pull requests: write` for merge and update-branch | GitHub's exact permission mapping. **UNVERIFIED** — confirm on the App creation screen (§7 R7). No docs page was fetched and none is quoted |
| 4 | Whether `gh pr view` without `--json` needs `Checks` | **UNVERIFIED** — needs a token (§7 R15). V11 |
| 5 | Which `gh pr merge` flag is this host's expected-head flag | **UNVERIFIED** — a local hook refuses `gh pr` invocations here (§9.7) |
| 6 | Whether a GitHub App is already installed on the repository | **UNVERIFIED** — needs App-authenticated access (§7 R8) |
| 7 | **The publisher itself** | does not exist (F7). Every `statuses: write` grant here provisions for unwritten code |
| 8 | Merge-policy reconciliation — `routing.evaluate_merge` vs `live-gate.js` | handover §43.5(b). §9.5 unblocks its option (i) by deciding the trusted revision; it does not choose the option |
| 9 | The apparatus threat model | §7 R9: `protocol/SECURITY-THREAT-MODEL-V2.md` is frozen and covers none of this |

---

## 10. The reviewable templates

Under `experiment/github-app/`. **Every file is a TEMPLATE, unapplied, and
carries that statement in its own header. Deployment is not authorised and
none of it has been submitted anywhere.**

### 10.1 What is there

| File | What it is |
|---|---|
| `app-manifest-gate.json` | permission declaration for `run-002-independent-review` — the publisher |
| `app-manifest-supervisor.json` | permission declaration for `run-002-supervisor` — the merger (the principal F2 says is missing) |
| `app-manifest-worker.json` | permission declaration for `run-002-worker` |
| `branch-protection-BEFORE.json` | the exact `PUT` body restoring §4.1's verified current state — **the rollback artefact** |
| `branch-protection-AFTER.json` | the exact `PUT` body for §4.2 / §9.6, with `__GATE_APP_ID__` unresolved |
| `env-var-names.md` | every config and environment variable **name** the scheme would use. **Names only; no value may ever be written there** |
| `check-templates.py` | the checker below |

Each JSON wraps its payload so the notice cannot leak into a request: the
manifests put it in `_TEMPLATE_NOTICE` beside a `manifest` key, the
protection bodies beside a `body` key. **Submit or `--input` the inner key
only** — `jq .body …` / `jq .manifest …`. An unknown key in a real request
is at best ignored and at worst rejected.

### 10.2 The checker, and what running it proved

Run from the repository root — it makes **no network call, no GitHub call,
touches no credential and writes nothing**:

```
$ python3 experiment/github-app/check-templates.py
```

**Result, 2026-10-02 at `b66944a`: 52 checks, 52 PASS, exit 0.** It
asserts, mechanically rather than by assertion in prose:

- **A** — all five JSON templates parse, and each carries its TEMPLATE notice.
- **B** — the AFTER body preserves `ci` @ `app_id` 15368 identically to
  BEFORE; restates every field BEFORE sets; omits `contexts`; sends
  `required_pull_request_reviews` as an object; `enforce_admins` true;
  `restrictions` null; and still carries the **unresolved**
  `__GATE_APP_ID__` placeholder — a real App ID in a template would mean an
  App exists, and none does.
- **C** — exactly one principal may write a commit status and it is the
  gate; the gate holds `checks: read` (**F1**); a principal exists that can
  merge (**F2**); that principal cannot write a status; `administration`
  and `actions` read are present (**F3, F4**); and the worker holds none of
  `statuses`, `checks`, `administration`, `actions`.
- **D** — all nineteen call sites behind §9.3 still contain the cited call
  at the cited line. If one moves, the check reports the line it moved to
  rather than passing silently, so the permission set cannot drift away
  from the code. **This check already earned itself:** it is how
  `prompts/accessibility.md`'s drift from `:31-32` to `:33-34` was found
  (§8).
- **E** — **F7 still holds**: `git grep statuses/ -- '*.py' '*.js'` returns
  nothing outside `experiment/`, so nothing in the repository posts a commit
  status. And §4.3a holds: `config/experiment.json` → `required_checks` is
  still exactly `["ci"]`.
- **F** — the TRUSTED APPARATUS REVISION stated in §9.5 is this checkout's
  `HEAD`.

**Check F was watched failing.** The checker was written before §9.5
existed, and its first run reported `FAIL  F. the proposal states a 40-hex
TRUSTED APPARATUS REVISION`, exit 1, with every other check passing (44 of
them at that point; the suite has since grown to 52 by pinning the eight
prompt call sites). It passed only once the pin was written into §9.5. A
guard nobody has watched fail is not a guard, and that failing run is the
only evidence that this one is real.

**What the checker does not do, deliberately:** it is not in `tests/`, is
not collected by any suite, has no filename a test runner matches, and is
run by hand. It checks the templates against the repository — **it cannot
check them against GitHub**, which is exactly where items 3–6 of §9.11 live.

---

**No App, credential, token, key, account, OS user, container or
repository was created. No protection, permission, setting, group or host
user was changed. No authenticated mutating call was made. No secret was
read, printed or pattern-matched. `T+00` remains `NOT_STARTED`.**
