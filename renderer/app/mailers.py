"""Mail delivery providers.

External (non-Snowflake) recipients:  SendGridMailer or GraphMailer, through the
RM_MAIL_EAI external access integration. Credentials come from a Snowflake secret
mounted as the MAIL_PROVIDER_CREDENTIALS environment variable (JSON).

Snowflake users:  NativeMailer -> SYSTEM$SEND_EMAIL (verified addresses only).

Local testing:   FileMailer writes RFC 822 .eml files you can open in any mail client.
"""
from __future__ import annotations

import base64
import json
import logging
import time
from dataclasses import dataclass
from email.message import EmailMessage as MimeMessage
from email.utils import make_msgid
from pathlib import Path

import requests

from .compose import EmailMessage

log = logging.getLogger(__name__)

SENDGRID_URL = "https://api.sendgrid.com/v3/mail/send"
GRAPH_TOKEN_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
GRAPH_SEND_URL = "https://graph.microsoft.com/v1.0/users/{sender}/sendMail"

# Provider payload limits (documented):
#   SendGrid v3 Mail Send: total message < 30 MB, <= 1,000 recipients per request.
#   Microsoft Graph sendMail with inline JSON attachments: attachments must be < 3 MB
#   each (larger files need an upload session), so we keep the whole message under 3 MB.
SENDGRID_BUDGET_BYTES = 28 * 1024 * 1024
GRAPH_BUDGET_BYTES = 3 * 1000 * 1000
SENDGRID_MAX_PERSONALIZATIONS = 1000


@dataclass
class SendResult:
    recipient: str
    ok: bool
    provider_message_id: str | None = None
    detail: str | None = None


def _post_with_retry(session: requests.Session, url: str, *, attempts: int = 4, **kwargs) -> requests.Response:
    """POST with exponential backoff on 429/5xx, honouring Retry-After."""
    delay = 2.0
    resp: requests.Response | None = None
    for attempt in range(1, attempts + 1):
        resp = session.post(url, timeout=60, **kwargs)
        if resp.status_code not in (429, 500, 502, 503, 504):
            return resp
        wait = float(resp.headers.get("Retry-After", delay))
        log.warning("POST %s -> %s (attempt %d/%d); retrying in %.0fs", url, resp.status_code, attempt, attempts, wait)
        time.sleep(min(wait, 60))
        delay *= 2
    assert resp is not None
    return resp


class SendGridMailer:
    channel = "SENDGRID"
    budget_bytes = SENDGRID_BUDGET_BYTES

    def __init__(self, creds: dict):
        self.api_key = creds["api_key"]
        self.from_email = creds["from_email"]
        self.from_name = creds.get("from_name", "Snowflake Report Mailer")
        self.reply_to = creds.get("reply_to")
        self.http = requests.Session()

    def payload(self, msg: EmailMessage, recipients: list[str]) -> dict:
        body = {
            # One personalization per recipient: nobody sees anyone else's address.
            "personalizations": [{"to": [{"email": r}]} for r in recipients],
            "from": {"email": self.from_email, "name": self.from_name},
            "subject": msg.subject,
            "content": [{"type": "text/plain", "value": msg.text}, {"type": "text/html", "value": msg.html}],
            "tracking_settings": {"click_tracking": {"enable": False}, "open_tracking": {"enable": False}},
        }
        if self.reply_to:
            body["reply_to"] = {"email": self.reply_to}
        if msg.attachments:
            body["attachments"] = [
                {
                    "content": base64.b64encode(a.content).decode(),
                    "type": a.mime_type,
                    "filename": a.filename,
                    "disposition": "inline" if a.inline else "attachment",
                    **({"content_id": a.content_id} if a.inline else {}),
                }
                for a in msg.attachments
            ]
        return body

    def send(self, msg: EmailMessage, recipients: list[str]) -> list[SendResult]:
        results: list[SendResult] = []
        for i in range(0, len(recipients), SENDGRID_MAX_PERSONALIZATIONS):
            batch = recipients[i:i + SENDGRID_MAX_PERSONALIZATIONS]
            resp = _post_with_retry(self.http, SENDGRID_URL, json=self.payload(msg, batch),
                                    headers={"Authorization": f"Bearer {self.api_key}"})
            ok = resp.status_code == 202
            mid = resp.headers.get("X-Message-Id")
            detail = None if ok else f"HTTP {resp.status_code}: {resp.text[:1500]}"
            results += [SendResult(r, ok, mid, detail) for r in batch]
        return results


class GraphMailer:
    channel = "MS_GRAPH"
    budget_bytes = GRAPH_BUDGET_BYTES

    def __init__(self, creds: dict):
        self.tenant = creds["tenant_id"]
        self.client_id = creds["client_id"]
        self.client_secret = creds["client_secret"]
        self.sender = creds["sender_upn"]
        self.http = requests.Session()
        self._token: str | None = None
        self._token_exp = 0.0

    def _access_token(self) -> str:
        if self._token and time.time() < self._token_exp - 120:
            return self._token
        resp = _post_with_retry(self.http, GRAPH_TOKEN_URL.format(tenant=self.tenant), data={
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "scope": "https://graph.microsoft.com/.default",
        })
        resp.raise_for_status()
        data = resp.json()
        self._token, self._token_exp = data["access_token"], time.time() + int(data.get("expires_in", 3600))
        return self._token

    def payload(self, msg: EmailMessage, recipient: str) -> dict:
        return {
            "message": {
                "subject": msg.subject,
                "body": {"contentType": "HTML", "content": msg.html},
                "toRecipients": [{"emailAddress": {"address": recipient}}],
                "attachments": [
                    {
                        "@odata.type": "#microsoft.graph.fileAttachment",
                        "name": a.filename,
                        "contentType": a.mime_type,
                        "contentBytes": base64.b64encode(a.content).decode(),
                        "isInline": a.inline,
                        **({"contentId": a.content_id} if a.inline else {}),
                    }
                    for a in msg.attachments
                ],
            },
            "saveToSentItems": False,
        }

    def send(self, msg: EmailMessage, recipients: list[str]) -> list[SendResult]:
        results: list[SendResult] = []
        url = GRAPH_SEND_URL.format(sender=self.sender)
        for r in recipients:  # one message per recipient keeps addresses private
            resp = _post_with_retry(self.http, url, json=self.payload(msg, r),
                                    headers={"Authorization": f"Bearer {self._access_token()}"})
            ok = resp.status_code == 202
            results.append(SendResult(r, ok, resp.headers.get("request-id"),
                                      None if ok else f"HTTP {resp.status_code}: {resp.text[:1500]}"))
        return results


class NativeMailer:
    """SYSTEM$SEND_EMAIL: one call per recipient so a single unverified address cannot block the rest."""
    channel = "SNOWFLAKE_NATIVE"

    def __init__(self, repo, integration: str):
        self.repo = repo
        self.integration = integration

    def send(self, msg: EmailMessage, recipients: list[str]) -> list[SendResult]:
        results = []
        for r in recipients:
            try:
                self.repo.send_native_email(self.integration, r, msg.subject, msg.html)
                results.append(SendResult(r, True))
            except Exception as exc:  # e.g. address not verified, not in ALLOWED_RECIPIENTS
                results.append(SendResult(r, False, None, str(exc)[:1500]))
        return results


class FileMailer:
    """Writes one .eml per recipient (local testing). Opening it in Apple Mail/Outlook shows the real rendering."""
    channel = "FILE"
    budget_bytes = SENDGRID_BUDGET_BYTES

    def __init__(self, out_dir: Path, from_email: str = "report-mailer@example.com"):
        self.out_dir = out_dir
        self.from_email = from_email
        out_dir.mkdir(parents=True, exist_ok=True)

    def send(self, msg: EmailMessage, recipients: list[str]) -> list[SendResult]:
        results = []
        for r in recipients:
            mime = to_mime(msg, self.from_email, r)
            path = self.out_dir / f"{r.replace('@', '_at_')}.eml"
            path.write_bytes(bytes(mime))
            results.append(SendResult(r, True, mime["Message-ID"], str(path)))
        return results


def to_mime(msg: EmailMessage, sender: str, recipient: str) -> MimeMessage:
    """multipart/alternative (text + related(html + inline images)) + mixed attachments."""
    mime = MimeMessage()
    mime["Subject"], mime["From"], mime["To"] = msg.subject, sender, recipient
    mime["Message-ID"] = make_msgid(domain="report-mailer")
    mime.set_content(msg.text)
    mime.add_alternative(msg.html, subtype="html")
    html_part = mime.get_payload()[1]
    for a in msg.attachments:
        maintype, subtype = a.mime_type.split("/", 1)
        if a.inline:
            html_part.add_related(a.content, maintype=maintype, subtype=subtype, cid=f"<{a.content_id}>",
                                  filename=a.filename, disposition="inline")
    for a in msg.attachments:
        if not a.inline:
            maintype, subtype = a.mime_type.split("/", 1)
            mime.add_attachment(a.content, maintype=maintype, subtype=subtype, filename=a.filename)
    return mime


def external_mailer(provider: str, credentials_json: str):
    if not credentials_json:
        raise RuntimeError("MAIL_PROVIDER_CREDENTIALS is empty; check the RM_MAIL_SECRET secret and the job spec")
    creds = json.loads(credentials_json)
    if provider == "SENDGRID":
        return SendGridMailer(creds)
    if provider == "MS_GRAPH":
        return GraphMailer(creds)
    raise ValueError(f"Unknown RM_MAIL_PROVIDER {provider!r} (expected SENDGRID or MS_GRAPH)")
