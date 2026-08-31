"""La convention des messages de commit doit se verifier, pas se rappeler.

Un audit de l'historique a trouve un seul ecart sur soixante-quatre commits,
et il etait incoherent avec lui-meme : "fiabilite" et "derive" sans accent,
"modele" avec, dans la meme phrase. Une convention appliquee de memoire finit
toujours ainsi.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from verifier_commits import SUJET_MAX, TYPES, verifier  # noqa: E402


class TestAccents:
    def test_un_sujet_ascii_passe(self):
        assert verifier("fix: bornes de ponctualite croissantes") == []

    @pytest.mark.parametrize(
        "sujet",
        [
            "feat: ajout d'un modèle ML",
            "fix: correction de la dérive",
            "docs: précisions sur l'entrepôt",
        ],
    )
    def test_un_accent_est_refuse(self, sujet):
        """LE cas trouve dans l'historique reel."""
        reproches = verifier(sujet)
        assert any("ASCII" in r for r in reproches)

    def test_le_caractere_fautif_est_nomme(self):
        """Reprocher sans montrer obligerait a chercher a l'oeil."""
        (reproche,) = [r for r in verifier("feat: modèle") if "ASCII" in r]
        assert "è" in reproche


class TestPrefixe:
    @pytest.mark.parametrize("type_", TYPES)
    def test_chaque_type_conventionnel_passe(self, type_):
        assert verifier(f"{type_}: quelque chose") == []

    def test_une_portee_est_acceptee(self):
        assert verifier("fix(dashboard): une correction") == []

    def test_un_changement_incompatible_est_accepte(self):
        assert verifier("feat!: une rupture") == []

    @pytest.mark.parametrize(
        "sujet",
        ["Initial commit: pipeline", "correction du bug", "WIP", "Fix: majuscule"],
    )
    def test_un_sujet_sans_prefixe_valide_est_refuse(self, sujet):
        assert any("prefixe" in r for r in verifier(sujet))


class TestLongueurEtPonctuation:
    def test_un_sujet_trop_long_est_refuse(self):
        """Trouve dans l'historique : 106 caracteres, tronque partout."""
        assert any("caracteres" in r for r in verifier("fix: " + "a" * SUJET_MAX))

    def test_la_limite_exacte_passe(self):
        assert verifier("fix: " + "a" * (SUJET_MAX - 5)) == []

    @pytest.mark.parametrize("tiret", ["\u2014", "\u2013"])
    def test_un_tiret_cadratin_est_refuse(self, tiret):
        """Le depot n'en contient aucun : les messages ne doivent pas en introduire."""
        assert any("tiret" in r for r in verifier(f"fix: avant {tiret} apres"))


def test_les_reproches_sont_cumules():
    """Un message doublement fautif doit tout dire d'un coup."""
    reproches = verifier("Mauvais sujet avec un accent é")
    assert len(reproches) >= 2
