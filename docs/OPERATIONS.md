# Operations guide

## Monitoring

```sql
-- Recent runs
SELECT run_id, schedule_id, trigger_type, status, error_message, queued_at, finished_at
FROM REPORT_MAILER.APP.RUNS ORDER BY queued_at DESC LIMIT 50;

-- Failures and rejections in the last 7 days
SELECT * FROM REPORT_MAILER.APP.DELIVERY_LOG
WHERE status <> 'SENT' AND logged_at > DATEADD(day, -7, CURRENT_TIMESTAMP()) ORDER BY logged_at DESC;

-- Task health
SELECT name, state, error_message, scheduled_time
FROM TABLE(REPORT_MAILER.INFORMATION_SCHEMA.TASK_HISTORY(RESULT_LIMIT => 100)) ORDER BY scheduled_time DESC;

-- Container logs for one run (job metadata is kept for 30 days)
SELECT SYSTEM$GET_SERVICE_LOGS('REPORT_MAILER.APP.RM_JOB_<run_id>', 0, 'renderer');
```

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Run stays `QUEUED` | Dispatcher suspended, or `RM_ADMIN` lacks `EXECUTE TASK` | `ALTER TASK RM_DISPATCHER_TASK RESUME;`, then re-check the grant in `sql/01` |
| `FAILED: Launch failed: …` | Image tag missing, pool unavailable, or EAI name wrong | `SHOW IMAGES IN IMAGE REPOSITORY …`; `DESCRIBE COMPUTE POOL RM_POOL;` |
| `FAILED: Timed out` | Job crashed before updating status (OOM, image pull) | Read `SYSTEM$GET_SERVICE_LOGS`. Raise the memory limit in the spec, or use a larger instance family. |
| Charts blank or missing | A library host is not allowed | The log line *"Blocked N request(s)…"* lists the host. Add it to both `RM_RENDER_ALLOWED_HOSTS` and `RM_EGRESS_RULE`. |
| Charts half-drawn | Custom animation not frozen | Set `window.REPORT_READY = true` after drawing, or set `wait_selector` / `wait_ms`. |
| Native email `FAILED` for one user | Address not verified, or the user has no email | The user verifies their email in Snowsight. The `DELIVERY_LOG.detail` column shows the error. |
| Recipient `SKIPPED: user not found` | New user or role grant not yet in `ACCOUNT_USAGE` (up to 2 h) | Wait, or add the address as EXTERNAL (if its domain is allowed). |
| Images hidden in native email | Client blocks remote images | Set the schedule channel to **EXTERNAL**, or ask users to trust the sender. |
| SendGrid `HTTP 403` | Sender not verified | Complete Single Sender or domain authentication in SendGrid. |
| Graph `HTTP 403 ErrorAccessDenied` | `Mail.Send` application permission missing, or blocked by an Application Access Policy | Grant admin consent, and include the sender mailbox in the policy. |
| "exceed the mail size budget" | A very long report on Graph (3 MB limit) | Use format PDF, reduce the render width, split the report, or use SendGrid (28 MB budget). |

## Security notes

- The job's network access is limited to `RM_EGRESS_RULE`. The browser is further limited to `RM_RENDER_ALLOWED_HOSTS`.
- Credentials exist only in `RM_MAIL_SECRET`. They are never logged, and the app cannot read them.
- External recipients never receive artifact links or presigned URLs. Their content is embedded in the message.
- All SQL that takes user input is parameterized. Identifiers that must be inlined (cron, timezone, procedure names, stage) are regex-validated.
- To rotate credentials, run `ALTER SECRET … SET SECRET_STRING = …`. The next run picks up the new value.

## Cost and sizing

| Component | Driver | Typical |
|---|---|---|
| Compute pool `RM_POOL` (`GEN_X64_G2_4`) | Billed while nodes are active. `AUTO_SUSPEND_SECS = 300` suspends after 5 idle minutes. | A render takes about 20–60 s. Batched schedules share one wake-up. |
| Warehouse `RM_WH` (XS) | Task bodies, metadata queries and the Streamlit app | Seconds per run; 60 s auto-suspend |
| Storage | `@REPORT_OUTPUTS`, about 0.5–2 MB per run | Purge outputs older than 30 days (below) |
| Mail provider | Provider pricing | Outside Snowflake |

Credit rates per instance family are in the Snowflake Service Consumption Table. To bound monthly spend, attach a budget or resource monitor to `RM_WH`, and monitor `SNOWFLAKE.ACCOUNT_USAGE.SNOWPARK_CONTAINER_SERVICES_HISTORY` for `RM_POOL`.

Concurrency: each job requests 1 CPU and 2 GiB, so one `GEN_X64_G2_4` node runs about 2–3 jobs at once. `MAX_NODES = 2` lets bursts scale out.

## Housekeeping

```sql
-- List run folders older than 30 days, then remove each one explicitly
SELECT run_id FROM REPORT_MAILER.APP.RUNS WHERE finished_at < DATEADD(day, -30, CURRENT_TIMESTAMP());
REMOVE @REPORT_MAILER.APP.REPORT_OUTPUTS/<run_id>/;
DELETE FROM REPORT_MAILER.APP.DELIVERY_LOG WHERE logged_at < DATEADD(day, -365, CURRENT_TIMESTAMP());
```
Presigned links in native emails expire after 7 days or less, so outputs older than that are no longer referenced by any email.
