"""Unit tests (no Snowflake, no network). Run: .venv/bin/pytest -q tests"""
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "renderer"))

from app import compose  # noqa: E402
from app.main import split_recipients  # noqa: E402
from app.mailers import GraphMailer, SendGridMailer, to_mime  # noqa: E402
from app.render import _host_allowed, shrink_for_budget, slice_image  # noqa: E402


def _rows(*pairs):
    return [{"recipient_type": t, "recipient_value": v} for t, v in pairs]


def test_split_recipients_dedupes_and_enforces_domains():
    resolve = lambda users, roles: [{"user_name": "ALICE", "email": "alice@acme.com"}]
    r = split_recipients(_rows(("SNOWFLAKE_USER", "alice"), ("SNOWFLAKE_USER", "ghost"),
                               ("EXTERNAL", "alice@acme.com"), ("EXTERNAL", "bob@partner.com"),
                               ("EXTERNAL", "eve@evil.com"), ("EXTERNAL", "not-an-email")),
                         resolve, ["acme.com", "partner.com"], 100)
    assert r.snowflake == ["alice@acme.com"]
    assert r.external == ["bob@partner.com"]
    reasons = dict(r.rejected)
    assert "ghost" in reasons and "eve@evil.com" in reasons and "not-an-email" in reasons


def test_split_recipients_max():
    with pytest.raises(ValueError):
        split_recipients(_rows(("EXTERNAL", "a@x.com"), ("EXTERNAL", "b@x.com")), lambda u, r: [], [], 1)


def test_subject_only_known_placeholders_and_no_newlines():
    s = compose.render_subject("{report_name}\r\nBcc: x {secret} {run_date}", "Sales", "Weekly",
                               datetime(2026, 11, 9, tzinfo=timezone.utc))
    assert s == "Sales Bcc: x {secret} 2026-11-09"


def test_external_message_escapes_and_uses_cids(tmp_path):
    img = tmp_path / "slice_001.png"
    Image.new("RGB", (10, 10), "white").save(img)
    m = compose.build_external("S", "<script>x</script>", "a & b", [img], 800, None,
                               artifact_url="javascript:alert(1)")
    assert "<script>" not in m.html and "&lt;script&gt;" in m.html
    assert 'src="cid:report-part-1@report-mailer"' in m.html
    assert "javascript:" not in m.html
    assert m.attachments[0].inline


def test_native_message_falls_back_to_links_when_too_long():
    urls = ["https://x.s3.amazonaws.com/" + "a" * 2000] * 80
    m = compose.build_native("S", "R", None, urls, 800, "https://app.snowflake.com/x", "https://p/pdf", 7)
    assert len(m.html) <= compose.NATIVE_MAX_CHARS
    assert "Open interactive report" in m.html and "<img" not in m.html


def test_provider_payloads(tmp_path):
    img = tmp_path / "s.png"
    Image.new("RGB", (4, 4)).save(img)
    msg = compose.build_external("S", "R", None, [img], 800, None)
    sg = SendGridMailer({"api_key": "k", "from_email": "f@x.com"}).payload(msg, ["a@x.com", "b@x.com"])
    assert len(sg["personalizations"]) == 2
    assert sg["attachments"][0]["disposition"] == "inline" and sg["attachments"][0]["content_id"]
    gr = GraphMailer({"tenant_id": "t", "client_id": "c", "client_secret": "s", "sender_upn": "f@x.com"}).payload(msg, "a@x.com")
    att = gr["message"]["attachments"][0]
    assert att["isInline"] and att["contentId"] == msg.attachments[0].content_id


def test_mime_structure(tmp_path):
    img = tmp_path / "s.png"
    Image.new("RGB", (4, 4)).save(img)
    pdf = tmp_path / "r.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    mime = to_mime(compose.build_external("S", "R", None, [img], 800, pdf), "f@x.com", "a@x.com")
    types = [p.get_content_type() for p in mime.walk()]
    assert types[:4] == ["multipart/mixed", "multipart/alternative", "text/plain", "multipart/related"]
    assert "application/pdf" in types


def test_slice_and_shrink(tmp_path):
    full = tmp_path / "full.png"
    Image.effect_noise((400, 3000), 80).convert("RGB").save(full)
    slices, (w, h) = slice_image(full, tmp_path, 1400)
    assert len(slices) == 3 and (w, h) == (400, 3000)
    png_total = sum(p.stat().st_size for p in slices)
    small = shrink_for_budget(slices, int(png_total * 0.9))
    assert all(p.suffix == ".jpg" for p in small)
    with pytest.raises(ValueError):
        shrink_for_budget(slices, 1_000)


def test_host_allowlist():
    assert _host_allowed("https://cdn.jsdelivr.net/x.js", ["cdn.jsdelivr.net"])
    assert not _host_allowed("https://evil.com/x.js", ["cdn.jsdelivr.net"])
    assert not _host_allowed("https://cdn.jsdelivr.net.evil.com/x", ["cdn.jsdelivr.net"])
    assert _host_allowed("file:///tmp/r.html", [])
