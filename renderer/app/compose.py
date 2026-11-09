"""Build the email bodies.

Two variants are produced from one render:

* EXTERNAL  - images embedded as inline CID attachments (works in Outlook, Gmail,
              Apple Mail without relying on remote-image loading) and optional PDF.
              Sent through the external provider (SendGrid / Microsoft Graph).
* NATIVE    - for Snowflake users through SYSTEM$SEND_EMAIL, which accepts only an
              HTML string (max 128,000 characters, no attachments). Images are
              referenced by presigned URLs on the output stage, and the email
              carries a button to the Snowflake artifact.

All user-controlled text is HTML-escaped; links are restricted to https.
"""
from __future__ import annotations

import html
import mimetypes
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

NATIVE_MAX_CHARS = 128_000
_BRAND = "#29B5E8"
_NAVY = "#1B3A6B"


@dataclass
class Attachment:
    filename: str
    content: bytes
    mime_type: str
    content_id: str | None = None  # set => inline image referenced as cid:<content_id>

    @property
    def inline(self) -> bool:
        return self.content_id is not None


@dataclass
class EmailMessage:
    subject: str
    html: str
    text: str
    attachments: list[Attachment] = field(default_factory=list)

    @property
    def size_bytes(self) -> int:
        # Base64 inflates binary content by ~4/3; providers count encoded size.
        return len(self.html.encode()) + len(self.text.encode()) + sum(len(a.content) * 4 // 3 for a in self.attachments)


def render_subject(template: str | None, report_name: str, schedule_name: str, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    values = {
        "report_name": report_name,
        "schedule_name": schedule_name,
        "run_date": now.strftime("%Y-%m-%d"),
        "run_datetime": now.strftime("%Y-%m-%d %H:%M UTC"),
    }
    text = template or "{report_name} - {run_date}"
    # Only the documented placeholders are substituted; anything else is left literally.
    subject = re.sub(r"\{(report_name|schedule_name|run_date|run_datetime)\}", lambda m: values[m.group(1)], text)
    return re.sub(r"[\r\n]+", " ", subject).strip()[:250]


def _safe_https(url: str | None) -> str | None:
    if url and re.match(r"^https://[^\s\"'<>]+$", url):
        return url
    return None


def _shell(title: str, intro: str | None, body_rows: str, footer_note: str) -> str:
    esc_title = html.escape(title)
    intro_html = (
        f'<tr><td style="padding:0 24px 16px 24px;font:14px/1.5 Arial,Helvetica,sans-serif;color:#333333;">'
        f"{html.escape(intro)}</td></tr>"
        if intro else ""
    )
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light"><title>{esc_title}</title></head>
<body style="margin:0;padding:0;background:#f4f6f8;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#f4f6f8;">
<tr><td align="center" style="padding:24px 8px;">
<table role="presentation" width="848" cellpadding="0" cellspacing="0" border="0" style="width:100%;max-width:848px;background:#ffffff;border-radius:8px;">
<tr><td style="background:{_NAVY};padding:18px 24px;border-radius:8px 8px 0 0;font:bold 18px Arial,Helvetica,sans-serif;color:#ffffff;">{esc_title}</td></tr>
<tr><td style="height:16px;line-height:16px;font-size:0;">&nbsp;</td></tr>
{intro_html}
{body_rows}
<tr><td style="padding:16px 24px 20px 24px;font:12px/1.5 Arial,Helvetica,sans-serif;color:#777777;border-top:1px solid #e5e7eb;">{footer_note}</td></tr>
</table></td></tr></table></body></html>"""


def _image_rows(srcs: list[str], width_px: int, alt: str) -> str:
    # Strips are stacked with zero spacing so they read as one continuous report.
    imgs = "".join(
        f'<img src="{html.escape(src, quote=True)}" width="{width_px}" alt="{html.escape(alt) if i == 0 else ""}" '
        f'style="display:block;width:100%;max-width:{width_px}px;height:auto;border:0;outline:none;text-decoration:none;">'
        for i, src in enumerate(srcs)
    )
    return f'<tr><td align="center" style="padding:0 24px;font-size:0;line-height:0;">{imgs}</td></tr>'


def _button(href: str, label: str) -> str:
    return (
        '<tr><td align="left" style="padding:20px 24px 4px 24px;">'
        f'<a href="{html.escape(href, quote=True)}" style="display:inline-block;background:{_BRAND};color:#ffffff;'
        'font:bold 14px Arial,Helvetica,sans-serif;text-decoration:none;padding:12px 22px;border-radius:6px;">'
        f"{html.escape(label)}</a></td></tr>"
    )


def build_external(subject: str, report_name: str, intro: str | None, images: list[Path],
                   image_width_px: int, pdf: Path | None, artifact_url: str | None = None) -> EmailMessage:
    """artifact_url is only passed for Snowflake users; external recipients cannot open artifacts."""
    attachments: list[Attachment] = []
    srcs: list[str] = []
    for i, img in enumerate(images, start=1):
        cid = f"report-part-{i}@report-mailer"
        mime = mimetypes.guess_type(img.name)[0] or "image/png"
        attachments.append(Attachment(filename=f"report_part_{i}{img.suffix}", content=img.read_bytes(),
                                      mime_type=mime, content_id=cid))
        srcs.append(f"cid:{cid}")
    if pdf is not None:
        attachments.append(Attachment(filename=f"{_slug(report_name)}.pdf", content=pdf.read_bytes(),
                                      mime_type="application/pdf"))
    footer = "This report was generated automatically by Snowflake Report Mailer."
    if pdf is not None:
        footer += " A PDF copy is attached."
    body = _image_rows(srcs, image_width_px, report_name)
    art = _safe_https(artifact_url)
    if art:
        body = _button(art, "Open interactive report in Snowflake") + body
    return EmailMessage(subject=subject, html=_shell(report_name, intro, body, html.escape(footer)),
                        text=_plain(report_name, intro, art, pdf is not None), attachments=attachments)


def build_native(subject: str, report_name: str, intro: str | None, image_urls: list[str], image_width_px: int,
                 artifact_url: str | None, pdf_url: str | None, link_expiry_days: int) -> EmailMessage:
    rows = ""
    art = _safe_https(artifact_url)
    if art:
        rows += _button(art, "Open interactive report in Snowflake")
    rows += _image_rows(image_urls, image_width_px, report_name) if image_urls else ""
    pdf = _safe_https(pdf_url)
    if pdf:
        rows += _button(pdf, "Download PDF")
    footer = (
        "Sent by Snowflake Report Mailer. The interactive version requires you to sign in to Snowflake. "
        f"Image and PDF links in this email expire in {link_expiry_days} days. "
        "If images are hidden, choose “Download pictures” / “Show images” in your mail client."
    )
    msg = EmailMessage(subject=subject, html=_shell(report_name, intro, rows, html.escape(footer)),
                       text=_plain(report_name, intro, art, False))
    if len(msg.html) > NATIVE_MAX_CHARS:
        # Very long reports produce many presigned URLs (each ~1.5 KB). Fall back to link-only.
        rows = (_button(art, "Open interactive report in Snowflake") if art else "") + (
            _button(pdf, "Download PDF") if pdf else "")
        msg.html = _shell(report_name, intro, rows, html.escape(footer))
    return msg


def _plain(report_name: str, intro: str | None, artifact_url: str | None, has_pdf: bool) -> str:
    lines = [report_name, ""]
    if intro:
        lines += [intro, ""]
    if artifact_url:
        lines += [f"Open in Snowflake: {artifact_url}", ""]
    lines.append("This email contains the rendered report as images" + (" and an attached PDF." if has_pdf else "."))
    return "\n".join(lines)


def _slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_")[:80] or "report"
