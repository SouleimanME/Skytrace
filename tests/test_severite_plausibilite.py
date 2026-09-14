"""Les controles de plausibilite de capteur ne doivent JAMAIS bloquer le build.

CE QUI EST ARRIVE. Le 5 septembre 2026 a 12h43 UTC, des transpondeurs ont
transmis des caps hors de l'intervalle [0, 360] - de 370 a 499 degres. Le test
`heading_deg` etait le seul des trois controles de plausibilite laisse en
`error`, par oubli : ses voisins `barometric_altitude_m` et `ground_speed_ms`
etaient bien en `warn`, avec un commentaire expliquant pourquoi.

CE QUI A RENDU LA PANNE PERMANENTE. La source lit tout l'historique du lac.
Une ligne aberrante n'est donc pas un incident du jour mais une condition
d'echec definitive : elle est relue a chaque build, et la retention la garde
180 jours. Du 5 au 15 septembre, la collecte horaire a echoue 294 fois de
suite, pour 35 lignes sur 10,6 millions. Environ 270 courriels.

Et les reessais n'y pouvaient rien : une donnee aberrante ne devient pas
correcte parce qu'on rejoue la collecte. Ils ont seulement re-ingere trois fois
par heure, pour trois fois le cout en credits. Ce second defaut est verrouille
ailleurs, par `tests/test_codes_de_sortie.py`.

CE QUE CE TEST VERROUILLE. Toute borne physique posee sur une mesure de
capteur doit etre en `severity: warn`. Un capteur qui ment est un FAIT a
signaler, pas un bogue du pipeline a traiter en panne. Les invariants
structurels - unicite, non-nullite, integrite referentielle - restent durs.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

MODELES = Path(__file__).resolve().parents[1] / "dbt" / "skytrace" / "models"

#: Colonnes qui portent une MESURE DE CAPTEUR, ou une valeur qui en derive
#: directement. Une valeur hors bornes y est du bruit, jamais une rupture de
#: contrat.
#:
#: Les colonnes des marts y figurent au meme titre que celles du staging : une
#: conversion d'unite (m/s vers noeuds) ne transforme pas une mesure de capteur
#: en invariant structurel, et `position_age_seconds` depend d'une horloge que
#: nous ne tenons pas. Ce qui reste en `error` ailleurs est calcule par nos
#: propres modeles : y echouer signale un bogue de notre SQL, et doit bloquer.
MESURES_DE_CAPTEUR = {
    "barometric_altitude_m",
    "geometric_altitude_m",
    "ground_speed_ms",
    "heading_deg",
    "vertical_rate_ms",
    # marts : memes grandeurs, autres unites, plus l'age de la position
    "barometric_altitude_ft",
    "ground_speed_kt",
    "position_age_seconds",
}


def bornes_declarees():
    """Rend (fichier, modele, colonne, severite) pour chaque `accepted_range`.

    Parcourt TOUS les modeles, pas seulement le staging : la premiere version
    de ce garde-fou ne regardait que `staging/`, et laissait donc passer
    exactement le meme piege un etage plus haut.
    """
    trouves = []
    for chemin in MODELES.rglob("*.yml"):
        contenu = yaml.safe_load(chemin.read_text(encoding="utf-8")) or {}
        for modele in contenu.get("models", []):
            for colonne in modele.get("columns", []):
                for test in colonne.get("data_tests", []) or []:
                    if not isinstance(test, dict) or "accepted_range" not in test:
                        continue
                    config = (test["accepted_range"] or {}).get("config") or {}
                    trouves.append(
                        (chemin.name, modele["name"], colonne["name"], config.get("severity"))
                    )
    return trouves


def test_il_existe_bien_des_bornes_a_verifier():
    """Un test qui ne trouve rien passerait pour de bonnes raisons imaginaires."""
    assert bornes_declarees(), "aucun accepted_range trouve : la lecture du yml a change"


@pytest.mark.parametrize("entree", bornes_declarees(), ids=lambda e: f"{e[1]}.{e[2]}")
def test_une_mesure_de_capteur_avertit_sans_bloquer(entree):
    _fichier, _modele, colonne, severite = entree
    if colonne not in MESURES_DE_CAPTEUR:
        pytest.skip(f"{colonne} n'est pas une mesure de capteur")
    assert severite == "warn", (
        f"`{colonne}` porte une borne physique en severite '{severite}'. "
        "Une valeur de capteur aberrante doit AVERTIR, jamais interrompre le "
        "build : la source est lue en entier, donc une seule ligne hors bornes "
        "bloque tous les builds suivants. C'est ce qui a coute dix jours de "
        "pannes horaires et environ 270 courriels en septembre 2026."
    )


def test_le_cap_est_couvert_par_ce_garde_fou():
    """La colonne qui a declenche l'incident doit rester dans le perimetre."""
    assert "heading_deg" in MESURES_DE_CAPTEUR
    colonnes = {c for _f, _m, c, _s in bornes_declarees()}
    assert "heading_deg" in colonnes, "le test de plage sur le cap a disparu"
