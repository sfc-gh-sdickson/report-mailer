-- =====================================================================
-- 02: metadata tables, stream
-- Run as: RM_ADMIN
-- =====================================================================
USE ROLE RM_ADMIN;
USE SCHEMA REPORT_MAILER.APP;

CREATE TABLE IF NOT EXISTS REPORTS (
  report_id           NUMBER AUTOINCREMENT START 1 INCREMENT 1 PRIMARY KEY,
  name                STRING NOT NULL,
  source_type         STRING NOT NULL,          -- HTML | PDF | ARTIFACT_LINK
  source_path         STRING,                   -- path inside @REPORT_SOURCES, e.g. sales_weekly/report.html
  refresh_procedure   STRING,                   -- optional fully qualified proc, CALLed before rendering
  artifact_url        STRING,                   -- Snowflake artifact link (Snowflake users only)
  artifact_type       STRING,                   -- REPORT | DASHBOARD | FILE | CHART
  artifact_share_role STRING,                   -- role the artifact is shared with
  viewport_width      NUMBER DEFAULT 1200,
  wait_selector       STRING,
  wait_ms             NUMBER DEFAULT 750,
  pdf_page_format     STRING DEFAULT 'SINGLE_PAGE', -- SINGLE_PAGE | LETTER | A4
  created_by          STRING,
  created_at          TIMESTAMP_LTZ DEFAULT CURRENT_TIMESTAMP(),
  is_active           BOOLEAN DEFAULT TRUE,
  CONSTRAINT chk_src CHECK (source_type IN ('HTML','PDF','ARTIFACT_LINK')) NOT ENFORCED
);

CREATE TABLE IF NOT EXISTS SCHEDULES (
  schedule_id           NUMBER AUTOINCREMENT START 1 INCREMENT 1 PRIMARY KEY,
  report_id             NUMBER NOT NULL,
  name                  STRING NOT NULL,
  cron_expr             STRING NOT NULL,        -- 5-field cron, e.g. 0 7 * * MON
  timezone              STRING NOT NULL,        -- IANA, e.g. America/Los_Angeles
  subject_template      STRING DEFAULT '{report_name} - {run_date}',
  delivery_format       STRING DEFAULT 'BOTH',  -- IMAGES | PDF | BOTH
  include_artifact_link BOOLEAN DEFAULT TRUE,
  sf_user_channel       STRING DEFAULT 'NATIVE',-- NATIVE (SYSTEM$SEND_EMAIL) | EXTERNAL (provider)
  message_intro         STRING,
  task_name             STRING,
  is_enabled            BOOLEAN DEFAULT TRUE,
  created_by            STRING,
  created_at            TIMESTAMP_LTZ DEFAULT CURRENT_TIMESTAMP()
);

CREATE TABLE IF NOT EXISTS SCHEDULE_RECIPIENTS (
  schedule_id     NUMBER NOT NULL,
  recipient_type  STRING NOT NULL,              -- SNOWFLAKE_USER | SNOWFLAKE_ROLE | EXTERNAL
  recipient_value STRING NOT NULL
);

CREATE TABLE IF NOT EXISTS RUNS (
  run_id              STRING PRIMARY KEY,       -- 'R' + 32 hex chars (safe as a spec template value)
  schedule_id         NUMBER NOT NULL,
  trigger_type        STRING NOT NULL,          -- SCHEDULED | MANUAL | TEST
  requested_by        STRING,
  override_recipients VARIANT,                  -- TEST runs: [{recipient_type, recipient_value}]
  status              STRING DEFAULT 'QUEUED',  -- QUEUED|LAUNCHING|RUNNING|SUCCEEDED|PARTIAL|FAILED
  job_name            STRING,
  error_message       STRING,
  output_prefix       STRING,
  queued_at           TIMESTAMP_LTZ DEFAULT CURRENT_TIMESTAMP(),
  launched_at         TIMESTAMP_LTZ,
  started_at          TIMESTAMP_LTZ,
  finished_at         TIMESTAMP_LTZ
);

CREATE TABLE IF NOT EXISTS DELIVERY_LOG (
  run_id              STRING,
  channel             STRING,                   -- SENDGRID | MS_GRAPH | SNOWFLAKE_NATIVE | VALIDATION
  recipient           STRING,
  recipient_kind      STRING,                   -- EXTERNAL | SNOWFLAKE_USER | REJECTED
  status              STRING,                   -- SENT | FAILED | SKIPPED
  provider_message_id STRING,
  detail              STRING,
  logged_at           TIMESTAMP_LTZ DEFAULT CURRENT_TIMESTAMP()
);

CREATE TABLE IF NOT EXISTS APP_CONFIG (config_key STRING PRIMARY KEY, config_value STRING);
CREATE TABLE IF NOT EXISTS APP_ADMINS (user_name STRING PRIMARY KEY);
CREATE TABLE IF NOT EXISTS DISPATCH_AUDIT (run_id STRING, seen_at TIMESTAMP_LTZ DEFAULT CURRENT_TIMESTAMP());

MERGE INTO APP_CONFIG t USING (
  SELECT 'ALLOWED_EXTERNAL_DOMAINS' k, '' v UNION ALL   -- comma list; empty = any domain (not recommended)
  SELECT 'MAX_RECIPIENTS_PER_RUN', '500' UNION ALL
  SELECT 'PRESIGNED_URL_SECONDS', '604800' UNION ALL      -- max 7 days for internal stages
  SELECT 'STUCK_RUN_MINUTES', '60'
) s ON t.config_key = s.k
WHEN NOT MATCHED THEN INSERT (config_key, config_value) VALUES (s.k, s.v);

-- Wakes the dispatcher only when a run is queued.
CREATE STREAM IF NOT EXISTS RUNS_STREAM ON TABLE RUNS APPEND_ONLY = TRUE;
