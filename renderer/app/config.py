"""Runtime configuration for the renderer job.

Values come from environment variables. In Snowpark Container Services the
SNOWFLAKE_* variables are injected automatically; RUN_ID comes from the spec
template; MAIL_PROVIDER_CREDENTIALS comes from a Snowflake secret.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return int(raw) if raw not in (None, "") else default


def _list(name: str) -> list[str]:
    raw = os.getenv(name, "")
    return [item.strip().lower() for item in raw.split(",") if item.strip()]


@dataclass(frozen=True)
class Settings:
    run_id: str = field(default_factory=lambda: os.getenv("RUN_ID", ""))
    app_database: str = field(default_factory=lambda: os.getenv("SNOWFLAKE_DATABASE", "REPORT_MAILER"))
    app_schema: str = field(default_factory=lambda: os.getenv("SNOWFLAKE_SCHEMA", "APP"))
    warehouse: str = field(default_factory=lambda: os.getenv("RM_WAREHOUSE", "RM_WH"))
    work_dir: str = field(default_factory=lambda: os.getenv("RM_WORK_DIR", "/tmp/report_mailer"))
    # Stage volume mount points (see renderer/spec/report_job.yaml).
    reports_mount: str = field(default_factory=lambda: os.getenv("RM_REPORTS_MOUNT", "/mnt/reports"))
    outputs_mount: str = field(default_factory=lambda: os.getenv("RM_OUTPUTS_MOUNT", "/mnt/outputs"))
    outputs_stage: str = field(default_factory=lambda: os.getenv("RM_OUTPUTS_STAGE", "@REPORT_MAILER.APP.REPORT_OUTPUTS"))
    presigned_url_seconds: int = field(default_factory=lambda: _int("RM_PRESIGNED_URL_SECONDS", 604_800))

    # Rendering
    default_viewport_width: int = field(default_factory=lambda: _int("RM_VIEWPORT_WIDTH", 1200))
    device_scale_factor: int = field(default_factory=lambda: _int("RM_DEVICE_SCALE_FACTOR", 2))
    slice_height_px: int = field(default_factory=lambda: _int("RM_SLICE_HEIGHT_PX", 1400))
    render_timeout_ms: int = field(default_factory=lambda: _int("RM_RENDER_TIMEOUT_MS", 90_000))
    email_image_width_px: int = field(default_factory=lambda: _int("RM_EMAIL_IMAGE_WIDTH_PX", 800))
    # Hosts the headless browser may fetch from (chart libraries, fonts). Every host here
    # must ALSO be in the RM_EGRESS_RULE network rule or the request fails at the network layer.
    render_allowed_hosts: list[str] = field(default_factory=lambda: _list("RM_RENDER_ALLOWED_HOSTS"))

    # Mail
    mail_provider: str = field(default_factory=lambda: os.getenv("RM_MAIL_PROVIDER", "SENDGRID").upper())
    mail_credentials_json: str = field(default_factory=lambda: os.getenv("MAIL_PROVIDER_CREDENTIALS", ""))
    native_integration: str = field(default_factory=lambda: os.getenv("RM_NATIVE_EMAIL_INTEGRATION", "RM_EMAIL_INT"))

    @property
    def fq_schema(self) -> str:
        return f"{self.app_database}.{self.app_schema}"


SETTINGS = Settings()
