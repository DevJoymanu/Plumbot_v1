#!/usr/bin/env bash
# Single entrypoint for every Railway service in this project.
#
# Railway applies the repo's railway.json to ALL services, so deploy.startCommand
# overrides whatever start command a service has in the dashboard. That silently
# turned the cron services into web servers on 2026-07-15 (commit 1422d0f) and
# stopped every follow-up and reminder for four weeks.
#
# The role is chosen by the PLUMBOT_CRON env var, set per service in Railway:
#   PLUMBOT_CRON unset       -> web service (migrate, collectstatic, gunicorn)
#   PLUMBOT_CRON=<commands>  -> cron service: runs each `manage.py <command>`
#                               once, in order, then exits. Comma-separated for
#                               a service that carries several jobs per tick.
#
# Cron ticks deliberately skip migrate/collectstatic: they'd race the web
# service's migration and waste the whole tick on a no-op collectstatic.
set -uo pipefail

if [ -n "${PLUMBOT_CRON:-}" ]; then
    status=0
    # One failing job must not stop the others on the same service, so each runs
    # in its own guarded block and the worst exit code is reported at the end.
    IFS=',' read -ra jobs <<< "$PLUMBOT_CRON"
    for job in "${jobs[@]}"; do
        job="$(echo "$job" | xargs)"   # trim surrounding whitespace
        [ -z "$job" ] && continue
        echo "[start.sh] cron job: manage.py ${job}"
        if ! python manage.py ${job}; then
            echo "[start.sh] cron job FAILED: ${job}" >&2
            status=1
        fi
    done
    exit $status
fi

set -e
echo "[start.sh] web role: migrate + collectstatic + gunicorn"
python manage.py migrate --noinput
python manage.py collectstatic --noinput
exec gunicorn Plumbing_CRM.wsgi
