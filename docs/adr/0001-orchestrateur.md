# ADR 0001 - Dagster plutôt qu'Airflow

- **Statut** : accepté
- **Date** : 2026-08-17

## Contexte

Le pipeline doit collecter un flux toutes les 15 minutes, déclencher des
transformations dbt, exécuter des contrôles qualité et rester
diagnosticable après un incident. Il doit aussi tourner sur un poste de
développement Windows sans dépendance lourde.

Trois candidats : Airflow, Dagster, ou un simple ordonnanceur système
(cron / Planificateur de tâches).

## Décision

**Dagster**, avec l'intégration `dagster-dbt`.

## Justification

**Contre l'ordonnanceur système.** Un cron lance une commande et ne sait
rien du reste : ni si l'exécution précédente a réussi, ni ce qu'elle a
produit, ni comment reprendre. Aucune traçabilité, aucune interface. C'est
suffisant pour un script, pas pour un pipeline.

**Contre Airflow.** Airflow est la référence du marché et le mot-clé le
plus fréquent dans les offres - l'écarter n'est pas anodin. Trois raisons
ont tranché :

1. **Airflow ne tourne pas nativement sous Windows.** Il exige WSL ou
   Docker. Pour un projet dont un objectif explicite est qu'un lecteur
   puisse le cloner et l'exécuter en trois commandes, c'est une barrière
   à l'entrée réelle.
2. **Airflow orchestre des tâches, Dagster des assets.** Ici l'unité de
   travail *est* une table. Le graphe Dagster montre les tables produites,
   leur fraîcheur et leur volumétrie ; un DAG Airflow montre des rectangles
   qui ont réussi ou échoué.
3. **`dagster-dbt` importe les modèles dbt individuellement.** La lignée va
   du fichier Parquet jusqu'à `fct_airport_activity`, modèle par modèle.
   L'équivalent Airflow (`Cosmos`) existe mais reste une couche
   supplémentaire à maintenir.

**Ce que ça coûte.** Dagster est moins demandé qu'Airflow dans les offres
d'emploi. Le risque est atténué par le fait que les concepts transférables
- DAG, idempotence, backfill, planification, gestion des dépendances -
sont identiques d'un outil à l'autre. Un pipeline Dagster bien construit
se réécrit en Airflow en une journée ; c'est le raisonnement qui se
transfère, pas la syntaxe.

## Conséquences

- L'orchestrateur s'installe via `pip`, sans Docker obligatoire.
- Les contrôles qualité amont s'expriment en `@asset_check` natifs, à côté
  des tests dbt qui apparaissent aussi comme des contrôles dans le graphe.
- Un `docker-compose.yml` est tout de même fourni pour la reproductibilité.
- Si le projet devait être repris par une équipe déjà outillée Airflow, la
  couche `orchestration/` serait à réécrire - mais elle seule : la
  bibliothèque `skytrace` et le projet dbt sont indépendants de
  l'orchestrateur.
