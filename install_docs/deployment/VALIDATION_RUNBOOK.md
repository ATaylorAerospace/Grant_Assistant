# Live-deploy validation runbook

Validates, against a real AWS account, the changes that were only type-checked
and unit-tested when they were written: deployment-unique resource names and
multi-deployment per region, the `GROW2_ENV` hardening, the scoped deployer IAM,
the Vite-built UI and `deploy-ui.sh`, the connector/domain-pack agent images,
and the scoped teardown. Run it once per release that touches `amplify/custom`,
`installation/`, or `bc/`.

Most checks are automated by **`scripts/validate-deployment.sh`**; this document
is the order to run things in, what a pass looks like, and what to do on a fail.

## Before you start

- A **non-production** account, a supported region (`us-east-1` recommended)
  with **no existing GROW2 stack** (`aws cloudformation list-stacks --query
  'StackSummaries[?starts_with(StackName,`amplify-grow2-`)]'` is empty).
- AdministratorAccess for Steps 1, 5 and 7 (bootstrap, IAM, final teardown);
  the operator policy is tested in Step 5.
- Work from CloudShell for deploys (`sudo -s; mkdir /home/install; cd /home/install;
  git clone …`) and from any machine with the AWS CLI + `curl` + `python3` for the
  checks. Docker is **not** needed.
- Budget: two stacks for ~3 hours. Record times in the table at the end.

Set these once per shell:

```bash
export REGION=us-east-1
export AWS_PAGER=""
```

---

## Step 1 — First deployment (`A`)

```bash
GROW2_IDENTIFIER=vala ./installation/deploy-grow2-bootstrap.sh $REGION
```

Wait for both CodeBuild projects to show **Succeeded** (CodeBuild → Build
projects: `grow2-arm64-deployer-$REGION`, then `grow2-seeder-<account>-$REGION-<id>`).
Note the deployer build's **phase durations** (Build → `npx ampx sandbox --once`)
for the timing table.

In the deployer log, confirm the post_build resolved the stack by name:

```
Root stack: amplify-grow2-vala-sandbox-xxxxxxxx
App bucket: amplify-grow2-vala-sandbox-…-deploymentassetsbucket…
Seeder project: grow2-seeder-<account>-<region>-xxxxxxxx
```

Then:

```bash
./scripts/validate-deployment.sh $REGION --identifier vala --expect-env dev --smoke
```

**Pass:** every section PASS; `--smoke` logs `agent produced results`. WARNs on
the UI (`HTTP 000`) are acceptable only if the seeder is still running.

**Common fails**

| Symptom | Where to look |
|---|---|
| `export GROW2-<id>-… missing` | The stack synthesized with the old names — confirm `amplify/custom/deployment.ts` is in the deployed commit and the deployer log printed `GROW2 deployment id: <id>`. |
| `runtime …_<id> not found` while export points at it | AgentCore runtime replacement still in progress; re-run in 5 min. |
| `export AgentCore-… points at '…'` mismatch | `AGENT_ARN_EXPORT_NAME` env on the Lambda disagrees with the export — check `backend.ts` `addEnvironment('AGENT_ARN_EXPORT_NAME', …)`. |
| smoke: `Lambda … invocation failed` | Guardrail or AppSync discovery; read the Lambda's CloudWatch log for `CloudFormation export not found`. |
| seeder FAILED at `npm run build` | Vite build inside CodeBuild (Node 20). Check `react-aws/package-lock.json` matches `package.json` (`npm ci --legacy-peer-deps` locally). |

---

## Step 2 — Local-development loop against `A`

```bash
GROW2_IDENTIFIER=vala ./scripts/fetch-outputs.sh $REGION          # must pick A unambiguously
cd react-aws && npm ci --legacy-peer-deps && npm run build && cd ..   # Vite build, seconds
cp bc/common/local-env.example bc/common/local-env                 # fill from amplify_outputs + console
./bc/invoke-local.sh proposal-evaluator-agent                       # no AWS writes
./bc/invoke-local.sh grants-search-agent-v2                         # live grants.gov + DynamoDB writes
```

**Pass:** `fetch-outputs.sh` writes `react-aws/src/amplify_outputs.json`; both
harness runs print a JSON response; the grants-search run writes rows to the
`GrantRecord-…` table for `sessionId: local-session-1`.

---

## Step 3 — UI fast path

Make a visible change (e.g. the title in `react-aws/index.html`), then:

```bash
time ./scripts/deploy-ui.sh $REGION
```

**Pass:** prints `Deployed: https://main.<domain>`; the change is live; wall
time ≈ 2 min. Record it.

---

## Step 4 — Second deployment (`B`) in the same region

```bash
GROW2_IDENTIFIER=valb ./installation/deploy-grow2-bootstrap.sh $REGION
./scripts/validate-deployment.sh $REGION --identifier valb --expect-env dev
./scripts/validate-deployment.sh $REGION --identifier vala --expect-env dev   # A still healthy
```

**Pass:** both validations PASS with **different** deployment ids; the summary
line in each shows `2 GROW2 root stack(s) in this region`. Specifically confirm
no CloudFormation *CREATE_FAILED* on: AgentCore runtimes (`…already exists`),
the Guardrail, the WAF WebACL, the OpenSearch collection/security policies,
`kb-docs-<account>-<region>-<id>`, any export (`Export … is already exported`),
the seeder CodeBuild project.

Also run without `--identifier`:

```bash
./scripts/fetch-outputs.sh $REGION
```

**Pass:** exits 1 listing both deployments and asking for `GROW2_IDENTIFIER`.

> Record the deployer build time for `B`: with the CodeBuild local cache warm
> from `A`, the agent image builds should be noticeably shorter than in Step 1.

---

## Step 5 — Scoped deployer IAM (redeploy `B` without admin)

```bash
aws cloudformation deploy --region $REGION --stack-name grow2-deployer-role \
  --template-file installation/iam/grow2-deployer-role.yaml --capabilities CAPABILITY_NAMED_IAM
export GROW2_DEPLOYER_POLICY_ARN=$(aws cloudformation describe-stacks --region $REGION \
  --stack-name grow2-deployer-role --query 'Stacks[0].Outputs[?OutputKey==`PolicyArn`].OutputValue' --output text)

aws iam create-policy --policy-name grow2-operator-policy \
  --policy-document file://installation/iam/grow2-operator-policy.json
```

Attach `grow2-operator-policy` to a **separate** IAM user/role (not an admin),
assume it, and redeploy `B` with a trivial change (e.g. a comment in
`bc/proposal-evaluator-agent/agent.py`):

```bash
GROW2_IDENTIFIER=valb GROW2_DEPLOYER_POLICY_ARN=$GROW2_DEPLOYER_POLICY_ARN \
  ./installation/deploy-grow2-bootstrap.sh $REGION
```

**Pass:** the operator can start the build; the deployer build **Succeeds** with
`AdministratorAccess` detached from `grow2-codebuild-deployer-role`
(`aws iam list-attached-role-policies --role-name grow2-codebuild-deployer-role`
shows only `grow2-codebuild-deployer-role-policy`).

**On `AccessDenied`** in the CodeBuild log: add the named action to **both**
`installation/iam/grow2-deployer-policy.json` and the YAML, redeploy the role
stack, re-run. Keep a list of what was added for the PR.

---

## Step 6 — `GROW2_ENV=prod` on `B`

```bash
GROW2_IDENTIFIER=valb GROW2_ENV=prod ./installation/deploy-grow2-bootstrap.sh $REGION
./scripts/validate-deployment.sh $REGION --identifier valb --expect-env prod
```

**Pass:** PITR enabled and deletion protection **on** for every table; the
demo user is **absent** only if this was a fresh prod deploy — on an upgraded
dev stack the seeder is skipped (idempotency) and the existing demo user stays;
note which case you hit.

---

## Step 7 — Scoped teardown

Delete `A` first and prove `B` survives:

```bash
GROW2_IDENTIFIER=vala ./installation/delete-grow2.sh $REGION
./scripts/validate-deployment.sh $REGION --identifier valb --expect-env prod
```

**Pass:** the delete script prints `Target deployment: amplify-grow2-vala-…`
and `NOTE: 1 other GROW2 deployment(s) exist` and **skips** the account-wide
sweeps; `B`'s validation still PASSes (runtimes, collection, exports, Amplify
app, tables all intact). Record the wall time.

Then `B` (prod → RETAIN leaves data behind by design):

```bash
GROW2_IDENTIFIER=valb ./installation/delete-grow2.sh $REGION
aws dynamodb list-tables --region $REGION --query 'TableNames[?contains(@,`GrantRecord`)]'
aws s3 ls | grep -E 'kb-docs|proposalsbucket'
```

**Pass:** the stack deletes; tables and buckets with RETAIN remain. Delete them
by hand (disable deletion protection first) and note the exact commands in
`install_docs/cleanup/TROUBLESHOOTING.md` under "prod teardown".

Finally remove the role stack and operator policy if they are not kept.

---

## Record the results

Fill this in and commit it with any fixes (replace the estimates in
`install_docs/maintenance/UPDATING.md` with the measured numbers):

| Measurement | Expected | Measured |
|---|---|---|
| First deploy `A` (deployer build) | 35–55 min | |
| Seeder `A` | 8–10 min | |
| Second deploy `B` (warm cache) | < `A` | |
| Redeploy `B`, one agent file changed | 10–15 min | |
| `deploy-ui.sh` | ~2 min | |
| Teardown `A` (with `B` present) | ≤ 30 min | |
| Teardown `B` (prod, RETAIN) | ≤ 30 min | |
| IAM actions added to the deployer policy | none | |

**Done when:** every step passes, the table is filled, and the policy/doc fixes
are merged. Then flip the "not yet run against AWS" wording in the README's
*Environments & multiple deployments* note and in `installation/iam/README.md`.
