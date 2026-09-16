# IHL-HIMS Backend - Complete Setup Guide

A start-to-finish, beginner-friendly walkthrough: what to click in the AWS
Console, what to type on your laptop, and what to do on the server, in
order. Every command below is annotated so you know what each line actually
does before you run it - don't paste blindly.

You already have AWS account access (not root), so this only creates one
new, narrowly-scoped IAM user - for Gitea CI/CD, for security reasons, not
because it's technically required.

**What you'll end up with:** one Linux EC2 instance (using its default
public IP - see the note in Part 4 on why we skipped the Elastic IP),
running the backend (Django), celery-worker, celery-beat, Redis, and
Postgres as Docker containers - and a Gitea Actions pipeline that
automatically rebuilds and redeploys it every time you push to `main`.

**You need exactly 2 S3 buckets** (see Part 3 and the frontend repo's
guide): `ihl-hims-dev-frontend` (public, serves the built React app) and
`ihl-hims-dev-uploads` (private, patient documents/prescriptions). If either
name is already taken by someone else on AWS globally, append your account
ID from Part 2.

---

## Part 1 - Create the CI/CD IAM user (console)

This user's keys go into Gitea's secrets for the deploy pipeline. Keeping it
separate from your own account access means a leaked secret can only do the
one narrow thing this user is allowed to do, not whatever your own access
covers.

1. AWS Console -> **IAM -> Users -> Create user**
2. Name: `ihl-hims-ci-deploy`.
   Leave **"Provide user access to the AWS Management Console" unchecked** -
   this user only ever authenticates via CLI/API keys, it should never be
   able to log into the web console at all.
3. **Attach policies directly -> Create policy -> JSON tab**, paste (swap in
   your real bucket name once you create it in Part 3):

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": ["s3:PutObject", "s3:GetObject", "s3:DeleteObject", "s3:ListBucket"],
    "Resource": [
      "arn:aws:s3:::ihl-hims-dev-uploads",
      "arn:aws:s3:::ihl-hims-dev-uploads/*"
    ]
  }]
}
```

   What each field means:
   - `"Effect": "Allow"` - this is a grant, not a denial
   - `"Action"` - the exact four S3 operations this user may perform: upload
     a file, read a file, delete a file, list what's in the bucket - nothing
     else (can't change bucket settings, can't touch any other bucket)
   - `"Resource"` - the first ARN (no trailing `/*`) covers actions *on the
     bucket itself* (like listing it); the second (with `/*`) covers actions
     *on the objects inside it* (like reading/writing files) - S3 requires
     both listed separately

4. Name the policy `ihl-hims-ci-policy`, create it, attach it to the user.
5. Open `ihl-hims-ci-deploy` -> **Security credentials** tab -> **Create
   access key** -> choose **"Command Line Interface (CLI)"** as the use case
   -> copy the **Access Key ID** and **Secret Access Key** now (the secret
   is only ever shown this one time). Save both somewhere safe - you'll
   paste them into Gitea in Part 7.

---

## Part 2 - Configure the AWS CLI on your laptop

You'll use *your own* existing access (not the CI user above) for every
manual step in this guide - the CI user's keys are only for Gitea.

```sh
aws configure
```
This prompts for four things, one at a time:
- **AWS Access Key ID** - from your own IAM user (not the CI one)
- **AWS Secret Access Key** - same
- **Default region name** - `ap-south-1`
- **Default output format** - `json`

Then verify it's actually wired up correctly. This command only *reads* your
identity - it creates nothing and costs nothing:

```sh
aws sts get-caller-identity
```
```json
{
    "UserId": "AIDAxxxxxxxxxxxxxxxxx",
    "Account": "123456789012",
    "Arn": "arn:aws:iam::123456789012:user/your-username"
}
```
The `"Account"` field is your 12-digit AWS account ID - write it down, you
may need it in Part 3 if a bucket name is already taken.

---

## Part 3 - Create the S3 bucket for file uploads (console)

This is where patient documents/prescriptions get stored (`FILE_UPLOAD_BUCKET`
/`FACILITY_S3_BUCKET` in CARE's settings).

1. **S3 -> Create bucket**
2. **Bucket name:** try `ihl-hims-dev-uploads` first. Bucket names are
   unique across *all* of AWS worldwide, not just your account - if AWS
   rejects it as taken, use `ihl-hims-dev-uploads-<your-account-id>` instead
   (guaranteed unique to you).
3. **AWS Region:** `ap-south-1` (must match what you used in `aws configure`)
4. **Block Public Access settings:** leave every box **checked (ON)** - this
   bucket holds patient data and must never be publicly reachable.
5. **Bucket Versioning:** select **Enable** - this means if a file is ever
   overwritten or deleted, S3 keeps the previous version recoverable.
6. Leave everything else default -> **Create bucket**

If you had to add the account-ID suffix, go back and update the bucket name
in the Part 1 policy JSON to match exactly (IAM policies check the name
literally - a mismatch silently denies access instead of erroring loudly).

---

## Part 4 - Create the EC2 instance (console)

1. **EC2 -> Instances -> Launch instances**
2. **Name:** `ihl-hims-dev` - just a label, used for `docker compose` and
   for finding the instance later in the console.
3. **Application and OS Images (AMI):** choose **Amazon Linux 2023**.
   Not Windows - Docker Desktop (Windows' only real Docker option) is built
   to run inside an interactive logged-in session, not as an unattended
   background service the way Docker Engine runs natively on Linux; and
   Windows Server AMIs carry a licensing fee that free tier doesn't waive.
4. **Instance type:** `t3.small` - 2GB RAM. `t3.micro` (1GB, free-tier) is
   genuinely too tight running Django + Celery worker + Celery beat + Redis
   + Postgres as five containers at once.
5. **Key pair (login):** Create new key pair.
   - Name: `ihl-hims-key`
   - Type: RSA
   - Format: `.pem`
   - Click **Create key pair** - your browser downloads the file
     immediately. **This is the only copy - AWS does not keep it and cannot
     regenerate it.** You'll use this same file to SSH in AND as a Gitea
     secret in Part 7.
6. **Network settings -> Edit** (click "Edit", not the summary view):
   - **VPC:** leave as the default one
   - **Auto-assign public IP:** **Enable** - without this the instance has
     no internet-reachable address at all
   - **Firewall (security groups): Create security group** with two rules:
     - Rule 1 (pre-filled): Type `SSH`, Port `22`, Source type **My IP**
       (auto-detects your current IP so only you can SSH in)
     - Click **Add security group rule**: Type `Custom TCP`, Port range
       `9000`, Source type **Anywhere (0.0.0.0/0)** - port 9000 is where
       Django/gunicorn listens; this has to be open to the internet so the
       frontend and browsers can reach the API
7. **Configure storage:** change the default 8 GiB to **30 GiB**, type
   **gp3** - more headroom for Docker images, Postgres data, and logs.
8. Click **Launch instance**.

### Using the instance's default public IP (no Elastic IP)

An Elastic IP is the "proper" way to get a permanently-fixed address - it
never changes even across stop/restart. We skipped it here because this
AWS account's Elastic IP quota (5 per region) was already fully used by
other resources, and requesting an increase either needs a permission this
IAM user doesn't have or just wasn't worth blocking setup on.

Instead, just use the **public IP the instance already has automatically**
(from "Auto-assign public IP: Enable" in step 6):

1. **EC2 -> Instances**, click on `ihl-hims-dev`
2. On the **Details** tab, copy the **Public IPv4 address**

**Write down this IP** - it's what you SSH to, the `EC2_HOST` Gitea secret
in Part 7, and what the frontend's `REACT_CARE_API_URL` points at.

**Trade-off to know about:** unlike an Elastic IP, this address can change
if the instance is ever **stopped and restarted** (a plain reboot is fine -
the IP only changes on stop/start, not reboot). If that happens, you'd need
to: re-copy the new public IP from this same Details tab, update the
`EC2_HOST` Gitea secret, and rebuild the frontend with the new
`REACT_CARE_API_URL`. For active setup/dev work this is unlikely to bite
you - you can always properly allocate an Elastic IP later (once the quota
is sorted) by following the original steps and re-pointing everything at
the new fixed address.

### Give the instance permission to reach the uploads bucket

This lets the EC2 box read/write S3 without ever storing an AWS access key
on the server itself - if the server is ever compromised, there's no key to
steal.

1. **IAM -> Roles -> Create role**
2. **Trusted entity type:** AWS service. **Use case:** EC2 -> Next
   (this defines *who* can use this role - here, "any EC2 instance you
   attach it to")
3. Skip attaching a managed policy for now -> Next -> **Role name:**
   `ihl-hims-ec2-role` -> Create role
4. Open the new role -> **Add permissions -> Create inline policy** -> JSON
   tab, paste the exact same JSON as Part 1 (matching your actual bucket
   name) -> name it `ihl-hims-ec2-s3-access` -> Create policy
5. Back on **EC2 -> Instances**, select `ihl-hims-dev` -> **Actions ->
   Security -> Modify IAM role** -> select `ihl-hims-ec2-role` -> **Update
   IAM role**
   (this is the step that actually attaches the role to the running
   instance - creating the role alone does nothing by itself)

---

## Part 5 - Connect and install Docker

Wait ~1-2 minutes after launch for the instance to finish booting, then
connect over SSH. Two equally valid options:

**Option A - native SSH (PowerShell/Git Bash):**
```sh
ssh -i /path/to/ihl-hims-key.pem ec2-user@<your-public-ip>
```
- `-i /path/to/ihl-hims-key.pem` - tells SSH which private key file proves
  your identity (must match the key pair from Part 4)
- `ec2-user` - the default login username baked into the Amazon Linux AMI
- `<your-public-ip>` - the address from Part 4

**Option B - PuTTY:** Host Name = your public IP, Port 22, Connection type
SSH; under Connection -> SSH -> Auth -> Credentials, browse to your key
file. Open, and log in as `ec2-user` when prompted.

**From here on, every command shown as a code block is typed inside this
same terminal session, regardless of which option you used to connect** -
PuTTY and native `ssh` both just give you a Linux shell, so there's no
difference for anything below. The one exception is file *uploads* from
your laptop to the box (the `scp` commands in Part 6 and Part 8) - those
use an OpenSSH tool, so if you're on PuTTY without OpenSSH installed,
use the WinSCP alternative shown at each of those two steps instead.

Once connected, install Docker line by line:

```sh
sudo dnf install -y docker
```
Installs Docker Engine from Amazon Linux's package manager (`dnf`). `-y`
auto-confirms the prompt.

```sh
sudo systemctl enable --now docker
```
`enable` makes Docker start automatically on every future boot; `--now`
also starts it immediately in this session.

```sh
sudo usermod -aG docker ec2-user
```
Adds your login user to the `docker` group, so you can run `docker`
commands without typing `sudo` every time.

```sh
sudo mkdir -p /usr/local/lib/docker/cli-plugins
``
Docker Compose (v2) is installed as a *plugin*, not a separate program -
this is the folder Docker looks in for plugins.

```sh
sudo curl -SL https://github.com/docker/compose/releases/latest/download/docker-compose-linux-x86_64 \
  -o /usr/local/lib/docker/cli-plugins/docker-compose
```
Downloads the latest Docker Compose binary straight from GitHub releases
into that plugins folder.

```sh
sudo chmod +x /usr/local/lib/docker/cli-plugins/docker-compose
```
Marks the downloaded file as executable - without this, Docker can see the
file but refuses to run it.

```sh
mkdir -p /opt/ihl-hims/deploy
```
Creates the folder where `docker-compose.yml` and `.env` will live on this
box (matches the paths used throughout the rest of this guide and in
`.gitea/workflows/deploy.yml`).

Now log out and back in so the group change from `usermod` actually applies
to your session:
```sh
exit
ssh -i /path/to/ihl-hims-key.pem ec2-user@<your-public-ip>
```

Verify both installed correctly:
```sh
docker --version
docker compose version
```
Each should print a version number, not an error.

---

## Part 6 - Get CARE's source into `backend-repo` and deploy the config

On your laptop, if you haven't already brought CARE's source into this repo:

```sh
cd backend-repo
```
Move into the local copy of your Gitea repo.

```sh
git clone https://github.com/ohcnetwork/care.git .
```
Pulls CARE's entire upstream source code into the current directory (the
trailing `.` means "here", not a new subfolder).

```sh
git remote rename origin upstream
```
Renames the link to the original CARE GitHub repo from `origin` to
`upstream` - freeing up the name `origin` for your own repo, and keeping a
clear way to pull future CARE updates later (`git pull upstream develop`).

```sh
git remote add origin <this repo's Gitea URL>
```
Points `origin` at your own Gitea repo instead.

```sh
git push -u origin develop:main
```
Pushes CARE's `develop` branch content to your Gitea repo's `main` branch,
and `-u` remembers this mapping so future plain `git push`/`git pull` know
where to go.

Now copy the deploy files to the box (still on your laptop, in `backend-repo`):

**Option A - `scp` (needs OpenSSH, works from PowerShell/Git Bash):**
```sh
scp -i /path/to/ihl-hims-key.pem deploy/docker-compose.yml ec2-user@<public-ip>:/opt/ihl-hims/deploy/
scp -i /path/to/ihl-hims-key.pem deploy/.env.example ec2-user@<public-ip>:/opt/ihl-hims/deploy/.env
```
`scp` copies a file over SSH. First line copies the container stack
definition; second copies the environment template, renaming it to `.env`
in the same step (that's the filename `docker-compose.yml` actually reads).

**Option B - WinSCP (if you're using PuTTY and don't have `scp`):**
1. Download/open [WinSCP](https://winscp.net) -> **New Session**
2. File protocol: **SCP**, Host name: your public IP, Port: `22`,
   User name: `ec2-user`
3. **Advanced -> SSH -> Authentication -> Private key file** -> browse to
   the same key file you use for PuTTY
4. **Login** - it opens a two-pane file browser (local on the left, the
   EC2 box on the right)
5. On the right pane, navigate to `/opt/ihl-hims/deploy/`
6. Drag `deploy/docker-compose.yml` from the left pane to the right
7. Drag `deploy/.env.example` over the same way, then on the right pane
   rename it from `.env.example` to `.env` (right-click -> Rename)

SSH back in and edit that `.env` file with real values:

```sh
ssh -i /path/to/ihl-hims-key.pem ec2-user@<public-ip>
nano /opt/ihl-hims/deploy/.env
```
`nano` is a simple terminal text editor - arrow keys to move, type to edit,
`Ctrl+O` then Enter to save, `Ctrl+X` to exit.

Fields to actually fill in, and why each one matters:
- `POSTGRES_PASSWORD` - a strong password with **no `@`, `:`, or `/`
  characters** (it gets embedded inside `DATABASE_URL`, a URL string where
  those characters have special meaning and would break parsing)
- `DJANGO_SECRET_KEY` - Django uses this to cryptographically sign sessions/
  tokens; generate a real one with:
  `python3 -c "import secrets; print(secrets.token_urlsafe(50))"`
- `DJANGO_ALLOWED_HOSTS` / `BACKEND_DOMAIN` - your public IP; Django
  rejects requests whose `Host` header isn't in this list, as a security
  measure against host-header attacks
- `CORS_ALLOWED_ORIGINS` / `CSRF_TRUSTED_ORIGINS` / `CURRENT_DOMAIN` - your
  frontend's S3 website URL; without this the browser blocks the frontend's
  API calls to the backend as cross-origin requests
- `FILE_UPLOAD_BUCKET` / `FACILITY_S3_BUCKET` - the uploads bucket from Part 3
- Leave `BUCKET_KEY`/`BUCKET_SECRET` commented out - the instance role from
  Part 4 already grants S3 access; adding keys here would be redundant and
  is a credential you'd then have to protect on the box itself

Log in to Gitea's container registry so `docker compose pull` can
authenticate later:
```sh
docker login gitea.example.com
```
Prompts for your Gitea username and a Gitea access token (Gitea -> Settings
-> Applications -> Generate New Token, with package read/write scope) -
saves credentials locally on the box so future pulls don't need to log in
again.

---

## Part 7 - Wire up Gitea Actions

In `backend-repo` on Gitea: **Settings -> Actions -> Secrets -> Add secret**,
add each of these one at a time:

| Secret | Value | Why the pipeline needs it |
|---|---|---|
| `EC2_HOST` | Your public IP | Tells the `ssh` step in `deploy.yml` where to connect |
| `EC2_USER` | `ec2-user` | The SSH login username |
| `EC2_SSH_KEY` | The full contents of `ihl-hims-key.pem` | Authenticates that SSH connection - never commit this file itself to the repo |
| `GITEA_REGISTRY_USER` | Your Gitea username | Logs in to Gitea's container registry to push the built image |
| `GITEA_REGISTRY_TOKEN` | A Gitea access token, package read/write scope | Same login, as a token instead of your account password |

Then trigger the first deploy:
```sh
git add -A
git commit -m "Initial deploy"
git push origin main
```
- `git add -A` - stages every changed/new file
- `git commit -m "..."` - saves those staged changes as one commit with that message
- `git push origin main` - uploads the commit to Gitea's `main` branch, which is exactly what `.gitea/workflows/deploy.yml`'s `on: push: branches: [main]` is watching for

Watch it run under the repo's **Actions** tab. In order, it: builds the
image from `docker/prod.Dockerfile`, pushes it to Gitea's registry, then
SSHes into the box and runs `docker compose pull && docker compose up -d`
to actually restart the containers with the new image.

---

## Part 8 - Set up daily backups

Since Postgres is a container (not managed RDS), nothing backs it up
automatically - this is cheap insurance against losing everything to a bad
`docker compose down -v` or a disk failure.

First, copy the backup script to the box the same way as `docker-compose.yml`
in Part 6 (Option A `scp`, or Option B WinSCP if you're using PuTTY):
```sh
scp -i /path/to/ihl-hims-key.pem deploy/backup-db.sh ec2-user@<public-ip>:/opt/ihl-hims/deploy/
```

SSH in and edit the crontab (the list of scheduled jobs for your user):
```sh
ssh -i /path/to/ihl-hims-key.pem ec2-user@<public-ip>
crontab -e
```
Add this one line at the bottom:
```
0 2 * * * /opt/ihl-hims/deploy/backup-db.sh >> /var/log/ihl-hims-backup.log 2>&1
```
Reading the cron schedule left to right - `0 2 * * *` means "at minute 0 of
hour 2, every day, every month, every day-of-week" i.e. **every day at
2:00 AM**. The rest of the line runs the backup script and appends
(`>>`) both its normal output and errors (`2>&1` redirects stderr into the
same place as stdout) into a log file you can check later.

Before this actually works, edit `BACKUP_BUCKET` at the top of
`backup-db.sh` (on the box, `nano /opt/ihl-hims/deploy/backup-db.sh`) to
match your real Part 3 bucket name.

---

## Part 9 - Verify it's actually working

```sh
ssh -i /path/to/ihl-hims-key.pem ec2-user@<public-ip>
cd /opt/ihl-hims/deploy
docker compose ps
```
Lists all 5 containers and their status - each should say `running` or
`healthy`, not `restarting` or `exited`.

```sh
docker compose logs backend --tail=50
```
Shows the last 50 log lines from the `backend` container - useful for
spotting startup errors (bad `.env` values, DB connection failures, etc).

```sh
curl http://localhost:9000/api/docs/
```
Makes a request to the API from *inside* the box itself - if this returns
HTML/JSON instead of a connection error, the backend is actually listening.

From your own machine/browser, the same URL using the public IP instead of
`localhost` (`http://<public-ip>:9000/api/docs/`) should load the API docs
- confirming the security group's port 9000 rule from Part 4 is working too.

---

## Note on `setup-ec2.sh`

That script automates everything in Parts 3-4 above via the AWS CLI instead
of the console. Since you're doing it by hand in the console, you don't
need it - it's left in `deploy/` as an optional alternative if you (or a
teammate) ever want to recreate this environment without clicking through
the console again, but it's not part of the path this guide follows.
