# Guide de lancement

Comment installer le projet, faire tourner le pipeline, et voir les donnees
sous toutes leurs formes. Toutes les commandes se lancent depuis la racine
du depot, dans un terminal.

## 1. Installation (une seule fois)

```bash
python -m venv .venv
```

```bash
.venv\Scripts\activate
```

```bash
pip install -e ".[transform,orchestration,dashboard,analysis,dev]"
```

A partir de la, la commande `skytrace` est disponible tant que
l'environnement est active (`.venv\Scripts\activate` a chaque nouveau
terminal).

## 2. Remplir la base (premier run)

Le tableau de bord et l'analyse lisent l'entrepot DuckDB. Il faut donc au
moins un passage du pipeline pour le remplir :

```bash
skytrace pipeline
```

Cette commande fait tout : un snapshot du trafic OpenSky, le referentiel
aeroports (si absent), la qualite de l'air (si perimee), puis la
transformation dbt et ses tests. A refaire quand on veut de la donnee
fraiche.

## 3. Voir les donnees

Cinq surfaces, chacune pour un usage different.

### a. Le tableau de bord (le plus visuel)

```bash
skytrace dashboard
```

Ouvre `http://localhost:8501` : carte du dernier releve, courbe du trafic,
classements des pays et aeroports, et la section trafic / qualite de l'air.
C'est la vue a montrer a quelqu'un de non technique.

### b. L'orchestrateur Dagster (la sante du pipeline)

```bash
skytrace dagster
```

Ouvre `http://localhost:3000` : le graphe de lignee (du fichier Parquet
jusqu'aux marts), l'historique des executions, les controles qualite. Pour
que la collecte tourne toute seule, activer les deux plannings dans l'onglet
Automation.

### c. La documentation dbt (le catalogue de donnees)

```bash
skytrace dbt docs generate
```

```bash
skytrace dbt docs serve
```

Ouvre `http://localhost:8080` : chaque modele avec sa description, chaque
colonne, les tests attaches, le SQL compile, et un graphe de lignee
interactif.

### d. L'analyse chiffree (trafic et qualite de l'air)

```bash
python scripts/analyse_qualite_air.py
```

Recalcule la correlation trafic / NO2 et regenere le rapport
[`docs/analyse_trafic_qualite_air.md`](analyse_trafic_qualite_air.md) et sa
figure a partir des donnees courantes.

### e. En SQL, directement

Etat du lac, de l'entrepot et des quotas :

```bash
skytrace info
```

Une requete ad hoc (mettre le `--limit` en option, jamais dans le SQL) :

```bash
skytrace dbt show --limit 15 --inline "select airport_label, sum(distinct_aircraft) as aeronefs from marts.fct_airport_activity group by 1 order by 2 desc"
```

## 4. Faire tourner la collecte en continu

- **En local** : `skytrace dagster`, puis activer les plannings dans
  l'onglet Automation. La collecte se declenche alors toute seule.
- **Dans le cloud** : c'est deja le cas. Le workflow GitHub Actions
  *Collecte planifiee* collecte une fois par heure et publie les donnees
  dans le depot ; le tableau de bord Streamlit se met a jour a chaque push.

## 5. Verifier que tout est sain

```bash
pytest -q
```

```bash
ruff check .
```

```bash
skytrace dbt build
```

## Aide-memoire

| Commande | Effet |
|---|---|
| `skytrace pipeline` | Collecte + transformation, en une fois |
| `skytrace dashboard` | Tableau de bord sur localhost:8501 |
| `skytrace dagster` | Orchestrateur sur localhost:3000 |
| `skytrace dbt docs generate` puis `serve` | Documentation dbt sur localhost:8080 |
| `python scripts/analyse_qualite_air.py` | Analyse trafic / qualite de l'air |
| `skytrace info` | Etat du lac, de l'entrepot, des quotas |
| `skytrace ingest-states` | Un seul snapshot de trafic |
| `skytrace ingest-air-quality` | Rafraichit la qualite de l'air |
| `skytrace dbt build` | Transformations et tests dbt |
| `pytest -q` / `ruff check .` | Tests unitaires / analyse statique |

## En ligne (ce que voit un recruteur)

- Code : https://github.com/SouleimanME/Skytrace
- Executions du pipeline : onglet Actions du depot
- Demo : l'URL Streamlit affichee au deploiement
