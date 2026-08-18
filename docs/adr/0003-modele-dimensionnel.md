# ADR 0003 - Modèle en étoile, grain et additivité

- **Statut** : accepté
- **Date** : 2026-08-17

## Contexte

La couche de restitution doit répondre à des questions de natures très
différentes : « où sont les avions maintenant ? », « comment évolue le
trafic sur 24 h ? », « quels aéroports sont les plus actifs ? ». Une seule
table large obligerait à rescanner l'intégralité des positions à chaque
question.

## Décision

Un **modèle en étoile** avec deux dimensions conformes et trois tables de
faits à des grains distincts, chaque grain étant déclaré explicitement.

| Table | Grain |
|---|---|
| `fct_aircraft_positions` | 1 aéronef × 1 instant |
| `fct_traffic_hourly` | 1 heure × 1 pays d'immatriculation |
| `fct_airport_activity` | 1 aéroport × 1 heure |
| `dim_aircraft` | 1 appareil (adresse OACI 24 bits) |
| `dim_airport` | 1 aérodrome |

## Justification

**Pourquoi un grain déclaré.** Le grain est le contrat de la table. Sans
lui, personne ne sait si `count(*)` compte des avions, des positions ou
des relevés. Chaque grain est protégé par un test - le test singulier
`assert_positions_grain_is_unique` échoue si un doublon apparaît dans la
table de faits centrale.

**Pourquoi des agrégats séparés.** `fct_traffic_hourly` et
`fct_airport_activity` répondent en millisecondes à des questions qui
demanderaient de rescanner des millions de lignes sur la table détaillée.
Leur coût de stockage est négligeable (quelques dizaines de lignes par
heure) face au gain d'interactivité.

**Pourquoi `dim_aircraft` est construite par agrégation.** Il n'existe pas
de référentiel d'immatriculations librement exploitable. L'observation
répétée suffit à caractériser un appareil : pays d'enregistrement,
indicatifs employés, plafond atteint, part de temps au sol. C'est une
dimension dérivée, et c'est assumé.

**Additivité - le piège documenté.** `distinct_aircraft` est une mesure
**non additive dans le temps** : sommer 24 heures ne donne pas le nombre
d'appareils de la journée, un même avion volant plusieurs heures. C'est
l'erreur d'analyse la plus fréquente sur ce type de modèle, et elle ne
produit aucun message d'erreur - juste un chiffre faux.

Deux garde-fous :

1. La colonne porte une description dbt explicite, visible dans la
   documentation générée.
2. `position_count`, elle additive, est fournie à côté pour les
   agrégations légitimes.

## Conséquences

- Le tableau de bord ne lit **que** la couche `marts` : il ne recalcule
  aucune agrégation et n'ouvre aucun Parquet.
- Une définition métier qui change se corrige dans un modèle dbt -
  versionné, testé, relu - et non dans une page Streamlit.
- `dim_airport` est une **dimension conforme** : elle est partagée par
  `int_positions_near_airports` et `fct_airport_activity`, garantissant
  qu'« aéroport » désigne la même chose partout.
- Les agrégats doivent être reconstruits après tout changement de
  définition en amont. `dbt build` s'en charge dans le bon ordre.
