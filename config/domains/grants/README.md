# Grants domain pack

Everything that makes GROW2 a *research-grant* assistant, as data rather than
code. The platform (auth, API, agents, knowledge base, UI shell) reads this pack;
a different funding domain is a different pack plus its source connectors.

| File | Read by | What it controls |
|------|---------|------------------|
| `prompts/*.json` + `manifest.json` | `amplify/custom/bedrock-prompts-stack.ts` (deployed as Bedrock managed prompts) | Per-agency proposal section prompts (NSF, NIH, DOE, NASA, European Commission). See [Adding Custom Prompts](../../../install_docs/reference/ADDING_PROMPTS.md). |
| `matching.json` | `bc/common/matching.py` via `common.domain_config.matching()` | Feature definitions (keyword lists, amount bands), researcher priors, feature likelihood ratios, keyword field weights, profile boosts, per-source behaviour (`sources`). |
| `sources.json` | `bc/common/sources/*` via `common.domain_config.sources()` | Endpoints, result limits and status filters per source connector. |

## How the pack reaches the agents

The AgentCore images are built from `bc/`. At synth time `agentcore-stack.ts`
copies this directory's JSON files to `bc/common/domain/` (git-ignored), and
`bc/invoke-local.sh` does the same for local runs — or set `GROW2_DOMAIN_DIR`
to this directory. Prompts are deployed by CDK directly from `prompts/`.

## Editing

- Change a weight: edit `matching.json`, run `./bc/invoke-local.sh grants-search-agent-v2`
  to see the effect, then deploy (only the two search images rebuild).
- Change a prompt: edit the JSON under `prompts/`, deploy (CDK diffs the prompts).
- Add a source: write a connector in `bc/common/sources/`, register it, add its
  endpoints here. See `docs/ARCHITECTURE.md`.
