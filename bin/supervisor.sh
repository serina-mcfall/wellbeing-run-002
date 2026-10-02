#!/usr/bin/env bash
# Thin wrapper. All logic lives in control/supervisor.py.
set -euo pipefail

# WHY 0002, AND WHY HERE. The Supervisor creates each worker's linked
# worktree; the worker then has to commit in it. Under the default 022 the
# checked-out files and the worktree's gitdir come out without the group
# write bit, and `run002-wrk` cannot write them - measured in
# tests/test_c22_shared_dispatch_modes.py. This is the third of the three
# parts of that arrangement; the other two are `core.sharedRepository=group`
# and A5's one-time chmod, both applied to the checkout rather than here.
#
# It is set on the wrapper so it covers the whole process tree, workers
# included. It does NOT widen `.runtime/`: that directory is 0700 owned by
# run002-sup, so nothing inside it is reachable by the worker whatever mode
# its files carry.
umask 0002

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"
exec python3 -m control.supervisor
