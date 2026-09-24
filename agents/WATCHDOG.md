# Watchdog
Minimal non-LLM process. Periodically check supervisor PID/heartbeat. If dead/stale, restart once and log. If restart fails, send Discord HUMAN_REQUIRED. Do nothing else.
