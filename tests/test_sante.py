"""Le document de sante doit dire ce qu'il sait, et avouer ce qu'il ignore.

Un document d'exploitation qui se trompe est pire qu'absent : on cesse de
verifier soi-meme. Ces tests portent sur le verdict et sur l'avertissement,
parce que l'avertissement fait partie du contrat.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from skytrace import storage


@pytest.fixture
def sans_r2(monkeypatch, tmp_path):
    """Reglages locaux : aucun appel reseau dans ces tests."""

    class Reglages:
        uses_r2 = False
        resolved_data_dir = tmp_path

    monkeypatch.setattr(storage, "get_settings", lambda: Reglages())
    return Reglages()


def fixer_age(monkeypatch, heures):
    monkeypatch.setattr(
        storage,
        "newest_snapshot_age_seconds",
        lambda *_a, **_k: None if heures is None else heures * 3600,
    )


class TestVerdict:
    def test_collecte_recente_vaut_ok(self, monkeypatch, sans_r2):
        fixer_age(monkeypatch, 2)
        assert storage.build_health_document()["etat"] == "OK"

    def test_au_dela_du_seuil_l_arret_est_annonce(self, monkeypatch, sans_r2):
        fixer_age(monkeypatch, storage.HEALTH_STALE_AFTER_HOURS + 1)
        assert storage.build_health_document()["etat"] == "COLLECTE_A_L_ARRET"

    def test_le_seuil_exact_reste_acceptable(self, monkeypatch, sans_r2):
        """A la limite on ne crie pas : le cron GitHub n'est pas ponctuel."""
        fixer_age(monkeypatch, storage.HEALTH_STALE_AFTER_HOURS)
        assert storage.build_health_document()["etat"] == "OK"

    def test_lac_vide_se_distingue_d_une_collecte_arretee(self, monkeypatch, sans_r2):
        """Jamais collecte et ne collecte plus sont deux situations distinctes."""
        fixer_age(monkeypatch, None)
        document = storage.build_health_document()
        assert document["etat"] == "AUCUNE_DONNEE"
        assert document["dernier_releve_il_y_a_heures"] is None


class TestContratPublic:
    def test_le_document_avoue_qu_il_se_fige(self, monkeypatch, sans_r2):
        """LE test qui compte, et il porte sur une limite, pas sur une valeur.

        Le fichier est statique. Si la collecte s'arrete, plus personne ne le
        reecrit : il se fige sur son dernier contenu, donc sur "OK". Un
        moniteur qui y cherche le mot OK ne verra jamais l'arret qu'il est
        cense detecter. Le document doit donc porter cet avertissement et
        publier l'horodatage qui permet au lecteur de trancher lui-meme.
        """
        fixer_age(monkeypatch, 1)
        document = storage.build_health_document()

        assert "publie_le" in document
        assert "fige" in document["avertissement"]
        assert "publie_le" in document["avertissement"]
        datetime.fromisoformat(document["publie_le"])  # doit etre analysable

    def test_l_horodatage_est_en_utc(self, monkeypatch, sans_r2):
        fixer_age(monkeypatch, 1)
        publie = datetime.fromisoformat(storage.build_health_document()["publie_le"])
        assert publie.utcoffset() == datetime.now(UTC).utcoffset()

    def test_le_seuil_publie_est_celui_applique(self, monkeypatch, sans_r2):
        """Publier un seuil different de celui utilise induirait en erreur."""
        fixer_age(monkeypatch, 1)
        assert storage.build_health_document()["seuil_heures"] == storage.HEALTH_STALE_AFTER_HOURS


def test_ecriture_locale_produit_du_json_analysable(monkeypatch, sans_r2, tmp_path):
    import json

    fixer_age(monkeypatch, 3)
    resultat = storage.publish_health()
    contenu = json.loads((tmp_path / storage.HEALTH_KEY).read_text(encoding="utf-8"))
    assert contenu["etat"] == "OK"
    assert resultat.size_bytes > 0


def test_la_cle_ne_tombe_pas_sous_raw():
    """Sous `raw/`, le document finirait un jour dans une source dbt."""
    assert not storage.HEALTH_KEY.startswith("raw/")
