"""Generates docs/images/*.svg (Foster Farms diagram style). Run: python3 scripts/gen_diagrams.py"""
from __future__ import annotations

from html import escape
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "docs" / "images"
C = {  # fill, light, dark
    "blue": ("#29b5e8", "#eaf7fc", "#0e7fad"),
    "green": ("#00a843", "#edf9f0", "#007a31"),
    "purple": ("#7b61ff", "#f3f0ff", "#5a3ec8"),
    "orange": ("#e8530e", "#fff3ed", "#b33f08"),
    "navy": ("#1B3A6B", "#eef2f8", "#1B3A6B"),
    "gray": ("#64748b", "#f8fafc", "#334155"),
}


class Svg:
    def __init__(self, w: int, h: int, title: str, subtitle: str):
        self.w, self.h, self.parts = w, h, []
        self.parts.append(f'<rect width="{w}" height="{h}" fill="#ffffff"/>')
        self.text(w / 2, 40, title, 24, "#1a1a2e", bold=True)
        self.text(w / 2, 64, subtitle, 13, "#666")

    def text(self, x, y, s, size=11, fill="#444", bold=False, anchor="middle"):
        b = ' font-weight="bold"' if bold else ""
        self.parts.append(f'<text x="{x}" y="{y}" text-anchor="{anchor}" fill="{fill}" font-size="{size}"{b}>{escape(s)}</text>')

    def solid(self, x, y, w, h, color, title, sub=None, num=None):
        f, light, _ = C[color]
        self.parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="10" fill="{f}" filter="url(#sh)"/>')
        cy = y + h / 2 + (-4 if sub else 5)
        self.text(x + w / 2, cy, title, 14, "#fff", bold=True)
        if sub:
            self.text(x + w / 2, cy + 18, sub, 11, "#f0f9ff")
        if num:
            self.badge(x, y, num, color)

    def card(self, x, y, w, h, color, title, sub, lines, num=None):
        f, light, dark = C[color]
        self.parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="10" fill="{light}" stroke="{f}" stroke-width="1.5" filter="url(#sh)"/>')
        self.text(x + w / 2, y + 24, title, 14, dark, bold=True)
        if sub:
            self.text(x + w / 2, y + 41, sub, 11, f)
        top = y + (51 if sub else 36)
        self.parts.append(f'<line x1="{x + 10}" y1="{top}" x2="{x + w - 10}" y2="{top}" stroke="{f}" stroke-opacity="0.35" stroke-width="1"/>')
        for i, ln in enumerate(lines):
            self.text(x + 14, top + 19 + i * 18, ln, 11, "#444", anchor="start")
        if num:
            self.badge(x, y, num, color)

    def badge(self, x, y, num, color):
        f = C[color][0]
        self.parts.append(f'<circle cx="{x}" cy="{y}" r="13" fill="#fff" stroke="{f}" stroke-width="2"/>')
        self.text(x, y + 4.5, str(num), 12, f, bold=True)

    def zone(self, x, y, w, h, label):
        self.parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="14" fill="#f7fcfe" stroke="#29b5e8" stroke-width="2" stroke-dasharray="8,5"/>')
        self.text(x + w - 18, y + 24, label, 13, "#0e7fad", bold=True, anchor="end")

    def path(self, pts, color="gray", label=None, dashed=False, label_at=None):
        f = C[color][0]
        d = " ".join(f"{'M' if i == 0 else 'L'}{px},{py}" for i, (px, py) in enumerate(pts))
        dash = ' stroke-dasharray="5,3"' if dashed else ""
        self.parts.append(f'<path d="{d}" fill="none" stroke="{f}" stroke-width="2"{dash}/>')
        (x1, y1), (x2, y2) = pts[-2], pts[-1]
        if x1 == x2:
            s = 1 if y2 > y1 else -1
            head = f"{x2 - 5},{y2 - 9 * s} {x2 + 5},{y2 - 9 * s} {x2},{y2}"
        else:
            s = 1 if x2 > x1 else -1
            head = f"{x2 - 9 * s},{y2 - 5} {x2 - 9 * s},{y2 + 5} {x2},{y2}"
        self.parts.append(f'<polygon points="{head}" fill="{f}"/>')
        if label:
            lx, ly = label_at
            width = 7 * len(label) + 12
            self.parts.append(f'<rect x="{lx - width / 2}" y="{ly - 12}" width="{width}" height="17" rx="4" fill="#fff"/>')
            self.text(lx, ly, label, 10.5, C[color][2])

    def save(self, name):
        head = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {self.w} {self.h}" '
                'font-family="Segoe UI, Arial, sans-serif">\n  <defs>\n'
                '    <filter id="sh"><feDropShadow dx="1" dy="2" stdDeviation="3" flood-color="#000" flood-opacity="0.12"/></filter>\n'
                '  </defs>\n')
        (OUT / name).write_text(head + "\n".join("  " + p for p in self.parts) + "\n</svg>\n", encoding="utf-8")


def architecture():
    s = Svg(1240, 910, "Report Mailer - Architecture",
            "Streamlit schedules · Tasks queue · SPCS renders charts headlessly · native + external email")
    s.solid(470, 88, 300, 52, "navy", "Report authors (Snowsight)", "role RM_USER")
    s.zone(30, 168, 1180, 470, "SNOWFLAKE ACCOUNT  ·  REPORT_MAILER.APP")
    # row of numbered stages
    s.card(60, 210, 250, 170, "blue", "Streamlit app", "REPORT_MAILER_APP · owner's rights",
           ["Register HTML / PDF / artifact", "Schedules + recipients", "Send now / test to me", "Run history, admin settings"], 1)
    s.card(350, 210, 250, 170, "purple", "Metadata", "tables + append-only stream",
           ["REPORTS · SCHEDULES", "SCHEDULE_RECIPIENTS", "RUNS  →  RUNS_STREAM", "DELIVERY_LOG · APP_CONFIG"], 2)
    s.card(640, 210, 250, 170, "green", "Tasks", "warehouse RM_WH",
           ["RM_SCHEDULE_<id>  (CRON)", "  → ENQUEUE_RUN()", "RM_DISPATCHER_TASK (stream)", "  → LAUNCH_QUEUED_RUNS()"], 3)
    s.card(930, 210, 250, 170, "orange", "SPCS render job", "compute pool RM_POOL",
           ["Headless Chromium (Playwright)", "Charts → PNG slices + PDF", "Compose CID / native email", "Send + log each recipient"], 4)
    s.path([(470, 114), (185, 114), (185, 210)], "navy")
    s.path([(310, 295), (350, 295)], "blue", None)
    s.path([(600, 295), (640, 295)], "purple")
    s.path([(890, 295), (930, 295)], "green")
    # supporting row
    s.card(60, 430, 340, 180, "blue", "Stages (SNOWFLAKE_SSE)", "mounted as SPCS stage volumes",
           ["@REPORT_SOURCES  → /mnt/reports  (source of record)", "@REPORT_OUTPUTS  → /mnt/outputs  (per run id)",
            "@RM_SPECS  report_job.yaml template", "Image repo RM_IMAGES  report-renderer:1.0.0"])
    s.card(440, 430, 360, 180, "purple", "Integrations + secret", "least-privilege egress",
           ["RM_EMAIL_INT  (TYPE = EMAIL)", "RM_EGRESS_RULE  mail API + chart CDNs", "RM_MAIL_EAI  external access integration",
            "RM_MAIL_SECRET  provider JSON credentials"])
    s.card(840, 430, 340, 180, "green", "Directory lookups", "SNOWFLAKE.SECURITY_VIEWER",
           ["ACCOUNT_USAGE.USERS  (emails)", "GRANTS_TO_USERS / GRANTS_TO_ROLES", "Roles expanded recursively", "Latency up to 2 hours"])
    s.path([(1055, 380), (1055, 400), (230, 400), (230, 430)], "orange", "reads source / writes output", label_at=(640, 404))
    s.path([(1120, 380), (1120, 430)], "orange")
    # deliveries
    s.card(60, 690, 340, 170, "blue", "Snowflake users", "SYSTEM$SEND_EMAIL (verified emails)",
           ["Button: open the artifact (live / snapshot)", "Images via 7-day presigned URLs", "PDF download link",
            "Or same provider email if channel = EXTERNAL"])
    s.card(450, 690, 340, 170, "orange", "External recipients", "SendGrid or Microsoft Graph",
           ["Charts as inline CID PNG images", "PDF attached (optional)", "Domain allowlist enforced twice",
            "No artifact link (needs Snowflake login)"])
    s.card(840, 690, 340, 170, "purple", "Snowflake artifact", "Workspaces / CoCo publish",
           ["Report / File: static snapshot", "Dashboard: live data", "Shared to a role; same link on re-publish",
            "Publishing is manual (no SQL API yet)"])
    s.path([(1180, 330), (1196, 330), (1196, 650), (230, 650), (230, 690)], "orange", "native", label_at=(400, 654))
    s.path([(620, 650), (620, 690)], "orange")
    s.text(675, 676, "provider API via EAI", 10.5, "#b33f08")
    s.path([(1010, 860), (1010, 882), (230, 882), (230, 860)], "purple", "artifact link inside the Snowflake-user email",
           dashed=True, label_at=(620, 886))
    s.save("architecture.svg")


def deployment_flow():
    s = Svg(1000, 820, "Deployment Flow", "Run in order · each step lists the script and the role that runs it")
    steps = [
        ("blue", "Roles, warehouse, database, stages", "sql/01_roles_db.sql · ACCOUNTADMIN"),
        ("purple", "Metadata tables + RUNS_STREAM", "sql/02_tables.sql · RM_ADMIN"),
        ("purple", "Stored procedures", "sql/03_procedures.sql · RM_ADMIN"),
        ("green", "Email int., egress, secret, EAI, pool, tasks", "sql/04_integrations.sql · ACCOUNTADMIN → RM_ADMIN"),
        ("orange", "Build + push renderer image (linux/amd64)", "scripts/build_push.sh · Docker + snow CLI"),
        ("blue", "Upload spec + Streamlit, create app", "sql/05_spcs_and_app.sql · snow sql -f"),
        ("green", "Set secret, domains, admins; smoke test", "docs/DEPLOYMENT.md §8 · RM_ADMIN"),
    ]
    y = 100
    for i, (col, title, sub) in enumerate(steps, start=1):
        x = 120 if i % 2 else 480
        s.solid(x, y, 400, 70, col, title, sub, num=i)
        if i < len(steps):
            nx = 480 if i % 2 else 120
            if i % 2:  # right-down L
                s.path([(x + 400, y + 35), (nx + 200, y + 35), (nx + 200, y + 100)], col)
            else:      # left-down L
                s.path([(x, y + 35), (nx + 200, y + 35), (nx + 200, y + 100)], col)
        y += 100
    s.save("deployment_flow.svg")


def delivery_sequence():
    s = Svg(1240, 640, "Delivery Sequence (one run)", "Owner's-rights code only enqueues; the caller's-rights dispatcher launches the job")
    row1 = [("navy", "Trigger", "CRON task or Send now"), ("purple", "ENQUEUE_RUN()", "RUNS row · status QUEUED"),
            ("green", "Dispatcher task", "WHEN STREAM_HAS_DATA"), ("orange", "EXECUTE JOB SERVICE", "ASYNC · run_id template")]
    row2 = [("orange", "Refresh + render", "optional proc · Chromium"), ("blue", "Publish outputs", "@REPORT_OUTPUTS/<run_id>/"),
            ("orange", "Send", "native + provider"), ("purple", "Log + status", "DELIVERY_LOG · RUNS")]
    for i, (col, t, sub) in enumerate(row1):
        x = 40 + i * 300
        s.solid(x, 110, 250, 74, col, t, sub, num=i + 1)
        if i < 3:
            s.path([(x + 250, 147), (x + 300, 147)], col)
    s.path([(1065, 184), (1065, 230), (165, 230), (165, 290)], "orange")
    for i, (col, t, sub) in enumerate(row2):
        x = 40 + i * 300
        s.solid(x, 290, 250, 74, col, t, sub, num=i + 5)
        if i < 3:
            s.path([(x + 250, 327), (x + 300, 327)], col)
    s.card(40, 420, 360, 190, "gray", "Status values", None,
           ["QUEUED → LAUNCHING → RUNNING", "→ SUCCEEDED | PARTIAL | FAILED", "PARTIAL = some sent, some failed/rejected",
            "RECONCILE_RUNS every 15 min fails stuck runs", "Job metadata kept 30 days"])
    s.card(440, 420, 360, 190, "gray", "Readiness before screenshot", None,
           ["load + networkidle", "optional wait_selector", "window.REPORT_READY === true, or", "500 ms of DOM quiet (30 s cap)",
            "animations frozen (CSS + Chart.js/ECharts/HC)"])
    s.card(840, 420, 360, 190, "gray", "Failure handling", None,
           ["Provider 429/5xx: retry with backoff", "One native call per recipient", "Launch errors → RUNS.error_message",
            "Logs: SYSTEM$GET_SERVICE_LOGS", "Run history tab in the app"])
    s.save("delivery_sequence.svg")


def recipient_routing():
    s = Svg(1240, 700, "Recipient Routing", "Every recipient is classified, validated, and logged")
    s.solid(470, 95, 300, 56, "navy", "SCHEDULE_RECIPIENTS", "or TEST override (requesting user)")
    s.solid(80, 210, 320, 66, "blue", "SNOWFLAKE_USER / SNOWFLAKE_ROLE", "resolve via ACCOUNT_USAGE")
    s.solid(840, 210, 320, 66, "orange", "EXTERNAL email", "regex + ALLOWED_EXTERNAL_DOMAINS")
    s.path([(570, 151), (570, 180), (240, 180), (240, 210)], "navy")
    s.path([(670, 151), (670, 180), (1000, 180), (1000, 210)], "navy")
    s.solid(80, 340, 320, 66, "green", "sf_user_channel ?", "set per schedule")
    s.path([(240, 276), (240, 340)], "blue")
    s.card(40, 470, 330, 190, "blue", "NATIVE", "SYSTEM$SEND_EMAIL · RM_EMAIL_INT",
           ["Verified emails in this account only", "Max 128,000 chars HTML, no attachments", "Artifact button + presigned images",
            "PDF as presigned link (≤ 7 days)", "Remote images may need 'show images'"])
    s.card(420, 470, 360, 190, "orange", "EXTERNAL provider (Snowflake users)", "SendGrid / Microsoft Graph",
           ["Same CID email as external recipients", "Plus artifact button", "Use when users' mail blocks remote images",
            "Counts toward provider quota"])
    s.card(830, 470, 370, 190, "orange", "External recipients", "SendGrid / Microsoft Graph",
           ["Inline CID PNG slices (no remote images)", "PDF attached if format PDF / BOTH", "Never receives the artifact link",
            "Budget: SendGrid 28 MB · Graph 3 MB", "Images re-encoded JPEG if over budget"])
    s.path([(160, 406), (160, 470)], "green", "NATIVE", label_at=(160, 442))
    s.path([(320, 406), (320, 430), (600, 430), (600, 470)], "green", "EXTERNAL", label_at=(460, 434))
    s.path([(1000, 276), (1000, 470)], "orange", "allowed", label_at=(1000, 380))
    s.solid(560, 290, 230, 60, "gray", "Rejected → SKIPPED", "logged with reason")
    s.path([(840, 243), (675, 243), (675, 290)], "gray", dashed=True)
    s.path([(400, 243), (620, 243), (620, 290)], "gray", dashed=True)
    s.save("recipient_routing.svg")


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    architecture(); deployment_flow(); delivery_sequence(); recipient_routing()
    print("wrote", sorted(p.name for p in OUT.glob("*.svg")))
