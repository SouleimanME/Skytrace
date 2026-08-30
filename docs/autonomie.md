# Ce qui fait tourner SkyTrace sans intervention

Ce document repond a une seule question : **que faut-il faire pour que le
projet continue de tourner ?** La reponse visee est « rien », et ce qui suit
dit ou elle tient et ou elle ne tient pas.

## Le principe

Une panne qui se repare en une commande ne doit pas produire un courriel.
Envoyer une alerte pour un incident que le systeme sait resoudre, c'est
transformer de l'automatisation en rappel de corvee - et **une alerte qui ne
demande aucune action apprend a ignorer les suivantes**.

Chaque mecanisme ci-dessous applique la meme regle : reparer d'abord, ne
signaler que ce qui resiste.

## Les quatre couches

### 1. Reessais dans la collecte

`collect.yml` appelle deux services par le reseau, OpenSky et R2. Un delai
depasse, un 502 passager ou une coupure de quelques secondes faisaient
echouer la collecte et partir un courriel, pour une panne qui n'existait plus
a la minute suivante.

**Trois tentatives espacees d'une minute.** Le pipeline est idempotent - la
table de faits est incrementale en `delete+insert` sur la cle
(appareil, instant) - donc rejouer ne duplique rien. Le workflow ne rougit que
si les trois echouent.

### 2. La veille repare au lieu de prevenir

`veille.yml` constatait l'arret de la collecte et passait au rouge. Il fallait
ensuite aller cliquer.

Elle **collecte elle-meme** desormais. La panne la plus frequente - le cron de
collecte n'a pas ete declenche - se resout sans que personne ne soit prevenu.
Le workflow ne passe au rouge que si la reparation echoue aussi : a ce
moment-la, le courriel est merite.

Deux passages par jour, seuil a dix heures.

> La reparation force `SKYTRACE_REGION: world`. Sans cette variable, la valeur
> par defaut (`france`) s'appliquerait et deposerait un releve dix fois plus
> etroit, indiscernable des autres en aval. L'erreur a deja ete commise a la
> main ; elle ne devait pas etre automatisee.

### 3. Le maintien en eveil ne crie plus pour un clic manque

Piloter un navigateur sans fil est capricieux. Un clic qui rate sa cible ou
une page lente faisaient echouer le workflow alors que l'application
repondait parfaitement.

**Trois tentatives, et le verdict porte sur l'application, pas sur le
navigateur.** Si le service repond a la fin, le workflow reussit meme si le
pilotage a echoue. Faire autrement revenait a mesurer la fiabilite de
Playwright au lieu de celle du service.

### 4. Anti-inactivite : la seule extinction certaine

**C'est le mecanisme le plus important des quatre.**

GitHub desactive automatiquement les taches planifiees d'un depot public reste
**soixante jours sans activite**. Ce n'est ni un incident ni un alea : c'est
une regle documentee qui finira par s'appliquer. Un projet termine n'est plus
modifie, donc il s'eteint tout seul - exactement quand plus personne ne le
surveille.

Les trois couches precedentes traitent des pannes passageres. Celle-ci traite
la seule extinction **certaine**.

`anti-inactivite.yml` ecrit la date dans `.github/derniere-activite.txt` et
pousse le commit, le 1er et le 15 de chaque mois. Quinze jours au plus entre
deux passages, soit un quart du delai : large marge si un passage saute.

Cout : vingt-six commits par an, tous marques `chore:` et `[skip ci]`.

## Ce qui peut encore vous ecrire

| Situation | Courriel ? | Pourquoi |
|---|---|---|
| Collecte ratee une fois | non | reessayee |
| Cron de collecte non declenche | non | la veille collecte a sa place |
| Navigateur capricieux | non | reessaye, verdict sur le service |
| Application endormie | non | reveillee par le keepalive |
| Trois collectes de suite en echec | **oui** | quelque chose resiste |
| Reparation impossible | **oui** | le systeme ne sait pas resoudre |
| Test dur de qualite en echec | **oui** | la donnee est fausse, il faut regarder |

Les trois derniers cas sont exactement ceux qui meritent votre attention. Si
vous ne voulez plus **aucun** courriel, la coupure se fait cote GitHub et non
dans ce depot : *Settings > Notifications > Actions*, decocher les
notifications d'echec. La surveillance externe prend alors le relais, si elle
est configuree.

## Ce qui reste hors de portee du depot

**Un blocage de l'ordonnanceur GitHub.** Du 26 au 28 aout 2026, les taches
planifiees du compte ont cesse d'etre declenchees pendant deux jours, puis ont
repris seules. Aucune configuration interne ne protege de cela : quand
l'ordonnanceur se tait, tout ce qui vit dedans se tait avec lui, y compris la
veille reparatrice.

La seule parade est exterieure : `scripts/declencher_collecte.py`, appele par
un cron tiers. Voir [surveillance-externe.md](surveillance-externe.md). Ce
n'est plus urgent depuis que l'ordonnanceur a repris, mais c'est la derniere
dependance non couverte.

**Les identifiants.** Les cles R2 et le compte OpenSky n'expirent pas d'
eux-memes, mais rien ici ne le detecterait avant que la collecte echoue trois
fois - auquel cas le courriel part, et il est merite.
