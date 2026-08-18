# Deploiement public (gratuit)

Ce document explique comment ce projet, concu pour tourner en local, est
rendu visible en ligne **sans aucun cout et sans serveur a administrer**.

L'idee tient en une phrase : **GitHub Actions joue le role de
l'ordonnanceur, Streamlit Community Cloud celui de la vitrine.**

```
1. GitHub Actions (collect.yml), toutes les 30 min :
   collecte OpenSky + dbt build (qualite)
        |
        |  git commit + push des fichiers Parquet
        v
2. Depot GitHub : data/raw/**/*.parquet s'accumule
        |
        |  chaque push redeploie l'application
        v
3. Streamlit Cloud : reconstruit les marts (dbt), sert le tableau de bord
```

Pourquoi ce decoupage plutot qu'un vrai serveur :

- **Cout nul.** Les minutes GitHub Actions sont illimitees sur un depot
  public ; Streamlit Community Cloud est gratuit.
- **Rien a administrer.** Pas de VPS, pas de conteneur en production, pas de
  certificat a renouveler.
- **L'historique git prouve que ca tourne.** Chaque snapshot est un commit
  horodate : un recruteur voit une collecte qui dure depuis des semaines.

Dagster n'est PAS deploye : il reste l'ordonnanceur de developpement local,
documente et illustre par des captures. GitHub Actions est son equivalent
pour la production gratuite. Savoir distinguer l'outil de developpement du
mecanisme de deploiement est exactement ce qu'on attend d'un data engineer.

## Etape 1 - Publier le code sur GitHub

Le depot est deja initialise localement avec un premier commit. Il reste a
le connecter a un depot distant **public** :

1. Creer un depot vide sur https://github.com/new (par ex. `skytrace`),
   **sans** README ni .gitignore (ils existent deja).
2. Le connecter et pousser :

   ```bash
   git remote add origin https://github.com/<ton-compte>/skytrace.git
   git push -u origin main
   ```

## Etape 2 - Lancer la premiere collecte

Le depot ne contient volontairement aucune donnee au depart. Pour amorcer :

1. Onglet **Actions** du depot GitHub.
2. Workflow **Collecte planifiee** -> **Run workflow**.

Cette execution collecte un premier snapshot, telecharge le referentiel
aeroports, et committe le tout. Ensuite le cron prend le relais toutes les
30 minutes, sans intervention.

> Aucun secret n'est requis : la collecte tourne en mode anonyme OpenSky
> (400 credits/jour, largement suffisant). Pour un quota superieur, ajouter
> `OPENSKY_CLIENT_ID` et `OPENSKY_CLIENT_SECRET` dans
> *Settings -> Secrets and variables -> Actions* du depot, puis les exposer
> comme variables d'environnement dans `collect.yml`.

## Etape 3 - Publier le tableau de bord

1. Aller sur https://share.streamlit.io et se connecter avec GitHub.
2. **New app** -> choisir le depot, la branche `main`, le fichier
   `dashboard/app.py`.
3. Deployer.

Au premier demarrage, l'application installe les dependances
(`requirements.txt`), **reconstruit les marts** a partir du lac Parquet
versionne (via dbt, une poignee de secondes), puis affiche le tableau de
bord. A chaque nouvelle collecte, un push redeploie l'application, qui
reconstruit et reflete la donnee fraiche.

## Le point de vigilance a connaitre (et a savoir expliquer)

Chaque collecte committe des fichiers Parquet dans le depot. A ~2,5 Mo par
jour, l'historique git grossit lentement. Pour une demonstration de quelques
semaines, c'est negligeable. Pour un fonctionnement indefini, la bonne
reponse est de sortir les donnees du depot vers un stockage objet
compatible S3 (Cloudflare R2, 10 Go gratuits ; Backblaze B2) : DuckDB lit
nativement `read_parquet('s3://...')`, et le depot ne porterait plus que le
code. C'est l'evolution logique du projet, et un bon sujet a evoquer en
entretien pour montrer qu'on connait les limites de sa propre solution.
