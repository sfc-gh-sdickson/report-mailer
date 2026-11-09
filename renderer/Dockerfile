# Report renderer + mailer job for Snowpark Container Services.
# SPCS nodes are linux/amd64, so always build with:  docker build --platform linux/amd64 ...
# The base image ships Chromium and every OS library Playwright needs; its tag
# MUST match the playwright version pinned in requirements.txt.
FROM mcr.microsoft.com/playwright/python:v1.64.1-noble-amd64

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /opt/renderer

COPY requirements.txt .
RUN pip install --no-cache-dir --break-system-packages -r requirements.txt

COPY app/ ./app/

# Job entrypoint. RUN_ID is injected by the SPCS spec template.
ENTRYPOINT ["python", "-m", "app.main"]
