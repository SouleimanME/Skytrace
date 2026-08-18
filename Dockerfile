# syntax=docker/dockerfile:1

# Image unique pour l'orchestrateur et le tableau de bord : les deux
# services partagent le meme code et le meme lac de donnees, seule la
# commande de demarrage differe.

FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    SKYTRACE_DATA_DIR=/app/data \
    SKYTRACE_DUCKDB_PATH=/app/data/warehouse/skytrace.duckdb \
    DAGSTER_HOME=/app/.dagster_home

WORKDIR /app

# Les dependances sont installees avant le code : tant que `pyproject.toml`
# ne change pas, cette couche reste en cache et les rebuilds sont rapides.
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --no-cache-dir -e ".[transform,orchestration,dashboard]"

COPY dbt/ ./dbt/
COPY orchestration/ ./orchestration/
COPY dashboard/ ./dashboard/
COPY scripts/ ./scripts/
# Configuration de l'instance Dagster (retention, concurrence, telemetrie).
COPY .dagster_home/dagster.yaml ./.dagster_home/dagster.yaml

# Le manifeste dbt est genere au build : en production, `prepare_if_dev()`
# ne le regenere pas, il doit donc exister dans l'image.
RUN mkdir -p /app/data/warehouse "$DAGSTER_HOME" \
    && skytrace dbt parse

# Un utilisateur non privilegie : un conteneur qui tourne en root est une
# mauvaise habitude, meme pour un projet personnel.
RUN useradd --create-home --uid 1000 skytrace \
    && chown -R skytrace:skytrace /app
USER skytrace

EXPOSE 3000 8501

CMD ["dagster", "dev", "-m", "orchestration.definitions", "--host", "0.0.0.0", "--port", "3000"]
