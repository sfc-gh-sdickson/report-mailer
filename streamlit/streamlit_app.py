"""Report Mailer - Streamlit in Snowflake UI (warehouse runtime, owner's rights).

The app only writes metadata and enqueues runs. Rendering and sending happen in
the SPCS job launched by RM_DISPATCHER_TASK (see sql/03_procedures.sql).
"""
from __future__ import annotations

import io
import json
import re
from zoneinfo import available_timezones

import streamlit as st

st.set_page_config(page_title="Report Mailer", layout="wide")

APP = "REPORT_MAILER.APP"
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
CRON_RE = re.compile(r"^[0-9A-Za-z*,/#?L-]+( [0-9A-Za-z*,/#?L-]+){4}$")
FOLDER_RE = re.compile(r"^[A-Za-z0-9_\-]{1,60}$")
FILE_RE = re.compile(r"^[A-Za-z0-9_\-. ]{1,120}\.(html?|pdf|css|js|png|jpe?g|svg|json)$", re.I)
CRON_PRESETS = {
    "Weekdays 07:00": "0 7 * * MON-FRI",
    "Mondays 08:00": "0 8 * * MON",
    "Daily 06:00": "0 6 * * *",
    "1st of month 07:00": "0 7 1 * *",
    "Custom": "",
}

session = st.connection("snowflake").session()


def viewer() -> tuple[str, str]:
    """Identity of the person using the app (the session itself runs as the owner role)."""
    u = st.user
    return (getattr(u, "user_name", None) or "UNKNOWN").upper(), (getattr(u, "email", None) or "")


def q(sql: str, params: list | None = None):
    return session.sql(sql, params=params or []).collect()


@st.cache_data(ttl=600, max_entries=4)
def directory() -> tuple[list[str], list[str]]:
    users = [r[0] for r in q("SELECT name FROM SNOWFLAKE.ACCOUNT_USAGE.USERS WHERE deleted_on IS NULL "
                              "AND COALESCE(disabled,FALSE)::BOOLEAN = FALSE AND email IS NOT NULL ORDER BY name")]
    roles = [r[0] for r in q("SELECT name FROM SNOWFLAKE.ACCOUNT_USAGE.ROLES WHERE deleted_on IS NULL ORDER BY name")]
    return users, roles


def config() -> dict[str, str]:
    return {r[0]: r[1] for r in q(f"SELECT config_key, config_value FROM {APP}.APP_CONFIG")}


user_name, user_email = viewer()
is_admin = bool(q(f"SELECT 1 FROM {APP}.APP_ADMINS WHERE user_name = ?", [user_name]))

st.title("Report Mailer")
st.caption(f"Signed in as {user_name}. Schedule HTML, PDF and artifact reports to Snowflake users and external recipients.")

tab_reports, tab_sched, tab_runs, tab_admin = st.tabs(
    ["Reports", "Schedules", "Run history", "Admin"])

# ----------------------------------------------------------------------------- Reports
with tab_reports:
    with st.form("new_report", clear_on_submit=True):
        st.subheader("Register a report")
        name = st.text_input("Report name")
        source_type = st.segmented_control("Source", ["HTML", "PDF", "ARTIFACT_LINK"], default="HTML") or "HTML"
        folder = st.text_input("Folder on @REPORT_SOURCES", help="Letters, digits, - and _. One folder per report.")
        files = st.file_uploader("Report file plus any sibling assets (CSS/JS/images)", accept_multiple_files=True,
                                 type=["html", "htm", "pdf", "css", "js", "png", "jpg", "jpeg", "svg", "json"])
        main_file = st.text_input("Main file name", help="e.g. report.html; required for HTML and PDF")
        refresh_proc = st.text_input("Refresh procedure (optional)", help="DB.SCHEMA.PROC called with no args before render")
        st.markdown("**Snowflake artifact (optional, Snowflake users only)**")
        artifact_url = st.text_input("Artifact link", help="Copy from the artifact's Share dialog")
        artifact_type = st.selectbox("Artifact type", ["", "REPORT", "DASHBOARD", "FILE", "CHART"])
        artifact_role = st.text_input("Role the artifact is shared with")
        width = st.number_input("Render width (px)", 600, 2400, 1200, 50)
        wait_selector = st.text_input("Wait for CSS selector (optional)")
        submitted = st.form_submit_button("Save report", type="primary")
    if submitted:
        errors = []
        if not name.strip():
            errors.append("Report name is required.")
        if source_type != "ARTIFACT_LINK":
            if not FOLDER_RE.match(folder or ""):
                errors.append("Folder must be 1-60 letters, digits, - or _.")
            if not FILE_RE.match(main_file or ""):
                errors.append("Main file name is invalid.")
            if main_file and main_file not in [f.name for f in files or []]:
                errors.append("Upload the main file (its name must match “Main file name”).")
        if artifact_url and not artifact_url.startswith("https://"):
            errors.append("Artifact link must start with https://")
        if refresh_proc and not re.fullmatch(r"[A-Za-z_][\w$]*(\.[A-Za-z_][\w$]*){2}", refresh_proc):
            errors.append("Refresh procedure must be a fully qualified name DB.SCHEMA.PROC.")
        for f in files or []:
            if not FILE_RE.match(f.name):
                errors.append(f"File name not allowed: {f.name}")
        if errors:
            for e in errors:
                st.error(e)
        else:
            for f in files or []:
                session.file.put_stream(io.BytesIO(f.getvalue()), f"@{APP}.REPORT_SOURCES/{folder}/{f.name}",
                                        auto_compress=False, overwrite=True)
            q(f"""INSERT INTO {APP}.REPORTS (name, source_type, source_path, refresh_procedure, artifact_url,
                     artifact_type, artifact_share_role, viewport_width, wait_selector, created_by)
                  SELECT ?,?,?,?,?,?,?,?,?,?""",
              [name.strip(), source_type, f"{folder}/{main_file}" if source_type != "ARTIFACT_LINK" else None,
               refresh_proc or None, artifact_url or None, artifact_type or None, artifact_role or None,
               int(width), wait_selector or None, user_name])
            st.success(f"Saved report “{name}”.")

    st.subheader("Registered reports")
    st.dataframe(q(f"""SELECT report_id, name, source_type, source_path, artifact_type, artifact_url, created_by,
                       created_at FROM {APP}.REPORTS WHERE is_active ORDER BY report_id DESC"""), hide_index=True)

# ----------------------------------------------------------------------------- Schedules
with tab_sched:
    reports = q(f"SELECT report_id, name, source_type FROM {APP}.REPORTS WHERE is_active ORDER BY name")
    if not reports:
        st.info("Register a report first.")
    else:
        users, roles = directory()
        cfg = config()
        allowed = [d.strip().lower() for d in cfg.get("ALLOWED_EXTERNAL_DOMAINS", "").split(",") if d.strip()]
        with st.form("new_schedule"):
            st.subheader("Create a schedule")
            rep = st.selectbox("Report", reports, format_func=lambda r: f"{r[1]} ({r[2]})")
            sname = st.text_input("Schedule name")
            preset = st.selectbox("Frequency", list(CRON_PRESETS))
            cron = st.text_input("Cron (minute hour day-of-month month day-of-week)", value=CRON_PRESETS[preset] or "")
            tzs = sorted(available_timezones())
            tz = st.selectbox("Time zone", tzs, index=tzs.index("America/Los_Angeles") if "America/Los_Angeles" in tzs else 0)
            subject = st.text_input("Subject", "{report_name} - {run_date}",
                                    help="Placeholders: {report_name} {schedule_name} {run_date} {run_datetime}")
            intro = st.text_area("Intro message (optional)")
            fmt = st.segmented_control("Deliver as", ["IMAGES", "PDF", "BOTH"], default="BOTH") or "BOTH"
            sf_channel = st.segmented_control("Snowflake users receive via", ["NATIVE", "EXTERNAL"], default="NATIVE",
                                              help="NATIVE = SYSTEM$SEND_EMAIL (verified addresses). "
                                                   "EXTERNAL = same provider as external recipients.") or "NATIVE"
            art_link = st.toggle("Include artifact link for Snowflake users", value=True)
            sf_users = st.multiselect("Snowflake users", users)
            sf_roles = st.multiselect("Snowflake roles (all members)", roles)
            ext = st.text_area("External emails (one per line)",
                               help=f"Allowed domains: {', '.join(allowed) or 'any'}")
            ok = st.form_submit_button("Create schedule", type="primary")
        if ok:
            externals = [e.strip().lower() for e in ext.splitlines() if e.strip()]
            errors = []
            if not sname.strip():
                errors.append("Schedule name is required.")
            if not CRON_RE.match(cron.strip()):
                errors.append("Cron must have 5 fields, e.g. 0 7 * * MON-FRI.")
            for e in externals:
                if not EMAIL_RE.match(e):
                    errors.append(f"Invalid email: {e}")
                elif allowed and e.split("@")[1] not in allowed:
                    errors.append(f"Domain not allowed: {e}")
            if rep[2] == "ARTIFACT_LINK" and externals:
                errors.append("Artifact-link reports can only go to Snowflake users.")
            if not (externals or sf_users or sf_roles):
                errors.append("Add at least one recipient.")
            if errors:
                for e in errors:
                    st.error(e)
            else:
                q(f"""INSERT INTO {APP}.SCHEDULES (report_id, name, cron_expr, timezone, subject_template,
                         delivery_format, include_artifact_link, sf_user_channel, message_intro, created_by)
                      SELECT ?,?,?,?,?,?,?,?,?,?""",
                  [rep[0], sname.strip(), cron.strip(), tz, subject, fmt, art_link, sf_channel, intro or None, user_name])
                sid = q(f"SELECT MAX(schedule_id) FROM {APP}.SCHEDULES WHERE created_by = ? AND name = ?",
                        [user_name, sname.strip()])[0][0]
                rows = ([("SNOWFLAKE_USER", u) for u in sf_users] + [("SNOWFLAKE_ROLE", r) for r in sf_roles]
                        + [("EXTERNAL", e) for e in externals])
                for t, v in rows:
                    q(f"INSERT INTO {APP}.SCHEDULE_RECIPIENTS (schedule_id, recipient_type, recipient_value) "
                      f"SELECT ?,?,?", [sid, t, v])
                res = q(f"CALL {APP}.UPSERT_SCHEDULE_TASK(?)", [sid])[0][0]
                (st.error if str(res).startswith("ERROR") else st.success)(f"Schedule {sid}: {res}")

        st.subheader("Schedules")
        scheds = q(f"""SELECT s.schedule_id, s.name, r.name AS report, s.cron_expr, s.timezone, s.delivery_format,
                       s.sf_user_channel, s.is_enabled, s.task_name, s.created_by
                       FROM {APP}.SCHEDULES s JOIN {APP}.REPORTS r USING (report_id) ORDER BY s.schedule_id DESC""")
        st.dataframe(scheds, hide_index=True)
        if scheds:
            pick = st.selectbox("Schedule", scheds, format_func=lambda r: f"{r[0]} - {r[1]}")
            c1, c2, c3, c4 = st.columns(4)
            with c1:
                if st.button("Send now", icon=":material/send:"):
                    rid = q(f"CALL {APP}.ENQUEUE_RUN(?, 'MANUAL', ?, NULL)", [pick[0], user_name])[0][0]
                    st.success(f"Queued run {rid}.")
            with c2:
                if st.button("Send test to me", icon=":material/science:"):
                    override = json.dumps([{"recipient_type": "SNOWFLAKE_USER", "recipient_value": user_name}])
                    rid = q(f"CALL {APP}.ENQUEUE_RUN(?, 'TEST', ?, PARSE_JSON(?))", [pick[0], user_name, override])[0][0]
                    st.success(f"Queued test run {rid} to {user_email or user_name}.")
            with c3:
                if st.button("Pause" if pick[7] else "Resume", icon=":material/pause:"):
                    q(f"UPDATE {APP}.SCHEDULES SET is_enabled = NOT is_enabled WHERE schedule_id = ?", [pick[0]])
                    q(f"CALL {APP}.UPSERT_SCHEDULE_TASK(?)", [pick[0]])
                    st.rerun()
            with c4:
                if st.button("Disable and remove task", icon=":material/delete:"):
                    q(f"CALL {APP}.DROP_SCHEDULE_TASK(?)", [pick[0]])
                    st.rerun()

# ----------------------------------------------------------------------------- Runs
with tab_runs:
    st.button("Refresh", icon=":material/refresh:")
    st.dataframe(q(f"""SELECT run_id, schedule_id, trigger_type, requested_by, status, error_message,
                       queued_at, started_at, finished_at FROM {APP}.RUNS ORDER BY queued_at DESC LIMIT 200"""),
                 hide_index=True)
    rid = st.text_input("Run id for delivery detail")
    if rid:
        st.dataframe(q(f"""SELECT channel, recipient, recipient_kind, status, provider_message_id, detail, logged_at
                           FROM {APP}.DELIVERY_LOG WHERE run_id = ? ORDER BY logged_at""", [rid.strip()]),
                     hide_index=True)

# ----------------------------------------------------------------------------- Admin
with tab_admin:
    if not is_admin:
        st.info("Only Report Mailer administrators (APP_ADMINS) can change settings.")
    else:
        cfg = config()
        with st.form("cfg"):
            doms = st.text_input("Allowed external domains (comma separated)", cfg.get("ALLOWED_EXTERNAL_DOMAINS", ""))
            maxr = st.number_input("Max recipients per run", 1, 5000, int(cfg.get("MAX_RECIPIENTS_PER_RUN", "500")))
            days = st.slider("Image/PDF link lifetime for native emails (days)", 1, 7,
                             max(1, int(cfg.get("PRESIGNED_URL_SECONDS", "604800")) // 86400))
            new_admin = st.selectbox("Add administrator", [""] + directory()[0])
            if st.form_submit_button("Save", type="primary"):
                for k, v in (("ALLOWED_EXTERNAL_DOMAINS", doms.lower().replace(" ", "")),
                             ("MAX_RECIPIENTS_PER_RUN", str(maxr)), ("PRESIGNED_URL_SECONDS", str(days * 86400))):
                    q(f"UPDATE {APP}.APP_CONFIG SET config_value = ? WHERE config_key = ?", [v, k])
                if new_admin:
                    q(f"MERGE INTO {APP}.APP_ADMINS t USING (SELECT ? u) s ON t.user_name = s.u "
                      "WHEN NOT MATCHED THEN INSERT (user_name) VALUES (s.u)", [new_admin])
                st.success("Saved.")
