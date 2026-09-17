# Runbook

For whoever is holding this at 07:00. Every alert `/ops/summary` can raise has a section here with the same
name, so a page hands you an answer rather than a number.

**The one thing to know first:** this system proposes, it never acts. Nothing reaches a tracker without a
person pressing approve in the console, and nothing writes to M3 at all. If you are unsure whether it has done
something, the answer is almost always no.

---

## Is it working?

```
curl -s -H "X-API-Key: $KEY" $API/ops/summary | jq '.alerts'
```

Empty list means: briefs are running, the model is being used, nothing is stuck waiting, nothing failed at the
tracker. The console's **Ops** page shows the same thing with the tables behind it.

---

## Alerts

### no-briefs

**Means:** no brief has been produced for more than two days.
**Why it matters:** this is the loudest thing the system can say. A missing brief is a silent failure — nobody
notices that nothing arrived until the week somebody needed it.

1. Is the scheduler running? On Railway: the cron service's last run and its logs.
2. Run it by hand and read the output:
   ```
   python scripts/morning.py --date 2026-09-15
   ```
3. If it fails on metrics — `metrics.daily_otif_total is empty` — gold or the metric layer has not run:
   ```
   make migrate && make metrics
   ```
4. If it fails on the database, check `DATABASE_URL` and that migrations are applied (`python scripts/migrate.py`).
5. If it builds by hand but not on schedule, the problem is the scheduler, not this system. The brief for a
   missed day can always be built later: `--date` takes any date the metrics cover.

### narration-falling-back

**Means:** more than half of recent briefs were the templated version rather than the model's.
**Why it matters:** the briefs are still correct — the template is validated like everything else — but they
read flatter, and the rate is telling you the model or the validator is unhappy.

1. Look at *why*: `select run_date, fallback_reason from ops.runs where narrated_by = 'fallback' order by run_date desc limit 10;`
2. `model failed twice: 429` or `5xx` → the provider is rate-limiting or down. Nothing to fix here; it will
   recover. If it persists, `LLM_PROVIDER=none` makes the fallback explicit and stops paying for failures.
3. `validator rejected both attempts` → read the complaints in the same column. If the same complaint recurs,
   that is a prompt or validator problem worth a change, not an incident.
4. Nothing here is urgent at 07:00. The brief went out.

### approvals-piling-up

**Means:** actions have been waiting more than three days for somebody to approve or reject them.
**Why it matters:** an approval nobody gave is usually a brief nobody read. The system will keep proposing the
same things and suppressing the duplicates, so it looks quiet while nothing happens.

1. `select * from ops.v_pending_approvals order by days_waiting desc;`
2. Ask the named `assignee_hint` whether they are seeing the brief at all — check `ops.v_delivery` for failed
   sends to their audience.
3. Rejecting is a perfectly good answer. Actions do not expire on their own by design: silence is not consent.

### action-failed

**Means:** the tracker refused an action (a 4xx). It will **not** be retried.
**Why it matters:** something about the request is wrong — a project key, a permission, a field the tracker
requires — and retrying a wrong request forever is how a queue fills with identical failures.

1. `select * from ops.v_action_failures where status = 'FAILED';` — `last_error` has the tracker's own words.
2. Fix the configuration (`JIRA_PROJECT_KEY`, credentials, permissions) and re-approve from the console.
3. If the ticket was in fact created and only the response failed, close the duplicate in the tracker — this
   system has no way to know, which is why it does not guess.

### action-retrying

**Means:** a tracker call timed out or returned a 5xx. The action stays open and the next run tries again.
**Why it matters:** usually nothing. It is here so that "it will sort itself out" is a decision somebody made
rather than an assumption.

1. If it clears on the next run, there is nothing to do.
2. If the same action retries for days, treat it as `action-failed` and look at `last_error`.

---

## Routine operations

### Run a brief by hand

```
python scripts/morning.py --date 2026-09-15                 # build and deliver
python scripts/morning.py --date 2026-09-15 --no-deliver    # build only
```

Idempotent by date and audience: running it twice returns the same brief rather than making a second one.

### Rebuild the metrics

```
make migrate      # schema
make metrics      # gold -> metrics.daily_*
make replay       # detection, scored against ground truth
```

### Change a threshold

Edit `policy.yaml` — never the code. The levels there were set from the measured distribution of each series
(see `docs/decisions.md` B-006); if you move one, re-run `make replay` and look at what it did to precision
and recall before you keep it.

### Turn the model off

`LLM_PROVIDER=none`. Briefs keep going out, templated and validated, and `ops.v_narration_health` shows 100%
fallback so nobody mistakes it for normal.

### Point it at a different tracker

`JIRA_MODE=live` plus `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN`, `JIRA_PROJECT_KEY`. It refuses to start
without all four, rather than quietly writing to the mock and looking like it worked.

---

## The deployment

Railway project `m3-supply-chain-command-center`, four services from one image:

| Service | Role | What it is |
|---|---|---|
| `Postgres` | — | the database: `ops`, `metrics`, `gold`, `graph_checkpoints` |
| `app` | `APP_ROLE` unset | the API, at https://app-production-d624.up.railway.app |
| `console` | `APP_ROLE=console` | the Streamlit console, at https://console-production-c572.up.railway.app |
| `cron` | `APP_ROLE=cron` | runs `scripts/morning.py` once and exits — a cron service that stays up is one that ran once |

`METRICS_SOURCE=postgres`, `JIRA_MODE=mock`, `DELIVERY_MODE=none`. The image generates its own gold at build
time, so a deployment needs no data uploaded — but the metric *tables* have to be filled once:

```
railway ssh --service app "python scripts/run_metrics.py"
```

`railway run` is the wrong tool for that: it runs the command on **your** machine with the deployment's
variables, and `postgres.railway.internal` is not reachable from there. `railway ssh` runs it inside the
container, which is where the private network is. From a workstation with a public database URL, the
equivalent is `python scripts/run_metrics.py --database-url <public url>` — the repo's `.env` wins over the
shell by design, so the URL has to be passed rather than exported.

### Two things the CLI cannot do

1. **The cron schedule.** Railway sets it per service in the dashboard: `cron` → Settings → Cron Schedule,
   e.g. `0 6 * * 1-5` for weekday mornings. Without it the service deploys, runs once and stops.
2. **Deploy on green.** CI has a deploy job gated on `vars.RAILWAY_DEPLOY == 'true'` and
   `secrets.RAILWAY_TOKEN`. Create a project token in the dashboard (Settings → Tokens), then:
   ```
   gh secret set RAILWAY_TOKEN        # paste the token when prompted
   gh variable set RAILWAY_DEPLOY --body true
   ```
   Set the variable *after* the secret: turning it on without a working token makes every push to `main`
   fail at the deploy step.

## What to check after a deploy

```
curl -s $API/healthz                                  # 200 {"status":"ok"}
curl -s -H "X-API-Key: $KEY" $API/ops/summary | jq .alerts
python scripts/morning.py --date <a date with metrics> --no-deliver
```

Then open the console, build a brief, and approve one action against `JIRA_MODE=mock`. If a ticket appears in
`ops.mock_jira`, every layer between the metrics and the tracker is working.

---

## Things that are not incidents

- **A quiet brief.** "Nothing above the reporting level" is a real answer, and the brief says which kind of
  quiet it was.
- **A holiday with big movements.** Policy caps anything inside a holiday window at WARN; the brief names the
  shutdown before the numbers.
- **Backlog never going HIGH.** Deliberate: the measurement could not separate a real surge from ordinary
  swings, so there is no HIGH rule for it (`docs/decisions.md` B-006). It is watched and reported at WARN.
- **A blocked action.** Policy refused something the system wanted to propose. It is shown in the console on
  purpose, so a policy that has drifted from what the business needs is visible.
