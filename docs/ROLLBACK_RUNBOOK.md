# Rollback Runbook — restoring single-tenant Plumbot

Step-by-step instructions to bring back the current (pre-multi-tenant) version of
Plumbot if the multi-tenant changes need to be abandoned or paused. Written
2026-07-15, before Phase 0 started.

**Anchors (the state this runbook restores):**
- Git tag: `pre-multitenant` (create it in Step 0 — the last single-tenant commit)
- Migration high-water mark: `bot/migrations/0039_appointment_booked_at`
- Railway: same service, same env vars (multi-tenant work never removes the
  Homebase env vars — they remain the seed values — so old code always finds them)

---

## Step 0 — Do this BEFORE Phase 0 starts (one-time safety net)

1. **Commit all pending work** so the tag lands on a clean tree:
   ```
   git status                # nothing should be modified/untracked that matters
   git add -A && git commit  # (with the gate passing)
   ```
2. **Tag the last single-tenant commit and push the tag:**
   ```
   git tag -a pre-multitenant -m "Last single-tenant Plumbot before multi-tenant migration"
   git push origin pre-multitenant
   ```
3. **Back up the production database** (from a machine with the Railway `DATABASE_URL`,
   or via `railway run`):
   ```
   pg_dump "$DATABASE_URL" -Fc -f plumbot_pre_multitenant_$(date +%Y%m%d).dump
   ```
   Store the dump somewhere off-Railway (local disk + a cloud drive).
4. **Snapshot the env vars** for reference:
   ```
   railway variables > env_snapshot_pre_multitenant.txt   # keep OUT of git
   ```
5. **Repeat 3 (fresh dump) and add a phase tag (`mt-phase-0`, `mt-phase-1`, …)
   at the end of every completed phase** — then rollback can target any
   intermediate point, not only the very beginning.

---

## Choosing the rollback level

| Situation | Level |
|---|---|
| Only code deployed since the tag — no new migrations have run on prod | **1 — code only** |
| Tenant migrations (0040+) have run, but the `tenant` FK is still **nullable** (early Phase 0) | **1** (old code coexists fine) or **2** if you want the schema gone |
| The `tenant` FK is **non-null** (Phase 0 complete or later) | **2 — code + reverse migrations** |
| Database is corrupted / migrations can't reverse cleanly | **3 — restore from dump (last resort)** |

How to tell what's on prod: `railway run python manage.py showmigrations bot | tail -20`
— anything after `0039_appointment_booked_at` with an `[X]` is multi-tenant schema.

---

## Level 1 — Roll back code only

Use when no multi-tenant migration has run in production, or the tenant FK is
still nullable (old code simply ignores the extra tables/columns).

1. **Fastest (no git surgery): redeploy the old build from Railway.**
   Railway dashboard → the Plumbot service → **Deployments** → find the deployment
   built from the `pre-multitenant` commit → ⋮ menu → **Redeploy**. Done in ~2 min.
2. **Durable (make git match): revert on main** — do this after (or instead of) 1,
   never force-push:
   ```
   git checkout main
   git revert --no-commit pre-multitenant..HEAD
   git commit -m "revert: roll back multi-tenant work to pre-multitenant"
   git push        # Railway auto-deploys the reverted code
   ```
3. Run the **verification checklist** (bottom of this doc).

---

## Level 2 — Roll back code AND schema

Use once the `tenant` FK is non-null (old code's INSERTs would fail against that
column). **Do this on a Saturday or after 18:00** — Homebase is closed, so no
inbound messages will hit the brief inconsistent window. Order matters:

1. **Redeploy the old code first** (Level 1, step 1). Old code reads fine against
   the newer schema; only *new-lead inserts* would fail while the non-null tenant
   column still exists — which is why you do this during closed hours.
2. **Immediately reverse the migrations** back to the high-water mark:
   ```
   railway run python manage.py migrate bot 0039_appointment_booked_at
   ```
   This drops the tenant tables and columns. (Build rule for the multi-tenant
   work, recorded here so it stays true: every schema/data migration must be
   reversible — additive migrations reverse automatically; data migrations get an
   explicit `reverse_code`, at minimum `RunPython.noop`.)
3. If a migration refuses to reverse, do **not** improvise on prod — go to Level 3.
4. Make git match prod (Level 1, step 2), then run the **verification checklist**.

Note: reversing drops the tenant metadata (tenant rows, per-tenant config) but no
customer data — leads/conversations/bookings live in the original tables and are
untouched by removing a `tenant` column.

---

## Level 3 — Restore the database from a dump (LAST RESORT)

⚠️ **This loses every lead, conversation, and booking received after the dump was
taken.** Only for a corrupted DB or an un-reversible migration. Prefer the newest
phase-boundary dump over the original one.

1. Redeploy the matching code first: for the `pre-multitenant` dump use the
   `pre-multitenant` deployment; for an `mt-phase-N` dump use that phase's tag.
2. Restore:
   ```
   pg_restore --clean --if-exists --no-owner -d "$DATABASE_URL" plumbot_pre_multitenant_YYYYMMDD.dump
   ```
3. Restart the Railway service (dashboard → ⋮ → Restart) so no stale connections
   or cached state survive.
4. Run the **verification checklist**.

---

## Verification checklist (after ANY rollback)

1. `railway logs` — clean startup, no tracebacks, webhook worker up.
2. **Live bot check:** WhatsApp the bot from a 999 test line — greeting flows,
   a price question answers correctly, the lead appears on the dashboard.
   Purge the test lead afterwards (Test Leads page).
3. **Dashboard check:** log in; leads list, detail page, and one export load.
4. **Gate check** (local, against the rolled-back code):
   ```
   git checkout pre-multitenant
   PLUMBOT_GATE=1 python tests/test_bot_responses.py
   python manage.py test bot
   ```
5. **Crons:** confirm the five cron jobs are still attached to the service in
   Railway and check the next `send_followups` run logs cleanly.
6. **Meta webhook:** nothing to change — the URL and verify token never change in
   the multi-tenant plan; just confirm an inbound message logs a webhook hit.

---

## Hotfixes — "the bot replied wrong, fix it now" during the migration

Bot-behaviour fixes don't stop for the migration. Two situations:

### While the migration is live on prod (the normal case)

Nothing special. Main is always deployable (every session ends with the gate
green and Homebase behaving identically), so a hotfix is just:

1. Fix the behaviour on `main`, **with a TEST 0 pin (and/or a scenario) for the
   bug** — same convention as always. The pin is not optional here: it's what
   guarantees the fix survives later Phase 2 extraction of that code path.
2. Commit (gate runs), push, deploy. Migration work resumes on top.

A migration session in progress but not finished? The uncommitted migration work
stays local/stashed; the hotfix commits and deploys alone. Never bundle a hotfix
into a half-done migration commit.

### While ROLLED BACK (prod runs the old version)

The trap: fixing prod's old code while `main` holds the multi-tenant work, then
losing the fix at roll-forward. Avoid it like this:

1. **If the rollback will last more than a day, use the git-revert form of
   Level 1** (not just a Railway redeploy) so `main`'s tip *is* what prod runs.
   Hotfixes are then ordinary commits on `main`: fix + TEST 0 pin, push, deploy.
2. At roll-forward, "revert the revert" re-applies the multi-tenant work **on
   top of** the hotfix commits — the fix is retained. If the hotfix touched
   lines the multi-tenant work also changed, git surfaces a conflict to resolve
   once, and the TEST 0 pin proves the behaviour survived.
3. If the rollback was Railway-redeploy-only (git untouched) and a hotfix is
   needed, branch from the tag — `git checkout -b hotfix/<bug> pre-multitenant`
   — deploy that branch, and cherry-pick the fix onto `main` immediately so the
   two lines never silently diverge.

### After Phase 2 lands, many of these stop being code fixes

Once scripts/prices/FAQ live in tenant config, a growing share of "the bot said
the wrong thing" becomes a **config edit in the platform console — no deploy at
all**, effective on the next message. Code deploys remain only for genuine
logic/flow bugs, which still follow the fix + TEST 0 pin + deploy loop above.

---

## Resuming (roll-forward) after a rollback

A rollback pauses the migration; it never deletes it. The multi-tenant code stays
in git history (the revert commits and/or the `mt-phase-N` tags). To resume:

**Rule for any Level 2 rollback:** take a fresh `pg_dump` *immediately before*
reversing migrations. Reversal drops the tenant tables — including any manually
entered tenant config (a second tenant's prices/profile/intake). The pre-rollback
dump is what lets roll-forward recover that config instead of retyping it.

- **After Level 1 via Railway redeploy only (git untouched):** nothing to undo —
  Railway dashboard → Deployments → redeploy the latest build. Done.
- **After Level 1 via git revert:** revert the revert —
  ```
  git revert --no-commit <first-revert-commit>..HEAD   # or revert the single revert commit
  git commit -m "resume: roll forward to multi-tenant work"
  git push                                             # Railway auto-deploys
  ```
- **After Level 2:** redeploy the multi-tenant code (as above), then re-apply the
  schema: `railway run python manage.py migrate`. The seed/backfill migrations
  re-create the `homebase` tenant from env + sales profile and re-assign **every**
  row — including leads that arrived while running the old version — so nothing
  needs manual fixing. Then restore any *other* tenants' config from the
  pre-rollback dump (or re-run their intake forms).
- **After Level 3:** redeploy the multi-tenant code and `migrate`. Leads received
  between the restored dump and the rollback are gone permanently (that was the
  Level 3 trade-off); everything after resuming accrues normally.

While rolled back, multi-tenant development can continue on a branch — fix
whatever prompted the rollback, prove it in TEST 0 / Scenario Lab, then roll
forward. Rollback and development are independent.

**Verification after roll-forward:** same checklist as below, plus
`showmigrations bot` shows the tenant migrations `[X]` again and the platform
console lists `homebase` as active.

---

## What you never need to touch during rollback

- **Meta / WhatsApp configuration** — same webhook URL, same verify token, same
  phone number throughout the migration.
- **Env vars** — Homebase credentials stay in env as seed values in every phase.
- **DeepSeek / SendGrid / Brevo keys** — platform-level, unchanged.
- **Domain / Railway service shape** — one service, one Postgres, before and after.
