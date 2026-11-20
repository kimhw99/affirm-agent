# The case service. Build it from the repo root, as docker-compose.yml does: besides
# services/case/ it needs contracts/ (kyb_contracts) and scripts/ (validate_golden.py,
# which seed.py runs).
FROM python:3.12-slim

WORKDIR /repo
COPY services/case/requirements.txt services/case/requirements.txt
RUN pip install --no-cache-dir -r services/case/requirements.txt

COPY contracts contracts
COPY scripts scripts
COPY services/case services/case

ENV PYTHONPATH=/repo/contracts PYTHONUNBUFFERED=1
WORKDIR /repo/services/case
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
