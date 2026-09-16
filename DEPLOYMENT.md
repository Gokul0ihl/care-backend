# IHL-HIMS backend (based on CARE)

This repo is a clone of [ohcnetwork/care](https://github.com/ohcnetwork/care)
with our own patches layered on top, deployed to a single EC2 box.

## Getting the upstream source in

```sh
git clone https://github.com/ohcnetwork/care.git .
git remote rename origin upstream
git remote add origin <this Gitea repo's URL>
git push -u origin develop:main
```

From then on, pull `upstream` periodically to bring in CARE updates, and keep
our changes on top as normal commits/PRs against `main`.

Upstream already ships everything needed to run this:
- `docker/prod.Dockerfile` - the production image (Django/gunicorn on port 9000)
- `scripts/start.sh`, `scripts/celery_worker.sh`, `scripts/celery_beat.sh` -
  entrypoints for the three processes (`celery_beat.sh` also runs
  `manage.py migrate` automatically on every start)

We don't add a custom Dockerfile - `docker/prod.Dockerfile` is already correct
for this stack (Pipfile-based deps, PDF generation libs, etc).

## Local development

Follow CARE's own instructions: `make up` (docker-compose with local
Postgres/Redis/MinIO), `make load-fixtures` for test data. Each developer runs
this on their own machine - nobody develops directly against the shared EC2
box.

## Deployment architecture

One EC2 instance (t3.small) with an Elastic IP, running:
- `backend` - Django/gunicorn, port 9000
- `celery-worker`, `celery-beat` - background jobs + scheduler (also runs migrations)
- `redis` - container, not ElastiCache
- `db` - Postgres container, not RDS - see "Backups" below

All defined in `deploy/docker-compose.yml`. No ECS, no Terraform, no managed
DB - see the project's chat history / architecture discussion for why (cost:
$0 during AWS free tier, ~$10-16/mo after, vs ~$47+/mo for the ECS/Aurora
alternative).

### First-time setup

1. Edit the variables at the top of `deploy/setup-ec2.sh` (region, key pair
   name, your IP, bucket names), then run it once, locally, with AWS CLI
   configured: `bash deploy/setup-ec2.sh`. It creates the security group, IAM
   instance profile, EC2 instance, Elastic IP, and S3 buckets, and prints next
   steps.
2. SSH in, copy `deploy/docker-compose.yml` to `/opt/ihl-hims/deploy/`, copy
   `deploy/.env.example` to `/opt/ihl-hims/deploy/.env` and fill in real values.
3. `docker login` to your Gitea registry on the box (needed for
   `docker compose pull` to authenticate).
4. Add these **repo secrets** in Gitea (Settings → Actions → Secrets):
   - `EC2_HOST`, `EC2_USER` (`ec2-user`), `EC2_SSH_KEY` (the private key)
   - `GITEA_REGISTRY_USER`, `GITEA_REGISTRY_TOKEN`
5. Set up the backup cron job - see `deploy/backup-db.sh`.
6. Push to `main` - CI builds the image, pushes it to Gitea's registry, and
   deploys it to the box.

### Backups

Since Postgres is a container, not managed RDS, there's no automatic backup -
`deploy/backup-db.sh` is a daily cron job (installed manually on the box, see
the script header) that dumps the DB and uploads it to S3, keeping 14 days.

### CORS / allowed hosts

Update `DJANGO_ALLOWED_HOSTS`, `CORS_ALLOWED_ORIGINS`, and
`CSRF_TRUSTED_ORIGINS` in `.env` to match the frontend's S3 website endpoint
(see `frontend-repo`) - without this, the frontend's requests will be
rejected by the backend.

Gitea Actions reads workflows from `.gitea/workflows/` - GitHub Actions YAML
syntax and most Marketplace actions (`docker/build-push-action`,
`appleboy/ssh-action`, etc.) work as-is, as long as the Gitea Actions runner
has outbound internet access to fetch them.
