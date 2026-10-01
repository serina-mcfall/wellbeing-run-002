# Configuration and environment variable NAMES — TEMPLATE, UNAPPLIED

**TEMPLATE. Nothing here is configured, exported, written to any file, or
deployed. Deployment is not authorised.** This file lists **names only**.
No value appears here, and no value may ever be written here, printed,
logged, grepped or pattern-matched.

Companion to `experiment/GITHUB-APP-WORKER-ISOLATION-PROPOSAL.md` §9.4 and
§9.8. Names are proposed, not implemented: **none of these is read by any
code in the repository today.** `control/gh.py:33` currently runs `gh` with
no explicit token and no `env=`, so it inherits ambient host auth; runbook
step 12 is the change that would make these names load-bearing.

---

## 1. Names that already exist in the code

| Name | Where | Status |
|---|---|---|
| `OPENROUTER_API_KEY` | `control/config.py:52` | existing, set |
| `DISCORD_WEBHOOK_URL` | `control/config.py:53` | existing, unset |
| `LANGFUSE_PUBLIC_KEY` | `control/config.py:54` | existing, unset |
| `LANGFUSE_SECRET_KEY` | `control/config.py:55` | existing, unset |
| `LANGFUSE_BASE_URL` | `control/config.py:56` | existing, unset |
| `SUPABASE_URL` | `control/config.py:57` | existing, unset |
| `SUPABASE_PUBLISHABLE_KEY` | `control/config.py:58` | existing, unset |
| `SUPABASE_SECRET_KEY` | `control/config.py:59` | existing, unset |
| `RUN_001_ROLE` | `control/worker_entry.py:114` | existing; set per worker |
| `RUN_001_TASK` | `control/worker_entry.py:115` | existing; set per worker |
| `PORT` | `control/worker_entry.py:120` | existing; set per worker |

These are product/observability secrets, loaded from
`~/.config/run-002/secrets.env` (mode 600) by `control/config.py:124`.
**They are unrelated to the GitHub principals below and must not be mixed
into the same file**, because the worker UID is meant to receive some of
them and none of the GitHub keys.

---

## 2. Proposed names — run002-sup (Supervisor and publisher)

Private-key **paths**, never key material. Each file `0400`, owner
`run002-sup`.

| Name | Holds | Read by |
|---|---|---|
| `RUN002_GATE_APP_ID` | numeric App ID of `run-002-independent-review` | publisher; also the value that replaces `__GATE_APP_ID__` in `branch-protection-AFTER.json` |
| `RUN002_GATE_APP_INSTALLATION_ID` | installation ID on the target repo | publisher |
| `RUN002_GATE_APP_KEY_PATH` | **path** to the gate App private key | publisher only |
| `RUN002_SUPERVISOR_APP_ID` | numeric App ID of `run-002-supervisor` | Supervisor |
| `RUN002_SUPERVISOR_APP_INSTALLATION_ID` | installation ID | Supervisor |
| `RUN002_SUPERVISOR_APP_KEY_PATH` | **path** to the supervisor App private key | Supervisor only |
| `RUN002_WORKER_APP_ID` | numeric App ID of `run-002-worker` | the minting step only |
| `RUN002_WORKER_APP_INSTALLATION_ID` | installation ID | the minting step only |
| `RUN002_WORKER_APP_KEY_PATH` | **path** to the worker App private key | the minting step only — **never exported into a worker process** |
| `RUN002_TRUSTED_GATE_PATH` | absolute path of the read-only trusted export, e.g. `/opt/run-002/gate-<40-hex>` | the gate invoker |
| `RUN002_TRUSTED_GATE_SHA` | the pinned 40-hex revision (§9.5) | recorded into the ledger with every decision |
| `GH_CONFIG_DIR` | gh's own config directory for this role | `gh`, standard |
| `GH_TOKEN` | the minted installation token for whichever principal this process is | `gh`, standard |

`GH_TOKEN` is a **standard `gh` variable**, so whichever value is in it is
the identity `gh` uses. That is precisely why the Supervisor process and
the publisher process must be separate processes with separate
environments: one `GH_TOKEN` per process, never both.

---

## 3. Proposed names — run002-wrk (every product worker)

| Name | Holds |
|---|---|
| `GH_TOKEN` | the worker's minted 1-hour installation token |
| `GH_CONFIG_DIR` | the worker's own gh config directory, under `run002-wrk`'s home |
| `HOME` | `run002-wrk`'s own home — `claude` and `codex` keep state in `$HOME` (§7 R5) |

**The worker environment must be constructed from an allow-list**, not from
`dict(os.environ)` as `control/worker_entry.py:180` does today. Any name in
section 2 above that reaches a worker process defeats the boundary.

Proposed allow-list for the worker child environment: `PATH`, `HOME`,
`USER`, `LANG`, `LC_ALL`, `TERM`, `TZ`, `GH_TOKEN`, `GH_CONFIG_DIR`,
`RUN_001_ROLE`, `RUN_001_TASK`, `PORT`, plus whichever product secrets from
section 1 the task genuinely needs. Everything else is dropped.

---

## 4. Storage rules

- Names and **paths** may live in a config file. **Key material may not.**
- The three private keys live outside the repository, outside
  `~/.config/run-002/secrets.env`, and outside any directory a worker can
  traverse.
- `.gitignore` must cover the key directory before any key exists.
- No agent may `cat`, `grep`, `strings`, `head`, `tail` or glob a key file.
  Existence and mode are checked with `ls -l` or `test -r` and nothing else.
