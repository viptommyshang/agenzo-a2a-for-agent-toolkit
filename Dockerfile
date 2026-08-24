FROM python:3.11-slim

WORKDIR /app

# Install uv for fast dependency management
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Copy project files
COPY pyproject.toml uv.lock ./
COPY agenzo_a2a_for_agent_toolkit/ ./agenzo_a2a_for_agent_toolkit/

# Install dependencies
RUN uv sync --frozen

# ── MCP Transport ──
ENV AGENZO_MCP_TRANSPORT=sse
ENV AGENZO_MCP_HOST=0.0.0.0
ENV AGENZO_MCP_PORT=8080

# ── Orchestrator (defaults, override via docker-compose or -e) ──
ENV AGENZO_A2A_BASE_URL=http://localhost:8000
ENV AGENZO_A2A_AGENT_ID=base-orchestrator
ENV AGENZO_A2A_MEMBER_ID=prod-user-001
ENV AGENZO_A2A_AGENT_NAME=kiro-agent
ENV AGENZO_A2A_STREAM=1
ENV AGENZO_A2A_HTTP_TIMEOUT=180

# Auth: set via runtime env (docker-compose / -e flags)
# ENV AGENZO_A2A_API_KEY=
# ENV AGENZO_A2A_API_KEY_FILE=
# ENV AGENZO_A2A_INVITATION_CODE=

EXPOSE 8080

CMD ["uv", "run", "python", "-m", "agenzo_a2a_for_agent_toolkit.server"]
