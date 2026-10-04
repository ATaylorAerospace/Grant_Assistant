# GROW2 Architecture — platform and domain

GROW2 is two things stacked on top of each other:

- a **platform**: authenticated multi-agent application on AWS (Cognito, AppSync,
  Lambda, Bedrock AgentCore, Knowledge Base on OpenSearch, Guardrails, WAF, a
  React shell, one-command deploy), and
- a **domain**: everything that makes it about *research grants* — where
  opportunities come from, how they are scored against a researcher, and which
  proposal sections each funder expects.

The split exists so that the second part can change without touching the first.
A grants deployment with modest changes edits the domain layer; a different
funding domain replaces it.

```
┌─────────────────────────────────────────────────────────────────────────┐
│ DOMAIN  (grants)                                                         │
│  config/domains/grants/   prompts/ · matching.json · sources.json        │
│  bc/common/sources/       grants_gov.py · eu_funding_portal.py           │
│  (matcher: bc/common/matching.py evaluates matching.json — platform)     │
│  amplify/data/resource.ts GrantRecord · EuGrantRecord · Proposal …       │
│  react-aws/src/components Grant Search · Proposals · Profile pages       │
├─────────────────────────────────────────────────────────────────────────┤
│ PLATFORM                                                                 │
│  amplify/auth · amplify/data (API, auth rules) · amplify/backend.ts      │
│  amplify/custom/   agentcore-stack · opensearch-collection-stack ·       │
│                    bedrock-prompts-stack · post-deployment-seeder ·      │
│                    deployment.ts (ids, dev/prod)                         │
│  amplify/functions/  kb-* pipeline · chat-handler · *-search-v2 glue ·   │
│                      agent-discovery-* · proposal-* glue                 │
│  bc/common/        GrantSource interface · domain_config loader          │
│  bc/*/agent.py     AgentCore runtimes (orchestration, A2A, persistence)  │
│  react-aws/        app shell, auth, layout, GraphQL client               │
│  installation/     CloudShell deploy, CodeBuild, IAM, teardown           │
└─────────────────────────────────────────────────────────────────────────┘
```

## Request flow

1. The React app calls AppSync (Cognito-authenticated, WAF rate-limited).
2. A Lambda resolver (`amplify/functions/grants-search-v2`, …) validates input,
   applies the Bedrock Guardrail, and invokes an AgentCore runtime by the ARN it
   reads from a CloudFormation export at run time.
3. The agent (`bc/<agent>/agent.py`) returns immediately and does the work in a
   background thread: it asks a **source connector** for grants, scores them with
   the **matcher**, and streams results back through AppSync mutations
   (`appsync_client.py`) so the UI updates live.
4. Proposal generation calls the PDF converter and the evaluator as sub-agents
   (A2A), reads the Knowledge Base, and uses the per-agency **prompts**.

Only step 3's connector call, the matcher's weights/feature lists, and step 4's
prompts are domain-specific.

## The domain layer

### Source connectors — `bc/common/sources/`

`GrantSource` (`base.py`) is the single interface to an external catalogue:

```python
class GrantSource(ABC):
    name: str                                   # 'GRANTS_GOV', 'EU_FUNDING', …
    def search(self, query, filters=None, **context) -> list[dict]   # UI-format grants
    def fetch(self, identifier) -> dict                               # one record's details
```

Connectors return the **UI format** documented in `base.py` (`grantId`, `title`,
`agency`, `amount`, `deadline`, `description`, …, `source`). Nothing downstream —
matcher, DynamoDB/AppSync writers, React — sees a provider's raw schema.

Two connectors exist: `GrantsGovSource` (US federal, live API) and
`EuFundingPortalSource` (EU portal, read from the nightly S3 cache that
`amplify/functions/eu-grants-cache-downloader` writes). The search agents only
hold thin aliases to them; their own code is orchestration and persistence.

**Adding a source** (a foundation database, an institutional feed, another
country's portal):

1. Add `bc/common/sources/<name>.py` implementing `GrantSource`; put its
   endpoints/limits in `config/domains/<domain>/sources.json`.
2. Register it in `bc/common/sources/__init__.py`.
3. Either route it through an existing search agent (`sources: ['<NAME>']` in
   the payload) or add a runtime in `amplify/custom/agentcore-stack.ts` using
   `agentArtifact('<dir>')` and a Lambda resolver like `grants-search-v2`.
4. Add tests next to `bc/common/tests/test_sources.py` (mock `httpx`/`boto3`).

### Domain config pack — `config/domains/<domain>/`

Data, not code: `prompts/` (deployed by `bedrock-prompts-stack.ts`),
`matching.json` (priors, feature likelihoods, keyword field weights — read by
both matchers through `common.domain_config`), `sources.json`. See the pack's
README. `agentcore-stack.ts` copies the JSON into `bc/common/domain/` at synth
so it ships inside every agent image; `bc/invoke-local.sh` points
`GROW2_DOMAIN_DIR` at the pack for local runs.

### What is still code

- The matcher itself (`bc/common/matching.py`) is platform code: it evaluates
  the `features`, `featureLikelihoods`, `keywordWeights` and per-`sources`
  behaviour declared in `matching.json`, so a new domain changes the JSON, not
  the Python. Golden tests in `bc/common/tests/test_matching.py` pin its
  numbers to the original per-agent implementations.
- The GraphQL models in `amplify/data/resource.ts` (`GrantRecord`,
  `EuGrantRecord`) and the React pages that render them.
- The agency list in `amplify/custom/agent-discovery-stepfunction-v2.ts` and the
  seeded `AgentConfig`.

## The platform layer

| Area | Code | Notes |
|------|------|-------|
| Deployment identity, dev/prod | `amplify/custom/deployment.ts` | Suffixes every account-unique name; flips retention/PITR/deletion-protection |
| Auth + API | `amplify/auth`, `amplify/data`, `backend.ts` (WAF, Guardrail, IAM) | Owner-scoped auth rules; Lambdas call AppSync with IAM |
| Agents | `amplify/custom/agentcore-stack.ts`, `bc/*/agent.py`, `bc/*/appsync_client.py` | One image per agent from the `bc/` context; least-privilege roles |
| Knowledge base | `opensearch-collection-stack.ts`, `amplify/functions/kb-*` | Upload → process → vector index → retrieve |
| Delivery | `installation/`, `scripts/`, `.github/workflows/ci.yml` | CloudShell bootstrap → CodeBuild ARM64 → seeder; local loop; UI fast deploy |

## Operational boundaries

- **Secrets and identifiers** never live in the repo: stack-specific names reach
  agents as environment variables set by CDK (`local-env.example` lists them).
- **Scores** are owned by the matcher; connectors must not set them.
- **Raw provider data** stops at the connector; if a UI needs a provider field,
  add it to the UI format with a provider-prefixed name (`euReference`, …).
