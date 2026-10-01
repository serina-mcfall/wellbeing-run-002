# C-05.3a Step 6b — Session Handover, Repair Plan and Implementation Record

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
