#!/usr/bin/env bash
# Daily Postgres backup -> S3. Since Postgres runs as a plain container
# (not managed RDS), this replaces automatic managed backups.
#
# Install on the EC2 box:
#   crontab -e
#   0 2 * * * /opt/ihl-hims/deploy/backup-db.sh >> /var/log/ihl-hims-backup.log 2>&1
#
# Requires the EC2 instance role to have s3:PutObject/s3:ListBucket on
# BACKUP_BUCKET (SETUP_GUIDE.md Part 4 already grants this on the uploads
# bucket - backups are stored there under a db-backups/ prefix).

set -euo pipefail

COMPOSE_DIR="/opt/ihl-hims/deploy"
BACKUP_BUCKET="ihl-hims-dev-uploads"   # must match the uploads bucket from SETUP_GUIDE.md Part 3
RETENTION_DAYS=14

cd "$COMPOSE_DIR"
set -a; source .env; set +a

TIMESTAMP=$(date +%Y%m%d-%H%M%S)
BACKUP_FILE="ihl-hims-db-${TIMESTAMP}.dump"

docker compose exec -T db pg_dump -U "${POSTGRES_USER:-postgres}" -Fc "${POSTGRES_DB:-ihl_hims}" > "/tmp/${BACKUP_FILE}"
aws s3 cp "/tmp/${BACKUP_FILE}" "s3://${BACKUP_BUCKET}/db-backups/${BACKUP_FILE}"
rm -f "/tmp/${BACKUP_FILE}"

# Prune backups older than RETENTION_DAYS
CUTOFF=$(date -d "-${RETENTION_DAYS} days" +%Y%m%d)
aws s3api list-objects-v2 --bucket "$BACKUP_BUCKET" --prefix "db-backups/" --query 'Contents[].Key' --output text \
  | tr '\t' '\n' \
  | while read -r key; do
      [ -z "$key" ] && continue
      FILE_DATE=$(echo "$key" | grep -oE '[0-9]{8}-[0-9]{6}' | cut -c1-8 || true)
      if [ -n "$FILE_DATE" ] && [ "$FILE_DATE" -lt "$CUTOFF" ]; then
        aws s3 rm "s3://${BACKUP_BUCKET}/${key}"
      fi
    done

echo "Backup complete: ${BACKUP_FILE}"
