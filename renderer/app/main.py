"""Job entrypoint.

SPCS:   python -m app.main            (RUN_ID comes from the job spec template)
Local:  python -m app.main --local samples/sales_weekly/report.html --out ./out --to someone@example.com
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import shutil
import sys
import traceback
from dataclasses import dataclass
from pathlib import Path

from . import compose
from .config import SETTINGS
from .mailers import FileMailer, NativeMailer, SendResult, external_mailer
from .render import RenderOptions, RenderResult, render_html, render_pdf, shrink_for_budget

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stdout)
log = logging.getLogger("report_mailer")

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
RELATIVE_PATH_RE = re.compile(r"^[A-Za-z0-9_\-./ ]+$")


@dataclass
class Recipients:
    snowflake: list[str]  # verified Snowflake user email addresses
    external: list[str]
    rejected: list[tuple[str, str]]  # (value, reason)


def split_recipients(rows: list[dict], resolve_sf, allowed_domains: list[str], max_recipients: int) -> Recipients:
    """Classify schedule recipients. Snowflake users/roles are resolved to e-mail addresses;
    external addresses must belong to an allowed domain (APP_CONFIG.ALLOWED_EXTERNAL_DOMAINS)."""
    users = [r["recipient_value"] for r in rows if r["recipient_type"] == "SNOWFLAKE_USER"]
    roles = [r["recipient_value"] for r in rows if r["recipient_type"] == "SNOWFLAKE_ROLE"]
    rejected: list[tuple[str, str]] = []

    sf_emails: list[str] = []
    resolved = resolve_sf(users, roles) if (users or roles) else []
    found_users = {u["user_name"].upper() for u in resolved}
    for u in users:
        if u.upper() not in found_users:
            rejected.append((u, "Snowflake user not found, disabled, or has no email (ACCOUNT_USAGE lags up to 2h)"))
    for u in resolved:
        e = (u["email"] or "").strip().lower()
        if EMAIL_RE.match(e) and e not in sf_emails:
            sf_emails.append(e)

    external: list[str] = []
    for r in rows:
        if r["recipient_type"] != "EXTERNAL":
            continue
        e = r["recipient_value"].strip().lower()
        domain = e.rsplit("@", 1)[-1] if "@" in e else ""
        if not EMAIL_RE.match(e):
            rejected.append((e, "Invalid email address"))
        elif allowed_domains and domain not in allowed_domains:
            rejected.append((e, f"Domain {domain} is not in ALLOWED_EXTERNAL_DOMAINS"))
        elif e in sf_emails:
            continue  # already covered as a Snowflake user
        elif e not in external:
            external.append(e)

    total = len(sf_emails) + len(external)
    if total > max_recipients:
        raise ValueError(f"{total} recipients exceeds MAX_RECIPIENTS_PER_RUN={max_recipients}")
    return Recipients(sf_emails, external, rejected)


def render_source(source_file: Path, source_type: str, out_dir: Path, report: dict, make_pdf: bool) -> RenderResult:
    opts = RenderOptions(
        viewport_width=int(report.get("viewport_width") or SETTINGS.default_viewport_width),
        device_scale_factor=SETTINGS.device_scale_factor,
        slice_height_px=SETTINGS.slice_height_px,
        timeout_ms=SETTINGS.render_timeout_ms,
        extra_wait_ms=int(report.get("wait_ms") or 750),
        wait_selector=report.get("wait_selector") or None,
        allowed_hosts=SETTINGS.render_allowed_hosts,
        make_pdf=make_pdf,
        pdf_page_format=report.get("pdf_page_format") or "SINGLE_PAGE",
    )
    if source_type == "PDF":
        return render_pdf(source_file, out_dir, opts)
    return render_html(source_file, out_dir, opts)


# --------------------------------------------------------------------------- SPCS
def run_job(run_id: str) -> int:
    from .db import Repo, connect

    conn = connect()
    repo = Repo(conn)
    try:
        run = repo.load_run(run_id)
    except Exception:
        log.exception("Cannot load run %s", run_id)
        return 2

    repo.set_run_status(run_id, "RUNNING")
    work = Path(SETTINGS.work_dir) / run_id
    try:
        cfg = repo.config()
        allowed = [d.strip().lower() for d in cfg.get("ALLOWED_EXTERNAL_DOMAINS", "").split(",") if d.strip()]
        max_recipients = int(cfg.get("MAX_RECIPIENTS_PER_RUN", "500"))
        expiry_seconds = min(int(cfg.get("PRESIGNED_URL_SECONDS", SETTINGS.presigned_url_seconds)), 604_800)

        if run.get("refresh_procedure"):
            log.info("Refreshing report via %s()", run["refresh_procedure"])
            repo.call_refresh_procedure(run["refresh_procedure"])

        # TEST runs carry their own recipient list (the requesting user); others use the schedule's list.
        override = run.get("override_recipients")
        rows = json.loads(override) if isinstance(override, str) else (override or None)
        rows = rows or repo.schedule_recipients(run["schedule_id"])
        recipients = split_recipients(rows, repo.resolve_snowflake_users, allowed, max_recipients)
        for value, reason in recipients.rejected:
            repo.log_delivery(run_id, "VALIDATION", value, "REJECTED", "SKIPPED", None, reason)

        fmt = run["delivery_format"]  # IMAGES | PDF | BOTH
        source_type = run["source_type"]  # HTML | PDF | ARTIFACT_LINK
        subject = compose.render_subject(run["subject_template"], run["report_name"], run["schedule_name"])
        intro = run.get("message_intro")
        results: list[tuple[str, str, SendResult]] = []

        render: RenderResult | None = None
        if source_type in ("HTML", "PDF"):
            source = _stage_source(run["source_path"], work / "src")
            render = render_source(source, source_type, work / "render", run, make_pdf=fmt in ("PDF", "BOTH"))
            _publish_outputs(render, run_id)
        elif recipients.external:
            raise ValueError("ARTIFACT_LINK reports have no file to render, so they cannot go to external recipients")

        # ---- external recipients (and Snowflake users when sf_user_channel = EXTERNAL) ----
        ext_targets = list(recipients.external)
        sf_via_external = run.get("sf_user_channel") == "EXTERNAL" and render is not None
        if sf_via_external:
            ext_targets += recipients.snowflake
        if ext_targets and render is not None:
            mailer = external_mailer(SETTINGS.mail_provider, SETTINGS.mail_credentials_json)
            images = [] if fmt == "PDF" else render.slices
            pdf = render.pdf if fmt in ("PDF", "BOTH") else None
            pdf_bytes = pdf.stat().st_size * 4 // 3 if pdf else 0
            images = shrink_for_budget(images, mailer.budget_bytes - pdf_bytes - 200_000) if images else images
            msg = compose.build_external(subject, run["report_name"], intro, images,
                                         SETTINGS.email_image_width_px, pdf)
            results += [(mailer.channel, "EXTERNAL", r) for r in mailer.send(msg, recipients.external)]
            if sf_via_external and recipients.snowflake:
                # Snowflake users get the same email plus a link to the live artifact.
                artifact = run["artifact_url"] if run.get("include_artifact_link") else None
                sf_msg = compose.build_external(subject, run["report_name"], intro, images,
                                                SETTINGS.email_image_width_px, pdf, artifact_url=artifact)
                results += [(mailer.channel, "SNOWFLAKE_USER", r) for r in mailer.send(sf_msg, recipients.snowflake)]

        # ---- Snowflake users through SYSTEM$SEND_EMAIL ----
        if recipients.snowflake and not sf_via_external:
            stage = SETTINGS.outputs_stage
            image_urls = []
            if render is not None and fmt != "PDF":
                image_urls = [repo.presigned_url(stage, f"{run_id}/{p.name}", expiry_seconds) for p in render.slices]
            pdf_url = None
            if render is not None and render.pdf is not None and fmt in ("PDF", "BOTH"):
                pdf_url = repo.presigned_url(stage, f"{run_id}/{render.pdf.name}", expiry_seconds)
            artifact = run["artifact_url"] if run.get("include_artifact_link") else None
            msg = compose.build_native(subject, run["report_name"], intro, image_urls, SETTINGS.email_image_width_px,
                                       artifact, pdf_url, max(1, expiry_seconds // 86_400))
            native = NativeMailer(repo, SETTINGS.native_integration)
            results += [(native.channel, "SNOWFLAKE_USER", r) for r in native.send(msg, recipients.snowflake)]

        for channel, kind, r in results:
            repo.log_delivery(run_id, channel, r.recipient, kind, "SENT" if r.ok else "FAILED",
                              r.provider_message_id, r.detail)

        sent = sum(1 for *_, r in results if r.ok)
        failed = len(results) - sent
        if not results:
            status, err = "FAILED", "No deliverable recipients (see DELIVERY_LOG for rejected entries)"
        elif failed == 0 and not recipients.rejected:
            status, err = "SUCCEEDED", None
        elif sent == 0:
            status, err = "FAILED", f"All {failed} deliveries failed"
        else:
            status, err = "PARTIAL", f"{sent} sent, {failed} failed, {len(recipients.rejected)} rejected"
        repo.set_run_status(run_id, status, err, output_prefix=run_id if render else None)
        log.info("Run %s finished: %s (%s)", run_id, status, err or "ok")
        return 0 if status != "FAILED" else 1
    except Exception as exc:
        log.exception("Run %s failed", run_id)
        repo.set_run_status(run_id, "FAILED", f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=5)}")
        return 1
    finally:
        shutil.rmtree(work, ignore_errors=True)
        conn.close()


def _stage_source(relative_path: str, dest: Path) -> Path:
    """Copy the report (and its sibling assets) from the read-only stage mount into local scratch space."""
    if not relative_path or not RELATIVE_PATH_RE.match(relative_path) or ".." in relative_path:
        raise ValueError(f"Invalid report source path {relative_path!r}")
    mount = Path(SETTINGS.reports_mount)
    src = (mount / relative_path.lstrip("/")).resolve()
    if mount.resolve() not in src.parents or not src.is_file():
        raise FileNotFoundError(f"Report source {relative_path!r} not found on @REPORT_SOURCES")
    shutil.copytree(src.parent, dest, dirs_exist_ok=True)
    return dest / src.name


def _publish_outputs(render: RenderResult, run_id: str) -> None:
    """Write rendered files to the output stage mount (whole-file writes only; stage volumes reject appends)."""
    out = Path(SETTINGS.outputs_mount) / run_id
    out.mkdir(parents=True, exist_ok=True)
    files = list(render.slices) + ([render.pdf] if render.pdf else []) + [render.full_png]
    for f in files:
        with open(f, "rb") as src, open(out / f.name, "wb") as dst:
            dst.write(src.read())


# --------------------------------------------------------------------------- local
def run_local(args: argparse.Namespace) -> int:
    source = Path(args.local)
    out = Path(args.out)
    shutil.rmtree(out, ignore_errors=True)
    source_type = "PDF" if source.suffix.lower() == ".pdf" else "HTML"
    report = {"viewport_width": args.width, "wait_ms": 750, "pdf_page_format": "SINGLE_PAGE"}
    render = render_source(source, source_type, out / "render", report, make_pdf=args.format in ("PDF", "BOTH"))
    images = [] if args.format == "PDF" else render.slices
    pdf = render.pdf if args.format in ("PDF", "BOTH") else None
    subject = compose.render_subject(None, args.name, "local-test")
    msg = compose.build_external(subject, args.name, "Weekly performance summary.", images,
                                 SETTINGS.email_image_width_px, pdf)
    results = FileMailer(out / "mail").send(msg, args.to)
    # Browser preview of exactly what recipients see (cid: references swapped for file paths).
    preview = msg.html
    for i, img in enumerate(images, start=1):
        preview = preview.replace(f"cid:report-part-{i}@report-mailer", img.resolve().as_uri())
    (out / "email_preview.html").write_text(preview, encoding="utf-8")
    log.info("Rendered %dx%d px into %d slice(s); blocked requests: %d", render.width_px, render.height_px,
             len(render.slices), len(render.blocked_requests))
    log.info("Message size %.1f KB; wrote %s", msg.size_bytes / 1024, ", ".join(r.detail for r in results))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Snowflake Report Mailer job")
    parser.add_argument("--local", help="Render a local HTML/PDF file without Snowflake")
    parser.add_argument("--out", default="./out")
    parser.add_argument("--to", nargs="*", default=["test.recipient@example.com"])
    parser.add_argument("--name", default="Local Test Report")
    parser.add_argument("--width", type=int, default=1200)
    parser.add_argument("--format", default="BOTH", choices=["IMAGES", "PDF", "BOTH"])
    args = parser.parse_args()
    if args.local:
        return run_local(args)
    if not SETTINGS.run_id:
        log.error("RUN_ID is not set")
        return 2
    return run_job(SETTINGS.run_id)


if __name__ == "__main__":
    sys.exit(main())
