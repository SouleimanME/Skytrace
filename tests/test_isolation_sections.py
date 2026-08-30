"""Une section qui echoue ne doit pas emporter la page.

CE QUE CES TESTS PROTEGENT. Streamlit execute le script de haut en bas :
une exception non rattrapee remplace toute la page par un ecran d'erreur.
Une borne de tranche mal calculee dans un panneau accessoire faisait donc
disparaitre la carte, les indicateurs et l'analyse. Le rapport entre la
cause et les degats etait absurde.

Ces tests verifient la propriete inverse : la panne d'une section reste
dans cette section.
"""

from __future__ import annotations

import ast
import sys
import types
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[1] / "dashboard" / "app.py"


def charger_section():
    """Extrait `section()` de l'application, sans executer Streamlit.

    Importer `dashboard/app.py` lancerait tout le tableau de bord. On isole
    la fonction par analyse syntaxique et on lui fournit un faux `st`.
    """
    arbre = ast.parse(APP.read_text(encoding="utf-8"))
    noeud = next(n for n in arbre.body if isinstance(n, ast.FunctionDef) and n.name == "section")

    appels = []

    class FauxStreamlit:
        @staticmethod
        def info(msg):
            appels.append(("info", msg))

        @staticmethod
        def warning(msg):
            appels.append(("warning", msg))

        @staticmethod
        def exception(exc):
            appels.append(("exception", repr(exc)))

        @staticmethod
        def expander(*_a, **_k):
            class Contexte:
                def __enter__(self):
                    return self

                def __exit__(self, *_):
                    return False

            return Contexte()

    faux_duckdb = types.SimpleNamespace(IOException=type("IOException", (Exception,), {}))
    espace = {"st": FauxStreamlit, "duckdb": faux_duckdb}
    exec(compile(ast.Module(body=[noeud], type_ignores=[]), "<section>", "exec"), espace)
    return espace["section"], appels, faux_duckdb


class TestIsolation:
    def test_une_section_saine_rend_vrai(self):
        section, appels, _ = charger_section()
        temoin = []
        assert section("Radar", temoin.append, "ok") is True
        assert temoin == ["ok"]
        assert appels == []

    @pytest.mark.parametrize(
        "panne",
        [
            ValueError("bins must increase monotonically"),
            KeyError("colonne_absente"),
            IndexError("single positional indexer is out-of-bounds"),
            ZeroDivisionError("division by zero"),
            TypeError("unsupported operand"),
            AttributeError("'NoneType' object has no attribute 'x'"),
        ],
    )
    def test_toute_exception_reste_contenue(self, panne):
        """Y compris celles qu'on n'a pas prevues : c'est tout l'interet."""
        section, appels, _ = charger_section()

        def casse():
            raise panne

        assert section("Ponctualité", casse) is False
        assert any(genre == "warning" for genre, _ in appels)
        assert any("Ponctualité" in msg for genre, msg in appels if genre == "warning")

    def test_une_panne_n_empeche_pas_la_suivante(self):
        """LE test qui compte : la contagion est le vrai danger."""
        section, _, _ = charger_section()
        rendus = []

        def casse():
            raise ValueError("panne")

        section("Fautive", casse)
        section("Suivante", rendus.append, "rendue")

        assert rendus == ["rendue"], "une section saine doit rendre apres une section en panne"

    def test_l_entrepot_verrouille_est_une_attente_pas_une_panne(self):
        """Une reecriture concurrente ne merite pas un message d'erreur."""
        section, appels, faux_duckdb = charger_section()

        def verrouille():
            raise faux_duckdb.IOException("lock")

        assert section("Radar", verrouille) is False
        assert appels[0][0] == "info", "un verrou doit informer, pas alarmer"


def test_toutes_les_sections_du_corps_passent_par_la_barriere():
    """Aucune section ne doit etre appelee en direct : une seule suffit a tout casser."""
    source = APP.read_text(encoding="utf-8")
    arbre = ast.parse(source)
    corps = next(
        n for n in arbre.body if isinstance(n, ast.FunctionDef) and n.name == "_render_body"
    )

    directs = [
        n.func.id
        for n in ast.walk(corps)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id.startswith("render_")
    ]
    assert directs == [], f"sections appelees hors barriere : {directs}"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
