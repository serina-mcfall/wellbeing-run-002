# C-05.3a Step 6b — Session Handover, Repair Plan and Implementation Record

> ## ⇒ READ SECTION 41 FIRST
>
> **§41 is the current session handover (2026-10-01).** It carries the
> verified branch, HEAD, push and working-tree state, what is implemented
> and what verifies it, the decisions already approved and applied, the
> decisions genuinely pending, and the dependency-ordered path to launch.
>
> **§16 is STALE** — it predates §§28–41 and still reports C-19 as
> unimplemented. Where any earlier section disagrees with §41, §41 wins.
> Sections are appended in working order, not numerical order.

**Status: IMPLEMENTED (2026-09-30), across three rounds.**

All five proven defects, the four authorised integration repairs, and the
reuse-branch dispatch-control bypass are in the working tree and verified. Read the implementation record in section order:
**sections 17, 18, 19, 20, 21, 22 and 23 are the most recent and win, then 12, 14, 15 and 16,
then 11, then 10**, each in turn winning over the plan in section 5. Sections
3–7 record the investigation and are preserved as the reasoning, not as
current behaviour.

Section 11.7 remains the live limitations list (section 12 adds none); §10.8's first two
entries were resolved by §11.1 and no longer apply.

**2026-10-01 additions.** §17 records the `skills/README.md` import, which
closes the last `SPEC_FILES` gap. §18 records **C-18 stage 1** —
`route_prs`'s GitHub observation moved outside T1. §19 records **C-18
stage 2** — notification delivery moved out of state transactions behind a
durable intent queue. §15's "not yet carried over" and §14.3's "no stage may
be started under this prompt" are superseded by those sections in that narrow
scope and nowhere else. **C-18 remains OPEN**; stages 3–7 are untouched.

Nothing is staged or committed. T+00 remains **NOT_STARTED**.

Canonical sources: `experiment/CONTRADICTION-AUDIT.md` (C-18, C-05a, C-05b),
`control/supervisor.py`, `control/routing.py`, `control/reconcile.py`,
`control/watchdog.py`. Where those disagree with this document, they win.

T+00 remains **NOT_STARTED**. Nothing here authorises launch. C-05.3b is
untouched. **C-18 remains OPEN**: stages 1, 2 and 3 of seven landed
2026-10-01 (§18, §19/§20, §21) and three stages do not close the row. The rehearsal plan in
`experiment/REHEARSAL-PLAN.md` is PROPOSED and unauthorised.

---

## 1. Verified current state

Branch `wip/c05-1-persistence`, working tree uncommitted, nothing staged.

Step 6b is **implemented and wired into `Supervisor.tick`**. The phase split is:

| Phase | Where | Lock held? |
|---|---|---|
| A — observe | `observe_security` | no |
| E-publish | `publish_security_results` → `_publish_one_security_result` | no |
| T1 — claim + ingest | `route_evidence` → `plan_security` / `ingest_security` | **yes** |
| C — materialise + spawn | `execute_security` | no |
| D — cache spawn | `confirm_security_spawn` | own transaction |

Phase E is deliberately **not** a `reap_workers` callback, because `reap_workers`
runs inside T1.

### Checks run

```
python3 -m unittest discover -s tests   →  Ran 1270 tests ... OK
git diff --check                        →  exit 0
```

The three `[FAIL] c16_probe: gate raised: GATE_EVALUATION_FAILED` lines on stderr
are the C-16 deliberate-exception probe printing its expected fail-closed path.
They are **not** test failures; the suite reports `OK`.

Focused counts: `test_c05_3_security_evidence.py` 86 · `test_c05_3_evidence_recovery.py`
54 · `test_c05_3_evidence_claim.py` 37 · `test_c05_3_evidence_routing.py` 32 ·
`test_c05_3_security_contract.py` 14.

### Mutation evidence already captured

Three Step 6b guards mutated in `control/supervisor.py`, each watched go red,
then restored (SHA-256 `48247d23…b5e0` identical before and after):

| Mutation | Caught by |
|---|---|
| `_current_claim`: drop the `attempt_id` half of the identity gate | 2 tests |
| `ingest_security`: drop the `claim_state == "COMPLETE"` guard | 2 tests |
| `publish_security_results`: drop the `worker in entries` liveness gate | 1 test |

### What is confirmed sound

- **Transaction boundary.** `test_t1_claims_without_touching_the_filesystem`
  pins that T1 creates no directory and no job file. C-18's requirement holds.
- **Identity gate.** `_current_claim` re-checks task association, PR, head SHA
  and attempt id. Publication precedes ingestion; a failed publication commits
  nothing.
- **`reconcile.reconcile()` is unaffected** — `PR_OPEN` and `WAITING_EVIDENCE`
  are not in `RECONCILABLE_STATES` (`control/reconcile.py:68`).

---

## 2. Approved constraints governing the repair

Carried from the authorising session, restated verbatim in substance:

1. External spawning outside T1 does **not** prohibit durable ownership
   registration in Phase D. The design must also cover crashes between
   acquisition/spawn and Phase D.
2. The durable security claim is the **attempt identity authority**.
3. Track actual acquired resources through the **existing ownership mechanism**
   where suitable.
4. Keep result publication/ingestion **outside `reap_workers` callbacks** that
   run under T1.
5. Release security review worktrees after worker termination **and** durable
   outcome publication, **outside** state transactions.
6. Publication failure **retains** resources and evidence for retry.
7. Cleanup failure remains **durably visible and retryable**.
8. `COMPLETE` must **not** erase ownership of resources that still physically
   exist.
9. Do **not** delete a worktree while its worker is live, or while liveness is
   unknown.
10. Do **not** add fields to the closed nine-key security claim. New resource
    metadata needs its own location, schema and validation.
11. Do **not** run `workmux`/`git` subprocesses inside state transactions or
    inside pure reconciliation checks.

---

## 3. Proven defects

Five defects, each reproduced. Probes were run read-only against the current
tree; no production or test file was modified.

### D1 — A previous SHA's provider output is published as the current SHA's security outcome

**Severity: highest.** This fabricates a security verdict for a commit no
reviewer ever looked at.

`routing.security_claim` derives `worker = f"{task_id.lower()}-security-{ordinal:04d}"`.
The SHA is **not** in the name, and the ordinal restarts at 1 for a new SHA
(`plan_security`: "a claim for a different head starts again at one"). So SHA-A
attempt 1 and SHA-B attempt 1 produce a byte-identical worker name — and every
worker artefact in `WORKER_LOG_DIR` is keyed on that name
(`.job.json`, `.status.json`, `.last.txt`, `.out`, `.entry.log`). Neither
`workers.write_job` nor `workers.start_job` clears a stale status or output file.

Reachable path: SHA-A completes and is published → head moves to SHA-B → fresh
claim, same worker name → Phase C creates the SHA-B attempt directory, then the
spawn fails (`mkdir` happens *before* `acquire_worktree`/`start_job`), so the
claim stays `PLANNED` and no SHA-B worker ever runs → the next tick's Phase
E-publish reads status and output **by worker name**.

Reproduction output:

```
step 1  SHA-A verdict : SECURITY_FAIL
step 1  worker name   : task-001-security-0001
step 2  SHA-B worker  : task-001-security-0001   <-- identical
step 3  SHA-B claim_state stays: PLANNED (spawn failed; no SHA-B worker ever ran)

RESULT: a durable security outcome now exists for SHA-B.
   published sha     : cccccccccccccccccccccccccccccccccccccccc
   verdict           : SECURITY_FAIL
   finding file      : ['app/sha-A-only.ts']
   finding summary   : ['THIS DEFECT EXISTS ONLY IN SHA-A']

   ingested task state: FIX_REQUIRED
   pending_findings   : ['app/sha-A-only.ts']
```

The claim-SHA check does not catch this: the claim's `sha` **is** SHA-B. What is
wrong is the provenance of the *output*, which the SHA check never examines.

### D2 — A completed review whose publication fails is abandoned and re-run as a second paid review

`publish_security_results` retries each tick, but if publication keeps failing
until the lease expires, `security_recovery_state` returns `PROVEN_NOT_RUNNING`
(worker absent from a good scan + lease expired), which is dispatchable.
`plan_security` then advances the ordinal and logs `SECURITY_ATTEMPT_ABANDONED`.

```
completed provider output still readable: True
durable outcome published              : False
recovery classification                : PROVEN_NOT_RUNNING

RESULT: a REPLACEMENT attempt was planned -- a second PAID review.
   new ordinal : 2  new worker: task-001-security-0002
   ledger      : ['SECURITY_OUTCOME_PUBLISHED', 'SECURITY_ATTEMPT_ABANDONED',
                  'SECURITY_EVIDENCE_CLAIMED']
```

The first review's output is still sitting readable in `WORKER_LOG_DIR` when the
replacement is planned. This burns budget and violates constraint 6.

### D3 — Replacing a stale-head claim loses ownership of a still-live older attempt

When the head moves while attempt 1 is still running, `security_recovery_state`
returns `NO_CLAIM` (stale head) — dispatchable — and `plan_security` overwrites
`record["security_evidence"]`. The live worker's claim is gone.

```
SHA-A attempt 1 worker : task-001-security-0001 (LIVE, lease has 1800s left)
recovery for SHA-B     : NO_CLAIM

claim now on the record: cccccccccccc ordinal 1 worker task-001-security-0001
SHA-A claim still held anywhere in state: False
```

**Interaction that matters for sequencing:** today the replacement claim happens
to reuse the *same worker name*, which is the only reason the live process is not
immediately unowned — and that reuse is D1. **Fixing D1 alone widens D3.** The
two fixes must land together.

### D4 — The supervisor singleton guard is a check-then-write race

`Supervisor.run` (`control/supervisor.py:2131-2143`) does
`exists()` → `read_text()` → `is_running()` → `write_text()` with no exclusion.

Two subprocesses executing that sequence verbatim, released from a shared
barrier, 40 trials:

```
trials: 40
trials where BOTH processes proceeded as supervisor: 16
```

This is an actual timing reproduction, not a structural argument. The existing
PID check must **not** be described as preventing concurrent startup.

The proposed remedy — non-blocking exclusive `fcntl.flock` on a dedicated lock
file, held for the process lifetime, the same primitive `control/state.py:219`
and `control/ledger.py:125` already use — was verified under the identical
harness:

```
trials: 40
trials where BOTH proceeded : 0
trials where exactly one was refused : 40

--- crash release: does the kernel free the lock when the holder dies? ---
PROCEEDED            (after the previous holder was SIGKILLed)
```

### D5 — Running security attempts are reported as orphan resources

`execute_security` spawns outside T1 and so never writes a `doc["workers"]`
record. `reconcile.detect_orphans` derives ownership only from `doc["workers"]`
and `task["retained_worktrees"]`, so a legitimately running attempt yields:

```
FINDINGS while the security attempt is legitimately running:
  ORPHAN_WORKTREE          …/run-002__worktrees/security-security-attempt-0001
  ORPHAN_WORKER_PROCESS    task-001-security-0001
```

Both are in `watchdog.ORPHAN_CLAIM_CHECKS`, so both reach a human as *"has no
durable owner"*. Detection never kills or removes, so the worker survives; the
cost is false alerts, and the worktree finding persists for the whole run
because nothing releases a security worktree.

---

## 4. Ownership design: claim-based vs `doc["workers"]` record

The comparison was settled by running the reaper against a synthetic
`role="security"` worker record that had just reached a terminal phase, with
Phase E not yet published:

```
before reap: record present        : True
before reap: retained_worktrees    : None
after  reap: record present        : False
after  reap: retained_worktrees    : None

=> ownership of /repo__worktrees/task-001-security-0001
   still recorded anywhere?        : False
```

`reap_workers` pops the record for **any** terminal worker, including one with no
role handler (`control/supervisor.py:1139-1147` — `handler` is `None` for an
unknown role, and `doc["workers"].pop(worker, None)` runs regardless), and
`_retain_worktree` retains only for `builder`/`fixer` unless `all_roles=True`.

| | `doc["workers"]` record | Durable claim |
|---|---|---|
| Written where | Phase D only (T1 has no `doc` in Phase C) | Already written in T1 |
| Survives worker termination | **No** — popped by `reap_workers` inside T1 | Yes, until `COMPLETE` and beyond |
| Ownership during publish retry | **Lost** — the retry window is exactly when the record is gone | Held |
| Worktree retained on reap | **No** (not builder/fixer) | n/a |
| Crash before Phase D | **No record at all** — unowned | Claim already durable from T1 |
| Interacts with T1 callbacks | Yes — pulls security into `reap_workers`, against constraint 4 | No |

**Recommendation: claim-based ownership for the process; the existing
`task["retained_worktrees"]` map for the worktree.**

The claim is already durable *before* the spawn, which is the only thing that
covers a crash between acquisition and Phase D. A worker record cannot be, because
Phase C holds no transaction.

---

## 5. Proposed repair plan

All items **PROPOSED**. Ordering matters; R1 and R3 must land in the same change.

### R1 — Bind the worker name to the SHA  *(fixes D1)*

`control/routing.py`, `security_claim`:

```
worker = f"{task_id.lower()}-security-{sha[:12]}-{ordinal:04d}"
```

35 characters at most, inside `SECURITY_WORKER_RE`'s 64, and `[a-z0-9-]`-safe
because `sha` is lowercase hex. **No new claim key** — `worker` is already one of
the nine, and only its derivation changes.

`security_claim_is_valid` gains a derivation check that needs no `task_id`:

```
claim["worker"].endswith(f"-{claim['sha'][:12]}-{claim['ordinal']:04d}")
→ else CLAIM_WORKER_PROVENANCE_MISMATCH
```

This makes stale artefacts **unreachable by construction**: every filesystem
artefact is keyed on the worker name, so a new SHA can never read an old SHA's
status or output. It is a stronger guarantee than a comparison, because there is
nothing left to compare.

### R2 — Preserve completed evidence for publication retry  *(fixes D2)*

Publication failure must suppress the `PROVEN_NOT_RUNNING` → replacement path
while the attempt's own output is still readable.

Smallest change: in `security_recovery_state`, add one observation field to
`SecurityObservation` — `output_readable: bool`, computed in Phase A from the
existing `workers.read_status` / `{worker}.last.txt` reads — and return
`SECURITY_RECOVERY_INDETERMINATE` (holds, does not dispatch) instead of
`PROVEN_NOT_RUNNING` when the lease has expired but the attempt's own output is
still present and its status is terminal. `publish_security_results` keeps
retrying every tick.

`SecurityObservation` is an observation dataclass, **not** the claim — adding a
field there does not touch the nine-key schema.

Escalation, so this cannot hang silently: when an attempt has been
publication-blocked for longer than a governed bound, raise a
`HUMAN_REQUIRED` intervention rather than dispatching a paid replacement. **The
bound is an open decision — see §8.**

### R3 — Do not discard a live attempt's claim  *(fixes D3)*

`plan_security` must not overwrite `record["security_evidence"]` while the
existing claim's worker is live or its liveness is unknown. Both facts are
already available: `observation.worker_live` and `observation.scan_ok`.

Proposed: when the claim is valid, is for a different head, and
(`worker_live` or `not scan_ok`), log `SECURITY_EVIDENCE_HELD` with outcome
`STALE_HEAD_ATTEMPT_LIVE` and return `None`. The head-moved reclaim happens on a
later tick, once the old attempt is provably finished.

This keeps the older attempt's identity in state for as long as its process
exists, satisfying constraints 8 and 9.

### R4 — Teach ownership to `detect_orphans`  *(fixes D5)*

`control/reconcile.py::detect_orphans`, two ownership sources added. **Pure —
no subprocess**, satisfying constraint 11:

- **Process.** A `security_evidence` claim that is a valid claim and whose
  `claim_state` is not `COMPLETE` owns the process named by `claim["worker"]`.
  A `COMPLETE` claim whose worktree entry is still present (below) also owns it,
  so `COMPLETE` never erases ownership of something still physically there.
- **Worktree.** Already covered by the existing `task["retained_worktrees"]`
  read, once R5 writes into it.

### R5 — Register the worktree in the existing ownership mechanism  *(constraint 3)*

Phase D (`confirm_security_spawn`) already holds its own transaction and already
receives the identifiers. It gains the acquired worktree path, which
`execute_security` already has as `path` from `acquire_worktree`, and writes:

```
task["retained_worktrees"][<path>] = {
    "retained_at": <iso>,
    "why": "SECURITY_ATTEMPT_ACTIVE",
}
```

**No new schema.** This is the existing map, the existing two keys, and the
existing `detect_orphans` ownership read. The only new thing is a new `why`
value, which is free text in the current structure.

**Crash between spawn and Phase D:** the worktree is registered but unowned for
at most one tick. Phase A already re-derives everything from the claim and the
filesystem, so the next tick's Phase D equivalent can re-register it. To make
that concrete, the re-registration must be idempotent and must run from the
claim, not only from a fresh spawn — see the lifecycle order below.

### R6 — Release the worktree after termination *and* publication  *(constraints 5, 7, 9)*

A new Phase F, **outside any transaction**, after `publish_security_results`:

For each claim whose worktree entry exists, release only when **all** hold:
1. the worker is absent from the `proc` scan **and** the scan succeeded
   (`entries is not None`);
2. a durable outcome exists (`read_security_outcome` is not `None`);
3. the claim is `COMPLETE`.

Then `workers.remove_worker(worker)`. On success, drop the
`retained_worktrees` entry in a small follow-up transaction. On failure, **leave
the entry in place** and log `SECURITY_WORKTREE_RELEASE_FAILED` with a finite
outcome — durably visible, and retried next tick.

This mirrors `release_review_worktree`'s shape
(`control/supervisor.py:1333-1341`) but gates on publication as well as
termination, which the reviewer version does not need.

### R7 — Atomic singleton  *(fixes D4)*

`Supervisor.run`, replacing the check-then-write:

```
self._singleton = open(config.SINGLETON_LOCK_PATH, "a+")
try:
    fcntl.flock(self._singleton.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    self.log("SUPERVISOR_START_REFUSED", outcome="ALREADY_RUNNING", …)
    return 1
config.PID_PATH.write_text(str(os.getpid()), encoding="utf-8")
```

- **Lifetime:** the file object is held on the instance so it is not garbage
  collected; the lock lives exactly as long as the process.
- **Crash/restart:** the kernel releases the lock when the holder dies, verified
  above with `SIGKILL`. No stale-lock cleanup, no PID liveness heuristic.
- **Watchdog compatibility:** unchanged. `watchdog.supervisor_pid()` still reads
  `PID_PATH` for liveness and reporting; the `SUPERVISOR_STAND_DOWN` path
  (`control/watchdog.py:647-664`) already handles "a start was refused because
  the incumbent is alive", which is precisely what a refused `flock` produces.
- `PID_PATH` stays as the reporting artefact; it is no longer load-bearing for
  exclusion.

### R8 — Operational integration

**Correctness (in scope):**

- **Freeze.** `plan_security` has no `doc.get("frozen_at")` check.
  `cli.py:276` calls `supervisor.freeze(doc)` for a human-initiated freeze, after
  which `cs.expired` is `False` on the next tick, so the early return does not
  cover it: `dispatchable()` refuses new builders but security still dispatches.
  Add the `frozen_at` guard to `plan_security`.
- **Provider state.** `dispatchable()` gates on `providers.may(doc, "new_builds")`
  and `providers.safe_hold(doc)`; `plan_security` gates on neither, so security
  dispatch continues into a degraded or cooling-down provider. Add the
  equivalent gate for the security provider.
- **Shutdown.** `execute_security` does not check `self.stopping`, so a SIGTERM
  during Phase C still spawns a paid worker. Check it before each spawn.
- **Busy bound.** Phase C does worktree creation and spawn with no
  `declare_busy`. C-18's remediation column explicitly asks for this: *"Where an
  external call must remain slow, bound it with declare_busy so a waiting
  Supervisor is not read as a dead one."*
- **Concurrency cap.** There is no `max_security` in
  `config/experiment.json["concurrency"]`. `execute_security` dispatches one
  attempt per eligible PR per tick with no aggregate cap; with nine tasks that is
  up to nine concurrent paid security workers, against `max_builders: 3` and
  `max_reviewers: 1`. **The value is an open decision — see §8.**

**Quality (optional, not correctness):**

- **Synthetic task title.** `execute_security` passes
  `{"id": task_id, "title": task_id}`, so `prompts/security.md:17`'s
  `Task: {{task_title}}` renders as `TASK-001` rather than the real title. The
  reviewer loses the task description. The real title is available in T1 and
  could travel in the planned identifiers.
- **Misleading branch label.** `prompts.security(..., branch=f"security/{attempt_id}", …)`
  is neither the PR branch nor the actual worktree branch
  (`security/{attempt_id}/{head[:12]}`). Cosmetic — the worktree is checked out
  at the correct SHA regardless.
- **Budget.** Security dispatch is not gated on `budget.hard_stop`. Noting it for
  completeness: **neither is builder nor reviewer dispatch**
  (`budget.metered_call_allowed` is consulted only on the Jev path), so this is
  pre-existing run-wide behaviour, not a Step 6b regression. Out of scope here.

---

## 6. Schemas and lifecycle order

### Schemas

**Security claim — UNCHANGED, still exactly nine keys.**
`sha`, `ordinal`, `attempt_id`, `worker`, `claim_state`, `claimed_at`,
`lease_expires_at`, `verdict`, `reason`. R1 changes only how `worker` is
*derived*, and adds a derivation check to `security_claim_is_valid`.

**Worktree ownership — existing mechanism, no new schema.**
`doc["tasks"][id]["retained_worktrees"][<abs path>] = {"retained_at": <iso>, "why": <str>}`
with the new `why` value `"SECURITY_ATTEMPT_ACTIVE"`. Validation: the path must
be absolute and resolve under `workers.managed_worktree_root()`; the check is
pure-string and runs no subprocess.

**`SecurityObservation` — one new field**, `output_readable: bool`. An
observation dataclass computed in Phase A, not durable state, not the claim.

### Lifecycle order per tick

```
outside lock   A   observe            claim + filesystem + proc scan → observations
outside lock   E1  publish            terminal + not live + unpublished → outcome.json
outside lock   F   release            COMPLETE + published + not live  → remove worktree
─── T1 ──────────────────────────────────────────────────────────────────────────
inside  lock   reap_workers           (unchanged; never sees a security worker)
inside  lock   E2  ingest             durable outcome → verdict, findings, transition
inside  lock   T1  claim              frozen/provider/stopping gates → plan attempt
inside  lock   route_prs, …           (unchanged)
─── T1 commits ──────────────────────────────────────────────────────────────────
outside lock   C   execute            mkdir → worktree → job → spawn
own txn        D   confirm            claim_state → SPAWNED, register worktree
outside lock   execute_merges         (unchanged, still after Phase C as directed)
```

Phase F is placed before T1 so a release and an ingest never contend. Phase D
registers the worktree in the same transaction that flips `claim_state`, so the
two can never disagree.

---

## 7. Tests and mutations the repair must carry

**Crash:**
- crash between `acquire_worktree` and Phase D → next tick re-registers the
  worktree from the claim; no duplicate spawn; no orphan finding
- crash between spawn and Phase D → recovery reads the job file as `SPAWNED`
  (existing rule) and ownership is restored, not re-allocated
- crash after publication, before ingestion → ingestion is idempotent (already
  covered)

**Concurrency:**
- two `flock` holders → exactly one proceeds (40-trial harness above, as a test)
- `SIGKILL`ed holder → the next start succeeds with no manual cleanup
- watchdog restart against a live incumbent → `SUPERVISOR_STAND_DOWN`, not a
  failed-recovery escalation

**Cleanup:**
- release refused while the worker is live
- release refused while the `proc` scan failed (`entries is None`)
- release refused while no durable outcome exists
- `remove_worker` fails → `retained_worktrees` entry survives, failure is in the
  ledger, next tick retries
- `COMPLETE` with a release still pending → ownership still asserted, **no**
  orphan finding

**Provenance (D1):**
- SHA-A completes, head moves to SHA-B, SHA-B spawn fails → **no** outcome is
  published for SHA-B
- worker names for (task, SHA-A, 1) and (task, SHA-B, 1) differ
- `security_claim_is_valid` rejects a claim whose `worker` does not carry its own
  `sha[:12]` and `ordinal`

**Publication retry (D2):**
- publication fails past lease expiry → `INDETERMINATE`, **no** replacement
  dispatch, output still readable
- publication then succeeds → the original attempt ingests; ordinal never advanced

**Live stale-head attempt (D3):**
- head moves while attempt 1 is live → claim **not** replaced,
  `SECURITY_EVIDENCE_HELD` / `STALE_HEAD_ATTEMPT_LIVE`
- old attempt finishes → reclaim proceeds on the next tick

**Operational (R8):**
- `frozen_at` set → no security dispatch
- provider degraded / `safe_hold` → no security dispatch
- `self.stopping` set → Phase C spawns nothing
- concurrency cap reached → surplus attempts wait

**Mutations to prove the new guards can fail:**

| Mutation | Must break |
|---|---|
| drop `sha[:12]` from the worker derivation | provenance tests |
| drop the `worker`↔`sha`/`ordinal` check in `security_claim_is_valid` | forged-claim test |
| drop `output_readable` from the `PROVEN_NOT_RUNNING` condition | D2 replacement test |
| drop the `worker_live or not scan_ok` guard in `plan_security` | D3 test |
| drop the `COMPLETE` clause from `detect_orphans` ownership | pending-cleanup orphan test |
| drop the publication requirement from Phase F | premature-release test |
| replace `LOCK_NB` with a blocking `flock` | singleton refusal test |
| drop the `frozen_at` / `stopping` gates | operational tests |

---

## 8. Genuine remaining decisions

These cannot be resolved from repository policy and need a governance answer
before implementation.

1. **`max_security` concurrency cap.** No value exists anywhere.
   `max_reviewers` is 1 and `max_builders` is 3; security is a paid provider
   review per PR. The number is a budget and rate-limit judgement, not a
   derivable fact.
2. **Publication-blocked escalation bound (R2).** How long an attempt may stay
   publication-blocked before raising `HUMAN_REQUIRED` instead of holding. The
   governed lease (1800 s + 60 s grace) is the obvious candidate but it currently
   means something else — "the worker may still be running" — and reusing it
   would conflate two different claims.
3. **C-number for this work.** D1–D4 are defects in Step 6b itself rather than
   inherited contradictions. Whether they are folded into C-05.3a's row, or
   raised as new audit rows, is an audit-governance call. D5 is arguably already
   inside C-18's remediation column.
4. **Whether R7 (singleton) belongs to C-05.3a at all.** It is a pre-existing
   run-wide defect that Step 6b did not introduce and does not depend on. It is
   included here because the authorising session asked for it, but it could
   equally be its own row and its own change.

---

## 10. Implementation record (2026-09-30)

Authoritative for what the code does. Built in three reviewable groups:
**A** atomic singleton; **B** identity, provenance, superseded ownership and
cleanup together; **C** publication retry and `max_security`.

### 10.1 Governance decisions applied

| Decision | Applied as |
|---|---|
| `max_security = 1` | `config/experiment.json` `concurrency.max_security`, loaded through `ExperimentConfig` like every other concurrency key |
| Count all owned live/in-flight attempts, including stale-head; unknown liveness must not free capacity | The count reads `claim_state != "COMPLETE"` across other PR records — never the `/proc` scan, so a failed scan cannot release a slot |
| Publication failure holds for retry | New finite recovery state `PUBLICATION_PENDING`, checked **before** the lease |
| No new timed escalation threshold | None added. Diagnostics use existing reporting: `SECURITY_EVIDENCE_HELD` outcomes and `SECURITY_OUTCOME_PUBLISHED` |
| Durable attempt ownership + existing `retained_worktrees` | Worktrees use `retained_worktrees` unchanged (same key, same two fields, new `why` value). Lifecycle detail lives in a new durable provenance record, not in that map |
| Preserve the closed nine-key claim | Unchanged. Only the **derivation** of `worker` changed |

### 10.2 Files changed

| File | Change |
|---|---|
| `config/experiment.json` | `concurrency.max_security: 1` |
| `control/config.py` | `SINGLETON_LOCK_PATH`; `max_security` field and load |
| `control/routing.py` | `security_worker_name` (SHA-bound); `_worker_binding`; two new claim diagnostics; `SecurityObservation.output_readable`; `SECURITY_RECOVERY_PUBLICATION_PENDING` |
| `control/gate_evidence.py` | `_read_json_nofollow` (extracted, now shared); provenance name/keys/invariants; `write_security_provenance`; `read_security_provenance`; `scan_security_attempts` |
| `control/supervisor.py` | `_acquire_singleton`; `_reported_pid`; provenance read in Phase A; `_security_output_pending`; `_provenance_matches`; provenance write in Phase C; provenance gate in publish; `_own_security_worktree`; `_security_slots_free`; stale-head-live guard; Phase F (`release_security_worktrees` + helpers) |
| `control/reconcile.py` | `_security_claimed_workers`; claim-based process ownership in both orphan-process loops |
| `tests/test_c05_3_step6b_repairs.py` | **NEW** — 52 tests |
| `tests/test_c16_exception_evidence.py` | Fixture: patch `SINGLETON_LOCK_PATH` |
| `tests/test_watchdog_liveness.py` | Repoint a source-text assertion from the racy guard to the atomic one |
| `tests/test_c05_3_evidence_routing.py` | Fixtures: `max_security`; `attempt_dir` writes provenance |
| `tests/test_c05_3_evidence_claim.py`, `..._recovery.py` | SHA-bound worker-name assertions |

### 10.3 Group A — atomic singleton

`Supervisor.run` no longer reads-then-writes the PID file. `_acquire_singleton`
opens `SINGLETON_LOCK_PATH` and takes `fcntl.LOCK_EX | fcntl.LOCK_NB`, keeping
the handle on the instance for the process lifetime and marking the descriptor
non-inheritable. The inode is never unlinked or replaced. `PID_PATH` is still
written, purely for `watchdog.supervisor_pid` and preflight.

Verified: 20 real concurrent process pairs, exactly one winner every time
(pre-repair: 16 double-starts in 40); lock released by the kernel after
`SIGKILL` with no cleanup; inode stable; descriptor non-inheritable; watchdog
PID reporting intact.

### 10.4 Group B — identity, provenance, ownership, cleanup

**Identity.** `worker = "{task}-security-{sha[:12]}-{ordinal:04d}"`, max 35
characters against the pattern's 64. Because every worker artefact is keyed on
this name, cross-SHA collision becomes **unreachable by construction** rather
than merely detected.

**Compatibility for existing records.** A pre-repair (legacy) worker name is
never rewritten. `security_claim_is_valid` returns
`CLAIM_WORKER_LEGACY_IDENTITY`, the recovery table classifies the claim
`UNKNOWN` — which dispatches nothing and ingests nothing — and the diagnostic
reaches the operator in `SECURITY_EVIDENCE_HELD`. A name matching neither form
returns `CLAIM_WORKER_PROVENANCE_MISMATCH`. Both cases keep the worker **owned**
for orphan purposes, because a possibly-live process must not become ownerless
just because its record is unreadable. In practice no legacy record exists:
T+00 is NOT_STARTED and no run has produced one.

**Provenance.** `security-attempt.json` is written into the attempt directory
after the worktree is acquired and **before** the spawn, recording
`{task_id, pr, sha, attempt_id, worker, worktree}`. Publication refuses unless
it matches the claim exactly; missing, mismatched or unreadable all refuse alike
with `PROVENANCE_UNVERIFIED`. This is what makes the reproduced
materialised-but-never-acquired path fail closed — no acquisition, no
provenance, no publication. Rewriting is idempotent for byte-identical content
(so a crash-resumed Phase C is not stranded) and refused for anything different.

**Ownership.** `reconcile.detect_orphans` gains a pure, subprocess-free
ownership source: any worker named by a `security_evidence` claim, at **any**
`claim_state` including `COMPLETE`. Worktrees are owned via `retained_worktrees`
with `why = "SECURITY_ATTEMPT"`, registered in T1 from the provenance the
lock-free Phase A already read — so a crash between acquisition and Phase D
leaves the resource owned on the very next tick without T1 touching a file.

**Superseded attempts.** `plan_security` refuses to overwrite a valid stale-head
claim while `worker_live` **or** `not scan_ok`, logging
`STALE_HEAD_ATTEMPT_LIVE`. Replacement happens only once the old attempt is
provably dead, so a live attempt is never left unowned — the name-collision fix
and the ownership fix landed together, as the plan required.

**Cleanup (Phase F).** `release_security_worktrees` runs between publication and
T1, holds no lock, and removes a worktree only when liveness is established and
the worker is absent, the outcome is durably published, and the governing claim
is settled. Failure keeps ownership, records
`SECURITY_WORKTREE_RELEASE_FAILED`, and retries next tick. An incomplete
evidence walk releases nothing. Only entries carrying this lifecycle's `why` are
ever dropped.

### 10.5 Group C — publication retry and capacity

`SecurityObservation.output_readable` is computed in Phase A through the *same*
provenance gate the publisher uses, so the classifier can never hold for a
publication the publisher would refuse. `security_recovery_state` returns
`PUBLICATION_PENDING` ahead of the lease check: a review that produced an answer
is never re-run because a **write** failed. An attempt with no readable output
still advances the ordinal exactly as before.

### 10.6 Verification

```
python3 -m unittest discover -s tests   →  Ran 1322 tests ... OK   (was 1270)
git diff --check                        →  exit 0
```

New file `tests/test_c05_3_step6b_repairs.py`: **52 tests**. The
`[FAIL] c16_probe` stderr lines remain the C-16 deliberate-exception probe, not
failures.

All four original defect reproductions re-run against the repaired tree:

| Defect | Before | After |
|---|---|---|
| D1 provenance | SHA-A's `SECURITY_FAIL` published as SHA-B's outcome → `FIX_REQUIRED` | names differ; no SHA-B outcome published |
| D2 publication retry | `PROVEN_NOT_RUNNING` → second paid review | `PUBLICATION_PENDING` → no replacement |
| D3 superseded ownership | live SHA-A claim erased | claim retained, `STALE_HEAD_ATTEMPT_LIVE` |
| D4 singleton | 16/40 double starts | 0/20 double starts |
| D5 orphan ownership | `ORPHAN_WORKTREE` + `ORPHAN_WORKER_PROCESS` | no findings |

**Mutations — 13 run, 13 caught** (all restored byte-identical, SHA-256
verified). `LOCK_NB` removed · SHA removed from the worker name · worker-binding
check removed · publication provenance gate removed · `PUBLICATION_PENDING`
removed · stale-head live guard removed · stale-head guard no longer treats a
failed scan as live · claim-based process ownership removed · worktree
registration removed · Phase F publication precondition removed ·
unknown-liveness release guard removed · failed release still drops ownership ·
capacity counting disabled.

**One mutation initially survived and was closed.** Deleting Phase F's
publication precondition broke nothing, because the test that appeared to cover
it was also blocked by the claim not yet being `COMPLETE` — it proved "release
refused", not "refused *by that rule*". A test was added building the one state
where only that rule can answer (claim `COMPLETE`, outcome since gone from
disk). The mutation is now caught.

### 10.7 Approved proposals deliberately NOT built

- **R2's escalation** (`HUMAN_REQUIRED` after a publication-blocked bound). The
  governing decision was "do not introduce a new timed escalation threshold".
  The hold is therefore indefinite and operator-visible via
  `PUBLICATION_PENDING` — see the limitation below.
- **R8's remaining operational gates** (`frozen_at`, provider state, `stopping`,
  `declare_busy`, prompt task title). Out of the authorised repair scope, which
  named `max_security` only. Still **PROPOSED**; the analysis in §5 R8 stands.

### 10.8 Unresolved limitations

1. **A superseded attempt whose publication never succeeded retains its
   worktree for the rest of the run.** The publisher only ever revisits the
   *current* claim, so its precondition can never be met. This follows the
   stated rule — release only after durable publication — rather than relaxing
   it. It is **not an unowned leak**: the `retained_worktrees` entry persists,
   so the Watchdog never reports it and an operator can see what is held and
   why. Pinned by
   `test_a_superseded_unpublished_attempt_is_retained_not_released`. Whether
   such an attempt may be released without a durable outcome is a governance
   decision.
2. **An indefinitely publication-blocked attempt holds its PR forever**, by
   decision. Visible as a repeating `SECURITY_EVIDENCE_HELD` /
   `PUBLICATION_PENDING`, with the underlying write failure in
   `SECURITY_OUTCOME_PUBLISHED` / `PUBLISH_FAILED`.
3. **`§8` decisions 3 and 4 remain open** — the C-number for this work, and
   whether the singleton repair belongs to C-05.3a at all.
4. **C-18 is still OPEN.** These repairs neither fix nor extend it; Phase F was
   added outside any transaction, and T1 is proven to perform no external work.

## 11. Integration repairs (2026-09-30, second implementation round)

Authoritative over §10 where they differ. Four authorised items; all four
implemented. §10.8's limitations 1 and 2 are resolved by item 11.1 and no
longer stand.

### 11.1 Superseded attempts are published on retry, then cleaned up

**Was:** publication iterated tasks in `EVIDENCE_STATES` and read the *current*
claim, so an attempt whose claim had been replaced was never revisited. Its
answer stayed unwritten and — because a worktree may only be released once
publication is durable — its worktree was retained for the whole run. §10.8
recorded that as a limitation; it was a gap that retry resolves.

**Now:** `publish_security_results` is driven by **durable provenance**
(`gate_evidence.scan_security_attempts`), so every attempt that ever acquired a
worktree is a retry candidate, superseded or not. Each publishes under its own
recorded task, PR, full SHA and attempt — never under whatever the PR record
holds now. The durable-publication requirement for cleanup is **unchanged**;
retry is what lets it be satisfied.

Stale outcomes cannot touch current work: ingestion still goes through
`_current_claim`, which re-checks SHA and attempt id, so a superseded outcome
reaches no finding, no debt and no state transition. Proven by
`test_a_stale_outcome_never_touches_the_current_task`.

`scan_ok` is deliberately ignored for publication — an incomplete walk costs a
retry, it is not an absence claim — and still fails closed for release.

New integrity rule: a provenance record must agree with the directory it was
found in (task, full SHA, attempt are all encoded in the path). A displaced or
corrupted record is dropped and marks the walk incomplete, so nothing is
released on it.

### 11.2 Existing dispatch controls applied

`_security_dispatch_block` gates **new attempts only**, in this order:
`frozen_at` → `stopping` → `providers.may(doc, "review")` →
`providers.usable(doc, <security provider>)`. Every one already governs another
dispatch site; none is new policy. `execute_security` additionally refuses to
spawn once `stopping` is set, for a signal that arrives after T1 commits.

Recovery is explicitly *not* gated: an existing claim still resumes, and
publication, ingestion and cleanup all continue while dispatch is blocked.

**Observed behaviour worth recording:** with the configured provider (`codex`)
in COOLDOWN, the block is `REVIEW_NOT_PERMITTED`, not `PROVIDER_UNAVAILABLE` —
codex's own paused policy sets `allows_review=False`. The `usable` check is
still reachable, because `roles.security.provider` is configuration: pointed at
a provider whose paused policy permits review (`grok`), it is the only thing
standing between a cooling-down provider and a dispatch to it. Both paths are
tested.

**Budget — a real gap, not an enforced control.** `budget.hard_stop` governs
metered OpenRouter/Jev spend through `metered_call_allowed`, its only consumer.
**No control anywhere in this repository gates subscription-provider worker
dispatch on budget** — builder, fixer, reviewer and observer are all ungated.
Security is therefore ungated too, rather than inventing a threshold. Pinned by
`test_no_budget_control_is_claimed_for_worker_dispatch`, which also asserts
`metered_call_allowed` still has exactly one consumer so this statement cannot
silently go stale.

### 11.3 Real prompt context, carried immutably

`routing.SecurityPlan` is a frozen dataclass — `task_id`, `task_title`, `pr`,
`sha`, `attempt_id`, `worker` — validated on construction, including
re-deriving the worker name from task, SHA and ordinal. `plan_security` returns
one instead of a tuple, so nothing but immutable scalars crosses out of T1 and
no reference into the state document survives the commit.

The security prompt now receives the **real task title**; it previously got the
task id, rendering `Task: TASK-001`. The branch reported to the prompt is also
now the branch the worktree is actually on.

### 11.4 Same-prefix full-SHA collision

**Reproduced first.** Two distinct SHAs sharing their first twelve characters
(`abc123def456…0000` and `abc123def456…1111`) produced **one** worker identity
while their attempt directories stayed distinct — and per-attempt provenance
could not catch it, because each record truthfully named its own full SHA. The
first commit's `SECURITY_FAIL`, naming a file only it contained, was published
as the second commit's durable outcome.

**Repaired at identity allocation, not by probability.** The worker name now
carries the **whole 40-character SHA**, making the name exactly as unique as the
directory it addresses. Longest name is 63 characters against
`SECURITY_WORKER_RE`'s 64; `routing.SECURITY_TASK_ID_MAX` (9) is the resulting
task-id ceiling, and a longer id raises at claim construction, surfacing as
`CLAIM_REFUSED` — a visible fail-closed refusal, never a truncated name. Every
task id in this run is 8 characters.

Twelve-character-prefix names are now classified `LEGACY` alongside pre-SHA
names: refused, never rewritten, and still owned for orphan purposes.

### 11.5 Files changed this round

| File | Change |
|---|---|
| `control/routing.py` | full-SHA `security_worker_name`; `SECURITY_TASK_ID_MAX`; prefix form demoted to `LEGACY`; frozen validated `SecurityPlan` |
| `control/gate_evidence.py` | provenance location-integrity check in `scan_security_attempts` |
| `control/supervisor.py` | provenance-driven `publish_security_results`; `_publish_one_security_result` keyed on provenance; `_security_dispatch_block`; `_security_plan`; `stopping` guard in Phase C; real task title and honest branch |
| `tests/test_c05_3_step6b_repairs.py` | 52 → 82 tests |
| `tests/test_c05_3_evidence_routing.py` | `providers.ensure`, `stopping`, `SecurityPlan` fixtures |
| `tests/test_c05_3_evidence_recovery.py` | same fixtures; `SecurityPlan` field assertions; full-SHA names |
| `tests/test_c05_3_evidence_claim.py` | full-SHA derivation assertion |

Three production files and four test files. `config/experiment.json`,
`control/config.py` and `control/reconcile.py` were **not** changed this round.

### 11.6 Verification

```
python3 -m unittest discover -s tests   →  Ran 1352 tests ... OK   (was 1322)
git diff --check                        →  exit 0
```

`tests/test_c05_3_step6b_repairs.py`: **82 tests** (was 52).

**Mutations — 20 run, 20 caught**, all restored byte-identical (SHA-256).
Nine new: full SHA reduced to a 12-char prefix · superseded attempts excluded
from publication (keyed on the real `(sha, attempt_id)` identity) · provenance
location-integrity check removed · outcome attributed to the current head
instead of its own · `stopping` guard removed from Phase C · `frozen_at` gate
removed · `providers.may(review)` gate removed · synthetic task title restored ·
`SecurityPlan` worker re-derivation removed. All eleven still-applicable
mutations from the previous round were re-run and remain caught.

**One mutation initially survived and was sharpened.** Excluding superseded
attempts keyed on `attempt_id` alone broke nothing — because the ordinal
restarts per SHA, so the superseded and current attempts share
`security-attempt-0001`. Re-keyed on `(sha, attempt_id)`, it fails four tests.
The weak mutation was the tell, not the code.

The same-prefix collision reproduction was re-run against the repaired tree:
names differ, no outcome published for the second commit.

### 11.7 Remaining limitations

1. **An attempt that never acquired a worktree owns nothing and publishes
   nothing.** It has no provenance record, so it is not a retry candidate. This
   is correct — no worktree means no worker ran — but it does mean the only
   evidence of such an attempt is the `SECURITY_EXECUTION_FAILED` ledger entry.
2. **No budget control gates security dispatch**, because none exists for any
   subscription-provider worker (§11.2). A run-wide gap, not specific to this
   path, and out of scope to invent.
3. **Publication-blocked holds remain indefinite** by decision — no escalation
   timer. Visible as repeating `SECURITY_EVIDENCE_HELD` / `PUBLICATION_PENDING`
   with `PUBLISH_FAILED` beneath it. Unlike the superseded case, this one has
   always been retryable and now resolves for superseded attempts too.
4. **§8 decisions 3 and 4 remain open** — the C-number for this work, and
   whether the singleton repair belongs to C-05.3a.
5. **R8's prompt/`declare_busy` remainder is still PROPOSED.** `declare_busy`
   was not applied: its governed semantics bound one slow declared operation,
   and Phase C's per-attempt spawn loop is not that shape. Recorded rather than
   forced.
6. **C-18 is still OPEN**, neither fixed nor extended.

## 12. Reuse-branch dispatch-control bypass — repaired (2026-09-30, round 3)

Authoritative over §11 where they differ.

### 12.1 The defect

`§11.2` claimed the dispatch controls gate "new attempts only", with the block
placed **after** the reuse branch on the reasoning that resuming a claim is
"recovery, not new work". **That reasoning was wrong.** `NOT_MATERIALIZED` means
nothing has ever run, and `MATERIALIZED_NOT_SPAWNED` means no job file was ever
written — in both, Phase C acquires a worktree and starts a **paid** worker.

Reproduced before repair:

```
frozen_at set        : True
codex state          : COOLDOWN
providers.may(review): False
recovery state       : NOT_MATERIALIZED
T1 planned attempts  : 1
workers.start_job called for: ['task-001-security-aaaa…-0001']

RESULT: BYPASS CONFIRMED — a PAID security worker spawned
        while the run was FROZEN and the provider was in COOLDOWN.
```

`frozen_at`, `providers.may(review)`, `providers.usable(...)` and
`max_security` were all bypassed. Only `stopping` held, at Phase C.

### 12.2 The repair

1. **`plan_security`** — the dispatch block moves **above** the reuse branch, so
   every recovery state that ends in a spawn is gated. A claim is a
   *reservation*, not permission to spend: what it preserves is identity (same
   ordinal, same attempt directory), which survives the hold unchanged.
2. **Capacity stays on the fresh path only.** A resumed claim already holds its
   slot — `_security_slots_free` counts it when any *other* PR asks — so
   checking it again would double-count one reservation and strand every resumed
   attempt at `max_security = 1`.
3. **`execute_security`** re-evaluates the controls against a fresh
   `store.read()` before spawning. T1 has committed and released the lock by
   then, so a freeze from `ctl freeze` or a provider entering COOLDOWN can land
   between planning and execution. The re-read takes no lock and performs no
   external work — no git, no GitHub, no process, no socket — which is the same
   non-transactional observation Phase A already uses.
4. **Unreadable provider state fails closed** (`CONTROLS_UNREADABLE`). Provider
   state that cannot be read is not provider state that permits spending.

Publication, ingestion and release are reached from elsewhere in the tick and
none passes through `plan_security`, so blocking dispatch never blocks evidence
or cleanup — asserted directly, not assumed.

### 12.3 Files changed

`control/supervisor.py` only. Tests: `tests/test_c05_3_step6b_repairs.py` (82 →
**96**, new `ReuseBranchDispatchControlCase`) and
`tests/test_c05_3_evidence_routing.py` (two Phase C fixtures now commit T1 first,
as production does).

### 12.4 A test that encoded the defect

`test_an_existing_attempt_still_resumes_while_dispatch_is_blocked` asserted that
a frozen run **still** plans a resumed claim, commented "Recovery is not new
work" — the exact wrong reasoning. It has been replaced by
`test_an_existing_claim_is_held_then_resumes_once_the_freeze_clears`, which pins
the governed behaviour: held while frozen with identity intact, resuming once
the control clears.

### 12.5 Verification

```
python3 -m unittest discover -s tests   →  Ran 1367 tests ... OK   (was 1352)
git diff --check                        →  exit 0
```

The pre-repair reproduction now reports `T1 planned attempts: 0 — blocked in T1,
no bypass`, with and without `stopping`.

**Mutations — 6 run, 5 caught, 1 informative survivor, all restored
byte-identical (SHA-256).**

| Mutation | Result |
|---|---|
| Block moved back after the reuse branch | caught (7 failures) |
| Phase C control re-check removed | caught |
| Unreadable provider state fails open | caught |
| Self-exclusion removed (real double-count defect) | caught (13 failures) |
| `stopping` arm removed from the shared block | caught (after adding a direct test) |
| Capacity check *added* to the reuse branch | **survived — see below** |

**The survivor, honestly.** Adding a capacity check to the reuse branch broke
nothing, because `_security_slots_free` excludes the PR's own claim, so at
`max_security = 1` with one PR it returns True either way. Two coexisting claims
cannot arise through the normal path at that cap, so the mutation is inert
rather than undetected. The real double-count defect — removing the
self-exclusion — is caught by 13 tests. Retargeting the mutation was the right
answer; weakening the design to make the first one bite would not have been.

A second survivor (`stopping` removed from Phase C's explicit guard) revealed
that the shared block now covers the same condition. Both are kept — the
explicit guard `break`s out of the loop during shutdown while the block
`continue`s — and a direct test now pins the block's own `stopping` arm so
neither can rot unnoticed.

### 12.6 Remaining limitation

`§11.7` items 1–6 stand unchanged. This repair adds none.

---

## 13. Rehearsal plan

`experiment/REHEARSAL-PLAN.md` — **PROPOSED, NOT AUTHORISED.** Separate checkout,
separate identity (`run-002-rehearsal`), disposable repository, ports 3300–3399.
Six decisions are recorded there as required and unanswered (D5–D10); **D7
(`protocol_present`) blocks all rehearsal work**, because `ctl start` refuses
unless every gate passes and without `started_at` the tick dispatches nothing.

No external repository was created, no host setting changed, no agent launched,
no paid call made. Original Run 002 T+00 remains **NOT_STARTED**.

## 14. C-18 implementation plan (PROPOSED — not implemented)

Read-only inspection, 2026-09-30. Authoritative requirement:
`CONTRADICTION-AUDIT.md` C-18 remediation column — *"Move external effects out
of the state transaction, following the plan → execute → commit shape C-14.1
already uses for merges: claim in state, perform external work with no lock
held, commit the result in a separate transaction that re-verifies the
identifiers it acted on. Where an external call must remain slow, bound it with
declare_busy."*

### 14.0 Why C-05.3a cannot simply be copied

Four structural differences, each found by inspection:

1. **Three T1 entry points, not one.** Security had exactly one
   (`route_evidence`). Dispatch is reached from `dispatch_builder` at `:2517`,
   from `route_prs` at `:1910`/`:1919`, **and from inside `reap_workers`** at
   `:1658`/`:1752` via the role-finished handlers. The reap path is the awkward
   one: it is nested inside another T1 consumer, so a plan list has to travel
   out of a callback rather than out of a top-level loop.
2. **Reservations live in three places**, not one closed object: task fields
   (`worker`, `branch`, `attempts`), PR-record fields (`review_cycles`,
   `repair_cycles`, `reviewed_head`, `reviewed_diff_hash`) and
   `doc["workers"][worker]`.
3. **Worker names derive from counters that currently increment in the same
   transaction as the dispatch.** `review/c{cycle}/{branch}` and
   `{task}-fixer-{n}` mean a plan/execute split must decide whether the counter
   increments at *plan* time (reserving the name, risking a gap if execution
   fails) or at *commit* time (risking two plans choosing one name). C-05.3a's
   ordinal namespace made this free; here it does not.
4. **`allocate_port` is not a pure state reservation** — it performs a real
   `127.0.0.1` bind test. It must split into "choose a candidate under the lock"
   and "prove it binds outside the lock".

### 14.1 Path-by-path analysis

#### `dispatch_builder` (`:264`)

| | |
|---|---|
| **External inside T1** | `prompts.write` (file) · `workers.create_worker` (workmux/git subprocess) · `workers.worktree_path` (workmux subprocess) · `workers.allocate_port` (**real bind test**) · `workers.write_job` (file) · `workers.start_job` (process/tmux spawn) |
| **State-only planning** | migration-lock acquisition (already state-only, C-15) · worker name and branch derivation · candidate port selection from the governed range · `READY` transition |
| **Durable reservation** | A builder claim on the task carrying worker, branch, candidate port and lease. The migration lock is *already* a durable reservation and its C-15 fresh-vs-re-entrant semantics must survive unchanged |
| **Execution outside** | create worktree → resolve path → prove the port binds → write job → spawn |
| **Recheck before commit** | task still exists, still in the dispatched state, still owns this claim, migration-lock ownership unchanged |
| **Crash / duplicate** | Job-file presence is the spawn evidence, as in C-05.3a. `attempts` must increment exactly once per real dispatch — today it increments only on success, which a split must preserve |
| **Ownership** | `doc["workers"]` record plus `retained_worktrees.pop` — both currently in the same transaction as the spawn and must stay consistent with it |

**C-15 interaction is the hazard.** `on_builder_dispatch_failure` releases the
migration lock only for a *fresh* owner, on the stated ground that no builder
has ever run under this ownership. Once the spawn moves outside T1 that ground
must be re-established from durable evidence, not from control flow.

#### `dispatch_reviewer` (`:443`)

| | |
|---|---|
| **External inside T1** | `gh.pr_diff_sha` (GitHub) · `evidence.collect` (**two further GitHub calls**) · `prompts.write` · `workers.acquire_worktree` (git) · `routing.material_diff_hash` (GitHub) · `write_job` · `start_job` |
| **State-only planning** | cycle number, worker name, review branch `review/c{n}/{branch}` |
| **Observation moves to Phase A** | head SHA, evidence items and material diff hash are all *observations*. They must be gathered before the lock, exactly as `observe_security` gathers the outcome and provenance today |
| **Durable reservation** | reviewer claim carrying cycle, worker, head SHA and the observed diff hash |
| **Recheck before commit** | PR still open, head **unchanged since observation**, task still owns the PR, cycle not already advanced |
| **Crash / duplicate** | `review_cycles` must not advance twice for one review. `approval_current`/`review_verdict` are reset at dispatch today — resetting them at plan time while the spawn later fails would leave a PR with no verdict and no reviewer |

**The head-moved race is the hazard.** `reviewed_head` is written from a value
fetched inside T1 today. Once fetched outside, a head that moves between
observation and commit must invalidate the plan — the same rule C-05.3a's
`head_sha != observation.head_sha` check enforces.

#### `dispatch_fixer` (`:519`)

| | |
|---|---|
| **External inside T1** | `prompts.write` · `workers.acquire_worktree` (git, `reuse_if_checked_out=True`) · `workers.allocate_port` (**bind test**) · `write_job` · `start_job` · **`notify_out` at `:588` — a synchronous outbound send** |
| **State-only planning** | repair-cycle number, worker name, finding ids in scope |
| **Durable reservation** | fixer claim carrying cycle, worker, candidate port, `open_finding_ids` |
| **Recheck before commit** | repair-cycle limit still not reached, findings still open, task still owns the PR |
| **Crash / duplicate** | `repair_cycles` exactly once per real dispatch; the limit check must be re-evaluated at commit, not only at plan |

**Worktree reuse is the hazard.** The Fixer deliberately reuses the Builder's
worktree. A plan/execute split must not let a Fixer acquire a worktree a live
Builder still holds — `reuse_if_checked_out=True` makes that silent rather than
an error.

#### `route_prs` (`:1805`)

| | |
|---|---|
| **External inside T1** | `gh.pr_view` at `:1831` |
| **Change** | The PR view is an observation. `tick` already fetches `open_prs` before T1; this call should join it there and be passed in, leaving `route_prs` pure state routing |
| **Recheck** | Routing decisions already re-read the record; the view must carry the head it was observed at so a moved head invalidates the decision |

#### `notify_out` (`:106`, 12 call sites)

| | |
|---|---|
| **External inside T1** | `self.notifier.send` — a synchronous outbound HTTP call on every T1 path that notifies |
| **Sites** | `:207`, `:233`, `:588`, `:1593`, `:1748`, `:1762`, `:1802`, `:2126`, `:2175`, `:2308`, `:2394`, `:2429`, `:2489` |
| **Coupled state** | It also appends the `NOTIFICATION` event **and** increments `doc["counters"]["human_interventions"]`. Effect and state are entangled, so they cannot simply be separated |

**Proposal:** T1 *enqueues* a durable notification intent (state-only, carrying
severity, title, body and an idempotency key) and increments the counter there,
as it does now. A post-commit drain performs the sends and appends a
`NOTIFICATION` event carrying the delivery result. This is the shape the
Watchdog orphan annunciation already uses — a durable pre-send fence, then at
most one external send per authorised attempt — so it is an existing repository
pattern, not a new invention. It also closes the **E1 non-blocking finding**
(Watchdog delivery evidence) as a side effect, though E1 is not in scope here.

### 14.2 Exact affected files

| File | Expected change |
|---|---|
| `control/supervisor.py` | All five paths; the plan/execute/commit split; notification queue and drain |
| `control/workers.py` | Split `allocate_port` into candidate selection (pure) and bind proof (external) |
| `control/state.py` | New durable reservation fields and their allowed transitions |
| `control/routing.py` | `material_diff_hash` called from observation, not from T1 |
| `control/evidence.py` | `collect` called from observation, not from T1 |
| `control/notify.py` | Possibly a queue/idempotency-key helper |

Tests to **retain unchanged** (they pin behaviour the split must preserve):
`tests/test_dispatch_invariant.py` · `tests/test_fixer_dispatch.py` ·
`tests/test_control_plane.py` · `tests/test_merge_boundary.py` ·
`tests/test_merge_invariant.py` · `tests/test_notification_coverage.py` ·
`tests/test_reconcile.py` · `tests/test_intervention*.py` ·
`tests/test_c09_resource_lifecycle.py` · `tests/test_c15*.py`

### 14.3 Staged implementation sequence

Each stage is independently shippable, independently testable, and leaves the
tree green. **No stage may be started under this prompt.**

| Stage | Scope | Why this order |
|---|---|---|
| **1** | `route_prs`: pass the PR view in from `tick`'s existing pre-T1 fetch | Smallest, no reservation design needed, removes one GitHub call from T1 immediately |
| **2** | `notify_out`: durable intent + post-commit drain | Touches every T1 path, so doing it early stops later stages re-treading it. Pattern already exists in the Watchdog |
| **3** | `workers.allocate_port` split: candidate selection (state) vs bind proof (external) | A prerequisite for both builder and fixer; isolated and separately testable |
| **4** | `dispatch_builder` | The simplest full split; C-15 migration-lock semantics are the one hazard and are already well-tested |
| **5** | `dispatch_reviewer` | Hardest: three GitHub observations move to Phase A, plus the head-moved invalidation rule |
| **6** | `dispatch_fixer` | Depends on stages 2 and 3; worktree-reuse hazard handled last with the other two proven |
| **7** | `declare_busy` bounds on any external call that remains slow | C-18 names this explicitly; meaningful only once the calls have moved |

### 14.4 New tests and mutations each stage owes

Per stage: a **fail-before/pass-after** proof that no external effect occurs
inside T1 (the shape of `test_t1_claims_without_touching_the_filesystem`, which
spies on `workers`/`gh`/`prompts` and asserts none is called); crash-between-
plan-and-execute recovery; duplicate-effect prevention across two ticks;
identity recheck refusing a superseded plan; and resource ownership surviving
the crash window.

Mutations that must be caught, at minimum: identity recheck removed · counter
incremented at plan instead of commit · head-moved invalidation removed ·
bind-proof skipped · notification drained before the fence commits · migration
lock released on a re-entrant owner.

### 14.5 What this plan does not decide

Whether C-18 lands before or after C-05.3b; whether `declare_busy` gets a
governed bound per call site; and whether the notification-queue change absorbs
the E1 finding formally. All three are governance calls.

---

## 15. `skills/README.md` — source located (2026-09-30)

Recovered **read-only** from Run 001. Nothing in Run 001 was modified.

| | |
|---|---|
| Path | `~/wellbeing-agent-experiment/agent-run-001/skills/README.md` |
| Commit | `0876aa2ad6c0ffc9bd028a6b5626465c7219b3ea` — *"experiment: freeze Run 001 Protocol v1.0"* |
| Blob | `efa42493c1f6059fea42868e162d5bad0e1a1289` |
| SHA-256 | `46d3833e5460430ec4c4f32e773a5d79e5b46eba70cba794d2bb0ddc3909329c` |
| Size | 1569 bytes · working tree matches the committed blob exactly |

**Content:** eight skill definitions — `scoped-implementation`,
`codebase-navigation`, `verification`, `accessibility`, `systematic-debugging`,
`handoff`, `jev-integration`, `companion-ai` — under the header *"These are
methods, not authority. Contract/spec always wins."*

**Faithful carry-over.** Copy the blob byte-for-byte; do not rewrite, reformat
or extend it. Record the source commit, blob id and SHA-256 above in the import
evidence so the provenance is independently checkable — which is precisely what
C-06's historical 49-file manifest could not provide.

**Two things this does NOT settle:**

1. **It is a Protocol v1.0 artefact.** Importing Run 001 source into Run 002 is
   governed by **C-06**, and that decision has not been made for this file. The
   content is Run-002-compatible on its face (it names Jev, the companion and
   accessibility, all of which exist here), but compatibility is not
   authorisation.
2. **Manifest impact: none.** Verified — `skills/README.md` appears in neither
   `manifest.PRODUCT_SPEC_FILES` nor `SEVERITY_POLICY_FILES`, and is not a
   prompt file, so importing it changes no frozen-input hash.

`prompts/builder.md` already references it in its reading order, so Run 002 is
currently pointing agents at a file that does not exist here.

---

## 17. `skills/README.md` imported (2026-10-01)

**DONE.** Section 15 located the source; this carries it over.

| | |
|---|---|
| Source repo | `~/wellbeing-agent-experiment/agent-run-001` — read-only throughout |
| Commit | `0876aa2ad6c0ffc9bd028a6b5626465c7219b3ea` |
| Blob | `efa42493c1f6059fea42868e162d5bad0e1a1289` |
| Method | `git --git-dir=<run-001>/.git cat-file blob <blob> > skills/README.md` |
| Destination | `skills/README.md`, 1569 bytes |
| Destination SHA-256 | `46d3833e5460430ec4c4f32e773a5d79e5b46eba70cba794d2bb0ddc3909329c` — equals the expected value |

Three provenance facts were checked independently, not assumed: the commit
resolves `skills/README.md` to exactly that blob; the blob is 1569 bytes; the
written file's SHA-256 matches. The extraction reads the object store only —
no worktree, no ref, no lock, no checkout, and nothing of Run 001's runtime,
processes, ports or credentials. `git status` in Run 001 is unchanged.

Provenance is recorded through the existing import mechanism,
`experiment/imported-source.sha256`, as a `#`-comment block naming commit,
blob, size, method, verification, authorisation and C-06 reconciliation, plus
the machine-checkable hash line. `sha256sum -c` passes on all twelve entries
— `sha256sum` treats `#` lines as comments, so the file stays verifiable.

**Authority boundaries preserved.** The file is copied byte-for-byte and
carries its own header, *"These are methods, not authority. Contract/spec
always wins."* Nothing was rewritten, reformatted or extended, so no new skill
authority is created and Protocol v2 contracts continue to outrank it.

**Manifest impact: none, verified rather than asserted.** `product_spec_sha`,
`severity_policy_hash`, `task_graph_hash` and `pr_template_hash` are
byte-identical before and after the import.

**Gate effect.** `protocol_present` now **PASSES** — `gate_protocol()` returns
`ok=True`, `missing: []`. It was the last of the two absent `SPEC_FILES`
entries; §16's remaining blocker is closed.

## 18. C-18 stage 1 — implemented (2026-10-01)

**Stage 1 only.** C-18 stays **OPEN**: stages 2–7 (§14.3) are untouched.

### 18.1 State before this session

Stage 1 was **not** implemented. `route_prs` still called
`gh.pr_view(self.cfg.github_repo, pr_number)` directly, inside T1, at
`control/supervisor.py:1831`. §14 was a plan, not a record.

### 18.2 What changed

`Supervisor.observe_closed_prs(snapshot, open_prs)` is new. It runs in
`tick()` before the transaction is opened, from the snapshot `tick()` already
reads for C-05.3a Phase A, and observes exactly the pull requests `route_prs`
would have asked about: a task's own PR, with a record, not already merged,
and absent from the open list. It **moves** a GitHub call rather than adding
one — an open PR, a merged record, a task with no PR and a PR with no record
are each proven to trigger no call at all.

`routing.ClosedPrObservation` is a frozen, validated dataclass carrying
`pr_number`, `observed_at` and a recursively read-only view
(`MappingProxyType`). Construction refuses a view that carries a different
pull request's `number`, a view with no `number`, and a `headRefOid` that is
not a full 40-character hex SHA. `observed` and `head_sha` are derived, not
stored twice.

`route_prs` gained a **required** `pr_observations` parameter and makes no
GitHub call. It was made required rather than defaulted deliberately: a
default would have let a caller silently lose external-merge detection.

### 18.3 How each requirement is met

| Requirement | How |
|---|---|
| No fallback GitHub call inside T1 | The `pr is None` branch reads the map or `continue`s. Pinned by a test patching the whole `gh` module and asserting `mock_calls == []` |
| Missing observation | Deferred: `PR_ROUTING_DEFERRED` / `OBSERVATION_MISSING`, no state change, retried next tick |
| Failed observation | Kept as a present observation with `observed=False` — the same "GitHub could not be observed" `merge_invariant.classify` already handles. **Failed is not collapsed into missing**, and neither fabricates a merge |
| Inconsistent observation | Dropped at the observation phase with `PR_OBSERVATION_REJECTED` / `OBSERVATION_INCONSISTENT`; `route_prs` then finds nothing and defers |
| Bound to the correct PR and full SHA | Validated on construction, plus a second `observation.pr_number != pr_number` check in T1. One PR's answer cannot reach another's record |
| Not proof of the current head | The observation is a within-tick value, never written to durable state (asserted), and each tick observes again (asserted across two ticks) |
| Downstream head-change checks preserved | `execute_merges`'s `merge_no_longer_ready` plus its own fresh `gh.pr_view` are untouched. That re-read is deliberately inside each merge's own transaction (C-14.1), and C-18's row excludes the merge from its finding |
| No unrelated GitHub calls migrated | `dispatch_builder`, `dispatch_reviewer`, `dispatch_fixer` and `notify_out` are unchanged |

### 18.4 Files changed

| File | Change |
|---|---|
| `control/routing.py` | `_frozen_json`; `ClosedPrObservation`; `MappingProxyType` import |
| `control/supervisor.py` | `observe_closed_prs`; `tick` takes the observation before the lock and passes it in; `route_prs` signature and the `pr is None` branch |
| `tests/test_c18_stage1_pr_observation.py` | **NEW** — 37 tests |
| `tests/test_merge_boundary.py` | Call sites pass the observation map; the two closed-PR helpers drive `observe_closed_prs` then `route_prs`, as `tick` does |
| `tests/test_notification_coverage.py` | Same; `_pr_view_result` now carries `number`, which real `gh pr view` always returns |
| `tests/test_dispatch_invariant.py`, `tests/test_fixer_dispatch.py` | Open-PR call sites pass `{}` |

No assertion in any existing test was weakened.

### 18.5 Verification

```
python3 -m unittest discover -s tests   →  Ran 1404 tests ... OK   (was 1367)
git diff --check                        →  exit 0
preflight.gate_protocol()               →  ok=True, missing: []
```

Focused: `test_c18_stage1_pr_observation.py` **37** · `test_merge_boundary.py`
35 · `test_notification_coverage.py` 24 · `test_dispatch_invariant.py` 22 ·
`test_fixer_dispatch.py` 11 · `test_merge_invariant.py` 67 ·
`test_reconcile.py` 24 · `test_control_plane.py` 81. All OK.

The `[FAIL] c16_probe` stderr lines remain the C-16 deliberate-exception
probe, not failures.

**Mutations — 3 run, 3 caught**, all restored byte-identical (SHA-256
verified before and after):

| Mutation | Result |
|---|---|
| **M1 — the required one:** `gh.pr_view` restored inside `route_prs`, inside T1 | caught, 7 tests. The flock-probe guard reports `[False, True]` against the expected `[False, False]` |
| M2 — a missing observation fails open (routed as "unobserved" instead of deferred) | caught, 3 tests |
| M3 — the PR-number binding check removed from `ClosedPrObservation` | caught, 4 tests |

The M1 guard is measured, not asserted structurally:
`test_every_pr_view_in_a_tick_happens_with_the_state_lock_free` probes the
**real** `flock` on `Store.lock_path` at the moment of every `gh.pr_view`
call, and a companion test proves the probe can detect a held lock — so the
guard cannot pass because the probe is broken.

### 18.6 What stage 1 does NOT claim

**A tick is not yet free of GitHub calls under a lock.** Measuring the lock
during a merge-ready tick showed `gh.pr_view` called with the lock held, twice
— those are `execute_merges`' own re-observation and `attempt_merge`'s
post-merge observation, both inside each merge's **own** short transaction by
C-14.1's design, and both explicitly outside C-18's finding. Separately,
`dispatch_reviewer` still calls `gh.pr_diff_sha` and `evidence.collect` inside
T1; that is **stage 5**, not a stage 1 regression. Stage 1's guarantee is
scoped to the `route_prs` path and the tests say so.

Stages 2–7 remain unstarted. C-18 remains **OPEN** and still carries
*"REQUIRED BEFORE: unattended multi-cycle rehearsal, the 5-hour unattended
stress test, and T+00."*

## 19. C-18 stage 2 — implemented (2026-10-01)

> **§20 corrects three defects in this section and wins over it**: the
> drain limits were per call rather than per tick, the send timeout was
> measured before the fence, and the real transport turned an ambiguous
> outcome into a retried failure. Read §20 with this.

**Stage 2 only.** C-18 stays **OPEN**: stages 3–7 (§14.3) are untouched.

### 19.1 The defect

`notify_out` called `self.notifier.send` synchronously, on whatever
transaction the caller held — twelve call sites, most of them reached from
T1. A hanging Discord call stalled the state transaction and with it every
task in the run, and no `declare_busy` bound covered it.

### 19.2 What changed

`notify_out` now **queues** and does not deliver. It builds a durable intent
in `doc["notifications"]`, committed atomically with the state change that
produced it, and `drain_notifications()` delivers afterwards with no lock
held.

The lifecycle is the one `watchdog.annunciate_orphans` already established —
`PENDING → ATTEMPTING → DELIVERED` behind a durable pre-send fence — with
this stage's governed additions.

### 19.3 Governed decisions applied

| Decision | Applied as |
|---|---|
| Retry indefinitely with backoff; **never drop** | `notify.backoff_seconds`: 30 s doubling to a 1800 s cap. Nothing is removed from the queue, so notification coverage cannot be lost |
| Retry **only** after a durably recorded failure | Convergence promotes `ATTEMPTING → PENDING` only on a ledger `NOTIFICATION`/`FAILED` event for the attempt that is fenced |
| An ambiguous outcome stays suppressed | A send that began and then raised records `NOTIFICATION`/`AMBIGUOUS` — deliberately **not** `FAILED` — so the intent stays `ATTEMPTING` and is never resent |
| Bound each tick's drain | `DRAIN_BUDGET_SECONDS = 10.0` **and** `DRAIN_MAX_ATTEMPTS = 20`; whichever binds first stops the drain |
| Enforce the budget on each send | Each send receives `timeout=<remaining budget>`. `notify.Notifier.send` gained a `timeout` parameter and passes it to `http.post_json`, whose 20 s default would otherwise be twice the whole budget and make it decorative |
| Report the backlog | Every drain returns and logs `pending` and `oldest_pending_age_seconds` in `NOTIFICATION_DRAIN` |
| No queue trimming in this stage | None added. Queue growth is recorded as an operational limitation in §19.7 |

### 19.4 Delivery and crash-recovery semantics

**The guarantee is AT MOST ONCE per durably authorised attempt.** It is
deliberately **not** exactly-once: a Discord webhook accepts no idempotency
key, so no implementation on this side could honour that claim.

| Window | Behaviour |
|---|---|
| Transaction rolls back | The intent was in `doc`, so it is discarded with everything else. Nothing deliverable exists, and the `human_interventions` increment rolls back with it |
| Fenced, send not started (budget spent) | Cannot occur. Intents are fenced **one at a time**, immediately before their own send, so an intent the budget never reached is untouched — still `PENDING`, still `attempt: 0` |
| Send returned failure | `NOTIFICATION`/`FAILED` appended, intent returns to `PENDING` with a backoff gate. Committed control-plane state is untouched |
| **Delivered, then the recording commit is lost** | The ledger `DELIVERED` event survives. The next drain's convergence reads it and settles the intent. **No resend** |
| Failed, then the recording commit is lost | Convergence reads the `FAILED` event for that attempt and returns the intent to `PENDING` with backoff |
| Send began, outcome unknown (exception mid-flight) | `AMBIGUOUS` recorded; the intent stays `ATTEMPTING` and is **never** retried. Possible loss is accepted over possible duplication, exactly as the watchdog policy states |
| Process restart | Nothing lives in memory. A fresh Supervisor reads the committed queue and continues |

Protocol v2 §Discord — *"Ledger append succeeds before Discord
notification"* — is **strengthened**: the business event and
`NOTIFICATION_QUEUED` are both durable at least one commit before any send.

### 19.5 Files changed

| File | Change |
|---|---|
| `control/notify.py` | `timeout` on `Notifier.send`; queue primitives — `QUEUE_KEY`, statuses, `backoff_seconds`, `queue`, `new_intent`, `is_due`, `due_intents`, `backlog` |
| `control/state.py` | `initial_document` gains `"notifications": {}` |
| `control/supervisor.py` | `notify_out` queues instead of sending; `drain_notifications`, `_converge_notifications`, `_notification_event`, `_fence_next_notification`, `_deliver_notification`, `_record_notification_outcome`, `_notification_backlog`; two drain points in `tick`; `secrets` and `redact` imports |
| `control/cli.py` | `cmd_freeze` drains after its transaction, so `ctl freeze` stays self-contained |
| `tests/test_c18_stage2_notification_queue.py` | **NEW** — 48 tests |
| `tests/test_notification_coverage.py` | One case re-pointed (see below) |
| `tests/test_intervention_integration.py` | One assertion re-pointed (see below) |

**Two drain points, deliberately.** The first is after `execute_merges` and
**before** the slow provider work, so a `HUMAN_REQUIRED` message never waits
behind an Observer that may legitimately run for minutes. The second is at
the very end of the tick, because `consult_jev` has its own transaction and a
crossed budget threshold there raises `HUMAN_REQUIRED`; without it that
hard-stop escalation would wait for the next tick. The second drain costs
nothing when nothing was queued.

**Test-interface changes, both re-pointed rather than weakened:**

- `test_primary_business_event_survives_a_discord_send_failure` asserted
  `notifier.send` fired inside `attach_pr` — the exact call this stage moves.
  It now asserts the intent is durably queued and still asserts `PR_OPENED`
  and the state change survive. The end-to-end version — a delivery that
  really fails during the drain, against a real store, rolling nothing back
  and leaving the intent retryable — is in the new file and is stronger.
- `test_counter_increments_exactly_once_even_on_recurrence` counted sends;
  it now counts queued intents. Exactly one intent means exactly one send can
  ever happen — the same claim, checked one step earlier. Both
  `human_interventions` assertions are untouched.

### 19.6 Verification

```
python3 -m unittest discover -s tests   →  Ran 1452 tests ... OK   (was 1404)
git diff --check                        →  exit 0
```

Focused: new file **48** · `test_notification_coverage` 24 ·
`test_intervention_integration` 35 · `test_intervention` 107 ·
`test_intervention_resolution` 47 · `test_c18_stage1_pr_observation` 37 ·
`test_merge_boundary` 35 · `test_control_plane` 81 ·
`test_c09_resource_lifecycle` 67 · `test_watchdog_reconcile` 84. All OK.

**Mutations — 6 run, 6 caught**, all restored byte-identical (SHA-256
verified before and after):

| Mutation | Caught by |
|---|---|
| **M1** delivery moved back inside the caller's transaction | 15 tests. The flock probe reports `[True, False]` against the expected `[False, False]` |
| **M2a** the intent is never persisted | 28 tests |
| **M2b** a failed delivery is acknowledged as `DELIVERED` | 4 tests |
| **M3** duplicate intent creation | 15 tests |
| M4 the per-send timeout is not bounded by the remaining budget | 1 test |
| M5 an ambiguous outcome is recorded as a failure and resent | 1 test |

All deliveries in every test are mocked; nothing reaches the network.

### 19.7 Limitations and one newly named residual

1. **The queue only grows.** Nothing is trimmed, by decision, so coverage
   cannot be lost — but a long outage leaves a large `doc["notifications"]`,
   and `state.json` is fully rewritten on every transaction. Delivered
   intents are retained too. Visible through `NOTIFICATION_DRAIN`'s
   `pending` and `oldest_pending_age_seconds`. Trimming is a later decision.
2. **An ambiguous send can lose a notification.** Stated plainly because the
   alternative is duplicate delivery and the destination offers no
   idempotency key. `NOTIFICATION`/`AMBIGUOUS` records every instance.
3. **`merge_invariant.annunciate` still sends inside T1 — newly named, not
   introduced here.** It calls `notifier.send` directly rather than through
   `notify_out`, and `Supervisor.invariant_violated` reaches it from
   `route_prs`. C-18's row enumerates "every T1 path reaching
   `self.notify_out`", so this sits outside stage 2's scope, and moving it
   would change C-14.2's annunciation evidence. It is the one remaining
   synchronous outbound send under T1 and needs its own authorised change.
4. **`declare_busy` bounds are still stage 7.** Not applied here.

Stages 3–7 remain unstarted. C-18 remains **OPEN** and still carries
*"REQUIRED BEFORE: unattended multi-cycle rehearsal, the 5-hour unattended
stress test, and T+00."*

## 20. C-18 stage 2 corrections (2026-10-01)

Authoritative over §19 where they differ. Three findings from §19's own
report, each reproduced by inspection before being changed. Scope was the
corrections only — no queue redesign, and the `merge_invariant` residual
(§19.7 item 3) is untouched and still open.

### 20.1 Finding 1 — the per-tick limits were per-call

**Confirmed.** `drain_notifications` computed `deadline` and `attempted`
fresh on entry, and `tick()` calls it twice, so a tick could spend **20 s and
40 attempts** against approved limits of 10 s and 20.

**Fixed.** `_DrainAllowance` holds the deadline and the remaining attempt
count. `tick()` mints exactly one with `new_drain_allowance()` and passes the
same object to both drain points. `drain_notifications(allowance=None)` mints
its own when called without one — which is what `ctl freeze` does, since a
one-shot command has no tick to share with.

### 20.2 Finding 2 — remaining time was measured before the fence

**Confirmed.** `remaining` was computed, then `_fence_next_notification()`
ran — a state transaction that waits on the flock and can itself consume the
remainder — and the stale figure was handed to the send.

**Fixed.** The allowance is recomputed immediately before delivery. If the
fence consumed it, **no send is made** and `_release_unsent_notification`
returns the intent to `PENDING`.

That release is the subtle part. The intent is already `ATTEMPTING`, and the
ambiguity rule would otherwise suppress it forever — but here we *know* no
send began, so it is freely retryable. It is recorded as its own ledger
outcome, `NOTIFICATION`/`NOT_ATTEMPTED`, **not** as `FAILED`: nothing failed,
so no backoff is applied and the intent is eligible again immediately. The
ledger append precedes the state write, so convergence releases the intent
even if that write is lost — the same restart-safety shape as every other
outcome here.

**What the budget actually bounds, stated accurately.** It bounds when a new
send may *start*, and it bounds each socket operation. It does **not**
guarantee the drain returns within 10 s. `http.post_json` passes its timeout
to `urllib.request.urlopen`, which applies it per socket operation — connect,
and each read — not to the request as a whole, so a slowly trickling response
can exceed it in total. The honest bound is **the budget plus at most one
in-flight send**. Tightening that needs a different transport, not a
different number.

### 20.3 Finding 3 — the transport collapsed ambiguity into failure

**Confirmed, and it was the serious one.** `http._send` caught every
non-HTTP error in one bare `except Exception` and returned
`Response(ok=False, status=0)`. A read timeout — where the webhook may have
been delivered in full and only the response lost — was therefore
indistinguishable from a server that positively rejected the message. The
drain recorded `FAILED`, convergence returned the intent to `PENDING`, and
it was **retried**. §19 claimed ambiguity was suppressed; through the real
transport it was not, because the only ambiguity path was `notifier.send`
raising, which in production it never does.

**Fixed at the transport, which is the only layer that knows.**
`http.Response` gains `ambiguous: bool = False` — defaulting False, so
telemetry, Jev, manifest and reconcile are untouched. `_send` classifies:

| Outcome | Classification | Why |
|---|---|---|
| `HTTPError` (4xx/5xx, incl. 429) | **known failure** | The server answered. The request arrived and was rejected |
| `URLError(ConnectionRefusedError)` | **known failure** | Nothing left the machine |
| `URLError(socket.gaierror)` | **known failure** | The name never resolved |
| `URLError(<anything else>)`, e.g. reset | **ambiguous** | May have been transmitted |
| Bare `TimeoutError` / `socket.timeout` | **ambiguous** | The archetypal case |
| Anything unforeseen | **ambiguous** | Fail toward a suppressed retry, never a duplicate |

`_NOT_TRANSMITTED` is an **allow-list**, deliberately: an unrecognised
failure costs a suppressed retry, never a duplicate delivery. A deny-list
would fail the other way.

Distinguishing refused/DNS from timeouts matters materially: treating an
ordinary outage as ambiguous would strand a whole outage's notifications at
`ATTEMPTING` and never retry them, which is the coverage loss the "never
drop" decision exists to prevent.

`Notifier.send` propagates `ambiguous` (never true when `ok`), and
`_deliver_notification` records `AMBIGUOUS` and leaves the intent
`ATTEMPTING` instead of recording `FAILED`.

### 20.4 Files changed

| File | Change |
|---|---|
| `control/http.py` | `socket` import; `Response.ambiguous`; `_NOT_TRANSMITTED`; three-way classification in `_send` |
| `control/notify.py` | `Notifier.send` returns `ambiguous` |
| `control/supervisor.py` | `_DrainAllowance`; `new_drain_allowance`; `drain_notifications(allowance)`; post-fence recompute; `_release_unsent_notification`; `NOT_ATTEMPTED` in `_converge_notifications`; ambiguity branch in `_deliver_notification`; one shared allowance across both `tick` drains |
| `tests/test_c18_stage2_notification_queue.py` | 48 → **73** tests: three new classes, and `_budget_clock` moved to the shared harness |

No existing assertion was changed or weakened. `control/cli.py` was not
touched this round — `ctl freeze` already calls `drain_notifications()` with
no argument, which is exactly the independent allowance it should get.

### 20.5 Verification

```
python3 -m unittest discover -s tests   →  Ran 1477 tests ... OK   (was 1452)
git diff --check                        →  exit 0
```

Focused: Stage 2 file **73** · `test_c18_stage1_pr_observation` 37 ·
`test_notification_coverage` 24 · `test_intervention_integration` 35 ·
`test_jev` 10 · `test_control_plane` 81 · `test_watchdog_reconcile` 84 ·
`test_c09_resource_lifecycle` 67 · `test_merge_boundary` 35 ·
`test_manifest` 22. All OK.

**Mutations — 6 run, 6 caught**, all restored byte-identical (SHA-256 over
`supervisor.py`, `notify.py`, `http.py` before and after):

| Mutation | Caught by |
|---|---|
| C1 each drain mints its own allowance again | 4 tests |
| C2 remaining time measured before the fence again | 4 tests |
| C3 a fenced-but-unsent intent left suppressed | 3 tests |
| C4 transport ambiguity treated as a known failure | 3 tests |
| C5 the transport stops marking timeouts ambiguous | 3 tests |
| C6 a refused connection wrongly marked ambiguous | 4 tests |

Transport behaviour is verified with `urllib.request.urlopen` mocked
**beneath the real `Notifier` and the real `http` wrapper** — not by making
`notifier.send` raise, which proves nothing about production, where it never
raises. Nothing reaches the network.

### 20.6 Stage 2 limitations after these corrections

1. **The 10-second budget is not a hard end-to-end deadline** — budget plus
   at most one in-flight send (§20.2). A transport-level limitation, now
   stated rather than implied.
2. **An ambiguous send can still lose a notification.** Unchanged and
   deliberate; every instance is recorded as `NOTIFICATION`/`AMBIGUOUS`.
   Nothing resends it, and nothing ever will without an idempotency key.
3. **The queue only grows.** No trimming, by decision. Delivered intents are
   retained too. Visible via `pending` / `oldest_pending_age_seconds`.
4. **A budget-exhausted fence consumes an attempt number.** `attempt` counts
   fences, not sends, so that ledger events stay uniquely keyed. It earns no
   backoff, but it does advance the number a later real failure backs off
   from. Practically negligible — the path fires only when the fence
   transaction itself eats the remainder.
5. **`merge_invariant.annunciate` still sends synchronously inside T1.**
   Unchanged, out of scope, still the one remaining in-T1 outbound send.

## 21. C-18 stage 3 — implemented (2026-10-01)

**Stage 3 only.** C-18 stays **OPEN**: stages 4–7 are untouched, and so is the
`merge_invariant` residual.

### 21.1 The requirement, and what it does and does not deliver

§14.0 item 4: `allocate_port` *"is not a pure state reservation — it performs
a real `127.0.0.1` bind test. It must split into 'choose a candidate under
the lock' and 'prove it binds outside the lock'."*

Stage 3 delivers the split. It does **not** migrate the callers, and this
document does not claim otherwise: `dispatch_builder` and `dispatch_fixer`
still call the composed `allocate_port` inside T1, so **a bind still happens
under the state lock today**. Moving those two call sites is stages 4 and 6.
What stage 3 provides is the under-lock half, proven to perform no bind,
which those stages need before they can move anything.

### 21.2 The split

| Function | Half | Contract |
|---|---|---|
| `select_port_candidates(doc, lo, hi)` | under the lock | Returns `(candidates, exhaustion_reason)`. No bind, no network, and **no write** |
| `probe_port(port)` | external | One real `127.0.0.1` bind, socket closed immediately. Returns `bool` |
| `allocate_port(doc, lo, hi)` | composed | **Unchanged** signature, return contract and exhaustion message |

**Why selection writes nothing, and why that is the safety property.**
Choosing a candidate makes no durable claim on it, so a failed probe has no
reservation to leak and no cleanup path that could be skipped. The durable
claim on a port is still made exactly where C-09 puts it — in the job file
written before spawn. This is what lets allocation fail conservatively
without any compensating logic.

**Point-in-time, not a guarantee.** `probe_port` closes the socket
immediately; all it establishes is that nothing held the port at that
instant. The probe-to-worker-bind TOCTOU window is unavoidable without a
reservation daemon, and a foreign listener taking the port inside it is
surfaced by C-09's reverse listener detection rather than prevented here.
That was already true of the single-function allocator; the split neither
widens nor narrows the window.

### 21.3 What is preserved, verified rather than assumed

- **Isolation range** — still `hostcheck.read_candidate_port_range()` by
  default; pinned by a test asserting it is consulted.
- **Durable ownership and reservations** — committed worker-record ports and
  `_reserved_job_file_ports` are unchanged, including every fail-closed
  branch: failed `/proc` scan, unreadable-but-present status, ambiguous agent
  identity.
- **Collision handling** — record ownership and job-file reservation exclude
  independently; an excluded port is never even probed.
- **Release behaviour** — untouched. Nothing in the lease RETRY/FAIL path or
  the job-file port recovery was modified.
- **Legacy compatibility** — `allocate_port` keeps its exact signature,
  `(port|None, reason)` contract and governed exhaustion message. All **67**
  existing C-09 lifecycle tests pass unchanged, with no edit to that file.

### 21.4 Files changed

| File | Change |
|---|---|
| `control/workers.py` | `select_port_candidates`; `probe_port`; `allocate_port` recomposed from the two |
| `tests/test_c18_stage3_port_split.py` | **NEW** — 34 tests |

Two functions added, one reimplemented. No other production file was touched
— deliberately, since touching `control/supervisor.py` would be stage 4 or 6.

### 21.5 Verification

```
python3 -m unittest discover -s tests   →  Ran 1511 tests ... OK   (was 1477)
git diff --check                        →  exit 0
```

Focused: new file **34** · `test_c09_resource_lifecycle` **67** (unchanged,
the legacy-compatibility proof) · full suite 1511.

**Mutations — 6 run, 5 caught directly, 1 inert survivor retargeted and then
caught.** All restored byte-identical (SHA-256 over `control/workers.py`).

| Mutation | Result |
|---|---|
| **M1 (required): the bind restored inside the under-lock half** | **caught.** The transaction-boundary test reports `[True × 10] != []` — ten binds observed while the real `flock` was held |
| M2 record-owned ports no longer excluded | caught, 7 tests |
| M3 a failed `/proc` scan no longer retains job-file ports | caught, 2 tests |
| M4 the probe never fails | caught, 4 tests |
| M5 allocation ignores the probe result | caught, 4 tests |
| M6 the probe stops closing its socket via the context manager | **survived — see below** |
| M6b the probe leaks a socket reference that outlives the call | caught, 8 tests |

The transaction-boundary guard is measured, not structural: a spy socket
records `lock_is_held(store.lock_path)` at every `bind`, and a companion test
proves the spy *would* report `[True]` for a bind deliberately made inside a
transaction — so the guard cannot pass because the spy is broken.

### 21.6 Defects encountered and the one unsuccessful approach

Recorded because they happened, not reconstructed.

1. **A test bug of mine, caught by the test failing.**
   `mock.patch.object(workers, "socket", wraps=socket)` wraps the *module*,
   so `socket.socket(...)` returned a wrapping Mock whose `bind` rejected a
   tuple address with `TypeError`. Replaced with the same default-MagicMock
   spy the sibling purity test uses, which is what actually answers "was a
   socket opened". Production code was not changed for this.

2. **Mutation M6 survived, and the honest reason is that it is inert.**
   Replacing `probe_port`'s `with socket.socket(...)` by a bare socket that
   is never explicitly closed broke nothing — CPython's refcounting closes
   the socket the moment the local goes out of scope at return, so the leak
   has no observable effect. Retargeted at the genuine defect shape, a
   reference that outlives the call (M6b), it is caught by 8 tests including
   `test_the_probe_releases_the_port_immediately`, which was written for
   exactly this property. The weak mutation was the tell, not the code — the
   same conclusion §12.5 reached about its own inert survivor. No test was
   weakened and no extra machinery was added to make the inert version bite.

No other approach was tried and abandoned.

### 21.7 Remaining integration boundaries

1. **Port binds have NOT left T1.** Both dispatch sites still call the
   composed `allocate_port` under the lock. Pinned by
   `StageThreeDidNotMigrateTheDispatchSites`, a deliberate boundary marker
   that fires if the call sites change — so "stage 3 done" can never be read
   as "binds have left T1". Stages 4 and 6.
2. **Selection is pure of network and of writes, not of all I/O.**
   `_reserved_job_file_ports` still reads the worker-log directory and scans
   `/proc`. Those are local reads, they are unchanged, and C-18's row names
   only the bind test — but they do still happen inside T1, so the under-lock
   half is "no network, no mutation", not "no syscalls".
3. **The TOCTOU window is unchanged**, by design (§21.2).
4. **Stages 4–7 and the `merge_invariant` residual are untouched.**

## 22. C-19 — the Jev implementation is not Jev (2026-10-01)

> **§23 refines this section and wins over it.** Review found four defects
> in the §22 proposal: unknown spend was left uncounted against a hard
> ceiling; the severity gap was misdiagnosed as needing a new validity
> check when one already exists and is already wired; the proposal implied
> all five decision kinds would become live calls, which the governed
> evidence path contradicts; and a post-response cost check was described
> as a control when it only detects. §22.1's discovery record and §22.2's
> documentation findings stand unchanged. Read §23 for the proposal.

**Status: RECORDED, NOT REPAIRED.** Audit row C-19. No implementation
change has been made, no call attempted. This section is the repair
proposal and the criteria drafted for review. Nothing here is approved.

### 22.1 What failed, and how it was found

Not found by a test. Found by a question.

The human reported signing up to a TypeSafe console account for Jev. A
read-only clarification of what a live `jev_minimal_decision` gate would
actually verify then established that **this repository has never talked to
Jev**, and that the word "TypeSafe" appears in no file in it.

Four facts, each from the tree:

| | |
|---|---|
| `product/AI.md` line 3 — **frozen, hash-pinned** | *"Jev via **OpenRouter Decisions API** behind DecisionService."* |
| `control/jev.py:20` | `https://openrouter.ai/api/v1/chat/completions` — a different API |
| `config/experiment.json` `roles.jev.model` | `google/gemini-2.5-flash-lite` — a general chat model prompted to emit JSON |
| `control/jev.py:171` | `source = "jev"` is a literal on the adapter's success path |

The module docstring says *"through the OpenRouter Decisions **pattern**"* —
a resemblance, not the named API. In hindsight that word is where the gap
was papered over.

**Nobody decided this.** `roles.jev.model` arrived in commit `327f33a`
*"Import proven Run 001 Python control plane (provenance, unmodified)"*. The
one Run 002 review that resolved model identifiers —
`experiment/evidence/C2-model-identifier-discovery.txt`, 2026-09-25 — lists
`roles.jev.model` under **"Not changed"**, on the reasoning that it was
*"already concrete OpenRouter model slugs … not in scope for this
correction"*. It was inherited and never examined against what Jev is.

**Why no test caught it.** `tests/test_jev.py` mocks `http.post_json`
entirely. It pins the vocabulary, the fallback behaviour and the frozen
enum — all correctly — and nothing about the endpoint, the model, or
whether the thing answering is a decision model. A fully-mocked adapter
test cannot notice that the adapter points somewhere else. C-01 verified
the *vocabulary* and was right to; it never examined transport.

**A related defect found in the same read, pre-existing:** a billable
response that yields no usable decision is spent but unaccounted.
`control/jev.py:160-165` returns a `fallback` Decision for an unparseable
or off-enum answer **without passing `actual_cost_usd`**, so `consult_jev`
logs `cost_source="none"` and `budget.record` adds nothing — while
OpenRouter has already charged for the input tokens.

### 22.2 Documentation verified 2026-10-01 — and what it does NOT guarantee

Checked against OpenRouter's own Decisions API reference and Jev guide.

**Confirmed:** Jev is served through OpenRouter at
`POST https://openrouter.ai/api/alpha/decisions`, model `typesafe/jev-1.13`,
authenticated with the **existing `OPENROUTER_API_KEY`**. Pricing is
$0.042/M input tokens with output tokens free. The `choice` primitive
returns a selected option, per-option probabilities and a confidence. A
second surface, `POST /api/v1/systemone`, exists for TypeSafe-SDK
compatibility.

**Not guaranteed — a repair must treat each as unknown, not assume it:**

1. **The endpoint is explicitly alpha.** The reference marks the tag
   *"Alpha feature endpoints for Decisions requests."* An alpha surface can
   change shape without notice. Neither surface carries a stability
   declaration.
2. **A versioned slug is not documented as an immutable build.** The docs
   describe `~typesafe/jev-latest` as tracking the newest release, and say
   nothing about whether `typesafe/jev-1.13` pins a fixed build. BOOTSTRAP
   item 6 already forbids assuming an alias identifies a fixed model; this
   extends to the version number.
3. **The returned `model` string is richer than the request and is
   unspecified.** The published example requests `typesafe/jev-1.13` and
   receives `typesafe/jev-1.13-20260917`. What the date suffix means, and
   whether it always appears, is undocumented.
4. **`usage.cost` is OPTIONAL in the response schema.** Only
   `input_tokens` and `output_tokens` are required; `cost` appears in an
   example. Budget accounting must not assume it is present.
5. **There is no documented refusal, abstain or low-confidence shape.** The
   docs define no behaviour for a question Jev cannot answer. A weak answer
   presumably arrives as an ordinary answer with low `confidence` — which
   this control plane currently records and then ignores.
6. **The sample response is an example, not a contract.** Field presence
   beyond the required set must be read defensively.

### 22.3 The smallest complete repair

Reaching real Jev needs **no new secret and no change to the budget
arrangement** — same `OPENROUTER_API_KEY`, same metered OpenRouter ceiling
that Protocol v2 and `BUDGET.md` already govern. Going direct to
`api.typesafe.ai` would need a new `TYPESAFE_API_KEY` in `REQUIRED_SECRETS`
and would put Jev spend *outside* the governed meter, so it is the worse
option on the repository's own terms.

| # | Change | File |
|---|---|---|
| R1 | `roles.jev.model` → `typesafe/jev-1.13`; **pin the version, never `~jev-latest`** | `config/experiment.json` |
| R2 | `ENDPOINT` → `https://openrouter.ai/api/alpha/decisions` | `control/jev.py` |
| R3 | Build `{model, state, questions: {kind: {type: "choice", instructions, criteria}}}` in place of `messages` + `response_format` | `control/jev.py` |
| R4 | Read `body["answers"][kind]`; require `type == "choice"`; take `choice` and `confidence`; keep the existing `choice not in spec["options"]` guard **unchanged** | `control/jev.py` |
| R5 | Map `usage.input_tokens` / `usage.output_tokens` (renamed from `prompt_tokens` / `completion_tokens`); treat `cost` as **optional** | `control/jev.py` |
| R6 | Record the returned `model` string verbatim as provenance — it is the only evidence of which build answered | `control/jev.py` |
| R7 | Pass `actual_cost_usd` on **every** fallback that followed a billed HTTP 200, closing the unaccounted-spend defect | `control/jev.py` |
| R8 | `DECISIONS` gains a `criteria` description per option (§22.4) | `control/jev.py` |

`Decision`'s shape does not change, so `consult_jev`, `gate_jev` and the
seven required decision-log fields need no edit. That is what keeps this
small.

### 22.4 Proposed `criteria` descriptions — AWAITING REVIEW

The Decisions API requires a `criteria` object mapping each option to a
description. `DECISIONS` today carries only a flat `options` list. These
descriptions **shape classification**, and the enums are frozen under
C-01a, so they are a governance artefact, not an implementation detail.
Nothing below is approved, and nothing has been written into the code.

Provenance is marked per group: **[Q]** quoted or closely paraphrased from
a governing document; **[D]** derived from control-plane code semantics
because no governing document defines the value; **[U]** unresolved, left
blank rather than invented.

**`finding_severity` — [Q], Protocol v2 "Severity — single source of truth"**

| Option | Proposed description |
|---|---|
| `P0` | Critical. Requires immediate escalation and always blocks merge. |
| `P1` | High. Must be repaired and always blocks merge. An actual accessibility failure inherits P1 as a deterministic minimum. |
| `P2` | Moderate. Recorded and tracked; does not independently block merge. |
| `P3` | Low or hardening. Recorded and tracked; non-blocking. |

**[U] Unresolved.** `protocol/SEVERITY-POLICY.md` rule 3 defines a fourth
outcome — evidence that is **INVALID**, which *"does not default to P2/P3,
and does not default to the P1 floor either"*. The enum cannot express it,
and the fallback is `P1`, which rule 3 explicitly forbids as a default.
What Jev should return for invalid evidence is undecided. Do not resolve
this by adding an enum value: the enum is frozen.

**`worker_health` — [D], no governing document defines the four values**

Protocol v2 names the enumeration and stops. These are derived from
`supervisor.detect_stale` and `route_awaiting_dispatch`, whose semantics are
authoritative for the control plane's own behaviour.

| Option | Proposed description |
|---|---|
| `HEALTHY` | The worker is making meaningful progress, or has not yet had time to; no fault is evidenced. |
| `SLOW` | Progress is evidenced but slower than expected. Slow is explicitly not stalled — `detect_stale`'s own rule is *"Stale means no progress, not merely slow."* |
| `STALLED` | No meaningful progress within the governed stale window, or the agent process has vanished without reporting a terminal outcome. |
| `WAITING` | The task is legitimately parked on something external — awaiting review, evidence, a provider reset, or a human — rather than the worker being unhealthy. |

**[D] flagged for review**, because a derived definition that later diverges
from `detect_stale` would make Jev and the deterministic detector disagree
silently. The Watchdog independently reconciles worker health regardless
(C-01 contract), so a wrong classification is bounded — but it should still
be read before approval.

**`model_routing` — [Q], Protocol v2 "Model assignment" + C-01 contract**

| Option | Proposed description |
|---|---|
| `STAY_TIER` | The current model tier is adequate; none of the recorded escalation conditions is evidenced. |
| `ESCALATE_ONE_TIER` | Evidence shows at least one recorded escalation condition: repeated blocking failure, genuine cross-system complexity, security-sensitive ambiguity, or a task frozen as HIGH_REASONING. One tier only. |
| `HUMAN_REQUIRED` | Beyond a one-tier escalation — an emergency or release-critical blocker, an explicitly highest-reasoning task, demonstrated lower-tier failure, or a situation needing a human decision rather than a different model. |

Note for the reviewer: the C-01 contract states *"Jev's recommendation is
not itself the evidence."* These descriptions say what the evidence must
show, not what Jev may assert.

**`incident_classification` — [D] from control-plane usage; enum frozen under C-01a**

| Option | Proposed description |
|---|---|
| `NONE` | The evidence affirmatively shows no incident occurred. Not a bucket for ambiguous or unclassifiable evidence. |
| `WORKER` | A worker process or agent failed, hung, died, or produced unusable output. |
| `PROVIDER` | An upstream model provider failed, refused, rate-limited, or entered cooldown. |
| `CI` | Continuous integration failed, or could not produce a verdict on the current exact SHA. |
| `EVIDENCE` | Required evidence is missing, stale, invalid, or cannot be independently attributed — review, accessibility, security, or the PR contract. |
| `SECURITY` | A security concern in the product or the apparatus, including a weakening of security controls. |
| `PRIVACY` | Personal data handled contrary to `product/PRIVACY.md`, or an unapproved privacy-affecting change. |
| `BUDGET` | A metered spend threshold was crossed, or a paid call was refused for budget. |
| `STATE_INVARIANT` | The control plane's own durable state contradicts an external fact or another source of truth — the condition `STATE_INVARIANT_VIOLATION` records. |
| `APPARATUS` | The control plane or its tooling failed: Supervisor, Watchdog, workmux, ledger, or state store. |
| `CREDENTIAL` | A credential is missing, expired, rejected, or at risk of exposure. |

**`queue_priority` — [U] UNRESOLVED. Descriptions deliberately NOT drafted.**

The C-01 contract defines only the *set* Jev may rank within: *"Ordering
among tasks already legal to dispatch (dependencies satisfied, not
blocked). Jev cannot reorder past a dependency or an unresolved block."*
**Nothing in this repository distinguishes HIGHEST from HIGH, or LOWEST
from LOW.** Writing five plausible-sounding gradations would be exactly the
invention C-01a's "no subtype taxonomy" rule exists to prevent.

This kind also has **no call site** — `queue_priority` is never invoked by
the control plane. So the cheapest honest resolution may be to leave it
undescribed and unused until something actually needs it, rather than
authoring criteria for a question nobody asks.

**Cross-cutting [U]:** Jev documents no abstain shape, so a question it
cannot answer returns an ordinary answer with low `confidence`. The control
plane records `confidence` and acts on none of it. Whether a confidence
floor should downgrade an answer to `fallback` — and what that floor is —
is undecided. Today's behaviour, treating any schema-valid answer as
authoritative, would carry over unchanged unless decided otherwise.

### 22.5 Everything affected

| Area | Item | Change |
|---|---|---|
| Code | `control/jev.py` | `ENDPOINT`, request build, response parse, usage mapping, provenance, fallback cost (R2–R8) |
| Code | `control/http.py` | **None.** `post_json` already carries `ambiguous` and needs no change |
| Config | `config/experiment.json` `roles.jev.model` | R1. Not a frozen-manifest input — verified: it is in neither `PRODUCT_SPEC_FILES` nor `SEVERITY_POLICY_FILES` |
| Callers | `supervisor.consult_jev` | **None.** `Decision` shape unchanged; already budget-gated via `metered_call_allowed` |
| Gate | `preflight.gate_jev` | **None functionally** — but its meaning changes from "the adapter did not fall back" to "Jev answered". Its `evidence` dict should carry the returned `model` string |
| Logging | `JEV_DECISION` ledger event | Already carries all seven Protocol v2 fields. `model` becomes real provenance; `cost_source` stays `"provider"`/`"none"` |
| Budget | `budget.record` | **None.** Already tolerates `actual_cost_usd=None`. R7 changes only what `jev.py` passes it |
| Tests | `tests/test_jev.py` | 2 of 10 touch the wire shape (`prompt_tokens`/`completion_tokens`); 8 are vocabulary and fallback tests that must pass **unchanged** |
| Tests | new file | The verification below |
| Docs | `CONTRADICTION-AUDIT.md` C-19, this section, `LAUNCH-CHECKLIST.md` | Updated when the repair lands, not before |

### 22.6 Verification the repair owes

Mocked transport beneath the real adapter, as C-18 stage 2 established for
the notifier — not by stubbing `jev.decide`.

1. **Endpoint and model.** The request goes to `/api/alpha/decisions` with
   the configured versioned slug, not `chat/completions` and not an alias.
2. **Request shape.** `state` and `questions` are built; `questions[kind]`
   has `type: "choice"`, `instructions`, and `criteria` whose keys are
   **exactly** `spec["options"]`.
3. **Parsing and enum validation.** A valid answer maps to
   `choice`/`confidence`; the existing off-enum guard still forces
   `fallback`.
4. **Missing or malformed answers.** Absent `answers`, absent
   `answers[kind]`, wrong `type`, absent `choice`, non-string `choice`,
   and an answer keyed to a different question all fall back without
   raising.
5. **Fallback and confidence.** Every fallback still returns a member of
   its own `options`; `source` is `"fallback"`; `confidence` is carried
   through when present and `None` when absent.
6. **Model provenance.** `Decision.model` is the **returned** string
   (`typesafe/jev-1.13-20260917`-shaped), not the requested slug; if the
   response omits `model`, that is recorded as unknown rather than
   back-filled from the request.
7. **Cost accounting when a billable response yields no valid decision.**
   HTTP 200 with `usage.cost` present but an unparseable or off-enum
   answer ⇒ `source == "fallback"` **and** `actual_cost_usd` is the billed
   cost, so `budget.record` charges it. This is the R7 regression and did
   not previously hold.
8. **Cost absent.** `usage` without `cost` ⇒ `actual_cost_usd is None`,
   `cost_source == "none"`, and `budget.record` adds nothing — no zero
   substituted for unknown.
9. **Budget denial prevents the call.** `allowed=False` ⇒ no HTTP call at
   all (assert the transport is never invoked), `source == "fallback"`.
10. **The preflight path.** `gate_jev` calls `decide` **without**
    `allowed=`, which defaults `True`, so the gate spends regardless of
    `hard_stop`. Pin that as the current truth and raise it as a decision
    (§22.8), rather than silently changing a governed gate.
11. **Mutations to run:** endpoint reverted to `chat/completions` ·
    criteria keys diverging from `options` · off-enum guard removed ·
    requested slug recorded as provenance instead of the returned one ·
    billed-fallback cost dropped · `allowed=False` still calling.

### 22.7 Proposed live verification — SEPARATE, AND NOT AUTHORISED

No call has been made. This is a proposal for one.

- **Scope:** exactly **one** `worker_health` Choice request, the same
  evidence `gate_jev` already uses.
- **Expected cost:** on published pricing, well under one US cent — the
  worked example bills $0.000019992 for 476 input tokens.
- **Budget control:** run it with `allowed=budget.metered_call_allowed(doc)`
  threaded through, so the governed `hard_stop` can refuse it. The gate
  does **not** do this today (§22.6 item 10), which is why the decision
  belongs here rather than being assumed.
- **Stop conditions — abort and change nothing if any holds:** the account
  has no Jev access (early access was waitlisted as of mid-September 2026);
  HTTP 402 or 429; the response omits `answers[kind]`; the returned `model`
  does not begin with the requested slug; `usage.cost` exceeds $0.01;
  `hard_stop` is set.
- **What it would establish:** that the account can reach
  `typesafe/jev-1.13`, the real response shape, and the real returned
  `model` string — which is the value BOOTSTRAP item 6 requires recording
  before freeze.
- **What it would NOT establish:** that `jev_minimal_decision` passes. That
  gate passes only when the repair is implemented and the gate runs against
  it.

### 22.8 Decisions required before implementing

1. Use real Jev via OpenRouter, or record a governed decision to keep a
   chat-model substitute and fix the wording that implies otherwise.
2. Approve, amend or reject each `criteria` group in §22.4.
3. Resolve `finding_severity` **[U]**: what Jev returns for INVALID
   evidence, given the enum cannot express it and `P1` is a forbidden
   default.
4. Resolve `queue_priority` **[U]**: author the five gradations, or leave
   the kind undescribed and unused.
5. Decide the confidence policy, or record that confidence stays advisory.
6. Decide whether `gate_jev` should be budget-gated.
7. Decide the version-pinning rule, given the docs do not promise a
   versioned slug is an immutable build: record the returned `model` per
   call and pin the observed build at freeze.

### 22.9 Status

C-19 **OPEN — recorded, not repaired**. No code, config or secret changed.
`jev_minimal_decision` remains **NOT YET EVALUATED** and must not be marked
PASS on the current implementation. C-18 remains OPEN at 3 of 7 and is
untouched by this. T+00 remains **NOT_STARTED**.

## 23. C-19 refined repair proposal (2026-10-01)

Authoritative over §22's proposal. Still **a proposal**: no code, config or
secret has changed and no call has been made.

### 23.1 Decisions recorded

Taken by the human, 2026-10-01, and binding on the repair:

1. **Use actual TypeSafe Jev through OpenRouter's Decisions API.**
2. **Preserve the existing decision enums and the frozen source files.**
3. **Budget denial must prevent the call from BOTH the Supervisor and
   preflight.** `gate_jev` becomes budget-gated; today it is not.
4. **Confidence stays advisory.** No numerical threshold is to be invented,
   and schema-valid output is not independent evidence of anything.
5. **`queue_priority` stays unused and unavailable for live calls** until
   its criteria are approved. Its vocabulary is preserved, not removed.
6. **Requested and returned model identifiers are recorded separately**, and
   neither a versioned slug nor a returned date suffix is described as proof
   of immutable weights.

### 23.2 What §22 got wrong

Four corrections. Recorded because the proposal was wrong, not reshaped.

**(a) Unknown spend was not accounted at all.** §22 said "cost absent ⇒
`actual_cost_usd is None` ⇒ `budget.record` adds nothing — no zero
substituted for unknown". That avoids the *lie* but leaves the *exposure*
invisible: `percent()` reads `spent_usd` only, `hard_stop` derives from it,
and `metered_call_allowed` is just `not hard_stop`. A run whose costs never
report would spend without limit while the gate stayed open. Against a hard
ceiling that is insufficient. §23.3 fixes it.

**(b) The severity gap was misdiagnosed.** §22 said a deterministic
validity check should be proposed. **One already exists**:
`control/severity.py::apply_severity_policy` implements SEVERITY-POLICY
rules 1–3 and already returns `{"valid": False, "severity": None,
"merge_blocked": True}` for invalid evidence — fail-closed, no enum member
invented, severity explicitly `None` rather than a substantive verdict. It
is already called at `apparatus/pr-evidence/validate.js:126`. Proposing a
second one would have duplicated a tested, governed rule. §23.4.

**(c) §22 implied all five kinds would become live calls.** The governed
path for `finding_severity` is not a Supervisor call at all:
`protocol/PR-EVIDENCE-V2.schema.json` makes `jev_severity` a **required
field of the accessibility finding record**, documented as *"the PRE-floor
input. POST-floor severity … is computed by the validator."* Severity
reaches the control plane through the evidence record and the validator,
not through `DecisionService`. Wiring it into `consult_jev` would have
invented a second, competing path. §23.5.

**(d) A detector was described as a control.** §22.7 listed
"`usage.cost` exceeds $0.01" as a live-verification stop condition. A cost
read from a response cannot cap a call already made. §23.7.

### 23.3 Unknown spend — the smallest compatible mechanism

**Existing mechanisms, verified by reading `control/budget.py` in full:**
`spent_usd` (actual only), `estimated_usd` (recorded by `record()` but
**inert** — nothing reads it for any decision), `percent()` over actual
only, `thresholds_crossed`/`hard_stop` from actual only,
`metered_call_allowed = not hard_stop`. There is **no reservation
mechanism**, and `estimated_usd` is the only unused field already in the
schema. The repair uses it rather than adding one.

The module's governing distinction is *"Reported provider cost is `actual`.
Anything we compute ourselves is `estimated` and is never allowed to
masquerade as actual."* That is a rule about **reporting spend**, not about
**permitting more of it**. The proposal keeps reporting untouched and
changes only permission.

| # | Change | Effect |
|---|---|---|
| **E1** | Every metered call that *may* have been billed records a non-zero **estimate** through the existing `record(doc, "openrouter", actual, estimated)` signature. Three triggers: `usage.cost` absent; HTTP 200 whose body is malformed or off-enum; transport `ambiguous` | Unknown spend becomes visible. Never recorded as actual. **Never zero** |
| **E2** | `budget.unresolved_exposure_usd(doc)` — a named reader over the existing `estimated_usd` | No schema change |
| **E3** | `metered_call_allowed` additionally refuses when `spent_usd + estimated_usd >= total_usd` | Further metered calls stop when unresolved exposure cannot be bounded inside the remaining budget |
| **E4** | `percent()`, `thresholds_crossed` and `hard_stop` are **unchanged** — actual spend only | An estimate never masquerades as actual, and never fabricates a threshold crossing |

**Estimate basis, two tiers.** `input_tokens` is a **required** response
field even where `cost` is optional, so the common case is bounded from the
response itself:

- *Tokens known, cost unknown* → `input_tokens × governed price per input
  token`.
- *Tokens unknown* (malformed body, or an ambiguous transport outcome where
  no body was read) → a **governed per-call worst case**. If no such
  constant is approved, exposure is **UNBOUNDED** and E3 refuses further
  metered calls until a human resolves it. Never zero, never guessed.

**Ambiguous transport reuses C-18 stage 2.** `http.Response.ambiguous`
already distinguishes "may have been transmitted" from "definitely not
sent". A refused connection or DNS failure was never billed and records no
estimate; a timeout or reset may have been billed in full and records one.
No new mechanism.

**Reconciliation, honestly.** The Decisions API offers no call-status
lookup, so an estimate cannot be retired automatically against the real
charge. `estimated_usd` therefore only grows, and that is the intended
signal: it is already surfaced by `budget.summary` and in every
`JEV_DECISION` ledger event. Clearing it is a human reconciliation against
the provider's own billing, not something the control plane can assert.

### 23.4 Invalid severity evidence — use the check that exists

**Traced, not assumed.** `finding_severity` has **no `DecisionService` call
site**. `apply_severity_policy` has **no production caller in Python** —
only `tests/test_severity.py`. The live caller is the Node validator,
`apparatus/pr-evidence/validate.js:126`, which passes `undefined` for the
requirement registry, so — by that module's own stated design — *"Every
FAILURE-classified accessibility finding fails closed as INVALID regardless
of its citation."* That is the pre-existing **C-04/C-02** gap, not something
C-19 introduces or can close.

**The ordering rule the repair adopts:**

1. **Validity is determined from the evidence record, before and
   independently of any classification.** An evidence record that fails
   `apply_severity_policy` is INVALID and merge-blocking whatever a model
   might have said — and, because validity is decidable without a model, an
   invalid record must never trigger a paid classification at all.
2. **A fallback never populates `jev_severity`.** `apply_severity_policy`'s
   first branch already returns `INVALID: unknown or missing jev_severity`
   for an absent field. So an unreachable Jev produces INVALID through the
   **existing** code path — no new enum member, no new status, and the
   fallback `P1` is never presented as a substantive verdict because it is
   never written into the record.

**The smallest necessary interface change** — and §22 was wrong to insist
the repair fit two files. `Decision` *can* distinguish these outcomes, via
`source`, but only by convention: every caller must remember to check it,
and for severity the failure mode is a blocking `P1` that looks exactly
like a real answer. Add one helper:

```
jev.confirmed_choice(decision) -> str | None      # choice when source == "jev", else None
```

Four lines. Any caller building an evidence record populates `jev_severity`
from `confirmed_choice` only, so forgetting the check becomes impossible
rather than merely discouraged. `Decision`'s fields are unchanged.

### 23.5 Which kinds are live after the repair

| Kind | Call sites | Status after C-19 |
|---|---|---|
| `worker_health` | `supervisor.consult_jev`, `preflight.gate_jev` | **LIVE.** The only kind the repair makes real |
| `finding_severity` | none — reaches the control plane as the `jev_severity` evidence field, validated by `apply_severity_policy` | **NOT a DecisionService call.** Vocabulary preserved. Whether a reviewer uses Jev to produce the field is a reviewer-prompt question, outside C-19 |
| `queue_priority` | none | **NOT AVAILABLE for live calls** (decision 5). Vocabulary preserved, criteria unapproved |
| `model_routing` | none | Not wired. Vocabulary preserved |
| `incident_classification` | none | Not wired. Vocabulary preserved |

**Enforcement, so "unavailable" is a mechanism rather than a note:** a
kind is callable only if it carries approved `criteria`. `decide` refuses
an unapproved kind before any HTTP call and returns the deterministic
fallback with a finite reason. `DECISIONS` keeps all five entries and every
enum unchanged, satisfying decisions 2 and 5 together.

### 23.6 Criteria — complete text, verified against callers

Provenance: **[Q]** quoted or closely paraphrased from a governing
document · **[D]** derived from control-plane code, no governing definition
exists · **[U]** unresolved, deliberately not drafted.

#### `worker_health` — [D] — the only kind that goes live

Verified against its actual evidence payload, which is exactly five fields:
`task`, `state`, `attempts`, `minutes_since_progress`, `review_queue_depth`.

| Option | Criteria text |
|---|---|
| `HEALTHY` | The worker is making meaningful progress, or has not yet had time to produce a progress signal. No fault is evidenced. |
| `SLOW` | Progress is evidenced but slower than expected for this task. Slow is explicitly not stalled: the control plane's own rule is that stale means no progress, not merely slow. |
| `STALLED` | No meaningful progress within the governed stale window, or the agent process has ended without reporting an outcome. |
| `WAITING` | The task is not being worked on and is not expected to be — it is parked awaiting an independent review, evidence, a provider reset, or a human — rather than a worker being unhealthy. |

**Overlap: HEALTHY versus WAITING — material, and a decision is needed.**
`consult_jev` filters to tasks in `("ACTIVE", "REVIEW")`. `REVIEW` means
"awaiting independent review" **by definition of the state machine**, so
`WAITING` is fully determined by the `state` field the evidence already
carries. Asking a model to infer a value the state machine already knows
invites disagreement without adding information — and the C-01 contract
already has the Watchdog independently reconciling worker health, so a
model's answer here is advisory over a fact. **Unresolved choice:** restrict
the question to `ACTIVE` tasks so it is genuinely about a working worker,
or keep `REVIEW` and accept that `WAITING` is trivially derivable. I have
not picked one.

#### `finding_severity` — [Q] — Protocol v2, "Severity — single source of truth"

| Option | Criteria text |
|---|---|
| `P0` | Critical. Requires immediate escalation and always blocks merge. |
| `P1` | High. Must be repaired and always blocks merge. An actual accessibility failure inherits P1 as a deterministic minimum, which classification can raise but never lower. |
| `P2` | Moderate. Recorded and tracked; does not independently block merge. |
| `P3` | Low or hardening. Recorded and tracked; non-blocking. |

**Resolved, and no longer [U].** §22 recorded "what Jev returns for INVALID
evidence" as unresolved. It is not: INVALID is not a severity and never was
(SEVERITY-POLICY rule 3). It is decided by `apply_severity_policy` from the
evidence record, before classification, and an absent `jev_severity` already
yields it. The enum needs no fourth member and the question needs no
abstain value.

#### `model_routing` — [Q] — Protocol v2 "Model assignment" + C-01 contract

| Option | Criteria text |
|---|---|
| `STAY_TIER` | The current model tier is adequate. None of the recorded escalation conditions is evidenced. |
| `ESCALATE_ONE_TIER` | Evidence shows at least one recorded escalation condition: repeated blocking failure, genuine cross-system complexity, security-sensitive ambiguity, or a task frozen as HIGH_REASONING. One tier only. |
| `HUMAN_REQUIRED` | The situation needs a human decision rather than a different model: an emergency or release-critical blocker, an explicitly highest-reasoning task, or demonstrated failure at a lower tier. |

**Overlap: escalation versus HUMAN_REQUIRED — genuinely unresolved.**
Protocol v2 lists *"repeated blocking failure"* among the one-tier
escalation conditions and *"demonstrated lower-tier failure"* among the
exceptional-tier conditions. On the same evidence both can read as true,
and **Protocol v2 states no precedence rule**. I have not invented one. The
C-01 contract's *"Jev's recommendation is not itself the evidence"* bounds
the consequence — the escalation still needs recorded evidence — but it
does not resolve which value to return.

#### `incident_classification` — [D] — enum frozen under C-01a

| Option | Criteria text |
|---|---|
| `NONE` | The evidence affirmatively shows no incident occurred. Not a bucket for ambiguous or unclassifiable evidence. |
| `WORKER` | A worker process or agent failed, hung, ended without an outcome, or produced unusable output. |
| `PROVIDER` | An upstream model provider failed, refused, rate-limited, or entered cooldown. |
| `CI` | Continuous integration failed, or could not produce a verdict on the current exact SHA. |
| `EVIDENCE` | Required evidence is missing, stale, invalid, or cannot be independently attributed — review, accessibility, security, or the PR contract. |
| `SECURITY` | A security concern in the product or the apparatus, including any weakening of security controls. |
| `PRIVACY` | Personal data handled contrary to the product privacy specification, or an unapproved privacy-affecting change. |
| `BUDGET` | A metered spend threshold was crossed, or a paid call was refused for budget. |
| `STATE_INVARIANT` | The control plane's own durable state contradicts an external fact or another source of truth. |
| `APPARATUS` | The control plane or its tooling failed: Supervisor, Watchdog, workmux, ledger, or state store. |
| `CREDENTIAL` | A credential is missing, expired, rejected, or at risk of exposure. |

**Overlapping causes — unresolved, and I have invented no precedence.**
C-01a froze the enumeration and forbade a subtype taxonomy, but set **no
rule for evidence that fits two classes**. Concrete collisions from this
repository's own failure modes:

| Situation | Competing classes |
|---|---|
| A provider rate-limit that also crosses a spend threshold | `PROVIDER` / `BUDGET` |
| A provider rejecting an expired key | `PROVIDER` / `CREDENTIAL` |
| A secret reaching a commit or a log | `SECURITY` / `CREDENTIAL` |
| A Watchdog or ledger fault while a worker is running | `APPARATUS` / `WORKER` |
| Review evidence missing because the worker crashed | `EVIDENCE` / `WORKER` |

Either a precedence rule is governed, or the descriptions must say the
classification names the **proximate** cause — which is itself a precedence
rule and so also needs deciding. This kind is not wired, so it does not
block the repair.

#### `queue_priority` — [U] — deliberately not drafted

Per decision 5 the vocabulary is preserved and the kind stays unavailable
for live calls. The C-01 contract defines only the *set* Jev may rank
within; **nothing in this repository distinguishes HIGHEST from HIGH or
LOWEST from LOW**, and it has no call site. Drafting five gradations would
be the invention C-01a's no-subtype-taxonomy rule exists to prevent.

### 23.7 Live verification — corrected, and still not authorised

**What actually caps a call before it is made:**

1. The budget gate, now E3-aware, evaluated before the request.
2. **The request payload we choose to send.** Jev bills input tokens only —
   output is free — so the cost ceiling is fixed by what we transmit. The
   `worker_health` evidence is five short scalars, so a worst-case cost is
   **computable before sending** from the published price. That computed
   bound is the cap.
3. Nothing else. There is no `max_tokens` equivalent that bounds spend,
   because output is not billed.

**What only detects, after the money is spent:** reading `usage.cost` from
the response. §22.7 listed it as a stop condition; it is a reconciliation
check and an abort signal for *subsequent* calls. Removing it would be
wrong, but calling it a control was.

**Scope if authorised:** one `worker_health` Choice request, budget-gated
on both paths. **Abort before sending** if the computed worst-case exceeds
the remaining budget, if `hard_stop` is set, or if `spent + estimated`
already exhausts the ceiling. **Abort after, affecting later calls only:**
HTTP 402/429; missing `answers[kind]`; a returned `model` not beginning
with the requested slug; an observed cost above the computed bound.

It would establish account access, the real response shape, and the real
returned `model` string. It would **not** establish that
`jev_minimal_decision` passes.

### 23.8 Affected files and the verification owed

| Area | Item | Change |
|---|---|---|
| Code | `control/jev.py` | Endpoint, request build, response parse, usage mapping, requested-vs-returned provenance, billed-fallback cost, unapproved-kind refusal, `confirmed_choice` |
| Code | `control/budget.py` | E2 `unresolved_exposure_usd`; E3 in `metered_call_allowed`. `percent`/`record`/thresholds unchanged |
| Code | `control/preflight.py` | `gate_jev` passes `allowed=budget.metered_call_allowed(...)` (decision 3) and records both model identifiers in its evidence |
| Code | `control/supervisor.py` | `consult_jev` records the estimate on an unresolved outcome. Already budget-gated |
| Code | `control/http.py` | **None.** `ambiguous` already exists |
| Code | `control/severity.py` | **None.** The validity rule is correct as written |
| Config | `config/experiment.json` | `roles.jev.model` → `typesafe/jev-1.13`; governed price constant if approved. Not a frozen-manifest input — verified |
| Tests | `tests/test_jev.py` | 2 of 10 touch the wire shape; the other 8 must pass unchanged |
| Tests | `tests/test_severity.py`, `test_budget*` | Must pass unchanged |
| Tests | new file | Below |
| Docs | C-19 row, §23, `LAUNCH-CHECKLIST.md` | On landing, not before |

**Verification the repair owes**, mocked beneath the real adapter:

1. Endpoint is `/api/alpha/decisions`; model is the configured versioned
   slug, never an alias.
2. `questions[kind].criteria` keys equal `spec["options"]` exactly.
3. Valid answer → `choice`, `confidence`; the off-enum guard still forces
   fallback.
4. Malformed answers: absent `answers`, absent `answers[kind]`, wrong
   `type`, absent/non-string `choice`, an answer keyed to another question.
5. **Malformed confidence:** absent, null, non-numeric, out of range →
   carried as `None`, never coerced, never blocking the decision. Advisory
   only (decision 4); no threshold exists to test.
6. **Model provenance:** requested and returned recorded **separately**;
   a response omitting `model` records the returned value as unknown and
   never back-fills it from the request (decision 6).
7. **Billed fallback:** HTTP 200 with `usage.cost` but an unusable answer →
   `source == "fallback"` **and** the cost charged to `spent_usd`.
8. **Unknown spend:** cost absent with tokens present → non-zero
   **estimate**, `spent_usd` unchanged; tokens also absent → unbounded
   exposure; ambiguous transport → estimate recorded; refused connection →
   no estimate. No zero substituted anywhere.
9. **E3:** `spent + estimated >= total` ⇒ `metered_call_allowed` False ⇒
   no HTTP call from either path. `percent()` and `hard_stop` unchanged by
   estimates.
10. **Preflight budget denial:** `gate_jev` makes no call when denied, and
    reports a distinct not-evaluated reason rather than a FAIL that looks
    like Jev answered wrongly.
11. **Unapproved kinds:** `queue_priority` (and any kind lacking approved
    criteria) refuses before any HTTP call and returns its fallback.
12. **`confirmed_choice`** returns `None` for every fallback and the choice
    only for `source == "jev"`.

**Mutations:** endpoint reverted · criteria keys diverging from options ·
off-enum guard removed · returned provenance replaced by the requested slug
· billed-fallback cost dropped · unknown spend recorded as zero · E3 removed
from `metered_call_allowed` · estimate added to `spent_usd` instead of
`estimated_usd` · preflight denial bypassed · an unapproved kind reaching
HTTP.

### 23.9 Genuinely unresolved

1. **Governed price per input token**, and the **per-call worst case** used
   when tokens are unknown. Without them E4 falls to unbounded exposure and
   Jev stops after the first unresolved call.
2. **`worker_health` scope:** restrict to `ACTIVE`, or keep `REVIEW` and
   accept `WAITING` as trivially derivable (§23.6).
3. **`model_routing` precedence** between `ESCALATE_ONE_TIER` and
   `HUMAN_REQUIRED` on overlapping evidence.
4. **`incident_classification` precedence** for evidence fitting two
   classes — or a governed "proximate cause" rule, which is itself a
   precedence rule.
5. **`queue_priority`** criteria, or a decision to retire the kind from use
   while keeping its vocabulary.
6. Whether the `finding_severity` **reviewer** path should use Jev at all —
   outside C-19, but it is where the enum is actually consumed.

Not unresolved, and previously listed as such: the confidence threshold
(decision 4 settles it — advisory, no threshold), severity-INVALID
(§23.4), and whether `gate_jev` is budget-gated (decision 3).

### 23.10 Status

C-19 **OPEN — refined, not repaired**. No code, config or secret changed.
`jev_minimal_decision` remains **NOT YET EVALUATED** and must not be marked
PASS on the current implementation. C-18 remains **OPEN at 3 of 7**,
untouched. T+00 remains **NOT_STARTED**.

## 24. C-19 worker_health repair — IMPLEMENTED (2026-10-01)

Authorised this session for the **existing `worker_health` path only**. §23 was
the proposal; this is what was actually built, including where the build
departs from §23 and why. **C-19 stays OPEN**: everything below is verified by
mocked transport, and no call has been made.

### 24.1 Human decisions that governed this build

§23.1's six decisions stand. Five more were taken this session, and three of
them overrule §23:

1. **`worker_health` scope keeps `ACTIVE`/`REVIEW`.** §23.9 item 2 is closed.
   `REVIEW` is not "no reviewer is working" — `route_awaiting_dispatch` treats
   it as the designed waiting state for a task whose PR is with an independent
   reviewer, so the worker really is parked, and `WAITING` is the honest
   answer rather than a redundant one.
2. **Every criteria statement must be answerable from the evidence actually
   sent.** This changed the approved text: see 24.3.
3. **No `confirmed_choice` helper, and no reviewer severity path.** §23.4
   proposed a four-line helper for a caller that does not exist. It would have
   been dead code in this repair.
4. **A post-call exposure check is not sufficient.** §23.3's E1–E3 detect
   unknown spend after the money is gone. The build reserves **before**
   sending instead: see 24.4.
5. **`estimated_usd` is not reused**, after inspecting what it means: 24.4.

### 24.2 What was already present versus what changed

Already correct, and deliberately untouched: the five frozen decision kinds and
their enums; the deterministic fallback for every kind; the off-enum guard;
`Decision`'s role as an advisory value the Supervisor validates; the Watchdog's
independent reconciliation of worker health; `control/severity.py`;
`control/http.py`, whose `ambiguous` flag from C-18 stage 2 is what makes
"definitely not sent" decidable; `percent()`, `thresholds_crossed` and
`hard_stop`, which remain actual-spend only.

Changed: the transport, the request and response shapes, cost and provenance
handling, admission control, and the two call sites.

| File | Change |
|---|---|
| `control/jev.py` | `ENDPOINT` → `/api/alpha/decisions`; Decisions request (`state` + `questions`, one `choice` question with `criteria`); defensive answer parsing; `requested_model`/`returned_model` recorded separately; `billing_state`; validated cost, tokens and confidence; refusal of kinds without approved criteria before any HTTP call. `SYSTEM_PROMPT`, the JSON-schema response format and `_render` are gone with the chat transport |
| `control/budget.py` | `valid_usd`; the reservation lifecycle — `reservations`, `outstanding_exposure_usd`, `reserve`, `settle`; exposure added to `metered_call_allowed`; `summary` reports exposure as its own figure |
| `control/state.py` | `budget.reservations` in the initial document (readers setdefault, so older documents still work) |
| `control/config.py` | `jev_pricing` on `ExperimentConfig`; `jev_reservation_usd`, which returns `None` — blocking the paid path — rather than guessing |
| `control/supervisor.py` | `consult_jev` reserves durably before the call and settles after; logs both model identifiers and the billing state |
| `control/preflight.py` | `gate_jev` is budget-gated (decision 3) and reports a refusal as NOT EVALUATED, distinct from a failed answer; evidence carries both identifiers |
| `config/experiment.json` | `roles.jev.model` → `typesafe/jev-1.13`; a `jev_pricing` block carrying the bound **and its basis** |
| `tests/test_jev.py` | 3 of 10 tests rewritten (they pinned the chat wire shape and "all five kinds are callable"); 7 unchanged |
| `tests/test_c05_3_step6b_repairs.py` | one source-count assertion renamed to the new consumer; its substantive claim unchanged |
| `tests/test_c19_jev_decisions.py` | new, 57 tests |

### 24.3 The criteria, as approved and as amended

§23.6's `worker_health` table was approved subject to every statement being
supportable by the five fields actually sent (`task`, `state`, `attempts`,
`minutes_since_progress`, `review_queue_depth`).

One statement was not. §23.6's `STALLED` read "or the agent process has ended
without reporting an outcome" — **the evidence contains no liveness
observation of any kind**, so that invites a confident answer drawn from
nothing. The shipped text judges STALLED from elapsed time and says
explicitly: *"the evidence contains no observation of any process, so do not
conclude that a process has died or ended."* A test pins that wording, because
it is a governance artefact rather than a comment.

`SLOW` and `WAITING` were likewise re-anchored to supplied fields. The other
four kinds keep their frozen vocabulary and have **no criteria**, which is now
a mechanism: `decide` refuses them before any HTTP call.

### 24.4 Budget: reservation, settlement, crash

**Why not `estimated_usd`.** Inspected before reuse, as required. Its only
writer is `record()`'s third argument, and the only production caller
(`supervisor.consult_jev`) passed `None` — so no historical estimate exists,
and none was reinterpreted. But the meaning is wrong for this job: an estimate
is a reporting figure that is never retired, while a reservation is *released*
when the request provably never left. Folding one into the other would make a
released reservation look like erased spend. A separate
`budget.reservations` map was added instead.

**Admission, before anything is sent.** `budget.reserve` is called inside the
state transaction that persists it, and that transaction commits before the
request leaves. It refuses unless `hard_stop` is clear **and**
`spent_usd + outstanding_exposure + this allowance ≤ total_usd`. Outstanding
exposure counts reservations in both states.

**Settlement.**

| Outcome | Effect |
|---|---|
| Provider reported a valid cost | Reservation deleted, cost added to `spent_usd`. Never both — no double count. Applies **even when the answer was unusable**: the money went either way |
| Transport proves nothing left (DNS failure, connection refused) | Reservation released; no spend |
| Anything else — timeout, any HTTP status, a 200 with no usable cost | Reservation retained, marked `UNRESOLVED`. Only a human reconciling against the provider's billing can clear it |

"Definitely not sent" is an allow-list of exactly the two transport failures
`control/http.py` already proves. A server that answered *anything* reached
the provider, and this control plane cannot tell from outside whether that was
billed.

**Crash between sending and settlement.** The reservation is on disk, `OPEN`,
before the request leaves; only the in-process call that created it can settle
it, so a dead process's reservation is never released and keeps counting as
exposure.

> **The second half of that sentence was wrong when first written, and §25
> corrects it.** "The retained exposure shrinks the remaining ceiling, so a
> restarted process is refused rather than silently paying twice" does not
> follow: against a $25 ceiling a lost $0.002688 reservation leaves headroom
> for thousands more calls. The test that appeared to prove it had rigged the
> ceiling to exactly one call, so it proved budget exhaustion, not crash
> safety. Exposure retention held; duplicate prevention did not.

**Values that must not weaken accounting.** `budget.valid_usd` rejects `bool`
(an `int` subclass, so `True` would have become $1.00), non-numerics, NaN,
infinity and negatives. An invalid cost is *missing*, which retains exposure
rather than releasing it. A reservation whose amount is unreadable contributes
the **whole ceiling**, so tampering closes the gate instead of opening it.

**Unchanged:** `percent()`, `thresholds_crossed` and `hard_stop` still read
actual spend only, so a reservation never fabricates a threshold crossing, and
an estimate never masquerades as actual.

### 24.5 The cost bound, and what it does not prove

Verified 2026-10-01 against OpenRouter's own surfaces:

- **Price.** `GET /api/v1/models/typesafe/jev-1.13/endpoints` reports
  `pricing.prompt` `0.000000042` per input token ($0.042/M),
  `pricing.completion` `0`, and **no per-request price field**. Cross-checked
  against the documented worked example — 476 input tokens billed
  $0.000019992, which is exactly 476 × 0.000000042, so that observation shows
  no surcharge and no minimum.
- **Token ceiling.** The bound is the largest documented maximum input a single
  request may carry: OpenRouter reports `context_length` 32,000 for the served
  model; TypeSafe's own models page is reported as 64,000 per request (32,000
  for `state` plus the longest question). **The two disagree, so the larger is
  used.**
- **Reservation.** 64,000 × $0.000000042 = **$0.002688 per call**, recorded in
  `config/experiment.json` with this basis beside it.

This bounds the **complete request** — model, state, questions, instructions
and every criteria description — precisely because it does not depend on
tokenizing the payload. **Payload bytes are not claimed to bound tokens
anywhere**; no tokenizer for this model is specified.

**Not established, and stated rather than papered over:** OpenRouter documents
neither what happens to a request exceeding the limit (rejected or truncated)
nor any billing overhead or minimum charge; the two published maxima disagree;
and neither the requested slug nor the returned dated string proves which
weights ran. The bound assumes only that billed input tokens cannot exceed the
maximum input a request may carry, and that no undocumented per-request
surcharge exists. It is **admission control, not a prediction** — actual cost
is whatever the provider reports, and `config.jev_reservation_usd` returning
`None` blocks the paid path outright rather than substituting a guess.

### 24.6 Verification

- **Focused:** `test_c19_jev_decisions` 57, `test_jev` 11, `test_control_plane`
  81 — **149/149 PASS**.
- **Full:** `python3 -m unittest discover -s tests` — **1569/1569 PASS**
  (baseline 1511 before this work; +57 new, +1 net in `test_jev`).
- **Mutations: 8/8 detected**, each restored and the restoration verified by
  SHA-256 plus a re-run: endpoint reverted to `chat/completions` · criteria
  keys diverging from the frozen options · off-enum guard removed · returned
  provenance back-filled from the request · billed-fallback cost dropped ·
  reservation ceiling arithmetic removed · unknown outcome releasing instead of
  retaining · preflight denial bypassed.
- `git diff --check` clean; `scripts/check_no_secrets.py` clean across 170
  tracked files.

**Failures encountered and fixed during the build:** three pre-existing tests
went red exactly as expected and were updated (two pinned the chat wire shape,
one pinned `budget.metered_call_allowed` as the Supervisor's single consumer);
and one test I had just written asserted a boundary its own arithmetic could
not reach — a $0.005 ceiling cannot hold two $0.002688 reservations. Production
code was not changed to make any of them pass.

### 24.7 What still blocks C-19

1. **No live call has been made.** Everything here is mocked beneath the real
   adapter. `jev_minimal_decision` remains **NOT YET EVALUATED** and must not
   be marked PASS from mocks — a mocked transport cannot show that the account
   can reach `typesafe/jev-1.13`, what the real response looks like, or what
   the returned `model` string actually is.
2. **The alpha endpoint may change shape without notice**, and the response
   schema's optional fields are read defensively for that reason.
3. **Still unresolved, unchanged from §23.9 and not invented here:**
   `model_routing` precedence · `incident_classification` precedence ·
   `queue_priority` criteria · whether the `finding_severity` reviewer path
   should use Jev at all.

## 25. C-19 crash/restart duplicate-call correction (2026-10-01)

Authoritative over §24.4's crash paragraph. Scope: this one property, verified
and then corrected. Nothing else in §24 changed.

### 25.1 The claim, and how it failed

§24.4 claimed a crash between sending and settlement could not "permit an
automatic duplicate paid request". **Verified against the real code and found
false.** A probe ran the real `consult_jev` against a real `Store` at the real
$25 ceiling: process A reserved, the request went out, the process died before
settling; the Watchdog's restart then **sent the same paid question again**.

```
after the crash:   requests sent: 1 · reservation: OPEN · exposure: $0.002688
                   spent: $0.00 · metered_call_allowed: True
after the restart: requests sent: 1 · spent: $0.00002
VERDICT: DUPLICATE PAID REQUEST PERMITTED
```

Two reasons, neither visible from the arithmetic:

1. **Exposure is not a brake at this scale.** $0.002688 against $25 leaves
   headroom for roughly nine thousand further calls. Ceiling arithmetic only
   bites once exposure fills the whole budget.
2. **The interval gate does not survive a restart.** `_last_jev` is `0.0` in
   `Supervisor.__init__`, so `time.monotonic() - 0.0 ≥ 900` on any machine up
   for more than fifteen minutes: the first consultation after a restart fires
   immediately. A crash loop therefore pays once per restart.

**Why the test did not catch it.** `test_a_crash_cannot_be_retried_into_a_
second_paid_request` set `total_usd` to exactly one allowance. With the
ceiling rigged that small, the second reservation was refused by arithmetic,
and the test read as proof of a property it never exercised. It is now named
`test_exposure_exhausting_the_ceiling_refuses_the_next_reservation`, which is
what it actually proves.

### 25.2 The correction

A third reservation status, `ABANDONED`, and a sweep that assigns it.

**The rule.** Only the in-process call that created a reservation ever settles
it, and a caller settles before it reserves again. So an `OPEN` reservation
seen at admission time belongs either to a process that is gone, or to this
process, which lost it between sending and settling. Both are the same fact:
the outcome of a request that may already have been billed will never be
learned. `budget.sweep_abandoned` runs inside the admission transaction and
promotes those to `ABANDONED`; `budget.duplicate_blocked` then refuses that
request outright, not through the arithmetic.

**The exception, and why it earns its code.** A genuinely concurrent caller —
preflight running beside the Supervisor — must not durably block the run. A
reservation is left alone while its owner is **provably** the process that
made it, using C-09's existing `(pid, start_ticks)` identity via
`proc.verified_alive`, which a reused PID cannot fake. Anything less than
proof — a dead owner, an unreadable `/proc`, no recorded ticks — is treated as
abandoned, because the failure that matters costs money twice.

**`ABANDONED` versus `UNRESOLVED`, which do different jobs.** `UNRESOLVED` was
*settled*: the call completed its lifecycle and reported an unknown cost. It
retains exposure and does not block — a timeout costs one call per 900-second
interval, which is bounded. `ABANDONED` was never settled at all, and the
restart path that produces it is unbounded. Only the unbounded one closes the
gate.

**What is suppressed: the request, not the budget.** The guard is keyed on the
reservation's `purpose`, which carries the LOGICAL REQUEST identity — the
question and what it is about:

| Caller | Identity | A second one is |
|---|---|---|
| `supervisor.consult_jev` | `supervisor.jev_consultation_identity(task)` — §25.4 | the same question, about the same worker attempt, on evidence that has not moved |
| `preflight.gate_jev` | `preflight.JEV_GATE_PURPOSE`, a constant | the same fixed probe, which never varies |

So a lost consultation is never repeated **while it is still the same
question**, a consultation about `TASK-002` — which was not a duplicate of
anything — still goes ahead, and a re-run of `ctl preflight` does not re-buy a
probe that may already have been billed. A blanket refusal of all metered spend was the first
shape of this fix and was wrong: it would turn one lost sub-cent request into
the loss of Jev for the whole run, and it would conflate "this must not be
repeated" with "there is no money". `metered_call_allowed` therefore answers
only the money question, exactly as before this correction.

**Clearing it is human.** Nothing in the control plane clears `ABANDONED`
automatically, and no command was added to do so — the same stance `hard_stop`
already takes (C-08b.2's S8 is NO_ACTION-only). The consequence, stated
plainly: **that one consultation is never made again for the rest of the run**
until a human reconciles the reservation against the provider's billing. Jev
remains available for every other question, and the affected task falls back
to the deterministic path, which the Watchdog reconciles independently
(C-01 contract).

**A refusal says which refusal it is.** `gate_jev` reports
`DUPLICATE_REQUEST_UNRESOLVED` rather than `BUDGET_REFUSED`, because the money
is there and reporting a ceiling problem would send a human to the wrong
place; `consult_jev` records `duplicate_suppressed` in its `JEV_DECISION`
metadata for the same reason. `budget.summary` reports
`abandoned_reservations`, so the condition is visible everywhere a budget
summary already appears.

### 25.4 The consultation identity

`jev:worker_health:<task>:a<attempts>:w<worker>:p<progress_marker>:<state>`

Keying only on the task id was too coarse: it suppressed the crashed
consultation **and every later one about that task**, so a worker that was
re-dispatched and recovered could never be asked about again. The identity is
now four durable task-record fields, each covering a change the others miss.

| Field | Written by | Catches |
|---|---|---|
| `attempts` | `dispatch_builder`, `+= 1` | a re-dispatched BUILDER. Its worker id is `<task>-builder` on every attempt, so nothing else moves |
| `worker` | every dispatch path | a re-dispatched fixer or reviewer, whose worker id carries its own cycle number while `attempts` stays put |
| `progress_marker` | `reap_workers`, **only when the agent's `output_bytes` differs** from the stored value | the worker having actually done something — the record's one durable marker of real progress |
| `state` | `transition` | a durable transition. ACTIVE and REVIEW are different questions about the same worker, and the criteria say so |

**Deliberately excluded**, because the question is unchanged when they move:
`minutes_since_progress` (recomputed from the clock every tick),
`last_progress_at` (the timestamp of the same event `progress_marker` already
identifies, without being a clock reading), `review_queue_depth` (another part
of the run entirely), and `updated_at`. A tick that merely polls changes none
of the four.

**The property that makes this the right cut.** A STALLED worker emits no new
output, so its marker does not move, its state does not change, and no new
worker is dispatched — the lost consultation stays suppressed, which is
exactly the case the suppression exists for. A worker that is genuinely
progressing, or has been replaced, poses a materially different question and
gets one.

**Lifecycle.**

1. `consult_jev` computes the identity from the task record and reserves under
   it, durably, before the request leaves.
2. Settled — cost known, or provably never sent — the reservation is removed
   or released, and the identity is free again. **Cadence is untouched**: it
   is still `JEV_INTERVAL_SECONDS` alone that paces successful consultations,
   and a settled call never blocks anything.
3. Lost in flight — the sweep marks it `ABANDONED` at the next admission, its
   exposure stays counted, and that identity is refused.
4. The next durable attempt or real progress produces a different identity,
   which is admitted. The abandoned reservation is preserved exactly as it
   was: still `ABANDONED`, still counted, still unrepeatable.

**Known limitation, not papered over:** the sweep runs at admission, so
nothing in durable state records the abandonment until the next attempted
spend. Nothing spends before that point — `reserve` is the only path to money
and it sweeps first — but a reader looking only at state will not see the
condition until then.

### 25.5 Verification

- The same probe, re-run after the fix: **`requests sent by process B: 0`**,
  reservation `ABANDONED`, `spent_usd` unchanged.
- **26 new tests**, including the four-step end-to-end run on ONE task at the
  real $25 ceiling — consultation sent, process dies before settling, restart
  on unchanged evidence sends nothing, wall-clock drift alone still sends
  nothing, then a new durable attempt is admitted while the abandoned
  reservation keeps its `ABANDONED` status and its exposure — plus real
  progress alone admitting the next consultation, another task proceeding, the
  preflight probe refusing to re-send itself end to end, the in-process loss,
  the unverifiable owner, and a **real second process** that reserves through
  the real `reserve` and must not be read as an orphan. Duplicate suppression
  is asserted with the ceiling provably free — `metered_call_allowed` True and
  headroom over a thousand allowances — so it cannot pass as budget exhaustion
  in disguise.
- **Focused:** `test_c19_jev_decisions` 68, `test_jev` 11, `test_control_plane`
  81 — 175/175 PASS. **Full: 1595/1595 PASS** (1569 before this correction).
- **Mutations 8/8 detected** on the guard: duplicate guard removed from
  admission · the purpose comparison dropped so the check matches nothing ·
  the call site abandoning the consultation identity for the task id alone ·
  the preflight probe reserving under a fresh identity each run · sweep
  removed from admission · unverifiable owner treated as alive · in-process
  loss not treated as abandoned · owner identity not recorded.
- **Mutations 6/6 detected** on the identity itself (§25.4): reverted to the
  task id only · each of the four fields dropped in turn · and the identity
  made to follow the clock by including `last_progress_at`. The last of
  these **initially survived an earlier round**, which exposed a real coverage
  hole: the concurrent-caller test injected the owner fields instead of
  letting `reserve` record them, so nothing proved the identity production
  writes. The test was rewritten to use a real second process, and the
  mutation is now detected.
- The original **8 C-19 mutations re-run and still 8/8 detected**; every
  mutation restored, restoration verified by SHA-256 and a re-run.
- `git diff --check` clean; `scripts/check_no_secrets.py` clean.

C-19 remains **OPEN**. No live call has been made; `jev_minimal_decision`
remains **NOT YET EVALUATED**. T+00 remains **NOT_STARTED**.

## 26. C-19 live verification — ATTEMPTED, NOT PERFORMED (2026-10-01)

> **Superseded by §27, which records the call actually being made.** This
> section stands as the record of the first attempt and of the guard refusing
> to spend without durable admission; §27 is the live result.

One live `worker_health` request was authorised, through the existing
budget-gated `jev_minimal_decision` gate, with no automatic retry. **The gate
refused before sending, and no request was made.** No money was spent and
nothing durable changed.

### 26.1 Pre-call verification

| Check | Result |
|---|---|
| Branch / tree | `wip/c05-1-persistence`, the C-19 work present and uncommitted |
| Configured model | `typesafe/jev-1.13` — the pinned build, not an alias |
| Governed allowance | $0.002688 per call (§24.5) |
| Ceiling | $25.00 |
| `OPENROUTER_API_KEY` | present — **checked by name only; no value was read, printed or logged** |
| Durable state document | **ABSENT** — `.runtime/state.json` does not exist |
| Unresolved duplicate probe | none — there are no reservations, because there is no document to hold them |

### 26.2 What happened

The gate was invoked on its own — `Preflight.gate_jev()` directly, not
`Preflight.run()` — so that nothing else ran and no `preflight.json` was
written over existing evidence. `urlopen` was wrapped in a counting spy that
**delegates to the real transport**, so "nothing left the machine" is an
observation rather than an assumption.

```
gate      : jev_minimal_decision
ok        : False
detail    : NOT EVALUATED: no state document, so a budget reservation
            cannot be made durable before sending
evidence  : {"not_evaluated": true, "reason_code": "NO_STATE_DOCUMENT"}
requests actually sent: 0
```

| Record | Value |
|---|---|
| Gate | **NOT EVALUATED** (`NO_STATE_DOCUMENT`) — neither PASS nor FAIL |
| Requested model | `typesafe/jev-1.13` — configured, never sent |
| Returned model | **none — no response exists** |
| Choice / confidence | **none — no answer exists** |
| Cost | **$0.00, known** — not "unknown cost": the request provably never left, which is the one case that is not ambiguous |
| Reservation | **none created**; the refusal precedes `reserve` |
| Remaining exposure | **$0.000000**, unchanged |
| Durable side effects | none — `state.json` still absent, `ledger.jsonl` still 0 bytes, no `preflight.json` |

### 26.3 Why, and what it does and does not show

C-19's own design requires the allowance to be committed to durable state
**before** the request leaves, so that a crash mid-flight cannot erase the
exposure (§24.4). With no state document there is nowhere to commit it, so the
gate refuses rather than sending an unreserved paid request. **The guard
behaved exactly as designed** — and this is the first time it has been
exercised against the real environment rather than a mock.

What this does **not** show: that the account can reach Jev, what a real
response looks like, or what the returned `model` string is. None of that is
established, and `jev_minimal_decision` therefore remains **NOT YET
EVALUATED** — a gate that refused to run is not a gate that passed. It shows
nothing about launch readiness, and it resolves no other part of C-19.

### 26.4 What would unblock it

The state document is created by `ctl init`, which initialises the run's
durable state — budget ceiling, provider records, migration lock, task graph.
That is a separate action with T+00 implications and **was not authorised
here**, so it was not taken. A later authorisation would need to cover it
explicitly, and the single-call scope would then apply unchanged: one
`worker_health` request, budget-gated, no retry, stopping on any failure or
ambiguity with the exposure retained.

T+00 remains **NOT_STARTED**. C-18 remains **OPEN at 3 of 7**.

## 27. C-19 live verification — PERFORMED (2026-10-01)

**Jev answered.** One live `worker_health` request, through the existing
budget-gated gate, authorised with no retry. This is the first time this
repository has ever reached Jev.

### 27.1 Preconditions, verified before the call

`ctl init` was run once, without `--force`, because §26's blocker was the
absent state document. It created the state document, nine tasks, provider
records AVAILABLE, the migration lock FREE and the $25 budget bucket; it
appended one `CONTROL_PLANE_INITIALISED` ledger event. It did **not** touch
`started_at`.

| Check | Value |
|---|---|
| Branch / HEAD | `wip/c05-1-persistence` at `6880229`, in sync with origin |
| `started_at` | **`None`** — T+00 **NOT_STARTED** |
| `baseline_sha` / `frozen_at` | `None` / `None` |
| Budget | $25.00 ceiling, $0.00 spent, $0.00 exposure, no `hard_stop` |
| Reservations | none; `duplicate_blocked` False for the gate purpose |
| Allowance | $0.002688 |
| Model | `typesafe/jev-1.13` |
| `OPENROUTER_API_KEY` | present — **by name only; no value read, printed or logged** |

### 27.2 The result

`Preflight.gate_jev()` invoked alone — not `Preflight.run()` — so nothing else
ran and no `preflight.json` was written. `urlopen` was wrapped in a counting
spy that delegates to the real transport and records the URL only, never the
request object, which carries the Authorization header.

| Record | Value |
|---|---|
| **Gate** | **PASS** — `jev_minimal_decision`, detail "Jev returned HEALTHY" |
| Requests sent | **1**, to `https://openrouter.ai/api/alpha/decisions` |
| Requested model | `typesafe/jev-1.13` |
| **Returned model** | **`typesafe/jev-1.13-20260917`** |
| Choice | `HEALTHY` — a member of the frozen enum, `source == "jev"` |
| Confidence | **0.98** — carried through, advisory, acted on by nothing |
| Cost | **$0.000022764, reported by the provider** — known, not unknown |
| Tokens | 542 input, 55 output |
| Duration | 487.5 ms |
| Billing state | `BILLED` |
| Reservation | created at $0.002688, **settled as actual spend and removed** |
| Exposure after | **$0.000000**; `reservations` empty; no abandoned entries |
| `spent_usd` after | $0.000023 |
| `started_at` after | **still `None`** |

### 27.3 What the real response confirms

1. **The pricing basis in §24.5 is exact.** 542 × $0.000000042 =
   $0.000022764, which is the cost the provider reported to the digit. The
   bound's price input is confirmed against a real invoice line rather than a
   worked example in documentation.
2. **The reservation was conservative, as designed** — $0.002688 against an
   actual $0.000022764, about 118× headroom. 542 tokens against the 64,000
   ceiling the bound assumes.
3. **The returned model is the dated build the docs describe**, and it begins
   with the requested slug — §23.7's post-call abort conditions all pass, so
   nothing bars a later call. This string is the value BOOTSTRAP item 6
   requires recording before freeze. It still does **not** prove immutable
   weights, and is not described as doing so.
4. **Settlement behaved exactly as C-19 specifies**: known cost became actual
   spend, the reservation was removed rather than counted twice, and exposure
   returned to zero.

**One honest observation, not introduced by C-19:** `budget.record` rounds to
six decimal places, so $0.000022764 accumulates as $0.000023. The exact
provider figure survives on the `Decision` and in gate evidence; only the
running total is rounded. At this ceiling it is immaterial, but it is rounding,
not truncation, so over many calls it can drift either way.

### 27.4 What this does NOT establish

- **Not launch readiness.** One gate passed. The full preflight has not been
  run; the other 23 gates remain NOT YET EVALUATED, `host_headroom` was
  failing when last measured, and `clean_baseline` depends on the tree.
- **No `PREFLIGHT_GATE` ledger event exists for this**, because the gate was
  invoked directly rather than through `Preflight.run()`, deliberately, so no
  `preflight.json` was written over anything. The durable trace of the spend
  is `state.json`'s `spent_usd`; this section is the evidence record.
- **C-19 is not closed.** Its live-verification requirement is now satisfied,
  but the alpha endpoint remains alpha, and four governance questions are
  still open and unresolved: `model_routing` precedence,
  `incident_classification` precedence, `queue_priority` criteria, and whether
  the `finding_severity` reviewer path should use Jev at all.
- C-18 remains **OPEN at 3 of 7**. C-05.3b has not begun. T+00 remains
  **NOT_STARTED**.

## 16. Current status and next action

**Done in the 2026-09-30 session:** reuse-branch bypass repaired (§12) ·
rehearsal plan drafted, unauthorised (§13) · C-18 plan above (§14) ·
`skills/README.md` source located (§15) · `experiment/LAUNCH-CHECKLIST.md`
authored.

**Done 2026-10-01:** `skills/README.md` imported and hash-verified (§17) ·
C-18 **stage 1** implemented, tested and mutation-checked (§18) · C-18
**stage 2** implemented, tested and mutation-checked (§19), then corrected on
three findings and re-verified (§20 — authoritative over §19) · C-18
**stage 3** implemented, tested and mutation-checked (§21) · **C-19 recorded**
— the Jev implementation is not Jev; discrepancy logged in the audit and a
repair proposal drafted (§22) and then refined after review (§23, which
wins over §22), nothing implemented · **C-19's `worker_health` path then
implemented, tested and mutation-checked (§24)** — real Decisions API
transport, durable budget reservation before sending, both model identifiers
recorded, unapproved kinds refused before HTTP · **§24's crash/restart
duplicate-call claim verified, found FALSE, and corrected (§25, authoritative
over §24.4's crash paragraph)** — a reservation lost in flight is now
`ABANDONED`, and that one logical consultation is never re-sent, while a new
worker attempt or real durable progress poses a new question that is. Still no
live call.

**Verified now:** 1595 tests OK (1511 before the C-19 work, 1569 before the
crash/restart correction) · 8/8 C-19 mutations plus 5/5 crash-guard mutations
detected and restored · `git diff --check` exit 0 ·
`scripts/check_no_secrets.py` clean · nothing staged or committed.

**Next action — two, and they are independent.**

**(a) C-19 live verification, and it gates a launch gate.** The code path now
exists and is mocked-verified (§24); what is missing is one real call. Authorise
or refuse §23.7's scope: exactly one `worker_health` Choice request,
budget-gated on both paths, aborting before sending if the computed
worst-case does not fit the remaining ceiling. Until that call is made and
its returned `model` string recorded, `jev_minimal_decision` remains **NOT
YET EVALUATED** and must not be marked PASS from mocks. Four governance
questions remain open and are not blocking this path: `model_routing`
precedence, `incident_classification` precedence, `queue_priority` criteria,
and whether the `finding_severity` reviewer path should use Jev at all.

**(b) C-18 **stage 4** — `dispatch_builder`'s plan/execute/commit
split (§14.1, §14.3). Stage 3 supplied the port halves it needs (§21); the
remaining hazard named in §14.1 is C-15's migration-lock fresh-vs-re-entrant
semantics, which must be re-established from durable evidence once the spawn
moves outside T1. Stage 4 is **not** authorised by this document.

Also unassigned and now explicitly named: `merge_invariant.annunciate`'s
direct `notifier.send`, the one remaining synchronous outbound send reached
from T1 (§19.7 item 3, §20.6 item 5). It is outside every numbered stage and
needs its own authorised change.

**Still open, unchanged:** C-18 stages 4–7 · C-05.3b (not begun) · C-04 ·
C-02 · C-04a realistic multi-cycle preflight (not run) · the five-hour
endurance rehearsal (PROPOSED, D5–D10 unanswered, D7 blocking) · the
`host_headroom` inotify failure · `clean_baseline` (the tree is uncommitted).
T+00 remains **NOT_STARTED**.

## 9. Session constraints observed

- Nothing staged, nothing committed.
- All tests and probes ran against temporary directories with mocked providers
  and mocked processes. No live launch, no paid provider calls.
- C-05.3b not begun. C-18 not fixed and not extended. T+00 remains
  **NOT_STARTED**.

*(Section 9 described the read-only investigation session that produced the
plan. The implementation session that followed is recorded in section 10.)*

## 28. C-04 CI-result and reviewer-identity adapters — IMPLEMENTED (2026-10-01)

Branch `wip/c04-adapters`, based on `wip/c05-1-persistence` at f2aa539. Scope
was exactly the two missing C-04 live-gate adapters and their tests. Nothing
under `product/`, `tasks/`, `protocol/` or `config/tasks.json` was touched, and
`apparatus/pr-evidence/validate.js` was deliberately **not** modified — another
agent owns it. The proposed diff for it is recorded in §28.6 rather than
applied.

### 28.1 What was built

**`apparatus/adapters/ci-result.js`** — replaces the self-asserted
`pkg.ci.status` read at `validate.js:81-82` with a question GitHub answers:
did every governed required check complete successfully for *exactly* this
40-character head SHA?

- `resolveCiResult(headSha, { repo, requiredChecks, fetchCheckRuns })` is the
  low-level function. `fetchCheckRuns` is **required and not defaulted**, so no
  call can reach the network unless a caller explicitly supplies a fetcher.
  Every test injects a stub; the real `gh api` path is never executed under
  `node --test`.
- `resolveRun002CiResult(headSha, options)` is the real entrypoint. It reads
  `github.repo` and `github.required_checks` from this module's own
  repository's `config/experiment.json` (today `serina-mcfall/wellbeing-run-002`
  and `["ci"]`) and uses
  `gh api repos/<repo>/commits/<sha>/check-runs?per_page=100` unless
  `options.fetchCheckRuns` overrides it.
- Passing conclusions are an **allow-list of exactly `success`**. GitHub's
  `neutral`, `skipped`, `stale`, `cancelled`, `timed_out` and `action_required`
  all mean the check did not actually verify this commit, so none of them may
  stand in for a pass.
- A required check that GitHub never ran is `REQUIRED_CHECK_MISSING`, not an
  absence to shrug at. This closes the "zero meaningful checks" hole that a
  bare `check_runs.length > 0` test would leave open.

**`apparatus/adapters/reviewer-identity.js`** — replaces the string allow-list
at `validate.js:47` / `:84-87`. `ALLOWED_REVIEW_PRODUCERS = new Set(['Codex
Reviewer'])` tests a name the submitting agent typed about itself; any worker
can type it. Protocol v2 §"PR contract" is explicit: *"Agents cannot satisfy
independent boxes by self-attestation."*

The adapter's trusted sources are the two durable Supervisor records, never the
evidence package:

- the append-only ledger (`.runtime/ledger.jsonl`) — `REVIEW_RESULT` carries
  `agent_id`, `role`, `provider` and `outcome`, written by the Supervisor;
- the PR record in durable state (`.runtime/state.json`, `doc.prs[n]`) —
  `reviewed_head`, the SHA the reviewer worker was actually dispatched against
  (`control/supervisor.py::dispatch_reviewer`, line 877).

Seven checks, each failing closed:
`UNKNOWN_REVIEW_PRODUCER` → `NO_REVIEW_EVIDENCE` → `FORGED_REVIEWER_IDENTITY`
(the `agent_id` must match the Supervisor's own reviewer worker-name shape,
`<task-id-lowercased>-review-<cycle>`) → `NOT_INDEPENDENT_PROVIDER` (must be
`codex`) → `SELF_REVIEW` (the same worker must never have acted as builder or
fixer) → `REVIEW_VERDICT_MISMATCH` (the package's verdict must be the verdict
the reviewer actually returned) → `REVIEW_SHA_MISMATCH`.

`loadReviewerEvidence(runtimeDir, prNumber)` is the directory-agnostic loader,
split out exactly as `git-head.js` splits `resolveTrustedHeadSha` from
`resolveRun002TrustedHeadSha`, so the real file-reading path is unit-testable
against temp fixtures. `resolveRun002ReviewerIdentity(claim)` hard-wires the
runtime directory from this module's own location and accepts no override, so
no caller can point it at another experiment's ledger.

### 28.2 Verified false vs could not verify

Both adapters tag every failure with a `determination` of `VERIFIED_FALSE` or
`COULD_NOT_VERIFY`, and both document that `COULD_NOT_VERIFY` is **not** weak
evidence of passing. A caller that treats it as healthy has reintroduced the
defect the adapters exist to remove. Tests assert the determination, not only
the reason code, on every failure path.

### 28.3 Defects found while building

1. **The ledger does not bind a review to a SHA.** `REVIEW_DISPATCHED` and
   `REVIEW_RESULT` record task, PR, role, provider, `agent_id` and verdict —
   and no head SHA. The *security* worker's name is SHA-bound by construction
   (`control/routing.py::security_worker_name`,
   `<task>-security-<sha>-<ordinal>`); the code reviewer's is not — it is
   `<task>-review-<cycle>` (`control/supervisor.py::dispatch_reviewer`, line
   870). This is an asymmetry between two evidence producers facing the same
   forgery risk.
2. **The only SHA binding available is mutable.** `prs[n].reviewed_head` lives
   in `.runtime/state.json` and the next review cycle overwrites it. The
   adapter therefore verifies the **current** review cycle only.
3. **The PR-evidence package carries no PR number.**
   `protocol/PR-EVIDENCE-V2.schema.json` has `task_id` and `head_sha` but no
   `pr_number`, so the reviewer-identity adapter cannot be driven from the
   package alone — the caller must supply the PR number out of band. That is
   why §28.6's proposed `checkLiveGate` takes `options.prNumber`.
4. **`validate.js`'s header comment is now out of date.** Lines 12-16 say the
   CI and worker-registry adapters "do not exist in this repository yet". Two
   of the four now do. Its owner needs to correct that text; this branch did
   not.
5. **`.github/workflows/ci.yml` still never calls any of this.** Confirmed
   again this session: the workflow runs `python -m unittest discover -s tests`
   and `scripts/check_no_secrets.py`, and runs Node steps only if a root
   `package.json` exists — which it does not. So neither these adapters nor
   `validate.js` run in CI today. Already recorded in the C-04 audit row and
   still part of its closure criteria.

### 28.4 Decisions taken

- **Only `required_checks` is judged.** Checks outside
  `config/experiment.json`'s `github.required_checks` are reported in
  `observedChecks` but do not block. That list is the human-declared definition
  of "CI" for Run 002; widening the gate inside an adapter would be a gate
  change nobody approved. A test pins the boundary so it cannot drift silently.
- **One page of check runs is read** (`per_page=100`, no `--paginate`, because
  `gh --paginate` concatenates JSON objects and breaks `JSON.parse`). More than
  100 check runs on one commit would leave a required check unseen, which is
  reported as `REQUIRED_CHECK_MISSING` — a fail-closed outcome, never a false
  pass.
- **Duplicate runs under one required name must all pass.** A re-run that
  leaves a failed entry under the required name still fails.
- **The latest `REVIEW_RESULT` governs.** The ledger is append-only and
  chronological, so the last matching event is the current cycle's verdict; an
  earlier cycle's `REVIEW_PASS` cannot cover a later `REVIEW_FAIL`.
- **Constants are drift-guarded by test, not by comment.**
  `INDEPENDENT_REVIEW_PROVIDER` is asserted equal to `config/experiment.json`'s
  `roles.reviewer.provider`, and `CODEX_REVIEWER_PRODUCER` is asserted to
  appear verbatim in Protocol v2's "Agent organisation" line.
- **The audit was not edited.** `experiment/CONTRADICTION-AUDIT.md` rows C-04
  and C-04a still say the CI-result and reviewer-identity adapters are unbuilt.
  That is now false, but the file has concurrent uncommitted edits from another
  stream, so this branch left it alone. Whoever reconciles the audit should
  update both rows and cite
  `experiment/evidence/C-04-ci-reviewer-adapters-node-test.txt` and
  `experiment/evidence/C-04-ci-reviewer-adapters-mutation.txt`.

### 28.5 What these adapters CANNOT establish

Stated plainly, because a reader skimming §28.1 could easily over-read it.

**Without a real CI run** (no product PR exists; T+00 has not occurred):

- Nothing here has ever been pointed at live GitHub. Every test injects a stub
  fetcher. The `gh api` call path, its JSON shape in practice, its auth
  behaviour, its rate limiting and its pagination behaviour are **unexercised**.
- The adapter cannot establish that `required_checks` names the checks that
  actually matter; it establishes only that the checks a human declared are the
  ones it enforces.
- It cannot tell a check run that genuinely tested the code from one produced
  by a workflow that was itself weakened. Protocol v2 §"Anti-cheating" forbids
  "weakening gates after seeing results"; this adapter has no view of that.
- Check runs are read for a commit, not for a pull request. A commit that is
  green but is not the PR's head is caught only because the SHA is supplied by
  the caller — the trusted-SHA half remains `git-head.js`'s job, and the
  composition that joins them does not exist yet.

**Without a real worker registry** (no run has occurred, so `.runtime/` does
not exist — it is gitignored and absent in any fresh checkout):

- The adapter has never read a real ledger or a real `state.json`. Every
  fixture is synthetic and hand-shaped to match what `control/supervisor.py` is
  read to write.
- It cannot bind a review to a SHA from durable append-only evidence, for the
  reason in §28.3 item 1. It can prove only that the Supervisor's *current*
  `reviewed_head` is the SHA being claimed. It **cannot** prove, from durable
  evidence alone, that an older cycle's `REVIEW_PASS` was not re-presented for
  a newer commit. Closing that needs a head SHA in the review ledger events, or
  a SHA-bound reviewer worker name as the security worker already has.
- It verifies that the Supervisor dispatched a worker named like a reviewer
  with provider `codex`. It does **not** verify that the Codex process that ran
  inside that worker was the model the role config names, that it had no
  network, or that its output was not edited in transit. Worker-process
  identity is Supervisor/Watchdog worker-awareness (BOOTSTRAP deliverable 2),
  not this adapter.
- `state.json` is mutable and is not signed. An actor who can write it can
  change `reviewed_head`. The ledger is append-only and is the stronger of the
  two sources; the SHA binding unavoidably rests on the weaker one today.

**Neither adapter resolves C-04.** They are two of four adapters. The
composition layer that computes a real live-gate decision still does not exist,
`validate.js` is unchanged, and the C-04a realistic multi-cycle fixture
preflight has not been run. `policyValid: true` plus these two adapters is
still not launch readiness, and this branch claims none.

### 28.6 Proposed `validate.js` change — NOT APPLIED

Recorded here rather than made, because another agent owns that file. It adds a
function and changes no existing behaviour; `checkOfflinePolicy` keeps its
current signature and semantics.

```diff
--- a/apparatus/pr-evidence/validate.js
+++ b/apparatus/pr-evidence/validate.js
@@
 const { applySeverityPolicy } = require('../severity/severity-floor.js');
+const { resolveRun002CiResult } = require('../adapters/ci-result.js');
+const { resolveRun002ReviewerIdentity } = require('../adapters/reviewer-identity.js');
@@
-module.exports = { checkOfflinePolicy: checkOfflinePolicy };
+// Live gate = offline policy AND every live adapter. Never OR, and never
+// "the offline half passed, so ship it". An adapter that COULD_NOT_VERIFY
+// blocks exactly as hard as one that VERIFIED_FALSE; only ok:true clears.
+// prNumber is an explicit argument because the evidence package has no
+// pr_number field to read it from (see handover section 28.3 item 3).
+function checkLiveGate(pkg, options) {
+  options = options || {};
+  const offline = checkOfflinePolicy(pkg);
+  const errors = offline.errors.slice();
+
+  const ci = (options.resolveCi || resolveRun002CiResult)(pkg && pkg.head_sha);
+  if (!ci.ok) {
+    errors.push('CI_UNVERIFIED[' + ci.determination + ']: ' + ci.reason + ' - ' + ci.detail);
+  }
+
+  const review = pkg && pkg.review;
+  const identity = (options.resolveReviewer || resolveRun002ReviewerIdentity)({
+    taskId: pkg && pkg.task_id,
+    prNumber: options.prNumber,
+    headSha: pkg && pkg.head_sha,
+    producer: review && review.provenance && review.provenance.producer,
+    verdict: review && review.verdict,
+  });
+  if (!identity.ok) {
+    errors.push('REVIEWER_UNVERIFIED[' + identity.determination + ']: ' + identity.reason + ' - ' + identity.detail);
+  }
+
+  return {
+    policyValid: offline.policyValid,
+    liveGateEligible: errors.length === 0,
+    errors: errors,
+  };
+}
+
+module.exports = { checkOfflinePolicy: checkOfflinePolicy, checkLiveGate: checkLiveGate };
```

The header comment at `validate.js:12-16` also needs correcting — see §28.3
item 4. Note that `checkLiveGate` as drafted does **not** remove `CI_NOT_PASS`
or `SELF_ATTESTED_OR_UNKNOWN_PRODUCER` from `checkOfflinePolicy`; both remain,
now as the offline half of a two-source check. Removing them would weaken the
offline checker for callers that have no live sources, which is the wrong
direction.

### 28.7 Verification performed

- `node --test apparatus/adapters/*.test.js` — **57 passed, 0 failed, exit 0**
  (8 `git-head`, 5 `task-record`, 20 `ci-result`, 24 `reviewer-identity`).
  Recorded in `experiment/evidence/C-04-ci-reviewer-adapters-node-test.txt`.
  Note: `node --test apparatus/adapters/` (directory form) does **not** work on
  Node 24 — it resolves the path as a module and fails with MODULE_NOT_FOUND.
  The glob form above is the working command.
- **Mutation check: 17 guards broken one at a time, 17/17 detected.** Each
  mutation was applied, the suite re-run, the file restored from its
  pre-mutation bytes, and restoration verified by sha256 comparison; the suite
  was re-run after all restorations and returned to 57/0. Recorded in
  `experiment/evidence/C-04-ci-reviewer-adapters-mutation.txt`. The guards
  exercised were: the `success`-only conclusion allow-list; wrong-commit check
  runs; absent required checks; incomplete checks; zero check runs; the
  required fetcher; malformed head SHA; the producer allow-list; the anchored
  reviewer worker-name pattern; the independent-provider check; the self-review
  check; the verdict comparison; the stale-SHA check; the malformed
  `reviewed_head` check; the ledger role filter; the unparseable-ledger-line
  refusal; and the `ledgerEvents` array guard.
- `python3 -m unittest discover -s tests` — **1595 tests, OK**. The `[FAIL]
  c16_probe` lines in that output are deliberate probe output inside passing
  tests, not failures.
- `npm test --prefix apparatus` (the whole Node apparatus suite) — 78 passed,
  2 failed. Both failures are **pre-existing and unrelated**:
  `apparatus/pr-evidence/validate.test.js` cannot resolve `ajv/dist/2020` and
  `apparatus/accessibility/run.test.js` cannot resolve Playwright, because
  `apparatus/node_modules` is absent in a fresh worktree and no install was run
  (installing would be a network call this session refused to make). Both new
  adapters have **zero dependencies**, which is why they pass without it.
- `python3 scripts/check_no_secrets.py` — clean, 191 tracked files.
- `git diff --check` and `git diff --cached --check` — clean.
- No network calls, no paid API calls, no notifications, no worker launches.

## 30. C-18 shared dispatch harness + stage 4 (builder) — implemented (2026-10-01)

**The harness plus stage 4 only.** C-18 stays **OPEN**: stages 5, 6 and 7
(§14.3) are untouched, and so is the `merge_invariant.annunciate` residual
(§19.7 item 3, §20.6 item 5).

Branch `wip/c18-stage4-builder`, off `wip/c05-1-persistence` at `f2aa539`.

### 30.1 Why a harness came first

An independent analysis found that stages 4, 5 and 6 cannot be built in
parallel: they collide on `tick()` itself, the dispatch-claim schema,
`has_worker`, `on_dispatch_failure`, and the stage-3 boundary-marker test.
This session therefore treats **the harness as the deliverable** and the
builder as its first consumer, so that reviewer and fixer can then be built
independently on top of it.

### 30.2 The harness interface — enough to use without reading the diff

A role joins the harness by supplying **three methods and one table entry**.
Nothing in `execute_dispatches`, `apply_dispatch_result`,
`confirm_dispatches` or `resume_dispatch_claims` changes.

```python
DISPATCH_HANDLERS = {
    "builder": _DispatchHandler(execute="_execute_builder_dispatch",
                                commit="_commit_builder_dispatch",
                                fail="_fail_builder_dispatch"),
    # stage 5 adds "reviewer", stage 6 adds "fixer"
}
```

**1. Plan — inside T1, STATE ONLY.**

```python
claim = state.new_dispatch_claim(
    role="reviewer", task_id=task["id"], worker=worker, branch=branch,
    claimed_at=clock.iso(self.now()),
    lease_expires_at=self._lease_expires("reviewer"),
    pr=pr_number,                 # None for the builder
    port_candidates=candidates,   # () for a role that needs no port
    observed_head=head,           # a full 40-hex SHA, or None
    context={...})                # role-private durable facts
state.dispatch_claims(task)["reviewer"] = claim
self._plan_dispatch(self.dispatch_plan(claim))
```

`new_dispatch_claim` raises `ValueError` on anything the rest of the control
plane would later refuse — unknown role, a worker name that is not one safe
path component, a non-positive PR, a short or uppercase SHA, a naive
timestamp, a lease that does not outlast the claim, non-integer port
candidates. Catch it and fail the dispatch; a claim that can never spawn
would hold the role's slot forever.

`_plan_dispatch` is an **accumulator, not a return value**, and that is the
whole reason it exists: reviewer and fixer plans originate inside
`reap_workers`' role-finished callbacks, which are nested in T1 and cannot
return anything to `tick`. Pinned by
`test_plan_dispatch_accumulates_and_escapes_a_reap_callback`.

**2. Execute — after T1 commits, NO LOCK HELD.**

```python
def _execute_reviewer_dispatch(self, plan: DispatchPlan) -> DispatchResult
```

May do anything external. **Must not touch the state document**: by the time
it runs, the document T1 read has committed and may have moved on.
Everything it needs travels on the frozen `DispatchPlan`
(`role, task_id, pr, worker, branch, port_candidates, observed_head,
context`), whose `context` is a read-only mapping copied out of the claim.

**3. Commit / fail — inside a state transaction, STATE ONLY.**

```python
def _commit_reviewer_dispatch(self, doc, plan, result) -> None
def _fail_reviewer_dispatch(self, doc, plan, result) -> None
```

Both run under a lock — `confirm_dispatches`' own per-result transaction on
the ordinary path, and **T1 itself on the recovery path** — so they must be
state-only and re-entrant. Both **must** re-verify identity with
`self._current_dispatch_claim(doc, plan)` before changing anything, and both
**must** clear the claim they acted on with `self._clear_dispatch_claim`.

`_current_dispatch_claim` returns `(task, claim)` and re-checks task
association, role, worker name and PR. `(task, None)` means superseded;
`(None, None)` means the task is gone.

**Recovery is free.** A role that stores a claim gets it:
`observe_dispatch_claims(snapshot, entries)` reads each claim's job file
before the lock; `resume_dispatch_claims(doc, observations)` then either
commits a claim whose job file proves it spawned — through the role's own
`commit`, from the job file's recorded worktree and port — or re-plans one
that never got that far. A claim with no bound observation is left alone.

**Slot reservation is free too.** `has_worker` consults
`has_dispatch_claim`, so a planned-but-unspawned reviewer or fixer stops
`route_awaiting_dispatch` dispatching a second one. `dispatchable` counts
active builder claims against `max_builders` for the same reason.

**Fixed positions in `tick()`:**

| Where | What |
|---|---|
| Before the lock | `observe_dispatch_claims` — only when an active claim exists |
| T1, first | `resume_dispatch_claims` — **before** `reap_workers`, so a recovered worker is reaped in the same tick |
| T1, as before | `route_evidence` · `route_prs` · `dispatchable` → `dispatch_builder` |
| After T1, first | `confirm_dispatches(execute_dispatches(self._dispatch_plans))` |

`self._dispatch_plans` is reset at the top of every tick. Nothing durable
lives in it.

### 30.3 The dispatch claim

`task["dispatch_claims"][role]` — one key per role, so a task holds at most
one in-flight dispatch per role. Accessors setdefault, so a state document
written before this key existed works unchanged (`budget.reservations` and
`notify.queue` follow the same rule).

| Field | Meaning |
|---|---|
| `role` · `task_id` · `pr` · `worker` · `branch` | identity; `pr` is None for a builder |
| `port_candidates` | the ordered list `select_port_candidates` chose UNDER the lock; the bind proof happens outside |
| `observed_head` | the commit a pre-lock observation was taken at (stage 5's head-moved rule) |
| `claim_state` | `PLANNED` or `SPAWNED`; both ACTIVE |
| `claimed_at` · `lease_expires_at` | timezone-aware, lease strictly later |
| `context` | role-private durable facts, copied not aliased |

A claim is **removed**, never parked in a terminal state, once its outcome
commits — so "active" and "present" mean the same thing and there is one
rule to remember. A malformed claim is deliberately **not** active: it
reserves nothing and carries no external effect of its own, so a later tick
may replace it rather than the task being stranded forever.

### 30.4 Stage 4: what left T1

| Call | Was | Now |
|---|---|---|
| `prompts.write` | inside T1 | `_execute_builder_dispatch` |
| `workers.create_worker` (workmux/git subprocess) | inside T1 | `_execute_builder_dispatch` |
| `workers.worktree_path` (workmux subprocess) | inside T1 | `_execute_builder_dispatch` |
| `workers.allocate_port` (**real 127.0.0.1 bind**) | inside T1 | split: `select_port_candidates` in T1 (no bind), `probe_port` in execute |
| `workers.write_job` | inside T1 | `_execute_builder_dispatch` |
| `workers.start_job` (process/tmux spawn) | inside T1 | `_execute_builder_dispatch` |

Commit-side state — `attempts += 1`, the ASSIGNED transition, `worker`,
`branch`, `assigned_at`, `last_progress_at`, `progress_marker`, the
`doc["workers"]` record and `retained_worktrees.pop` — commits afterwards,
in `_commit_builder_dispatch`. `attempts` still counts exactly one per real
dispatch, and still zero for a dispatch that failed.

**One failure path deliberately does not cross the boundary.** An empty
candidate list from `select_port_candidates` is decided in T1, so
`on_builder_dispatch_failure` is reached from the same transaction that
acquired the migration lock and the in-memory freshness answer is exactly as
durable as the acquire it came from.

### 30.5 The C-15 hazard, and how it is resolved

`lock_newly_acquired` was a local boolean — `owner_before is None` — read a
few statements earlier in the **same** transaction that acted on it. Once a
dispatch failure can be handled after T1 has committed, that boolean
describes a document that no longer exists: between the two transactions the
lock can be released, re-acquired by this task, or acquired by another.

`Supervisor._migration_lock_release_permitted(doc, plan)` rebuilds the
answer from **four durable facts, all of which must hold**:

1. **This plan acquired the lock rather than inheriting it** —
   `context["lock_newly_acquired"]`, written under the lock that did it.
2. **This task still owns the lock** — `migration_lock.owner_task`.
3. **It is still the same ownership episode** —
   `migration_lock.acquired_at` equals `context["lock_acquired_at"]`.
   `acquired_at` is set on a fresh acquisition, left untouched by a
   re-acquire from the same owner, and cleared on release, so an episode
   that ended and began again has a different value and this plan knows
   nothing about the new one.
4. **No builder has been dispatched under that episode** —
   `task["assigned_at"]` is written only by a committed builder dispatch, so
   `assigned_at >= acquired_at` is literally C-15's "a previous builder ran
   while it held the lock", read from state instead of inferred from
   control flow.

Every disagreement answers **False**, which RETAINS the lock. That is the
safe direction: retaining a lock no builder ever used costs a human
decision; releasing one a builder mutated schema under is the corruption
C-15 exists to prevent.

Timestamps are compared as **datetimes, never as strings**. Pacific/Auckland
changes offset twice a year, and across that boundary a later instant can
sort earlier lexicographically — `02:10+12:00` is later than `02:30+13:00`
but sorts before it. An unparseable timestamp answers False. Pinned by
`test_the_comparison_survives_a_daylight_saving_offset_change`.

`on_builder_dispatch_failure`'s signature, both branches, its fail-closed
`RuntimeError`s and its intervention are **unchanged**. All 18 pre-existing
C-15 lifecycle tests pass with no assertion altered.

**This is strictly more conservative than the pre-C-18 code in one case that
code could not reach**: a claim whose ownership episode changed between plan
and commit is now treated as re-entrant (HUMAN_REQUIRED) rather than
released. Stated rather than hidden.

### 30.6 Controls preserved

`execute_dispatches` re-reads the committed document and refuses to spawn on
`self.stopping`, `frozen_at`, a paused provider policy
(`providers.may(doc, "new_builds")`, or `"review"` for the reviewer) and
`providers.safe_hold` for the builder. Every one of these already governs
this dispatch somewhere else; none is invented. Controls that cannot be read
answer `CONTROLS_UNREADABLE` and **block** — a document whose controls are
unreadable is not a document that permits spending. A held plan is not an
error: its claim stays exactly as it is and the next tick resumes it.

### 30.7 Files changed

| File | Change |
|---|---|
| `control/state.py` | `re` import; `DISPATCH_*` constants; `dispatch_claims`; `dispatch_claim`; `dispatch_claim_active`; `_aware_moment`; `new_dispatch_claim` |
| `control/supervisor.py` | `DispatchPlan`, `DispatchObservation`, `DispatchResult`, `_DispatchHandler`; `DISPATCH_HANDLERS`; `_plan_dispatch`, `dispatch_plan`, `observe_dispatch_claims`, `resume_dispatch_claims`, `_dispatch_block`, `execute_dispatches`, `apply_dispatch_result`, `confirm_dispatches`, `_current_dispatch_claim`, `_clear_dispatch_claim`; `dispatch_builder` rewritten as a planner; `_execute_builder_dispatch`, `_commit_builder_dispatch`, `_fail_builder_dispatch`, `_migration_lock_release_permitted`; `has_worker` plus `has_dispatch_claim`; `dispatchable` counts claims; `tick` wiring; `_dispatch_plans` in `__init__` |
| `control/workers.py` | `allocate_port` docstring corrected — the builder no longer calls it |
| `tests/test_c18_stage4_dispatch_harness.py` | **NEW** — 67 tests |
| `tests/test_c18_stage3_port_split.py` | boundary marker rewritten (see §30.9) |
| `tests/test_migration_lock_lifecycle.py` | `dispatch_fail` helper drives the three phases; **no assertion changed** |
| `tests/test_c09_resource_lifecycle.py` | `TestDispatchPopulation.dispatch` helper, same; **no assertion changed** |

`control/merge_invariant.py` was not touched. No frozen source
(`product/`, `tasks/`, `protocol/`, `config/tasks.json`) was touched.

### 30.8 Verification

```
python3 -m unittest discover -s tests   →  Ran 1664 tests ... OK   (was 1595)
python3 scripts/check_no_secrets.py     →  no secret-shaped material, 185 files
git diff --check                        →  exit 0
preflight.gate_protocol()               →  ok=True, missing: []
```

The `[FAIL] c16_probe` stderr lines remain the C-16 deliberate-exception
probe, not failures.

**The transaction-boundary proof is measured, not structural.** A spy
records `lock_is_held(store.lock_path)` — an independent `open()` plus
`flock(LOCK_NB)` against the real lock file — at the moment of every
`prompts.write`, `create_worker`, `worktree_path`, `probe_port` and
`start_job` in a full real tick, and a second test leaves `probe_port`
unmocked so a **genuine 127.0.0.1 bind** is measured through a spy socket.
Two companion tests prove the probe detects a held lock and that the spy
reports `True` for a deliberate in-transaction call, so neither guard can be
green because the instrument is broken.

**Mutations — 6 run, 6 caught.** `control/supervisor.py` and
`control/state.py` restored byte-identical, SHA-256 verified before and
after (`8127a2e5…` and `8e59bce3…`), and the full suite re-run green.

| Mutation | Result |
|---|---|
| **M1 the harness is bypassed** — `dispatch_builder` executes and commits inline inside T1 | **caught, 8 tests.** The boundary spy reports `[('prompts.write', True), ('create_worker', True), ('worktree_path', True), ('probe_port', True), ('start_job', True)]` against the expected all-False |
| **M2 an external call returns to T1** — the composed `allocate_port` restored under the lock | **caught, 5 tests.** The real-bind spy reports `[True, False]` against `[False, False]`; the stage-3 marker also fires |
| **M3 the claim is invisible to `has_worker`** | caught, 3 tests — a second reviewer and a second fixer are both dispatched |
| **M4 the C-15 branch is mis-derived** — the remembered flag alone, no re-derivation | caught, 7 tests |
| **M5 `attempts` counted at plan instead of at commit** | caught, 3 tests |
| **M6 the C-15 release is always permitted** — the dangerous direction | caught, 13 tests, including 4 pre-existing C-15 lifecycle tests |

Every test uses temporary directories, mocked processes and mocked GitHub.
No worker was launched, no notification sent and no paid call made. The only
socket operation anywhere is a loopback bind on a port the test first proved
was free.

### 30.9 The stage-3 boundary marker, moved on rather than deleted

`StageThreeDidNotMigrateTheDispatchSites` asserted
`source.count("workers.allocate_port(doc)") == 2` plus the absence of
`select_port_candidates` and `probe_port` from `supervisor.py`. Stage 4
migrating the builder is **exactly the event that marker existed to make
visible**, so it fired as designed. It is now
`OnlyTheFixerStillBindsUnderTheLock` and pins the new boundary: exactly ONE
composed call remains, that call is inside `dispatch_fixer`, the builder
uses `select_port_candidates` in its planning half and `probe_port` in its
execute half, and the surviving site is named so "stage 4 done" can never be
read as "all port binds have left T1".

Declared in `.claude/.test-change` before the edit, together with the two
helper re-points.

### 30.10 What stage 4 does NOT claim, and known limitations

1. **Port binds have NOT fully left T1.** `dispatch_fixer` still calls the
   composed `workers.allocate_port(doc)` under the lock. Stage 6.
2. **`dispatch_reviewer` is unchanged** and still makes `gh.pr_diff_sha`,
   `evidence.collect` and `routing.material_diff_hash` calls inside T1.
   Stage 5.
3. **`merge_invariant.annunciate` still sends synchronously inside T1.**
   Untouched by instruction; it is a separate integration task.
4. **The orphan-worktree window is longer than it was.** If `create_worker`
   succeeds and the process then dies before the commit, a worktree exists
   that `retained_worktrees` does not yet own, so `reconcile.detect_orphans`
   may report it. The same exposure existed before C-18 — a failed
   `start_job` committed BLOCKED with no retained entry either — but the
   window now spans a crash rather than a few statements. Recovery still
   converges: the claim names the worker, and the next tick either commits
   it from its job file or re-plans it.
5. **`start_job` success is not proof a process is running.** It reports
   that `tmux send-keys` or `Popen` succeeded. Unchanged from before, and
   the job file plus C-09's lease machinery remain the real evidence.
6. **A fail-closed `RuntimeError` in the commit phase re-runs the external
   work on the next tick.** The claim is not cleared when the transaction
   aborts, so the next tick re-executes before failing again. The identical
   loop existed before C-18 — the raise aborted T1 and `dispatch_builder`
   ran again next tick with the same external calls — so this is not a
   regression, but it is a real retry loop and it is named here.
7. **`declare_busy` bounds are still stage 7.** Not applied.
8. **No rehearsal, no live launch.** This change is not evidence of launch
   readiness and does not claim any.

Stages 5, 6 and 7 remain unstarted. C-18 remains **OPEN** and still carries
*"REQUIRED BEFORE: unattended multi-cycle rehearsal, the 5-hour unattended
stress test, and T+00."*

---

## 29. C-02 accessibility requirement registry — BUILT AND WIRED (2026-10-01)

Branch: `wip/c02-requirement-registry`, cut from `wip/c05-1-persistence`
@ `4eeaa7c`. Worked in an isolated worktree.

**Document-provenance note.** This handover document is untracked in the
main checkout and therefore did not exist in this worktree. To append
section 29 without losing anyone else's work, the main checkout's copy was
copied in verbatim and this section appended. The branch therefore carries
a point-in-time snapshot of the shared document. If another agent appended
a section to the main checkout's copy after that snapshot was taken, the
two copies must be reconciled by hand before merge — this branch's copy is
not authoritative for sections 1-27.

### 29.1 The defect, verified before fixing

`apparatus/pr-evidence/validate.js:126` called
`applySeverityPolicy(f, undefined)`. The second parameter is
`knownRequirementIds`. `apparatus/severity/severity-floor.js:73-81` reads:

```js
const registry = knownRequirementIds ? new Set(knownRequirementIds) : null;
if (!registry || !registry.has(unmet_requirement)) { return invalid(...); }
```

So every FAILURE-classified accessibility finding returned INVALID
regardless of its citation, and the validator then set
`blockingFindingFound = true`. The old header comment in `validate.js`
(lines 24-28) stated this openly: the registry was "equally unbuilt".

That fail-closed behaviour is correct and is preserved. What was missing
was the registry that lets a legitimate finding be valid. Without it the
accessibility box could never go green, so the gate was not deterministic
policy — it was an unconditional block.

### 29.2 What was built

**`apparatus/accessibility/requirement-registry.js`** (new) — a frozen,
identifier-keyed list of the 17 accessibility requirements, each entry
carrying `id`, `group`, and `source_phrase`.

Derivation, mechanical and recorded in the module header:

| Source | Group | Entries |
|---|---|---|
| `product/ACCESSIBILITY.md` line 2, "Definition of done: ..." | `definition-of-done` | 12 |
| `product/ACCESSIBILITY.md` line 3, "Cognitive accessibility: ..." | `cognitive` | 5 |

Each comma-separated phrase in those two lines becomes exactly one entry.
`source_phrase` holds the phrase verbatim; that is the provenance link.
`product/ACCESSIBILITY.md` was **not** edited — it is an imported frozen
source and is covered by `experiment/imported-source.sha256`.

Identifiers are **semantic, not positional** — `ACC-DOD-VISIBLE_FOCUS`, not
`ACC-DOD-03`. A positional identifier would silently re-point at a
different requirement if the source list were ever reordered, making
already-filed evidence cite the wrong thing. (This is the same stability
concern recorded for finding IDs under "Unclassified: finding-ID
stability".) The full set:

```
ACC-DOD-SEMANTIC_HTML                    ACC-DOD-RESPONSIVE_LAYOUT
ACC-DOD-KEYBOARD_OPERATION               ACC-DOD-TOUCH_TARGETS
ACC-DOD-VISIBLE_FOCUS                    ACC-DOD-HEADING_STRUCTURE
ACC-DOD-MEANINGFUL_LABELS                ACC-DOD-CHART_SUMMARIES
ACC-DOD-SCREEN_READER_FORMS_ERRORS       ACC-COG-PREDICTABLE_NAVIGATION
ACC-DOD-COLOUR_INDEPENDENT_MEANING       ACC-COG-CONCISE_INSTRUCTIONS
ACC-DOD-SUFFICIENT_CONTRAST              ACC-COG-LOW_INFORMATION_DENSITY
ACC-DOD-REDUCED_MOTION                   ACC-COG-CLEAR_BACK_EXIT_ROUTES
                                         ACC-COG-NO_UNNECESSARY_URGENCY_PUNISHMENT
```

**The derivation is checked, not asserted.**
`apparatus/accessibility/requirement-registry.test.js` re-parses
`product/ACCESSIBILITY.md` independently of the registry's data and asserts
the two phrase sets are equal in both directions. A phrase added to the
frozen source, or an entry invented in the registry, turns the suite red
rather than silently widening or narrowing the gate. The registry is also
`Object.freeze`d at both levels so a caller cannot push an identifier in at
runtime; the test proves the freeze actually throws (the test file is
`'use strict'` for exactly this reason — in sloppy mode a write to a frozen
property fails silently and the test would have been vacuous).

### 29.3 What was wired

`apparatus/pr-evidence/validate.js:126` now passes `REQUIREMENT_IDS`
instead of `undefined`. The stale header paragraph that claimed the
registry was unwired was replaced — leaving it would have been a false
claim in the file that the claim is about.

A CLI entry point was added to `validate.js` (`require.main === module`) so
CI can actually execute the checker over a package file. It fails closed:
no arguments exits 2; an unreadable file or unparseable JSON exits 1; any
`policyValid: false` exits 1.

**No severity semantics changed.** `apparatus/severity/severity-floor.js`
has a zero-byte diff against `HEAD` and `control/severity.py` was not
touched, so `manifest.SEVERITY_POLICY_FILES`
(`protocol/SEVERITY-POLICY.md` + `control/severity.py`) hashes to the same
value. Both modules already took `knownRequirementIds` as a caller-supplied
parameter; building the registry outside them was sufficient, exactly as
their own headers predicted ("the identifier registry the PR evidence
validator maintains").

### 29.4 Decision — the schema asymmetry, and why `unmet_requirement` stays a free string

`protocol/PR-EVIDENCE-V2.schema.json:534` types `unmet_requirement` as a
bare `{"type": "string"}`, whereas `check_id` is enum-constrained at
:588, :652 and :708. The question was whether the registry should close
that asymmetry by constraining the field in the schema.

**Decision: no. Enforce at the validator; leave the schema's free string.**
Reasons, in order of weight:

1. **The two failures mean different things and have different remedies.**
   `SEVERITY-POLICY.md` rule 3 makes an unrecognised citation *INVALID
   evidence*, and rule 4 names its resolution path: escalation to an
   independent reviewer who supplies a clarified classification. A schema
   enum would instead emit `SCHEMA_INVALID` — "this package is malformed"
   — which has no such path and would read as a producer bug rather than
   a judgement that needs a human. Collapsing them would destroy
   information the policy depends on.
2. **It matches the layering the schema itself declares.** The
   `accessibilityFinding` description already says post-floor severity "is
   computed by the validator, so the ACCEPTED_NONBLOCKING/P0-P1 exclusion
   for these findings is a validator rule, not a schema rule". Registry
   membership belongs on the same side of that line.
3. **The schema is a frozen imported source** this work is forbidden to
   edit, so the option was unavailable in any case — but points 1 and 2
   are why it should stay that way even if it were editable.

Consequence to be aware of: a producer can still emit a syntactically
valid package containing a nonsense `unmet_requirement`. It will be
rejected — loudly, as `INVALID_ACCESSIBILITY_EVIDENCE` — but by the
validator, not the schema. Anything that validates the schema *without*
running `validate.js` therefore does not enforce the registry. That is the
reason for the CI work in 29.5.

### 29.5 Defect found in CI, and what was done about it

`.github/workflows/ci.yml` as it stood ran exactly two things
unconditionally: `python -m unittest discover -s tests -v` and
`python scripts/check_no_secrets.py`. Its five Node steps were every one of
them gated on `steps.app.outputs.present == 'true'`, which is true only if
a **root** `package.json` exists. It does not. `apparatus/package.json`
exists and declares `"test": "node --test"`, but nothing ran it.

So: **no Node test in this repository had ever run in CI**, and nothing
invoked `apparatus/pr-evidence/validate.js` — despite Protocol v2 line 236
stating "CI validates the schema". The 64 apparatus tests that existed
before this work were green only because somebody ran them by hand.

Four unconditional steps were added before the product-app block (which is
left untouched):

1. `Set up Node (apparatus)` — Node 24, npm cache keyed on
   `apparatus/package-lock.json`.
2. `Install apparatus dependencies` — `npm ci` in `apparatus/`.
3. `Install Playwright Chromium` — `apparatus/accessibility/run.test.js`
   drives a real Chromium and a real axe-core scan; without the browser
   those tests error rather than skip, so installing it is part of running
   the suite honestly.
4. `Apparatus tests` — `npm test` in `apparatus/`.
5. `Validate PR evidence packages` — runs the `validate.js` CLI over every
   `*.json` under `evidence/`.

The YAML was parsed with `yaml.safe_load` and the step list inspected (16
steps, correct `if:` distribution). The discovery shell was executed in
both states: empty (exit 0, explicit message) and populated with a
deliberately bad package (exit 1, all twelve policy errors printed).

### 29.6 Verification performed

Apparatus suite, full, real Chromium: **76 pass, 0 fail** (was 64 before
this work; +12). Control-plane suite:
`python3 -m unittest discover -s tests` — **914 tests, OK**, unaffected by
this branch. `python3 scripts/check_no_secrets.py` — "No secret-shaped
material found in 164 tracked files", exit 0. `git diff --check` — clean,
exit 0.

Mutation checks — each applied, run, observed red, reverted, and the
revert confirmed by SHA-256 against a pre-mutation baseline of all three
source files:

| # | Mutation | Tests that went red |
|---|---|---|
| 1 | Registry bypassed — `validate.js` back to `applySeverityPolicy(f, undefined)` | 1 (`a FAILURE citing a real registry identifier is no longer INVALID evidence`) |
| 2 | P1 floor removed — `Math.min(rawRank, floorRank)` → `rawRank` | 2, including the pre-existing RUN001-F1 regression test |
| 3 | Registry membership check bypassed — `if (!registry \|\| !registry.has(...))` → `if (false)` | 4, including two pre-existing severity-floor tests |
| 4 | Registry drift — an entry whose phrase is not in the frozen source | 3 derivation tests |

After mutation 4 was reverted, all three files hashed byte-identical to
baseline and `git diff apparatus/severity/severity-floor.js` was empty.

Mutation 1 is caught by only one test. That is a deliberately narrow
guard — the other registry tests call `applySeverityPolicy` directly with
`REQUIREMENT_IDS`, so they test the policy, not the wiring. The one test
that exercises the wiring is the one that must go red, and it does.

### 29.7 Limitations — what this does NOT establish

- **It does not resolve C-02 or C-04.** C-02's status is PROPOSED and
  becomes RESOLVED only when the contradiction audit checks the rule
  against its tested implementation. This is one input to that, not the
  audit.
- **It does not claim launch readiness.** T+00 remains NOT_STARTED. Per
  `AGENTS.md`, a partial pass cannot authorize launch.
- **A recognised citation is not a confirmed one.** Matching an identifier
  proves only that the reviewer named a real requirement from the frozen
  product spec. Whether that requirement is actually unmet at the reviewed
  SHA is still the independent Accessibility Reviewer's judgement, and
  nothing here verifies it.
- **The CI validator step validates nothing today.** No PR evidence package
  exists before T+00, so with an empty `evidence/` directory the step
  reports that it validated nothing and passes. It becomes a real gate only
  once packages are committed there. The `evidence/` path is a convention
  this work introduces; no other component reads or writes it yet.
- **The CI steps have not been observed running on GitHub.** The YAML was
  parsed and every command was executed locally, but no workflow run has
  happened — this branch has not been merged to a branch CI triggers on
  (`ci.yml` triggers on `main` only, for push and pull_request).
- **The Python port is unwired.** `control/severity.py`'s
  `apply_severity_policy` still defaults `known_requirement_ids=None`. It
  has no production caller today (only `tests/test_severity.py`), so
  nothing is currently broken by that — but if a Python caller is ever
  added it will fail closed exactly as `validate.js` did, and it will need
  a Python-side registry. Wiring one was out of scope here and would touch
  a frozen-manifest input if done carelessly.
- **The registry is derived from the frozen spec, not from WCAG.** It says
  what `product/ACCESSIBILITY.md` says and nothing more. It is not a
  conformance checklist.


## 31. C-05.3b accessibility dispatch — design proposal (PROPOSED — NOT APPROVED — NOT IMPLEMENTED)

**Status: PROPOSED. Not approved. Not implemented. No production code was
changed to write this.** Read-only inspection, 2026-10-01, against
`wip/c05-1-persistence` at `f2aa539`. Nothing here authorises a launch, a
paid provider call, a worker, a rehearsal or a code change. Ten of the
decisions below are marked **GOVERNANCE** and cannot be taken by an
implementer; five of those are **blocking** (§31.14).

### 31.1 The defect this closes, verified

`control/supervisor.py:1871-1873`, at the end of `ingest_security`:

```
        # SECURITY_PASS deliberately does NOT advance to REVIEW: accessibility
        # is a required evidence class and is not implemented until C-05.3b,
        # so the task holds in WAITING_EVIDENCE.
```

`SECURITY_FAIL` transitions to `FIX_REQUIRED` at `:1868-1870`. `SECURITY_PASS`
falls off the end of the function. `WAITING_EVIDENCE`'s outbound edges
(`control/state.py:114-115`) include `REVIEW`, but **nothing in the repository
ever takes that edge** — grep for `"REVIEW"` as a `transition` target from an
evidence path returns nothing. The asymmetry is pinned by
`tests/test_c05_3_evidence_routing.py:314-321`
(`test_security_pass_alone_never_reaches_review`), which ticks three times and
asserts the task is still `WAITING_EVIDENCE`.

The consequence is the one `experiment/REHEARSAL-PLAN.md:268` names: a passing
task stalls permanently and only failing tasks progress. An idle run that looks
healthy.

### 31.2 What is already governed, and what is not

**Approved (C-05a, RESOLVED 2026-09-30, `experiment/CONTRADICTION-AUDIT.md:42`)
— four timeout constants and nothing else:**

| Key | Value | Consumer today |
|---|---|---|
| `timeouts.security` | 1800 | C-05.3a (`_lease_expires("security")`) |
| `timeouts.accessibility_soft_seconds` | 120 | **none** |
| `timeouts.accessibility_hard_seconds` | 300 | **none** |
| `timeouts.product_server_readiness_seconds` | 120 | **none** |

The row is explicit that these are *"initial rehearsal governance values, not
empirical findings"*, that they are *"never auto-tuned"*, and that
*"C-05.3b consumes the accessibility and product-server values"*. It is equally
explicit about shape, and that sentence is the single most load-bearing piece
of governance this proposal has:

> "The `_seconds` keys are deliberately NOT role-shaped: accessibility runs
> in-process via `gate_evidence.run_accessibility` and the product server is a
> control-plane-owned process, so neither is a leased worker role and neither
> may be reachable as `timeouts[role]`."

Two facts follow directly, and most of this design is downstream of them:

1. The **automated** half is an in-process call, not a leased worker.
2. The **product server** is a control-plane-owned process, not a worker.

**Not approved — no design exists anywhere.** Every other C-05.3b reference in
the repository is a status marker saying it has not begun:
`experiment/LAUNCH-CHECKLIST.md:53` ("Not started"),
`C05-3a-SESSION-HANDOVER.md:3062`, `:3118`, `:3129`,
`experiment/REHEARSAL-PLAN.md:268`, and the supervisor comment above. There is
no `control/accessibility_contract.py`, no parser, no
`ACCESSIBILITY_PASS`/`ACCESSIBILITY_FAIL` token anywhere under `control/`, no
caller of `gate_evidence.run_accessibility`, and no caller of
`control/prompts.py::accessibility` (`:99-110`).

### 31.3 Why C-05.3a cannot simply be copied

Ten structural differences, each found by inspection. The form of this section
follows §14.0.

1. **Two halves, not one review.** Security is a single provider judgement about
   a diff. Accessibility is an in-process Node browser run *and* a separate
   leased provider review, and `agents/ACCESSIBILITY.md:8` forbids collapsing
   them: *"Automated checks do not replace this qualitative review; neither
   replaces the other."* The governed timeout keys already encode the split —
   `security` is role-shaped, the accessibility keys deliberately are not.

2. **Six of the nine claim keys are meaningless for the automated half.**
   `worker`, `lease_expires_at` and the whole `security_worker_name` /
   `SECURITY_WORKER_RE` SHA-binding defence (`control/routing.py:619-652`) exist
   for one reason, stated at `:621-626`: every worker artefact in
   `WORKER_LOG_DIR` is keyed on the worker name, and nothing clears a stale one.
   The automated half writes nothing into `WORKER_LOG_DIR`. Its isolation is
   `allocate_attempt`'s `O_EXCL` mkdir (`control/gate_evidence.py:643-663`).
   Copying the name defence would add a field with nothing to defend and a
   validator rule that can never fire.

3. **The attempt ordinal is chosen in a different place.** C-05.3a's whole
   safety argument is that the ordinal is chosen *under the exclusive state
   lock* and nowhere else (`plan_security` docstring,
   `control/supervisor.py:1045-1049`). `allocate_attempt` chooses it by racing
   `mkdir` on the filesystem, outside any lock. Those two cannot both be true
   for one attempt, and §31.5 has to pick.

4. **Accessibility needs a product server that does not exist.** Security needs
   nothing beyond the provider worker. `apparatus/accessibility/run.js` takes a
   URL and never starts anything — `withPage` goes straight to
   `page.goto(url)`. `product/` contains six specification markdown files and
   **no `package.json`, no source, no start command**. Nothing anywhere in
   `control/`, `apparatus/`, `scripts/` or `bin/` starts a product server.

5. **C-09 has no concept of a control-plane-owned port.** The C-09 audit row
   (`experiment/CONTRADICTION-AUDIT.md:17`) scopes the allocator as
   *"builder + fixer only; reviewers are read-only sandboxes"*.
   `workers.select_port_candidates` (`control/workers.py:275-304`) excludes
   exactly two sets: ports on committed `doc["workers"]` records, and ports
   reserved by a job file (`_reserved_job_file_ports`, `:233-272`). A
   control-plane-owned product server is in neither. Security never had to
   touch the port mechanism at all. This cuts **both ways** — see §31.6.

6. **No analogue of SURFACES_INCOMPLETE.** Security grades twelve named
   `SECURITY_SURFACES` and a missing one fails closed
   (`routing.security_is_consistent:305-308`). Accessibility has ten
   `accessibilityCheck` enum values, nine produced by a machine and the tenth
   (`COGNITIVE_SENSORY_REVIEW`) produced by a provider. Completeness therefore
   spans **two producers**, and a single `CHECKS_INCOMPLETE` reason has to be
   satisfied jointly.

7. **Findings do not carry a severity the gate can read.** A security finding
   carries `severity` directly. An accessibility finding carries `jev_severity`
   *plus* `classification` *plus* `unmet_requirement`, and must be run through
   `control/severity.py::apply_severity_policy` against a requirement registry
   that does not exist yet. The automated half emits **no severity of any
   kind** — `run.js` emits `{check_id, result, sha, artifact_reference}` and
   nothing else.

8. **The blocking rule is stricter and differently shaped.** Security: P0/P1
   block. Accessibility: a deterministic **P1 floor** is applied to any
   `FAILURE` classification (`severity.py:104-107`), *and* invalid evidence
   blocks independently of severity (`severity.py:49`, `agents/ACCESSIBILITY.md:15`).
   An unclassifiable finding is a missing-evidence state, not a P2.

9. **The qualitative role has no governed timeout.** `config/experiment.json`'s
   `timeouts` object has `security: 1800` and **no `accessibility` key**.
   `_lease_expires` (`control/supervisor.py:1879-1885`) reads
   `self.cfg.extra["timeouts"][role]`, so `_lease_expires("accessibility")`
   raises `KeyError`. C-05a made the omission deliberate for the in-process
   half — but the *qualitative* half is a leased worker, and it has no number.
   C-05.3a had its number before it started. This is blocking governance
   (§31.12, G1).

10. **The run blocks the tick for longer than the staleness threshold.**
    `run_accessibility` calls `workers.run_bounded` synchronously with
    `hard_timeout_seconds = 300`. `supervisor.heartbeat_stale_seconds` is
    **120**. A supervisor running accessibility looks dead to the Watchdog
    unless `declare_busy` wraps it. The security worker is spawned and polled,
    so C-05.3a never met this. `declare_busy` is **mandatory here**, not the
    optional stage-7 polish C-18 describes.

### 31.4 Shape of the solution

Two evidence producers, two claims, one composite gate.

```
                    ┌─ accessibility_auto    (in-process Node + product server)
WAITING_EVIDENCE ───┼─ accessibility_review  (leased provider worker)
                    └─ security_evidence     (landed, C-05.3a)
                              │
                              └─► composite gate at one SHA ──► REVIEW
```

Everything new is written in the C-18 plan/execute/confirm shape from the
start. No new external effect is added inside T1.

### 31.5 The automated half — `accessibility_auto`

**Claim schema — seven keys, deliberately not nine.** On the PR record, beside
`security_evidence`:

```
sha            full 40-hex head this attempt is about
attempt_id     "attempt-NNNN", gate_evidence's existing namespace
claim_state    PLANNED | RUNNING | COMPLETE
claimed_at     canonical tz-aware second-precision
port           the product server's governed C-09 port for this attempt
verdict        ACCESSIBILITY_AUTO_PASS | ACCESSIBILITY_AUTO_FAIL | None
reason         "" or one finite failure reason
```

`port` is the durable ownership record §31.6 needs — the single field that both
the allocator's exclusion set and the orphan-listener check read. No `worker`
(there is none), no `lease_expires_at` (nothing is leased), no `ordinal`
separate from `attempt_id`. **Reason for refusing the C-05.3a shape:**
a nullable field that a validator must special-case is a field that will one day
be read as meaningful. §31.3 item 2 is the argument.

`claim_state` has **three** values, not C-05.3a's three-with-different-meanings.
`SPAWNED` does not apply — nothing is spawned — so the middle state is `RUNNING`,
and its durable witness is the pair of lifecycle markers C-05.2 already writes
(`attempt-running.marker` / `attempt-terminal.marker`,
`gate_evidence.py:72-76`). Recovery reads markers, not `/proc`:

| On disk | Meaning | Action |
|---|---|---|
| no attempt dir for `(task, sha, attempt_id)` | the plan never materialised | re-plan |
| RUNNING, no TERMINAL, no outcome | crashed mid-run, **or** still running | see D1 below |
| TERMINAL + `attempt-outcome.json` | finished | ingest |
| TERMINAL, no outcome | impossible by construction (`run_accessibility:1470-1474` publishes the outcome *before* the marker) — treat as corrupt, fail closed |

**D1 — the one genuinely hard recovery case.** C-05.3a could distinguish "still
running" from "crashed" by scanning `/proc` for the named worker. There is no
worker here: the run happens inside the supervisor's own process, so if the
supervisor is alive and in Phase C the run is alive, and if the supervisor
restarted the run is dead. The supervisor's own singleton lock
(`_acquire_singleton`) already proves at most one supervisor exists. **Proposed
rule:** a `RUNNING` claim found by a *newly started* supervisor is dead by
construction — a supervisor that holds the singleton lock and does not have the
run on its own stack cannot have it running anywhere. Mark the attempt abandoned
(`reason = SPAWN_OR_RUN_INCOMPLETE`), advance the ordinal, re-plan. This is
*weaker* evidence than C-05.3a's `/proc` proof and must be stated as such — but
unlike a paid provider review, a wasted automated re-run costs CPU and nothing
else, so the fail-closed direction here is "re-run", not "hold".

**Ordinal allocation — the C-05.3a invariant versus `allocate_attempt`.** Two
options, and this one must be decided, not left:

- **(a) Keep `allocate_attempt` as-is**: Phase C picks the ordinal by `mkdir`,
  Phase D writes the resulting `attempt_id` back into the claim. The window
  between plan and confirm is exactly C-05.3a's `spawn_uncommitted` condition
  (`routing.security_spawn_uncommitted`), already modelled.
- **(b) Choose the ordinal in T1** and add
  `gate_evidence.allocate_attempt_at(task_id, sha, ordinal)` that still uses
  `O_EXCL` mkdir but fails closed on collision instead of advancing.

**Recommended: (b).** It preserves the invariant C-05.3a's whole safety argument
rests on — the ordinal is chosen under the exclusive lock and nowhere else — and
it makes the claim complete at plan time, so there is no window in which a
durable claim exists with a null `attempt_id`. The cost is one new
`gate_evidence` function and the loss of `allocate_attempt`'s
advance-on-collision convenience, which the automated path does not need
(a collision there means the claim was already materialised, which is
information, not an obstacle). Reversible either way; this is a design call an
implementer may take, not governance.

**Lifecycle, per tick:**

```
outside lock   A   observe       read markers + attempt-outcome.json for the claimed attempt
outside lock   E1  (none)        the outcome is already published by run_accessibility itself
─── T1 ────────────────────────────────────────────────────────────────────────
inside  lock   E2  ingest        durable outcome → verdict, reason
inside  lock   B   plan          gates → choose ordinal → write PLANNED claim
─── T1 commits ────────────────────────────────────────────────────────────────
declare_busy   C   execute       start product server → wait readiness → run_accessibility → stop server
own txn        D   confirm       claim_state → COMPLETE (or RUNNING on a partial)
```

Phase C is wrapped in `self.run_declared("accessibility", bound, ...)` where
`bound = product_server_readiness_seconds + accessibility_hard_seconds +
BUSY_MARGIN_SECONDS` = 120 + 300 + margin. **This is required, not optional:**
see §31.3 item 10.

**Honest cost, stated rather than hidden.** `tick()` is sequential and
`supervisor.poll_seconds` is 30. One accessibility attempt can therefore delay
the next tick by up to ~7 minutes — roughly fourteen poll intervals — during
which no merge, no dispatch and no notification drain happens. Making the Node
run non-blocking would contradict C-05a's "runs in-process" governance, so this
proposal accepts the stall and declares it. **GOVERNANCE (G7, non-blocking):**
whether that stall is acceptable at `max_builders = 3`, or whether C-05a's
in-process ruling should be revisited.

### 31.6 The product server

This is the least answerable question in the proposal and most of it escalates.

**What is settled by governance.** C-05a: the product server is "a
control-plane-owned process", with `product_server_readiness_seconds = 120`.
So the control plane starts it and the control plane stops it — not a worker,
not the Builder, not `run.js`.

**What is settled by inspection.** `run.js` starts nothing and binds nothing;
it takes a URL that must already resolve (`run.js:122`,
`page.goto(url, {waitUntil: 'load'})`). Its own comments explain that even a
Playwright `BrowserServer` was avoided *"because launching a server would bind a
websocket port that C-09's listener reconciliation would then have to account
for"*. So the product server is squarely the control plane's problem.

**Proposed shape, for the parts that are derivable:**

- **Lifetime: one attempt.** Started at the top of Phase C, stopped in the same
  Phase C after `run_accessibility` returns *or raises*. Not a long-lived
  process, not shared between attempts, not surviving a tick. Rationale: a
  server outliving its attempt is a process with no durable owner, which is
  precisely the orphan class C-09 exists to prevent, and nothing on the PR
  record could name it.
- **Working tree: the attempt's own worktree at the exact head SHA.** Same rule
  as C-05.3a's security worktree, same ownership mechanism
  (`retained_worktrees` with a new `why` value `ACCESSIBILITY_ATTEMPT`), same
  release-after-outcome-is-durable discipline (Phase F). Evidence bound to a
  different commit is not evidence (`gate_evidence._adjudicate:1410-1412`
  already enforces this on the result file).
- **Readiness: poll the chosen URL, bounded by
  `product_server_readiness_seconds` (120).** Readiness is proven by the server
  answering, never by the process existing — a process that started and is
  failing to serve must not read as ready.
- **Readiness timeout is an apparatus failure, not a product defect.** Proposed
  reason token `PRODUCT_SERVER_UNREADY`, recorded as a `FAILED` attempt outcome
  with an empty check list. It must **not** transition to `FIX_REQUIRED`:
  nothing was observed about the product, and `gate_evidence`'s stated rule 2
  (`:22-25`) is that *"a run that did not happen never reads as a run that
  passed"* — the converse holds too, and a server that would not start is not
  evidence that the page is inaccessible. The task holds, and the hold is
  bounded by G4.
- **Port: taken from the governed C-09 range `[3200, 3299]`** via the already-
  landed C-18 stage 3 split — `select_port_candidates` under the lock (T1),
  `probe_port` outside it (Phase C).

**HAZARD — and it is two defects, not one, because the port mechanism is
blind in both directions.**

*Forward:* `select_port_candidates` excludes only `doc["workers"]` ports and
job-file ports. A control-plane-owned server appears in neither, so from the
moment it binds until it exits, `dispatch_builder` can select and hand a Builder
the same port.

*Reverse — and this is the worse half.* `control/reconcile.py:427-453` scans
`/proc/net/tcp` and `/proc/net/tcp6` for listeners inside `[3200, 3299]` and
emits `FOREIGN_OR_ORPHAN_LISTENER` for any in-range listener with no owning
worker record. A product server owned by the control plane has no worker record
by construction, so **every tick it runs, the Watchdog reports it as an orphan.**
That is exactly C-05.3a's D5 (§3, *"Running security attempts are reported as
orphan resources"*) reappearing in a new resource class — and D5's repair, R4,
taught `detect_orphans` about claim-based ownership rather than inventing a
second ownership store. The same repair shape applies here.

The repository's existing durable port-claim mechanism is the job file, written
before spawn — but `_reserved_job_file_ports` resolves liveness through
worker-entry and recorded-agent identity (`:249-271`), which a non-worker
process does not have, so a synthetic job file would be *misread*, not merely
unhelpful.

**Proposed minimal fix, following R4 rather than inventing anything:** the
accessibility claim carries a `port` field — already durable, already in `doc`,
already scoped to the attempt — and that one field is read by both
`select_port_candidates` (as a third exclusion source) and the orphan-listener
check (as proof of ownership). One store, two readers, matching R4's reasoning
exactly. **GOVERNANCE (G2, blocking):** this widens a C-09 mechanism whose row
is still `OPEN / PARTIAL`, and C-09 ownership rules are governed, not an
implementer's call.

**ESCALATED — not answerable from the repository at all (G3, blocking):**
*what command starts the product server.* `product/` is specifications only.
There is no `package.json`, no framework choice committed, no dev-server
command, no build, no dependency install, and no governed timeout for an
install step (which is unbounded and would sit inside the 120 s readiness
budget). The Builder creates the product during the run, so the command cannot
be known pre-T+00 — but it also cannot be *discovered* safely, because
"run whatever `package.json` says" is arbitrary code execution chosen by a
worker. A governance answer is needed on: the committed command (or the
committed contract a Builder must satisfy), whether dependency install is in or
out of the readiness budget, and what happens on the very first PR when no
product exists yet.

### 31.7 The qualitative half — `accessibility_review`

Structurally the closest thing to C-05.3a in this proposal, and the only place
copying is appropriate.

**Claim schema: the nine keys, unchanged in shape** — `sha`, `ordinal`,
`attempt_id`, `worker`, `claim_state`, `claimed_at`, `lease_expires_at`,
`verdict`, `reason` — stored at `record["accessibility_review"]`, with
`accessibility_worker_name(task_id, sha, ordinal)` deriving
`"{task}-a11y-{sha}-{ordinal:04d}"` and the full SHA, for exactly the reason
`routing.py:619-642` gives. Attempt namespace
`a11y-attempt-NNNN`, which like `security-attempt-NNNN` cannot match the
`attempt-` prefix `scan_sidecars` walks, so browser observation never sees it.

**Name length check, done rather than assumed.** `SECURITY_NAME_OVERHEAD` is
`len("-security-") + 40 + len("-9999")` = 55, leaving 9 characters for the task
id inside the 64-character `SECURITY_WORKER_RE` bound — and §routing's comment
notes this run's eight-character task ids fit at 63. `"-a11y-"` is four
characters shorter than `"-security-"`, so the accessibility form is 59 for the
same task ids. Fits, with more headroom, not less.

**Recovery, dispatch gates, worktree ownership, publication retry and release
are the C-05.3a tables with `security` renamed.** `security_recovery_state`'s
eight-state table, the `PROVEN_NOT_RUNNING` three-condition rule, the
`PUBLICATION_PENDING` state, the `STALE_HEAD_ATTEMPT_LIVE` refusal, the
`_security_dispatch_block` gate set (frozen / provider / stopping / slots) and
Phase F's release-only-after-publication rule all transfer unchanged. That is
the *only* part of C-05.3a this proposal copies, and it copies it wholesale and
deliberately.

**BLOCKER — no governed timeout.** `_lease_expires("accessibility")` raises
`KeyError` today (§31.3 item 9). An implementer cannot pick a number: C-05a is
explicit that these values are governance decisions and *"changing any one of
them requires another explicit governance decision rather than a code change"*.
Adding one is the same kind of act. **GOVERNANCE (G1, blocking).** Note the
C-05a row's own reasoning predicts the required shape: the qualitative review
*is* a leased worker, so its key must be bare and role-shaped
(`timeouts.accessibility`), not `_seconds`-suffixed — which also means the key
name `accessibility` would then be reachable as `timeouts[role]`, exactly what
C-05a wanted to prevent for the *other* two values. The two halves must not
share a key name.

### 31.8 The verdict contract — `control/accessibility_contract.py`

New module, in the exact shape of `control/security_contract.py`: a dependency-
light vocabulary leaf, importing nothing from `control/` except — see below —
one alias set, reading no file, spawning nothing. One string literal per token,
in this repository, in this file.

```python
# ---------------------------------------------------------------- verdicts
ACCESSIBILITY_PASS         = "ACCESSIBILITY_PASS"
ACCESSIBILITY_FAIL         = "ACCESSIBILITY_FAIL"
ACCESSIBILITY_UNPARSEABLE  = "ACCESSIBILITY_UNPARSEABLE"
ACCESSIBILITY_VERDICTS     = frozenset({ACCESSIBILITY_PASS, ACCESSIBILITY_FAIL})

ACCESSIBILITY_AUTO_PASS    = "ACCESSIBILITY_AUTO_PASS"
ACCESSIBILITY_AUTO_FAIL    = "ACCESSIBILITY_AUTO_FAIL"

# ---------------------------------------------------- adjudication reasons
#  (qualitative review — produced while deciding whether a parsed review
#   is a coherent contract)
OUTPUT_UNPARSEABLE          = "OUTPUT_UNPARSEABLE"      # alias, see below
VERDICT_UNRECOGNISED        = "VERDICT_UNRECOGNISED"    # alias, see below
FINDING_FIELDS_INVALID      = "FINDING_FIELDS_INVALID"  # alias, see below
PASS_WITH_BLOCKING_FINDINGS = "..."                     # alias, see below
FAIL_WITHOUT_BLOCKING_FINDINGS = "..."                  # alias, see below
CLASSIFICATION_INVALID      = "CLASSIFICATION_INVALID"  # severity.py said INVALID
CHECKS_INCOMPLETE           = "CHECKS_INCOMPLETE"       # fewer than ten check_ids

# -------------------------------------------- apparatus reasons, automated
RESULT_MISSING    = "RESULT_MISSING"
RESULT_UNREADABLE = "RESULT_UNREADABLE"
RESULT_EMPTY      = "RESULT_EMPTY"
SHA_MISMATCH      = "SHA_MISMATCH"
PRODUCT_SERVER_UNREADY = "PRODUCT_SERVER_UNREADY"

# ------------------------------------------- apparatus reasons, shared
TIMED_OUT, EXIT_NONZERO, OUTPUT_MISSING, PROVIDER_FAILURE,
SPAWN_OR_RUN_INCOMPLETE   # aliased from security_contract — see below
```

**Three deliberate differences from the security contract, each with a reason:**

- **No `SURFACES_INCOMPLETE`.** The accessibility prompt has no surfaces block
  (`prompts/accessibility.md:68-93`). Completeness is about the ten
  `accessibilityCheck` values, spanning two producers, so the reason is
  `CHECKS_INCOMPLETE` and it is evaluated against the **union** of the nine
  automated results and the qualitative `COGNITIVE_SENSORY_REVIEW`. Reusing the
  security name would make a reader believe a surfaces grid exists.

- **A new `CLASSIFICATION_INVALID`.** Security has no equivalent because a
  security finding's severity is self-describing. An accessibility finding goes
  through `severity.apply_severity_policy`, which can return
  `{"valid": False, ...}` for six distinct contradictions — all of which must
  fail the review closed rather than be read as a lesser finding, per
  `agents/ACCESSIBILITY.md:15`.

- **The five process reasons are aliased, not restated.** `security_contract`'s
  module docstring states the rule: *"There is exactly one string literal per
  token in this repository."* `TIMED_OUT`, `EXIT_NONZERO`, `OUTPUT_MISSING`,
  `PROVIDER_FAILURE` and `SPAWN_OR_RUN_INCOMPLETE` already have their one
  literal, and `control/gate_evidence.py:172-176` already aliases exactly these
  five from `security_contract` for exactly this reason. This proposal follows
  that existing pattern rather than inventing a third neutral leaf. It reads
  slightly oddly — accessibility importing from a module named `security` — and
  the alternative (extracting a shared `attempt_contract.py` leaf and
  re-exporting from both) is cleaner but refactors landed, tested code for a
  naming aesthetic. **Recommended: alias now, extract only if a third evidence
  class ever arrives.**

**A real defect this surfaces.** `gate_evidence._adjudicate` (`:1391-1418`)
produces `"TIMED_OUT"`, `"EXIT_NONZERO"`, `"RESULT_MISSING"`,
`"RESULT_UNREADABLE"`, `"RESULT_EMPTY"` and `"SHA_MISMATCH"` as **bare string
literals inline**, with no governed union and no module owning them. Two of
those six are second literals for tokens `security_contract` already owns. This
is the exact drift `security_contract`'s docstring exists to prevent, and
C-05.3b is the natural place to close it. Not a behaviour change; a
single-definition change.

**Parser: `routing.parse_accessibility(text)` + `accessibility_is_consistent`,
modelled on `parse_security` / `security_is_consistent`,** keeping every stated
rule: reverse-scan fenced blocks (the prompt asks for the block last); never
default a missing severity; never drop a malformed finding to make a review look
coherent; return an unrecognised verdict token rather than falling back to an
earlier block. Fixed precedence for one deterministic reason per review:
unparseable → unrecognised verdict → structure → per-finding severity policy →
verdict-vs-findings.

### 31.9 Finding IDs — `TASK###-R#-A11Y-###`

Three producers refuse to allocate, and they are right to:

- `apparatus/accessibility/run.js:13-19` — *"does NOT allocate
  TASK###-R#-A11Y-### finding IDs — that belongs to whatever assembles a PR
  evidence package (not yet built; no product PR exists pre-T+00), and
  inventing one here would be exactly the kind of fabricated capability
  Protocol v2's anti-cheating rule forbids."*
- `prompts/accessibility.md:77` asks the reviewer for `"id": "A1"` — a local
  ordinal that will not validate against the schema pattern
  (`PR-EVIDENCE-V2.schema.json`, `accessibilityFinding.id`).
- `protocol/PR-EVIDENCE-V2.schema.json` requires the canonical form, and
  requires a `finding_id` on any check whose `result` is `FAIL`.

**Proposed resolution: C-05.3b does NOT mint finding IDs.** It stores raw check
results and raw qualitative findings, bound to `(task_id, sha, attempt_id)`, in
the durable attempt outcome. The assembler `run.js` names — the PR evidence
package — is **C-04**, which is not built, and minting here would create a
second allocator that C-04 would then have to either adopt or override.

This is affordable because of a fact worth stating plainly: **the composite
REVIEW gate (§31.10) needs verdicts, not finding IDs.** C-05.3b can be complete
and correct without ever producing a `TASK###-R#-A11Y-###` string.

**The consequence must be stated rather than glossed:** until C-04 lands, the
run produces durable, SHA-bound, parseable accessibility evidence that is **not
a schema-valid Protocol v2 PR evidence package**. C-05.3b unblocks
`WAITING_EVIDENCE`; it does not deliver the evidence artefact Protocol v2
specifies. Anyone citing C-05.3b as "the accessibility gate is implemented"
would be overclaiming.

**For when C-04 does mint them — the ownership answer, and the hazard §14.0
item 3 already named.**

- **R# is an accessibility *evidence-cycle* counter, not the reviewer's
  `review_cycles`.** A new per-PR-record `accessibility_cycles`, incremented
  **exactly once per accepted evidence set, in the same transaction as the
  ingest** (Phase E2) — never at plan time.
- **The ### ordinal** is allocated in that same ingest transaction, `001`
  upward, over a deterministic ordering: the nine automated FAILs first in
  `run.js`'s fixed emission order (`run.js:454-462`), then the qualitative
  findings in emitted order. Deterministic so that re-ingesting the same durable
  outcome yields byte-identical IDs — ingestion is already required to be
  idempotent.
- **Why commit-time and not plan-time.** §14.0 item 3 states the dilemma:
  increment at plan and a failed execution burns a number, leaving a hole;
  increment at commit and two plans might choose one name. Here the second horn
  does not exist, because the two counters answer different questions and live
  in different namespaces. The *attempt* ordinal is chosen at plan time under
  the lock (that is what makes two concurrent plans impossible); the *evidence
  cycle* R# is chosen at ingest time, so an attempt that timed out, crashed, or
  produced unparseable output never consumes one. Attempts are counted;
  accepted evidence sets are numbered. Keeping them separate is what dissolves
  the hazard rather than trading one horn for the other.
- **GOVERNANCE (G5, non-blocking):** whether R# is per-evidence-class or shared
  with `SEC-###` and `CODE-###` across one PR. `protocol/RUN-002-PROTOCOL-v2.0.md:238-241`
  gives the format and the lifecycle but does not say. If shared, the counter
  cannot live in C-05.3b at all.

### 31.10 Mapping check results through the severity registry

**What exists.** `control/severity.py::apply_severity_policy(finding,
known_requirement_ids)` implements the P1 floor and the evidence-shape rules,
and fails closed to `INVALID` — `merge_blocked: True` — when
`known_requirement_ids` is absent or does not contain the cited
`unmet_requirement` (`severity.py:95-102`).

**What is missing, and is being built concurrently.** The registry itself.
C-02/C-04 own it. **This proposal does not build it.**

**Assumptions this design makes about C-02, stated so they can be checked:**

1. The registry is reachable from `control/` as an in-memory collection of
   identifier strings, obtained without a filesystem read at call time — so a
   caller inside T1 performs no external work. If C-02 lands as a file read,
   the read must move to Phase A observation and this assumption breaks.
2. Identifiers are the kebab-case form already used by the prompt and the
   severity tests — `visible-focus-indicator`, `keyboard-focus-trap`.
3. The registry is a *set membership* test, with no severity attached to the
   identifier. `severity.py` applies the floor itself.

**The mapping gap, which is this section's real finding.** `run.js` emits **no
severity and no requirement citation** — nine `{check_id, result, sha,
artifact_reference}` objects and nothing more. So an automated `FAIL` cannot be
handed to `apply_severity_policy` as it stands. Something must assert *which
requirement a failing check proves unmet*. The proposed mechanism is a static
in-code map, `CHECK_REQUIREMENT`, from each of the nine `check_id` values to one
registry identifier, turning a FAIL into
`{classification: "FAILURE", unmet_requirement: <mapped>, jev_severity: "P1"}`
and letting the floor apply trivially.

**GOVERNANCE (G6, blocking for the automated FAIL path only):** the contents of
that map. It is a policy statement about what each automated check proves, it
does not exist anywhere in the repository, and an implementer inventing it would
be writing accessibility policy. Nine entries. `AXE_SCAN` is the awkward one: a
single axe violation id is far more specific than a check id, so either the map
is check-level and coarse, or the axe violation ids become registry identifiers
in their own right.

**Fail-closed behaviour until then, which is correct and should be stated as
designed rather than apologised for:** with no registry, every automated FAIL
resolves to `INVALID` with `merge_blocked: True`. The gate holds. A task with a
failing accessibility check never reaches REVIEW, and never silently passes. The
automated FAIL path is inert-but-safe until C-02 lands — the right direction to
be wrong in.

### 31.11 The composite REVIEW gate

**The predicate.** In T1, for a task in `WAITING_EVIDENCE` with PR record `R`
and the head `H` observed *this tick* (the same `H` that `route_evidence`
already receives and that `plan_security` already checks against
`observation.head_sha`):

```
gate_fires(R, H)  ⇔  all three of:

   claim_ok(R["security_evidence"],     H, SECURITY_PASS)
   claim_ok(R["accessibility_auto"],    H, ACCESSIBILITY_AUTO_PASS)
   claim_ok(R["accessibility_review"],  H, ACCESSIBILITY_PASS)

where claim_ok(c, H, want) ⇔
       isinstance(c, dict)
   and <class>_claim_is_valid(c)[0]
   and c["claim_state"] == "COMPLETE"
   and c["sha"] == H
   and c["verdict"] == want
   and c["reason"] == ""
```

Fires `transition(..., "REVIEW", ...)`. State-only; the transition is already
legal (`state.py:114`).

**How a mixed-SHA evidence set is rejected — and why this shape and not
another.** Each claim is compared **to `H`, independently**. The three claims
are never compared to each other. That is strictly stronger than pairwise
equality, which would accept three claims that agree perfectly with one another
and all describe a commit that has since been superseded. It is the same rule
`plan_security` enforces at `supervisor.py:1059-1066`, and the same rule
`_adjudicate` enforces on the result file at `gate_evidence.py:1410-1412`:
*"Evidence bound to a different commit is not evidence about this one."*

**The consequence worth naming: a head move needs no reset path.** The predicate
is pure over `(claims, H)`. When the head moves, no claim's `sha` equals the new
`H`, so the gate simply does not fire — nothing is cleared, nothing is
invalidated, and there is no half-cleared record a crash could leave behind.
This is why the design does not add an `approval_current`-style invalidation
flag: it would be a second source of truth for something the SHA comparison
already decides. `state.py:106-113` already states the governing rule —
*"a prior SHA's pass never carries forward."*

**Precedence, in fixed order, so one record yields one outcome:**

| Condition | Result |
|---|---|
| any claim `COMPLETE` at `H` with a `*_FAIL` verdict | `FIX_REQUIRED` (as `ingest_security` already does) |
| all three `COMPLETE` at `H` and all `*_PASS` | `REVIEW` |
| anything else — absent, stale-SHA, `PLANNED`, `RUNNING`, or a non-empty `reason` | **hold** in `WAITING_EVIDENCE` |

FAIL outranks PASS: a head with a known blocking finding should not consume a
reviewer cycle, which is the ground `state.py:106-113` gives for the
`WAITING_EVIDENCE → FIX_REQUIRED` edge existing at all.

**Placement: inside `route_evidence`, as the last step of the per-task loop** —
after this tick's ingests, so an evidence set completed this tick fires the same
tick rather than one tick later. Not a separate method called afterwards: it
must see the ingests, and a second loop over the same tasks would read the same
document twice for no benefit.

**GOVERNANCE (G4, blocking).** The third row holds **forever**. That is today's
defect rewritten at a higher resolution: a task whose product server will never
start, or whose evidence is permanently unparseable, waits indefinitely with no
escalation. §8 item 2 raised exactly this question for C-05.3a's
publication-blocked case and it is still open. C-05.3b needs the answer before
it ships, because it multiplies the number of ways to be permanently stuck by
three. The governed lease is the obvious candidate and, as §8 already notes,
currently means something else.

### 31.12 Concurrency

`config/experiment.json`'s `concurrency` block holds `max_builders: 3`,
`max_fixers: 1`, `max_reviewers: 1`, `max_security: 1`, `max_observers: 1`,
`max_repair_cycles: 3`. There is **no** `max_accessibility`, and
`control/config.py:81-86` has no field for one.

**GOVERNANCE (G7, blocking).** Two numbers are needed, not one, because the two
halves consume different resources:

- `max_accessibility_auto` — bounded by host resources, not money: each attempt
  is a Chromium plus a product server plus a worktree plus a port from a
  100-port range. It is also, per §31.5, serialised inside `tick()` regardless,
  so a value above 1 buys nothing until the in-process ruling changes.
  **Suggested starting point: 1** — but a suggestion is not a governance
  decision and this proposal does not take it.
- `max_accessibility_review` — a paid provider review per PR, exactly the budget
  and rate-limit judgement §8 item 1 named for `max_security` (which was
  subsequently governed to 1).

### 31.13 Placement against C-18

**C-18's state:** stages 1–3 landed (§§18–21); stages 4–7 open. Stage 3 — the
`allocate_port` split into `select_port_candidates` (pure, lock-safe) and
`probe_port` (external) — is **landed**, and the product server needs exactly
that split. Stages 4 and 6 migrate the *existing* builder and fixer callers;
C-05.3b has no existing callers to migrate.

**Proposed ordering: C-05.3b may land before C-18 stages 4–7, and should.**

The reasoning is that C-05.3b adds **no new external effect inside T1**. Every
new path is written plan/execute/confirm from the start:

| Work | Phase | Lock held? |
|---|---|---|
| read markers, outcome, provenance | A | no |
| `select_port_candidates` | B (T1) | yes — pure, no bind (stage 3) |
| choose ordinal, write claim, ingest, fire the gate | B/E2 (T1) | yes — state only |
| `probe_port`, start server, poll readiness | C | no, under `declare_busy` |
| `allocate_attempt_at`, `run_accessibility` | C | no, under `declare_busy` |
| stop server, release worktree | C/F | no |
| `claim_state` → COMPLETE | D | own transaction |

Waiting for stages 4–7 would mean an idle run continues to be the only
observable behaviour (§31.1) while work that does not depend on them is blocked.
Landing C-05.3b first adds one more consumer of the stage-3 split, which is
evidence that the split's contract is right before stages 4 and 6 bet on it.

**§14.5 records this ordering as explicitly undecided** — *"Whether C-18 lands
before or after C-05.3b"* — and it remains a governance call.
**GOVERNANCE (G8, non-blocking):** the above is a recommendation with its
reasoning shown, not a decision taken.

**Two concrete collisions to coordinate, neither a blocker.**

1. C-18 stage 4 (`dispatch_builder`) will touch `select_port_candidates`' call
   site at the same time as G2 proposes widening its exclusion set. Same
   function, two changes, two agents. Sequence them.

2. **A landed test will go red, deliberately, and must be updated rather than
   worked around.** `tests/test_c18_stage3_port_split.py:413-425`
   (`StageThreeDidNotMigrateTheDispatchSites`) asserts against the *source text*
   of `control/supervisor.py`:

   ```python
   self.assertEqual(source.count("workers.allocate_port(doc)"), 2)
   self.assertNotIn("workers.select_port_candidates", source)
   self.assertNotIn("workers.probe_port", source)
   ```

   Its docstring says why it exists: *"Recorded as a test so nobody can later
   read 'stage 3 done' as 'port binds have left T1'."* The moment C-05.3b uses
   the split halves in `supervisor.py` — which §31.13 requires it to — the last
   two assertions fail. The honest update narrows them to the builder and fixer
   dispatch sites the test is actually about, rather than deleting the test: its
   purpose (stopping "stage 3 done" being read as "stages 4 and 6 done") is
   still worth protecting, and the first assertion already carries it.

### 31.14 Governance questions, collected

Nothing below can be answered from repository policy. **Blocking** means
implementation cannot start without it.

| # | Question | Blocking? |
|---|---|---|
| **G1** | The qualitative accessibility reviewer's worker timeout. `timeouts` has no `accessibility` key and `_lease_expires("accessibility")` raises `KeyError`. Must not collide with the `_seconds` keys C-05a deliberately kept non-role-shaped. | **Yes** |
| **G2** | May C-09 recognise a control-plane-owned port? Two halves: the allocator must not re-issue it to a Builder, and the orphan-listener check must not annunciate it. C-09's row is still `OPEN / PARTIAL` and scopes the allocator to "builder + fixer only". | **Yes** |
| **G3** | What command starts the product server; whether dependency install sits inside the 120 s readiness budget; what happens on the first PR, before any product exists. `product/` is specifications only. | **Yes** |
| **G4** | How long `WAITING_EVIDENCE` may hold on unobtainable evidence before `HUMAN_REQUIRED`. Same open question as §8 item 2, now with three ways to trigger it. | **Yes** |
| **G5** | Is `R#` per-evidence-class or shared across `A11Y`/`SEC`/`CODE` on one PR? If shared, the counter cannot live in C-05.3b. | No |
| **G6** | The nine `check_id` → requirement-identifier map. A policy statement about what each automated check proves. Blocks the automated-FAIL path only; the PASS path and the gate work without it. | Partial |
| **G7** | `max_accessibility_auto` and `max_accessibility_review`. Budget and rate-limit judgement, as `max_security` was. | **Yes** |
| **G8** | C-05.3b before or after C-18 stages 4–7. §14.5 left it open; §31.13 recommends before. | No |
| **G9** | Whether a 7-minute serialised stall inside `tick()` is acceptable, or whether C-05a's "runs in-process" ruling should be revisited so the browser run can be spawned and polled. | No |
| **G10** | A C-number for this work, and whether the `gate_evidence._adjudicate` inline-literal drift (§31.8) is folded in or raised as its own audit row. | No |

### 31.15 Expected affected files

| File | Change |
|---|---|
| `control/accessibility_contract.py` | **new** — the vocabulary leaf |
| `control/routing.py` | `parse_accessibility`, `accessibility_is_consistent`, both claim builders, `accessibility_worker_name`, the recovery table, both `*_claim_is_valid` |
| `control/supervisor.py` | Phases A–F for both halves; the composite gate inside `route_evidence`; `run_declared` around the browser run |
| `control/gate_evidence.py` | `allocate_attempt_at`; the a11y attempt namespace, provenance and outcome publication; the inline-literal fix |
| `control/workers.py` | the third port-exclusion source (**G2**) |
| `control/reconcile.py` | teach the orphan-listener check about claim-owned ports (**G2**), following R4's shape |
| `control/severity.py` | **no change** — it already does its job; only a caller is missing |
| `control/state.py` | **no change** — `WAITING_EVIDENCE → REVIEW` is already legal |
| `config/experiment.json` | `timeouts.accessibility` (**G1**), `concurrency.max_accessibility_*` (**G7**) |
| `control/config.py` | fields for the above |
| `prompts/accessibility.md` | remove the "dispatch is not yet implemented" paragraph (`:9-17`) once it is |
| `apparatus/accessibility/run.js` | **no change** — its contract is sound and its refusals are correct |

Tests to retain unchanged, because they pin behaviour this must preserve:
`tests/test_c05_3_evidence_routing.py` (except
`test_security_pass_alone_never_reaches_review`, which becomes
`..._alone_still_never_reaches_review` with the other two claims absent),
`tests/test_c05_3_evidence_claim.py`, `tests/test_c05_3_evidence_recovery.py`,
`tests/test_c05_3_security_contract.py`, `tests/test_c09_resource_lifecycle.py`,
`apparatus/accessibility/run.test.js`.

One test must be **narrowed, not deleted**:
`tests/test_c18_stage3_port_split.py::StageThreeDidNotMigrateTheDispatchSites`
— see §31.13.

### 31.16 Staged sequence

No stage may be started under this proposal. Each leaves the tree green.

| Stage | Scope | Why here |
|---|---|---|
| 1 | `control/accessibility_contract.py` + parser + `accessibility_is_consistent` | Pure, no state, no dispatch; and it closes the `_adjudicate` literal drift immediately |
| 2 | The composite gate, reading claims that do not exist yet | Fires only when all three are present; provably inert until stages 3 and 5 land, and testable today with synthetic claims |
| 3 | `accessibility_review` — the C-05.3a copy | Needs **G1**. Highest-value half: it alone exercises the gate end to end |
| 4 | Product server lifecycle + port ownership | Needs **G2** and **G3**. Isolated from the claims; separately testable against a trivial static server |
| 5 | `accessibility_auto` — claim, phases, `allocate_attempt_at` | Depends on stage 4. Last because it is the half with the weakest recovery evidence (§31.5 D1) |
| 6 | `CHECK_REQUIREMENT` map → `apply_severity_policy` | Needs **G6** and C-02. Inert-but-safe before it; nothing regresses by waiting |

**Tests each stage owes,** in the shape §7 and §14.4 already set: a
fail-before/pass-after proof that no external effect occurs inside T1 (the
`test_t1_claims_without_touching_the_filesystem` pattern — spy on `workers`,
`gh`, `prompts`, `gate_evidence` and assert none is called); crash between plan
and execute; duplicate-effect prevention across two ticks; identity recheck
refusing a superseded plan; and resource ownership surviving the crash window.

**Mutations that must be caught, at minimum:** drop the `sha == H` comparison
from any one of the three `claim_ok` legs (→ a mixed-SHA set passes the gate);
compare the three claims to each other instead of to `H` (→ a unanimously stale
set passes); increment `accessibility_cycles` at plan time (→ a failed attempt
burns an R#); drop `declare_busy` around the browser run (→ a healthy supervisor
reads as wedged past 120 s); drop the registry argument from
`apply_severity_policy` (→ an arbitrary `unmet_requirement` string validates);
read a readiness timeout as `FIX_REQUIRED` (→ an apparatus failure is reported
as a product defect); drop the third port exclusion (→ a Builder is handed the
product server's port); drop claim-owned ports from the orphan-listener check
(→ a working product server is annunciated as `FOREIGN_OR_ORPHAN_LISTENER`);
let `claim_ok` accept a non-empty `reason`.

### 31.17 Honest assessment of size

This is **larger than C-05.3a**, not comparable to it. C-05.3a was one evidence
producer with one claim; this is two producers with two claims, a composite gate
over three, a new process class the repository has never started, and a C-09
mechanism change. Stage 3 is a well-understood copy. Stages 4 and 5 are new
ground: nothing in this repository has ever started a server, and the automated
half's crash recovery rests on a singleton-lock argument rather than the `/proc`
proof C-05.3a could make.

**Four blocking governance answers are needed before any code is written**
(G1, G2, G3, G7); the fifth, G4, is needed before it ships rather than before
it starts. And G3 may not be answerable before a product exists at all —
which, if true, makes stages 4–5 genuinely blocked on T+00 rather than on an
implementer, and would mean the gate can only be closed for the *qualitative*
half pre-launch. That possibility should be considered explicitly rather than
discovered during implementation.

### 31.18 What this section does not do

- It does not implement anything. No production file was modified.
- It does not approve the four C-05a timeout values for any new consumer; it
  states which ones a design would consume and where.
- It does not build, specify or constrain C-02's requirement registry, nor
  C-04's evidence-package assembler. It states the assumptions it makes about
  both so they can be checked against what those agents actually land.
- It does not claim C-05.3b delivers a schema-valid Protocol v2 PR evidence
  package. It does not (§31.9).
- It does not answer G1–G10. Ten questions are escalated; none is guessed.
- C-05.3b remains **not begun**. T+00 remains **NOT_STARTED**.

## 35. C-04a realistic multi-cycle FIXTURE preflight harness — BUILT (2026-10-01)

Branch `wip/c04a-fixture-preflight`, cut from `wip/c05-1-persistence` @ `4eeaa7c`.
Worked in an isolated worktree.

**Document-provenance note.** Same situation as §29. This handover document is
untracked in the main checkout and did not exist in this worktree, so the main
checkout's copy (4814 lines, no sections 32-34 present at the time) was copied
in verbatim and this section appended. Sections 1-31 on this branch are a
point-in-time snapshot and are **not authoritative**; reconcile by hand before
merge if another agent appended in the meantime.

### 35.1 The requirement, verified before building to it

`experiment/CONTRADICTION-AUDIT.md` row C-04a, verbatim:

> (1) Pre-T+00: tested adapter code plus a realistic multi-cycle preflight
> (Protocol v2 "Preflight"; BOOTSTRAP deliverable 4) — Builder→PR→Review
> FAIL→Fix→CI→Accessibility/Security→fresh re-review→merge, at least two
> review cycles, exercised against an isolated FIXTURE PR/worktree with real
> git mechanics and real (not simulated) schema/validator/adapter calls. A
> real product task PR is explicitly NOT required for this.

Cross-checked against `protocol/RUN-002-PROTOCOL-v2.0.md` §"Preflight —
realistic, not synthetic" (lines 329-332), which adds "exact-SHA evidence
invalidation/regeneration" and "P2 demonstrably non-blocking" to the same
bullet list. Both quotes match the task brief exactly.

### 35.2 What was built

New directory `apparatus/fixture-preflight/`. **No existing file under
`control/` or `apparatus/` was modified** — parallel agents own those.

| File | Role |
|---|---|
| `fixture-repo.js` | REAL git. Creates a throwaway repository under the OS temp dir and drives the real `git` binary: real branches, real commits, real head SHAs, a real registered worktree, and its own `config/isolation.json` so the git-head adapter's worktree branch is exercised for real. |
| `fake-github.js` | INJECTED FAKE GitHub. In-memory. No network, no `gh`, no token, no real PR. Models exactly four forge behaviours: a draft cannot be merged; a merge naming a non-current head is refused; a merged/closed PR cannot be merged again; a push does **not** retroactively rebind an earlier review's SHA. |
| `evidence.js` | Builds real PR-EVIDENCE-V2 packages bound to REAL fixture SHAs (never `'a'.repeat(40)`), including all ten Protocol v2 accessibility checks and all twelve security surfaces. |
| `merge-eligibility.js` | **THE SEAM.** Local merge-eligibility composition, pending the parallel work stream that owns the production layer. See §35.4. |
| `scenario.js` | The two scenario drivers and their event transcripts. |
| `multi-cycle.test.js` | The Protocol v2 preflight scenario (12 tests). |
| `lifecycle.test.js` | The operator-required autonomous lifecycle acceptance scenario (8 tests). |

Run with:

```
cd apparatus && node --test fixture-preflight/*.test.js
```

20 tests, 20 passed, 0 failed.

### 35.3 REAL vs STUBBED — the honest split

**Real, not simulated:**

- **git** — every branch, commit, head SHA and worktree, created by invoking
  the real `git` binary. Commit identity is asserted independently
  (`git cat-file -t`), and SHAs are re-resolved by the adapter rather than
  trusted from the fixture.
- **`apparatus/adapters/git-head.js`** — `resolveTrustedHeadSha` is called for
  real, on both its branch identity path (with the mandatory `run-002/` prefix)
  and its worktree identity path (real `config/isolation.json`, real
  `git worktree list --porcelain` registration check).
- **`apparatus/adapters/task-record.js`** — called against this repository's
  real committed `config/tasks.json` to resolve dependent tasks. The
  "unblock dependent tasks" step is therefore driven by real task-graph data,
  not an invented dependency list.
- **`apparatus/pr-evidence/validate.js`** — called for real, which really
  compiles `protocol/PR-EVIDENCE-V2.schema.json` under Ajv strict mode and
  really applies `apparatus/severity/severity-floor.js`. **P0/P1 blocking and
  P2/P3 non-blocking are therefore decided by production code, not by anything
  in this harness.**

**Stubbed, and deliberately so:**

- **GitHub** — `fake-github.js`. C-04a explicitly does not require a real
  product PR, and the brief forbids live GitHub, real PRs, real merges, paid
  calls, workers and notifications. None occur.
- **The merge-eligibility composition** — `merge-eligibility.js`. See §35.4.
- **The content of the evidence** — no axe run, no CI job and no reviewer
  produced the check results. That is the agreed shape of a *fixture*
  preflight: the mechanics and the calls are real, the subject is a fixture.

### 35.4 The seam, and the interface expected from the parallel layer

A parallel work stream owns the production merge-eligibility composition.
`apparatus/fixture-preflight/merge-eligibility.js` is a stand-in so this
harness could be driven end to end without blocking on it. The tests are
written against the **interface**, not the implementation, so the swap is a
one-line change per test setup:

```js
decideMergeEligibility({
  repoRoot,     // string  — repository the git adapter resolves in
  identity,     // object  — { kind: 'branch', ref } | { kind: 'worktree', path }
  evidence,     // object  — a PR-EVIDENCE-V2 package
  pullRequest,  // object  — { number, state, draft, headSha,
                //             reviews: [{ producer, sha, verdict }] }
}) -> {
  eligible:       boolean,
  reasons:        string[],   // stable codes, empty iff eligible
  trustedHeadSha: string | null,
}
```

Reason codes emitted: `GIT_HEAD_UNRESOLVED:<adapter reason>`,
`OFFLINE_POLICY_FAILED`, `NO_EVIDENCE`, `EVIDENCE_SHA_STALE`,
`EVIDENCE_SECTION_SHA_STALE:<section>`, `NO_PULL_REQUEST`, `PR_NOT_OPEN`,
`PR_IS_DRAFT`, `PR_HEAD_DIVERGED`, `NO_APPROVING_REVIEW_AT_HEAD`.

Inside it, the git-head adapter call and the validator call are **real**; the
pull-request state rules (draft, open, head agreement, approval freshness) and
the composition order are the stubbed part. It fails closed: anything it cannot
establish becomes a reason, never a pass.

**Change needed in a file this agent does not own.**
`apparatus/pr-evidence/validate.js:74-79` cross-checks only `ci.sha` and
`review.sha` against `head_sha`. It never compares `accessibility.sha` or
`security.sha`, so an evidence package can carry accessibility or security
evidence from a different commit and still pass the offline policy. It also has
no access to a trusted head at all, by its own design. Both gaps are closed in
`merge-eligibility.js` rather than in the validator, because another agent owns
that file. The exact change wanted there is: extend the `INTERNAL_SHA_MISMATCH`
loop to cover `accessibility` and `security` whenever those blocks carry a
`sha`.

### 35.5 Scenario 1 — the Protocol v2 multi-cycle preflight

`runMultiCycleScenario`, in `scenario.js`:

1. Builder commits on a real `run-002/fixture-task-001` branch → **SHA1**.
2. PR opened at SHA1 (fake forge).
3. CI reported PASS at SHA1.
4. **Review cycle 1 → REVIEW_FAIL** on a P1 code finding. Eligibility refuses:
   `OFFLINE_POLICY_FAILED`, `NO_APPROVING_REVIEW_AT_HEAD`.
5. Fixer commits → **SHA2**. The head really moves; the fake forge is told.
6. **Exact-SHA invalidation.** The unchanged cycle-1 evidence is re-decided
   against the new trusted head and refused.
7. **Regeneration.** CI, accessibility and security re-run at SHA2;
   **review cycle 2 → REVIEW_PASS** at SHA2.
8. Eligible → merged at SHA2.

The isolating test matters more than step 6 does on its own: the cycle-1
package also carries a P1 and a REVIEW_FAIL, so its refusal cannot prove
*staleness* was the cause. A separate test builds an **otherwise-clean**
package at SHA1, asserts `checkOfflinePolicy` passes it on its own terms, then
asserts eligibility refuses it with `EVIDENCE_SHA_STALE` present and
`OFFLINE_POLICY_FAILED` **absent**.

**P2 demonstrably non-blocking.** The merging package carries three open P2
findings at once — a P2 `ACCEPTED_NONBLOCKING` code finding, a P2
`ACCEPTED_NONBLOCKING` security finding, and a P2 `NON_FAILURE` accessibility
finding with an affirmative rationale — and still merges. Two controls stop
that from degenerating into "nothing ever blocks": the same scenario with a P1
instead does **not** merge, and a test flips only the code finding's severity
between P2 and P1 on an otherwise byte-identical package and asserts the real
validator's verdict flips with it.

### 35.6 Scenario 2 — the autonomous lifecycle (operator acceptance requirement)

`runAutonomousLifecycle`. Full shape: **create PR → independent review →
required evidence/checks → mark ready if drafted → merge when eligible →
confirm merge → unblock dependent tasks.**

**(a) DRAFT → READY → MERGED, no human step.**
The PR opens as a draft. Eligibility is consulted *first* and returns exactly
`['PR_IS_DRAFT']` — everything else already passes. The driver marks ready
**only when being a draft is the sole remaining obstacle**, so a failing PR is
never un-drafted to force it through. It re-decides, merges, then **confirms
the merge by reading the forge back** rather than trusting the merge call's
return. Dependents are then resolved from the real task graph:
merging `TASK-001` unblocks `TASK-002`, `TASK-003`, `TASK-004`. The test
asserts the stage order `pr_created → check_reported → review_submitted →
pr_marked_ready → pr_merged` and that **no transcript event carries
`actor: 'human'`**.

**(b) CHANGED HEAD — a stale approval must not authorize a merge.**
An approving review lands at SHA1, then an unreviewed commit lands at SHA2.
Eligibility refuses with `NO_APPROVING_REVIEW_AT_HEAD`; the PR stays OPEN; no
`pr_merged` event is emitted; nothing downstream unblocks.

Mutation 4 (§35.7) exposed a weakness in the first version of case (b): the
evidence was *also* stale, so the approval-freshness rule was not independently
load-bearing. A test was therefore **added** — the dangerous real-world shape,
where CI/accessibility/security are all regenerated at the new head and the
package is clean, but **nobody re-reviewed**. Eligibility must refuse with
`['NO_APPROVING_REVIEW_AT_HEAD']` as the *only* reason. Under mutation 4 that
test reports `eligible: true`, i.e. a stale approval would genuinely have
authorized a merge.

### 35.7 Mutation testing — each proved red, restored, restoration verified

A fixture that passes by construction proves nothing. Every required mutation
was applied, the suite run, the red observed, the mutation reverted, and the
green re-observed. Baseline throughout: 20 passed / 0 failed.

| # | Mutation | Where | Result |
|---|---|---|---|
| 1 | Stale-SHA evidence set accepted — drop the `EVIDENCE_SHA_STALE` check | `merge-eligibility.js` | **2 red** — EXACT-SHA INVALIDATION, EXACT-SHA REGENERATION. Restored → 19/19. |
| 2 | Second review cycle skipped — reuse cycle 1's review after the fix | `scenario.js` | **4 red** — incl. "AT LEAST TWO review cycles" (`got 1`) and the P2 test (the merge never happened). Restored → 19/19. |
| 3 | P2 treated as blocking — block on any finding regardless of severity | `merge-eligibility.js` | **7 red** — incl. "P2 IS DEMONSTRABLY NON-BLOCKING" and the draft lifecycle (the ready step never fired). Restored → 19/19. |
| 4 | Changed head does not invalidate approval — accept any approval at any SHA | `merge-eligibility.js` | **2 red** — both case-(b) tests. The added test reported `eligible: true`. Restored → 20/20. |
| 5 | Draft PR merged without the ready step — drop the `PR_IS_DRAFT` hold | `merge-eligibility.js` | **2 red** — incl. `missing lifecycle stage: pr_marked_ready`. Restored → 20/20. |

Mutation 4 was run twice: once before the §35.6 test was added (1 red) and once
after (2 red), to confirm the new test catches it independently of the evidence
staleness that was masking it.

No `MUTATION` marker survives in the tree; verified by grep after restoration.

### 35.8 What remains before C-04a could be called satisfied

This harness discharges the *fixture preflight* half of C-04a requirement (1).
It does **not** discharge the row. Outstanding:

1. **The production merge-eligibility composition layer does not exist.** The
   decision this harness proves correct is made by a stub
   (`fixture-preflight/merge-eligibility.js`), not by shipping code. Until the
   parallel layer lands and the tests are re-pointed at it, what is proven is
   the *scenario*, not the *system*. **This is the single largest gap.**
2. **The CI-result and reviewer-identity adapters are not driven here.** §28
   reports them implemented on `wip/c04-adapters`; they were not present in
   this worktree. The harness still reads the submitted `ci.status` field,
   which is exactly the self-attestation `validate.js:11-19` warns about. Once
   those adapters merge, the harness should call them in place of the submitted
   fields.
3. **The accessibility requirement registry is not driven here.** §29 reports
   C-02 built and wired. The harness's accessibility finding is deliberately
   `NON_FAILURE`, which does not exercise the registry path at all; a
   `FAILURE`-classified finding citing a real registry identifier should be
   added once that lands, together with its negative (an unknown identifier
   must fail closed).
4. **`validate.js` still does not SHA-bind accessibility or security
   evidence.** See §35.4. Closed in the stub only.
5. **CI does not invoke any of this.** `.github/workflows/ci.yml` still does
   not run `apparatus/pr-evidence/validate.js`, the apparatus test suite, or
   this harness — the C-04 closure criterion that "the schema, validator, and
   adapters existing is not sufficient if CI never actually calls any of it"
   applies verbatim to the fixture harness too.
6. **The audit row has not been updated.** `experiment/CONTRADICTION-AUDIT.md`
   C-04a and `experiment/PREFLIGHT-FINDINGS.md` still read as though the
   multi-cycle preflight has not been run. Updating them is deliberately left
   to whoever reconciles the parallel branches, because items 1-5 above mean
   the row should move from PARTIAL to PARTIAL-with-better-evidence, **not** to
   GREEN.
7. **Requirement (2) is untouched and should stay that way** — per-product-PR
   checking after T+00 needs no pre-launch demonstration by the row's own
   wording.

### 35.9 Environment note

`apparatus/node_modules` does not exist in a fresh worktree (it is gitignored
and lives in the main checkout). It was symlinked in so `ajv` would resolve.
The symlink is untracked and was **not** committed; a reviewer checking this
branch out needs either that symlink or `npm install` under `apparatus/`.

## 36. C-05.3b foundations — IMPLEMENTED, NOT WIRED (2026-10-01)

**Status: the leaves are built and proved. Nothing is wired.** No dispatch
path, no state transition, no ingest, no `Supervisor` change, no
`config/experiment.json` change. A task in `WAITING_EVIDENCE` still holds
exactly as it did before this branch, and §31.1's defect is still open. This
section is not a claim that C-05.3b is implemented; it is stage 1 and stage 2
of §31.16's six, and the four blocking governance answers are still blocking.

Branch `wip/c05-3b-foundations`, based on `wip/c05-1-persistence` at `8425e32`.
Section 31 is the specification this follows.

### 36.1 What was built

| Artefact | What it is |
|---|---|
| `control/accessibility_contract.py` | **new** — the verdict and failure-reason vocabulary, in `security_contract.py`'s exact shape |
| `control/routing.py` — new functions only | `normalize_accessibility_auto`, `accessibility_auto_finding`, `review_gate_fires`, `_leg_passes`, and the check-id / leg tables |
| `tests/test_c05_3b_accessibility_contract.py` | **new** — 36 tests |
| `tests/test_c05_3b_review_gate.py` | **new** — 22 tests |

**One existing line in `control/routing.py` was changed:** its import, to add
`accessibility_contract`. No existing function in that file was touched —
`evaluate_merge`, `parse_security`, `security_is_consistent`,
`security_claim_is_valid` and the claim helpers are byte-identical.

**The contract.** Five verdict tokens in two deliberately disjoint families
(`ACCESSIBILITY_PASS` / `_FAIL` / `_UNPARSEABLE` for the qualitative half,
`ACCESSIBILITY_AUTO_PASS` / `_FAIL` for the machine half), seven failure
reasons defined here, ten aliased from `security_contract`, and two frozensets:
`ACCESSIBILITY_FAILURE_REASONS` (seventeen) and
`ACCESSIBILITY_AUTO_FAILURE_REASONS` (nine). The two verdict families do not
overlap, and a test holds that line: a single shared `ACCESSIBILITY_PASS` would
make a record carrying only the automated half indistinguishable from one
carrying both, which is precisely the collapse `agents/ACCESSIBILITY.md:8`
forbids.

`SURFACES_INCOMPLETE` is deliberately **not** aliased in — §31.8's reasoning,
held by a test that asserts the attribute does not exist.

**The composite gate.** `review_gate_fires(record, head_sha)` — pure over
`(record, head)`, reads no state, touches no file, mutates nothing. Every leg
is compared to the head observed this tick and never to another leg. A
unanimously stale evidence set — three claims agreeing perfectly with each
other about a superseded commit — does not fire, and that case is a test, not
a comment.

Ordering inside the predicate is load-bearing and is explained in the source:
the three structural checks run first, the one claim validator that exists
second. See §36.5 D1 for why.

**The automated normalisation.** `normalize_accessibility_auto(checks, head)`
returns `(verdict, "")` or `(None, reason)`, never both and never neither, in a
fixed precedence. It takes the check LIST, not the whole attempt outcome: a
`FAILED` outcome already carries its own finite reason from `gate_evidence`,
and re-deriving it would be a second opinion about something already
adjudicated. A duplicated `check_id` is refused rather than last-wins — two
disagreeing results for one check is unresolved evidence, and letting the later
one win would make the order of a JSON array decide a gate.

### 36.2 What was deliberately NOT built, and why

- **Any `Supervisor` wiring.** Out of scope by instruction, and it depends on
  G1/G3 and on the C-18 harness work. The gate predicate exists and is proved;
  calling it is someone else's commit.
- **`parse_accessibility` / `accessibility_is_consistent`** — the qualitative
  parser. §31.16 puts it in stage 1, but it must run findings through
  `severity.apply_severity_policy` against a requirement registry, and both
  assumptions §31.10 recorded about that registry turned out to be false
  (§36.3). Building a parser on a falsified assumption would have to be
  rewritten rather than extended. The vocabulary it needs is defined and
  waiting.
- **The `CHECK_REQUIREMENT` map contents.** G6. The structure is built, the map
  is empty, and a test pins the consequence: every automated FAIL yields a
  finding with no citation, which `severity.py` rates `INVALID` with
  `merge_blocked: True`. The gate holds. This is §31.10's designed
  inert-but-safe state, not an oversight — and the test means filling the map
  becomes a deliberate act with a visible consequence.
- **Claim builders and claim validators** for either accessibility leg. They
  belong with the claims, which belong with the wiring.
- **The `gate_evidence._adjudicate` inline-literal repair** (§31.8). The four
  literals now have an owner in `accessibility_contract`; `_adjudicate` still
  spells them inline. The repair is four lines, but `gate_evidence.py` is owned
  by another agent this cycle and a merge conflict there is more expensive than
  the drift. **Behavioural agreement is pinned instead**: a test drives the real
  `_adjudicate` down each of its six failure paths and asserts the reason it
  emits is in `ACCESSIBILITY_AUTO_FAILURE_REASONS`. Rename either side and the
  suite goes red. The repair itself is follow-up F1.
- **The FAIL and hold rows of §31.11's precedence table.** `review_gate_fires`
  answers only "may this advance to REVIEW". What a FAIL routes to is the
  caller's, and how long a hold may last is G4.

### 36.3 Two design assumptions that are now false — material, found while building

§31.10 recorded three assumptions about C-02's requirement registry "stated so
they can be checked". Two of them do not hold against what C-02 actually landed
in `apparatus/accessibility/requirement-registry.js`:

1. **Assumption 2 is wrong about the identifier form.** §31.10 assumed
   kebab-case — `visible-focus-indicator`, `keyboard-focus-trap`. The registry
   that landed uses `ACC-DOD-VISIBLE_FOCUS`, `ACC-COG-PREDICTABLE_NAVIGATION`:
   a group prefix plus a SCREAMING_SNAKE semantic name, seventeen entries
   (twelve definition-of-done, five cognitive). Anything written against the
   assumed form would cite identifiers the registry does not contain, and
   `severity.py` would rate every such finding `INVALID` — fail-closed, so
   nothing unsafe, but a gate that can never open.

2. **Assumption 1 is not met.** It required the registry to be "reachable from
   `control/` as an in-memory collection of identifier strings, obtained
   without a filesystem read at call time". What landed is a **JavaScript
   module with no Python counterpart**. `grep` for `known_requirement_ids`
   across `control/` and `tests/` returns only `severity.py`'s own definition —
   there is no Python caller and no Python registry. §31.10 itself says what
   follows: *"If C-02 lands as a file read, the read must move to Phase A
   observation and this assumption breaks."* It has broken, in a stronger form:
   there is nothing to read from Python at all.

   Assumption 3 (set membership, no severity attached to the identifier) **does**
   hold.

This is a decision, not a defect to fix quietly — see D2 below.

### 36.4 Verification

- `python3 -m unittest discover -s tests` — **1722 tests, OK**, run standalone
  and unpiped. 58 of those are new.
- `python3 scripts/check_no_secrets.py` — clean. `git diff --check` — clean.
- No paid call, no notification, no worker, no browser launch, no rehearsal.

**Mutations, each applied to the real source, proved red, then restored with
the restoration verified by checksum and a green re-run:**

| Mutation | Result |
|---|---|
| Legs compared to each other instead of to the head (a mixed / unanimously stale evidence set is accepted) | 5 tests red |
| A missing leg treated as passing | 3 tests red |
| An unparseable verdict, and an unreadable check list, treated as a pass | 9 tests red |
| The head comparison dropped in both the gate and the normalisation | 8 tests red |
| The `isinstance(result, str)` guard removed from the normalisation | 4 tests red (errors) |

The fifth was not on the required list. It was found by re-reading the diff
before committing: `result in CHECK_RESULTS` hashes its left operand, so a
check whose `result` holds a dict or a list — which durable JSON can carry —
raised `TypeError` out of a function whose entire contract is to return a
finite reason. Fixed, and the same hazard is covered for `check_id`. Failing
closed means RETURNING a reason, never raising one.

Two further guards are tested but were not required: an uncheckable head
(`None`, `""`, a short hex string, uppercase) never fires the gate — without
that guard an empty head and an empty claim sha compare equal and every leg
passes vacuously — and one leg's verdict can never satisfy another leg.

### 36.5 Material decisions still needed

G1, G3, G4 and G7 are unchanged and still blocking; nothing here answers or
pre-empts any of them. The four below are **new**, found while building. Each
is phrased so it can be answered in a sentence.

**D1 — `security_claim_is_valid` raises `TypeError` on a claim whose verdict is
unhashable.** `routing.py:1039` evaluates `verdict in SECURITY_VERDICTS`; a
verdict holding a `{}` or a `[]` — both of which durable JSON can carry —
raises instead of returning a diagnostic, so a corrupt PR record crashes the
tick rather than failing closed. This is a C-05.3a defect reachable from its
existing call sites, not only from this work. It was recorded rather than
patched because that function is C-05.3a's and is outside this branch's scope.
`review_gate_fires` orders its checks so the crash is unreachable through the
new gate, and a test pins both halves of that. **Decision needed: is this
repaired in C-05.3a, or folded into C-05.3b?**

**D2 — the requirement registry needs a Python reader, and nobody owns it.**
Per §36.3 the registry is JavaScript-only. Something must make those seventeen
identifiers reachable from `control/` before any accessibility FAIL can be
adjudicated as anything but `INVALID`. **Decision needed: does C-02 export a
Python-readable form (a generated JSON beside the JS module is the obvious
shape), does C-04 own it, or does C-05.3b read `product/ACCESSIBILITY.md`
itself?** The third option duplicates C-02's parser and would need its own
drift test. Whichever is chosen, §31.10's "no filesystem read inside T1" rule
means the read belongs in Phase A observation, not in the transaction.

**D3 — G6's answer must be expressed in `ACC-DOD-*` / `ACC-COG-*`
identifiers.** Not a new question, but the design's worked example for it is now
wrong (§36.3), so answering G6 from §31.10 as written would produce a map that
can never match. **Decision needed: confirm that the nine `check_id` →
requirement map is to be written against the landed registry's identifier
form.** Also still open from §31.10: whether `AXE_SCAN` maps coarsely to one
requirement or whether axe violation ids become registry identifiers in their
own right — the landed registry derives strictly from
`product/ACCESSIBILITY.md` and has no room for axe ids, which makes the coarse
option the only one available without widening C-02's charter.

**D4 — the two accessibility legs' claim validators are not registered with the
gate.** `_leg_passes` checks what can be checked about any leg — object,
`COMPLETE`, bound to this head, carrying this leg's verdict, carrying no
reason — and the security leg additionally gets `security_claim_is_valid`. The
two accessibility legs have no validator because their claims do not exist yet.
**Decision needed: whoever builds the `accessibility_auto` and
`accessibility_review` claims must add their validators to `review_gate_fires`
in the same commit** — a leg whose claim exists but whose validator is not
wired in is checked more weakly than the security leg, and the difference is
invisible at the call site. Flagged here so it is not discovered later.

**F1 — follow-up, not a decision.** Fold the four inline literals in
`gate_evidence._adjudicate` into `accessibility_contract` once that file is no
longer contended. Behaviour-neutral; the agreement test already prevents drift
in the meantime.

### 36.6 What this section does not claim

- It does not claim the accessibility gate is implemented. It is not wired; a
  passing task still holds in `WAITING_EVIDENCE` forever.
- It does not claim C-05.3b delivers a schema-valid Protocol v2 PR evidence
  package. §31.9 is unchanged — no finding IDs are minted.
- It does not answer G1 through G10.
- It does not change `control/severity.py`, `control/state.py`,
  `config/experiment.json`, any frozen source, or any existing function in
  `control/routing.py`.
- T+00 remains **NOT_STARTED**.

## 34. C-04 live merge-gate composition layer — IMPLEMENTED (2026-10-01)

Branch `wip/c04-merge-composition`, cut from `wip/c05-1-persistence` @ `8425e32`,
worked in an isolated worktree. Scope was the composition layer, the
merge-eligibility path in `control/routing.py`, and end-to-end SHA-bound
provenance. Nothing under `product/`, `tasks/`, `protocol/` or
`config/tasks.json` was touched. `Supervisor.dispatch_reviewer`,
`dispatch_fixer`, `on_dispatch_failure`, `control/merge_invariant.py` and
`tests/test_c18_stage3_port_split.py` were not modified — other agents own them.

**Document-provenance note.** This section was appended only inside this
worktree's copy of this file, per the integration owner's instruction. This
branch's copy is a point-in-time snapshot and is not authoritative for sections
1–33.

### 34.1 What was built

**`apparatus/pr-evidence/live-gate.js`** (new) — the composition entry point.
Before it, nothing in the repository imported two adapters together, so no code
anywhere computed a live merge decision; §28.5 recorded that gap explicitly.

```js
evaluateLiveMergeEligibility(request, adapters) -> {
  decision: 'ELIGIBLE' | 'DENIED',
  trustedHeadSha: string | null,
  draftState: 'READY' | 'DRAFT' | 'UNKNOWN',
  blockedOnlyByDraft: boolean,
  reasons: [ { code, determination, detail } ],
  adapters: { headSha, ci, task, reviewer, reviewProvenance,
              evidenceShaBinding, offlinePolicy },
}
```

`request` — `{ identity, prNumber, pkg, prView, ledgerEvents }`.
`adapters` — `{ resolveHeadSha(identity), resolveCi(headSha),
resolveTask(taskId), resolveReviewer(claim) }`, **all four required and all
injected**. Nothing is defaulted to a real entrypoint, so this module cannot
reach git, `gh` or `.runtime/` even by accident, and a caller must say
explicitly where each fact comes from. Every test injects stubs.

**The anchor is the trusted head SHA, never the package.** `git-head.js`
resolves the current commit for the task identity. CI, the task record, the
reviewer identity, the review provenance and every evidence block are then
checked against *that* value. `pkg.head_sha` is never an input to any adapter;
it is only ever compared to the trusted head and must equal it. This is the
whole point of C-04: a model's claimed SHA is a claim, and a claim can be
written by the thing being judged.

**DEFAULT DENY, with a backstop.** The decision starts `DENIED` and is raised
to `ELIGIBLE` by one explicit positive assertion at the bottom that re-reads
every adapter's own `ok` flag, the trusted SHA's shape and the draft state.
Deleting a denial above is therefore not enough to open the gate — proved by
mutation M5 (§34.8), where the CI adapter's result was replaced wholesale and
the suite still went red.

Every unknown denies. An unobservable head, an absent `isDraft`, a missing
`mergeStateStatus`, an unreadable ledger and any adapter returning
`COULD_NOT_VERIFY` are all denials. `COULD_NOT_VERIFY` is not weak evidence of
health; it is the absence of evidence.

### 34.2 The ledger contract this gate requires

A parallel agent is adding the head SHA to the review ledger events. This is
the exact contract `live-gate.js` codes against, and it is implemented against
fixtures so neither agent blocks the other.

> `REVIEW_DISPATCHED` and `REVIEW_RESULT`, for `role: "reviewer"`, MUST each
> carry the full 40-character lowercase-hex head SHA the reviewer worker was
> dispatched against, at **`metadata_redacted.head_sha`**.

Mechanically, in `control/supervisor.py::dispatch_reviewer` and the
`REVIEW_RESULT` emission in `ingest_review`, that is:

```python
self.log("REVIEW_DISPATCHED", ..., head_sha=head)
self.log("REVIEW_RESULT", ..., head_sha=record["reviewed_head"],
         metadata_redacted=review.as_dict())
```

`control/ledger.py`'s `FIELDS` tuple has no `head_sha`, and `Ledger.append`
merges every unrecognised kwarg into `metadata_redacted` (`ledger.py:97-103`),
so the two calls above land the value in the right place with no change to
`ledger.py` at all.

Three properties the composition relies on, in order of how it reads them:

1. **Both events must carry it.** `REVIEW_RESULT` alone is not enough: the
   dispatch is what binds the *request* to a commit, the result binds the
   *answer*. A result bound to commit B whose dispatch names commit A is
   refused (`REVIEW_PROVENANCE_SHA_MISMATCH`).
2. **Either spelling is read, and disagreement is fatal.** If `head_sha` is
   later promoted into `FIELDS` it will appear at the top level instead. Both
   are read, so the promotion does not break this module. When **both** are
   present they must be identical — two disagreeing provenances are worse than
   one, because nothing may choose between them
   (`REVIEW_PROVENANCE_CONFLICT`).
3. **The SHA-bound `REVIEW_RESULT` must name the worker reviewer-identity
   already verified.** Otherwise a correctly-SHA-bound event from one dispatch
   could vouch for a verdict produced by another
   (`REVIEW_PROVENANCE_WORKER_MISMATCH`).

**Why this is separate from `reviewer-identity.js`'s own SHA check, rather than
a duplicate of it.** That adapter's only SHA source is
`prs[n].reviewed_head` in `.runtime/state.json` — **mutable** state that the
next review cycle overwrites and that anyone who can write the file can edit;
its own header says so, and §28.5 records it as the adapter's central
limitation. The ledger is append-only and fsynced per event. So the two checks
answer different questions, and the ledger one is the stronger: a verdict for
commit B must be carried by events that themselves name commit B. An older
cycle's `REVIEW_PASS` can no longer be re-presented against a newer commit,
which §28.3 item 2 and §28.5 both recorded as unclosable without exactly this
change. **That closes defect 1 of §28.3 for the code reviewer** — the
asymmetry with the SHA-bound security worker name.

Until the parallel agent lands the field, every real `REVIEW_RESULT` will fail
`REVIEW_PROVENANCE_UNBOUND` with determination `COULD_NOT_VERIFY`. That is the
correct behaviour, not a regression: no live merge is authorised today anyway,
and failing closed on absent provenance is the requirement.

### 34.3 Every evidence class is bound, not just two

`checkOfflinePolicy` compares only `ci.sha` and `review.sha` to `head_sha`
(`validate.js:94-99`). Accessibility and security evidence produced against a
**different commit** therefore passes the offline policy untouched — a hole the
fixture-harness stream found independently and which this branch closes rather
than inherits.

`verifyEvidenceShaBinding` requires every SHA-bearing path in the package to
equal the trusted head:

| Path | Schema-required | Treated as |
|---|---|---|
| `{ci,review}.sha` | yes | absence denies |
| `{ci,review,accessibility,security}.provenance.sha` | yes | absence denies |
| `{accessibility,security}.checks[n].sha` | yes | absence denies |
| `{accessibility,security}.sha` | **no** | checked when present; absence is a schema gap (§34.5), not a forgery |

Binding belongs in the composition rather than in `checkOfflinePolicy` for a
reason beyond file ownership: the offline checker can only compare these values
to `head_sha`, which the submitter also wrote, so agreement there proves
internal consistency and nothing else. Here each is compared to the head git
itself resolved. A test asserts the control — `checkOfflinePolicy` returns
`policyValid: true` for a package with accessibility evidence from another
commit — so the composition is demonstrably doing work nothing else does.

### 34.4 `control/routing.py::evaluate_merge` — two defects fixed

**(a) Approval invalidation keyed on the diff, not the SHA.** `evaluate_merge`
compared `reviewed_diff_hash` to `current_diff_hash` and never once compared
`reviewed_head`, which `dispatch_reviewer` has recorded all along
(`supervisor.py:1478`). Protocol v2 "Evidence provenance" says *"new SHA =>
regenerate required automated evidence"*
(`protocol/RUN-002-PROTOCOL-v2.0.md:223`) — the head SHA is the thing that rule
names. **A force-push producing a byte-identical diff from a different commit
passed the gate**, merging a commit no reviewer had seen while every piece of
SHA-bound evidence belonged to a commit that was no longer the head.

The head comparison now runs **before** the diff check. Both remain: a
reconciliation merge moves the head legitimately, and a changed diff under an
unchanged head is a different fault. Absence is a denial, not an exemption —
`None != None` is `False`, so without an explicit guard "nothing to compare"
would have read as "nothing changed". Both denials set
`invalidate_approval=True`, or the next tick would re-evaluate the same stale
approval against the new head.

**(b) A draft and a closed pull request shared one refusal sentence.** `state
!= "OPEN" or pr.get("isDraft")` returned `"PR #N is not an open, ready pull
request"` for both, so `MERGE_BLOCKED` could not distinguish them and a draft
could stall a task with nothing in durable evidence saying a draft did it. They
are now two branches with two reasons and two finite condition codes.

**(c) A finite condition vocabulary.** `MergeDecision` gains
`condition: str` — one of fourteen `routing.MERGE_*` codes — and
`attempt_merge` writes it into the `MERGE_BLOCKED` event's
`metadata_redacted.condition` alongside the existing prose. Durable evidence no
longer has to be parsed as English.

`blank_pr_record` gains `"reviewed_head": None`, declared as explicit absence
like its sibling optional fields.

### 34.5 Draft-to-ready — GAP RECORDED, NOTHING IMPLEMENTED

**Determination: marking a drafted PR ready is NOT governed.** Evidence,
checked rather than assumed:

- The word "draft" does not appear in `protocol/RUN-002-PROTOCOL-v2.0.md` at
  all. "Definition of done" lists eleven conditions and names no draft state;
  "Merge execution" and "PR contract" say nothing about it.
- No governed state exists for it. The task lifecycle is `QUEUED → READY →
  ASSIGNED → ACTIVE → PR_OPEN → REVIEW → FIX_REQUIRED → MERGE_READY → MERGED →
  COMPLETE` (protocol line 159-161). `READY` there is a *task* state meaning
  dependencies are met; it has nothing to do with a pull request's draft flag.
- Nothing in the control plane creates a draft pull request: `--draft` appears
  nowhere in `control/`, `scripts/`, `bin/` or `guardrails/`, and nothing calls
  `gh pr ready`. `isDraft` is read in exactly two places — `gh.py:65` (a
  requested field) and the gate.
- Every existing test fixture hard-codes `isDraft: False`, so the draft path
  had **never been exercised**. `tests/test_c04_merge_eligibility.py` now
  exercises it.

So nothing was implemented for draft-to-ready, and no eligibility predicate was
reused to justify one. Inventing the action would amend a frozen specification
by implication. A test (`test_nothing_in_routing_marks_a_pull_request_ready`)
pins that absence, so a future addition has to be deliberate.

**The smallest explicit amendment proposed**, for the operator to accept or
refuse — one sentence, added to "Merge execution" in
`protocol/RUN-002-PROTOCOL-v2.0.md`:

> *"A pull request opened in draft may be marked ready for review by the
> Supervisor, and only by the Supervisor, when every condition of the
> deterministic merge gate other than the draft flag itself is satisfied. The
> transition is recorded as `PR_MARKED_READY` and is reversible by a human at
> any time."*

Two properties make it the smallest safe form: it reuses the *existing* gate
rather than introducing a second, weaker predicate, and it names one actor, so
no worker can take its own pull request out of draft. If accepted, the
implementation is a single call sited exactly where `MERGE_PR_IS_DRAFT` is
reported today, behind `blockedOnlyByDraft`.

**Meanwhile, a draft is never a silent stall.** `blockedOnlyByDraft` is true
exactly when the draft flag is the *only* thing standing between the pull
request and eligibility — a reportable "this would merge if a human took it out
of draft", distinguishable from every other refusal.

**Other gaps found and NOT papered over:**

1. **The PR-evidence schema carries no `pr_number`.** Confirmed again: the
   top-level required set is `task_id, head_sha, ci, review, accessibility,
   security`. The reviewer-identity adapter and the review-provenance check
   both need a PR number, so `evaluateLiveMergeEligibility` takes it out of
   band as `request.prNumber`. `protocol/PR-EVIDENCE-V2.schema.json` is a
   frozen imported source and was not edited. Same gap as §28.3 item 3,
   unresolved.
2. **`accessibility.sha` and `security.sha` are optional in the schema** while
   `ci.sha` and `review.sha` are required. An applicable accessibility block
   can therefore omit its own block-level commit. `provenance.sha` is required
   and is bound, so the hole is narrow, but the asymmetry is unexplained and
   the schema cannot be edited here.
3. **Nothing in the control plane submits a GitHub approving review**, while
   branch protection on `main` requires one. `mergeStateStatus` would be
   `BLOCKED` and the gate would correctly refuse — but as a *governance*
   condition, not an evidence defect, so `PR_BLOCKED_BY_BRANCH_PROTECTION` is
   named separately and says so in its detail text. No review-submission code
   was added; that is with the operator.
4. **`validate.js`'s header is still partly out of date** (§28.3 item 4): lines
   12-16 say the CI and worker-registry adapters "do not exist in this
   repository yet". All four now exist and a composition layer now joins them.
   Correcting that text was left alone this session to avoid a conflicting edit
   on a file three streams have touched; it should be corrected at integration.
5. **`.github/workflows/ci.yml` still never runs any of this.** No root
   `package.json` exists, so the Node steps are skipped. Neither `validate.js`,
   the adapters, nor `live-gate.js` run in CI today. Still part of C-04's
   closure criteria.

### 34.6 C-14 durability and annunciation — preserved, not weakened

`control/merge_invariant.py` was read and **not modified**. Its guarantees are
untouched by this work:

- The classifier stays pure — no network, no state mutation, no ledger write,
  no clock read — so the Supervisor and the Watchdog still reach identical
  verdicts from independently-made observations.
- `accepted_findings` is still not an input; debt expectations still come from
  the ledger.
- Every unprovable path still returns `UNPROVABLE` rather than the convenient
  answer.
- `annunciate`'s fingerprint, its FROZEN-state normalisation, its single
  `HUMAN_INTERVENTION_REQUESTED`, its notification and its
  `STATE_INVARIANT_VIOLATION` are all unchanged.

Nothing was added **after** `attempt_merge` inside the dedicated merge
transaction, which is C-14.1's rule. The only change to `attempt_merge` is one
extra key in an already-existing `MERGE_BLOCKED` event on the *refusal* path,
which runs before anything irreversible and returns immediately.

One behavioural consequence worth stating plainly: the new head check makes
`evaluate_merge` **stricter**, so some merges that would previously have
proceeded now will not. Four existing tests in `test_merge_boundary.py` went
red precisely because their fixtures carried no head at all — a fixture gap,
not a regression — and `TestMergeOriginContract` and the C-14 invariant cases
pass unchanged once the fixtures carry one.

### 34.7 Test counts

| Suite | Before | After | Added |
|---|---|---|---|
| `node --test` (from `apparatus/`) | 127 | **177** | 50 |
| `python3 -m unittest discover -s tests` | 1664 | **1682** | 18 |

New files: `apparatus/pr-evidence/live-gate.js`,
`apparatus/pr-evidence/live-gate.test.js`,
`tests/test_c04_merge_eligibility.py`.

The five required regressions, each with an explicit test:

| Requirement | Test |
|---|---|
| stale output does not authorise merge | `REGRESSION stale output: a verdict produced against an older head denies`, plus a control proving `checkOfflinePolicy` accepts the same package |
| changed head invalidates the approval | `REGRESSION changed head: …` (JS) and `test_a_force_push_with_an_identical_diff_is_refused` (Python — the case the old code got wrong) |
| missing provenance denies | `REGRESSION missing provenance: …` ×3, plus `test_a_record_with_no_reviewed_head_is_refused` |
| mismatched provenance denies | `REGRESSION mismatched provenance: …` ×3 |
| a model's own claimed SHA denies | `REGRESSION self-claimed SHA: …` ×2 |

Existing tests edited, all declared in `.claude/.test-change` first, all fixture
gaps rather than weakened assertions: `test_control_plane.py`,
`test_review_evidence.py`, `test_merge_boundary.py`,
`test_c05_3_evidence_claim.py`. Each lacked `reviewed_head`/`headRefOid`, or
pinned the exact `blank_pr_record` field set. No assertion was removed or
relaxed.

### 34.8 Mutation campaign — 6 broken, 6 detected, 6 restored

Recorded in `experiment/evidence/C-04-merge-composition-mutation.txt`. Each
mutation was applied to a file whose pre-mutation sha256 was recorded, the
suite re-run, the file restored from its pre-mutation bytes, and the
restoration verified by sha256. Both suites were re-run after all restorations
and returned to 177/0 and OK.

| # | Mutation | Result |
|---|---|---|
| M1 | default-deny flipped to default-allow (`decision: ELIGIBLE` unconditionally) | DETECTED — 37 failed |
| M2 | stale verdict accepted (package/trusted-head comparison disabled) | DETECTED — 2 failed |
| M3 | changed head not invalidating (`routing.py` drops the `reviewed_head` comparison) | DETECTED — 4 Python failures |
| M4 | changed head not invalidating (live gate ignores a moved PR head) | DETECTED — 2 failed |
| M5 | an adapter's result ignored (CI adapter's answer replaced with a pass) | DETECTED — 2 failed |
| M6 | the accessibility requirement registry bypassed (`applySeverityPolicy(f, undefined)`) | DETECTED — 2 failed |

M6 needed a strengthened assertion to catch at all, and that is worth recording:
bypassing the registry makes *every* FAILURE-classified accessibility finding
`INVALID_ACCESSIBILITY_EVIDENCE`, which still denies — so a test that only
checked "this denies" would have stayed green while the registry did nothing
and the accessibility box could never go green. The test now asserts the
finding blocks as a **recognised** P1 and that no
`INVALID_ACCESSIBILITY_EVIDENCE` is raised.

### 34.9 Verification performed

- `node --test` from `apparatus/` — **177 passed, 0 failed, exit 0**.
  `experiment/evidence/C-04-merge-composition-node-test.txt`.
- `python3 -m unittest discover -s tests` — **1682 tests, OK**.
  `experiment/evidence/C-04-merge-composition-python-test.txt`. The `[FAIL]
  c16_probe` lines are deliberate probe output inside passing tests, as §28.7
  records.
- Mutation campaign — 6/6 detected, 6/6 restored and verified.
- `python3 scripts/check_no_secrets.py` — clean.
- `git diff --check` and `git diff --cached --check` — clean.
- No network calls, no GitHub writes, no merges, no paid API calls, no
  notifications, no worker launches. Every adapter in every test is a stub.

**Environment note.** `apparatus/node_modules` is absent in a fresh worktree, so
`validate.test.js` (ajv) and `accessibility/run.test.js` (Playwright) could not
resolve their dependencies — the two pre-existing failures §28.7 recorded. No
install was run; instead the main checkout's already-installed packages were
symlinked in per-package, which `node_modules/` already gitignores. That is a
local convenience only, carried in no commit.

### 34.10 What this does NOT establish

Stated plainly, because §34.1 reads stronger than the evidence is.

- **Nothing here has touched live GitHub, live git for a real PR, or a real
  ledger.** Every adapter in every test is a stub. The composition's *logic* is
  proved; the behaviour of the four adapters against reality is exactly as
  unexercised as §28.5 left it.
- **`.runtime/` does not exist** — no run has occurred, it is gitignored, and
  every ledger fixture is synthetic and hand-shaped to what `supervisor.py` is
  read to write. The §34.2 contract is a contract, not an observation.
- **The C-04a realistic multi-cycle FIXTURE preflight has still not been run.**
  That — Builder→PR→Review FAIL→Fix→CI→Accessibility/Security→fresh
  re-review→merge, at least two review cycles, against an isolated fixture
  PR/worktree with real git mechanics — remains C-04a's requirement (1), and it
  is the gap between "the composition layer exists and is tested" and "the live
  merge gate has been demonstrated".
- **C-04 is therefore not closed by this branch.** The composition layer now
  exists, which §28.5 named as the missing piece; the fixture preflight, the CI
  wiring (§34.5 gap 5) and the ledger `head_sha` field (§34.2) are all still
  open. `decision: 'ELIGIBLE'` from a fixture is not launch readiness and this
  branch claims none.
- **The audit was not edited.** `experiment/CONTRADICTION-AUDIT.md` rows C-04
  and C-04a have concurrent edits from other streams; whoever reconciles them
  should cite the three evidence files named in §34.9.

---

## 33. C-18 stage 6 (fixer) — implemented (2026-10-01)

**The fixer only.** C-18 stays **OPEN**: stages 5 and 7 (§14.3) are untouched,
and so is the `merge_invariant.annunciate` residual (§19.7 item 3, §20.6
item 5).

Branch `wip/c18-stage6-fixer`, off `wip/c05-1-persistence` at `8425e32`.
Built against the harness interface as §30.2 specifies it; the stage-4 diff
was not read.

### 33.1 What left T1

| Call | Was | Now |
|---|---|---|
| `prompts.write` | inside T1 | `_execute_fixer_dispatch` |
| `workers.acquire_worktree(..., reuse_if_checked_out=True)` (a `git worktree list`, and `workmux add` on the create path) | inside T1 | `_execute_fixer_dispatch` |
| `workers.allocate_port` (**real 127.0.0.1 bind**) | inside T1 | split: `select_port_candidates` in T1 (no bind), `probe_port` in execute |
| `workers.write_job` | inside T1 | `_execute_fixer_dispatch` |
| `workers.start_job` (process/tmux spawn) | inside T1 | `_execute_fixer_dispatch` |

Commit-side state — `repair_cycles += 1`, `open_finding_ids`, `task["worker"]`,
the `doc["workers"]` record, `retained_worktrees.pop`, the FIX_REQUIRED
transition, the `FIX_DISPATCHED` ledger event and the `notify_out` queue write
— commits afterwards in `_commit_fixer_dispatch`.

Four decisions stay entirely inside T1, because each is a state decision with
no external effect: the paused-provider branch (`WAITING_PROVIDER_RESET`), the
repair-cycle-limit escalation, an empty candidate list from
`select_port_candidates`, and a claim `state.new_dispatch_claim` refuses. None
of the four writes a claim, so there is nothing to recover and nothing to undo.

**`notify_out` is still the stage-2 durable queue write.** It was not turned
back into a synchronous send; it simply moved from T1 to the commit
transaction, so the intent is still committed with the state change that
earned it. Pinned by `test_the_commit_queues_the_notification_and_sends_nothing`
and `test_any_delivery_of_that_intent_happens_with_the_lock_free`.

### 33.2 The reused-worktree hazard, and how it is resolved

This is why the plan of record sequenced the fixer last.

`acquire_worktree(..., reuse_if_checked_out=True)` does not give the fixer a
private checkout. Git allows a branch in exactly one worktree and the Fixer
commits to the Builder's PR branch, so it works in **whichever worktree
already holds that branch** — the Builder's. That worktree is shared mutable
state. Before C-18 the claim authorising its use was taken in the same
transaction as the spawn and could not go stale between them. Splitting plan
from execute opens that window; a `ctl` command, or any other writer, can
replace or clear the fixer claim between T1 committing and the spawn.

The asymmetry that decides the fix: for the **builder**, a commit lost to a
superseded claim leaves an orphan worktree nobody owns. For the **fixer**, it
leaves a **live paid agent writing into a branch checkout a different owner is
now responsible for**, and no later transaction can take that back.

**Resolution.** `_execute_fixer_dispatch` re-verifies the claim against the
**committed** document — read-only, no lock, the same thing `execute_dispatches`
already does for its controls — immediately before anything irreversible, and
abandons the dispatch if it has moved on. The check sits at a deliberate
boundary in the method:

* **Above it, everything is repeatable.** `prompts.write` overwrites its own
  path; acquisition either reuses an existing checkout or runs
  `workmux add --open-if-exists`, which is a no-op on a second run.
* **Below it, nothing is.** The job file is the durable spawn evidence
  `resume_dispatch_claims` reads, and `start_job` launches an agent.

It is deliberately **unconditional** rather than only on the reuse path. Even
a worktree the fixer creates itself is a checkout of the shared PR branch, so
there is no case where skipping it is safe, and one unconditional check is
simpler than a conditional one.

**Fail-closed.** An unreadable or missing document answers False and refuses
to spawn. The costs are not symmetric: refusing costs one retry, spawning
costs a shared branch. Three guards now exist and they are different guards —
lock-held at plan, **lock-free at execute**, lock-held at commit. Only the
middle one can prevent the external effect; the other two only keep state
consistent. M3 below is the measurement of exactly that: with the execute-side
check removed, the state-outcome tests stay green and only the
external-effect tests go red.

**Half-prepared, after a crash.** On the reuse path `acquire_worktree`
mutates the worktree not at all — it looks it up and returns it, pinned by
`test_the_reuse_path_prepares_nothing_inside_the_worktree`. Everything the
fixer does prepare (prompt file, job file) lives outside the worktree and is
rewritten identically on a retry under the same claimed worker name. So the
crash cases reduce to the two §30.2 already handles: no job file → re-planned
under the same identity; job file present → committed from it, never
re-spawned. Both measured end to end.

### 33.3 Files changed

| File | Change |
|---|---|
| `control/supervisor.py` | `DISPATCH_HANDLERS` gains `"fixer"`; `dispatch_fixer` rewritten as a planner; `_dispatch_claim_still_current`, `_execute_fixer_dispatch`, `_commit_fixer_dispatch`, `_fail_fixer_dispatch` added |
| `control/workers.py` | `allocate_port` docstring corrected — no supervisor dispatch path calls it any more |
| `tests/test_c18_stage6_fixer.py` | **NEW** — 48 tests |
| `tests/test_c18_stage3_port_split.py` | boundary marker narrowed again (see §33.5) |
| `tests/test_fixer_dispatch.py` · `tests/test_notification_coverage.py` · `tests/test_intervention_integration.py` · `tests/test_c09_resource_lifecycle.py` | helpers drive the three phases; **no assertion changed** |

`control/supervisor.py`'s `dispatch_reviewer`, `on_dispatch_failure`,
`control/merge_invariant.py` and everything under `apparatus/` were not
touched. No frozen source (`product/`, `tasks/`, `protocol/`,
`config/tasks.json`) was touched. All five test-file changes were declared in
`.claude/.test-change` before the edits.

### 33.4 Verification

```
python3 -m unittest discover -s tests   →  Ran 1712 tests ... OK   (was 1664)
python3 scripts/check_no_secrets.py     →  no secret-shaped material, 194 files
git diff --check                        →  exit 0
Preflight().gate_protocol()             →  ok=True
```

The `[FAIL] c16_probe` stderr lines remain the C-16 deliberate-exception
probe, not failures.

**The transaction-boundary proof is measured, not structural.** A spy records
`lock_is_held(store.lock_path)` — an independent `open()` plus
`flock(LOCK_NB)` against the real lock file — at the moment of every
`prompts.write`, `worktree_for_branch`, `create_worker`, `probe_port`,
`write_job` and `start_job` in a full real tick that routes a REVIEW_FAIL
task to a fixer. A second test leaves `probe_port` unmocked so a **genuine
127.0.0.1 bind** is measured through a spy socket, and asserts the port that
really bound is the port that was committed. `acquire_worktree` is NOT mocked
in these ticks — only its `git worktree list` call is spied — so the real
reuse decision runs.

Three companion tests prove the instrument: the lock probe detects a held
lock; the **socket** spy reports `[('bind', True)]` for a deliberate
in-transaction bind; the **call** spy reports `[('start_job', True)]` for a
deliberate in-transaction call. Neither boundary test can be green because an
instrument is broken.

**Mutations — 5 run, 5 caught.** `control/supervisor.py` restored from a
byte-identical backup after each, SHA-256 verified before and after
(`bd397493…`), the mutation marker grepped for and absent, and the full suite
re-run green afterwards.

| Mutation | Result |
|---|---|
| **M1 the harness is bypassed** — `dispatch_fixer` executes and commits inline inside T1 | **caught, 16 tests.** The boundary spy reports `[('prompts.write', True), ('worktree_for_branch', True), ('probe_port', True), ('write_job', True), ('start_job', True)]` against the expected all-False; the real-bind spy reports `[True]` against `[False]` |
| **M2 the port bind returns to T1** — the composed `allocate_port` restored under the lock | **caught, 7 tests.** The real-bind spy reports `[True, False]` against `[False, False]`; the stage-3 marker fires on both of its new assertions |
| **M3 a reused worktree is accepted without re-verifying the claim** | **caught, 5 tests** — a stolen claim, a cleared claim, an unreadable document and a missing document all spawn; a job file appears for an abandoned dispatch. **Every state-outcome test stayed green**, which is the point: only the external-effect tests can see this |
| **M4 the claim is invisible to `has_worker`** | caught, 5 tests — 2 new, and 3 pre-existing stage-4 tests |
| **M5 `repair_cycles` counted at plan instead of at commit** | caught, 9 tests — 7 new, and 2 pre-existing `test_fixer_dispatch` tests |

Every test uses temporary directories, mocked subprocesses and mocked GitHub.
No worker was launched, no notification was delivered to any destination, no
paid call was made and no rehearsal was run. The only socket operations are
loopback binds on ports the tests first proved were free.

### 33.5 The stage-3 boundary marker, narrowed again

`OnlyTheFixerStillBindsUnderTheLock` asserted that exactly ONE
`workers.allocate_port(doc)` call survived in `supervisor.py` and that it was
the fixer's. Stage 6 migrating the fixer is **exactly the event that marker
existed to make visible**, so it fired as designed. It is now
`NoDispatchPathStillBindsUnderTheLock`: ZERO composed allocator calls survive,
and each of the two dispatch roles selects under the lock in its planning half
and probes outside it in its execute half. It was narrowed rather than deleted
— it still fails the moment a bind is put back by hand, which is what M1 and
M2 above use it for.

### 33.6 What stage 6 requires from the parallel stage-5 work

`on_dispatch_failure` is shared by the reviewer and the fixer, and stage 5 is
generalising it. Stage 6 did not touch it. **The requirement is simply that
its existing signature and behaviour survive:**

```python
def on_dispatch_failure(self, doc, task, pr_number: int, role: str, reason: str) -> None
```

`_fail_fixer_dispatch` calls it positionally as
`self.on_dispatch_failure(doc, task, plan.pr, "fixer", result.reason)`, and
`dispatch_fixer` calls it the same way on its two in-T1 failure branches. It
already takes `role`, so no change is needed for the fixer; if stage 5 adds
parameters, they must be keyword arguments with defaults. The fixer also
relies on it remaining **state-only** — it is now reached from
`confirm_dispatches`' per-result transaction, which must not perform external
work.

`_fail_fixer_dispatch` guards the call with `str(plan.pr) in doc["prs"]`
because `on_dispatch_failure` indexes `doc["prs"][str(pr_number)]`
unconditionally and a KeyError there would abort the commit transaction and
leave the claim behind, producing a retry loop.

### 33.7 What stage 6 does NOT claim, and known limitations

1. **`dispatch_reviewer` is unchanged** by this branch and still makes
   `gh.pr_diff_sha`, `evidence.collect` and `routing.material_diff_hash`
   calls inside T1. Stage 5, in parallel.
2. **`merge_invariant.annunciate` still sends synchronously inside T1.**
   Untouched by instruction.
3. **`workers.allocate_port` now has no production caller at all.** It is
   kept, not deleted: its contract is pinned by the stage-3 tests and it is
   the honest single-shot allocator for anything outside a transaction. Its
   docstring says so. Removing it is a separate decision, not stage 6's.
4. **A job file written immediately before a crash commits a worker that may
   never have started.** `resume_dispatch_claims` treats the job file as
   spawn evidence, so a crash in the microseconds between `write_job` and
   `start_job` commits a fixer record with no process. C-09's lease machinery
   is what surfaces that. This is the harness's existing §30.10 item 5
   property, inherited rather than introduced.
5. **The fixer's orphan-worktree exposure is smaller than the builder's, not
   larger.** On the reuse path no worktree is created, so the §30.10 item 4
   window does not apply; on the create path it applies exactly as it does to
   the builder.
6. **A claim whose observation says "worker live, no job file" defers
   forever.** Inherited from `resume_dispatch_claims` (§30.2); nothing expires
   a dispatch claim yet, so `lease_expires_at` on a claim is currently
   recorded and unread. Not introduced here and not fixed here.
7. **A fail-closed `RuntimeError` in the commit phase would re-run the
   external work on the next tick.** §30.10 item 6, unchanged; the fixer's
   commit phase raises nothing of its own.
8. **`declare_busy` bounds are still stage 7.** Not applied.
9. **No rehearsal, no live launch, no paid call, no notification delivered.**
   This change is not evidence of launch readiness and does not claim any.

Stages 5 and 7 remain outstanding. C-18 remains **OPEN** and still carries
*"REQUIRED BEFORE: unattended multi-cycle rehearsal, the 5-hour unattended
stress test, and T+00."*

---

## 32. C-18 stage 5 (reviewer) on the shared dispatch harness, plus the approved draft-to-ready authority (2026-10-01)

Branch `wip/c18-stage5-reviewer`, off `wip/c05-1-persistence` at `8425e32`
(which already carries the stage-4 harness, `d39ad95`). Stage 5 only. C-18
stays **OPEN**: stage 7 and the `merge_invariant.annunciate` residual are
untouched, and stage 6 is a separate branch.

### 32.1 What left T1

| Call | Was | Now |
|---|---|---|
| `gh.pr_diff_sha` | inside T1 | `observe_review_heads`, before the lock |
| `routing.material_diff_hash` | inside T1 | `observe_review_heads`, before the lock |
| `gh.pr_view` (the re-derivation) | did not exist | `_review_preflight`, after T1 commits |
| `evidence.collect` (two GitHub calls) | inside T1 | `_execute_reviewer_dispatch` |
| `prompts.write` | inside T1 | `_execute_reviewer_dispatch` |
| `workers.acquire_worktree` (workmux/git) | inside T1 | `_execute_reviewer_dispatch` |
| `workers.write_job` | inside T1 | `_execute_reviewer_dispatch` |
| `workers.start_job` (process/tmux spawn) | inside T1 | `_execute_reviewer_dispatch` |
| `gh.mark_ready` (new) | did not exist | `_review_preflight` |

Commit-side state — `review_cycles += 1`, `approval_current = False`,
`review_verdict = None`, `reviewed_head`, `reviewed_diff_hash`,
`task["worker"]`, the `doc["workers"]` record, the retained-worktree pop and
the REVIEW transition — commits afterwards in `_commit_reviewer_dispatch`.
`review_cycles` still counts exactly one per review that actually started.

The reviewer joins the harness exactly as §30.2 specifies: three methods
(`_execute_reviewer_dispatch`, `_commit_reviewer_dispatch`,
`_fail_reviewer_dispatch`) and one `DISPATCH_HANDLERS` entry. Nothing in
`execute_dispatches`, `apply_dispatch_result`, `confirm_dispatches` or
`resume_dispatch_claims` changed.

### 32.2 The head-moved hazard, and how it is resolved

`reviewed_head` used to be written from a head fetched *inside* T1, so it was
necessarily current. Once the head is observed before the lock, the claim can
outlive the commit it names — most sharply on the recovery path, where
`resume_dispatch_claims` re-plans a claim observed a whole tick earlier. A
review cut from a superseded commit would still write `reviewed_head` as
though it had reviewed the current one, and the merge gate would then approve
code no reviewer ever saw.

Two gates, at the two places the plan can go stale:

1. **Execute, before any worker exists.** `_review_preflight` re-resolves the
   pull request with one `gh.pr_view` and requires `headRefOid` to still equal
   `plan.observed_head`. Any disagreement — **including an unresolvable head**
   — invalidates the plan: a head that cannot be *proved* unchanged has not
   been proved unchanged. Nothing is acquired, written or spawned.
2. **Commit, state-only.** `_commit_reviewer_dispatch` additionally requires
   `claim["observed_head"] == plan.observed_head`. This is load-bearing and
   not redundant: a claim re-planned at the *same cycle* has the same worker
   name and the same PR, so `_current_dispatch_claim` alone would accept it,
   and committing the old result against it would bind the new reservation's
   record to the old reservation's SHA.

A failed preflight releases the claim through `_fail_reviewer_dispatch`, so
`route_awaiting_dispatch` re-routes the task on the next tick against a
*freshly observed* head. Nothing retries inside the call. The existing
dispatch-failure limit bounds the re-routing — see §32.8 item 3 for the cost
of that choice.

### 32.3 THE LEDGER EVENT CONTRACT (for the merge-gate composition layer)

The defect: a review was bound to a SHA only by `prs[<n>].reviewed_head` in
**mutable** `state.json`, which every later cycle overwrites. The ledger is
append-only and is the right home. The security worker name is already
SHA-bound (`routing.security_worker_name`); the review path was the
asymmetric side of that defence.

**Consume these two events. The contract is exact.**

| | `REVIEW_DISPATCHED` | `REVIEW_RESULT` |
|---|---|---|
| Emitted by | `Supervisor._commit_reviewer_dispatch` | `Supervisor.on_reviewer_finished` |
| When | the dispatch commits, after the worker has started | the reviewer's verdict is read out of the finished worker |
| `task_id` | the task | the task |
| `pr_id` | the pull request number (int) | the pull request number (int) |
| `agent_id` | the reviewer worker name | the same worker name |
| `role` | `"reviewer"` | `"reviewer"` |
| `outcome` | `"DISPATCHED"` | the verdict (`REVIEW_PASS` / `REVIEW_FAIL` / `REVIEW_UNPARSEABLE`) |
| **`metadata_redacted.head_sha`** | **the 40-char lowercase-hex SHA the review worktree was cut from** | **the SHA the verdict applies to** |
| other `metadata_redacted` | `review_cycle` (int, post-increment) | everything `routing.Review.as_dict()` already produced: `verdict`, `gates`, `summary`, `finding_ids`, `counts` |

Field name is **`head_sha`**, nested under **`metadata_redacted`**, agreed
with the C-04 composition layer that consumes it.

**Mechanism, and why there is exactly one spelling.** `ledger.FIELDS` has no
`head_sha`, and `Ledger.append` merges every unrecognised kwarg into
`metadata_redacted`. So the emitting code passes `head_sha=<sha>` as a bare
kwarg — `ledger.py` is **not changed**, and a top-level `head_sha` is
structurally impossible. The composition layer also reads a top-level spelling
for forward compatibility and treats **disagreement between the two as fatal**,
which is why it is emitted in one place only. Verified end to end through the
real `Ledger` and the file on disk, not only through a mocked `log`
(`test_the_real_ledger_writes_head_sha_into_metadata_redacted_only`).

Note this is the one place that deliberately departs from the control plane's
older `metadata_redacted.head` convention (`REVIEW_EVIDENCE_GATHERED`, the
security events, and this stage's own `REVIEW_PREFLIGHT_DEFERRED`,
`DISPATCH_CLAIMED` and `PR_MARKED_READY_FOR_REVIEW`). Those are diagnostics;
these two are a consumed contract, and the consumer's spelling wins.

**Semantics a consumer may rely on:**

- On `REVIEW_DISPATCHED`, `head_sha` is **always** a full SHA. The dispatch
  cannot commit without one — `new_dispatch_claim` refuses anything that is
  not 40 lowercase hex, and the preflight has proved GitHub still reports that
  exact commit.
- On `REVIEW_RESULT`, `head_sha` is read from the **worker record**
  (`meta["head"]`, written at dispatch and destroyed at reap), falling back to
  `prs[<n>].reviewed_head`. The worker-record binding is preferred precisely
  because the per-PR field is mutable and a later cycle overwrites it.
- **`head_sha` may be `null` on `REVIEW_RESULT`**, for a worker record minted
  before this binding existed. The key is **always present**; a null means
  *this verdict is not attributable to a commit*. It must be read as an
  unbound review, never as "not checked" and never as a pass. A missing key
  would be indistinguishable from an old ledger line, which is why null is
  emitted explicitly.
- **`agent_id` names the worker on both events**, so a SHA-bound result can be
  checked by reviewer-identity verification. A head with no worker beside it
  would prove nothing about who produced the verdict.
- Pairing: `(task_id, pr_id, agent_id)` identifies one review cycle.
  `agent_id` is `<task-id-lowercased>-review-<cycle>` and is unique per cycle.
- These events are **evidence of what was reviewed, not of approval.**
  Approval lives in `REVIEW_PASS_RECORDED` and in `prs[<n>].approval_current`.

This is what closes the §28.3 defect: an older cycle's REVIEW_PASS could not
previously be proved not to have been re-presented for a newer commit, because
the only SHA binding was a mutable field the newer cycle had already
overwritten.

Pinned by `TheLedgerBindsAReviewToItsCommit` (9 tests).

### 32.4 The approved authority amendment — draft to ready for review

Recorded verbatim, as approved by the operator:

> "The Supervisor may mark a draft PR for a governed task ready for review
> before independent review dispatch. This action grants no approval or merge
> eligibility."

Implemented in `Supervisor._review_preflight`, in the **execute** phase. All
four operator conditions:

1. **Re-verify first.** Association and head are re-derived from GitHub in one
   `gh.pr_view` — pull request found, still `OPEN`, `headRefName` still equal
   to the claim's branch, `headRefOid` still equal to `observed_head`. The
   plan supplies only what the fresh answer is checked *against*; nothing is
   marked ready off bookkeeping built earlier in the tick. The state-side half
   of the same check (`task["pr"]`, `task["branch"]`) is re-made at commit.
2. **Defer on failure, with a finite diagnostic.** Every disagreement returns
   one code from `supervisor.REVIEW_PREFLIGHT_CODES` —
   `PR_NOT_FOUND`, `PR_NOT_OPEN`, `PR_ASSOCIATION_MISMATCH`, `HEAD_MOVED`,
   `READY_FOR_REVIEW_REFUSED` — annunciated as
   `REVIEW_PREFLIGHT_DEFERRED` with the code as `outcome`, and carried into
   the durable `DISPATCH_FAILED` record as `reason`. **Never exception prose**,
   which is the C-16 rule. The review is not dispatched anyway and nothing
   retries inside the call.
3. **It grants nothing, structurally.** `_review_preflight` runs in the
   execute phase, which holds no state document and no lock, so there is no
   `approval_current`, `review_verdict` or merge record in scope for it to
   touch. Its only channel back into the control plane is `DispatchResult`,
   whose fields are `(plan, ok, worktree, port, reason)` — no field an
   approval could ride on. And the commit half of a *successful* dispatch sets
   `approval_current = False` and `review_verdict = None` unconditionally, so
   marking ready can only ever move a pull request **away** from merge
   eligibility. All three are asserted, not merely stated.
4. **Success is annunciated** as `PR_MARKED_READY_FOR_REVIEW`
   (`outcome="READY"`, `metadata_redacted.head`, and
   `metadata_redacted.grants = "NO_APPROVAL_NO_MERGE_ELIGIBILITY"`).

**This path is EXCEPTIONAL, not routine.** `prompts/builder.md` opens pull
requests with a bare `gh pr create` and `--draft` appears nowhere in the
repository, so a governed task's pull request is ready by construction. A
draft here means a human or an external tool made one. That claim is pinned by
a test that greps the two files that could introduce it, so it cannot
silently stop being true.

**This is the first fixture in the repository that sets `isDraft: True`.**
Every pre-existing fixture hard-codes it False, so the draft path had never
executed once before this stage.

### 32.5 `on_dispatch_failure`, generalised for both roles

Signature **unchanged** — `(doc, task, pr_number, role, reason)`, positional,
state-only, as the stage-6 fixer requires. One behaviour added: the pull
request record is now looked up with `.get` and a missing record is **refused
and logged** (`DISPATCH_FAILURE_REFUSED`, `outcome="PR_RECORD_GONE"`) instead
of raising `KeyError`.

This is not cosmetic. Before C-18 the only caller had just read that record
inside the same transaction, so it was certain to exist. A harness role now
calls this from the commit transaction that runs *after* T1, and in that
window an externally merged pull request can complete its task and take the
record with it. The `KeyError` would abort the commit transaction — discarding
the claim release beside it and stranding the dispatch in a retry loop. With
the record present, every line is exactly what it was; all pre-existing
callers and assertions are unchanged.

Stage 6 reported hitting this and guarding its own call site. **The guard is
now inside the function and no caller needs to repeat it.** Pinned by
`test_a_pull_request_record_gone_is_refused_not_raised` (both roles) and
`test_the_commit_half_also_refuses_a_vanished_record`.

### 32.6 The pre-lock observation, and what it costs

`observe_review_heads(snapshot, open_prs) -> {pr_number: ReviewObservation}`
is narrowed the way the security heads are. A pull request is asked about only
when its task is in `ROUTING_STATES`, the record exists and is unmerged, the
pull request is open, it is not already a merge candidate, it is not routed to
a fixer by a `REVIEW_FAIL` verdict with findings, and `has_worker` sees no
reviewer or fixer (worker **or** claim). A snapshot whose worker map cannot be
read yields no observations at all, so every pull request defers rather than
being dispatched from a document that could not be understood.

`head` and `diff_hash` are taken in the same breath, so the committed
`reviewed_diff_hash` always describes the committed `reviewed_head`.

**Costs, stated rather than hidden:**

- A tick with a pull request genuinely awaiting review now makes
  `gh.pr_diff_sha` + `material_diff_hash` **before** the lock and one
  `gh.pr_view` **after** it. Previously it made all three inside T1. The call
  count per successful dispatch is the same; the lock-held time is not.
- A pull request that is eligible but whose dispatch is held (paused review
  provider, frozen run) is re-observed every tick. Those are `gh` reads, not
  paid calls, and the narrowing above keeps a quiet tick at zero.
- `observation` is a **required** parameter of `dispatch_reviewer`, so a
  caller that forgets it defers loudly (`REVIEW_DISPATCH_DEFERRED`,
  `outcome="OBSERVATION_MISSING"`) rather than silently reviewing nothing.
  `route_prs` and `route_awaiting_dispatch` take it as an optional keyword
  with a `None` default, which is the plumbing only.
- **`on_fixer_finished` now defers by one tick.** It calls
  `dispatch_reviewer(..., None)` from inside T1, where no observation exists —
  at observation time the fixer was still live. The task is left in REVIEW
  with its verdict cleared and no worker, which is exactly what
  `route_awaiting_dispatch` picks up next tick, against a head observed
  *after* the fix landed rather than before it. One tick of latency, bought
  for a fresher head.

### 32.7 Files changed, and verification

| File | Change |
|---|---|
| `control/supervisor.py` | `REVIEW_PREFLIGHT_CODES`; `ReviewObservation`; `observe_review_heads`; `dispatch_reviewer` rewritten as a planner; `_review_preflight`, `_review_deferred`, `_execute_reviewer_dispatch`, `_commit_reviewer_dispatch`, `_fail_reviewer_dispatch`; `DISPATCH_HANDLERS` reviewer entry; `on_dispatch_failure` generalised; `route_prs`/`route_awaiting_dispatch` plumbing; `on_fixer_finished` deferral; `on_reviewer_finished` SHA binding; `tick` wiring |
| `control/gh.py` | `mark_ready` (new) |
| `tests/test_c18_stage5_reviewer.py` | **NEW** — 64 tests |
| `tests/test_dispatch_invariant.py` | three tests re-pointed at plan → execute → commit; **no assertion changed** |
| `tests/test_c09_resource_lifecycle.py` | one test re-pointed, same; **no assertion changed** |
| `tests/test_c18_stage4_dispatch_harness.py` | the no-handler marker moved from `"reviewer"` (which stage 5 claims) to `"observer"`; **no assertion changed** |

`control/merge_invariant.py`, `control/state.py`, `dispatch_fixer`,
`tests/test_c18_stage3_port_split.py` and everything under `apparatus/` were
not touched. No frozen source (`product/`, `tasks/`, `protocol/`,
`config/tasks.json`) was touched. All three test-file changes were declared in
`.claude/.test-change` before the edits.

```
python3 -m unittest discover -s tests   ->  Ran 1728 tests ... OK   (was 1664)
python3 scripts/check_no_secrets.py     ->  no secret-shaped material, 194 files
git diff --check                        ->  exit 0
Preflight(cfg).gate_protocol()          ->  ok=True, missing: []
```

The `[FAIL] c16_probe` stderr lines remain the C-16 deliberate-exception
probe, not failures.

**The transaction-boundary proof is measured, not structural.** A spy records
`lock_is_held(store.lock_path)` — an independent `open()` plus
`flock(LOCK_NB)` against the real lock file — at the moment of every
`gh.pr_diff_sha`, `material_diff_hash`, `gh.pr_view`, `gh.mark_ready`,
`evidence.collect`, `prompts.write`, `acquire_worktree`, `write_job` and
`start_job` in a full real tick. Two companion tests prove the probe detects a
held lock and that the same spy reports `True` for a deliberate
in-transaction call, so neither guard can be green because the instrument is
broken.

**Mutations — 7 run, 7 caught.** `control/supervisor.py` restored
byte-identical after every one, SHA-256 verified against a pre-mutation
baseline, and the full suite re-run green. M1–M3 and M5 were run against
baseline `1f1a9417…`; M4, M6 and M7 against `e699d437…` after the ledger field
name was agreed with the C-04 composition layer.

| Mutation | Result |
|---|---|
| **M1 the harness is bypassed** — `dispatch_reviewer` executes and commits inline inside T1 | **caught, 16 tests.** The boundary spy reports `[('gh.pr_diff_sha', False), ('material_diff_hash', False), ('gh.pr_view', True), ('evidence.collect', True), ('prompts.write', True), ('acquire_worktree', True), ('write_job', True), ('start_job', True)]` against the expected all-False |
| **M2 an external call returns to T1** — the head and diff-hash fetches restored inside `dispatch_reviewer` | **caught, 11 tests.** The spy reports a second `gh.pr_diff_sha` and `material_diff_hash` pair, both `True` |
| **M3 a moved head does NOT invalidate the plan** | caught, 7 tests, including a draft marked ready on a superseded head |
| **M4 the SHA binding dropped from both ledger events** | caught, 5 tests |
| **M7 the SHA emitted under the WRONG spelling** — `metadata_redacted.head` instead of `metadata_redacted.head_sha` | caught, 5 tests. The consumer treats a spelling disagreement as fatal, so a silently renamed field had to be a red suite |
| **M5 the claim is invisible to `has_worker`** | caught, 5 tests — a second reviewer and a second fixer are both dispatched |
| **M6 the draft is marked ready WITHOUT re-verifying the head** — ordering inverted, everything else intact | **caught, 2 tests, both by EXTERNAL-EFFECT assertions.** No state outcome can see this: a draft wrongly marked ready leaves state identical to one correctly deferred. Only the absence of `gh.mark_ready` in the recorded call list proves it |

M6 is deliberately the answer to stage 6's finding that state-outcome
assertions cannot catch a lost external guard. M1, M2 and M3 are likewise
caught by call-site assertions, not only by state.

Every test uses temporary directories, mocked processes and mocked GitHub. No
worker was launched, no notification sent, no paid call made and no network
reached. Unlike stage 4 there is no socket operation anywhere: the reviewer
takes no port.

### 32.8 What stage 5 does NOT claim, and known limitations

1. **No rehearsal, no live launch, no GitHub call was actually made.** Every
   `gh` entry point is mocked, including `gh.mark_ready`, which has therefore
   **never run against a real pull request**. The command
   (`gh pr ready <n> --repo <repo>`) is unexercised outside tests. This change
   is not evidence of launch readiness and does not claim any.
2. **`dispatch_fixer` is unchanged** and still calls the composed
   `workers.allocate_port(doc)` under the lock. Stage 6, separate branch.
3. **A legitimately moving head consumes dispatch-failure budget.** Every
   preflight deferral increments `prs[<n>].dispatch_failures`, which escalates
   to HUMAN_REQUIRED at `max_repair_cycles`. A branch being pushed to
   repeatedly will therefore eventually raise a human intervention rather than
   waiting it out. That is a deliberate choice — something pushing during
   review is worth a human looking — but it is a behaviour change from "the
   head is whatever T1 saw", and a separate counter was not invented for it.
4. **The one-tick deferral after a fixer finishes** (§32.6) is new latency on
   the repair loop. It was chosen over fetching a head inside T1.
5. **`REVIEW_RESULT.head` can be null** for a worker record minted before this
   stage. A consumer must handle that explicitly; see §32.3.
6. **`merge_invariant.annunciate` still sends synchronously inside T1.**
   Untouched by instruction.
7. **The amendment's authority is narrow.** It permits exactly one action on
   exactly one kind of pull request. Nothing here authorises the Supervisor to
   approve, to merge, or to change a review verdict, and §32.4 condition 3
   makes that structural rather than conventional.
8. **`declare_busy` bounds are still stage 7.** Not applied.
9. **Nothing here touches the merge gate.** `routing.evaluate_merge`, the
   head-SHA-before-diff-hash invalidation and the 14 finite `MERGE_*`
   condition codes all landed elsewhere and were deliberately not duplicated.
   `REVIEW_PREFLIGHT_CODES` is a separate, reviewer-dispatch vocabulary and
   shares no identifier with them.
10. **The C-04 composition layer has not been run against this branch.** The
    event contract in §32.3 was agreed in writing and verified from this side
    end to end through the real `Ledger`, but the two halves have not been
    executed together.

C-18 remains **OPEN** and still carries *"REQUIRED BEFORE: unattended
multi-cycle rehearsal, the 5-hour unattended stress test, and T+00."*
T+00 remains **NOT_STARTED**.

## 37. Integration session — D1–D4, C-18a, and the production gate (2026-10-01)

Integration owner session on `wip/c05-1-persistence`. Two commits, both
pushed: `8099db6` (D1–D4 and the C-18a amendment) and `4d0651e` (the C-04a
fixture driven through the production composition).

### 37.1 Recovery, verified against the checkout rather than the report

| Claim in the handover | Verified | How |
|---|---|---|
| `6f43985` on `wip/c05-1-persistence`, pushed, clean, synchronized | **TRUE** | `git status` clean; `rev-list --left-right --count origin/...` = `0 0` |
| PRs #1–#9 merged into that branch | **TRUE** | `gh pr list --state all` — nine PRs, all `MERGED`; `git branch -r --no-merged HEAD` is empty |
| 1,852 Python tests | **TRUE** | re-run at HEAD: `Ran 1852 tests ... OK` |
| 197 apparatus tests | **TRUE** | re-run at HEAD: `pass 197, fail 0` |
| Secret scan clean | **TRUE** | 212 tracked files, clean; `git diff --check` clean |

No unfinished git operation, no stash, nine agent worktrees all parked on
their merged branches. Nothing newer than the checkpoint existed, so
nothing had to be preserved.

**One correction to the recovery brief itself.** Handover §16 ("Current
status and next action") predates §§28–36 and is stale — it still reports
C-19 as unimplemented and names stage 4 as the next action. The live
status is `LAUNCH-CHECKLIST.md` plus the audit rows, which is what this
session worked from.

### 37.2 D1 — the malformed security-claim crash, repaired in its owning code

§36.5 D1 asked whether this is repaired in C-05.3a or folded into
C-05.3b. The operator directed the former: repair it in its owning code.

**Two crash sites, not the one reported.** D1 named `verdict`.
`claim_state` reaches a frozenset membership test on the same function and
fails identically. `in` hashes its left operand, so a `dict` or a `list` —
both of which durable JSON carries — raised `TypeError` out of a function
whose entire contract is to return a finite diagnostic. A corrupt PR
record crashed the tick instead of failing closed.

**Why one unhashable type was not enough to find it.** A `set` value
*survives* both sites, because CPython retries a failed set lookup as a
frozenset. Probing with `set()` alone would have reported the function
healthy.

**Policy is unchanged.** Every member of both frozensets is a string, so a
non-string could never have been a member. The guard decides whether the
refusal is RETURNED or RAISED; it does not change what is refused, or the
diagnostic it is refused with.

Tests: three new cases, including a standing property that no value in any
of the nine claim fields can raise out of the validator — so a membership
test added later without a guard fails even if nobody extends the two
specific cases. Mutations: each guard removed independently (4 failures +
4 errors; 4 errors), restored, restoration checksum-verified.

**Consequence elsewhere, recorded rather than left to be discovered.**
`review_gate_fires` carried a comment saying the crash was C-05.3a's to
fix and that its own check ordering kept the crash unreachable. That
comment was true and is now false; it has been rewritten. The ordering is
kept, for defence in depth, not because it is load-bearing. One test in
`test_c05_3b_review_gate.py` *pinned the defect* with
`assertRaises(TypeError)`; it now asserts the stronger thing it was
standing in for — a finite refusal is returned — and still asserts the
gate's own behaviour, which is unchanged.

### 37.3 D2/D3 — the requirement registry, reachable from Python

§36.3 found two of §31.10's three assumptions false: the registry landed
as `ACC-DOD-*`/`ACC-COG-*` SCREAMING_SNAKE identifiers, not kebab-case,
and as a **JavaScript module with no Python counterpart**. Until this
session there was nothing for a Python caller to pass as
`severity.apply_severity_policy`'s `known_requirement_ids`, so every
accessibility FAILURE rated `INVALID` whatever it cited — fail-closed, but
a gate that could never open.

**What was rejected, and why.** Deriving the seventeen identifiers a
second time in Python — parsing `product/ACCESSIBILITY.md` directly —
would create exactly the competing source of truth the operator excluded:
two parsers that can disagree, with no rule saying which wins.

**What was built.** The canonical JS registry is serialised to a generated
`apparatus/accessibility/requirement-registry.json`, which
`control/accessibility_registry.py` reads **once at import** — so no
filesystem read happens at call time and the read stays outside T1, as
§31.10 requires. Every link in the chain is machine-checked:

```
product/ACCESSIBILITY.md          imported, frozen, never edited
  -> requirement-registry.js        requirement-registry.test.js      (existing)
    -> requirement-registry.json    requirement-registry-json.test.js (new, byte-identical)
      -> control/accessibility_registry.py   reads it; derives nothing
```

The Python side re-derives the JSON's provenance from the frozen document
**independently of both**, so the control plane does not take the
artefact's word for where it came from.

`control/severity.py` is untouched. It is a frozen-hash input
(`manifest.SEVERITY_POLICY_FILES`) and editing it would move a frozen
hash; this only supplies the argument it already takes. **No severity
policy changed.**

D3 is answered as a fact rather than a decision: all seventeen
`ACC-DOD-*`/`ACC-COG-*` identifiers are asserted to rate through the real
policy to P1, and the pre-C-02 kebab-case form still fails closed. G6 —
what each automated check *proves* — remains a policy question and is
unanswered (§37.8).

A missing or malformed artefact **raises at import** rather than yielding
an empty registry. An empty registry would be fail-closed at the gate and
therefore safe, but silently so, and a permanently-shut gate nobody is
told about is indistinguishable from a working one until a task stalls for
a reason no diagnostic names.

Mutations, both caught on both the Node and Python sides: a hand-renamed
identifier (stale committed JSON), and an invented eighteenth requirement.

### 37.4 D4 — claim-validator registration, made structural

§36.5 D4 recorded an instruction to a future implementer: whoever builds
the accessibility claims must register their validators in the same
commit. An instruction nobody checks is not a control, and the weakness it
warns about is invisible at the call site — a leg whose claim exists but
whose validator is unregistered is checked structurally only, and
`review_gate_fires` still returns a confident `True`.

`review_gate_fires` now iterates `REVIEW_GATE_CLAIM_VALIDATORS` instead of
naming the one validator that exists; `UNVALIDATED_REVIEW_GATE_LEGS`
declares the two legs that have none; and `REVIEW_GATE_CLAIM_BUILDERS`
drives a tripwire test that fails the moment an accessibility claim
*builder* appears without its validator. Behaviour today is identical, and
a test proves the registry is actually read by registering a
always-refusing validator and asserting the gate shuts.

### 37.5 C-18a — the five-hour rehearsal amendment, and what it obliges

Recorded at audit row **C-18a**. The exact amended sentence is the
"REQUIRED BEFORE" clause in C-18's status cell, left **unedited in place**
with its original rationale so the history still reads; C-18a records what
was waived and what was not.

**Verified rather than assumed:** `protocol/RUN-002-PROTOCOL-v2.0.md`
states no hour-bounded endurance or stress requirement anywhere. Its only
hour figures are the 24-hour run itself and "The 24-hour clock never
pauses". The five-hour duration originates in the audit's C-18 row and in
`REHEARSAL-PLAN.md`. **Nothing frozen is rewritten by this amendment.**

Only the duration is waived. Multi-cycle verification through the
production apparatus, the launch gates, independent review, the
accessibility and security checks, exact-SHA evidence, budget enforcement,
recovery checks and stop controls all stand.

**The obligation attached in its place was partly an engineering gap.**
Run 002 is now itself the endurance experiment, so "how long did it run
and why did it stop" is a result. Against the operator's five
requirements:

| Must be reconstructible | Status | Evidence |
|---|---|---|
| The first failure | **Already covered** | the append-only ledger is timestamped and carries `activity_class: FAILED_WORK`; the earliest such event is the first failure |
| Elapsed runtime | **Already covered** | `doc["started_at"]` is the T+00 anchor; ledger timestamps measure from it |
| Task / PR state | **Already covered** | `.runtime/state.json` — `tasks`, `prs`, `workers`, `counters` |
| Recovery attempts | **Already covered** | `DISPATCH_RECOVERED`, `RECOVERED_FROM_JOB_FILE` (Supervisor); `SUPERVISOR_RESTARTED` and `SUPERVISOR_RESTART_FAILED` (Watchdog) — a restart that worked and one that did not are distinct events, so attempts can be counted honestly |
| **The stopping reason** | **WAS MISSING — now closed** | `SUPERVISOR_STOPPED` carried no reason and the signal number was discarded (`def stop(_signum, _frame)`), so a governed stop and a mystery exit were indistinguishable |

| Must be distinguishable | Status |
|---|---|
| Autonomous recovery vs human intervention | **Already covered** — `counters.recoveries` and the recovery events are written only by the Supervisor and Watchdog; `counters.human_interventions` and the whole `control/intervention.py` lifecycle (request / acknowledge / resolve, with human minutes) are the human side, and nothing autonomous increments them |
| An early stop from a completed 24-hour run | **Covered by the stop reason plus the clock anchor** |
| The run clock must not be reset or extended | **Already enforced** — `cli.cmd_start` refuses a second start: *"already started at …; the protocol is frozen"* |

`_stop_reason()` now emits a fixed finite vocabulary — `SIGTERM`,
`SIGINT`, `LOOP_EXITED_WITHOUT_SIGNAL`, `UNKNOWN_SIGNAL_<n>` — per C-16, no
runtime prose. The first signal wins, so a second `SIGTERM` during the
closing tick cannot rewrite why the stop began.

**What no event this process writes could ever cover, stated plainly:**
SIGKILL, power loss, or an exception escaping `run()` leave **no**
`SUPERVISOR_STOPPED` at all. The absence is itself the evidence — a
`SUPERVISOR_STARTED` with no matching `SUPERVISOR_STOPPED` means the
process died rather than stopped — and the Watchdog's restart events are
what separate an autonomous recovery from a run that simply ended there.

`_stop_reason` reads its field through `getattr` deliberately. It is the
last thing the process writes; a reason-reporter that raised would delete
the very event that tells a SIGKILL apart from a clean shutdown. That was
not hypothetical — the first version asserted the attribute and broke
three existing C-16 tests that construct a Supervisor with `__init__`
bypassed.

### 37.6 C-04a — the fixture now drives the PRODUCTION gate, and that caught a defect

§35.8 item 1 named the largest gap: the harness decided through a stub, so
what it proved was the SCENARIO, not the SYSTEM.

`apparatus/fixture-preflight/production-gate.js` now drives
`apparatus/pr-evidence/live-gate.js` with **all four C-04 adapters real** —
`git-head` against a real git repository and real worktree, `ci-result`,
`task-record` against this repository's committed `config/tasks.json`, and
`reviewer-identity` — and only the **external services** injected: the
in-memory forge, the ledger, the durable PR record, and GitHub's
`mergeStateStatus`. No network, no `gh`, no real pull request, no merge on
a real forge, no paid call, no worker, no notification.

`scenario.js` takes the decider as an input, so both deciders run the
**same scenario definition**. Two copies would drift, and then "the
production gate passes the scenario" would quietly stop meaning the same
scenario.

**THE DEFECT, which would have stalled every product PR at T+00.**
`live-gate.js` required a `sha` on **every** security check.
`PR-EVIDENCE-V2.schema.json`'s `securityCheck` requires
`result`/`artifact_reference`/`sha` only under `if relevant === true`, and
Protocol v2 defines twelve security surfaces, so any realistic package
marks most of them irrelevant — each one then denying
`EVIDENCE_SHA_UNBOUND` / `COULD_NOT_VERIFY`. **The live merge gate could
never have returned `ELIGIBLE` for a real pull request.** Fail-closed, so
nothing unsafe could merge; but a gate that can never open is an outage
waiting for T+00, and it would have been found there, on every product PR
at once.

Repaired to follow the schema's own conditional. A `sha` that **is**
present is still compared to the trusted head whether required or not:
un-required is not un-checked.

Two things worth keeping about how it was found. The pre-existing
`live-gate.test.js` pinned **neither** behaviour — which is how the defect
survived a reviewed, mutation-tested branch. And the first version of the
new tests let a mutation through: setting every `sha` to not-required
SURVIVED, because nothing covered an *absent* sha where the schema
requires one. Three cases were added for that; the mutation is now caught.

Driven through production the whole lifecycle completes: cycle 1 refused
on the real offline policy; the cycle-1 package refused at the new head on
`EVIDENCE_SHA_STALE` and, independently, `REVIEW_PROVENANCE_SHA_MISMATCH`;
cycle 2 `ELIGIBLE` and merged at the trusted head; the draft lifecycle
marked ready and merged with no `actor: human` event; dependents unblocked
from the real task graph; a stale approval refused.

C-20(b)'s branch-protection condition is **exercised, not hidden**:
`mergeStateStatus` is a stated input with no concealed `CLEAN` default,
and a test drives `BLOCKED` and asserts the gate names it as a
repository-governance condition rather than an evidence defect.

### 37.7 The autonomous product-PR lifecycle, step by step

The operator's requirement: create → mark ready if drafted → required
independent review and governed checks/evidence → merge when eligible →
verify actual merged SHA → complete the task and unblock dependencies.

| # | Step | Governing requirement | Implementation | Verification | Remaining gap |
|---|---|---|---|---|---|
| 1 | **Create PR** | Protocol v2 "PR contract"; BOOTSTRAP 4 | `gh.create_pr`; builder dispatch on the C-18 stage-4 harness | C-18 stage-4 tests; fixture scenarios | None known |
| 2 | **Mark ready if drafted** | C-20a **A** (approved amendment) | `gh.mark_ready` (`gh.py:124`) called at `supervisor.py:1694`; re-verifies task/PR association and current head, defers with a finite diagnostic on failure | C-18 stage-5 tests; fixture asserts ready fires **only** when draft is the sole obstacle, and that `blockedOnlyByDraft` says so structurally | None known. Marking ready grants no approval and no merge eligibility — the gate still denies everything else |
| 3 | **Required evidence: CI** | Protocol v2 "PR contract"; C-04 | `adapters/ci-result.js`, allow-list of exactly `success`, `REQUIRED_CHECK_MISSING` for a check GitHub never ran | 57 adapter tests; fixture proves the adapter beats the package's own `ci.status` claim | Never run against real GitHub |
| 4 | **Required evidence: security** | C-05.3a, C-05b | `routing.parse_security`, the claim lifecycle, SHA-bound worker names | C-05.3a suites; D1 crash now repaired | None blocking |
| 5 | **Required evidence: accessibility** | Protocol v2 "Accessibility gate"; C-02; C-05.3b | Contract, composite gate and automated normalisation **built**; the Python registry reader **now exists** | 58 + 20 tests | **BLOCKING — not wired.** `review_gate_fires` has **no caller in `control/`**, and `ROUTING_STATES = ("PR_OPEN", "REVIEW", "FIX_REQUIRED")` omits `WAITING_EVIDENCE`, so a task that reaches it is never routed again. This is C-20(a), still open |
| 6 | **Independent review** | Protocol v2 "Agents cannot satisfy independent boxes by self-attestation" | `adapters/reviewer-identity.js`, seven fail-closed checks; review ledger events SHA-bound by C-18 stage 5 | Fixture refuses a verdict the ledger does not carry, and a review bound to a superseded commit | Never run against real GitHub |
| 7 | **Merge when eligible** | C-04; C-14.1 | `live-gate.js` (composition) and `routing.evaluate_merge` (Python) | 218 apparatus tests incl. the production lifecycle | **Two merge gates in two languages.** The Supervisor calls `routing.evaluate_merge`; **nothing in `control/` calls `live-gate.js`**. Only the Python one is reachable from a tick. Plus C-20(b): GitHub requires an approving review nothing produces |
| 8 | **Verify the actual merged SHA** | C-10.2; C-14.1 | Re-reads the PR after merge and stores `mergeCommit.oid` as `merge_sha_observed` (`supervisor.py:3514-3520`) | C-14 tests; fixture confirms the merge by reading the forge back rather than trusting the merge call | None known |
| 9 | **Complete the task, unblock dependents** | Protocol v2 task graph | Completion only after a verified merge; dependents from `config/tasks.json` | Fixture unblocks TASK-002/003/004 from the **real** task graph | None known |

**The lifecycle is blocked at step 5, and only at step 5, by engineering.**
Step 7 carries a second blocker (C-20(b)) that is governance, not code.

### 37.8 Engineering versus policy — the separation

**Engineering, and no longer blocked by any decision.** Closed this
session: D1, D2, D3, D4, the stopping-reason evidence gap, and the
`live-gate.js` security-surface defect.

**Engineering, unblocked, not yet done** — these need no governance answer
and are the next implementable work:

1. **Wire `review_gate_fires` into the Supervisor** and add
   `WAITING_EVIDENCE` to `ROUTING_STATES`. This is C-20(a) and the single
   thing standing between the apparatus and an unattended lifecycle. It
   needs G1/G3/G4/G7 **only for the qualitative reviewer half**; the
   automated half and the gate predicate are built and proved.
2. **Join the two merge gates.** `live-gate.js` is unreachable from a
   tick. Either the Supervisor calls it, or `routing.evaluate_merge`
   gains the same adapter-backed checks, or one is explicitly declared
   the offline half of the other. Leaving two gates in two languages with
   different reason vocabularies is the defect.
3. **Drive the fixture's CI and reviewer adapters from the C-02 registry
   with a FAILURE-classified finding** citing a real `ACC-*` identifier
   plus its negative (§35.8 item 3). The registry reader now exists, so
   this is unblocked.
4. **F1** — fold `gate_evidence._adjudicate`'s four inline literals into
   `accessibility_contract`. Behaviour-neutral; the agreement test
   prevents drift meanwhile.

**Genuine policy decisions, with briefs below:** G1, G3, G4, G6, G7, C-18
stage 7's bounds, the `merge_invariant.annunciate` residual, and C-20a(C).

**No longer decisions:** D5–D10 were prerequisites of the five-hour
rehearsal and are moot as rehearsal blockers under C-18a. D7 — previously
"blocks everything" — is resolved as a matter of fact: both missing
`SPEC_FILES` entries now exist, so `protocol_present` passes. The
annotated table is in `REHEARSAL-PLAN.md` §9. The one fragment that
survives, re-homed, is D6's *evidence retention* and
*autonomous-vs-human* requirement, now binding on the real run under
C-18a and discharged in §37.5.

### 37.9 Decision briefs

Each gives the governing text, concrete options, a recommendation, and the
smallest amendment that would settle it. **None is implemented.**

---

**G1 — the qualitative accessibility reviewer's worker timeout.**
*Governing text (§31.14):* "`timeouts` has no `accessibility` key and
`_lease_expires("accessibility")` raises `KeyError`. Must not collide with
the `_seconds` keys C-05a deliberately kept non-role-shaped." **Blocking.**

C-05a already governs `accessibility_soft_seconds` = 120 and
`accessibility_hard_seconds` = 300 — but those bound the **browser run**,
not a reviewer **worker lease**. The reviewer is an agent, and its peers
are `security` = 1800, `reviewer` = 1800, `fixer` = 2400.

- **(a) `timeouts.accessibility = 1800`**, matching `security` and
  `reviewer`. — *Recommended.* It is the same kind of thing as its two
  nearest neighbours, and a number equal to an existing governed number
  is the least novel choice available.
- (b) A different value — requires a reason this role differs.
- (c) Reuse `accessibility_hard_seconds` (300) — **rejected**: it would
  silently bound an agent by a browser budget, and §31.14 explicitly
  warns against colliding with the `_seconds` keys.

*Smallest amendment:* add one key `"accessibility": 1800` to
`config/experiment.json`'s `timeouts` object. No code change; the lease
helper already reads the table.

---

**G3 — the product server lifecycle.**
*Governing text (§31.14):* "What command starts the product server;
whether dependency install sits inside the 120 s readiness budget; what
happens on the first PR, before any product exists. `product/` is
specifications only." **Blocking** for the automated accessibility half.

§31.17 already notes this "may not be answerable before a product exists
at all". That is the key asymmetry: it is a question about an artefact the
run is supposed to *produce*.

- **(a) Defer the automated half until a product exists, and make the
  pre-product state explicit** — an accessibility run with no server to
  drive is `NOT_APPLICABLE` with a stated reason, not a silent pass and
  not a stall. — *Recommended.* It is the only option that does not
  require inventing a command for software that does not exist, and the
  schema already models explicit applicability
  (`accessibility.applicable` plus `not_applicable_reason`, which
  `validate.js` enforces).
- (b) Govern a start command now against `product/ARCHITECTURE.md` —
  guesses at a build that has not been made.
- (c) Require the builder to declare its own start command in the
  evidence package — lets the thing being judged choose how it is judged.

*Smallest amendment:* a governed sentence stating that before the first
product server exists, the automated accessibility leg is
`NOT_APPLICABLE` with a fixed reason, and that it becomes required from
the first PR that ships a runnable server. Install-time budget stays open
until there is something to install.

---

**G4 — how long `WAITING_EVIDENCE` may hold before `HUMAN_REQUIRED`.**
*Governing text (§31.14):* "How long `WAITING_EVIDENCE` may hold on
unobtainable evidence before `HUMAN_REQUIRED`." **Blocking before ship,
not before start** (§36.2: "how long a hold may last is G4" — the gate
predicate itself does not need it).

This one is sharper after C-18a: with Run 002 as the endurance
experiment, a task silently holding forever is no longer merely untidy —
it consumes run time that is now the measured result.

- **(a) A governed wall-clock bound, then `HUMAN_REQUIRED`** with a
  finite condition code. — *Recommended*, with the bound set to the
  longest single evidence timeout plus one retry — on today's numbers
  `1800 × 2 = 3600 s`. Derived from existing governed numbers rather
  than chosen.
- (b) Bound by attempts rather than time — attempts may never be made if
  dispatch itself is what is stuck, which is the case the bound exists
  for.
- (c) No bound — the present behaviour, and the C-20(a) failure mode.

*Smallest amendment:* one governed key, e.g.
`timeouts.waiting_evidence_hold = 3600`, plus a named condition code on
the `HUMAN_REQUIRED` transition. The escalation path and the intervention
lifecycle already exist.

---

**G6 — the nine `check_id` → requirement map.** *Partial blocker:*
"Blocks the automated-FAIL path only; the PASS path and the gate work
without it."

D3 is settled: the map must be written in landed `ACC-DOD-*`/`ACC-COG-*`
identifiers, and a Python reader now exists. What remains is genuinely
policy — *what each automated check proves*.

Eight of the nine map cleanly onto registry entries
(`RESPONSIVE_375PX` → `ACC-DOD-RESPONSIVE_LAYOUT`, `TOUCH_TARGETS` →
`ACC-DOD-TOUCH_TARGETS`, `KEYBOARD_OPERATION` →
`ACC-DOD-KEYBOARD_OPERATION`, `FOCUS_ORDER_VISIBLE_NO_TRAPS` →
`ACC-DOD-VISIBLE_FOCUS`, `LABELS_AND_TEXT_ERRORS` →
`ACC-DOD-MEANINGFUL_LABELS`, `REDUCED_MOTION_…` →
`ACC-DOD-REDUCED_MOTION`, and so on). **`AXE_SCAN` is the real question**:
one axe run covers many requirements at once.

- **(a) Map `AXE_SCAN` coarsely to one requirement.** — *Recommended*,
  and §36.5 D3 notes it is "the only option available without widening
  C-02's charter": the registry derives strictly from
  `product/ACCESSIBILITY.md` and has no room for axe violation ids.
- (b) Make axe violation ids registry identifiers in their own right —
  widens C-02's charter and breaks the frozen-document derivation.

*Smallest amendment:* a governed nine-row table in
`protocol/SEVERITY-POLICY.md` or a new governed file, written in the
landed identifier form. Until it exists the map stays empty and every
automated FAIL yields an uncited finding that `severity.py` rates
`INVALID` with `merge_blocked: True` — the designed inert-but-safe state,
pinned by a test.

---

**G7 — `max_accessibility_auto` and `max_accessibility_review`.**
*Governing text (§31.14):* "Budget and rate-limit judgement, as
`max_security` was." **Blocking.**

- **(a) Both `1`**, matching `max_security: 1`, `max_fixers: 1`,
  `max_reviewers: 1`. — *Recommended.* Every non-builder role in the
  governed concurrency table is already 1; the automated half also drives
  a real browser, which is the heaviest single resource the run uses, and
  C-08c inotify headroom is already failing on this host.
- (b) Higher for the automated half — buys throughput against the one
  resource the host is already short of.

*Smallest amendment:* two keys in `config/experiment.json`'s
`concurrency` object, both `1`.

---

**C-18 stage 7 — `declare_busy` bounds per call site.**
*Governing text (§14.5):* "whether `declare_busy` gets a governed bound
per call site … All three are governance calls." C-18's row:
"Where an external call must remain slow, bound it with `declare_busy` so
a waiting Supervisor is not read as a dead one."

- **(a) One governed bound for all call sites**, set to the longest
  external operation plus margin. — *Recommended.* Simplest to reason
  about, and a single number cannot drift between sites. Per-site bounds
  are the kind of configuration that earns its complexity only once a
  site is demonstrably different.
- (b) Per-call-site bounds — more precise, more surface, and §14.5 left
  precisely this undecided.
- (c) Derive the bound from each site's existing timeout — attractive,
  but several sites have no timeout of their own today.

*Smallest amendment:* one governed key plus a statement that every
`declare_busy` call site uses it until a site is shown to need its own.
**Note:** six of seven stages are done, so this is the last numbered
stage, and C-18's "REQUIRED BEFORE … T+00" clause still stands after
C-18a — the amendment waived only the five-hour clause.

---

**The `merge_invariant.annunciate` residual.**
*Governing text (§19.7 item 3):* "It calls `notifier.send` directly rather
than through `notify_out`, and `Supervisor.invariant_violated` reaches it
from `route_prs`. … moving it would change C-14.2's annunciation
evidence. It is the one remaining synchronous outbound send under T1 and
needs its own authorised change."

The tension is real: the whole point of C-14.2's annunciation evidence is
that the notification is *proof* the invariant violation was announced,
and routing it through a durable queue makes the announcement eventual.

- **(a) Move it behind the stage-2 durable intent queue and restate
  C-14.2's evidence as the durable NOTIFICATION_QUEUED commit rather than
  the send.** — *Recommended.* Stage 2 already established that the
  business event and `NOTIFICATION_QUEUED` are both durable at least one
  commit before any send, which is a **stronger** guarantee than a
  synchronous send that can fail silently. It also removes the last
  synchronous outbound call from T1, which is what C-18 exists for.
- (b) Leave it synchronous and accept one blocking send under T1 —
  keeps C-14.2's evidence untouched, leaves C-18 permanently incomplete.
- (c) Make it synchronous but hard-bounded — a smaller stall, same shape.

*Smallest amendment:* a governed sentence stating that C-14.2's
annunciation evidence is satisfied by the durable queued intent rather
than by a completed send, plus the authorisation to move the call.

---

### 37.10 C-20a(C) — the GitHub approving-review / status-check mechanism

**Read-only investigation only.** No protection was changed, no credential
created, no review or status published. The operator's hold stands.

**The repository and branch.** `serina-mcfall/wellbeing-run-002`, branch
`main` (`config/experiment.json` → `github.repo`, `github.main_branch`).

**Protection as it actually is today**, read via
`gh api repos/serina-mcfall/wellbeing-run-002/branches/main/protection`:

| Setting | Current value |
|---|---|
| `required_status_checks.strict` | `true` |
| `required_status_checks.contexts` | `["ci"]` |
| `required_status_checks.checks` | `[{context: "ci", app_id: 15368}]` — **pinned to one app** |
| `required_pull_request_reviews.required_approving_review_count` | `1` |
| `required_pull_request_reviews.dismiss_stale_reviews` | **`false`** |
| `required_pull_request_reviews.require_last_push_approval` | **`false`** |
| `required_pull_request_reviews.require_code_owner_reviews` | `false` |
| `enforce_admins` | **`false`** |
| `allow_force_pushes` / `allow_deletions` | `false` / `false` |
| `required_linear_history` | `false` |
| `required_conversation_resolution` | `false` |

**The finding that settles the approving-review half.** All nine PRs were
authored by `serina-mcfall`, and the authenticated identity is
`serina-mcfall` (`gh api user`) — **measured, not assumed**. GitHub does
not permit a user to approve their own pull request; that is documented
platform behaviour and was **deliberately not tested here**, because
testing it would mean attempting to publish a real review, which the
operator has not authorised. So the Supervisor posting an approving review
under this identity cannot satisfy `required_approving_review_count: 1` —
not as a matter of policy, but because the author and the approver are the
same account. The operator's instinct in holding C was correct, and the
option is not merely unapproved: on this repository, with this identity,
it is unavailable.

If it were ever pursued it would require a **second, distinct GitHub
identity** that does not author the PRs — which is a new credential, and
therefore outside what is authorised.

**Three further facts the investigation surfaced, which matter
independently of C:**

1. **`dismiss_stale_reviews: false` and `require_last_push_approval:
   false`** mean GitHub will **not** invalidate an approval when a new
   commit lands. Decision E — a new head invalidates the prior review — is
   therefore enforced **only** by the control plane's own SHA binding
   (`routing.evaluate_merge`'s head comparison and `live-gate.js`'s ledger
   provenance). Nothing on the GitHub side backs it up.
2. **`enforce_admins: false`** means an admin can merge past every rule
   above. An autonomous run must never rely on protection as its last
   line of defence, because for an admin identity there is no line.
3. **The `ci` context is pinned to `app_id: 15368`** (GitHub Actions).
   That pin is the only thing that makes `ci` unforgeable by a token with
   write access.

**Could builders publish the same passing status? — Yes, as things
stand.** `control/gh.py` shells out to the `gh` CLI with **no explicit
token**, inheriting the ambient host authentication, and
`control/worker_entry.py:113` copies `os.environ` into every worker
process. A builder worker therefore runs with **the same GitHub identity
as the Supervisor**. Any status the Supervisor can POST to
`/repos/{repo}/statuses/{sha}`, a builder can POST — including a passing
one on its own commit. A new required context added **without** an
`app_id` pin would be self-attestable by the thing being judged, which is
precisely what Protocol v2's PR contract forbids: *"Agents cannot satisfy
independent boxes by self-attestation."*

**What a sound mechanism would therefore require** — stated as a proposal,
not a request to proceed:

| Element | Requirement |
|---|---|
| Context name | A new context, e.g. `run-002/independent-review`, distinct from `ci` |
| Before | `contexts: ["ci"]`, `required_approving_review_count: 1` |
| After | `contexts: ["ci", "run-002/independent-review"]` with the new context **pinned to a dedicated app_id**; `required_approving_review_count` **reduced to 0** only if the new context genuinely replaces the human-approval requirement — which is itself a governance decision, not an implementation detail |
| Evidence provenance | The status may be published **only** from the SHA-bound `REVIEW_RESULT` ledger event plus `live-gate.js`'s verdict — never from the submitted evidence package |
| Credential boundary | **The publishing credential must not be reachable by any worker.** Today it is, via inherited `os.environ`. This requires a dedicated GitHub App installation token held by the Supervisor process alone, and `worker_entry.py` scrubbing it from the child environment. Without both, the mechanism is self-attestation with extra steps |
| Expected-head protection | The status must be posted against the exact 40-hex SHA and the merge must use `--match-head-commit` (or the API's `expected_head_sha`), so a push between publish and merge cannot inherit the status |
| Stale-approval protection | Set `dismiss_stale_reviews: true` and/or `require_last_push_approval: true`, so decision E is enforced by GitHub as well as by the control plane |
| Verification plan | On a **throwaway repository**, not `wellbeing-run-002`: prove a worker-held credential **cannot** post the context; prove a push after publishing invalidates the merge; prove the gate still denies when the ledger lacks a SHA-bound `REVIEW_RESULT`; prove an admin merge is still possible and record that as an accepted residual risk or set `enforce_admins: true` |

**Recommendation.** Do not pursue the approving-review option at all — it
is unavailable for the reasons above. The status-check mechanism is
viable **only** with the dedicated-app credential boundary and the
`worker_entry.py` environment scrub; without those two it is strictly
worse than the current state, because it would convert a visible block
into an invisible self-attestation. **No change is requested here.**

### 37.11 Remaining launch prerequisites after the C-18a amendment

**Implementation blockers**

1. **C-20(a) — the accessibility dispatch wiring.** The one thing stopping
   an unattended lifecycle. Needs G1, G3, G4, G7.
2. **C-18 stage 7** (`declare_busy` bounds) — governance-gated.
3. **The `merge_invariant.annunciate` residual** — governance-gated.
4. **The two unjoined merge gates** — engineering, unblocked.

**Governance**

5. G1, G3, G4, G6, G7; stage 7's bound; the annunciate authorisation;
   C-20a(C) (recommended: do not pursue).

**Environment and gates**

6. **`required_secrets` WILL FAIL** — `~/.config/run-002/secrets.env` does
   not exist and 7 of 8 required names are absent. Checked **by name
   only**; no value was read. Human action, and it blocks
   `langfuse_otel_trace`, `supabase_health` and `discord_delivery`.
7. **`host_headroom` FAILS** — inotify instances 77/128 = 60% against a
   governed 50% ceiling. RepoQL owns 67 of 77; two `rql serve` daemons
   indexing unrelated projects hold 41. Stopping those two alone would
   reach 36/128 = 28%. **Human decision; not actioned — the brief forbids
   unrelated host-process shutdowns.** The ceiling must not be raised.
8. **No `preflight.json` exists**, so `ctl start` would refuse on all 24
   gates as never-run. The PASS rows in the checklist are documented
   manual observations, not durable machine records.
9. **`clean_baseline`** — its recorded cause is gone and the tree is
   clean, but the gate has not been executed, and an unexecuted gate is
   not a PASS.
10. **C-04a** remains PARTIAL-with-better-evidence, **not** GREEN: nothing
    has run against real GitHub.

**No longer a prerequisite:** the five-hour endurance rehearsal (C-18a).

### 37.12 What this section does not claim

- It does not claim C-05.3b is implemented. It is still not wired; a task
  reaching `WAITING_EVIDENCE` still holds forever.
- It does not claim C-04 or C-04a is GREEN. The fixture exercises the
  production composition; it is not the integrated system, and no run has
  touched real GitHub.
- It does not claim the run is fully reconstructible. §37.5 states exactly
  which cases no event this process writes could cover.
- It answers none of G1–G10. Nine briefs are offered; none is a decision.
- No gate was run, no threshold changed, no branch protection touched, no
  credential created, no paid call made, no product worker launched, and
  no merge to `main`.
- **T+00 remains NOT_STARTED.**

## 38. The runtime is connected to the evidence gate (2026-10-01)

Operator instruction: *"connecting the runtime to the verified merge
policy. The fixture must exercise the same eligibility path the Supervisor
actually uses; testing an otherwise uncalled gate is not sufficient. Keep
missing or invalid evidence blocking merges."*

### 38.1 The hole, reproduced before it was closed

`routing.evaluate_merge` is the gate the Supervisor calls
(`supervisor.py:3728`). It consulted **no evidence of any kind**. A record
carrying `REVIEW_PASS`, a current approval, a matching head and diff, green
CI and a `CLEAN` merge state — but **no security evidence and neither
accessibility leg** — returned:

```
evaluate_merge  ALLOWED = True | all merge gates satisfied | MERGE_OK
review_gate_fires       = False   <- the uncalled gate
```

Reproduced against the real code, not inferred.

**It was reachable in ordinary routing.** `route_awaiting_dispatch`
dispatches a reviewer whenever the task is in `ROUTING_STATES`, which
includes `PR_OPEN` — its own comment says *"A first dispatch from PR_OPEN
is ordinary routing, not a recovery"* — and `dispatch_reviewer` transitions
the task straight to `REVIEW`. `WAITING_EVIDENCE` is entered only from
`EVIDENCE_STATES = ("PR_OPEN", "WAITING_EVIDENCE")`, and `REVIEW` is not
one of them. So a task reviewed before its evidence was claimed never
enters the evidence state at all, and nothing downstream noticed.

This is C-20(a)'s deadlock seen from the other end. The deadlock was the
*visible* symptom; this was the silent one.

### 38.2 What changed

`evaluate_merge` now calls `review_gate_fires(record, observed_head)` and
denies with a new finite condition `MERGE_EVIDENCE_INCOMPLETE`
(`"EVIDENCE_INCOMPLETE"`) when any required class is not a completed pass
at the head GitHub reports.

**Placed inside `evaluate_merge`, not at the Supervisor call site.** A
check at the call site protects that call site; a check in the gate
protects every caller, present and future. The cost is that
`evaluate_merge` is no longer "the six conditions from Protocol v1.0" —
its docstring is updated and `test_review_evidence`'s
`test_the_six_merge_conditions_are_untouched` was deliberately tripped and
updated, which is the guard doing its job.

**Absence denies.** `review_gate_fires` returns False for absent, stale,
`PLANNED`, incomplete, failed and malformed evidence alike. It is not a
verdict: False means "not every required class is a completed pass at this
head", which is the correct reading for a merge gate. Missing evidence is
not permission.

### 38.3 Eighteen tests were passing for the wrong reason

Five modules built records that asserted a merge **passes** while carrying
no evidence at all. They were not wrong about what they were testing; they
were wrong about what a mergeable PR is. `tests/mergeable_evidence.py`
defines the three complete legs once — the security leg through the real
`routing.security_claim` so it genuinely satisfies
`security_claim_is_valid` — and the five fixtures use it.

One builder was missed by the first pass and caught by the suite rather
than by reading: `test_merge_boundary`'s ordering fixture ends differently
from its siblings, so a `replace_all` did not reach it.

### 38.4 The runtime-side fixture

`tests/test_c04a_runtime_merge_lifecycle.py` — 17 tests driving the C-04a
multi-cycle shape through `evaluate_merge` itself: the no-evidence
regression and its control, each leg independently load-bearing, an
incomplete and a failed leg, exact-SHA invalidation and regeneration across
two heads, two review cycles, and the orderings that must not collapse
(evidence does not substitute for review, review does not substitute for
evidence, a draft still blocks, RED still wins, failing CI still blocks).

One test asserts the connection directly: when `review_gate_fires` refuses,
`evaluate_merge` must refuse. If that ever stops holding, the runtime has
silently stopped requiring evidence again.

**The relationship between the two fixtures, stated plainly.**
`apparatus/fixture-preflight/production-lifecycle.test.js` exercises
`live-gate.js`, which the runtime does **not** call. It proves the
composition layer and the four C-04 adapters against real git and injected
external services. `tests/test_c04a_runtime_merge_lifecycle.py` exercises
the gate the Supervisor **does** call. Both are needed and neither
substitutes for the other; the JS one is not evidence about the runtime.

### 38.5 Verification

- **1,893 → 1,910 Python tests**, exit 0. **218 apparatus tests**, exit 0.
- Mutation: the evidence gate deleted from `evaluate_merge` → **11 red**.
  Restored, restoration verified by `diff`.
- **An inert mutation, recorded rather than glossed.** Gating on
  `record["reviewed_head"]` instead of `observed_head` survives the whole
  suite — because the head check immediately above already proves the two
  equal, so the substitution is semantically identical today. The comment
  originally implied the variable choice was what protected the gate; it
  does not, the earlier check does, and the comment now says so.

### 38.6 What is still NOT connected — the severity floor

`control/severity.py::apply_severity_policy` **has no caller in
`control/`**. Verified, not assumed: `git grep` finds only comments
referring to it. The deterministic P0/P1 accessibility floor, the
requirement-registry validity check, and the "INVALID evidence always
blocks merge" rule therefore run **only** in
`apparatus/severity/severity-floor.js` via `validate.js` — which the
runtime never invokes.

What the runtime does instead, at `supervisor.py:3049`:

```python
if f.get("severity") in routing.BLOCKING_SEVERITIES
```

It reads the severity field **as submitted** and splits blocking from
debt. The thing being judged states its own severity, and nothing
re-derives it.

**Why this was not fixed in the same change.** `apply_severity_policy`
expects an accessibility finding — `jev_severity`, `classification`,
`unmet_requirement` / `non_failure_rationale`. Code-review findings carry
none of those and would rate `INVALID`, which would make every ordinary
code finding merge-blocking. Applying it correctly requires knowing which
findings are accessibility findings, and that discrimination is part of
C-05.3b's accessibility **ingest**, which is not built. So this is
genuinely **blocked on C-05.3b**, not merely undone.

Until then the honest statement is: the runtime requires every evidence
class to have passed, and does **not** independently validate the severity
of the findings inside them.

### 38.7 What this section does not claim

- It does not claim C-05.3b is wired. `WAITING_EVIDENCE` is still absent
  from `ROUTING_STATES`, so the evidence route still has no exit; what
  changed is that a PR which skipped that route can no longer merge.
- It does not claim the runtime applies the severity floor. §38.6.
- It does not claim `live-gate.js` is in the runtime path. It is not.
- No gate was run, no threshold changed, no live action taken. T+00
  remains **NOT_STARTED**.

## 39. G1 and G7 applied; claim validators and the severity floor integrated (2026-10-01)

Recorded at audit row **C-05c**.

### 39.1 G1 and G7, applied at their real readers

| Decision | Value | Reader |
|---|---|---|
| **G1** | `timeouts.accessibility = 1800` | `Supervisor._lease_expires(role)` indexes `timeouts[role]`; the key had to be role-shaped or the `KeyError` would have stayed exactly where it was |
| **G7** | `max_accessibility_auto = 1`, `max_accessibility_review = 1` | typed fields on `ExperimentConfig`, parsed **without a default** so a missing governed bound fails at load |

G1 is stated as separate from the browser deadlines and a test asserts the
separation: `accessibility` (1800) must not equal
`accessibility_soft_seconds` (120) or `accessibility_hard_seconds` (300).
An agent lease and a browser budget answer different questions.

`config/experiment.json` is **not** one of the five frozen content hashes.
Captured before and after: all four are byte-identical.

### 39.2 Claim-validator integration — D4 fully closed

`accessibility_auto_claim_is_valid` (§31.5's seven keys) and
`accessibility_review_claim_is_valid` (§31.7's nine) are built and
**registered** in `REVIEW_GATE_CLAIM_VALIDATORS`.
`UNVALIDATED_REVIEW_GATE_LEGS` is now empty, and the registration tripwire
still holds the invariant for any leg added later.

Both follow `security_claim_is_valid`'s discipline, including §36.5 D1's
lesson applied **up front**: every membership test is guarded, and a test
drives every hostile value through every field of both claims asserting
that a diagnostic is *returned*, never raised.

Properties worth naming:

- The two verdict families stay disjoint — an `ACCESSIBILITY_PASS` can
  never satisfy the automated leg, and an `ACCESSIBILITY_AUTO_PASS` can
  never satisfy the qualitative one.
- `ACCESSIBILITY_UNPARSEABLE` is not an accepted claim verdict, for the
  same reason `SECURITY_UNPARSEABLE` is not: unreadable output is an
  attempt that produced no verdict, and recording it *as* a verdict would
  turn "we could not read the result" into a judgement about the product.
- The qualitative worker name is SHA-bound (`-a11y-<sha>-NNNN`) and a
  pre-SHA or prefix form is refused — the ordinal restarts at 1 for a new
  head, so an unbound name repeats across a head change.
- A qualitative-only failure reason is refused on the automated leg, so a
  machine run can never be recorded as having failed a judgement it never
  made.

### 39.3 Severity-floor integration — the call that did not exist

`control/severity.py::apply_severity_policy` had **no caller in
`control/`**. `routing.adjudicate_accessibility_finding` is now that
caller, supplying the canonical C-02 registry through
`control/accessibility_registry.py`.
`routing.accessibility_findings_block_merge` is the default-deny set
form: a malformed findings array blocks, an INVALID finding blocks, P0/P1
block, P2/P3 do not independently block, and an empty list does not —
because whether a scan *happened* is the verdict's job, which
`review_gate_fires` already enforces.

**No severity policy changed.** `control/severity.py` is a frozen-hash
input and is untouched; this supplies the argument it always took.

**G6's consequence, pinned.** `CHECK_REQUIREMENT` is still empty, so every
automated FAIL yields an uncited FAILURE, which the policy rates `INVALID`
with `merge_blocked: True`. A test walks all nine automated checks and
asserts each one blocks. A second test is the **G6 tripwire**: any mapping
that *is* added must name an identifier the frozen registry actually
contains — G6 may reuse existing identifiers, it may not mint new ones.

### 39.4 The Supervisor orchestration, not the gate in isolation

`tests/test_c04a_supervisor_merge_evidence.py` reuses
`test_merge_boundary.MergeBoundaryCase` — a real `Supervisor`, a real
`Store`, a genuine state file, a real `tick()` — and asks whether a pull
request with missing or stale evidence actually fails to reach `gh.merge`.
The evidence is the **call records**, not the decision object: a gate that
refuses while the orchestration merges anyway would be a passing unit test
and a merged commit.

Covered: no evidence at all; each leg missing independently; evidence
bound to a superseded commit; an incomplete claim; a claim its own
validator refuses; that `MERGE_BLOCKED` durably carries
`EVIDENCE_INCOMPLETE`; and that no merge means no completion, no
`merge_sha_observed` and no `MERGED` state.

### 39.5 Verification

- **1,920 → 1,975 Python tests**, **218 apparatus**, both exit 0.
- Mutations, each proved red then restored and verified by `diff`:
  the evidence gate removed from `evaluate_merge` → **9 red** at the
  Supervisor level; the accessibility validators unregistered → **3 red**;
  the registry no longer supplied to the severity policy → **19 red**.
- Frozen content hashes captured before and after the config change:
  byte-identical.

### 39.6 What is still NOT enforced

The severity floor now has a caller, but **that caller has no call site in
a live path yet**: accessibility findings only exist once C-05.3b's
*ingest* mints them, and the ingest is the wiring that G3 and G2 gate. So
the adjudicator is ready and proved; it is not yet reached by a running
tick.

Stated plainly so it cannot be read as full enforcement: the runtime now
requires every evidence **class** to be a completed pass at the observed
head, and can correctly rate an accessibility **finding** — but nothing
produces those findings yet.

T+00 remains **NOT_STARTED**.

## 40. C-05.3b completed: both halves, wired to the tick (2026-10-01)

Recorded at audit rows **C-09a** (amended twice), **C-20** (a, partially
closed) and **C-02a** (new, blocking).

### 40.1 What exists now

| Piece | Where | Reached from a tick? |
|---|---|---|
| The `WAITING_EVIDENCE` exit | `Supervisor.advance_if_evidence_complete` | **Yes** — from `ingest_security` and from `route_evidence` |
| Automated attempt lifecycle | `control/accessibility_evidence.py` | **Yes** — `execute_accessibility` |
| Automated claim / commit | `plan_accessibility_auto`, `ingest_accessibility_auto`, `commit_accessibility` | **Yes** |
| Qualitative parse / adjudicate | `routing.parse_accessibility`, `accessibility_is_consistent` | via ingest |
| Qualitative claim / commit | `plan_accessibility_review`, `ingest_accessibility_review` | helpers — no tick dispatch yet |
| Claim validators (both legs) | `routing`, registered in `REVIEW_GATE_CLAIM_VALIDATORS` | **Yes** |
| Severity floor | `routing.adjudicate_accessibility_finding` | **Yes** — via both ingests |
| G6 maps | `routing` | **Yes** |

### 40.2 Three defects found while building, each fixed rather than worked around

**The port-release asymmetry.** An attempt that never STARTED a server
never bound the port, so its claim must be released unconditionally.
Applying the listener-gone rule there would strand the port permanently
whenever something *else* happened to be listening on it. Once a server
really ran, the rule applies in full.

**The wrong dispatch control.** The first version gated new automated
attempts on `_security_dispatch_block`, which also requires the security
*provider* to be usable. The automated half consults no provider; that
would have held accessibility evidence hostage to an unrelated outage. It
now has its own block checking `frozen_at` and `stopping`.

**The `getattr` default.** `route_evidence` reads the services factory
through `getattr` because the safe default is *do not plan*. A Supervisor
built without `__init__` — which several suites do deliberately — must not
start claiming ports it cannot execute against.

### 40.3 What blocks, and what is merely inert

**BLOCKING, governance: C-02a.** `prompts/accessibility.md` is frozen and
its required-output example cites `visible-focus-indicator`, a pre-C-02
kebab-case identifier the registry does not contain. A reviewer following
its own prompt is refused `CLASSIFICATION_INVALID`. The parser does **not**
translate it: inventing that mapping would defeat the registry check,
which is what C-02 exists for. Fail-closed and safe; the qualitative FAIL
path cannot produce a usable finding until it is resolved. Three options
and a recommendation are in the audit row.

**INERT, not blocked: the services factory.** Nothing injects one, because
no product exists to build before the first product PR. The automated half
is wired and refuses to claim rather than minting claims it could never
execute. This resolves itself when a product exists; it needs no decision.

**NOT BUILT: the qualitative tick dispatch.** `plan_accessibility_review`
and `ingest_accessibility_review` are proved through the Supervisor but
are not yet called from `route_evidence`, because spawning that worker
belongs on the C-18 stage-4 dispatch harness and spending a provider call
is governed separately from building the path.

### 40.4 Verification

- **2,075 → 2,171 Python tests**, **218 apparatus**, both exit 0.
- Demonstrated through the real Supervisor with injected services: failed
  evidence blocking, repair then fresh evidence at a new head succeeding,
  changed-head invalidation, restart recovery including a claim in flight
  not being re-planned, server cleanup with the port returned, the path to
  REVIEW, and the tick path end to end.
- **Fifteen mutations** across this work, each proved red, restored, and
  the restoration verified by `diff`. Two attempted mutations were
  DISCARDED rather than reported because they produced `IndentationError`
  and therefore tested nothing.

### 40.5 What this section does not claim

- It does not claim a real accessibility scan has ever run. No product
  exists; every service is injected.
- It does not claim the qualitative half is dispatched by a tick.
- It does not claim C-02a is resolved.
- T+00 remains **NOT_STARTED**.

## 41. SESSION HANDOVER — integration session close (2026-10-01)

Read this section first. It supersedes §16, which is stale.

### 41.1 State at handover — verified, not reported from memory

| | |
|---|---|
| Branch | `wip/c05-1-persistence` |
| HEAD | **`4ae1488`** |
| Pushed | **Yes** — `0 0` against `origin/wip/c05-1-persistence` |
| Working tree | **Clean.** `git status --porcelain --untracked-files=all` is empty |
| Unfinished edits | **None.** Every change this session is committed and pushed |
| Running processes | **None.** No background task, test run or agent is still executing; nothing will modify the checkout after this handover |
| Subagents | **None were ever dispatched.** A parallel-agent instruction arrived and was superseded before any agent was created, so there is no agent output to review and no agent worktree to reconcile |
| Worktrees | The nine pre-existing `agent-*` worktrees from the earlier PR streams, unchanged. **None created this session, none removed** |
| Verification at HEAD | **2,171 Python tests OK · 218 apparatus tests OK**, both exit 0. Secret scan clean. `git diff --check` clean |
| T+00 | **NOT_STARTED** (`started_at: None`) |

**Baseline comparison.** The session began at `6f43985` with 1,852 Python
and 197 apparatus tests, all re-verified before any change was made.

### 41.2 Twelve commits, all pushed

```
4ae1488  Reconcile the C-05.3b checklist row and record section 40
780be6d  C-05.3b: the qualitative half, and the tick that drives both
cb85145  C-05.3b: the automated accessibility half, built and wired
eb5349a  C-20(a): WAITING_EVIDENCE finally has a governed exit
7681550  G6: explicit reviewed check and axe-rule requirement mappings
6554784  Apply G3's attempt budget and G4's cumulative evidence wait
03dd57b  G2: claim-owned product-server ports, through C-09
da5f7d0  Apply G1 and G7; integrate claim validators and the severity floor
e905800  Make the Supervisor's own merge gate require the evidence classes
3748a5a  Record the integration session: section 37 and checklist reconciliation
4d0651e  Drive the C-04a fixture through the PRODUCTION gate, and fix what that caught
8099db6  Close D1-D4, and record the five-hour rehearsal amendment as C-18a
```

### 41.3 Approved decisions, all applied — PRESERVE THESE

| Decision | Applied as | Where |
|---|---|---|
| **G1** | `timeouts.accessibility = 1800`, separate from the browser deadlines | `config/experiment.json`; audit **C-05c** |
| **G7** | `max_accessibility_auto = 1`, `max_accessibility_review = 1`, typed and required at load | `config/experiment.json`, `control/config.py`; **C-05c** |
| **G2** | Claim-owned ports through C-09; ownership ends at RELEASE, never at expiry | `workers.claimed_product_server_ports`, `reconcile`; audit **C-09a** |
| **G9** | Claim in T1 / execute outside / commit with re-verification | `control/accessibility_evidence.py`, `Supervisor.execute_accessibility` |
| **G3 phase limits** | install+build **600**, readiness **120**, scan **120/300**, teardown **60**, attempt total **1080** (= the sum) | `config/experiment.json`; **C-05c** |
| **G3 direction** | Clean isolated build at the trusted head; no artefact reuse; **no NOT_APPLICABLE route** | `accessibility_evidence`; **C-05c** |
| **G4** | Cumulative evidence wait **18000 s**, an experiment limit; HUMAN_REQUIRED at exhaustion | `control/state.py`, `Supervisor._escalate_evidence_wait` |
| **G6** | Explicit reviewed maps; no wildcards; unmapped stays blocking | `control/routing.py` |
| **C-18a** | Five-hour rehearsal waived; Run 002 is the endurance experiment | audit **C-18a** |
| **A, D, E** (C-20a) | Draft→ready authority, distinct diagnostics, new-SHA invalidation | unchanged from earlier sessions |
| **C** (C-20a) | NOT approved; investigation only | §37.10, below |

### 41.4 Completed implementation, with what verifies it

- **Merge gate requires evidence.** `evaluate_merge` consulted none; a PR
  with no security and neither accessibility leg returned "all merge gates
  satisfied". Reproduced, fixed, and verified through the **real
  Supervisor** reaching `gh.merge` — not through the gate in isolation.
- **`WAITING_EVIDENCE` has a governed exit** (C-20(a)), reached from the
  security ingest *and* from `route_evidence`.
- **Both C-05.3b halves built and wired**, with the tick path
  (`route_evidence` → `execute_accessibility` → `commit_accessibility`)
  proved end to end with injected services.
- **Severity floor and both claim validators integrated** and reached from
  live call sites.
- **C-04a fixture drives the production gate**, which caught a defect that
  made the live gate unopenable for any real PR.
- Roughly **forty mutations** across the session, each proved red, restored,
  and the restoration verified by `diff` or checksum. **Two attempted
  mutations were discarded rather than reported** because they produced
  `IndentationError` and therefore tested nothing.

### 41.5 Remaining C-05.3b work

1. **The qualitative tick dispatch.** `plan_accessibility_review` and
   `ingest_accessibility_review` are built and proved through the
   Supervisor, but `route_evidence` does not call them: spawning that
   worker belongs on the C-18 stage-4 harness and spends a provider call,
   which is governed separately.
2. **A services factory.** Nothing injects one, so the automated half is
   wired and inert. **This needs no decision** — it resolves when a product
   exists to build. Planning deliberately refuses while the factory is
   absent, rather than minting claims that could never be executed.
3. **Connected lifecycle verification is partial.** Failed evidence,
   repair, fresh evidence, changed-head invalidation, restart recovery,
   cleanup and the path to REVIEW are demonstrated. **Merge confirmation
   and dependency unblocking are demonstrated only in the JavaScript
   fixture**, not yet in one Python test that runs the whole chain.

### 41.6 C-18: stage 7 and the notification amendment — PROPOSED, NOT IMPLEMENTED

**Stage 7, the `declare_busy` bound.** `supervisor.py:51` already states
the rule — *"Derived from each operation's own timeout, never open-ended"* —
and `observer_bound` implements it as `own timeout + BUSY_MARGIN_SECONDS`
(60). Applying that existing rule makes every value exact:

| Call site | Own timeout | Bound |
|---|---|---|
| builder execute | 3600 | **3660** |
| fixer execute | 2400 | **2460** |
| reviewer execute | 1800 | **1860** |
| security execute | 1800 | **1860** |
| accessibility auto execute | 1080 | **1140** |
| accessibility review execute | 1800 | **1860** |
| notification drain | 10/tick | **70** |

An earlier recommendation of *one* bound for all sites is **withdrawn**:
the operations differ by 3.4×, and a single value would have to be 3660,
letting a wedged reviewer look healthy for an hour against a 120 s
staleness threshold.

**The notification amendment**, exact proposed wording:

> C-14.2's annunciation evidence is satisfied by the durable
> `NOTIFICATION_QUEUED` commit, not by a completed send. A queued intent,
> committed atomically with the state change that produced it, is the
> evidence that the violation was annunciated; delivery is a separate,
> retried concern recorded by `NOTIFICATION` outcomes.

Stronger than today, because stage 2 already guarantees both are durable
at least one commit before any send, whereas a synchronous send can fail
silently. Honest cost: annunciation becomes eventual.

### 41.7 GitHub App and isolation — PREPARED, NOTHING CREATED OR CHANGED

Target `serina-mcfall/wellbeing-run-002`, branch `main`. **Trusted
apparatus revision: `4ae1488` on `wip/c05-1-persistence`.** `main` is
`4eeaa7c` and contains neither `live-gate.js` nor
`control/accessibility_registry.py` — verified; no merge to `main` is
proposed.

App permissions: **Commit statuses read/write; Contents read; Pull requests
read; nothing else.** Workers get a separate credential with
`Contents: write` + `Pull requests: write` and **no** status access.

**Environment scrubbing is not isolation.** Under one UID a worker can read
`~/.config/gh/`, invoke `gh` itself, use a credential helper, edit the gate
code it is authored against, and write `.runtime/ledger.jsonl` — which is
**mode 644** and append-only only by convention. The boundary requires a
**different OS user** (or container) for workers, and the gate executing
from a trusted revision rather than from the PR's own tree.

Protection diff: add `run-002/independent-review` **app-pinned**, keep `ci`
unchanged, `required_approving_review_count` 1 → **0**, `enforce_admins`
false → **true**. `require_last_push_approval` is **withdrawn** — with zero
required approvals it governs nothing. Verification runs as the worker UID
on a throwaway repo: reading the key, `gh auth status`, posting the
context, editing gate code and writing the ledger must **all fail**.

### 41.8 Decisions genuinely pending — these need the operator

1. **C-02a — the frozen prompt contradiction. BLOCKING the qualitative FAIL
   path.** `prompts/accessibility.md` is frozen and its example cites
   `visible-focus-indicator`, which the registry does not contain, so a
   reviewer following its own prompt is refused `CLASSIFICATION_INVALID`.
   Fail-closed and safe. Three options in the audit row; recommendation is
   **(a)** amend the frozen example as a deliberate freeze amendment.
2. **C-18 stage 7's bounds** — the table in §41.6.
3. **The notification amendment** — the wording in §41.6.
4. **C-20a(C) / GitHub** — §41.7. Recommendation: nothing until the App
   route *and* the separate worker UID are both authorised.

### 41.9 Environment blockers — unchanged, human action required

- **`required_secrets` WILL FAIL.** `~/.config/run-002/secrets.env` does
  not exist; 7 of 8 names absent. **Checked by name only — no value was
  read.** Blocks `langfuse_otel_trace`, `supabase_health`,
  `discord_delivery`.
- **`host_headroom` FAILS.** inotify 77/128 = 60% against a governed 50%
  ceiling. RepoQL owns 67 of 77; two `rql serve` daemons for unrelated
  projects hold 41. **Not actioned** — unrelated host processes are out of
  bounds. The ceiling must not be raised.
- **No `preflight.json` exists**, so `ctl start` would refuse on all 24
  gates as never-run. The PASS rows in the checklist are documented manual
  observations, not durable machine records.
- **`clean_baseline`** — the tree is clean, but the gate has not been
  executed and an unexecuted gate is not a PASS.

### 41.10 Shortest dependency-ordered path to launch

1. **C-02a** (decision) → unblocks the qualitative FAIL path.
2. **Stage 7 bounds + notification amendment** (decisions) → closes C-18.
3. **Qualitative tick dispatch** (engineering, needs 1 and 2's harness).
4. **A services factory** — arrives with the first product build; no
   decision needed.
5. **Provision the eight secrets** (human) → unblocks three gates.
6. **Clear inotify headroom** (human decision about unrelated daemons).
7. **Run the real preflight** → produces `preflight.json`.
8. **C-04a against real GitHub**, then T+00.

Steps 1–2 and 5–6 are independent of each other and can proceed in
parallel. Nothing in 1–4 requires a paid call.

### 41.11 What this section does not claim

- No real accessibility scan has ever run; no product exists, and every
  service in every test is injected.
- `live-gate.js` is still not called by the runtime; `routing.evaluate_merge`
  is the Python-side gate and the two remain unjoined.
- No gate was run, no threshold weakened, no protection or credential
  touched, no paid call made, no product worker launched, no merge to
  `main`. Run 001 untouched. C-18a stands.
- **T+00 remains NOT_STARTED.**

## 42. SESSION HANDOVER — delegated-decision session close (2026-10-01)

**Read this section first. It supersedes §41, which in turn superseded §16.**
§41 is still accurate about what it describes; it is simply older, and one
of its summary claims was wrong — see 42.3.

### 42.1 State at handover — verified, not reported from memory

| | |
|---|---|
| Branch | `wip/c05-1-persistence` |
| HEAD | **`a0d57f6`**, plus this section's own documentation commit on top of it. Everything verified below was measured at `a0d57f6`; the commit that adds §42 touches only `experiment/` documents and no code |
| Pushed | **Yes** — `git status -sb` shows no divergence from `origin/wip/c05-1-persistence` |
| Working tree | **Clean.** `git status --porcelain` empty |
| Verification at HEAD | **2,273 Python tests OK · 218 apparatus tests OK**, both exit 0. Secret scan clean over 244 tracked files. `git diff --check` clean |
| Baseline at session start | `1387275`, 2,171 Python / 218 apparatus |
| Subagents | **Three dispatched, all complete and all integrated.** Two worked in isolated worktrees and their single commits were cherry-picked after review; the third was read-only and returned a report |
| T+00 | **NOT_STARTED** |

**A note on the two worktree agents.** Both reported that their worktree was
cut at `4eeaa7c` rather than the stated `1387275` — 21 and 41 commits stale
respectively — and both fast-forwarded before starting. Their commits apply
cleanly to `1387275` and each touches exactly the one file it owned. Worth
knowing if more worktree agents are dispatched: **verify the base, do not
assume the harness cut it where you asked.**

### 42.2 What the operator delegated, and what was done with it

The operator was away for this session and delegated three of the four
pending decisions. Each authorisation is recorded **verbatim** in its own
evidence file, because the instruction said: *"If repository governance
requires a record of my decision, record this instruction and your selected
implementation."*

| Decision | Record | Outcome |
|---|---|---|
| **C-02a** — the frozen prompt | `experiment/evidence/C-02a-frozen-prompt-amendment.txt` | **APPLIED.** One line of `prompts/accessibility.md` |
| **C-18 stage 7** — `declare_busy` bounds | handover 42.5 + the C-18 audit row | **APPLIED.** Five phases bounded; the proposed table was not adopted |
| **Notification amendment** | `experiment/evidence/C-18-notification-amendment.txt` | **APPLIED.** The last synchronous send is out of T1 |
| **C-20a(C)** — GitHub App + worker UID | `experiment/GITHUB-APP-WORKER-ISOLATION-PROPOSAL.md` | **NOT APPLIED. Still the operator's.** Proposal only; nothing created or changed |

All earlier approvals — G1, G2/G9, G3's phase limits and direction, G4, G6,
G7, C-18a — are **unchanged and still applied**. None was reopened.

### 42.3 The discrepancy §41 contained, resolved

§41.4 said both accessibility halves were "built and wired, with the tick
path proved end to end". §41.5 said the qualitative tick dispatch was
unfinished. Both sentences described something true. The summary did not:

- the **automated** half genuinely was tick-connected end to end, but
  `route_evidence` gates it on `accessibility_services_factory`, which was
  `None` and which nothing ever set. It never planned. **Wired and inert.**
- the **qualitative** half was callable and proved through the Supervisor,
  but `route_evidence` called **neither** `plan_accessibility_review` **nor**
  `ingest_accessibility_review`. Nothing dispatched it in a real run, so
  `review_gate_fires` could never see that leg pass and **no product PR could
  ever have left `WAITING_EVIDENCE`.**

Both are now closed. Precisely what is callable, dispatched and unwired as
of `a0d57f6`:

| | Callable | Dispatched automatically by `tick()` | Still unwired |
|---|---|---|---|
| Automated accessibility | yes | **yes** — `route_evidence` → `execute_accessibility` → `commit_accessibility`, and the factory is now real | nothing in the chain. It has never faced a real product |
| Qualitative accessibility | yes | **yes** — `route_evidence` → `execute_accessibility_review` → `confirm_accessibility_review_spawn` → `reap_workers` → `on_accessibility_review_finished` | nothing in the chain. It has never spent a real provider call |
| `live-gate.js` | yes | **no** | unchanged from §41. `routing.evaluate_merge` is the Python-side gate; the two remain unjoined |

### 42.4 C-02a — what was changed, and the residual that was also closed

`prompts/accessibility.md:80` now reads `"unmet_requirement":
"ACC-DOD-VISIBLE_FOCUS"`. One line. `source_phrase` is "visible focus" —
the same requirement, named canonically; no other registry entry concerns
focus visibility, so the mapping was not ambiguous.

**Hash footprint.** Exactly one of six `prompt_hashes` entries moved,
`5aa0a2ea…` → `51a1bd6a…`. The other four frozen content hashes are
untouched. **No recorded baseline was invalidated**:
`protocol/experiment-manifest.template.json` still carries
`"prompt_hashes": "TO_BE_FILLED"` and no `.runtime` manifest baseline
exists, so this is a **pre-freeze correction**, not a post-T+00 deviation.
The post-T+00 procedure was not bypassed — it does not yet apply.

**No parser alias, and that is now enforced.** `parse_accessibility` still
passes an unrecognised citation through untouched and the severity policy
still rates it INVALID. Both properties have tests, so a later pass cannot
quietly add a mapping table.

**The residual the amendment did not cover.** Fixing one example tells the
reviewer nothing about the other sixteen identifiers, and the document the
prompt sends it to carries source prose, not canonical IDs. Widening the
frozen file would have exceeded the authorisation. Instead the full
vocabulary is injected into the `{{evidence}}` substitution the Supervisor
already owns and fills, rendered **from the registry** so it cannot drift
from what the severity policy accepts. No further frozen-file change, no
new prompt hash, no second source of truth.

### 42.5 C-18 stage 7 — why the proposed table was not adopted

§41.6's seven-row table priced each bound at **the worker's lease** —
builder execute at `timeouts.builder` = 3600. That is wrong.
`_execute_builder_dispatch` writes a prompt, makes a worktree, probes ports
and calls `workers.start_job`, which `Popen`s and returns. **It never waits
for the worker.** A 3660 s bound would let a wedged `workmux add` look
healthy for an hour against a 120 s staleness threshold — precisely the
failure `declare_busy` exists to prevent, and the same argument the table
used to reject a single shared bound, turned on its own rows.

What landed instead — each bound the sum of the external timeouts that
phase can actually incur, **derived from the constants those calls use** so
the two cannot drift:

| Phase | Per item | Derivation |
|---|---|---|
| dispatch execute | **370** | `gh.TIMEOUT` 120 + `WORKMUX_TIMEOUT` 180 + `WORKMUX_PATH_TIMEOUT` 30 + 2×`TMUX_TIMEOUT` 20 |
| security execute | **370** | same calls, same shape |
| accessibility auto execute | **1080** | G3's governed attempt budget |
| merges | **720** | six `gh` calls at 120 |
| each notification drain | **30** | `DRAIN_BUDGET_SECONDS` 10 + one in-flight send |

plus one `BUSY_MARGIN_SECONDS` (60) per phase, and **per item × count**
because every one of these phases loops — a fixed number under-bounds a
batch of two.

Three things the proposal did not contain:

1. **`execute_merges` was absent entirely.** Six `gh` calls per candidate,
   and the phase where a false-positive Watchdog kill is least acceptable
   because `gh.merge` is irreversible.
2. **The stated rule is contradicted by its own only implementation.**
   §41.6 says `observer_bound` is "own timeout + BUSY_MARGIN_SECONDS". It
   is not — it also multiplies by the retry (330 × 2 + 60 = 720, not 390).
3. **Accessibility was never stage-7 polish.** One attempt may use all
   1080 s, nine times the staleness threshold. It has been harmless only
   because no services factory existed to make it run.

**Declaring cannot make detection stricter.** `watchdog.heartbeat_fresh`
tests beat age first and the window second, so a declaration can only
extend freshness. Expiry and recovery are verified against the real
watchdog, including that a cleared 1140 s window does not outlive 120 s
ordinary staleness.

### 42.6 The notification amendment — and the one place it had to differ

`merge_invariant.annunciate` no longer takes a `notifier`. It takes
`announce`, a **required** sink with no default.

- **Supervisor** → queues through `notify_out`, returns `delivered: None`,
  and `drain_notifications` delivers later with bounded retries. This is
  what removes the last synchronous outbound send from T1.
- **Watchdog** → writes the durable intent **and sends immediately**, then
  records the outcome against that intent.

**The Watchdog exemption is not an oversight.** It is a separate process,
it has no drain, and it exists for exactly the case where the Supervisor is
not ticking. Queuing its escalation would have made the Watchdog's
guarantee depend on the component it is watching, which the authorisation's
"preserve the existing escalation guarantees" forbids. It still gains
something: a failed Watchdog send used to vanish, and now stays PENDING for
the Supervisor's drain to retry.

`QUEUED` is never recorded as delivery. `annunciation_intent_id` links the
violation event to the `NOTIFICATION` outcome that later reports delivery.
The governing sentence lives in the **audit**, not in `protocol/` —
"annunciat" appears in none of the nine protocol files — and the C-14 row's
original "sent directly" wording is retained verbatim with the amendment
appended, not rewritten.

### 42.7 The services factory — the thing §41.5 said needed no decision

§41.5 item 2 said a services factory "needs no decision — it resolves when
a product exists to build". **Half true, and the half that was wrong
mattered:** the plumbing is apparatus code, not product code, and leaving
it to the product builder would have meant the first product PR arriving
with nothing able to check it.

`control/accessibility_services.py` implements `run_attempt`'s seven-service
contract, wired in `Supervisor.__init__`. Notable properties:

- **G3's clean isolated build, literally.** Each attempt gets its own
  `git worktree add --detach` checkout of exactly `plan.sha`. Not the
  Builder's worktree — that carries its `node_modules` and `.next`, and a
  scan there judges artefacts rather than the commit.
- **The server starts in its own process group**, so teardown can actually
  finish. `npm run start` execs Next, which spawns workers; signalling only
  npm would leave the real listener holding a governed port, and
  `confirm_release` would then correctly refuse to release it forever.
- **SIGTERM, then SIGKILL** if it survives. The kill is not politeness.
- **Disposal after the verdict is durable**, in a `finally`, because a
  failed attempt is exactly the one whose checkout would otherwise leak.

Every external edge is injected with a real default, so the fixture tests
exercise the real sequencing without running npm, binding a socket or
reading `/proc`.

### 42.8 A defect the new tests caught, worth not repeating

The first version of `confirm_accessibility_review_spawn` set
`claim_state = "DISPATCHED"` and `claim["worktree"]`. Both violate
`accessibility_review_claim_is_valid`'s **closed key set and closed state
set** (`PLANNED` | `COMPLETE` only). The claim silently failed its own
validator, so `review_gate_fires` refused the leg forever and the task
could never leave `WAITING_EVIDENCE`.

Fixed by **conforming, not by widening the guard**. `PLANNED` already means
"in flight" to `plan_accessibility_review`, and the worktree belongs on the
worker record where every other role keeps it.

### 42.9 Verification — what was actually demonstrated

Through the **real Supervisor** with external services injected:

| Required demonstration | Where |
|---|---|
| Required evidence failure blocks review/merge | `test_c05_3b_evidence_to_review`, `test_c05_3b_connected_merge_unblock::test_a_missing_evidence_class_never_even_reaches_review` |
| Repair produces fresh evidence and independent review | `test_c05_3b_connected_lifecycle` |
| A changed head rejects stale approvals/evidence | `test_c05_3b_connected_lifecycle`, `test_a_claim_that_moved_while_spawning_is_discarded` |
| Restart preserves claims and cumulative deadlines | `test_c05_3b_qualitative_dispatch` (3 new cases, a genuinely new Supervisor over the same store) + `test_g3_g4_evidence_budget::test_a_restart_retains_both_the_total_and_the_open_interval` |
| Owned processes cleaned up; ports not released while listening | `test_c05_3b_services_factory::ThroughTheRealAttemptCase` — including that a **failed** listener observation also holds the port |
| Merge completion verified before task completion and dependency unblocking | `test_c05_3b_connected_merge_unblock` — **closes §41.5 item 3**, which was previously proved only in the JavaScript fixture |
| Missing/malformed/untrusted evidence cannot authorize success | `test_an_unknown_citation_still_holds_rather_than_failing`, `test_a_failed_worker_is_not_a_verdict`, `test_a_non_list_checks_field_is_unusable_not_empty` |

**Roughly twenty mutations** across the session, each proved red, restored,
and the restoration verified byte-for-byte by SHA-256 or `diff`. One of
them — reverting `control/supervisor.py` with `git checkout --` while the
stage-7 work was still uncommitted — **destroyed uncommitted work** and the
edits had to be re-applied. Use file copies for mutation backups, or commit
first.

**One test of mine was written on a false premise and went red.**
`clear_busy` also rewrites `at`, which I had assumed it did not. The code
was right and the test was wrong; the test was corrected to the real
contract and a second case added for the property that actually matters.

### 42.10 Environment — eight gates EXECUTED, read-only

Eight side-effect-free, non-network, non-billable gates were run directly:

```
PASS  protocol_present              PASS  clean_baseline
PASS  task_graph_valid              PASS  budget_configured_by_human
PASS  manifest_readiness            PASS  migration_lock_free
PASS  host_headroom                 FAIL  required_secrets
```

Two changes from §41.9 worth noting:

- **`host_headroom` now PASSES** — inotify 48/128 = 37.5%, not the 60% on
  record. The unrelated `rql serve` daemons are no longer running. **This
  does not close the item**: the count tracks concurrent tooling, so a PASS
  taken on a quiet host says nothing about the host at launch. If those
  daemons return, it fails again. The ceiling must not be raised.
- **`clean_baseline` has now actually been executed** and passes. §41.9 was
  right to refuse to call an unexecuted gate a PASS.

`required_secrets` fails exactly as recorded: the file is absent and 7 of 8
names are unset. **Checked by name only — no value was read, printed or
logged at any point in this session.**

### 42.11 What remains, and who owns it

| # | Item | Owner | Next action |
|---|---|---|---|
| 1 | **C-20a(C)** — GitHub App + worker UID | **OPERATOR** | Read `experiment/GITHUB-APP-WORKER-ISOLATION-PROPOSAL.md`. **Do not approve §41.7's permission set** — F1–F5 there show it produces a gate that denies every PR forever |
| 2 | **Provision the eight secrets** | **HUMAN, on the host** | `~/.config/run-002/secrets.env`, mode 600. Unblocks three gates and supplies the paid gates' credentials |
| 3 | **inotify headroom at launch** | **HUMAN, on the host** | Currently passing. Re-measure on a quiet host immediately before preflight; decide about unrelated indexers if they have returned |
| 4 | **Run the real preflight** | ENGINEERING, after 2–3 | Produces `preflight.json`, without which `ctl start` refuses on all 24 as never-run. Cannot be done now: it would make paid calls and send a real notification |
| 5 | **C-04a against real GitHub** | ENGINEERING, after 1 | The `gh` path, auth, rate limiting and pagination are all unexercised |
| 6 | **Join or retire one of the two merge gates** | ENGINEERING | `live-gate.js` is still unreachable from a tick. Unchanged from §41 |
| 7 | **`prompts/accessibility.md`'s stale disclosure** | **OPERATOR** (small) | Its text still says the automated pipeline's dispatch "is not yet implemented". That is now false. **Deliberately not amended** — the C-02a delegation covered the identifier only, and the instruction said it was "not permission to silently rewrite unrelated frozen requirements". It is fail-safe (it makes the reviewer more cautious, not less), but it is a false statement in a frozen file |

### 42.12 What this section does not claim

- **No real accessibility scan has ever run.** No product exists, every
  service in every test is injected, and neither dispatch path has faced a
  real PR, URL or provider call.
- **No preflight record exists**, so every PASS above is an observation,
  not a durable machine record, and `ctl start` would still refuse.
- `live-gate.js` is still not called by the runtime.
- No gate was weakened, no threshold lowered, no protection or credential
  touched, no App or OS user created, no paid call made, no product worker
  launched, no merge to `main`. Run 001 untouched. C-18a stands.
- **T+00 remains NOT_STARTED.**
