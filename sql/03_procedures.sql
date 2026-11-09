-- =====================================================================
-- 03: procedures
-- Owner's-rights procedures may run SELECT/DML/DDL/GRANT/SHOW only, so the
-- Streamlit app (owner's rights) never launches jobs itself. It enqueues a
-- row in RUNS; the triggered dispatcher task (04) launches the SPCS job with
-- the caller's-rights LAUNCH_QUEUED_RUNS().
-- Run as: RM_ADMIN
-- =====================================================================
USE ROLE RM_ADMIN;
USE SCHEMA REPORT_MAILER.APP;

-- Queue one run. Used by schedule tasks and by the Streamlit app.
CREATE OR REPLACE PROCEDURE ENQUEUE_RUN(P_SCHEDULE_ID NUMBER, P_TRIGGER STRING, P_REQUESTED_BY STRING,
                                        P_OVERRIDE VARIANT)
RETURNS STRING
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
DECLARE
  v_run_id STRING DEFAULT 'R' || REPLACE(UUID_STRING(), '-', '');
BEGIN
  IF (:P_TRIGGER NOT IN ('SCHEDULED','MANUAL','TEST')) THEN
    RETURN 'ERROR: invalid trigger';
  END IF;
  INSERT INTO RUNS (run_id, schedule_id, trigger_type, requested_by, override_recipients)
    SELECT :v_run_id, :P_SCHEDULE_ID, :P_TRIGGER, :P_REQUESTED_BY, :P_OVERRIDE;
  RETURN :v_run_id;
END;
$$;

-- Create or replace the per-schedule task. Cron and timezone are validated
-- with strict patterns because they must be embedded literally in the DDL.
CREATE OR REPLACE PROCEDURE UPSERT_SCHEDULE_TASK(P_SCHEDULE_ID NUMBER)
RETURNS STRING
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
DECLARE
  v_cron STRING; v_tz STRING; v_enabled BOOLEAN; v_task STRING; v_sql STRING;
BEGIN
  SELECT cron_expr, timezone, is_enabled INTO :v_cron, :v_tz, :v_enabled
    FROM SCHEDULES WHERE schedule_id = :P_SCHEDULE_ID;
  IF (NOT REGEXP_LIKE(:v_cron, '^[0-9A-Za-z*,/#?L-]+( [0-9A-Za-z*,/#?L-]+){4}$')) THEN
    RETURN 'ERROR: invalid cron expression';
  END IF;
  IF (NOT REGEXP_LIKE(:v_tz, '^[A-Za-z][A-Za-z0-9_+/-]{1,40}$')) THEN
    RETURN 'ERROR: invalid timezone';
  END IF;
  v_task := 'REPORT_MAILER.APP.RM_SCHEDULE_' || :P_SCHEDULE_ID::STRING;
  v_sql := 'CREATE OR REPLACE TASK ' || :v_task ||
           ' WAREHOUSE = RM_WH SCHEDULE = ''USING CRON ' || :v_cron || ' ' || :v_tz || '''' ||
           ' USER_TASK_TIMEOUT_MS = 300000' ||
           ' AS CALL REPORT_MAILER.APP.ENQUEUE_RUN(' || :P_SCHEDULE_ID::STRING || ', ''SCHEDULED'', ''TASK'', NULL)';
  EXECUTE IMMEDIATE :v_sql;
  IF (:v_enabled) THEN
    EXECUTE IMMEDIATE 'ALTER TASK ' || :v_task || ' RESUME';
  END IF;
  UPDATE SCHEDULES SET task_name = :v_task WHERE schedule_id = :P_SCHEDULE_ID;
  RETURN :v_task;
END;
$$;

CREATE OR REPLACE PROCEDURE DROP_SCHEDULE_TASK(P_SCHEDULE_ID NUMBER)
RETURNS STRING
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
BEGIN
  EXECUTE IMMEDIATE 'DROP TASK IF EXISTS REPORT_MAILER.APP.RM_SCHEDULE_' || :P_SCHEDULE_ID::STRING;
  UPDATE SCHEDULES SET task_name = NULL, is_enabled = FALSE WHERE schedule_id = :P_SCHEDULE_ID;
  RETURN 'dropped';
END;
$$;

-- Launch every queued run as an asynchronous SPCS job. Caller's rights:
-- invoked only by RM_DISPATCHER_TASK, which runs as RM_ADMIN.
CREATE OR REPLACE PROCEDURE LAUNCH_QUEUED_RUNS()
RETURNS STRING
LANGUAGE SQL
EXECUTE AS CALLER
AS
$$
DECLARE
  launched INTEGER DEFAULT 0;
  c CURSOR FOR SELECT run_id FROM RUNS WHERE status = 'QUEUED' ORDER BY queued_at LIMIT 50;
BEGIN
  -- Consume the stream so the triggered task does not fire again for the same rows.
  INSERT INTO DISPATCH_AUDIT (run_id) SELECT run_id FROM RUNS_STREAM;
  FOR r IN c DO
    LET rid STRING := r.run_id;
    IF (REGEXP_LIKE(:rid, '^R[0-9a-f]{32}$')) THEN
      LET job STRING := 'REPORT_MAILER.APP.RM_JOB_' || :rid;
      UPDATE RUNS SET status = 'LAUNCHING', job_name = :job, launched_at = CURRENT_TIMESTAMP()
        WHERE run_id = :rid AND status = 'QUEUED';
      BEGIN
        EXECUTE IMMEDIATE
          'EXECUTE JOB SERVICE IN COMPUTE POOL RM_POOL NAME = ' || :job ||
          ' ASYNC = TRUE QUERY_WAREHOUSE = RM_WH EXTERNAL_ACCESS_INTEGRATIONS = (RM_MAIL_EAI)' ||
          ' FROM @REPORT_MAILER.APP.RM_SPECS SPECIFICATION_TEMPLATE_FILE = ''report_job.yaml''' ||
          ' USING (run_id => ''' || :rid || ''')';
        launched := launched + 1;
      EXCEPTION
        WHEN OTHER THEN
          UPDATE RUNS SET status = 'FAILED', finished_at = CURRENT_TIMESTAMP(),
                 error_message = 'Launch failed: ' || :SQLERRM WHERE run_id = :rid;
      END;
    END IF;
  END FOR;
  RETURN 'launched ' || :launched;
END;
$$;

-- Mark runs that never reached a terminal state (image pull error, pool
-- capacity, container crash before status update) as FAILED.
CREATE OR REPLACE PROCEDURE RECONCILE_RUNS()
RETURNS STRING
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
DECLARE
  mins INTEGER;
BEGIN
  SELECT COALESCE(TRY_TO_NUMBER(MAX(config_value)), 60) INTO :mins FROM APP_CONFIG WHERE config_key = 'STUCK_RUN_MINUTES';
  UPDATE RUNS SET status = 'FAILED', finished_at = CURRENT_TIMESTAMP(),
         error_message = COALESCE(error_message, 'Timed out; check SYSTEM$GET_SERVICE_LOGS(''' || job_name || ''', 0, ''renderer'')')
   WHERE status IN ('LAUNCHING','RUNNING')
     AND DATEADD(minute, :mins, COALESCE(started_at, launched_at)) < CURRENT_TIMESTAMP();
  RETURN 'reconciled';
END;
$$;
