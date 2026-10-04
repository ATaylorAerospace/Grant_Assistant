# Build context is bc/ (see agentArtifact() in amplify/custom/agentcore-stack.ts).
# Shared layout for all GROW2 agents (see bc/invoke-local.sh for running locally):
# one base image and an identical dependency layer so CodeBuild's Docker layer
# cache is reused across agents and across deploys.
FROM public.ecr.aws/docker/library/python:3.12-slim

WORKDIR /app

# Dependency layer — only rebuilt when requirements.txt changes.
COPY eu-grants-search-agent-v2/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application layer — rebuilt on every code change (cheap).
# Shared package: source connectors + domain config (bc/common, incl. the
# domain pack synced to common/domain/ at synth time).
COPY common/ ./common/
COPY eu-grants-search-agent-v2/agent.py .
COPY eu-grants-search-agent-v2/appsync_client.py .
COPY eu-grants-search-agent-v2/bayesian_matcher.py .

EXPOSE 8080
CMD ["python", "agent.py"]
