#!/usr/bin/env bash
# Build the renderer image for SPCS (linux/amd64) and push it to REPORT_MAILER.APP.RM_IMAGES.
# Usage: scripts/build_push.sh <snow-connection-name> [image-tag]
# Requires: Docker Desktop (signed in, if your org enforces sign-in), Snowflake CLI (snow) >= 3.x,
#           a role with WRITE on the image repository (RM_ADMIN).
set -euo pipefail

CONN="${1:?usage: build_push.sh <connection> [tag]}"
TAG="${2:-1.0.0}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

REPO_URL="$(snow spcs image-repository url RM_IMAGES --database REPORT_MAILER --schema APP --role RM_ADMIN -c "$CONN")"
REGISTRY="${REPO_URL%%/*}"
echo "Repository: $REPO_URL"

snow spcs image-registry login --role RM_ADMIN -c "$CONN"

docker build --platform linux/amd64 -t "report-renderer:$TAG" "$ROOT/renderer"
docker tag "report-renderer:$TAG" "$REPO_URL/report-renderer:$TAG"
docker push "$REPO_URL/report-renderer:$TAG"

# Keep the job spec pointing at the tag that was pushed.
sed -i.bak -E "s#(report-renderer:)[A-Za-z0-9._-]+#\1$TAG#" "$ROOT/renderer/spec/report_job.yaml" && rm -f "$ROOT/renderer/spec/report_job.yaml.bak"
echo "Pushed $REPO_URL/report-renderer:$TAG (registry $REGISTRY). Re-upload the spec: sql/05_spcs_and_app.sql"
