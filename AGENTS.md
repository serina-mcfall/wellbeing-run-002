# Run 002 agent boundary

This directory is Run 002. Run 001 is live and read-only for original
product/task specification reference only.

Do not write to, signal, kill, clean, lock, reuse, overwrite, or otherwise
interfere with Run 001 processes, files, worktrees, Git branches, ports,
browser profiles, tmux sessions, databases, deployments, credentials,
budgets, logs, or runtime resources. Do not import Run 001 product code,
generated implementation, or live runtime state.

Keep all Run 002 state, temporary resources, and generated artifacts under
Run 002-specific names and locations. Check ownership before any cleanup
or process action. If ownership is uncertain, stop that action and record
HUMAN_REQUIRED.

The files under product/, tasks/, and config/tasks.json are copied original
specifications. Protocol v2 in protocol/ governs Run 002 control-plane
policy. Record and resolve contradictions before freezing either.

Do not start T+00, launch the autonomous supervisor, or dispatch product
workers until the contradiction audit, full isolation proof, realistic
multi-cycle preflight, and every launch gate pass. A partial pass cannot
authorize launch.
