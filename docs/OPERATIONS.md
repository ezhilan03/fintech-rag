# AWS operations

## Bootstrap

`infra/aws` creates a dedicated t4g.small host, ECR repository, private versioned encrypted bucket, seven-day CloudWatch log group, IAM roles and restricted SSM command document. It reuses an existing VPC/subnet and GitHub OIDC provider. It does not own Recon resources.

Provide account_id, subnet_id, vpc_id and a tested Amazon Linux 2023 ARM64 ami in an ignored tfvars file. Run Terraform init, validate and plan; inspect the plan before applying. The account guard prevents use of a different account. Back up state securely outside Git after every infrastructure change.

Create `/fintech-rag-portfolio/anthropic-key` as an SSM SecureString through a secure credential workflow. Never put the value in Terraform variables, repository files, workflow inputs or shell history. The runtime role can read only that parameter. The API containers cannot obtain instance-role credentials through IMDS (required tokens, hop limit one).

Create the GitHub `rag-release` environment restricted to `main`, and set environment variables from Terraform outputs: RAG_INSTANCE_ID, RAG_SSM_DOCUMENT, RAG_BUCKET, RAG_REPOSITORY and RAG_RELEASE_ROLE. The trust policy binds the immutable GitHub owner/repository IDs and that environment. A $5 account-wide budget with early warnings must exist before operation; this is a notification guard, not automatic spending enforcement.

## Release and rollback

Run the manual release workflow from main. Its evidence artifact includes the exact Git revision and ECR image digest. Repeating a revision reuses its immutable image; it does not overwrite the tag. Release operations are serialized.

For rollback, an authorized operator may invoke the SSM document with a previously verified revision whose image and release manifest are retained. The current demo script applies additive schema changes only. For future destructive migrations, explicitly design and test rollback before shipping them. Do not assume reverting application code can revert a database schema.

## Backups and failures

The runner writes `backups/<revision>.dump` and `reports/<revision>.json` to S3. Bucket versioning retains previous runs under the same key. A successful report verifies restoring the dump into a disposable database and compares table counts and chunk content checksums before dropping that temporary database. Protect and review retention as data grows.

Application logs are in `/portfolio/fintech-rag-portfolio`; `Portfolio/FintechRAG / DemoSuccess` records outcome. This is an observed metric, not a configured paging alert. A run deliberately stops the database, expects health 503, restarts it and expects health 200. The API and database containers stop when verification finishes. There is no persistent public endpoint.

If the workflow fails, inspect its evidence and the SSM invocation using the administrator account. Do not print the root-only app environment or parameter value. Verify EC2 is stopped. The host stop timer is a fallback, not a substitute for confirming stopped state. Failed bootstrap may still incur up to an hour of instance usage.

## Cost and teardown

Track combined Recon and RAG usage against the account budget. Stopped instances retain billable EBS volumes; ECR, S3 and logs also remain billable. Instance deletion alone does not remove the retained root disk. Full teardown must deliberately inventory and delete unwanted retained disks, images, bucket versions and parameter values after preserving required evidence/backups. Terraform intentionally refuses forced deletion of repositories/buckets with data.
