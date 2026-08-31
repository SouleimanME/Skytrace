"""Le declencheur externe doit nommer la cause, pas seulement echouer.

Un cron externe qui echoue silencieusement remplacerait un ordonnanceur qui
se tait par un autre. Ces tests portent sur la traduction d'une reponse HTTP
en cause identifiable, et sur le fait qu'un jeton ne fuit nulle part.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import declencher_collecte as dc  # noqa: E402


class TestDiagnostic:
    def test_deux_cent_quatre_est_le_seul_succes(self):
        """L'API repond 204 sans corps quand elle accepte la demande."""
        accepte, _ = dc.diagnostiquer(204, "")
        assert accepte

    def test_deux_cents_n_est_pas_un_succes(self):
        """Un 200 signale une autre route que celle attendue, donc une erreur."""
        accepte, _ = dc.diagnostiquer(200, "{}")
        assert not accepte

    @pytest.mark.parametrize(
        ("code", "attendu"),
        [
            (401, "jeton"),
            (403, "permission"),
            (404, "introuvable"),
            (422, "refusee"),
            (0, "injoignable"),
        ],
    )
    def test_chaque_echec_nomme_sa_cause(self, code, attendu):
        """Un message generique obligerait a deviner, donc a ne rien corriger."""
        accepte, message = dc.diagnostiquer(code, "detail")
        assert not accepte
        assert attendu in message.lower()

    def test_le_403_distingue_la_permission_du_jeton(self):
        """La confusion la plus courante : jeton valide mais portee trop etroite."""
        _, message = dc.diagnostiquer(403, "")
        assert "Actions" in message


class TestSecuriteDuJeton:
    def test_le_jeton_ne_se_lit_que_dans_l_environnement(self):
        """Passe en argument, il resterait dans l'historique du terminal."""
        source = Path(dc.__file__).read_text(encoding="utf-8")
        assert "SKYTRACE_GITHUB_TOKEN" in source
        assert "argv" not in source.split("def _requete")[0].split("jeton")[-1][:200]

    def test_absence_de_jeton_arrete_avant_tout_appel(self, monkeypatch, capsys):
        monkeypatch.delenv("SKYTRACE_GITHUB_TOKEN", raising=False)

        def interdit(*_a, **_k):
            raise AssertionError("aucun appel reseau ne doit partir sans jeton")

        monkeypatch.setattr(dc, "_requete", interdit)
        assert dc.main(["prog", "proprietaire/depot"]) == 2
        assert "SKYTRACE_GITHUB_TOKEN" in capsys.readouterr().err

    def test_le_jeton_n_est_jamais_affiche(self, monkeypatch, capsys):
        """LE test qui compte : un secret imprime dans un journal est un secret perdu."""
        secret = "valeur-factice-qui-ne-doit-jamais-paraitre"
        monkeypatch.setenv("SKYTRACE_GITHUB_TOKEN", secret)
        monkeypatch.setattr(dc, "derniere_execution", lambda *_a, **_k: "2026-01-01T00:00:00Z")
        monkeypatch.setattr(dc, "_requete", lambda *_a, **_k: (401, "Bad credentials"))

        dc.main(["prog", "proprietaire/depot"])

        capture = capsys.readouterr()
        assert secret not in capture.out
        assert secret not in capture.err


class TestVerificationReelle:
    def test_un_accuse_de_reception_ne_suffit_pas(self, monkeypatch, capsys):
        """L'erreur qui a coute une soiree : croire un declenchement sur parole.

        GitHub repond 204 pour dire qu'il a recu la demande. Il ne promet pas
        qu'une execution demarre - et le soir de la panne, aucune n'a demarre
        alors que l'interface semblait accepter. Le script doit donc constater
        l'apparition d'une execution, et echouer s'il n'en voit aucune.
        """
        monkeypatch.setenv("SKYTRACE_GITHUB_TOKEN", "jeton")
        monkeypatch.setattr(dc, "_requete", lambda *_a, **_k: (204, ""))
        # L'horodatage ne bouge jamais : aucune execution n'a ete creee.
        monkeypatch.setattr(dc, "derniere_execution", lambda *_a, **_k: "2026-01-01T00:00:00Z")
        monkeypatch.setattr(dc, "ATTENTE_MAX_SECONDES", 0.2)
        monkeypatch.setattr(dc, "INTERVALLE_SECONDES", 0.05)

        assert dc.main(["prog", "proprietaire/depot"]) == 1
        assert "AUCUNE execution" in capsys.readouterr().err

    def test_une_execution_apparue_vaut_succes(self, monkeypatch):
        monkeypatch.setenv("SKYTRACE_GITHUB_TOKEN", "jeton")
        monkeypatch.setattr(dc, "_requete", lambda *_a, **_k: (204, ""))
        horodatages = iter(["2026-01-01T00:00:00Z", "2026-01-01T00:05:00Z"])
        monkeypatch.setattr(dc, "derniere_execution", lambda *_a, **_k: next(horodatages))
        monkeypatch.setattr(dc, "INTERVALLE_SECONDES", 0.01)

        assert dc.main(["prog", "proprietaire/depot"]) == 0


def test_la_branche_visee_est_la_branche_par_defaut():
    """Un workflow ne se declenche que depuis la branche par defaut."""
    assert dc.BRANCHE == "main"


def test_la_peremption_de_l_api_est_documentee():
    """GitHub annonce lui-meme la fin de vie de la version epinglee.

    Mesure du 31 aout 2026, en-tetes de reponse :

        Deprecation: Tue, 10 Mar 2026
        Sunset:      Fri, 10 Mar 2028

    Le declencheur externe cessera donc de fonctionner en mars 2028 si
    personne ne releve la version. Ce test ne peut pas l'empecher, mais il
    garantit que la date reste ecrite noir sur blanc a cote du code qui en
    depend, plutot que perdue dans un historique de conversation.
    """
    assert dc.API_VERSION == "2022-11-28"
    assert dc.API_VERSION_SUNSET == "2028-03-10"
    source = Path(dc.__file__).read_text(encoding="utf-8")
    assert "Sunset" in source, "la date de fin de vie doit rester visible dans le code"
