-- =====================================================================
-- 05: upload spec + Streamlit, create app, grant users
-- Run as: RM_ADMIN from the repo root with Snowflake CLI, e.g.
--   snow sql -f sql/05_spcs_and_app.sql -c <connection>
-- (PUT needs a client; it does not run in a Snowsight worksheet.)
-- The image must already be pushed (docs/DEPLOYMENT.md step 5).
-- =====================================================================
USE ROLE RM_ADMIN;
USE SCHEMA REPORT_MAILER.APP;

PUT file://renderer/spec/report_job.yaml @RM_SPECS AUTO_COMPRESS = FALSE OVERWRITE = TRUE;
PUT file://streamlit/streamlit_app.py   @RM_APP_CODE AUTO_COMPRESS = FALSE OVERWRITE = TRUE;
PUT file://streamlit/environment.yml    @RM_APP_CODE AUTO_COMPRESS = FALSE OVERWRITE = TRUE;

CREATE OR REPLACE STREAMLIT REPORT_MAILER_APP
  FROM @REPORT_MAILER.APP.RM_APP_CODE
  MAIN_FILE = 'streamlit_app.py'
  QUERY_WAREHOUSE = RM_WH
  TITLE = 'Report Mailer'
  COMMENT = 'Schedule HTML/PDF/artifact reports to Snowflake and external recipients';
ALTER STREAMLIT REPORT_MAILER_APP ADD LIVE VERSION FROM LAST;

GRANT USAGE ON STREAMLIT REPORT_MAILER_APP TO ROLE RM_USER;

-- First administrator (manages domains/limits in the app's Admin tab).
MERGE INTO APP_ADMINS t USING (SELECT CURRENT_USER() u) s ON t.user_name = s.u
  WHEN NOT MATCHED THEN INSERT (user_name) VALUES (s.u);

-- Sample report for the smoke test.
PUT file://samples/sales_weekly/report.html @REPORT_SOURCES/sales_weekly AUTO_COMPRESS = FALSE OVERWRITE = TRUE;
