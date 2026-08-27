# Deploiement public (gratuit)

Ce document explique comment ce projet, concu pour tourner en local, est
rendu visible en ligne **sans aucun cout et sans serveur a administrer**.

L'idee tient en une phrase : **GitHub Actions joue le role de
l'ordonnanceur, Streamlit Community Cloud celui de la vitrine.**

```
1. GitHub Actions (collect.yml), une fois par heure :
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
une heure, sans intervention.

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

## Le stockage : de git vers R2

Historiquement, chaque collecte committait ses fichiers Parquet dans le
depot. A l'echelle mondiale et en haute frequence, git n'est plus adapte.
Le lac vit desormais sur **Cloudflare R2** (voir [`r2.md`](r2.md)) : le depot
ne porte plus que le code, et le volume n'est borne que par la retention
(`SKYTRACE_RETENTION_DAYS`, 180 jours par defaut).

## La limite a connaitre : le cron GitHub n'est pas ponctuel

Le workflow est declare une fois par heure, mais GitHub execute les crons
**"au mieux"**, sans garantie de ponctualite - d'autant plus sur un depot
public peu actif. Mesure faite sur ce projet, les ecarts reels entre deux
collectes consecutives :

| Ecart observe | 52 min | 57 min | 98 min | 105 min | 125 min | 226 min |
|---|---|---|---|---|---|---|

Deux consequences assumees dans le code :

1. **Le cron est decale des minutes rondes** (`17 * * * *` plutot que
   `0 * * * *`). La contention est maximale a :00, ou tout le monde
   planifie ; se decaler reduit sensiblement l'attente.
2. **La cadence demandee est celle que l'ordonnanceur peut tenir.** Elle a
   ete ramenee de 30 a 60 minutes : sur 48 executions quotidiennes demandees,
   GitHub n'en honorait qu'une quinzaine. Reclamer une cadence qui n'est pas
   tenue ne collecte rien de plus, cela rend seulement le decalage illisible.
   Une heure est de surcroit la resolution de l'analyse trafic / NO2.
2. **Les seuils de fraicheur du tableau de bord sont calibres sur le
   comportement observe**, pas sur la cadence theorique : vert jusqu'a
   75 min, orange jusqu'a 4 h, rouge au-dela. Alerter des 35 min afficherait
   une alerte en permanence - c'est-a-dire n'alerterait plus sur rien.

Pour une ponctualite garantie, il faudrait un ordonnanceur dedie (Dagster
sur une machine, ou un service cron payant). Le compromis retenu ici est
assume : gratuit, sans serveur, et la latence reelle est rendue visible
plutot que masquee.

## L'autre limite : l'application se met en veille

Streamlit Community Cloud arrete le conteneur d'une application restee sans
visiteur. Le lien affiche alors une page "Zzzz" avec un bouton de reveil.
Pour un lien de portfolio c'est genant : le premier visiteur tombe sur une
page qui a l'air cassee.

**La collecte, elle, n'est pas affectee.** GitHub Actions et R2 sont
independants de Streamlit : pendant que la page dort, le pipeline continue
d'ecrire dans le lac. C'est l'affichage qui s'eteint, pas la donnee.

Deux details de la plateforme, decouverts en la sondant, qu'il faut connaitre
pour diagnostiquer l'etat de l'application.

**L'application n'est pas servie a la racine.** L'adresse publique rend une
coquille Streamlit Cloud qui embarque l'application dans une iframe, sous le
prefixe `/~/+/`. Sonder la racine ne dit donc rien de l'etat reel :

| Chemin | Application en marche | Application endormie |
|---|---|---|
| `/_stcore/health` | `200` + du HTML | `200` + du HTML |
| `/~/+/_stcore/health` | `200` + le texte `ok` | pas de `ok` |

Le bon test est donc le second, et il porte sur le CORPS de la reponse, pas
sur son statut - qui vaut 200 dans tous les cas.

**La page de veille est rendue en JavaScript.** Son bouton de reveil n'existe
pas au chargement du document : le chercher immediatement revient a conclure
qu'il n'y en a pas.

Cela explique aussi pourquoi **un `curl` periodique ne suffit pas** : la
requete sur la racine est servie par le proxy sans jamais toucher au
conteneur, donc elle ne remet pas le compteur d'inactivite a zero. Seule une
session websocket - donc un vrai navigateur - compte comme une visite.

Le workflow `keepalive.yml` fait cette visite quatre fois par jour avec
Playwright (`scripts/keepalive.py`), et clique le bouton de reveil si
l'application dort deja. Il est separe du workflow de collecte a dessein :
son echec est cosmetique et ne doit jamais faire passer au rouge celui qui
porte les donnees.

Pour l'activer, definir la variable de depot `SKYTRACE_APP_URL` (Settings ->
Secrets and variables -> Actions -> onglet Variables) avec l'adresse publique
du tableau de bord. Sans elle le workflow ne fait rien, pour qu'un depot
forke n'aille pas visiter l'application de quelqu'un d'autre.

A savoir : l'offre gratuite est dimensionnee pour des applications peu
visitees, et la maintenir eveillee par une visite programmee va contre cet
esprit meme si rien ne l'interdit explicitement. L'intervalle est donc large
- quatre visites par jour, la ou une par minute n'apporterait rien. Une
alternative sans ambiguite existe : un hebergeur qui ne met pas en veille.


## La veille : savoir que la collecte s'est arretee

GitHub previent par courriel quand un workflow ECHOUE. Il ne dit rien de
trois pannes silencieuses, pourtant les plus probables ici :

- la collecte reussit mais n'ecrit rien (source muette, quota epuise) ;
- GitHub desactive les taches planifiees d'un depot public reste soixante
  jours sans commit, et la collecte s'arrete sans un seul echec ;
- les identifiants R2 expirent.

Dans ces trois cas tout reste vert et la donnee cesse d'arriver. La seule
question qui les couvre est : quand le LAC a-t-il ete ecrit pour la derniere
fois ? On interroge donc le lac et non l'entrepot, qu'une reconstruction
fraiche a partir d'un lac fige rendrait faussement rassurant.

```bash
skytrace watchdog --max-age-hours 10
```

La commande sort en erreur si le dernier releve depasse le seuil, ce qui fait
passer le workflow `veille.yml` au rouge et declenche le courriel de GitHub.
Pas de service de plus, pas de compte de plus.

Le seuil de dix heures est large a dessein : les ecarts mesures entre deux
collectes vont de la minute a plus de trois heures, et la cadence est passee
a une collecte par heure. Un seuil serre alerterait en permanence,
c'est-a-dire n'alerterait plus.

### L'angle mort de cette veille

Elle ne peut pas detecter que GitHub a cesse d'executer les crons. C'est un
cron GitHub qui surveille des crons GitHub : il partage le sort de ce qu'il
observe. C'est arrive le 26 aout 2026, la panne a dure 32 heures, et rien
n'a alerte.

Le complement vit donc en dehors : voir
[`surveillance-externe.md`](surveillance-externe.md).

**Limite assumee.** Si GitHub desactive les taches planifiees du depot, il
desactive aussi celle-ci : la veille s'eteint avec ce qu'elle surveille. Elle
couvre les pannes de la collecte, pas la disparition de l'ordonnanceur. Un
commit de temps en temps garde les deux en vie.
