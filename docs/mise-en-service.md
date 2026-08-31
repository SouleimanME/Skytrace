# Les trois actions qui ne peuvent venir que de vous

Tout le reste du projet tourne seul. Ce document liste ce qui reste, pourquoi
aucun code ne peut le faire a votre place, et ce qui s'active tout seul une
fois fait.

Chacune est **facultative** : sans elle, le pipeline continue de fonctionner,
en moins bien sur un point precis. Aucune n'est un prealable.

---

## 1. Identifiants OpenSky : dix fois plus de credits

**Ce que cela change, et ce que cela ne change PAS.**

| | Anonyme | Avec un compte |
|---|---|---|
| Budget quotidien | 400 credits | **4 000** |
| Historique | refuse | **1 heure seulement** |
| Rattrapage des trous de plusieurs heures | impossible | **toujours impossible** |

**Une correction, et elle est importante.** Ce document annoncait d'abord que
le compte rendrait l'historique accessible et permettrait de combler les 102
heures perdues. **C'etait faux.** Mesure faite apres coup, avec un compte
reel :

```
t-5 min   200        t-55 min  200
t-65 min  403        t-2 h     403        t-24 h  403
"Historical data more than 1 hour ago can only be retrieved with /states/own"
```

Au-dela d'une heure, OpenSky ne sert que `/states/own`, reserve a ceux qui
**alimentent le reseau avec leur propre recepteur ADS-B**. Un simple compte
ne suffit pas.

**Consequence a assumer : les trous sont definitifs.** Les 102 heures perdues
le restent, et celles a venir aussi. Aucun reglage n'y change quoi que ce
soit - c'est une limite de la source. La seule parade est de ne pas creer de
trou, ce qui renvoie a l'action 2.

**Ce que le compte apporte reellement.** Un budget de 4 000 credits au lieu
de 400. La consommation actuelle est de 96 par jour, soit une marge de 40x
au lieu de 4x. Cela n'ouvre rien aujourd'hui, mais cela retire un plafond qui
se serait fait sentir en augmentant la cadence ou la zone.

**A faire.** Creer un client API sur
[opensky-network.org](https://opensky-network.org) (*Account > API Client*),
puis deposer les deux valeurs dans *Settings > Secrets and variables >
Actions* :

| Nom | Valeur |
|---|---|
| `OPENSKY_CLIENT_ID` | l'identifiant du client |
| `OPENSKY_CLIENT_SECRET` | son secret |

Verification, sans jamais afficher les valeurs :

```bash
.venv\Scripts\skytrace.exe info
```

Le budget doit passer de 400 a 4 000.

**Le rattrapage reste actif**, mais pour ce qu'il peut vraiment : une collecte
manquee de moins d'une heure, rejouee a l'instant manque. C'est modeste. La
commande le dit elle-meme plutot que de laisser croire le contraire :

```bash
.venv\Scripts\skytrace.exe backfill
```

---

## 2. Declencheur externe : survivre a une panne de l'ordonnanceur GitHub

**Ce que cela change.** Du 26 au 28 aout 2026, GitHub a cesse d'executer les
taches planifiees du compte pendant plus de quarante heures, puis a repris
seul. Pendant ce temps, **tout ce qui vit dans GitHub Actions se tait** - y
compris la veille reparatrice, qui est elle aussi un cron.

Un cron exterieur declenche la collecte par l'API, la meme voie que le bouton
*Run workflow*, celle qui a continue de marcher pendant toute la panne.

**Pourquoi je ne peux pas le faire.** Cela exige un jeton GitHub emis depuis
votre compte, et son depot chez un tiers. Je ne manipule pas vos jetons.

**A faire.** Le detail complet est dans
[surveillance-externe.md](surveillance-externe.md). En bref : un jeton a
portee fine (depot `Skytrace` seul, permission *Actions: Read and write*),
teste par

```bash
python scripts/declencher_collecte.py SouleimanME/Skytrace
```

puis une tache horaire sur [cron-job.org](https://cron-job.org) qui appelle

```
POST https://api.github.com/repos/SouleimanME/Skytrace/actions/workflows/collect.yml/dispatches
```

**Ce qui s'active seul.** Rien a changer dans le depot : `collect.yml` accepte
deja `workflow_dispatch`, et son groupe de concurrence empeche une collecte
declenchee de croiser une collecte planifiee.

---

## 3. Battement de coeur : etre prevenu si tout s'arrete

**Ce que cela change.** Les surveillances internes se taisent avec ce
qu'elles observent. Un battement inverse le sens : le pipeline **annonce**
qu'il a tourne, et le service tiers alerte quand l'annonce n'arrive pas. Une
absence ne peut pas etre falsifiee par un composant a l'arret.

**A faire.** Une verification gratuite sur
[Healthchecks.io](https://healthchecks.io), periode 1 heure, delai de grace
3 heures, et son adresse de ping dans le secret `SKYTRACE_HEARTBEAT_URL`.

**Ce qui s'active seul.** `collect.yml` et `veille.yml` envoient deja le
battement en derniere etape, et sautent l'etape quand le secret est absent.

---

## Ce que cela donne, action par action

| Fait | Effet |
|---|---|
| Rien | Le projet tourne. Une panne de l'ordonnanceur passe inapercue jusqu'au prochain regard. |
| 1 seul | Budget x10. Les trous restent des trous : la source ne les rend pas. |
| 1 + 2 | La collecte ne depend plus de l'ordonnanceur GitHub. |
| 1 + 2 + 3 | Vous etes prevenu si l'ensemble s'arrete malgre tout. |

Aucune de ces trois n'est requise pour que la donnee continue d'arriver. Elles
enlevent, chacune, une facon dont le projet peut se degrader sans le dire.

**La continuite ne se repare pas, elle se preserve.** Les positions ADS-B
manquees ne se rattrapent pas au-dela d'une heure : la seule facon d'avoir
une serie continue est de ne pas l'interrompre. C'est ce qui rend l'action 2
plus importante que l'action 1, contrairement a ce que ce document affirmait
d'abord.
