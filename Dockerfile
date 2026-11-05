FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY requirements-livekit-agent.txt ./
RUN pip install --no-cache-dir -r requirements-livekit-agent.txt \
    && useradd --create-home --uid 10001 agent
COPY --chown=agent:agent livekit_agent/ ./livekit_agent/
USER agent
CMD ["python", "-m", "livekit_agent.interview_agent", "start"]
