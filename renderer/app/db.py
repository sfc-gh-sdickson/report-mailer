"""Snowflake access for the renderer job.

Inside SPCS the job authenticates as its service user with the OAuth token that
Snowflake mounts at /snowflake/session/token, plus SNOWFLAKE_HOST/ACCOUNT. That
token only works from inside the service, so no credentials are stored anywhere.
"""
from __future__ import annotations

import json
import logging
import os
from contextlib import contextmanager
from typing import Any, Iterator

import snowflake.connector

from .config import SETTINGS

log = logging.getLogger(__name__)

SPCS_TOKEN_PATH = "/snowflake/session/token"


def connect() -> snowflake.connector.SnowflakeConnection:
    common = dict(
        warehouse=SETTINGS.warehouse,
        database=SETTINGS.app_database,
        schema=SETTINGS.app_schema,
        session_parameters={"QUERY_TAG": f"report_mailer:{SETTINGS.run_id}"},
    )
    if os.path.exists(SPCS_TOKEN_PATH):
        with open(SPCS_TOKEN_PATH, "r", encoding="utf-8") as f:
            token = f.read()
        return snowflake.connector.connect(
            host=os.environ["SNOWFLAKE_HOST"],
            account=os.environ["SNOWFLAKE_ACCOUNT"],
            authenticator="oauth",
            token=token,
            **common,
        )
    # Local development only: a named connection from ~/.snowflake/connections.toml.
    return snowflake.connector.connect(connection_name=os.getenv("RM_LOCAL_CONNECTION", "default"), **common)


class Repo:
    """All SQL the job runs. Every statement is parameterized."""

    def __init__(self, conn: snowflake.connector.SnowflakeConnection):
        self.conn = conn

    @contextmanager
    def _cur(self) -> Iterator[Any]:
        cur = self.conn.cursor(snowflake.connector.DictCursor)
        try:
            yield cur
        finally:
            cur.close()

    def query(self, sql: str, params: tuple | None = None) -> list[dict]:
        with self._cur() as cur:
            cur.execute(sql, params or ())
            return [{k.lower(): v for k, v in row.items()} for row in cur.fetchall()]

    def execute(self, sql: str, params: tuple | None = None) -> None:
        with self._cur() as cur:
            cur.execute(sql, params or ())

    # ---- run lifecycle -------------------------------------------------
    def load_run(self, run_id: str) -> dict:
        rows = self.query(
            """
            SELECT r.run_id, r.schedule_id, r.trigger_type, r.requested_by, r.override_recipients,
                   s.name AS schedule_name, s.subject_template, s.delivery_format,
                   s.include_artifact_link, s.sf_user_channel, s.message_intro,
                   p.report_id, p.name AS report_name, p.source_type, p.source_path, p.refresh_procedure,
                   p.artifact_url, p.artifact_type, p.viewport_width, p.wait_selector, p.wait_ms,
                   p.pdf_page_format
            FROM RUNS r
            JOIN SCHEDULES s ON s.schedule_id = r.schedule_id
            JOIN REPORTS p ON p.report_id = s.report_id
            WHERE r.run_id = %s
            """,
            (run_id,),
        )
        if not rows:
            raise LookupError(f"Run {run_id} not found")
        return rows[0]

    def set_run_status(self, run_id: str, status: str, error: str | None = None,
                       output_prefix: str | None = None) -> None:
        self.execute(
            """
            UPDATE RUNS SET status = %s,
                   error_message = COALESCE(%s, error_message),
                   output_prefix = COALESCE(%s, output_prefix),
                   started_at = IFF(%s = 'RUNNING', CURRENT_TIMESTAMP(), started_at),
                   finished_at = IFF(%s IN ('SUCCEEDED','PARTIAL','FAILED'), CURRENT_TIMESTAMP(), finished_at)
            WHERE run_id = %s
            """,
            (status, (error or None) and error[:4000], output_prefix, status, status, run_id),
        )

    def log_delivery(self, run_id: str, channel: str, recipient: str, recipient_kind: str,
                     status: str, provider_message_id: str | None, detail: str | None) -> None:
        self.execute(
            """
            INSERT INTO DELIVERY_LOG (run_id, channel, recipient, recipient_kind, status,
                                      provider_message_id, detail)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (run_id, channel, recipient, recipient_kind, status, provider_message_id,
             (detail or None) and detail[:4000]),
        )

    # ---- configuration ---------------------------------------------------
    def config(self) -> dict[str, str]:
        return {r["config_key"]: r["config_value"] for r in self.query("SELECT config_key, config_value FROM APP_CONFIG")}

    # ---- recipients --------------------------------------------------------
    def schedule_recipients(self, schedule_id: int) -> list[dict]:
        return self.query(
            "SELECT recipient_type, recipient_value FROM SCHEDULE_RECIPIENTS WHERE schedule_id = %s",
            (schedule_id,),
        )

    def resolve_snowflake_users(self, user_names: list[str], role_names: list[str]) -> list[dict]:
        """Return [{user_name, email}] for named users plus every user who holds a role
        (directly or through role inheritance). ACCOUNT_USAGE has up to 2 hours of latency."""
        if not user_names and not role_names:
            return []
        users_json = json.dumps([u.upper() for u in user_names])
        roles_json = json.dumps([r.upper() for r in role_names])
        return self.query(
            """
            WITH RECURSIVE target_roles(role_name) AS (
                SELECT VALUE::STRING FROM TABLE(FLATTEN(INPUT => PARSE_JSON(%s)))
                UNION ALL
                SELECT g.grantee_name
                FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_ROLES g
                JOIN target_roles t ON g.name = t.role_name
                WHERE g.granted_on = 'ROLE' AND g.privilege = 'USAGE'
                  AND g.granted_to = 'ROLE' AND g.deleted_on IS NULL
            ),
            role_members AS (
                SELECT DISTINCT gu.grantee_name AS user_name
                FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_USERS gu
                JOIN target_roles t ON gu.role = t.role_name
                WHERE gu.deleted_on IS NULL
            ),
            named AS (
                SELECT VALUE::STRING AS user_name FROM TABLE(FLATTEN(INPUT => PARSE_JSON(%s)))
            )
            SELECT u.name AS user_name, u.email
            FROM SNOWFLAKE.ACCOUNT_USAGE.USERS u
            WHERE u.deleted_on IS NULL
              AND COALESCE(u.disabled, FALSE)::BOOLEAN = FALSE
              AND u.email IS NOT NULL
              AND u.name IN (SELECT user_name FROM role_members UNION SELECT user_name FROM named)
            """,
            (roles_json, users_json),
        )

    # ---- stage URLs ------------------------------------------------------------
    # File I/O uses SPCS stage volumes (/mnt/reports, /mnt/outputs); see renderer/spec.
    def presigned_url(self, stage: str, relative_path: str, seconds: int) -> str:
        # The stage argument must be a literal @name; it comes from configuration, never user input.
        import re
        if not re.fullmatch(r"@[A-Za-z_][A-Za-z0-9_$]*(\.[A-Za-z_][A-Za-z0-9_$]*){0,2}", stage):
            raise ValueError(f"Invalid stage name {stage!r}")
        rows = self.query(f"SELECT GET_PRESIGNED_URL({stage}, %s, %s) AS url", (relative_path, int(seconds)))
        return rows[0]["url"]

    def call_refresh_procedure(self, procedure: str) -> None:
        if not _is_safe_identifier(procedure):
            raise ValueError(f"Refusing to call unsafe procedure name: {procedure!r}")
        self.execute(f"CALL {procedure}()")

    def send_native_email(self, integration: str, address: str, subject: str, html: str) -> None:
        self.execute("CALL SYSTEM$SEND_EMAIL(%s, %s, %s, %s, 'text/html')", (integration, address, subject, html))


def _is_safe_identifier(name: str) -> bool:
    import re
    return bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*(\.[A-Za-z_][A-Za-z0-9_$]*){0,2}", name or ""))
