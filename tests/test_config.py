"""Tests de la configuration et du calcul de couts."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from skytrace.config import (
    BOUNDING_BOXES,
    CREDITS_ANONYME,
    CREDITS_AUTHENTIFIE,
    BoundingBox,
    Settings,
)


class TestBoundingBox:
    def test_area_is_the_product_of_both_spans(self):
        box = BoundingBox("test", lamin=40.0, lomin=0.0, lamax=50.0, lomax=10.0)
        assert box.area_sq_deg == pytest.approx(100.0)

    @pytest.mark.parametrize(
        ("span", "expected_cost"),
        [
            (4.0, 1),  # 16 deg2  -> palier 1
            (9.0, 2),  # 81 deg2  -> palier 2
            (15.0, 3),  # 225 deg2 -> palier 3
            (40.0, 4),  # 1600 deg2 -> palier maximal
        ],
    )
    def test_credit_cost_follows_the_published_tiers(self, span, expected_cost):
        box = BoundingBox("test", lamin=0.0, lomin=0.0, lamax=span, lomax=span)
        assert box.credit_cost == expected_cost

    def test_tier_boundaries_are_inclusive(self):
        # Exactement 25 deg2 doit rester dans le palier a 1 credit :
        # une erreur de comparaison stricte ici ferait payer double.
        box = BoundingBox("test", lamin=0.0, lomin=0.0, lamax=5.0, lomax=5.0)
        assert box.area_sq_deg == 25.0
        assert box.credit_cost == 1

    def test_france_stays_within_the_anonymous_budget(self):
        # 96 executions par jour (toutes les 15 min) doivent tenir dans les
        # 400 credits du mode anonyme. Ce test protege le choix de cadence
        # inscrit dans les plannings Dagster.
        assert BOUNDING_BOXES["france"].credit_cost * 96 <= 400

    def test_contains(self):
        france = BOUNDING_BOXES["france"]
        assert france.contains(48.8566, 2.3522)  # Paris
        assert not france.contains(51.5074, -0.1278)  # Londres, hors fenetre


class TestSettings:
    def test_unknown_region_is_rejected(self, tmp_path):
        with pytest.raises(ValidationError, match="region inconnue"):
            Settings(SKYTRACE_REGION="atlantide", SKYTRACE_DATA_DIR=tmp_path)

    def test_region_is_normalised(self, tmp_path):
        assert Settings(SKYTRACE_REGION="  EUROPE ", SKYTRACE_DATA_DIR=tmp_path).region == "europe"

    def test_missing_credentials_means_anonymous(self, settings):
        assert settings.is_anonymous

    def test_credentials_disable_anonymous_mode(self, authenticated_settings):
        assert not authenticated_settings.is_anonymous

    def test_partial_credentials_stay_anonymous(self, tmp_path):
        # Un identifiant sans secret ne permet aucune authentification :
        # mieux vaut basculer en anonyme que d'echouer a chaque appel.
        partial = Settings(OPENSKY_CLIENT_ID="id-seul", SKYTRACE_DATA_DIR=tmp_path)
        assert partial.is_anonymous

    def test_empty_env_var_falls_back_to_default(self, monkeypatch):
        # `SKYTRACE_DATA_DIR=` dans un `.env` doit se comporter comme une
        # variable absente, pas comme le chemin vide.
        monkeypatch.setenv("SKYTRACE_DATA_DIR", "")
        assert Settings().resolved_data_dir.name == "data"

    def test_dbt_env_uses_forward_slashes(self, settings):
        # DuckDB interprete les motifs glob avec des slash avant, y compris
        # sous Windows : un antislash casserait read_parquet('**/*.parquet').
        env = settings.dbt_env()
        assert "\\" not in env["SKYTRACE_DATA_DIR"]
        assert "\\" not in env["SKYTRACE_DUCKDB_PATH"]

    def test_ensure_directories_is_idempotent(self, settings):
        settings.ensure_directories()
        settings.ensure_directories()
        assert settings.states_dir.is_dir()
        assert settings.airports_dir.is_dir()


class TestBudgetDeCredits:
    """Le garde-fou de credits doit suivre ce que le compte permet.

    Il etait ecrit en dur a 400 - la limite ANONYME. Poser des identifiants
    OpenSky n'aurait donc rien change : notre propre compteur aurait refuse
    les appels bien avant qu'OpenSky ne les refuse, et le gain attendu du
    compte (dix fois plus de credits, plus l'historique) serait reste
    invisible. Un garde-fou cale sur la mauvaise limite bride en silence.
    """

    def test_sans_identifiants_le_budget_est_anonyme(self, monkeypatch):
        monkeypatch.delenv("OPENSKY_CLIENT_ID", raising=False)
        monkeypatch.delenv("OPENSKY_CLIENT_SECRET", raising=False)
        monkeypatch.delenv("SKYTRACE_DAILY_CREDIT_BUDGET", raising=False)
        reglages = Settings()
        assert reglages.is_anonymous
        assert reglages.resolved_credit_budget == CREDITS_ANONYME

    def test_avec_identifiants_le_budget_decuple(self, monkeypatch):
        monkeypatch.setenv("OPENSKY_CLIENT_ID", "un-identifiant")
        monkeypatch.setenv("OPENSKY_CLIENT_SECRET", "un-secret")
        monkeypatch.delenv("SKYTRACE_DAILY_CREDIT_BUDGET", raising=False)
        reglages = Settings()
        assert not reglages.is_anonymous
        assert reglages.resolved_credit_budget == CREDITS_AUTHENTIFIE
        assert CREDITS_AUTHENTIFIE == 10 * CREDITS_ANONYME

    def test_une_valeur_explicite_l_emporte(self, monkeypatch):
        """Sert a se brider volontairement, jamais a depasser ce que le compte permet."""
        monkeypatch.setenv("OPENSKY_CLIENT_ID", "un-identifiant")
        monkeypatch.setenv("OPENSKY_CLIENT_SECRET", "un-secret")
        monkeypatch.setenv("SKYTRACE_DAILY_CREDIT_BUDGET", "1000")
        assert Settings().resolved_credit_budget == 1000

    def test_un_identifiant_seul_ne_suffit_pas(self, monkeypatch):
        """Une moitie d'identifiants n'authentifie rien : on reste au budget anonyme."""
        monkeypatch.setenv("OPENSKY_CLIENT_ID", "un-identifiant")
        monkeypatch.delenv("OPENSKY_CLIENT_SECRET", raising=False)
        monkeypatch.delenv("SKYTRACE_DAILY_CREDIT_BUDGET", raising=False)
        reglages = Settings()
        assert reglages.is_anonymous
        assert reglages.resolved_credit_budget == CREDITS_ANONYME
