#!/usr/bin/env bash
# Run 002 — Stage 1, the root-side steps that need NO GitHub credentials.
#
#   sudo bash experiment/github-app/stage1-root-phase1.sh 2>&1 | tee ~/stage1-phase1.log
#
# Covers A2, A5, A6, C1, C2, C3 and every V-step that does not need an App
# token: V1, V1b, V1c, V4, V4b, V5, V5b, V14a-V14d, V14g, V14g-export.
#
# WHAT IT DOES NOT DO. It creates no GitHub App, downloads no key, pushes
# nothing, and reaches NO stop gate. V2/V3/V6/V7-V12 and V14f are the
# credential half and need the Apps from A3/A4. 10b is reached only after
# those pass.
#
# IT STOPS AT THE FIRST FAILURE, and an isolation check that comes out the
# wrong way is a failure. `set -e` plus explicit expectations below: a
# command that must be REFUSED is run with `!` so a success aborts the run.
#
# REVERSIBLE VIA ~/run-002-stage1-rollback, captured by A0 before anything
# here existed. Nothing below deletes, moves or rewrites any pre-existing
# file, branch, worktree or process.

set -euo pipefail

WS=/home/serina/wellbeing-agent-experiment/agent-run-002
WTROOT="${WS}__worktrees"
R=/home/serina/run-002-stage1-rollback
PIN=ab1ceee8b2d8ac88663c51a36ccf99b240f4c061
EXPORT=/opt/run-002/gate-ab1ceee
EXPORT_WTNAME=gate-ab1ceee
PROD=serina-mcfall/wellbeing-run-002

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
ok()  { printf '   \033[32mOK\033[0m   %s\n' "$*"; }
bad() { printf '   \033[31mFAIL\033[0m %s\n' "$*"; exit 1; }

[ "$(id -u)" -eq 0 ] || bad "run this with sudo"

# --------------------------------------------------------------- preconditions
say "PRECONDITIONS — refusing to act on anything A0 did not record as ABSENT"
[ -s "$R/ownership-BEFORE.txt" ]   || bad "A0 capture missing: $R/ownership-BEFORE.txt"
[ -s "$R/identities-BEFORE.txt" ]  || bad "A0 capture missing: identities-BEFORE.txt"
grep -q 'ABSENT group run002'      "$R/identities-BEFORE.txt" || bad "group run002 pre-existed — not this stage's to create"
grep -q 'ABSENT user run002-sup'   "$R/identities-BEFORE.txt" || bad "run002-sup pre-existed — not this stage's to create"
grep -q 'ABSENT user run002-wrk'   "$R/identities-BEFORE.txt" || bad "run002-wrk pre-existed — not this stage's to create"
grep -q "ABSENT $WTROOT"           "$R/worktree-root-BEFORE.txt" || bad "$WTROOT pre-existed"
grep -q 'ABSENT core.sharedRepository' "$R/sharedrepo-BEFORE.txt"  || bad "core.sharedRepository was already set"
ok "A0's capture is present and records all five as ABSENT"

# ------------------------------------------------------------------- A2
say "A2 — identities (action 1)"
getent group  run002     >/dev/null || groupadd --system run002
getent passwd run002-sup >/dev/null || useradd --system --create-home --shell /usr/sbin/nologin --gid run002 run002-sup
getent passwd run002-wrk >/dev/null || useradd --system --create-home --shell /bin/bash          --gid run002 run002-wrk
id run002-sup >/dev/null && id run002-wrk >/dev/null || bad "identities not created"
ok "group run002, users run002-sup and run002-wrk exist"
id run002-sup; id run002-wrk

# ------------------------------------------------------------------- A5
say "A5 — the ownership and the dispatch writability arrangement (action 5)"

# 1. git creates group-writable, setgid objects/refs/worktree gitdirs FROM NOW ON.
#    NOT `git init --shared=group`: on an existing repository it repairs no
#    existing mode and rewrites the value to the numeric form. Both measured.
git -C "$WS" config core.sharedRepository group
ok "core.sharedRepository = $(git -C "$WS" config core.sharedRepository)"

# 2. Ownership, then the three directories part 1 does NOT fix retroactively.
chown -R run002-sup:run002 "$WS"
chmod -R 0750 "$WS"
chmod -R g+ws "$WS/.git/objects" "$WS/.git/refs" "$WS/.git/logs"
ok "checkout re-owned; objects/refs/logs setgid and group-writable"

# 3. The worker worktree root. A SIBLING of <WS>: `chmod -R <WS>` never
#    reaches it, and its parent is serina-owned, so run002-sup cannot
#    create it. Root creates it once; nothing after this needs privilege.
install -d -o run002-sup -g run002 -m 2770 "$WTROOT"
ok "worktree root $WTROOT is $(stat -c '%a %U %G' "$WTROOT")"

# 4. Claw back what §6 withholds — AFTER the above, never before.
chmod 0700 "$WS/.runtime"
chmod -R 0750 "$WS/.git/hooks"
chmod 0640 "$WS/.git/config"
ok "clawed back: .runtime 0700, hooks 0750, config 0640"

# 5. GIT'S OWNERSHIP GUARD — A FOURTH PART, FOUND 2026-10-02 WHILE EXECUTING.
#    Since git 2.35.2 git refuses to parse the config of a repository owned
#    by another user. Every worker worktree is created by run002-sup and run
#    by run002-wrk, so EVERY worker git command would abort with "detected
#    dubious ownership" - not a permission boundary, a configuration gap,
#    and it would stop the run dead at the first dispatch.
#
#    `safe.directory` takes exact paths only; `git help config` documents no
#    glob but the single value `*`. Worker worktrees are named per task, so
#    exact entries cannot be pre-seeded without a per-dispatch code change.
#
#    `*` IS SCOPED TO run002-wrk'S OWN GLOBAL CONFIG, AND NOTHING ELSE. What
#    the guard prevents is running another user's hooks and config; the
#    other user here is run002-sup, the trusted dispatcher, and the worker
#    is ALREADY denied write on both `.git/hooks` (0750) and `.git/config`
#    (0640) by the claw-back above. It grants the worker no access it did
#    not have: file modes, not this setting, are what decide that.
#    THE OPERATOR MAY OVERRULE IT in favour of per-dispatch exact entries,
#    which is a code change and R-class work.
sudo -H -u run002-wrk git config --global --add safe.directory '*'
sudo -H -u run002-sup git config --global --add safe.directory "$WS"
ok "safe.directory set: run002-wrk '*' (scoped to its own config), run002-sup $WS"

# ------------------------------------------------------------------- A6
say "A6 — origin SSH to HTTPS (action 6c), as run002-sup who owns .git/config"
sudo -u run002-sup git -C "$WS" remote set-url origin "https://github.com/$PROD.git"
echo "   stored : $(sudo -u run002-sup git -C "$WS" config remote.origin.url)"
echo "   push   : $(sudo -u run002-wrk git -C "$WS" remote get-url --push origin)"
ok "A6 applied — V14f below is what judges the resolved URL under the worker"

# ------------------------------------------------- C1/C2/C3 — the trusted export
# ORDERED HERE BY THE OPERATOR: the export is created and prepared BEFORE
# V13 and V14g-export, rather than after Phase B.
say "C1 — create the read-only export at the pin (action 7)"
install -d -m 0755 -o run002-sup -g run002 /opt/run-002
sudo -u run002-sup git -C "$WS" worktree add --detach "$EXPORT" "$PIN"
# core.sharedRepository made this gitdir group-writable like every other.
# §6 withholds it: gate_invoker.export_revision() reads its HEAD to prove
# the export is at the pin, and a worker that could write it could claim
# any revision.
chmod 0700 "$WS/.git/worktrees/$EXPORT_WTNAME"
head_line=$(cat "$WS/.git/worktrees/$EXPORT_WTNAME/HEAD")
case "$head_line" in
  ref:*) bad "export HEAD is symbolic ($head_line) — --detach was omitted" ;;
esac
[ "$head_line" = "$PIN" ] || bad "export HEAD is $head_line, expected the pin $PIN"
ok "export at $EXPORT, HEAD is the pin as 40 hex, gitdir $(stat -c '%a' "$WS/.git/worktrees/$EXPORT_WTNAME")"

say "C2 — runtime dependencies, BEFORE anything is frozen (action 7b)"
# -H so npm's cache and config land in run002-sup's home, not root's.
sudo -H -u run002-sup sh -c "cd $EXPORT/apparatus && npm ci --omit=dev"
[ -f "$EXPORT/apparatus/node_modules/ajv/dist/2020.js" ] || bad "ajv missing — the gate cannot load"
pw=$(ls "$EXPORT/apparatus/node_modules" | grep -c playwright || true)
[ "$pw" -eq 0 ] || bad "playwright present in a production export ($pw)"
ok "ajv present, playwright absent"

say "C3 — freeze it, only now (action 7c)"
chmod -R a-w "$EXPORT"
ok "export frozen read-only"

# -------------------------------------------------- B2 — the filesystem half
# V1/V1b/V1c use `test -r`, NEVER `cat`, so no value can reach a transcript
# even on unexpected success.
say "B2 — filesystem isolation: V1, V1b, V1c, V4, V4b, V5, V5b"
! sudo -u run002-wrk test -r /home/serina/.config/gh/hosts.yml || bad "V1  — worker can read the gh config"
ok "V1  gh config unreadable by run002-wrk"
if [ -e /home/serina/.ssh/id_ed25519 ]; then
  ! sudo -u run002-wrk test -r /home/serina/.ssh/id_ed25519 || bad "V1b — worker can read the SSH key"
  ok "V1b SSH key unreadable by run002-wrk"
else
  ok "V1b no /home/serina/.ssh/id_ed25519 on this host — recorded, not skipped silently"
fi
if [ -e /etc/run-002/keys/gate.pem ]; then
  ! sudo -u run002-wrk test -r /etc/run-002/keys/gate.pem || bad "V1c — worker can read the gate key"
  ok "V1c gate key unreadable by run002-wrk"
else
  ok "V1c no key placed yet (A4 pending) — re-run V1c after A4"
fi
! sudo -u run002-wrk touch "$EXPORT/apparatus/pr-evidence/live-gate.js" 2>/dev/null || bad "V4  — worker wrote the frozen export"
ok "V4  frozen export refuses the worker"
! sudo -u run002-wrk sh -c "echo x >> $WS/apparatus/pr-evidence/live-gate.js" 2>/dev/null || bad "V4b — worker wrote live gate code"
ok "V4b live gate code refuses the worker"
! sudo -u run002-wrk sh -c "echo x >> $WS/.runtime/ledger.jsonl" 2>/dev/null || bad "V5  — WORKER CAN APPEND TO THE LEDGER"
ok "V5  ledger refuses the worker"
! sudo -u run002-wrk test -r "$WS/.runtime/state.json" || bad "V5b — WORKER CAN READ STATE.JSON"
ok "V5b state.json unreadable by the worker"

# ------------------------------------------------------------------- B7 — V14
say "B7 — V14a-V14d: a worktree made the way dispatch makes one, with NO rescue"
# Created by run002-sup under the Supervisor's own umask, in the real root,
# with NO chown after it. --orphan so the probe starts from no history:
# nothing of this repository is pushed, and no existing branch is involved.
sudo -u run002-sup sh -c "umask 0002; git -C $WS worktree add -q --orphan -b probe/v14 $WTROOT/probe-v14"
ok "V14a worktree created by run002-sup"

say "V14b — the modes, BEFORE any worker touches them"
stat -c '   %a %U %G %n' "$WTROOT/probe-v14" "$WS/.git/worktrees/probe-v14"
for p in "$WTROOT/probe-v14" "$WS/.git/worktrees/probe-v14"; do
  m=$(stat -c '%a' "$p"); g=$(stat -c '%G' "$p")
  [ "$g" = run002 ] || bad "V14b — $p is group $g, expected run002"
  case "$m" in *7?|*6?|*3?|*2?) ;; *) bad "V14b — $p mode $m has no group write bit" ;; esac
done
grep -q 'umask 0002' "$WS/bin/supervisor.sh" || bad "V14b — bin/supervisor.sh has no umask 0002"
[ "$(sudo -u run002-sup git -C "$WS" config core.sharedRepository)" = group ] || bad "V14b — sharedRepository is not group"
ok "V14b all three parts of the arrangement are present on disk"

say "V14c/V14d — the worker writes and commits, with no privilege and no help"
sudo -u run002-wrk sh -c "umask 0002; echo probe > $WTROOT/probe-v14/V14-PROBE"
sudo -u run002-wrk git -C "$WTROOT/probe-v14" add V14-PROBE
sudo -u run002-wrk git -C "$WTROOT/probe-v14" \
  -c user.email=run002-wrk@invalid -c user.name=run002-wrk \
  -c commit.gpgsign=false commit -q -m 'v14 probe'
sudo -u run002-wrk git -C "$WTROOT/probe-v14" log --oneline -1
ok "V14d THE WORKER COMMITTED — objects, refs and its gitdir all stayed writable"

say "V14g — the protected paths, every one of which MUST be refused"
hooks_mode=$(stat -c '%a' "$WS/.git/hooks")
[ "$hooks_mode" = 750 ] || bad "V14g — .git/hooks is $hooks_mode, expected 750"
! sudo -u run002-wrk sh -c "echo x > $WS/.git/hooks/pre-commit" 2>/dev/null || bad "V14g — worker wrote a git hook"
ok "V14g hooks refuse the worker (dir mode 750, not merely absent)"
! sudo -u run002-wrk sh -c "echo x >> $WS/.git/config" 2>/dev/null || bad "V14g — worker wrote .git/config"
ok "V14g .git/config refuses the worker"
! sudo -u run002-wrk ls "$WS/.runtime" >/dev/null 2>&1 || bad "V14g — worker listed .runtime"
ok "V14g .runtime refuses the worker"
! sudo -u run002-wrk cat "$WS/.git/worktrees/$EXPORT_WTNAME/HEAD" >/dev/null 2>&1 \
  || bad "V14g-export — WORKER READ THE EXPORT'S HEAD, the pin proof"
ok "V14g-export the export's gitdir refuses the worker"

say "PHASE 1 COMPLETE — no stop gate reached"
cat <<'EOF'
   Done: A2, A5, A6, C1, C2, C3, V1, V1b, V1c(partial), V4, V4b, V5, V5b,
         V14a, V14b, V14c, V14d, V14g, V14g-export.
   NOT done, and needing the three Apps from A3/A4:
         V2, V2b, V3, V3b, V3c, V6, V7, V8, V9, V10, V11, V12, V13, V14f.
   V14e is NOT APPLICABLE under a single run002-wrk identity. It is not a pass.
   The 10b STOP gate is NOT reached until the credential half passes.
EOF
