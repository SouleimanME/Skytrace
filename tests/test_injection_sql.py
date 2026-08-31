"""Aucune valeur venue de l'exterieur ne doit devenir de la syntaxe SQL.

CE QUE CES TESTS PROTEGENT. Un audit a trouve trois requetes construites par
interpolation de chaine, dont une alimentee DIRECTEMENT par les arguments de
la ligne de commande :

    adresses = ", ".join(f"'{a.lower()}'" for a in icao24 if a)

Une apostrophe dans une valeur cassait la requete ; une valeur choisie
pouvait en changer le sens. Les trois sont desormais liees en parametres.

Un audit ne protege que le jour ou il est fait. Ces tests, eux, echouent le
jour ou quelqu'un revient a une f-string.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

RACINE = Path(__file__).resolve().parents[1]

#: Charges utiles classiques. Aucune ne doit ni passer, ni casser la requete.
CHARGES = [
    "4bb4e7' or '1'='1",
    "'; drop table marts.dim_aircraft; --",
    "' union select null,null,null,null,null,null,null,null --",
    "4bb4e7'--",
    "'",
    "'" * 10,
]


class TestFiltreAdresseOaci:
    @pytest.mark.parametrize("charge", CHARGES)
    def test_aucune_charge_ne_passe_le_filtre(self, charge):
        from skytrace.ml import _est_adresse_oaci

        assert not _est_adresse_oaci(charge)

    @pytest.mark.parametrize("valide", ["4bb4e7", "ABCDEF", "000000", "a1b2c3"])
    def test_une_vraie_adresse_passe(self, valide):
        from skytrace.ml import _est_adresse_oaci

        assert _est_adresse_oaci(valide)

    def test_le_filtre_est_bien_appele_avant_la_requete(self):
        """Le filtre ne sert a rien s'il n'est pas sur le chemin."""
        source = (RACINE / "src" / "skytrace" / "ml.py").read_text(encoding="utf-8")
        corps = source.split("def predict_aircraft")[1].split("\ndef ")[0]
        assert "_est_adresse_oaci" in corps
        assert "?" in corps, "les adresses doivent etre liees, pas interpolees"


class TestRequetesLiees:
    def test_aucune_valeur_externe_interpolee_dans_le_tableau_de_bord(self):
        """LE test qui aurait empeche la faille : pas de f-string dans du SQL."""
        source = (RACINE / "dashboard" / "app.py").read_text(encoding="utf-8")
        mots = re.compile(r"\b(select|from|where|group by|order by)\b", re.I)

        fautives = []
        for noeud in ast.walk(ast.parse(source)):
            if not isinstance(noeud, ast.JoinedStr):
                continue
            texte = "".join(p.value for p in noeud.values if isinstance(p, ast.Constant))
            interpole = [p for p in noeud.values if isinstance(p, ast.FormattedValue)]
            if interpole and mots.search(texte):
                fautives.append(noeud.lineno)

        assert fautives == [], (
            f"SQL construit par interpolation aux lignes {fautives}. "
            "Utiliser des marqueurs `?` et l'argument `params` de `load`."
        )

    def test_load_accepte_des_parametres(self):
        """Sans ce parametre, les appelants n'ont pas d'autre choix que la f-string."""
        source = (RACINE / "dashboard" / "app.py").read_text(encoding="utf-8")
        signature = next(
            n
            for n in ast.walk(ast.parse(source))
            if isinstance(n, ast.FunctionDef) and n.name == "load"
        )
        noms = [a.arg for a in signature.args.args]
        assert "params" in noms


def test_les_regles_de_securite_restent_activees():
    """La protection durable n'est pas l'audit, c'est la regle qui le rejoue."""
    config = (RACINE / "pyproject.toml").read_text(encoding="utf-8")
    selection = config.split("select = [")[1].split("]")[0]
    assert '"S"' in selection, "les regles de securite (bandit) doivent rester actives"
