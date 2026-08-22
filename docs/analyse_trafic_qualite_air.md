# Le trafic aerien se lit-il dans le NO2 au sol ?

Analyse construite sur le mart `fct_airport_hourly_air_quality`, qui joint l'activite aeroportuaire horaire (positions ADS-B OpenSky) aux concentrations de polluants mesurees au sol a proximite (Open-Meteo).

## Donnees

- Panel : **307 observations** (aeroport x heure).
- Aeroports : **14**.
- Periode : 2026-08-17 19:00:00+00:00 -> 2026-08-21 21:00:00+00:00 (UTC).

> Echantillon encore modeste : il s'etoffe a chaque execution du pipeline. Les ordres de grandeur ci-dessous sont deja stables, la precision augmentera avec le temps.

## Resultat

Hypothese testee : *plus il y a d'avions autour d'un aeroport a une heure donnee, plus le NO2 mesure au sol est eleve.*

| Niveau de controle | Correlation avions ~ NO2 |
|---|---|
| 1. Brute (pooled) | **+0.149** |
| 2. Intra-aeroport (retrait de l'effet aeroport) | **-0.225** |
| 3. Intra-aeroport et de-saisonnalisee (retrait du cycle jour/nuit) | **-0.159** |

Vu aeroport par aeroport, la correlation est negative pour **12 des 14** aeroports ayant assez d'heures :

| Aeroport | Heures | r (avions ~ NO2) |
|---|---|---|
| LYS | 17 | -0.455 |
| FRA | 25 | -0.432 |
| LGW | 26 | -0.360 |
| BOD | 16 | -0.276 |
| NCE | 21 | -0.242 |
| ORY | 25 | -0.236 |
| BCN | 26 | -0.229 |
| ZRH | 26 | -0.108 |
| MRS | 19 | -0.096 |
| TLS | 19 | -0.090 |
| CDG | 25 | -0.061 |
| BSL | 25 | -0.060 |
| NTE | 11 | +0.018 |
| MXP | 26 | +0.051 |

![Trafic vs NO2](img/analyse_trafic_qualite_air.png)

## Interpretation

La correlation brute, faiblement positive, disparait des qu'on controle les facteurs de confusion :

- **Effet 'entre aeroports'.** Les grands hubs sont dans des metropoles au NO2 de fond plus eleve. Ils cumulent donc beaucoup de trafic ET beaucoup de NO2, ce qui gonfle la correlation globale sans qu'il y ait de lien de cause a effet. Une fois la moyenne de chaque aeroport retiree, cet artefact tombe.
- **Cycle journalier partage.** Trafic aerien et NO2 (chauffage, trafic routier) montent le jour et baissent la nuit. Retirer la moyenne par heure de la journee neutralise ce co-mouvement.

**Conclusion : a l'echelle horaire, le trafic aerien n'est pas un predicteur detectable du NO2 au sol.** Le NO2 mesure a proximite d'un aeroport est domine par le trafic routier, le chauffage et la meteo, pas par les avions eux-memes. Le resultat est presente comme une correlation descriptive, jamais comme une causalite - et l'hypothese naive de depart est explicitement rejetee par les donnees.

## Limites

- Open-Meteo fournit un NO2 **modelise** (reanalyse), pas une station de mesure physique au bout de piste.
- Le point de reference est le centre de l'aeroport ; le panache reel depend du vent, non pris en compte ici.
- L'activite est inferee de la couverture ADS-B, inegale selon les zones.
