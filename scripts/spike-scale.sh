#!/usr/bin/env bash
#
# CPM-OPERATE-S10: measure the evidence tables at ten thousand packages.
#
# Runs `tests/spikes/spike_evidence_scale.py` against a throwaway `postgres:17`,
# the image the gate uses, on `gate-postgres.sh`'s terms: a named container on a
# port nothing else binds, a readiness loop that waits for TCP rather than the
# unix socket, and the container removed on every exit path so a failed spike
# never leaves twenty-odd million rows running on a laptop.
#
# It is a script and not a `cmd` in `pixi.toml` for the reason its siblings are:
# pixi runs task commands through `deno_task_shell`, which has no `for`, no `if`
# and no command substitution, and the readiness loop is the part that must not
# be dropped. The pixi task `spike-scale` is the entry point; this is what it runs.
#
# The spike is never part of `pixi run ci`: the module is named `spike_*.py`,
# which `[tool.pytest.ini_options] python_files` does not match, so the gate's
# `pytest tests/` never collects it and only this script names it.
#
# The whole run is copied to `.spike-runs/<timestamp>.log` (gitignored), so the
# plans the story records are a file rather than scrollback.
set -uo pipefail

CONTAINER="pg-spike"
# Deliberately neither 5432 (a local PostgreSQL) nor 55432 (`gate-postgres.sh`):
# the two scripts must be able to run side by side without one measuring the
# other's database.
PORT="55433"
DB_URL="postgres://spike:spike@localhost:${PORT}/spike"
READY_TIMEOUT_SECONDS=30
SPIKE_MODULE="tests/spikes/spike_evidence_scale.py"
RUN_DIR=".spike-runs"

if ! docker info >/dev/null 2>&1; then
    echo "docker is not running; this spike needs it to start ${CONTAINER}" >&2
    exit 1
fi

# A container already running under this name is somebody's run in progress --
# or a leak from one that was killed outside the trap below. Either way the
# right answer is to say so, not to remove it and measure over its ashes.
if docker ps --format '{{.Names}}' | grep -qx "${CONTAINER}"; then
    echo "${CONTAINER} is already running; a spike is in progress, or a previous one leaked it." >&2
    echo "Wait for it, or 'docker rm -f ${CONTAINER}' if you know it is yours to remove." >&2
    exit 1
fi

# `docker rm -f` rather than `stop`: the container is `--rm`, so stopping it
# disposes of it, but a container that never started cleanly may sit created
# and not running, which `stop` leaves behind and the guard above then refuses.
# INT and TERM as well as EXIT, as `gate-redis.sh` explains: a Ctrl-C mid-run
# must still remove the container rather than leave it for the next run to
# find. This is a script with its own process, so EXIT is the end of the run.
cleanup() {
    docker rm -f "${CONTAINER}" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

# The exit status is checked rather than assumed: a failed pull -- a rate-limited
# registry, no network -- otherwise surfaces thirty seconds later as "never
# became ready", which points at the readiness loop instead of at the pull.
if ! docker run -d --rm --name "${CONTAINER}" \
    -e POSTGRES_USER=spike \
    -e POSTGRES_PASSWORD=spike \
    -e POSTGRES_DB=spike \
    -p "${PORT}:5432" \
    postgres:17 >/dev/null; then
    echo "docker run failed to start ${CONTAINER} (postgres:17 on port ${PORT}); see the output above" >&2
    exit 1
fi

# `-h localhost` is load-bearing: the image's entrypoint runs initdb against a
# temporary server started with `listen_addresses=''`, so a bare `pg_isready`
# reports success on the unix socket while TCP is still closed.
ready=""
for _ in $(seq "${READY_TIMEOUT_SECONDS}"); do
    if docker exec "${CONTAINER}" pg_isready -h localhost -U spike -d spike >/dev/null 2>&1; then
        ready=1
        break
    fi
    sleep 1
done

if [ -z "${ready}" ]; then
    echo "${CONTAINER} never became ready in ${READY_TIMEOUT_SECONDS}s; see 'docker logs ${CONTAINER}'" >&2
    exit 1
fi

mkdir -p "${RUN_DIR}"
log="${RUN_DIR}/$(date -u +%Y%m%dT%H%M%SZ).log"

# `-s` so the spike's printed plans and timings reach the terminal and the log
# (they are what the story records); `-m spike` selects the marker every test
# in the module carries. The tests run in the order pytest-django settles --
# plain `django_db` cases first, the transactional purge after them, the
# printing test last -- and the module's own comment on its `pytestmark` says
# why that order is the one it needs.
DATABASE_URL="${DB_URL}" pixi run -e dev pytest "${SPIKE_MODULE}" -m spike -s 2>&1 | tee "${log}"
status=${PIPESTATUS[0]}

if [ "${status}" -eq 0 ]; then
    echo "spike-scale: the evidence tables were measured at ten thousand packages on postgres:17 (log: ${log})"
else
    echo "spike-scale: the spike failed against postgres:17 (exit ${status}; log: ${log})" >&2
fi
exit "${status}"
