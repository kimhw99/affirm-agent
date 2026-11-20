# The auth service. Build it from the repo root, as docker-compose.yml does: besides
# services/auth/ it needs contracts/ (kyb_contracts).
FROM python:3.12-slim

WORKDIR /repo
COPY services/auth/requirements.txt services/auth/requirements.txt
RUN pip install --no-cache-dir -r services/auth/requirements.txt

COPY contracts contracts
COPY services/auth services/auth

ENV PYTHONPATH=/repo/contracts PYTHONUNBUFFERED=1
WORKDIR /repo/services/auth
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
