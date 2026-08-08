#!/usr/bin/env bash
# Single entrypoint for every Railway service in this project.
#
# Railway applies the repo's railway.json to ALL services, so deploy.startCommand
# overrides whatever start command a service has in the dashboard. That silently
# turned the cron services into web servers on 2026-07-15 (commit 1422d0f) and
# stopped every follow-up and reminder for four weeks.
#
# The role is chosen by the PLUMBOT_CRON env var, set per service in Railway:
#   PLUMBOT_CRON unset      -> web service (migrate, collectstatic, gunicorn)
#   PLUMBOT_CRON=<command>  -> cron service: runs `manage.py <command>` once and exits
#
# Cron ticks deliberately skip migrate/collectstatic: they'd race the web service's
# migration and waste the whole tick on a no-op collectstatic.
set -euo pipefail

if [ -n "${PLUMBOT_CRON:-}" ]; then
    echo "[start.sh] cron role: manage.py ${PLUMBOT_CRON}"
    exec python manage.py ${PLUMBOT_CRON}
fi

echo "[start.sh] web role: migrate + collectstatic + gunicorn"
python manage.py migrate --noinput
python manage.py collectstatic --noinput
exec gunicorn Plumbing_CRM.wsgi
