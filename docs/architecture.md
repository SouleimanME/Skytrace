# Architecture

Ce document décrit le fonctionnement interne du pipeline : ce qui circule,
sous quelle forme, et pourquoi.

## 1. Vue d'ensemble

```
OpenSky API  +
             |
OurAirports  +--> Ingestion Python --> Parquet partitionné (bronze)
                                            |
                                            v
                                       DuckDB + dbt
                                 staging -> intermediate -> marts
                                            |
                                            v
                                  Streamlit  /  SQL ad hoc

Dagster orchestre, planifie et trace l'ensemble.
```

Le pipeline suit une architecture en médaillon (bronze / silver / gold).
Chaque couche a un contrat, et une seule raison de changer.

## 2. Couche bronze - le lac de données

### Format et disposition

```
data/raw/opensky_states/
    ingest_date=2026-08-17/
        ingest_hour=19/
            states_1786994411.parquet
            states_1786995311.parquet
        ingest_hour=20/
    ingest_date=2026-08-18/
```

**Un fichier par appel API.** Trois propriétés en découlent :

1. **Idempotence.** Le nom du fichier dérive de l'horodatage du snapshot.
   Rejouer une collecte écrase le même fichier au lieu d'en créer un
   deuxième - la reprise après incident ne duplique jamais rien.
2. **Élagage de partitions.** DuckDB lit la structure Hive et ne descend que
   dans les répertoires concernés par un filtre `WHERE ingest_date = ...`.
3. **Immuabilité.** Aucun fichier n'est jamais modifié. Toute correction
   passe par un modèle dbt, donc versionnée dans git.

### Pourquoi Parquet et non JSON

Le snapshot brut renvoyé par OpenSky pèse environ 470 Ko en JSON pour 900
aéronefs. Le même contenu en Parquet compressé zstd : **47 Ko**, soit un
facteur 10. Sur un an de collecte à 96 snapshots/jour, l'écart est de
~16 Go contre ~1,6 Go.

À cela s'ajoute la lecture colonnaire : une requête sur trois colonnes ne
lit que ces trois colonnes, là où le JSON impose de désérialiser chaque
ligne intégralement.

### Schéma figé

Le schéma Arrow est déclaré explicitement dans
`src/skytrace/opensky/schema.py`, il n'est jamais inféré.

**Raison :** l'inférence produit des schémas incohérents d'un fichier à
l'autre. Un `squawk` entièrement nul sur un snapshot nocturne devient une
colonne de type `null`, incompatible avec la colonne `string` des autres
fichiers - et la lecture globale du lac échoue. Le problème n'apparaît que
plusieurs semaines après la mise en service, quand un creux de trafic
survient. Un test unitaire couvre ce cas
(`test_columns_that_are_entirely_null_keep_their_type`).

## 3. Couches silver et gold - dbt

### Chaîne de transformation

| Modèle | Matérialisation | Rôle |
|---|---|---|
| `stg_opensky__states` | vue | Typage, renommage métier, rejet des positions invalides |
| `stg_ourairports__airports` | vue | Typage `try_cast`, filtrage aux pistes exploitables |
| `int_positions_deduplicated` | vue | Une ligne par (appareil, instant) |
| `int_positions_near_airports` | vue | Rapprochement spatial avion <-> aéroport |
| `fct_aircraft_positions` | **incrémental** | Table de faits centrale |
| `fct_traffic_hourly` | table | Agrégat horaire par pays |
| `fct_airport_activity` | table | Agrégat horaire par aéroport |
| `dim_aircraft` | table | Dimension appareil |
| `dim_airport` | table | Dimension aéroport |

Les couches intermédiaires sont des **vues** : elles ne coûtent rien en
stockage et se recalculent à la volée. Seules les tables de faits et de
dimensions sont matérialisées, car elles sont lues en boucle par le
tableau de bord.

### Une exception au contrat de lecture

Le tableau de bord ne lit que les marts, à une exception près : quand on
clique un appareil sur la carte, il interroge Planespotters pour obtenir une
photographie de CET appareil. Cet appel reste hors du lac, et c'est
délibéré - une image n'est pas une donnée analytique : on ne l'agrège pas, on
ne l'historise pas, et la stocker reviendrait à recopier l'œuvre d'un
photographe. L'appel est mis en cache 24 h et son échec est silencieux : une
vignette manquante ne doit jamais empêcher l'affichage de la fiche.

### Stratégie incrémentale

`fct_aircraft_positions` utilise `incremental_strategy = 'delete+insert'`
avec la clé `(aircraft_icao24, snapshot_at)`.

```sql
{% if is_incremental() %}
where snapshot_at > (
    select coalesce(max(snapshot_at), '1970-01-01'::timestamptz)
    from {{ this }}
)
{% endif %}
```

**`delete+insert` plutôt que `append`** : un rejeu remplace les lignes du
snapshot concerné au lieu de les ajouter une seconde fois. C'est ce qui
rend l'exécution sûre en cas de reprise après incident.

Reconstruction complète si nécessaire :

```bash
skytrace dbt build --full-refresh
```

### Le dédoublonnage n'est pas décoratif

L'ingestion est idempotente mais pas *exactement-une-fois*. Deux workers
qui se chevauchent, ou une relance manuelle, produisent deux fichiers
contenant le même couple (appareil, instant). Sans
`int_positions_deduplicated`, tous les comptages d'aéronefs seraient
gonflés - silencieusement, sans qu'aucune requête n'échoue.

Le test singulier `assert_positions_grain_is_unique` est le garde-fou de
cet invariant.

## 4. Jointure spatiale

Rattacher chaque position basse à l'aéroport survolé est un produit
cartésien : *positions × aéroports*. Avec 5 273 aéroports et une année de
collecte, l'approche naïve devient inexploitable.

**Solution retenue - blocking par grille :**

1. Filtrer les positions à celles au sol ou sous 1 200 m (une minorité).
2. Découper le monde en cellules de 1°, précalculées dans `dim_airport`.
3. Ne comparer chaque position qu'aux aéroports de sa cellule **et des 8
   cellules voisines**.
4. Appliquer haversine uniquement aux couples survivants.
5. Ne garder que l'aéroport le plus proche par position.

**Le voisinage est indispensable.** Sans lui, un avion situé à 200 m d'un
aéroport mais de l'autre côté d'une frontière de cellule ne serait jamais
rattaché - un bug qui ne se manifeste que sur certains aéroports, donc
très difficile à repérer a posteriori.

L'étape 5 évite le double comptage : Orly et Villacoublay sont assez
proches pour qu'un même appareil tombe dans les deux rayons.

## 5. Orchestration

### Assets plutôt que tâches

Dagster raisonne en **assets** - les tables et fichiers produits - et non
en tâches. Le graphe affiche donc ce qui existe, sa fraîcheur, sa
volumétrie et son historique de production.

La lignée est continue de bout en bout parce que les clés d'asset Python
sont alignées sur les sources dbt :

```python
STATES_ASSET_KEY = AssetKey(["raw", "opensky_states"])
```

`dagster-dbt` traduit la source dbt `raw.opensky_states` vers cette même
clé. Résultat : le graphe relie le fichier Parquet au modèle `staging`,
puis aux marts, sans description manuelle.

### Métadonnées historisées

Chaque matérialisation publie des métadonnées (nombre d'aéronefs, crédits
consommés, taille du fichier, empreinte SHA-256 du référentiel). Dagster
les historise, ce qui donne gratuitement des courbes de volumétrie et de
consommation de quota - et permet de voir qu'un fichier amont n'a pas
changé depuis la veille.

### Cadences

| Job | Cron | Justification |
|---|---|---|
| `traffic_pipeline_job` | `*/15 * * * *` | Les positions sont un flux : non collectées, elles sont perdues |
| `reference_refresh_job` | `0 4 * * *` | Le référentiel est un état, il bouge de quelques lignes par mois |

## 6. Gestion du quota

OpenSky facture chaque appel en crédits selon la surface demandée :

| Surface | Coût |
|---|---|
| ≤ 25 deg² | 1 crédit |
| ≤ 100 deg² | 2 crédits |
| ≤ 400 deg² | 3 crédits |
| > 400 deg² ou monde | 4 crédits |

Le barème est modélisé dans `BoundingBox.credit_cost`. Un compteur
(`CreditLedger`) persiste la consommation quotidienne dans un fichier JSON
et refuse tout appel qui dépasserait le budget.

**Pourquoi persister sur disque** : un ordonnanceur qui relance un worker
toutes les 15 minutes repartirait sinon de zéro à chaque exécution, et
dépasserait le quota sans jamais s'en apercevoir.

**Le crédit n'est décompté qu'après une réponse effectivement servie** :
une panne côté OpenSky ne doit pas consommer le quota du jour. Un test
unitaire couvre ce cas.

## 7. Tout est en UTC, et c'est verrouillé

Un `TIMESTAMPTZ` stocke un instant absolu - la donnée en base n'est jamais
ambiguë. Mais DuckDB **l'affiche et le découpe** dans le fuseau de la
session, qui vaut par défaut celui de la machine.

Deux conséquences, l'une cosmétique et l'autre pas :

1. Un relevé de 19:54 UTC s'affichait « 21:54 » sur une machine à Paris,
   sous un libellé « UTC » - un affichage faux.
2. `date_trunc('hour', snapshot_at)` découpe les buckets **dans le fuseau
   de session**. Deux développeurs dans deux fuseaux obtiennent des
   agrégats horaires étiquetés différemment, et un fuseau à décalage non
   entier (Inde, UTC+5:30) déplace carrément les bornes. Le même code, sur
   la même donnée, produit deux résultats.

Le fuseau est donc forcé à UTC aux trois endroits qui lisent l'entrepôt :

| Endroit | Mécanisme |
|---|---|
| `dbt/skytrace/profiles.yml` | `settings: TimeZone: "UTC"` |
| `src/skytrace/warehouse/duck.py` | `SET TimeZone` à l'ouverture |
| `dashboard/app.py` | idem, sur la connexion en lecture seule |

C'est cohérent avec le reste : les partitions du lac (`ingest_date`,
`ingest_hour`) sont dérivées d'UTC, et les plannings Dagster sont déclarés
en `execution_timezone="UTC"`.

## 8. Résilience réseau

| Situation | Comportement |
|---|---|
| 429, 500, 502, 503, 504 | Nouvel essai, backoff exponentiel (2 s -> 60 s) |
| Timeout, erreur de transport | Nouvel essai |
| 401 avec identifiants | Jeton invalidé puis renouvelé, nouvel essai |
| 404, 400 | Échec immédiat - réessayer ne peut pas aider |
| Réponse `states: null` | Snapshot vide, ce n'est pas une erreur (trafic nocturne) |

Le multiplicateur de backoff est un paramètre de configuration
(`SKYTRACE_RETRY_BACKOFF_SECONDS`), mis à zéro dans les tests pour que la
suite s'exécute instantanément.

## 9. Chemin de migration vers le cloud

Le projet est conçu pour que le passage à l'échelle ne soit pas une
réécriture :

| Composant local | Équivalent cloud | Ce qui change |
|---|---|---|
| `data/raw/*.parquet` | S3 / GCS | Le préfixe de chemin |
| DuckDB | BigQuery, Snowflake, Redshift | L'adaptateur dbt et `profiles.yml` |
| dbt | dbt (identique) | Rien - les modèles sont portables |
| Dagster local | Dagster+ / Dagster sur Kubernetes | Le mode de déploiement |
| Streamlit | Metabase, Superset, Looker | La couche de restitution |

Les modèles SQL, les tests et le modèle dimensionnel sont inchangés. C'est
précisément l'intérêt de faire porter la logique métier par dbt plutôt que
par du code d'orchestration.
