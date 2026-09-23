#!/bin/bash
# ExecStop for write-or-perish-celery.service (issue #312).
#
#   celery-graceful-stop.sh <main-pid> [grace-seconds]
#
# 1. SIGTERM the Celery main process: a warm shutdown. It stops consuming,
#    waits for the pool processes to finish their running tasks, returns
#    reserved and ETA messages to the queue, and exits.
# 2. If it is still draining after the grace period (default 240 s), SIGTERM
#    the pool processes. That is what the old KillMode=control-group did at
#    once: the running task gets SystemExit, and the main process, still
#    alive, logs the lost task and restores its reserved messages. Without
#    this step systemd's SIGKILL at TimeoutStopSec would take the main
#    process down too, and those messages would wait out the Redis
#    visibility timeout (1 h) with nothing in the worker log.
# 3. systemd then waits for the main process and SIGKILLs whatever is left
#    (KillMode=mixed, TimeoutStopSec in the unit).
set -u

MAINPID="${1:-}"
GRACE="${2:-240}"

[ -n "$MAINPID" ] || exit 0
kill -TERM "$MAINPID" 2>/dev/null || exit 0

for _ in $(seq "$GRACE"); do
    kill -0 "$MAINPID" 2>/dev/null || exit 0
    sleep 1
done

echo "celery drain exceeded ${GRACE}s; terminating pool processes" >&2
pkill -TERM -P "$MAINPID" || true
exit 0
