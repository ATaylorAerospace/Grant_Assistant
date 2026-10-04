# Build context is bc/ (see agentArtifact() in amplify/custom/agentcore-stack.ts).
# Shared layout for all GROW2 agents (see bc/invoke-local.sh for running locally):
# one base image and the same layer order so CodeBuild's Docker layer cache is
# reused across deploys. This agent additionally needs WeasyPrint's native libs.
FROM public.ecr.aws/docker/library/python:3.12-slim

# System libraries for WeasyPrint (HTML -> PDF)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpango-1.0-0 \
    libpangoft2-1.0-0 \
    libharfbuzz0b \
    libffi-dev \
    libjpeg-dev \
    libopenjp2-7-dev \
    libcairo2 \
    libgdk-pixbuf-2.0-0 \
    shared-mime-info \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependency layer — only rebuilt when requirements.txt changes.
COPY pdf-converter-agent/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application layer — rebuilt on every code change (cheap).
# Shared package: source connectors + domain config (bc/common, incl. the
# domain pack synced to common/domain/ at synth time).
COPY common/ ./common/
COPY pdf-converter-agent/agent.py .

EXPOSE 8080
CMD ["python", "agent.py"]
