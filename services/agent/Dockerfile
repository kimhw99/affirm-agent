# The agent service. Build it from the repo root, as docker-compose.yml does: besides
# services/agent/ it needs contracts/ (kyb_contracts).
FROM python:3.12-slim

WORKDIR /repo
COPY services/agent/requirements.txt services/agent/requirements.txt
RUN pip install --no-cache-dir -r services/agent/requirements.txt

COPY contracts contracts
COPY services/agent services/agent

ENV PYTHONPATH=/repo/contracts PYTHONUNBUFFERED=1
WORKDIR /repo/services/agent
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
