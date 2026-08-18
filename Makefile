# Raccourcis de developpement.
# Sous Windows sans `make`, les commandes equivalentes sont dans le README.

PYTHON ?= python

.DEFAULT_GOAL := help
.PHONY: help install demo pipeline ingest build test lint format docs dagster dashboard clean

help: ## Affiche cette aide
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install: ## Installe le projet et toutes ses dependances
	$(PYTHON) -m pip install -e ".[transform,orchestration,dashboard,dev]"

demo: ## Genere un lac synthetique puis construit les modeles (sans reseau)
	$(PYTHON) scripts/generate_demo_data.py --hours 12
	skytrace dbt build

pipeline: ## Pipeline complet sur donnees reelles : ingestion + dbt
	skytrace pipeline

ingest: ## Un seul snapshot de trafic
	skytrace ingest-states

build: ## Transformations dbt et controles qualite
	skytrace dbt build

test: ## Tests unitaires Python
	pytest --cov=skytrace --cov-report=term-missing

lint: ## Analyse statique
	ruff check .
	ruff format --check .

format: ## Reformate le code
	ruff format .
	ruff check --fix .

docs: ## Genere la documentation dbt (lignee, colonnes, tests)
	skytrace dbt docs generate

dagster: ## Interface d'orchestration sur http://localhost:3000
	skytrace dagster

dashboard: ## Tableau de bord sur http://localhost:8501
	skytrace dashboard

clean: ## Supprime les artefacts de build (le lac de donnees est preserve)
	rm -rf dbt/skytrace/target dbt/skytrace/logs .pytest_cache .ruff_cache htmlcov .coverage
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
