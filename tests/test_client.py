"""Tests du client OpenSky : quotas, resilience, parametrage des requetes."""

from __future__ import annotations

import httpx
import pytest
import respx

from skytrace.config import BOUNDING_BOXES
from skytrace.opensky.client import (
    BASE_URL,
    CreditBudgetExceededError,
    CreditLedger,
    OpenSkyClient,
)

STATES_URL = f"{BASE_URL}/states/all"


def snapshot_payload(count: int = 2) -> dict:
    return {
        "time": 1755441600,
        "states": [
            [
                f"abc{index:03d}",
                "TEST123 ",
                "France",
                1755441600,
                1755441600,
                2.35,
                48.85,
                10000.0,
                False,
                230.0,
                180.0,
                0.0,
                None,
                10100.0,
                "1000",
                False,
                0,
                3,
            ]
            for index in range(count)
        ],
    }


@pytest.fixture
def fast_settings(settings):
    """Configuration sans attente entre les tentatives."""
    return settings.model_copy(update={"retry_backoff_seconds": 0.0})


class TestCreditLedger:
    def test_starts_at_zero(self, tmp_path):
        ledger = CreditLedger(tmp_path / "ledger.json", daily_budget=400)
        assert ledger.spent_today() == 0
        assert ledger.remaining_today() == 400

    def test_consumption_accumulates_and_persists(self, tmp_path):
        path = tmp_path / "ledger.json"
        CreditLedger(path, 400).record(3)
        CreditLedger(path, 400).record(3)

        # Un nouveau process doit retrouver le compteur : sinon un
        # ordonnanceur qui relance un worker toutes les 15 minutes
        # repartirait de zero et depasserait le quota sans s'en rendre compte.
        assert CreditLedger(path, 400).spent_today() == 6

    def test_refuses_a_call_that_would_exceed_the_budget(self, tmp_path):
        ledger = CreditLedger(tmp_path / "ledger.json", daily_budget=10)
        ledger.record(8)
        ledger.check_affordable(2)  # 8 + 2 = 10, exactement le budget : accepte
        with pytest.raises(CreditBudgetExceededError, match="Budget OpenSky epuise"):
            ledger.check_affordable(3)

    def test_a_corrupted_file_does_not_crash_the_pipeline(self, tmp_path):
        path = tmp_path / "ledger.json"
        path.write_text("{ceci n'est pas du json", encoding="utf-8")
        assert CreditLedger(path, 400).spent_today() == 0


class TestGetStates:
    @respx.mock
    def test_returns_a_typed_snapshot(self, fast_settings):
        respx.get(STATES_URL).mock(return_value=httpx.Response(200, json=snapshot_payload(3)))
        with OpenSkyClient(fast_settings) as client:
            snapshot = client.get_states()

        assert len(snapshot) == 3
        assert snapshot.region == "france"
        assert snapshot.snapshot_ts == 1755441600
        assert snapshot.credits_spent == 3

    @respx.mock
    def test_bounding_box_is_sent_as_query_parameters(self, fast_settings):
        route = respx.get(STATES_URL).mock(
            return_value=httpx.Response(200, json=snapshot_payload())
        )
        with OpenSkyClient(fast_settings) as client:
            client.get_states()

        params = route.calls.last.request.url.params
        france = BOUNDING_BOXES["france"]
        assert float(params["lamin"]) == france.lamin
        assert float(params["lomax"]) == france.lomax
        assert params["extended"] == "1"

    @respx.mock
    def test_world_sends_no_bounding_box(self, fast_settings):
        route = respx.get(STATES_URL).mock(
            return_value=httpx.Response(200, json=snapshot_payload())
        )
        world = fast_settings.model_copy(update={"region": "world"})
        with OpenSkyClient(world) as client:
            client.get_states()

        assert "lamin" not in route.calls.last.request.url.params

    @respx.mock
    def test_an_empty_response_is_not_an_error(self, fast_settings):
        respx.get(STATES_URL).mock(
            return_value=httpx.Response(200, json={"time": 1755441600, "states": None})
        )
        with OpenSkyClient(fast_settings) as client:
            snapshot = client.get_states()

        # OpenSky renvoie `null` plutot qu'une liste vide quand aucun
        # aeronef n'est visible : ce cas se produit reellement la nuit.
        assert len(snapshot) == 0

    @respx.mock
    def test_credits_are_recorded_after_a_successful_call(self, fast_settings):
        respx.get(STATES_URL).mock(return_value=httpx.Response(200, json=snapshot_payload()))
        with OpenSkyClient(fast_settings) as client:
            client.get_states()
            assert client.ledger.spent_today() == 3

    @respx.mock
    def test_the_call_is_blocked_once_the_budget_is_spent(self, fast_settings):
        respx.get(STATES_URL).mock(return_value=httpx.Response(200, json=snapshot_payload()))
        tight = fast_settings.model_copy(update={"daily_credit_budget": 4})

        with OpenSkyClient(tight) as client:
            client.get_states()  # 3 credits consommes
            with pytest.raises(CreditBudgetExceededError):
                client.get_states()  # 3 de plus depasseraient les 4 alloues


class TestResilience:
    @respx.mock
    def test_retries_on_rate_limiting_then_succeeds(self, fast_settings):
        route = respx.get(STATES_URL).mock(
            side_effect=[
                httpx.Response(429),
                httpx.Response(503),
                httpx.Response(200, json=snapshot_payload(2)),
            ]
        )
        with OpenSkyClient(fast_settings) as client:
            snapshot = client.get_states()

        assert len(snapshot) == 2
        assert route.call_count == 3

    @respx.mock
    def test_gives_up_after_the_configured_number_of_attempts(self, fast_settings):
        route = respx.get(STATES_URL).mock(return_value=httpx.Response(503))
        with OpenSkyClient(fast_settings) as client, pytest.raises(Exception, match="503"):
            client.get_states()

        assert route.call_count == fast_settings.max_retries

    @respx.mock
    def test_network_errors_are_retried(self, fast_settings):
        route = respx.get(STATES_URL).mock(
            side_effect=[
                httpx.ConnectTimeout("delai depasse"),
                httpx.Response(200, json=snapshot_payload(1)),
            ]
        )
        with OpenSkyClient(fast_settings) as client:
            assert len(client.get_states()) == 1
        assert route.call_count == 2

    @respx.mock
    def test_no_credit_is_charged_when_every_attempt_fails(self, fast_settings):
        respx.get(STATES_URL).mock(return_value=httpx.Response(503))
        with OpenSkyClient(fast_settings) as client:
            with pytest.raises(Exception, match="503"):
                client.get_states()
            # Le compteur ne doit avancer que sur un appel reellement servi,
            # sinon une panne cote OpenSky consommerait le quota du jour.
            assert client.ledger.spent_today() == 0

    @respx.mock
    def test_a_client_error_is_not_retried(self, fast_settings):
        route = respx.get(STATES_URL).mock(return_value=httpx.Response(404))
        with OpenSkyClient(fast_settings) as client, pytest.raises(httpx.HTTPStatusError):
            client.get_states()

        # Reessayer un 404 ne peut pas aider : echec immediat.
        assert route.call_count == 1
