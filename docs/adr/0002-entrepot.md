# ADR 0002 - DuckDB plutôt qu'un entrepôt cloud

- **Statut** : accepté
- **Date** : 2026-08-17

## Contexte

Les transformations dbt ont besoin d'un moteur SQL analytique. Le projet
produit quelques millions de lignes par mois, doit rester gratuit, et doit
pouvoir être exécuté par n'importe qui après un `git clone`.

Candidats : BigQuery (offre gratuite), Snowflake (crédits d'essai),
PostgreSQL local, DuckDB.

## Décision

**DuckDB**, en base fichier unique sous `data/warehouse/`.

## Justification

**Contre PostgreSQL.** Postgres est transactionnel, orienté ligne. Sur des
agrégations analytiques balayant des millions de lignes, il est
structurellement plus lent qu'un moteur colonnaire, et il faudrait de plus
charger les Parquet dans des tables au lieu de les lire sur place.

**Contre BigQuery et Snowflake.** Les deux imposent un compte, une carte
bancaire (même sans débit), et une configuration d'identifiants. Pour un
projet destiné à être cloné et exécuté par un lecteur, chaque étape de ce
type fait abandonner une partie du public. S'y ajoute le risque de facture
accidentelle si quelqu'un lance un `--full-refresh` sur un gros volume.

**Pour DuckDB :**

- **Il lit le Parquet directement.** `read_parquet('.../**/*.parquet',
  hive_partitioning = true)` - pas d'étape de chargement, pas de copie des
  données. Le lac *est* la source dbt.
- **Il est colonnaire et vectorisé.** L'ensemble de la chaîne (9 modèles,
  63 tests) s'exécute en ~1 seconde sur un poste ordinaire.
- **Zéro installation.** Une dépendance `pip`, un fichier sur disque.
- **Le SQL est très proche du standard PostgreSQL**, plus `QUALIFY`, les
  fonctions de liste et un excellent support des dates.

## Conséquences

- **Un seul écrivain à la fois.** Le tableau de bord ouvre donc l'entrepôt
  en lecture seule et gère explicitement le cas « verrouillé par un run en
  cours » - plusieurs lecteurs concurrents restent possibles.
- **Pas d'exécution distribuée.** Au-delà de quelques centaines de millions
  de lignes, il faudra migrer.
- **La migration est peu coûteuse par construction** : elle touche
  `profiles.yml`, l'adaptateur dbt et `warehouse/duck.py`. Les modèles SQL,
  les tests et le modèle dimensionnel sont inchangés - c'est précisément
  pourquoi la logique métier vit dans dbt et non dans du code Python
  d'orchestration.
- Les fonctions non standard utilisées (`QUALIFY`, `quantile_cont`,
  `to_timestamp`, `epoch`) ont toutes un équivalent direct en BigQuery et
  Snowflake.
