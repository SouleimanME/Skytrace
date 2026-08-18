# ADR 0004 - Blocking par grille plutôt qu'extension spatiale

- **Statut** : accepté
- **Date** : 2026-08-17

## Contexte

Il faut rattacher chaque position d'aéronef en phase basse à l'aéroport
qu'elle survole. Formellement, c'est une jointure par proximité entre deux
ensembles de points : *positions × aéroports*.

À l'échelle du projet, `dim_airport` contient 5 273 aérodromes. Une année
de collecte représente plusieurs millions de positions. La jointure naïve
est un produit cartésien de plusieurs milliards de calculs de distance.

Candidats : extension `spatial` de DuckDB (index R-tree), calcul naïf, ou
blocking par grille.

## Décision

**Blocking par grille de 1°**, calcul de haversine en SQL pur, sans
extension.

## Justification

**Contre le calcul naïf.** Il fonctionne sur un snapshot et s'effondre à
mesure que le lac grossit - le pire profil de défaillance : la démo passe,
la production casse trois semaines plus tard.

**Contre l'extension `spatial`.** Elle apporte de vrais index R-tree et
serait le bon choix à grande échelle. Deux raisons de ne pas la prendre
ici : elle ajoute un téléchargement d'extension au démarrage (donc une
dépendance réseau en CI, et un point de panne pour quelqu'un qui clone le
projet), et elle masque le raisonnement. Or ce raisonnement est
précisément ce que le projet doit montrer.

**Pour le blocking :**

1. Filtrer d'abord les positions au sol ou sous 1 200 m - une minorité du
   volume. Un long-courrier à 11 000 m au-dessus d'Orly n'a rien à voir
   avec l'activité d'Orly.
2. Précalculer une cellule de 1° pour chaque aéroport, stockée dans
   `dim_airport.grid_cell`.
3. Générer pour chaque position ses 9 cellules candidates (la sienne et
   ses 8 voisines) et joindre par égalité - une jointure de hachage, que
   le moteur optimise nativement.
4. N'appliquer haversine qu'aux couples survivants.
5. Ne conserver que l'aéroport le plus proche par position.

**Le voisinage n'est pas une précaution, c'est une correction.** Sans les
8 cellules adjacentes, un appareil à 200 m d'un aéroport mais de l'autre
côté d'une frontière de cellule ne serait jamais rattaché. Ce bug ne
toucherait que les aéroports proches d'un multiple entier de degré - donc
une minorité, donc invisible dans une vérification rapide, et redoutable à
diagnostiquer six mois plus tard.

**L'étape 5 évite le double comptage.** Orly et Villacoublay sont assez
proches pour qu'un même appareil tombe dans les deux rayons de 8 km.
Sans arbitrage, le même mouvement serait compté deux fois.

## Conséquences

- Aucune dépendance externe : la CI n'a besoin d'aucun téléchargement
  d'extension.
- Haversine sur une sphère introduit une erreur d'environ 0,5 % face à un
  modèle ellipsoïdal. Sur un rayon de 8 km, cela représente quelques
  dizaines de mètres - sans effet sur le rattachement à un aéroport.
- Une cellule de 1° mesure environ 111 km en latitude et de 0 à 111 km en
  longitude selon la latitude. Près des pôles les cellules deviennent très
  étroites, ce qui augmente le nombre de candidats - sans incidence ici,
  aucun aéroport commercial n'étant à ces latitudes.
- Le rayon (`airport_activity_radius_km`) et le plafond d'altitude
  (`approach_altitude_m`) sont des variables dbt, réglables sans toucher au
  SQL.
- Si le volume dépassait un jour la capacité de cette approche, le passage
  à l'extension `spatial` ne toucherait qu'un seul modèle,
  `int_positions_near_airports`.
