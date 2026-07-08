# Code Review — Top 8 Errors, Bugs & Omissions

_Scope: AWS Amplify Gen2 backend, Python Bedrock AgentCore agents (`bc/`), Lambda handlers (`amplify/functions/`), and the React frontend (`react-aws/`)._

Each finding below was verified by reading the cited source lines directly. Findings are ranked by severity. No code fixes are applied in this document — it is a review; recommended fix directions are included for each item.

---

## 1. CRITICAL — Cross-tenant data exposure: every model is readable/writable by any user or API-key holder

**Where:** `amplify/data/resource.ts` (lines 80, 99, 115, 138, 192, 212, 261–289, 468, 525, 576–605, and `expiresInDays: 365` at line 638); client usage in `react-aws/src/graphql/client.js:15,44-54,51`

Nearly every data model — including `UserProfile` (email, name, institution, ORCID) and `Proposal` — is authorized with:

```ts
.authorization((allow) => [allow.authenticated(), allow.publicApiKey()])
```

Only one model (line 245) uses `allow.owner()`. There is **no owner scoping** on the PII/content models, so:

- Any **logged-in user** can read/write **every other user's** profiles and proposals (`allow.authenticated()` grants access to all rows, not just their own).
- The **public API key** lives 365 days and is shipped in the client bundle. `client.js` sends it as `x-api-key` (line 51) and, worse, **silently falls back to it** whenever the Cognito session lookup throws (lines 44–54) — an auth failure becomes unauthenticated, un-scoped access rather than a hard failure.

**Impact:** Full cross-tenant read/write of PII and user content by anyone with the (bundle-extractable) key, for up to a year.

**Fix direction:** Add `allow.owner()` to user-data models and drop `publicApiKey` from anything holding PII. Reserve the API key for genuine system/agent paths (or move the Python agents to IAM auth). In `client.js`, fail closed instead of falling back to the API key.

---

## 2. HIGH — Permanent backdoor account seeded on every deploy

**Where:** `amplify/custom/post-deployment-seeder.ts:383-401` (email/password again at line 447)

```python
EMAIL = "test_user@example.com"
PASSWORD = "Password123!"
...
cognito.admin_set_user_password(..., Password=PASSWORD, Permanent=True)
```

The post-deployment CodeBuild step creates a Cognito user with a **hardcoded, permanent, publicly-known password** on every deploy. Combined with finding #1 (no owner isolation), this is a standing credential into all production data.

**Fix direction:** Remove the seeded user, or gate it strictly behind a non-production flag and use a generated secret that is never committed.

---

## 3. HIGH — IDOR: any authenticated user can list any other user's proposals

**Where:** `amplify/functions/proposals-query/handler.py:51-61`

```python
if field_name == 'listProposalsByUser':
    user_id = arguments.get('userId')   # client-supplied, never checked
    ...
    response = table.query(
        IndexName='proposalsByUserId',
        KeyConditionExpression=Key('userId').eq(user_id),
        ...
    )
```

`userId` comes straight from the GraphQL argument, and the resolver auth is only `allow.authenticated()`. The handler never reads `event['identity']['sub']` or compares it to the requested `userId`. Any logged-in user can call `listProposalsByUser(userId: "<victim-sub>")` and receive another user's proposals. The sibling `proposal-download/handler.py:65` *does* verify ownership — this handler is the outlier.

**Bonus bug in the same file:** errors return `json.dumps({'error': ...})` (a JSON *string*) while success returns a dict (lines 54, 78, 82). The resolver returns `a.json()`, so failures surface as a normal 200 payload the client can't distinguish from success.

**Fix direction:** Derive `user_id` from `event.identity.sub` (or reject when it doesn't match the argument), and return a consistent shape / raise on error.

---

## 4. HIGH — S3 event loop: document processor re-triggers itself and double-ingests every document

**Where:** `amplify/backend.ts:1328-1332` + `amplify/functions/kb-document-processor/handler.py:108-118, 329`

The S3 notification fires on `OBJECT_CREATED` with `{ prefix: 'user-' }` and **no suffix filter**:

```ts
s3.EventType.OBJECT_CREATED,
...
{ prefix: 'user-' }
```

The processor writes extracted text back under the same prefix:

```python
# store_extracted_text → user-{userId}/{documentId}/extracted.txt
text_key = f"{prefix}/extracted.txt"
s3_client.put_object(Bucket=bucket, Key=text_key, ...)
```

That `PUT` re-matches the notification filter and re-invokes the processor on `extracted.txt`, which calls `start_ingestion_job` a second time (line 329) and re-flips the document status. Copy events are filtered, but the `extracted.txt` PUT is not.

**Impact:** Every PDF triggers a redundant Bedrock ingestion job; under load this produces `ConflictException` storms.

**Fix direction:** Add a suffix filter to the S3 notification (or per-extension filtering), or early-return in the handler for keys ending in `extracted.txt`.

---

## 5. HIGH — EU grant records silently never persist: a dict is written into a String GraphQL field

**Where:** `bc/eu-grants-search-agent-v2/agent.py:524` (extraction logic that should be reused is at lines 473–479)

```python
'euFrameworkProgramme': grant.get('frameworkProgramme', ''),
```

The raw EU API `frameworkProgramme` is a dict (`{description, abbreviation}`). The code even extracts a proper string from that same dict ten lines earlier for the `agency` field:

```python
framework_programme = grant.get('frameworkProgramme', {})
if isinstance(framework_programme, dict):
    agency = (framework_programme.get('description')
              or framework_programme.get('abbreviation')
              or 'European Commission')
```

But `euFrameworkProgramme` gets the raw dict. It flows into the `createEuGrantRecord` AppSync mutation, whose field is a `String` (the `''` default confirms this), so the mutation is rejected. `create_eu_grant_record` returns `False`, and `write_eu_grant_record` (line 585) **ignores the return value** — so the failure is swallowed.

**Impact:** EU grant records are never saved, yet the agent still reports `SEARCH_COMPLETE`.

**Fix direction:** Store the extracted abbreviation/description string (mirror the `agency` extraction), and check/log the mutation return value instead of discarding it.

---

## 6. HIGH — Proposal evaluator grades against empty guidelines (40% of the quality score is meaningless)

**Where:** `bc/proposal-generation-agent/agent.py:401` → `bc/proposal-evaluator-agent/agent.py:317-318`

The generator passes the prepared prompts as `prompt`:

```python
evaluation = call_proposal_evaluator_agent(
    proposal_content=complete_proposal,
    prompt=prepared_prompts,   # Dict[section_name -> prompt_string]
    ...
)
```

But the evaluator expects an object with `content` / `successCriteria`:

```python
prompt_content = prompt.get('content', '')          # -> ''
success_criteria = prompt.get('successCriteria', []) # -> [] (and never used)
```

`prepared_prompts` is a `Dict[section_name → prompt_string]`, so `.get('content')` and `.get('successCriteria')` are always missing. `evaluate_guideline_adherence` (weighted **40%**) then asks Claude to grade the proposal against an empty "GRANT GUIDELINES AND SUCCESS CRITERIA" block.

**Impact:** Every evaluation silently produces a degraded/bogus guideline-adherence score.

**Fix direction:** Pass a `{content, successCriteria}` payload (e.g. concatenated section prompts plus the criteria), or make the evaluator consume the section-keyed dict directly.

---

## 7. MED-HIGH — kb-search: cross-user leak on enrichment failure, and pagination breaks Bedrock's 100-result cap

**Where:** `amplify/functions/kb-search/handler.py:107-129`

**(a) Fail-open user isolation.** Bedrock retrieval intentionally runs with `metadata_filter=None` across **all** users' documents; isolation happens only in `enrich_search_results`. On any exception the code falls back to the raw hits:

```python
try:
    enriched_results = enrich_search_results(search_results, user_id)
except Exception as e:
    print(f"Error enriching search results: {str(e)}")
    enriched_results = search_results   # raw, unfiltered, cross-user hits
```

The later `apply_dynamodb_filters` only checks agency/category/date — not ownership. A transient DynamoDB error leaks other users' document excerpts.

**(b) Pagination exceeds Bedrock's cap.** `numberOfResults` is capped at 100 by Bedrock, but:

```python
fetch_limit = (limit + offset) * 2   # unclamped
```

With e.g. `limit=10, offset=45`, `fetch_limit=110` → `ValidationException`, breaking the whole search on any second page or large limit.

**Fix direction:** Fail closed (return an error, not raw results) on enrichment failure, and clamp `fetch_limit = min(fetch_limit, 100)`.

---

## 8. MEDIUM — Copy/paste drift: US search agent nulls out legitimate 0.0 scores (EU copy already fixed)

**Where:** `bc/grants-search-agent-v2/agent.py:587-588` vs `bc/eu-grants-search-agent-v2/agent.py:574-575`

US (buggy):

```python
"profileMatchScore": float(grant.get('profileMatchScore', 0)) if grant.get('profileMatchScore') else None,
"keywordScore": float(grant.get('keywordScore', 0)) if grant.get('keywordScore') else None,
```

EU (fixed):

```python
"profileMatchScore": float(grant.get('profileMatchScore', 0)) if grant.get('profileMatchScore') is not None else None,
"keywordScore": float(grant.get('keywordScore', 0)) if grant.get('keywordScore') is not None else None,
```

The US truthiness test treats a real computed `0.0` as absent and stores `None`. `keywordScore` is legitimately `0.0` whenever no query terms match, so those grants lose their scores in the UI. This is emblematic of broader drift between the two agent copies — for example `relevanceScore` means *keyword* score in the US matcher (`bayesian_matcher.py:448`) but *profile* score in the EU matcher (`bayesian_matcher.py:367`), producing inconsistent ranking and stored values.

**Fix direction:** Apply the `is not None` guard in the US agent; longer-term, factor the shared scoring/serialization logic into one module to stop the two copies from diverging.

---

## Notable runner-ups

These are real defects that didn't make the top 8 but are worth fixing:

- **Step Function timeout too short.** `amplify/custom/agent-discovery-stepfunction-v2.ts:145` sets the state-machine timeout to 8 minutes, but the critical path's own task timeouts sum to far more (invoke task alone is 10 min at `:60`; plus wait/consolidate/update). Long searches abort mid-run, leaving results unwritten and config unstamped.
- **No HTTP timeout on AppSync mutations.** All three `appsync_client.py` copies call `requests.post(...)` (~line 59) with no `timeout=`. A single stalled connection wedges the daemon background thread indefinitely.
- **Unpaginated scan drops users.** `amplify/functions/agent-discovery-scheduler/handler.py:39-46` does a single `table.scan` with a filter and no `LastEvaluatedKey` loop. Once the AgentConfig table exceeds one 1 MB page, active configs beyond it silently never get scheduled discovery.
- **JWT tokens logged.** `react-aws/src/components/TokenDebugger.jsx:25-26,74` prints full Cognito access & ID tokens to the console and renders them into the DOM. This debug component should never ship to production.
- **User data destroyed on stack replace.** The KB document bucket (`amplify/custom/opensearch-collection-stack.ts:63-70`) and the proposals bucket (`amplify/backend.ts:1468-1476`) use `RemovalPolicy.DESTROY` + `autoDeleteObjects`; the proposals bucket additionally expires objects after 7 days. Stack replacement silently deletes user uploads/generated proposals.
- **OpenSearch network/data policy over-broad.** `amplify/custom/opensearch-collection-stack.ts:141-147,190-196` sets `AllowFromPublic: true` and grants `iam::<account>:root` on the collection — publicly reachable and accessible to any principal in the account.
- **Unbound variable in except block.** `bc/user-profile-agent/agent.py:261` logs `profile_response` in the `except`, but if `get_item()` raised, that name was never bound → `NameError` masks the real DynamoDB error and skips the graceful return.
- **Warm-container race.** Both search agents and the proposal agent store request-scoped state in module globals and mutate `os.environ` inside `invoke()` while a background thread runs for minutes. A second invocation on the same warm container overwrites the first thread's tables/endpoint.
- **EU-branch indentation bug (legacy path).** `amplify/functions/agent-discovery-search/handler.py:1106-1134`: the per-session `for` loop sits outside the `else`, so when `EU_GRANT_RECORDS_TABLE` is unset the code logs "skipping" and then hits undefined `eu_table` → `NameError`. Also `:353` has an unconditional `raise` that makes the `force_run` early-return and interval-check paths below it dead code.
- **First-page-only listings.** `amplify/functions/prompt-manager/handler.py:79` uses `maxResults=50` and ignores `nextToken`, returning only the first page of prompts.
- **Client-declared file size trusted.** `amplify/functions/kb-document-upload/handler.py:95-99,256-265` validates the client-supplied `fileSize` but the presigned PUT has no `content-length-range`, so a client can declare a small size and upload an arbitrarily large object.

## Housekeeping (dead/stale files)
- `amplify/backend.ts.backup-20260127-185458` — committed 54 KB backup with older, broader IAM policies.
- `amplify/functions/kb-document-processor/handler_fixed.py` — stale duplicate of the deployed `handler.py`.
- `amplify/custom/index-creator-lambda.py` — effectively empty; the real one is `amplify/functions/opensearch-index-creator/handler.py`.
