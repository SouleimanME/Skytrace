# Surveillance externe

## Pourquoi elle existe

Toute la surveillance du projet vivait dans GitHub Actions : la collecte, le
maintien en eveil, et la veille censee detecter l'arret de la collecte.

Le 26 aout 2026 a 16 h 38 UTC, GitHub a cesse d'executer les taches
planifiees du compte. Les trois se sont arretees en meme temps, et un dépôt
sans rapport (`hanabi`) s'est arrete vingt minutes plus tard - donc au niveau
du compte, pas du projet. Aucune n'avait echoue : elles ont simplement cesse
d'etre declenchees.

**La veille etait aveugle a cette panne.** Elle a ete ecrite pour detecter un
arret silencieux de la collecte, et c'est exactement ce qui s'est produit.
Mais c'est un cron GitHub qui surveille des crons GitHub : quand GitHub
s'arrete de les executer, le surveillant s'arrete avec le surveille. Il
partage le sort de ce qu'il observe, ce qui annule sa raison d'etre dans le
seul cas ou il aurait servi.

La panne a dure 32 heures. Elle a ete decouverte par un test manuel en
navigation privee.

Un moniteur externe ne partage pas ce sort. Il vit chez un tiers, interroge
l'application depuis l'exterieur, et alerte que la cause soit Streamlit,
GitHub ou le reseau.

## Ce qu'il couvre, et ce qu'il ne couvre pas

| Panne | Detectee ? |
|---|---|
| Application endormie par inactivite | oui |
| Application plantee au demarrage | oui |
| Streamlit Cloud indisponible | oui |
| GitHub cesse d'executer les crons | oui, **indirectement** : sans keepalive l'application finit par dormir |
| Collecte a l'arret, application debout | **non** : voir plus bas |

**Il ne reveille pas l'application.** Une requete HTTP est servie par le
proxy de Streamlit Cloud sans jamais toucher au conteneur : elle ne remet pas
le compteur d'inactivite a zero. Seule une vraie session navigateur compte
comme une visite, et c'est ce que fait le workflow `keepalive.yml`. Le
moniteur previent, il ne soigne pas. Les deux sont complementaires.

**Il ne voit pas la fraicheur des donnees.** Une application debout qui sert
des donnees vieilles de trois jours lui parait saine. C'est l'objet de la
seconde moitie de ce document.

## L'adresse a surveiller, et pourquoi celle-la

Mesure prise sur le deploiement public, dans les deux etats :

| Adresse | Endormie | Reveillee | |
|---|---|---|---|
| `/` | 303 | 303 | inutilisable |
| `/_stcore/health` | 303 | 303 | inutilisable |
| `/~/+/_stcore/health` | **400** | **200 `ok`** | **discriminante** |

L'application n'est pas servie a la racine : l'adresse publique rend une
coquille Streamlit Cloud qui l'embarque sous le prefixe `/~/+/`. La racine
redirige vers l'authentification **dans les deux etats**, avec le meme code
303 : elle ne distingue donc rien, et un moniteur pointe dessus ne peut pas
dire si les visiteurs voient le tableau de bord ou la page de veille.

Consequence directe sur la configuration : **le moniteur ne doit pas suivre
les redirections.** Mesure faite en les suivant depuis la racine : la reponse
part en boucle entre l'application et `/-/login`, cinquante sauts avant que
le client abandonne. Un moniteur ainsi regle n'alerte donc pas sur le bon
signal - il produit une erreur de transport permanente, quel que soit l'etat
reel de l'application, et devient un detecteur qu'on finit par ignorer.

Le piege est verrouille par un test.

## Verifier la cible avant de configurer quoi que ce soit

```bash
python scripts/verifier_sante.py https://skytrace-data.streamlit.app
```

Le script sonde l'adresse, rend le diagnostic, et imprime les valeurs exactes
a reporter. Il sort en 0 si l'application repond, en 1 sinon - utilisable tel
quel dans un cron local ou une tache planifiee.

## Mise en place

Le compte est a creer par vous : ce depot ne contient aucun identifiant, et
n'en demande aucun.

Deux prestataires gratuits conviennent. **UptimeRobot** (50 moniteurs, 5 min
d'intervalle) ou **cron-job.org** (gratuit, intervalle a la minute, alerte
par courriel sur echec).

Reglages, identiques chez l'un et l'autre :

| Champ | Valeur |
|---|---|
| Type | HTTP(s), verification du code de reponse |
| Adresse | `https://skytrace-data.streamlit.app/~/+/_stcore/health` |
| Intervalle | 5 minutes |
| Alerte si | le code de reponse n'est pas `200` |
| Suivre les redirections | **non** |
| Notification | courriel |

Une fois le moniteur actif, la verification se fait toute seule : laisser
l'application s'endormir une fois, et confirmer que l'alerte arrive.

## Ce qui reste dans GitHub Actions

| Workflow | Role | Cadence |
|---|---|---|
| `collect.yml` | collecte un releve | une fois par heure |
| `keepalive.yml` | ouvre une vraie session, reveille si besoin | 3 fois par jour |
| `veille.yml` | echoue si le lac n'a pas ete ecrit depuis 10 h | 2 fois par jour |

Ces trois-la restent utiles quand GitHub fonctionne. Le moniteur externe est
la couche qui tient quand il ne fonctionne plus.


---

# Surveiller la FRAICHEUR, et non plus seulement la disponibilite

## Une idee qui ne marche pas, et pourquoi

L'approche naturelle est de publier un fichier d'etat sur un stockage public
et de faire chercher au moniteur le mot `OK` dedans.

**Elle ne detecte pas la panne qu'on veut detecter.** Un fichier statique est
ecrit par la collecte. Si la collecte s'arrete, plus personne ne le reecrit :
il se fige sur son dernier contenu, donc sur `OK`. Le moniteur continue de
lire `OK` indefiniment et ne signale rien. Il faudrait comparer l'horodatage
du fichier a l'heure courante, ce qu'un moniteur par mot-cle ne sait pas
faire : evaluer la fraicheur demande un calcul au moment de la LECTURE, et un
objet statique ne calcule rien.

Le fichier d'etat est publie quand meme, parce qu'il a une valeur propre
detaillee plus bas, mais il porte lui-meme cet avertissement.

## Ce qui marche : le battement de coeur

On inverse le sens. Au lieu que le moniteur interroge le pipeline, **c'est le
pipeline qui annonce qu'il a tourne**, et le service tiers alerte quand
l'annonce n'arrive pas dans le delai imparti.

L'absence d'un signal ne peut pas etre falsifiee par un composant a l'arret.
C'est toute la difference : une surveillance qui interroge se tait avec ce
qu'elle observe, une surveillance qui attend un signal crie quand il manque.

C'est exactement la panne du 26 aout : GitHub cesse d'executer les crons, le
battement n'arrive plus, l'alerte part. Aucune des trois surveillances
internes ne pouvait le faire.

### Mise en place

1. Creer un compte sur **[Healthchecks.io](https://healthchecks.io)** (offre
   gratuite : 20 verifications, alerte par courriel). **UptimeRobot** propose
   l'equivalent sous le nom *Heartbeat monitor*.
2. Creer une verification. Reglages :

   | Champ | Valeur |
   |---|---|
   | Periode | 1 heure (la cadence de collecte) |
   | Delai de grace | 3 heures |
   | Notification | courriel |

   Le delai de grace est large a dessein : GitHub execute les crons « au
   mieux » et les ecarts mesures depassent regulierement trois heures. Un
   delai serre alerterait en permanence, c'est-a-dire n'alerterait plus.

3. Copier l'adresse de ping fournie, et l'enregistrer dans le depot sous
   *Settings > Secrets and variables > Actions > New repository secret* :

   | Nom | Valeur |
   |---|---|
   | `SKYTRACE_HEARTBEAT_URL` | l'adresse de ping |

Le workflow `collect.yml` envoie le battement en derniere etape, **et
seulement si tout ce qui precede a reussi**. Un battement envoye apres un
echec dirait « tout va bien » alors que rien n'a ete collecte.

Sans ce secret, l'etape est simplement sautee : le depot reste clonable et
utilisable sans compte chez un tiers.

## Le document de sante publie sur le lac

```bash
skytrace publier-sante
```

Ecrit `sante.json` a la racine du bucket, en JSON, apres chaque collecte :

```json
{
  "etat": "OK",
  "publie_le": "2026-08-28T01:27:48+00:00",
  "dernier_releve_il_y_a_heures": 0.42,
  "seuil_heures": 10,
  "avertissement": "Document statique : il se fige si la publication s'arrete. ..."
}
```

Il n'est pas la cible d'un moniteur par mot-cle, pour la raison exposee plus
haut. Il sert a autre chose, et c'est reel :

- **repondre a la question « ca marche ? » sans identifiants ni tableau de
  bord** : une adresse, un fichier, lisible depuis n'importe quoi ;
- **alimenter une page d'etat ou un script tiers**, qui eux savent comparer
  `publie_le` a l'heure courante ;
- **diagnostiquer apres coup** : l'horodatage du dernier etat publie borne le
  moment de la panne.

### Le rendre lisible : un Worker, pas un bucket public

Le fichier est ecrit dans tous les cas, mais le bucket est prive. La solution
evidente - cocher *Public access* - a ete ecartee apres avoir regarde ce
qu'elle exposerait :

| Prefixe | Fichiers | Taille | Source |
|---|---|---|---|
| `raw/opensky_states` | 104 | 23,4 Mo | OpenSky |
| `raw/opensky_aircraft_db` | 1 | 11,5 Mo | OpenSky |
| `raw/ourairports` | 1 | 4,4 Mo | OurAirports |
| `raw/model_predictions` | 1 | 0,9 Mo | le classifieur |
| autres | 4 | 0,2 Mo | referentiels, sonde, sante |

Aucun secret ni donnee personnelle - mais **35 Mo de donnees OpenSky brutes
en telechargement libre**. Le probleme n'est pas la confidentialite, c'est la
redistribution : consommer leur API pour un projet et republier leur base en
miroir ne sont pas la meme chose, et leurs conditions encadrent la seconde.
Le seul fichier utile pese 297 octets.

Le bucket reste donc prive, et un Worker Cloudflare sert cette unique cle.

#### Ce que le Worker ajoute, et qui ne pouvait pas exister autrement

Un fichier statique ne peut pas signaler sa propre peremption : il se fige et
continue d'annoncer `OK`. **Le Worker calcule au moment de la lecture** et
traduit l'etat en code HTTP :

| Situation | Code | `verdict` |
|---|---|---|
| Publication recente et collecte fraiche | `200` | `TOUT_VA_BIEN` |
| Collecte a l'arret | `503` | `COLLECTE_A_L_ARRET` |
| Publieur a l'arret depuis plus de 10 h | `503` | `PUBLICATION_A_L_ARRET` |
| Document jamais publie | `503` | `JAMAIS_PUBLIE` |
| Tout autre chemin | `404` | |

**La fraicheur devient donc un code HTTP**, et le moniteur de disponibilite
deja en place la detecte sans rien savoir de JSON ni de dates.

Il croise deux sources, parce qu'elles peuvent diverger. `uploaded`, tenu par
R2, dit si le publieur tourne ; le champ `etat` du corps dit si la collecte
est fraiche. Le cas s'est presente pendant l'ecriture de ce Worker : collecte
morte depuis 33 heures, mais document republie a la main quelques minutes
plus tot. Une version qui n'aurait regarde que `uploaded` aurait repondu 200
sur un pipeline mort.

#### Deploiement

Le Worker vit dans `cloudflare/sante/`. Il n'y a aucun identifiant a fournir :
la liaison R2 est interne au compte, et c'est tout l'interet.

```bash
npx wrangler deploy --config cloudflare/sante/wrangler.toml
```

La commande ouvre une page de connexion Cloudflare au premier lancement.
Elle rend une adresse de la forme :

```
https://skytrace-sante.<votre-sous-domaine>.workers.dev
```

Verification :

```bash
curl -i https://skytrace-sante.<votre-sous-domaine>.workers.dev/sante.json
```

Attendu aujourd'hui : `503` avec `"verdict": "COLLECTE_A_L_ARRET"`, puisque
la collecte est effectivement arretee. Le `200` reviendra avec elle.

Verifier aussi qu'aucune autre cle ne fuit :

```bash
curl -i https://skytrace-sante.<votre-sous-domaine>.workers.dev/raw/opensky_states/
```

Attendu : `404`. Le Worker ne filtre pas une liste d'interdits, il n'autorise
qu'une seule cle.

#### A surveiller

Ajouter un second moniteur, a cote de celui qui surveille l'application :

| Champ | Valeur |
|---|---|
| Type | HTTP(s), sur le code de reponse |
| Adresse | l'adresse workers.dev ci-dessus |
| Intervalle | 15 minutes |
| Alerte si | le code n'est pas `200` |

L'offre gratuite couvre 100 000 requetes par jour ; ce moniteur en consomme
96. Le Worker repond `Cache-Control: no-store` : un `200` mis en cache serait
reservi apres l'arret de la collecte et masquerait la panne.

#### Ce qui n'a pas pu etre verifie

Le Worker n'a pas ete execute : cette machine n'a ni Node, ni Deno, ni
`wrangler`. Ce qui a ete verifie, c'est le contrat dont il depend - la cle
existe, R2 porte bien l'horodatage de depot que lit `objet.uploaded`, et le
corps est du JSON analysable a cinq champs. La logique elle-meme est courte
et sans dependance, mais elle sera confirmee par le premier `curl` apres
deploiement, pas avant.


---

# Declencher la collecte depuis l'exterieur

## Pourquoi ne plus dependre de l'ordonnanceur GitHub

Le 26 aout 2026 a 16 h 38 UTC, les taches planifiees du compte ont cesse de
s'executer. Ce qui a ete constate ensuite, sur deux jours :

| Fait | Consequence |
|---|---|
| Les executions par `push` ont continue de fonctionner | Actions n'est pas restreint |
| Un depot voisin, calendriers non modifies, est reparti seul | Ce n'est pas le compte |
| SkyTrace : plus de 40 h sans un seul declenchement planifie | L'ordonnanceur, specifiquement |
| Pousser un commit n'a rien change | L'activite du depot ne suffit pas |

La lecon n'est pas qu'il faut mieux configurer le cron. C'est que
**l'ordonnanceur de GitHub n'est pas un composant sur lequel fonder la
disponibilite d'un pipeline**. Sa documentation le dit elle-meme : les taches
planifiees s'executent « au mieux », sans garantie.

Le declenchement par API n'emprunte pas ce chemin. C'est la voie du bouton
*Run workflow*, celle qui a continue de marcher pendant toute la panne.

## Le principe

Un cron **externe** appelle l'API GitHub toutes les heures. La collecte cesse
de dependre de l'ordonnanceur, exactement comme la surveillance a cesse d'en
dependre. Le cron `schedule` reste en place : quand il fonctionne, il fait le
travail ; quand il se tait, l'appel externe prend le relais.

Les deux peuvent se declencher a la meme heure sans dommage. Le workflow
declare `concurrency: collecte`, donc une seconde execution attend la
premiere, et une collecte en double ne duplique rien : la table de faits est
incrementale en `delete+insert` sur la cle (appareil, instant).

## 1. Creer un jeton a portee etroite

**github.com/settings/personal-access-tokens/new**

| Champ | Valeur |
|---|---|
| Repository access | *Only select repositories* -> `SouleimanME/Skytrace` |
| Permissions | *Actions* -> **Read and write** |
| Expiration | la plus courte qui vous convienne |

Rien d'autre. Ce jeton ne peut que lire et declencher des workflows sur ce
seul depot : c'est le minimum necessaire, et le maximum de degats possible
s'il fuitait reste borne a ce perimetre.

## 2. Le verifier avant de le confier a un tiers

```bash
export SKYTRACE_GITHUB_TOKEN=...
python scripts/declencher_collecte.py SouleimanME/Skytrace
```

Sous PowerShell : `$env:SKYTRACE_GITHUB_TOKEN = "..."`.

Le jeton est lu dans l'environnement et **jamais passe en argument** :
l'historique du terminal le conserverait. Il n'est jamais affiche non plus,
ce qu'un test verifie.

Le script declenche le workflow **puis verifie qu'une execution est apparue**.
C'est une distinction qui a coute une soiree : l'API repond `204` pour dire
qu'elle a recu la demande, pas qu'un travail a demarre. Le soir de la panne,
un declenchement cru effectif n'avait laisse aucune trace.

Chaque echec nomme sa cause plutot que d'echouer en bloc :

| Code | Cause |
|---|---|
| `401` | jeton absent, expire ou mal copie |
| `403` | jeton valide mais sans la permission *Actions* |
| `404` | depot ou workflow introuvable, ou jeton sans acces a ce depot |

## 3. Le programmer chez un tiers

Sur **cron-job.org** (gratuit), creer une tache :

| Champ | Valeur |
|---|---|
| URL | `https://api.github.com/repos/SouleimanME/Skytrace/actions/workflows/collect.yml/dispatches` |
| Methode | `POST` |
| Planification | toutes les heures |
| Corps | `{"ref":"main"}` |

En-tetes a ajouter :

```
Accept: application/vnd.github+json
Authorization: Bearer VOTRE_JETON
X-GitHub-Api-Version: 2022-11-28
Content-Type: application/json
```

Activer la notification par courriel en cas d'echec. Une reponse `204` est un
succes ; cron-job.org la traite comme telle.

## Ce que cela ne couvre pas

Le declencheur garantit que **la demande part**. Il ne garantit pas que
GitHub l'honore : si les Actions du compte etaient un jour reellement
restreintes, l'appel repondrait `403` et la collecte s'arreterait malgre
tout. C'est precisement pour cela que le battement de coeur existe en
parallele - il ne surveille pas la demande, il surveille le **resultat**.

Les deux mecanismes se completent : l'un declenche, l'autre verifie que
quelque chose est arrive. Aucun des deux ne suffit seul.
