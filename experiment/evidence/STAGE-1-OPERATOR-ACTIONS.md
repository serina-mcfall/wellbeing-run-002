# Stage 1 — the actions only the operator can take

A0 and A1 are **done and passing** (`STAGE-1-EXECUTION-LOG.md`). Everything
below is blocked by an access control working as intended, not by unfinished
work.

Two boundaries, both measured at execution time:

- **`sudo -n true` fails** — sudo wants a password, so no agent session can
  run A2, A5, A6, C1–C3 or any `sudo -u run002-*` V-step.
- **A GitHub App cannot be created without a browser.** A3 is a UI flow and
  A4's keys are generated inside it.

---

## ACTION 1 — run the root script *(covers A2, A5, A6, C1, C2, C3 and the credential-free V-steps)*

```
cd /home/serina/wellbeing-agent-experiment/agent-run-002
sudo bash experiment/github-app/stage1-root-phase1.sh 2>&1 | tee ~/stage1-phase1.log
```

**Read it first — it is commented throughout.** It re-checks A0's capture
before touching anything and **refuses to act** on any identity, path or
config value A0 did not record as ABSENT. It stops at the first failure,
and a protected path that answers the wrong way is a failure.

It covers, in the order your approval set: **A2** identities → **A5** the
ownership and writability arrangement → **A6** origin to HTTPS → **C1/C2/C3
the trusted export, created and prepared before V13 and V14g-export** →
**V1, V1b, V1c, V4, V4b, V5, V5b** → **V14a–V14d, V14g, V14g-export**.

It creates no App, downloads no key, pushes nothing, and **reaches no stop
gate**.

### Two things to know before you run it

**1. It ends this session's access to the repository.** A5 sets
`chown -R run002-sup:run002` and `chmod -R 0750`, and `serina` is not in
group `run002`. After it runs I cannot read the repo, run the suite, or
commit — only through `sudo`. Everything that needed writing has been
committed first, deliberately.

Optional, your call, not required by any V-step:

```
sudo usermod -aG run002 serina     # restores READ for your own account
```

`.runtime/` stays `0700 run002-sup`, so it remains unreadable to the group
either way. This widens nothing a root-capable operator did not already
have.

**2. It makes one decision you can overrule.** Git refuses to parse a
repository owned by another user, and every worker worktree is created by
`run002-sup` and run by `run002-wrk` — so **every worker git command would
abort with "detected dubious ownership"** and the run would stop at the
first dispatch. This was found while executing; the procedure did not have
it.

`safe.directory` accepts exact paths only — `git help config` documents no
glob except the single value `*` — and worker worktrees are named per task,
so exact entries cannot be pre-seeded without a per-dispatch code change.
The script sets `safe.directory = *` **in `run002-wrk`'s own global config
and nowhere else**. What the guard prevents is running another user's hooks
and config; that other user is `run002-sup`, the trusted dispatcher, and the
worker is already denied write on `.git/hooks` (0750) and `.git/config`
(0640). **Say so and I will replace it with per-dispatch exact entries
instead** — that is a code change and R-class work.

---

## ACTION 2 — create the three Apps *(A3, browser)*

**GitHub → Settings → Developer settings → GitHub Apps → New GitHub App**,
three times. Source of truth: the `manifest` key of each file in
`experiment/github-app/`. Submit **nothing** from `_TEMPLATE_NOTICE` or
`_principal`.

For **all three**: **Webhook → uncheck Active**; subscribe to **zero**
events; **Where can this app be installed → Only on this account**;
permissions **exactly** as listed and nothing more.

| App name | Permissions — exactly these |
|---|---|
| `run-002-independent-review` | Metadata **read** · Checks **read** · Commit statuses **write** · Pull requests **read** |
| `run-002-supervisor` | Metadata **read** · Administration **read** · Actions **read** · Checks **read** · Commit statuses **read** · Pull requests **write** · Contents **write** · Issues **write** |
| `run-002-worker` | Metadata **read** · Contents **write** · Pull requests **write** |

**The separation is the point, so check these three negatives:**
- the **gate** has **no** contents, **no** pull-requests write, **no**
  administration, **no** actions — it cannot merge what it blessed (V8);
- the **supervisor** has **no** commit-statuses *write* — it cannot publish
  the verdict it then acts on (V3d);
- the **worker** has **no** statuses, **no** checks, **no** administration,
  **no** actions — the thing being judged cannot publish the verdict on
  itself.

**The App's homepage URL field in the manifests points at the production
repository.** That is metadata only. **Do not install any App on the
production repository** — installation is the throwaway only, and that is
step B1.

**Record as you go**, because the creation screen answers three questions
this repository could not:

1. Is **Issues: write** actually needed for `gh pr comment`, or does
   Pull requests: write cover it?
2. Is **Contents: write** needed alongside Pull requests: write for a
   squash merge and `--delete-branch`?
3. Each App's numeric **App ID** — the gate's is needed later for
   `__GATE_APP_ID__`.

---

## ACTION 3 — the three private keys *(A4)*

Generate one key per App from its settings page, then:

```
sudo install -d -m 0700 -o run002-sup -g run002 /etc/run-002/keys
sudo install -m 0400 -o run002-sup -g run002 ~/Downloads/<gate>.pem       /etc/run-002/keys/gate.pem
sudo install -m 0400 -o run002-sup -g run002 ~/Downloads/<supervisor>.pem /etc/run-002/keys/supervisor.pem
sudo install -m 0400 -o run002-sup -g run002 ~/Downloads/<worker>.pem     /etc/run-002/keys/worker.pem
sudo rm -f ~/Downloads/*.pem
```

**Verify without reading the files:** `sudo ls -l /etc/run-002/keys` shows
`-r-------- run002-sup run002` on all three. **Never `cat` a key**, and do
not paste one into this session — these are live credentials from the moment
they exist.

Then re-run **V1c**, which action 1 could only record as "no key placed
yet":

```
sudo -u run002-wrk test -r /etc/run-002/keys/gate.pem; echo $?     # must print 1
```

---

## THEN TELL ME

Paste the **tail of `~/stage1-phase1.log`**, the `ls -l` of the key
directory, the three **App IDs**, and your answers to the two permission
questions. I will take Stage 1 from **B1** (install the three Apps on the
throwaway) through the credential half — V2, V3, V6, V7–V12, V13 and
V14f — and stop at the **10b STOP gate**.

**V14e stays NOT APPLICABLE**, and the shared-worker limitation is retained
explicitly: under a single `run002-wrk` identity, peers reach each other's
files, so C-22 closes only for what Stage 1 actually proves.
