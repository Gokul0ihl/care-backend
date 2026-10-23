#!/usr/bin/env bash
# Builds the backend image from the checkout in SRC_DIR and moves the EC2
# compose stack onto it, rolling back if it doesn't come up healthy.
#
# Run (as ec2-user, over SSM Run Command) by .github/workflows/deploy-ec2.yml
# after it checks out the commit.
# By hand on the box (check out first, then run, never the other way round:
# git replacing this file while bash is reading it breaks the run):
#   cd /opt/ihl-hims/source && git fetch origin main && git checkout --detach FETCH_HEAD
#   bash deploy/deploy.sh
set -euo pipefail

DEPLOY_DIR="${DEPLOY_DIR:-/opt/ihl-hims/deploy}"
# Optional per-box overrides of the settings below (e.g. BACKUP_S3_URI).
if [[ -f "$DEPLOY_DIR/deploy.env" ]]; then
  # shellcheck source=/dev/null
  source "$DEPLOY_DIR/deploy.env"
fi
SRC_DIR="${SRC_DIR:-/opt/ihl-hims/source}"
IMAGE_REPO="${IMAGE_REPO:-ihl-hims-backend}"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:9001/ping/}"
WAIT_TIMEOUT="${WAIT_TIMEOUT:-600}"
BACKUP_DIR="${BACKUP_DIR:-/opt/ihl-hims/backups}"
KEEP_BACKUPS="${KEEP_BACKUPS:-5}"
BACKUP_S3_URI="${BACKUP_S3_URI:-}"

log() { printf '\n==> %s\n' "$*"; }

exec 9>"$DEPLOY_DIR/.deploy.lock"
if ! flock -n 9; then
  echo "Another deploy is already running on this box" >&2
  exit 1
fi

cd "$SRC_DIR"
sha="$(git rev-parse HEAD)"
image="$IMAGE_REPO:$sha"

log "Building $image"
docker build --file docker/prod.Dockerfile --build-arg APP_VERSION="$sha" --tag "$image" .

cd "$DEPLOY_DIR"
env_file=.env
previous="$(sed -n 's/^BACKEND_IMAGE=//p' "$env_file" | tail -n 1)"

set_image() {
  if grep -q '^BACKEND_IMAGE=' "$env_file"; then
    sed -i "s|^BACKEND_IMAGE=.*|BACKEND_IMAGE=$1|" "$env_file"
  else
    printf 'BACKEND_IMAGE=%s\n' "$1" >> "$env_file"
  fi
}

# Ask the NEW image which migrations it would apply (core apps and the
# care_emergency plug alike), without starting it. A missing dependency, such
# as a plug migration needing a core migration this build doesn't have, fails
# here, before anything running is touched.
log "Checking migrations for $image"
# BACKEND_IMAGE from the environment must win over the old value in .env, or
# the check below would test the running code instead of the new build.
images="$(BACKEND_IMAGE="$image" docker compose config --images)"
if ! grep -qxF "$image" <<< "$images"; then
  echo "!! docker compose would not use $image for the migration check; nothing was changed" >&2
  exit 1
fi
if ! plan="$(BACKEND_IMAGE="$image" docker compose run --rm --no-deps -T --entrypoint "" \
    backend python manage.py migrate --plan 2>&1)"; then
  printf '%s\n' "$plan" >&2
  echo "!! Migration check failed; nothing was changed" >&2
  exit 1
fi

if grep -q "No planned migration operations" <<< "$plan"; then
  log "No pending migrations"
else
  printf '%s\n' "$plan"
  plug_steps="$(grep -c '^care_emergency\.' <<< "$plan" || true)"
  log "Pending migrations, $plug_steps of them from care_emergency. Backing up the database first"
  mkdir -p "$BACKUP_DIR"
  dump="$BACKUP_DIR/pre-deploy-$(date -u +%Y%m%dT%H%M%SZ)-${sha:0:12}.dump"
  # The dump holds patient data: readable by this user only.
  if ! (umask 077 && docker compose exec -T db \
      sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom' > "$dump.partial"); then
    rm -f "$dump.partial"
    echo "!! Backup failed; not deploying" >&2
    exit 1
  fi
  mv "$dump.partial" "$dump"
  echo "Saved $dump ($(du -h "$dump" | cut -f1))"
  if [[ -n "$BACKUP_S3_URI" ]]; then
    aws s3 cp --only-show-errors --sse AES256 "$dump" "${BACKUP_S3_URI%/}/$(basename "$dump")"
    echo "Copied to $BACKUP_S3_URI"
  fi
  ls -1t "$BACKUP_DIR"/pre-deploy-*.dump | tail -n +"$((KEEP_BACKUPS + 1))" | xargs -r rm -f
fi

compose_changed=0
# Line endings are ignored: copies uploaded from Windows have CRLF.
if ! diff -q --strip-trailing-cr "$SRC_DIR/deploy/docker-compose.yml" docker-compose.yml > /dev/null; then
  log "docker-compose.yml changed in the repo; updating it (old copy in docker-compose.yml.bak)"
  cp docker-compose.yml docker-compose.yml.bak
  cp "$SRC_DIR/deploy/docker-compose.yml" docker-compose.yml
  compose_changed=1
fi

# --wait blocks until every service with a healthcheck is healthy. celery-beat
# runs migrations before it reports healthy, and the backend waits for it.
start() { docker compose up -d --wait --wait-timeout "$WAIT_TIMEOUT"; }
healthy() { curl -fsS --max-time 10 "$HEALTH_URL" > /dev/null; }

log "Starting $image (previous: ${previous:-none})"
set_image "$image"
if start && healthy; then
  log "Deployed $sha"
else
  echo "!! $image did not come up healthy" >&2
  docker compose ps || true
  # The CI log is public (public repo) and app logs can hold patient data, so
  # keep them on the box, readable by this user only.
  failed_log="$DEPLOY_DIR/last-failed-deploy.log"
  (umask 077 && docker compose logs --tail 200 backend celery-beat celery-worker > "$failed_log" 2>&1) || true
  echo "!! Container logs saved on the box to $failed_log" >&2
  if [[ -n "$previous" && "$previous" != "$image" ]]; then
    log "Rolling back to $previous"
    if [[ "$compose_changed" == 1 ]]; then
      cp docker-compose.yml.bak docker-compose.yml
    fi
    set_image "$previous"
    # Migrations already applied by the new image are not undone. If the old
    # image can't run on the new schema, restore the pre-deploy dump by hand.
    start || echo "!! Rollback did not come up healthy either; check the box" >&2
  fi
  exit 1
fi

# Redeploying the same commit leaves .env with no record of the older rollback
# image, so don't prune tagged images in that case.
if [[ "$previous" != "$image" ]]; then
  log "Pruning images (keeping current $image and rollback ${previous:-none})"
  docker image ls "$IMAGE_REPO" --format '{{.Repository}}:{{.Tag}}' \
    | grep -vxF -e "$image" -e "${previous:-$image}" \
    | xargs -r docker image rm > /dev/null 2>&1 || true
fi
docker image prune -f > /dev/null
docker builder prune -f --filter until=168h > /dev/null || true
