#!/usr/bin/env bash
# Fleet IAM, to paste into AWS CloudShell (eu-west-1) with an admin identity, never
# from the box (§3.5). Copied from FoxHouse (deploy/iam-cloudshell.sh).
#
# CALLING CONVENTION: `bash iam-cloudshell.sh` (no argument). It handles BOTH repos:
# 1. Creates or updates the role fleet-deploy (GitHub OIDC, trust pinned on
#    Foxugly/Fleet_server, environment production, BOTH `sub` forms: OPERATIONS.md
#    §3.11 GOTCHA): SSM on the instance + s3:PutObject on
#    foxugly-deploy/builds/fleet-server/* (the backend bundle).
# 2. Same for fleet-frontend-deploy (Foxugly/Fleet_frontend), S3
#    foxugly-deploy/builds/fleet-frontend/*.
# 3. Extends the statement SsmReadFleetAppConfig of the inline policy
#    foxugly-fleet-app-config of the instance role foxugly-fleet-ec2 (§3.14: never
#    a new policy) with /fleet/prod and /fleet-frontend/prod. The previous policy
#    is saved to ~/foxugly-fleet-app-config.<timestamp>.json and the diff printed.
#    The instance role already reads foxugly-deploy/builds/* (s3:GetObject).
#
# Re-runnable: an existing role is updated, a resource already present is not doubled.
# Prints the two role ARNs: set each as AWS_DEPLOY_ROLE_ARN in the `production`
# environment of its repo.
set -euo pipefail
umask 027

EXPECTED_ACCOUNT=362629935151
OWNER_ID=3275928            # Foxugly
INSTANCE_ID=i-0fe664678563bae5f
REGION=eu-west-1
BUCKET=foxugly-deploy
FLEET_ROLE=foxugly-fleet-ec2
FLEET_POLICY=foxugly-fleet-app-config

ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
[ "$ACCOUNT" = "$EXPECTED_ACCOUNT" ] || { echo "Wrong account: $ACCOUNT (expected $EXPECTED_ACCOUNT)." >&2; exit 1; }
OIDC="arn:aws:iam::${ACCOUNT}:oidc-provider/token.actions.githubusercontent.com"
aws iam get-open-id-connect-provider --open-id-connect-provider-arn "$OIDC" >/dev/null

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

# deploy_role <role> <repo> <repo id> <S3 prefix under builds/> <description>
deploy_role() {
  local role="$1" repo="$2" repo_id="$3" prefix="$4" description="$5"
  [[ "$repo_id" =~ ^[0-9]+$ ]] || { echo "repo id of $repo missing (gh api repos/Foxugly/$repo --jq .id)." >&2; exit 1; }

  cat > "$WORK/$role-trust.json" <<JSON
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"Federated": "${OIDC}"},
    "Action": "sts:AssumeRoleWithWebIdentity",
    "Condition": {
      "StringEquals": {
        "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
        "token.actions.githubusercontent.com:sub": [
          "repo:Foxugly@${OWNER_ID}/${repo}@${repo_id}:environment:production",
          "repo:Foxugly/${repo}:environment:production"
        ]
      }
    }
  }]
}
JSON

  cat > "$WORK/$role-policy.json" <<JSON
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "SendDeployCommand",
      "Effect": "Allow",
      "Action": "ssm:SendCommand",
      "Resource": [
        "arn:aws:ec2:${REGION}:${ACCOUNT}:instance/${INSTANCE_ID}",
        "arn:aws:ssm:${REGION}::document/AWS-RunShellScript"
      ]
    },
    {
      "Sid": "UploadDeployBundle",
      "Effect": "Allow",
      "Action": "s3:PutObject",
      "Resource": "arn:aws:s3:::${BUCKET}/builds/${prefix}/*"
    },
    {
      "Sid": "ReadDeployResult",
      "Effect": "Allow",
      "Action": "ssm:GetCommandInvocation",
      "Resource": "*"
    }
  ]
}
JSON

  if aws iam get-role --role-name "$role" >/dev/null 2>&1; then
    aws iam update-assume-role-policy --role-name "$role" --policy-document "file://$WORK/$role-trust.json"
  else
    aws iam create-role --role-name "$role" --description "$description" \
      --assume-role-policy-document "file://$WORK/$role-trust.json" >/dev/null
  fi
  aws iam put-role-policy --role-name "$role" --policy-name "$role" \
    --policy-document "file://$WORK/$role-policy.json"
  echo "  $role: trust + inline policy $role up to date"
}

# Resources literally: builds/fleet-server/* and builds/fleet-frontend/*.
deploy_role fleet-deploy Fleet_server 1466918968 fleet-server \
  "GitHub Actions deploy for Foxugly/Fleet_server (SSM + S3 builds/fleet-server)"
deploy_role fleet-frontend-deploy Fleet_frontend 1467008189 fleet-frontend \
  "GitHub Actions deploy for Foxugly/Fleet_frontend (SSM + S3 builds/fleet-frontend)"

# --- Instance role: read /fleet/prod and /fleet-frontend/prod ---
BACKUP=~/foxugly-fleet-app-config.$(date +%Y%m%d-%H%M%S).json
aws iam get-role-policy --role-name "$FLEET_ROLE" --policy-name "$FLEET_POLICY" \
  --query PolicyDocument --output json > "$BACKUP"

python3 - "$BACKUP" "$ACCOUNT" "$REGION" > "$WORK/fleet-policy.json" <<'PY'
import json, sys
path, account, region = sys.argv[1:]
doc = json.load(open(path))
target = [s for s in doc["Statement"] if s.get("Sid") == "SsmReadFleetAppConfig"]
if len(target) != 1:
    sys.exit("Statement SsmReadFleetAppConfig missing or duplicated: nothing is changed.")
s = target[0]
res = s["Resource"] if isinstance(s["Resource"], list) else [s["Resource"]]
for r in (f"arn:aws:ssm:{region}:{account}:parameter/fleet/prod",
          f"arn:aws:ssm:{region}:{account}:parameter/fleet/prod/*",
          f"arn:aws:ssm:{region}:{account}:parameter/fleet-frontend/prod",
          f"arn:aws:ssm:{region}:{account}:parameter/fleet-frontend/prod/*"):
    if r not in res:
        res.append(r)
s["Resource"] = res
out = json.dumps(doc, indent=2)
if len(json.dumps(doc, separators=(",", ":"))) > 10240:
    sys.exit("The policy would exceed the 10 240-character inline limit: nothing is changed.")
print(out)
PY

echo "--- Diff on ${FLEET_POLICY} (previous copy: ${BACKUP}) ---"
diff <(python3 -m json.tool "$BACKUP") "$WORK/fleet-policy.json" || true
aws iam put-role-policy --role-name "$FLEET_ROLE" --policy-name "$FLEET_POLICY" \
  --policy-document "file://$WORK/fleet-policy.json"

# s3:GetObject on foxugly-deploy/builds/*: the instance role already has it (foxugly,
# quizonline, foxhouse...); printed to check it, nothing changed.
echo "--- Bundle reads by the instance role (expected: s3:GetObject on ${BUCKET}/builds/*) ---"
for p in $(aws iam list-role-policies --role-name "$FLEET_ROLE" --query PolicyNames --output text); do
  aws iam get-role-policy --role-name "$FLEET_ROLE" --policy-name "$p" --query PolicyDocument --output json \
    | python3 -c "import json,sys; d=json.load(sys.stdin); [print(sys.argv[1], json.dumps(s)) for s in d['Statement'] if sys.argv[2] in json.dumps(s.get('Resource', ''))]" "$p" "$BUCKET"
done

# kms:Decrypt: SecureStrings use the aws/ssm key; check a statement already covers it
# (the other sites read their SecureStrings).
echo "--- KMS statements of ${FLEET_POLICY} ---"
python3 -c "import json,sys; d=json.load(open(sys.argv[1])); [print(json.dumps(s)) for s in d['Statement'] if any('kms' in a for a in ([s['Action']] if isinstance(s['Action'],str) else s['Action']))]" "$WORK/fleet-policy.json"

echo
echo "Fleet_server   AWS_DEPLOY_ROLE_ARN = $(aws iam get-role --role-name fleet-deploy --query Role.Arn --output text)"
echo "Fleet_frontend AWS_DEPLOY_ROLE_ARN = $(aws iam get-role --role-name fleet-frontend-deploy --query Role.Arn --output text)"
