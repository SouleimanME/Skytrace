"""Garde-fou : les noms de colonnes du tableau de bord restent en ASCII.

Ce test existe parce que l'erreur s'est produite trois fois.

L'interface est en francais accentue, mais les noms de colonnes servent de
cle entre le SQL et le Python. Accentuer `overview['aeronefs']` sans toucher
a l'alias `as aeronefs` de la requete donne un KeyError qui ne se voit qu'a
l'execution, dans une section que l'on n'ouvre pas forcement en relisant.
Meme piege pour les identifiants Python accentues, qui « marchent » tant que
toutes leurs occurrences sont accentuees de la meme facon.

La regle retenue, et ce que ce test verifie :

  * un identifiant Python ne porte jamais d'accent ;
  * une chaine MINUSCULE dont la version sans accent est un alias SQL du
    fichier est un nom de colonne, donc elle reste en ASCII.

Les libelles d'affichage, eux, portent une majuscule ("Aeronefs distincts")
et restent accentues : c'est ce qui les distingue.
"""

from __future__ import annotations

import ast
import io
import re
import tokenize
import unicodedata
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[1] / "dashboard" / "app.py"


@pytest.fixture(scope="module")
def source() -> str:
    return APP.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def arbre(source: str) -> ast.Module:
    return ast.parse(source)


def sans_accent(texte: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", texte) if unicodedata.category(c) != "Mn"
    )


def alias_sql(arbre: ast.Module) -> set[str]:
    """Alias de colonnes declares dans les requetes du fichier."""
    alias: set[str] = set()
    for noeud in ast.walk(arbre):
        est_requete = (
            isinstance(noeud, ast.Constant)
            and isinstance(noeud.value, str)
            and re.search(r"\bselect\b", noeud.value, re.IGNORECASE)
        )
        if est_requete:
            alias |= {
                m.lower()
                for m in re.findall(r"\bas\s+([a-z_][a-z0-9_]*)", noeud.value, re.IGNORECASE)
            }
    return alias


def test_des_alias_sql_sont_bien_detectes(arbre):
    # Si cette extraction cassait, le test suivant passerait pour de mauvaises
    # raisons : il ne verifierait plus rien.
    alias = alias_sql(arbre)
    assert len(alias) > 15
    assert "aeronefs" in alias


def test_aucun_identifiant_python_accentue(source):
    accentues = [
        (jeton.start[0], jeton.string)
        for jeton in tokenize.generate_tokens(io.StringIO(source).readline)
        if jeton.type == tokenize.NAME and any(c > "\x7f" for c in jeton.string)
    ]
    assert accentues == [], (
        "identifiants accentues (ils fonctionnent tant que toutes leurs "
        f"occurrences le sont, puis cassent silencieusement) : {accentues}"
    )


def test_aucun_nom_de_colonne_accentue(arbre):
    alias = alias_sql(arbre)
    collisions = sorted(
        {
            (noeud.lineno, noeud.value)
            for noeud in ast.walk(arbre)
            if isinstance(noeud, ast.Constant)
            and isinstance(noeud.value, str)
            and any(c > "\x7f" for c in noeud.value)
            and noeud.value == noeud.value.lower()
            and " " not in noeud.value
            and sans_accent(noeud.value).lower() in alias
        }
    )
    assert collisions == [], (
        "chaines accentuees qui designent une colonne SQL : le SQL, lui, "
        f"reste en ASCII, donc la lecture echouera. {collisions}"
    )


def test_les_libelles_daffichage_restent_accentues(source):
    # Le garde-fou ne doit pas pousser a tout desaccentuer : l'interface est
    # en francais. On verifie qu'elle l'est reste.
    assert '"Aéronefs distincts"' in source
    assert "Aéroport" in source


class TestBornesDePonctualite:
    """Les tranches d'ecart entre releves doivent croitre, pour TOUTE cadence.

    CE QUE CE TEST AURAIT ATTRAPE. Les bornes etaient ecrites en dur a partir
    d'une cadence de 30 minutes : `[0, SCHEDULE_MINUTES + 5, 60, 120, 240]`.
    Le passage a 60 minutes a donne `[0, 65, 60, ...]`, ou la premiere borne
    depasse la deuxieme. `pd.cut` refuse des bornes non croissantes, et le
    tableau de bord DEPLOYE plantait entierement - pas seulement la section
    fautive : l'exception remontait jusqu'au corps de la page.

    Rien ne l'avait vu. Les tests portaient sur les colonnes lues et sur les
    accents, jamais sur une valeur DERIVEE d'une constante de configuration.
    Changer la constante etait pourtant l'operation la plus probable.
    """

    @staticmethod
    def bornes(cadence: int) -> list[float]:
        """Reproduit le calcul de `render_collection_punctuality`."""
        ponctuel = min(cadence + 5, 2 * cadence - 1)
        return [0, ponctuel, 2 * cadence, 4 * cadence, 8 * cadence, float("inf")]

    @pytest.mark.parametrize("cadence", [5, 10, 15, 20, 30, 45, 60, 90, 120, 180, 240])
    def test_les_bornes_croissent_strictement(self, cadence):
        bornes = self.bornes(cadence)
        assert all(bornes[i] < bornes[i + 1] for i in range(len(bornes) - 1)), (
            f"bornes non croissantes a {cadence} min : {bornes}"
        )

    def test_pandas_accepte_reellement_ces_bornes(self):
        """Le vrai juge est `pd.cut`, pas notre relecture des inegalites."""
        import pandas as pd

        for cadence in (15, 30, 60, 120):
            bornes = self.bornes(cadence)
            decoupe = pd.cut(
                pd.Series([1.0, 50.0, 200.0, 5000.0]),
                bins=bornes,
                labels=[f"t{i}" for i in range(len(bornes) - 1)],
                right=False,
            )
            assert decoupe.notna().all()

    def test_la_cadence_du_code_est_couverte(self):
        """La valeur reellement en service doit passer, pas seulement des cas d'ecole."""
        source = APP.read_text(encoding="utf-8")
        ligne = next(x for x in source.splitlines() if x.startswith("SCHEDULE_MINUTES"))
        cadence = int(ligne.split("=")[1].strip())
        bornes = self.bornes(cadence)
        assert all(bornes[i] < bornes[i + 1] for i in range(len(bornes) - 1))
