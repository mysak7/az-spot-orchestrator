# OIDC / Workload Identity Federation — CI/CD Auth

How GitHub Actions authenticates to cloud providers without storing any secrets.
Covers this project (Azure) and how SEIP (AWS) implements the same pattern.

---

## The Problem

GitHub Actions needs cloud credentials to deploy. The naive solution is to generate
a long-lived service principal secret, paste it into GitHub Secrets, and rotate it
manually. That secret can leak, never expires by default, and is painful to audit.

## The Solution: OIDC Token Exchange

GitHub's OIDC provider issues a **short-lived signed JWT** (≈5 min TTL) at the start
of every workflow job. The cloud provider validates that token and exchanges it for
its own short-lived access token. No static secret ever exists.

```
GitHub Actions job starts
        │
        ▼
GitHub OIDC provider mints a JWT
  iss = "https://token.actions.githubusercontent.com"
  sub = "repo:owner/repo:ref:refs/heads/master"   ← exact caller identity
  aud = "api://AzureADTokenExchange"               ← Azure  (or "sts.amazonaws.com" for AWS)
  exp = now + 5 minutes
        │
        ▼
Workflow sends JWT to cloud provider (Azure AD / AWS STS)
        │
        ▼
Cloud validates: is there a federated credential matching iss + sub?
        │ yes
        ▼
Cloud returns a short-lived access token (Azure token / AWS session creds)
        │
        ▼
Workflow uses token normally (az CLI / aws CLI / SDK)
```

The `sub` claim is the security boundary. It encodes the exact repo, branch, PR, or
environment that produced the token. A token from a different repo or branch has a
different `sub` and is rejected.

---

## This Project — Azure

### Infrastructure (Terraform)

All OIDC infrastructure is in `terraform/github_oidc.tf`:

| Resource | Purpose |
|---|---|
| `azuread_application` | App Registration — identity for GitHub Actions |
| `azuread_service_principal` | Service Principal attached to the App Registration |
| `azuread_application_federated_identity_credential.cd_master` | Trusts tokens from `master` branch pushes |
| `azuread_application_federated_identity_credential.ci_pr` | Trusts tokens from pull requests |
| `azurerm_role_assignment` (Contributor) | VM provisioning, network, run-command on the RG |
| `azurerm_role_assignment` (Storage Blob Data Contributor) | Read/write model cache blobs |
| `github_actions_variable` × 3 | Sets `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID` in the repo automatically |

No client secret is created. The App Registration has no password.

### Federated Credentials

Two subjects are trusted — one per workflow context:

```hcl
# CD: master branch deploys
subject = "repo:mysak7/az-spot-orchestrator:ref:refs/heads/master"

# CI: pull request jobs (future integration tests)
subject = "repo:mysak7/az-spot-orchestrator:pull_request"
```

Any token whose `sub` does not exactly match one of these is rejected by Azure AD.
A compromised token from a fork or a different branch cannot assume the role.

### Role Scope

Roles are assigned at **resource group scope**, not subscription scope:

```
Subscription
└── az-spot-orchestrator-rg   ← Contributor assigned here
    ├── VMs, NICs, PIPs        (provisioned by orchestrator)
    ├── VNets, NSGs
    └── control-plane VM       (run-command target for deploy)

Storage Account: azspotmodelcache
└── Storage Blob Data Contributor assigned here
```

GitHub Actions cannot touch anything outside the resource group.

### Workflow Usage (`cd.yml`)

```yaml
permissions:
  id-token: write   # allows GitHub to mint an OIDC token for this job

steps:
  - uses: azure/login@v2
    with:
      client-id: ${{ vars.AZURE_CLIENT_ID }}        # not a secret
      tenant-id: ${{ vars.AZURE_TENANT_ID }}         # not a secret
      subscription-id: ${{ vars.AZURE_SUBSCRIPTION_ID }}  # not a secret
```

`client-id`, `tenant-id`, and `subscription-id` are identifiers, not credentials.
They are stored as GitHub **Variables** (not Secrets) and set automatically by
`make oidc-setup` — nothing is pasted manually.

### Setup

One command, run once after `terraform apply` has provisioned the main infrastructure:

```bash
make oidc-setup
```

Internally this:
1. Gets `GITHUB_TOKEN` from `gh auth token` (no manual token copy)
2. Runs `terraform init -upgrade` to pull the GitHub provider
3. Runs `terraform apply` scoped to only the OIDC resources
4. Sets the 3 GitHub Actions variables via the Terraform GitHub provider

---

## SEIP Project — AWS (reference implementation)

SEIP (`~/GitHub/SEIP`) uses the identical OIDC pattern on AWS instead of Azure.

### Infrastructure

Defined in `seip-infrastructure/terraform/modules/iam/github_oidc.tf`:

```hcl
resource "aws_iam_openid_connect_provider" "github" {
  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
  thumbprint_list = [
    "6938fd4d98bab03faadb97b34396831e3780aea1",
    "1c58a3a8518e8759bf075b76b750d4f2df264fcd",
  ]
}

resource "aws_iam_role" "github_actions" {
  name = "github-actions-deploy-role"
  assume_role_policy = jsonencode({
    Condition = {
      StringLike = {
        "token.actions.githubusercontent.com:sub" = [
          for repo in var.github_repos : "repo:${repo}:*"
        ]
      }
    }
  })
}
```

The `StringLike` + wildcard (`repo:mysak7/seip-gui:*`) trusts all branches and
events in each listed repo. Broader than our per-branch credentials but simpler
when you have many repos sharing the same role.

### Permissions

The IAM role policy covers:

- **ECR** — push images, set lifecycle policies
- **EC2** — describe instances, VPCs, subnets
- **SSM** — `SendCommand`, `GetCommandInvocation` (deploy trigger)
- **App Runner** — start deployments

### Workflow Usage

```yaml
- uses: aws-actions/configure-aws-credentials@v4
  with:
    role-to-assume: arn:aws:iam::317781017752:role/github-actions-deploy-role
    aws-region: eu-central-1
```

The role ARN is hardcoded in the workflow YAML — it is not sensitive (IAM role ARNs
are not secret), so no GitHub Secrets or Variables are needed at all.

### Deployment Method

SEIP uses **AWS SSM `SendCommand`** to run a shell script on the EC2 instance:

```yaml
- name: Deploy via SSM
  run: |
    aws ssm send-command \
      --instance-ids $INSTANCE_ID \
      --document-name "AWS-RunShellScript" \
      --parameters commands=["docker pull ...", "systemctl restart seip-gui"]
```

This project uses the equivalent **Azure VM Run Command**:

```yaml
- name: Deploy via VM run-command
  run: |
    az vm run-command invoke \
      --resource-group az-spot-orchestrator-rg \
      --name az-spot-orchestrator-vm \
      --command-id RunShellScript \
      --scripts "docker compose pull && docker compose up -d"
```

Both avoid storing an SSH private key in GitHub.

### Secrets Strategy

SEIP stores no secrets in GitHub at all. Sensitive runtime config (API keys, DB
passwords) lives in **AWS SSM Parameter Store** and is fetched by services at
startup. The EC2 instance role grants `ssm:GetParameter` on `/dev/seip-*/*` paths.

---

## Comparison

| | This project (Azure) | SEIP (AWS) |
|---|---|---|
| OIDC issuer | `token.actions.githubusercontent.com` | `token.actions.githubusercontent.com` |
| Token exchange | Azure AD federated credential | AWS STS `AssumeRoleWithWebIdentity` |
| Subject scope | Per-branch (tighter) | Per-repo wildcard (simpler) |
| GitHub Secrets | None | None |
| GitHub Variables | 3 (set by Terraform automatically) | 0 (role ARN hardcoded) |
| Deploy mechanism | `az vm run-command invoke` | `aws ssm send-command` |
| Runtime secrets | Azure Cosmos / Storage keys in `.env` | AWS SSM Parameter Store |
| Setup command | `make oidc-setup` | `terraform apply` (part of full infra) |
| Registry | `ghcr.io` (GitHub, free) | AWS ECR |
