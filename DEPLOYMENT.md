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
for this stack (uv-locked deps, PDF generation libs, etc).

## Local development

Follow CARE's own instructions: `make up` (docker-compose with local
Postgres/Redis/MinIO), `make load-fixtures` for test data. Each developer runs
this on their own machine - nobody develops directly against the shared EC2
box.

## Deployment architecture

One EC2 instance (t2.small: 1 vCPU, 2 GB RAM, burstable CPU credits) running:
- `backend` - Django/gunicorn, port 9000
- `celery-worker`, `celery-beat` - background jobs + scheduler (also runs migrations)
- `redis` - container, not ElastiCache
- `db` - Postgres container, not RDS - see "Backups" below

All defined in `deploy/docker-compose.yml`. No ECS, no Terraform, no managed
DB - see the project's chat history / architecture discussion for why (cost:
$0 during AWS free tier, ~$10-16/mo after, vs ~$47+/mo for the ECS/Aurora
alternative).

### Deploy pipeline (GitHub Actions)

`.github/workflows/deploy-ec2.yml` deploys on every push to `main` (doc-only
changes are skipped), or by hand from **Actions → Deploy backend to EC2 → Run
workflow** with a branch, tag or full commit SHA. There is no image registry:

1. After approval (see below), the job gets temporary AWS credentials through
   GitHub OIDC and sends an **SSM Run Command** to the instance. There is no
   SSH: the SSM agent on the box picks the command up over an outbound
   connection, so port 22 doesn't need to be open to GitHub. The command runs
   as `ec2-user`, checks the commit out in `/opt/ihl-hims/source` (cloned if
   missing) and runs `deploy/deploy.sh`. The checkout is left on a detached
   commit, so for a manual build run `git checkout main && git pull` there
   rather than plain `git pull`. Any uncommitted edits in that folder are
   discarded. The full output goes to `/opt/ihl-hims/deploy/last-deploy.log`;
   the job shows its last 20,000 characters once the command finishes (SSM
   doesn't stream output).
2. `deploy.sh` builds `docker/prod.Dockerfile` as `ihl-hims-backend:<sha>`.
   It then runs `manage.py migrate --plan` with the new image, before
   starting it. If that errors (for example a `hmis` migration needs
   a core migration this build doesn't have), the deploy stops and nothing
   changes. If any migrations are pending, core or hmis, it first takes a
   `pg_dump` to `/opt/ihl-hims/backups` (keeps the last 5, owner-only
   permissions; set `BACKUP_S3_URI` in `/opt/ihl-hims/deploy/deploy.env` to
   also copy it to S3).
   Next it copies `deploy/docker-compose.yml` over the box's copy if it changed, sets
   `BACKEND_IMAGE` in `/opt/ihl-hims/deploy/.env`, and runs
   `docker compose up -d --wait`. `celery-beat` runs migrations before it
   reports healthy, and the backend waits for it.
3. If the stack isn't healthy within 10 minutes, or `/ping/` doesn't answer,
   it switches `BACKEND_IMAGE` back to the previous image, restarts it and
   fails the job. Migrations the new image applied are **not** rolled back.
   If the old image can't run on the new schema, restore the pre-deploy dump:
   `docker compose exec -T db pg_restore -U postgres -d ihl_hims --clean < /opt/ihl-hims/backups/<file>.dump`.
4. It keeps exactly two images, the current one and the previous one (for
   rollback), and removes every other `ihl-hims-backend` image and old build
   cache.

Only one deploy runs at a time: the workflow has a concurrency group, and the
script takes a lock file on the box. To roll back by hand to the previous release,
run the workflow with its commit SHA. Its image is still on the box, so the
build is mostly cache hits. Any older release has to be rebuilt.

**One-time setup.** Steps 1–3 are in AWS, 4 on the box, 5 in GitHub.

1. **GitHub as an identity provider.** IAM → Identity providers → Add
   provider → OpenID Connect, URL `https://token.actions.githubusercontent.com`,
   audience `sts.amazonaws.com`.
2. **Let the box talk to SSM.** Attach the AWS managed policy
   `AmazonSSMManagedInstanceCore` to the instance role `ihl-hims-ec2-role`.
   The SSM agent is preinstalled on Amazon Linux; within a few minutes the
   instance shows as *Online* in Systems Manager → Fleet Manager. If it
   doesn't, restart the agent: `sudo systemctl restart amazon-ssm-agent`.
3. **Deploy role** (e.g. `ihl-hims-gha-deploy`): IAM → Roles → Create role →
   Web identity, provider `token.actions.githubusercontent.com`. Trust policy
   condition:

   ```json
   "StringEquals": {
     "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
     "token.actions.githubusercontent.com:sub": "repo:Gokul0ihl/care-backend:environment:development"
   }
   ```

   Inline permission policy (it can run commands on this one instance only,
   using only the standard shell document):

   ```json
   {
     "Version": "2012-10-17",
     "Statement": [
       {
         "Effect": "Allow",
         "Action": "ssm:SendCommand",
         "Resource": [
           "arn:aws:ec2:ap-south-1:654843162285:instance/<instance-id>",
           "arn:aws:ssm:ap-south-1::document/AWS-RunShellScript"
         ]
       },
       { "Effect": "Allow", "Action": "ssm:GetCommandInvocation", "Resource": "*" }
     ]
   }
   ```
4. **On the box** (as `ec2-user`): `docker compose version` must be v2.17+
   (for `--wait`), then:

   ```sh
   sudo dnf install -y git
   sudo mkdir -p /opt/ihl-hims/backups && sudo chown ec2-user: /opt/ihl-hims /opt/ihl-hims/backups
   ```
5. **In GitHub**, on the `development` environment (Settings → Environments),
   add the approval rules below and these **environment variables**:

   | Variable | Value |
   |---|---|
1. After approval (see below), the job gets temporary AWS credentials through
   GitHub OIDC and sends an **SSM Run Command** to the instance. There is no
   SSH: the SSM agent on the box picks the command up over an outbound
   connection, so port 22 doesn't need to be open to GitHub. The command runs
   as `ec2-user`, checks the commit out in `/opt/ihl-hims/source` (cloned if
   missing) and runs `deploy/deploy.sh`. The checkout is left on a detached
   commit, so for a manual build run `git checkout main && git pull` there
   rather than plain `git pull`. Any uncommitted edits in that folder are
   discarded. The full output goes to `/opt/ihl-hims/deploy/last-deploy.log`;
   the job shows its last 20,000 characters once the command finishes (SSM
   doesn't stream output).
   Set this up **before** the workflow reaches `main`. If the environment
   doesn't exist when the first run starts, GitHub creates it with no
   reviewers and that run deploys without approval.

Everything here is free: IAM, OIDC, STS and SSM Run Command have no charge,
and Actions minutes are free for a public repo.

If the job stops while the command is still running on the box (job
timeout, or someone cancels the run), the deploy carries on there. Check
`last-deploy.log` on the box. A new run can't take the box's lock, so it fails
fast with "Another deploy is already running" until it's done.

**Approval before each deploy.** `main` has no branch protection, so every
push would otherwise go straight to the box. The job runs in the
`development` GitHub environment, and that environment needs required
reviewers (repo admin, one-time):

1. **Settings → Environments → `development`** (create it with exactly that
   name if it isn't listed).
2. Tick **Required reviewers**, add the people who may approve (any one of
   them is enough), and tick **Prevent self-review**.
3. Under **Deployment branches and tags**, choose **Selected branches** and add
   `main`, so a manual run can't deploy an arbitrary branch.
4. **Save protection rules.**

After that, each run stops at *Waiting for review* before it touches AWS or
the box. A reviewer opens the run and picks **Review deployments → Approve and
deploy** or **Reject**; cancelling the run also works. Unreviewed runs expire
after 30 days. If several pushes land while one is waiting, the concurrency
group keeps only the newest waiting run, so one approval deploys the latest
`main`. A deploy that has already started is never cancelled.

**The Actions logs are public** (the repo is public). Anything the job
prints can be read by anyone. The job prints the tail of the deploy output
(git, image build, migration plan, `docker compose ps`), but `deploy.sh` never
prints container logs, which can hold patient data. On the box, readable by
`ec2-user` only:

- `/opt/ihl-hims/deploy/last-deploy.log`: full output of the last deploy
- `/opt/ihl-hims/deploy/last-failed-deploy.log`: container logs from the last
  failed deploy (or run `docker compose logs` there)

### First-time setup (original Gitea flow)

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
