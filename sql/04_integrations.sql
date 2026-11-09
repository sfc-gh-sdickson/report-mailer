-- =====================================================================
-- 04: email integration, egress, secret, compute pool, tasks
-- Run as: RM_ADMIN (granted CREATE INTEGRATION / COMPUTE POOL in 01).
-- Choose ONE mail provider block (SendGrid or Microsoft Graph).
-- =====================================================================
USE ROLE RM_ADMIN;
USE SCHEMA REPORT_MAILER.APP;

-- Native email for Snowflake users (verified addresses in this account only).
CREATE NOTIFICATION INTEGRATION IF NOT EXISTS RM_EMAIL_INT
  TYPE = EMAIL ENABLED = TRUE
  COMMENT = 'Report Mailer: SYSTEM$SEND_EMAIL to Snowflake users';

-- Egress for the renderer job: mail provider + chart CDNs used by reports.
-- Keep this list identical to RM_RENDER_ALLOWED_HOSTS in renderer/spec/report_job.yaml.
CREATE OR REPLACE NETWORK RULE RM_EGRESS_RULE
  MODE = EGRESS TYPE = HOST_PORT
  VALUE_LIST = (
    'api.sendgrid.com:443',                 -- SendGrid
    -- 'graph.microsoft.com:443', 'login.microsoftonline.com:443',   -- Microsoft Graph (instead of SendGrid)
    'cdn.jsdelivr.net:443', 'cdnjs.cloudflare.com:443', 'unpkg.com:443', 'cdn.plot.ly:443',
    'fonts.googleapis.com:443', 'fonts.gstatic.com:443'
  );

-- Mail provider credentials as one JSON string.
--   SendGrid : {"api_key":"SG....","from_email":"reports@yourco.com","from_name":"Reports","reply_to":"bi@yourco.com"}
--   Graph    : {"tenant_id":"...","client_id":"...","client_secret":"...","sender_upn":"reports@yourco.com"}
CREATE SECRET IF NOT EXISTS RM_MAIL_SECRET
  TYPE = GENERIC_STRING
  SECRET_STRING = '{"api_key":"REPLACE_ME","from_email":"reports@example.com","from_name":"Snowflake Reports"}';

CREATE EXTERNAL ACCESS INTEGRATION IF NOT EXISTS RM_MAIL_EAI
  ALLOWED_NETWORK_RULES = (REPORT_MAILER.APP.RM_EGRESS_RULE)
  ALLOWED_AUTHENTICATION_SECRETS = (REPORT_MAILER.APP.RM_MAIL_SECRET)
  ENABLED = TRUE
  COMMENT = 'Report Mailer renderer egress';

-- Compute pool for render jobs. GEN_X64_G2_4 = 3 vCPU / 13 GiB (AWS current generation).
-- Use CPU_X64_S on accounts where GEN_X64_G2 is not offered (SHOW COMPUTE POOL INSTANCE FAMILIES).
CREATE COMPUTE POOL IF NOT EXISTS RM_POOL
  MIN_NODES = 1 MAX_NODES = 2
  INSTANCE_FAMILY = GEN_X64_G2_4
  AUTO_RESUME = TRUE INITIALLY_SUSPENDED = TRUE AUTO_SUSPEND_SECS = 300
  COMMENT = 'Report Mailer render jobs';

-- Dispatcher: fires only when RUNS_STREAM has new rows.
CREATE OR REPLACE TASK RM_DISPATCHER_TASK
  WAREHOUSE = RM_WH
  WHEN SYSTEM$STREAM_HAS_DATA('REPORT_MAILER.APP.RUNS_STREAM')
  AS CALL REPORT_MAILER.APP.LAUNCH_QUEUED_RUNS();

CREATE OR REPLACE TASK RM_RECONCILER_TASK
  WAREHOUSE = RM_WH
  SCHEDULE = '15 MINUTE'
  AS CALL REPORT_MAILER.APP.RECONCILE_RUNS();

ALTER TASK RM_DISPATCHER_TASK RESUME;
ALTER TASK RM_RECONCILER_TASK RESUME;
