# SkyTrace

**Pipeline de données end-to-end sur le trafic aérien européen.**
Collecte les positions ADS-B de tous les avions en vol toutes les 15 minutes,
les transforme en modèle dimensionnel testé, et les expose dans un tableau
de bord.

> Ingestion Python -> lac Parquet partitionné -> DuckDB -> dbt (staging /
> intermediate / marts) -> Dagster -> Streamlit. Tourne en local sans compte
> cloud, **et se déploie gratuitement en ligne** (GitHub Actions + Streamlit
> Community Cloud) - voir [Déploiement](#déploiement-en-ligne-gratuit).

<!--
Une fois le dépôt public créé, coller les badges ci-dessous (remplacer
<compte> par ton identifiant GitHub) et le lien de la démo Streamlit :

[![CI](https://github.com/<compte>/skytrace/actions/workflows/ci.yml/badge.svg)](https://github.com/<compte>/skytrace/actions/workflows/ci.yml)
[![Collecte](https://github.com/<compte>/skytrace/actions/workflows/collect.yml/badge.svg)](https://github.com/<compte>/skytrace/actions/workflows/collect.yml)

**Démo en ligne** : https://<compte>-skytrace.streamlit.app
-->

## Pourquoi ce projet

La plupart des projets de portfolio « data » sont des notebooks sur un CSV
figé. Ils ne montrent aucune des compétences qu'on exerce réellement en
Data Engineering : **la donnée qui arrive en continu, qui est sale, qui
grossit, et qu'il faut fiabiliser**.

Les positions ADS-B ont exactement ces propriétés :

| Propriété | Conséquence technique démontrée |
|---|---|
| Flux éphémère (une position non collectée est perdue) | Ordonnancement, idempotence, rejeu |
| ~900 aéronefs × 96 relevés/jour | Partitionnement, format colonnaire, modèle incrémental |
| Champs nuls, indicatifs mal formés, positions aberrantes | Tests de qualité, couche de nettoyage isolée |
| API à quota strict | Gestion de budget, backoff exponentiel, tolérance aux pannes |
| Aucun référentiel intégré | Jointure spatiale avec une source externe |

## Architecture

```mermaid
flowchart LR
    subgraph sources["Sources"]
        A["OpenSky Network<br/>API REST / OAuth2"]
        B["OurAirports<br/>CSV"]
        B2["Open-Meteo<br/>qualité de l'air"]
        B3["OpenSky aircraft DB<br/>+ OpenFlights"]
    end

    subgraph bronze["Bronze - lac de données"]
        C["Parquet zstd<br/>ingest_date=/ingest_hour=<br/><i>brut, immuable</i>"]
    end

    subgraph silver["Silver - staging dbt"]
        D["stg_opensky__states<br/>stg_ourairports__airports<br/><i>typage, nettoyage</i>"]
        E["int_positions_deduplicated<br/>int_positions_near_airports<br/><i>dédoublonnage, jointure spatiale</i>"]
    end

    subgraph gold["Gold - marts dbt"]
        F["fct_aircraft_positions<br/><i>incrémental</i>"]
        G["fct_traffic_hourly<br/>fct_airport_activity"]
        H["dim_aircraft<br/>dim_airport<br/>dim_airline"]
        K["fct_airport_hourly_air_quality<br/><i>trafic x pollution</i>"]
        L["fct_airline_airport_activity<br/><i>compagnies par aéroport</i>"]
    end

    I["Streamlit"]

    A --> C
    B --> C
    B2 --> C
    B3 --> C
    C --> D --> E --> F --> G
    D --> H
    F --> H
    E --> L
    G --> K
    G --> I
    H --> I
    K --> I
    L --> I

    J["Dagster<br/>ordonnancement + lignée + contrôles"] -.pilote.-> C
    J -.pilote.-> D
```

**Séparation des responsabilités** - chaque couche a un contrat clair :

- **Bronze** : fidèle à la source, aucune interprétation. Permet de rejouer
  n'importe quelle transformation sans re-consommer de quota API.
- **Silver** : typage, nettoyage, dédoublonnage. Correspondance 1<->1 avec la
  source, donc toute anomalie est localisable.
- **Gold** : modèle en étoile, vocabulaire métier. C'est la seule couche que
  le tableau de bord a le droit de lire.

## Démarrage rapide

> Guide complet (installer, lancer, voir les données sous toutes leurs
> formes) : [`docs/guide-lancement.md`](docs/guide-lancement.md).

### 1. Installation

```bash
python -m venv .venv
```

```bash
.venv\Scripts\activate
```

```bash
pip install -e ".[transform,orchestration,dashboard,dev]"
```

### 2. Configuration (optionnelle)

```bash
copy .env.example .env
```

**Le pipeline fonctionne sans aucune configuration**, en mode anonyme
(400 crédits/jour, zone France). Pour aller plus loin, créer un compte sur
[opensky-network.org](https://opensky-network.org), générer un client API
depuis la page *Account*, et renseigner `OPENSKY_CLIENT_ID` /
`OPENSKY_CLIENT_SECRET` dans `.env`.

> Depuis mars 2025, OpenSky n'accepte plus que le flow OAuth2
> `client_credentials` - l'authentification par login/mot de passe est
> supprimée. Le client gère le jeton et son renouvellement automatiquement.

### 3. Premier run complet

```bash
skytrace pipeline
```

Cette commande enchaîne : un snapshot de trafic -> le référentiel aéroports
(au premier lancement) -> `dbt build` (modèles + tests).

### 4. Voir le résultat

```bash
skytrace info
```

```bash
skytrace dashboard
```

Le tableau de bord se recharge périodiquement (réglable dans la barre
latérale) et affiche un **bandeau de fraîcheur** : vert si le dernier relevé
date de moins d'un cycle, rouge s'il remonte à plusieurs heures - auquel cas
le planning Dagster est à l'arrêt et la série temporelle ne se remplit plus.

Sur la carte, chaque appareil est une silhouette orientée selon son cap réel.
**Un clic ouvre sa fiche** : photographie de l'appareil lui-même (Planespotters,
appelée par adresse OACI 24 bits, donc la vraie machine et non une image
générique du type), compagnie, immatriculation, modèle, année, altitude,
vitesse et cap. Les exploitants d'État ou militaires sont signalés - par
heuristique sur le nom de l'exploitant, donc faillible dans les deux sens.

### 5. Lancer l'orchestrateur

```bash
skytrace dagster
```

L'interface s'ouvre sur `http://localhost:3000` : graphe de lignée complet
(du fichier Parquet jusqu'aux marts), historique des exécutions, contrôles
qualité, et les deux plannings prêts à être activés.

> **Passer par `skytrace dagster` plutôt que `dagster dev` directement.**
> Sans `DAGSTER_HOME`, Dagster crée un répertoire temporaire qu'il supprime
> en sortant : l'historique des matérialisations, l'état actif/inactif des
> plannings et les journaux de run sont perdus à chaque redémarrage - tous
> les assets réaffichent alors *Never materialized*. La commande positionne
> la variable vers `.dagster_home/` (et `PYTHONLEGACYWINDOWSSTDIO`, sans
> laquelle Dagster n'archive pas la sortie des étapes sous Windows).

Au premier lancement, les deux plannings sont **inactifs** : les activer
dans l'onglet *Automation* pour que la collecte tourne toute seule.

## Modèle de données

Modèle en étoile classique, avec deux tables de faits à des grains
différents et deux dimensions conformes.

| Table | Grain | Type |
|---|---|---|
| `fct_aircraft_positions` | 1 aéronef × 1 instant | Fait - **incrémental** |
| `fct_traffic_hourly` | 1 heure × 1 pays | Fait agrégé |
| `fct_airport_activity` | 1 aéroport × 1 heure | Fait agrégé |
| `dim_aircraft` | 1 appareil (adresse OACI 24 bits) | Dimension |
| `dim_airport` | 1 aérodrome | Dimension conforme |

**Points d'ingénierie notables :**

- `fct_aircraft_positions` est **incrémental** avec la stratégie
  `delete+insert` : chaque exécution ne traite que les snapshots nouveaux,
  et rejouer un snapshot remplace ses lignes au lieu de les dupliquer.
- La jointure avion <-> aéroport utilise un **blocking spatial** : une grille
  de 1° réduit le produit cartésien avant le calcul de haversine, qui ne
  s'applique qu'aux couples survivants et à leurs 8 cellules voisines.
- `distinct_aircraft` est documenté comme **mesure non additive** - sommer
  24 heures ne donne pas le total journalier.

## Question analytique : le trafic se lit-il dans le NO2 ?

Une deuxième source, **Open-Meteo Air Quality** (gratuite, sans clé), fournit
les concentrations horaires de polluants au sol aux coordonnées des grands
aéroports. Jointe au trafic dans le mart `fct_airport_hourly_air_quality`,
elle sert une vraie question chiffrée : *plus il y a d'avions autour d'un
aéroport à une heure donnée, plus le NO2 mesuré au sol est-il élevé ?*

La réponse, en contrôlant progressivement les facteurs de confusion :

| Niveau de contrôle | Corrélation avions ~ NO2 |
|---|---|
| Brute (toutes observations) | **+0.15** |
| Intra-aéroport (retrait de l'effet aéroport) | **-0.23** |
| Intra-aéroport et dé-saisonnalisée (retrait du cycle jour/nuit) | **-0.16** |

La corrélation positive naïve **s'inverse** une fois retiré l'effet « entre
aéroports » (les gros hubs sont dans des métropoles au NO2 de fond plus
élevé). **Conclusion : à l'échelle horaire, le trafic aérien n'est pas un
prédicteur détectable du NO2 au sol** - dominé par le trafic routier, le
chauffage et la météo. Le résultat est présenté comme une corrélation
descriptive, et l'hypothèse naïve est explicitement rejetée par les données.

### Ce que vaut ce chiffre

Un coefficient publié nu ne dit pas s'il est distinguable du hasard. Deux
précautions, imposées par la structure du panel (825 heures-aéroport, mais
seulement **14 aéroports**) :

- **L'intervalle de confiance est calculé par bootstrap sur les grappes** : on
  retire des aéroports avec remise, pas des heures. Deux heures consécutives
  au même aéroport partagent la météo, le trafic de fond et la circulation
  alentour ; les traiter comme des témoignages indépendants donnerait un
  intervalle faussement étroit.
- **Le test est une permutation à l'intérieur de chaque aéroport.** Cela
  conserve la structure - chaque aéroport garde ses heures et ses niveaux - et
  ne détruit que l'appariement heure par heure, c'est-à-dire exactement ce que
  l'hypothèse nulle affirme inexistant.

Résultat : **r = −0,149, IC 95 % [−0,200 ; −0,077], p ≈ 0,001**. L'intervalle
exclut zéro : l'inversion de signe n'est pas un accident d'échantillonnage.

Une nuance contre-intuitive, vérifiée en la mesurant plutôt qu'en la
supposant. On lit souvent que le bootstrap par grappes élargit toujours
l'intervalle. C'est faux : cela dépend de l'homogénéité de l'effet entre
groupes. Ici l'effet intra est négatif dans 10 aéroports sur 14, avec un
écart-type de 0,175 seulement, donc l'intervalle par grappes se révèle
**plus étroit** que le naïf. Sur un panel où les groupes divergeraient, il
serait bien plus large. Les deux cas sont couverts par un test.

**Limite assumée** : 14 grappes, c'est peu, et un intervalle calculé sur si
peu de groupes est lui-même incertain. C'est mieux qu'un chiffre nu, ce n'est
pas une étude épidémiologique.

Analyse reproductible (`python scripts/analyse_qualite_air.py`), rapport
détaillé et figure : [`docs/analyse_trafic_qualite_air.md`](docs/analyse_trafic_qualite_air.md).
Estimateurs et tests : [`src/skytrace/stats.py`](src/skytrace/stats.py).

## Enrichissement flotte : compagnies et constructeurs

Une troisième source croise deux référentiels gratuits pour donner du sens
aux appareils observés :

- **Base aéronefs OpenSky** (~500 000 appareils) : type réel, constructeur,
  modèle, opérateur et code d'exploitant par adresse OACI 24 bits.
- **OpenFlights** : le code OACI d'exploitant donne le nom de la compagnie.

### Rattacher un appareil à sa compagnie : deux clés valent mieux qu'une

La première version déduisait la compagnie du seul préfixe d'indicatif. C'est
la clé la plus couvrante et la plus bruyante : les codes se réutilisent, et
OpenFlights, figé depuis des années, contient des entrées périmées. Le
tableau de bord affichait ainsi **Tiphook PLC**, un loueur de conteneurs,
parmi les compagnies de Francfort.

Trois règles, vérifiées sur la donnée :

1. **Le code d'exploitant déclaré prime sur le préfixe d'indicatif.** Il
   couvre un tiers des appareils observés, mais il est déclaré et non deviné.
   Les appareils étiquetés « O Air » déclarent DLH : ce sont des Lufthansa.
2. **Un préfixe n'est retenu que s'il est attesté**, c'est-à-dire s'il sert de
   code d'exploitant à au moins un appareil de la base. Un code que personne
   ne déclare est une collision, pas une compagnie. Cette règle écarte un
   rattachement sur cinq : « 12 North », « Regional Air Iceland », « All
   Spain ».
3. **Une entrée OpenFlights périmée cède devant l'exploitant nommé** dans la
   base aéronefs. C'est ce qui transforme « Tiphook PLC » en AeroLogic.

La colonne `airline_source` dit laquelle des deux clés a tranché, pour qu'un
classement puisse distinguer un rattachement déclaré d'un rattachement déduit.
Le nom canonique reste celui d'OpenFlights partout ailleurs : les deux sources
concordent dans 90 % des cas, et les 10 % restants sont des variantes
d'écriture qui fragmenteraient les agrégats.

Cela débloque des analyses lisibles :

- **Part de marché des compagnies par aéroport** (`fct_airline_airport_activity`) :
  à Paris-CDG, Air France domine devant TNT, FedEx et Delta.
- **Airbus vs Boeing**, et une leçon de comptage. Rapportée aux *positions
  observées* des appareils rattachés à une compagnie, la part du duopole est
  de **80 %** (Boeing ~44 %, Airbus ~36 %). Comptée en *cellules*, toute
  aviation confondue, elle tombe à **30 %** - et même à 19 % si l'on garde au
  dénominateur les appareils dont la base ne connaît pas le constructeur.

  Les deux chiffres sont exacts et répondent à deux questions différentes.
  L'écart vient de l'aviation générale : 36 726 appareils sans compagnie sont
  vus 2,8 fois chacun, contre 27 195 avec compagnie vus 9,2 fois. Les Cessna
  et les Piper sont innombrables mais volent peu et court ; les avions de
  ligne sont moins nombreux et volent en permanence. Le tableau de bord
  affiche la première lecture, celle du trafic, et donne la seconde en
  légende.
- **Âge des flottes par compagnie** : sur les compagnies d'au moins 25
  appareils datés, l'écart va d'environ 7 ans à 33 ans, soit un facteur 4,5.
  Le fret et le régional exploitent des appareils convertis en fin de vie, le
  low-cost renouvelle pour la consommation de carburant. Biais assumé et
  affiché : l'année de construction n'est connue que pour la moitié de la base.

## Le rythme du monde, en heure solaire

La tendance horaire du tableau de bord est en UTC, ce qui mélange tous les
fuseaux : le matin japonais y tombe au même endroit que la nuit américaine, et
le cycle s'annule. En ramenant chaque position à l'heure **solaire** de sa
longitude (quinze degrés par heure), le rythme réapparaît : le trafic culmine
vers 10 h locale et tombe au plus bas vers 1 h, dans un rapport de près de
**12 pour 1**.

L'heure solaire ignore les fuseaux administratifs et l'heure d'été. Pour lire
un cycle jour / nuit c'est précisément ce qu'il faut, puisque le soleil ne
connaît pas les décrets.

## Deux signaux que la donnée portait sans les montrer

- **Codes de détresse.** Le transpondeur transmet un code à quatre chiffres,
  et l'OACI en réserve trois : 7500 détournement, 7600 panne radio, 7700
  urgence générale. Ils sont désormais traduits dans `emergency_kind` et
  listés dans le tableau de bord. À lire comme un signal et non comme un
  fait : un 7500 résulte presque toujours d'une erreur de sélection.
- **Fraîcheur de la position.** `position_age_seconds` mesure l'écart entre
  l'instant du relevé et la dernière position émise. La médiane est d'une
  seconde, mais la queue de distribution monte à plusieurs heures : OpenSky
  conserve le dernier point connu d'un appareil sorti de couverture. Au-delà
  de 300 secondes, soit au-delà du 99e centile mesuré, la position est
  marquée `is_position_stale` et **écartée de la carte** plutôt que dessinée
  comme du trafic courant. Le nombre de positions écartées est affiché.

Le tableau de bord expose les deux (sélecteur d'aéroport + camembert
constructeurs).

## Le tableau de bord mesure aussi le pipeline

Un onglet **Coulisses** rassemble ce qu'un tableau de bord montre rarement :
ce qu'il vaut lui-même.

- **Ponctualité réelle de la collecte.** Le cron est déclaré toutes les
  30 minutes ; l'écart médian observé est de 73 minutes, et 15 % seulement des
  intervalles tiennent dans les 45 minutes. GitHub exécute les tâches
  planifiées « au mieux » et dépriorise les dépôts publics peu actifs. Les
  seuils du bandeau de fraîcheur sont calibrés sur cette distribution mesurée,
  pas sur la cadence théorique.
- **Couverture du réseau, chiffrée.** 85 % des positions viennent de deux
  régions, l'Europe et l'Amérique du Nord. La carte mesure autant la densité
  des récepteurs bénévoles que celle du trafic : comparer des volumes entre
  régions n'a pas de sens, et les analyses restent valables à l'intérieur
  d'une région, pas entre elles.
- **Comment c'est construit**, et ce que le tableau de bord ne prétend pas
  être : ni du temps réel, ni des trajectoires, ni un recensement. Chaque
  limite est mesurée dans l'onglet plutôt qu'affirmée.

## Savoir que ça tourne encore

Deux garde-fous, nés de deux incidents réels.

**Une veille de fraîcheur** (`skytrace watchdog`, workflow `veille.yml`).
GitHub prévient quand un workflow échoue, mais pas quand il réussit sans rien
produire, ni quand il désactive lui-même les tâches planifiées d'un dépôt
resté soixante jours sans commit. La seule question qui couvre ces cas est
« quand le lac a-t-il été écrit pour la dernière fois ? ». Seuil à six
heures : les écarts réels entre deux collectes dépassent régulièrement trois
heures, et un seuil serré alerterait en permanence.

**Un test de migration** dans la CI. Les étapes existantes rejouaient
`dbt build` deux fois avec le *même* code : c'est de l'idempotence. Ce qui
casse en production, c'est un entrepôt bâti avec les *anciens* modèles puis du
dbt *neuf* par-dessus - une colonne ajoutée à un modèle incrémental n'est pas
rétro-remplie, un test `not_null` échoue sur tout l'historique, et l'aval est
ignoré sans que rien ne devienne rouge. La CI rejoue désormais cette
séquence : elle construit avec les modèles du commit précédent, puis avec
ceux d'aujourd'hui, et refuse le moindre modèle ignoré. Rejoué sur le commit
fautif, le garde-fou détecte bien les 40 modèles ignorés.

## Qualité des données

Le pipeline embarque **54 tests de données** exécutés à chaque `dbt build`
(9 modèles + 54 tests = 63 nœuds), qui bloquent la construction des modèles
en aval dès qu'un contrôle échoue.

| Type | Exemples |
|---|---|
| Intégrité | unicité de `dim_aircraft`, clés étrangères vers les dimensions |
| Plausibilité physique | latitude ∈ [-90, 90], altitude ∈ [-500 m, 20 000 m], vitesse ≤ 780 kt |
| Domaine | `flight_phase` ∈ {sol, montée, descente, croisière, inconnu} |
| Grain | aucun doublon (appareil, instant) dans la table de faits |
| Cohérence | `distinct_aircraft` ≤ `position_count` dans les agrégats |
| Fraîcheur | `dbt source freshness` alerte si l'ingestion s'est arrêtée |
| Amont | contrôle Dagster : un snapshot vide fait échouer l'exécution |

Le test générique `accepted_range` est implémenté dans le projet plutôt
qu'importé de `dbt_utils` : une dépendance de moins, et le comportement
reste lisible dans le dépôt.

**Pourquoi un snapshot vide est traité comme une erreur** : l'API répond
`200 OK` avec une liste vide. Sans contrôle explicite, le pipeline
continuerait à tourner en produisant du vide, et le problème ne serait
détecté que par un humain regardant un tableau de bord plat.

## Orchestration

Deux cadences, parce que les deux sources n'ont pas la même nature :

| Planning | Fréquence | Contenu |
|---|---|---|
| `traffic_every_15_minutes` | `*/15 * * * *` | Snapshot de trafic + toute la chaîne dbt |
| `reference_daily` | `0 4 * * *` | Référentiel aéroports + reconstruction en aval |

**Le budget de crédits est calculé, pas espéré.** La zone `france` fait
160 deg², soit 3 crédits par appel selon le barème OpenSky. À 96 exécutions
par jour, cela fait 288 crédits sur les 400 alloués en anonyme - la marge
absorbe les relances manuelles. Ce calcul est **verrouillé par un test
unitaire** (`test_france_stays_within_the_anonymous_budget`) : élargir la
zone sans fournir d'identifiants fait échouer la CI.

Un compteur de crédits persisté sur disque refuse tout appel qui dépasserait
le quota, y compris après un redémarrage du worker.

## Décisions techniques

Les arbitrages sont documentés sous forme d'ADR (Architecture Decision
Records) dans [`docs/adr/`](docs/adr/) :

| ADR | Décision |
|---|---|
| [0001](docs/adr/0001-orchestrateur.md) | Dagster plutôt qu'Airflow |
| [0002](docs/adr/0002-entrepot.md) | DuckDB plutôt qu'un entrepôt cloud |
| [0003](docs/adr/0003-modele-dimensionnel.md) | Modèle en étoile, grain et additivité |
| [0004](docs/adr/0004-jointure-spatiale.md) | Blocking par grille plutôt qu'extension spatiale |

Voir aussi [`docs/architecture.md`](docs/architecture.md) pour le détail des
flux, du partitionnement et de la stratégie incrémentale.

## Déploiement en ligne (gratuit)

Le projet tourne en local, mais un pipeline ne se juge qu'en fonctionnement.
Il se déploie donc gratuitement, **sans serveur à administrer** :

- **GitHub Actions** joue le rôle de l'ordonnanceur en production : le
  workflow [`collect.yml`](.github/workflows/collect.yml) collecte un
  snapshot toutes les 30 min, le transforme, et publie les fichiers Parquet
  dans le dépôt. Gratuit et illimité sur un dépôt public, aucun secret requis
  (mode anonyme OpenSky).
- **Streamlit Community Cloud** héberge le tableau de bord. Au démarrage, il
  reconstruit les marts à partir du lac Parquet versionné, puis se redéploie
  à chaque nouvelle collecte.

Dagster n'est pas déployé : il reste l'ordonnanceur de développement local.
GitHub Actions est son équivalent cloud gratuit. Distinguer l'outil de
développement du mécanisme de déploiement fait partie du métier.

**Marche à suivre complète : [`docs/deploiement.md`](docs/deploiement.md).**

## Structure du dépôt

```
src/skytrace/         Bibliothèque : config, client API, ingestion, entrepôt
    config.py           Configuration 12-factor, barème de crédits
    opensky/            OAuth2, client résilient, schéma Arrow
    ingestion/          Écriture Parquet partitionné (couche bronze)
    warehouse/          Accès DuckDB
dbt/skytrace/         Transformations SQL, tests, macros
    models/staging/       Nettoyage et typage
    models/intermediate/  Dédoublonnage, jointure spatiale
    models/marts/         Modèle en étoile
    tests/                Tests singuliers + test générique maison
orchestration/        Assets, contrôles, jobs et plannings Dagster
dashboard/            Application Streamlit (auto-reconstruction cloud)
scripts/              Génération de données synthétiques (démo, CI)
tests/                Tests unitaires Python (réseau simulé)
docs/                 Architecture, ADR et guide de déploiement
requirements.txt      Dépendances lues par Streamlit Community Cloud
.github/workflows/    ci.yml (tests) + collect.yml (collecte planifiée)
```

## Limites connues

Elles sont assumées et documentées plutôt que masquées :

- **La zone est un rectangle**, pas une frontière. La fenêtre « France »
  attrape Zurich, Barcelone et Gatwick. C'est le comportement de l'API
  OpenSky, qui ne prend qu'une boîte englobante.
- **Atterrissage / décollage sont inférés**, pas déclarés. ADS-B ne diffuse
  pas de plan de vol : les colonnes `climbing_aircraft` et
  `descending_aircraft` reposent sur le taux de montée observé à proximité
  d'un aéroport. Fiable en tendance, pas comme comptage officiel de
  mouvements.
- **La couverture ADS-B est très inégale, c'est la limite principale.** Le
  réseau OpenSky repose sur des récepteurs hébergés par des bénévoles : là où
  personne n'en installe, aucun avion n'est vu, même s'il en passe. Mesuré sur
  un relevé mondial de 8 194 aéronefs :

  | Europe | Amérique du Nord | Asie (S/E) | Afrique | Moyen-Orient | Russie |
  |---|---|---|---|---|---|
  | 57 % | 19 % | 13 % | 4 % | **0,6 %** | **0,2 %** |

  Le Moyen-Orient à 0,6 % alors que Dubaï et Doha comptent parmi les premiers
  hubs mondiaux montre bien qu'il s'agit d'un biais d'observation, pas d'une
  réalité du trafic. **Ces données mesurent le trafic observable par le
  réseau, pas le trafic réel** : toute comparaison entre régions est donc à
  proscrire, et les analyses de ce projet restent intra-région ou
  intra-aéroport.
- **En mode anonyme, pas d'historique.** Seul l'instant présent est
  accessible : la profondeur d'historique se construit en laissant tourner
  l'ordonnanceur.

## Sources et licences

- [OpenSky Network](https://opensky-network.org) - données ADS-B, usage
  non commercial et recherche
  ([conditions](https://opensky-network.org/about/terms-of-use))
- [OurAirports](https://ourairports.com/data/) - référentiel aéroports,
  domaine public
- [Open-Meteo](https://open-meteo.com/) - qualité de l'air, usage non
  commercial gratuit
- [OpenSky aircraft database](https://opensky-network.org/aircraft-database) -
  métadonnées aéronefs, usage non commercial et recherche
- [OpenFlights](https://openflights.org/data.html) - référentiel compagnies,
  Open Database License
- [Planespotters](https://www.planespotters.net/photos/api) - photographies
  d'aéronefs, usage non commercial ; chaque cliché est crédité à son auteur
  dans l'interface

Code sous licence MIT.
