# The API gateway. Build it from the repo root, as docker-compose.yml does: besides
# services/gateway/ it needs contracts/ (kyb_contracts).
FROM python:3.12-slim

WORKDIR /repo
COPY services/gateway/requirements.txt services/gateway/requirements.txt
RUN pip install --no-cache-dir -r services/gateway/requirements.txt

COPY contracts contracts
COPY services/gateway services/gateway

ENV PYTHONPATH=/repo/contracts PYTHONUNBUFFERED=1
WORKDIR /repo/services/gateway
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
