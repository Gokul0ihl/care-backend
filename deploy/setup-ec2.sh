#!/usr/bin/env bash
# One-time provisioning for the shared IHL-HIMS deployment box.
# Run this ONCE, locally, with the AWS CLI configured. It creates:
#   - a security group (SSH from your IP, app port 9000 open)
#   - an IAM role + instance profile granting S3 access (no AWS keys needed on the box)
#   - the EC2 instance itself, with Docker installed via user-data
#   - an Elastic IP, associated with the instance (this is your stable address)
#   - the S3 buckets for the frontend site and patient file uploads
#
# After this runs, add YOUR_ELASTIC_IP / the bucket names to:
#   - backend-repo/deploy/.env.example -> .env
#   - frontend-repo's REACT_CARE_API_URL / S3_BUCKET
# Re-running this script is NOT idempotent - it's meant to run once.

set -euo pipefail

# --- Edit these before running ---
AWS_REGION="ap-south-1"
PROJECT_NAME="ihl-hims"
ENVIRONMENT="dev"
INSTANCE_TYPE="t3.small"
KEY_PAIR_NAME="your-existing-ec2-keypair"     # must already exist in this region
MY_IP_CIDR="203.0.113.10/32"                    # your IP, for SSH access - get it from `curl -s ifconfig.me`
FRONTEND_BUCKET="ihl-hims-dev-frontend"   # must be globally unique
UPLOADS_BUCKET="ihl-hims-dev-uploads"     # must be globally unique
# ----------------------------------

NAME_PREFIX="${PROJECT_NAME}-${ENVIRONMENT}"

echo "==> Creating S3 buckets"
aws s3api create-bucket --bucket "$FRONTEND_BUCKET" --region "$AWS_REGION" \
  --create-bucket-configuration LocationConstraint="$AWS_REGION"
aws s3api put-bucket-website --bucket "$FRONTEND_BUCKET" --website-configuration \
  '{"IndexDocument":{"Suffix":"index.html"},"ErrorDocument":{"Key":"index.html"}}'
aws s3api put-public-access-block --bucket "$FRONTEND_BUCKET" --public-access-block-configuration \
  "BlockPublicAcls=false,IgnorePublicAcls=false,BlockPublicPolicy=false,RestrictPublicBuckets=false"
aws s3api put-bucket-policy --bucket "$FRONTEND_BUCKET" --policy "$(cat <<POLICY
{
  "Version": "2012-10-17",
  "Statement": [{
    "Sid": "PublicReadGetObject",
    "Effect": "Allow",
    "Principal": "*",
    "Action": "s3:GetObject",
    "Resource": "arn:aws:s3:::${FRONTEND_BUCKET}/*"
  }]
}
POLICY
)"

aws s3api create-bucket --bucket "$UPLOADS_BUCKET" --region "$AWS_REGION" \
  --create-bucket-configuration LocationConstraint="$AWS_REGION"
aws s3api put-bucket-versioning --bucket "$UPLOADS_BUCKET" --versioning-configuration Status=Enabled
aws s3api put-public-access-block --bucket "$UPLOADS_BUCKET" --public-access-block-configuration \
  "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"

echo "==> Creating security group"
VPC_ID=$(aws ec2 describe-vpcs --filters Name=isDefault,Values=true --query 'Vpcs[0].VpcId' --output text --region "$AWS_REGION")
SG_ID=$(aws ec2 create-security-group --group-name "${NAME_PREFIX}-sg" \
  --description "IHL-HIMS deployment box" --vpc-id "$VPC_ID" --region "$AWS_REGION" \
  --query 'GroupId' --output text)
aws ec2 authorize-security-group-ingress --group-id "$SG_ID" --protocol tcp --port 22 --cidr "$MY_IP_CIDR" --region "$AWS_REGION"
aws ec2 authorize-security-group-ingress --group-id "$SG_ID" --protocol tcp --port 9000 --cidr 0.0.0.0/0 --region "$AWS_REGION"

echo "==> Creating IAM role + instance profile for S3 access"
aws iam create-role --role-name "${NAME_PREFIX}-ec2-role" --assume-role-policy-document '{
  "Version": "2012-10-17",
  "Statement": [{"Effect": "Allow", "Principal": {"Service": "ec2.amazonaws.com"}, "Action": "sts:AssumeRole"}]
}'
aws iam put-role-policy --role-name "${NAME_PREFIX}-ec2-role" --policy-name "${NAME_PREFIX}-s3-access" --policy-document "$(cat <<POLICY
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket"],
    "Resource": ["arn:aws:s3:::${UPLOADS_BUCKET}", "arn:aws:s3:::${UPLOADS_BUCKET}/*"]
  }]
}
POLICY
)"
aws iam create-instance-profile --instance-profile-name "${NAME_PREFIX}-ec2-profile"
aws iam add-role-to-instance-profile --instance-profile-name "${NAME_PREFIX}-ec2-profile" --role-name "${NAME_PREFIX}-ec2-role"
sleep 10 # IAM propagation

echo "==> Launching EC2 instance"
AMI_ID=$(aws ec2 describe-images --owners amazon \
  --filters "Name=name,Values=al2023-ami-*-x86_64" "Name=state,Values=available" \
  --query 'sort_by(Images, &CreationDate)[-1].ImageId' --output text --region "$AWS_REGION")

INSTANCE_ID=$(aws ec2 run-instances \
  --image-id "$AMI_ID" \
  --instance-type "$INSTANCE_TYPE" \
  --key-name "$KEY_PAIR_NAME" \
  --security-group-ids "$SG_ID" \
  --iam-instance-profile Name="${NAME_PREFIX}-ec2-profile" \
  --block-device-mappings 'DeviceName=/dev/xvda,Ebs={VolumeSize=30,VolumeType=gp3}' \
  --user-data '#!/bin/bash
dnf install -y docker
systemctl enable --now docker
usermod -aG docker ec2-user
DOCKER_CONFIG_DIR=/usr/local/lib/docker/cli-plugins
mkdir -p $DOCKER_CONFIG_DIR
curl -SL https://github.com/docker/compose/releases/latest/download/docker-compose-linux-x86_64 -o $DOCKER_CONFIG_DIR/docker-compose
chmod +x $DOCKER_CONFIG_DIR/docker-compose
mkdir -p /opt/ihl-hims/deploy
chown -R ec2-user:ec2-user /opt/ihl-hims' \
  --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=${NAME_PREFIX}}]" \
  --region "$AWS_REGION" \
  --query 'Instances[0].InstanceId' --output text)

echo "==> Waiting for instance to be running"
aws ec2 wait instance-running --instance-ids "$INSTANCE_ID" --region "$AWS_REGION"

echo "==> Allocating and associating an Elastic IP"
ALLOC_ID=$(aws ec2 allocate-address --domain vpc --region "$AWS_REGION" --query 'AllocationId' --output text)
aws ec2 associate-address --instance-id "$INSTANCE_ID" --allocation-id "$ALLOC_ID" --region "$AWS_REGION"
ELASTIC_IP=$(aws ec2 describe-addresses --allocation-ids "$ALLOC_ID" --region "$AWS_REGION" --query 'Addresses[0].PublicIp' --output text)

FRONTEND_WEBSITE_ENDPOINT="${FRONTEND_BUCKET}.s3-website.${AWS_REGION}.amazonaws.com"

cat <<SUMMARY

==================== Setup complete ====================
Instance ID:            $INSTANCE_ID
Elastic IP:              $ELASTIC_IP
Frontend bucket:         $FRONTEND_BUCKET
Frontend website URL:    http://$FRONTEND_WEBSITE_ENDPOINT
Uploads bucket:          $UPLOADS_BUCKET

Next steps:
1. SSH in once Docker finishes installing (~1-2 min):
     ssh -i /path/to/${KEY_PAIR_NAME}.pem ec2-user@$ELASTIC_IP
2. scp deploy/docker-compose.yml to /opt/ihl-hims/deploy/ on the box, copy
   deploy/.env.example to /opt/ihl-hims/deploy/.env and fill in real values
   (use the Elastic IP and bucket names above).
3. docker login to your Gitea registry on the box so `docker compose pull`
   can authenticate: docker login gitea.example.com
4. Add these as Gitea repo secrets on backend-repo:
     EC2_HOST=$ELASTIC_IP
     EC2_USER=ec2-user
     EC2_SSH_KEY=<contents of ${KEY_PAIR_NAME}.pem>
     GITEA_REGISTRY_USER / GITEA_REGISTRY_TOKEN
5. Set up the backup cron job (see deploy/backup-db.sh).
==========================================================
SUMMARY
