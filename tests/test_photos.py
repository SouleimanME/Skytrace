"""Tests de la recuperation de photos d'aeronefs (reseau simule)."""

from __future__ import annotations

import httpx
import respx

from skytrace.photos import USER_AGENT, fetch_photo, looks_military

ICAO = "780570"
URL = f"https://api.planespotters.net/pub/photos/hex/{ICAO}"

PAYLOAD = {
    "photos": [
        {
            "id": "1859596",
            "thumbnail_large": {"src": "https://t.plnspttrs.net/12601/1859596_x_280.jpg"},
            "photographer": "DimageA1",
            "link": "https://www.planespotters.net/photo/1859596/b-6570",
        }
    ]
}


class TestFetchPhoto:
    @respx.mock
    def test_returns_photo_with_credit(self):
        respx.get(URL).mock(return_value=httpx.Response(200, json=PAYLOAD))
        photo = fetch_photo(ICAO)

        assert photo is not None
        assert photo.thumbnail_url.endswith("_280.jpg")
        # Le credit accompagne toujours l'image : les photos ont un auteur.
        assert photo.photographer == "DimageA1"
        assert photo.page_url.startswith("https://www.planespotters.net/")

    @respx.mock
    def test_sends_contact_url_in_user_agent(self):
        # L'API refuse (403) tout appel dont le User-Agent ne permet pas de
        # joindre l'appelant : le contact fait partie du contrat d'usage.
        route = respx.get(URL).mock(return_value=httpx.Response(200, json=PAYLOAD))
        fetch_photo(ICAO)

        sent = route.calls.last.request.headers["user-agent"]
        assert sent == USER_AGENT
        assert "+http" in sent

    @respx.mock
    def test_absence_of_photo_is_not_an_error(self):
        respx.get(URL).mock(return_value=httpx.Response(200, json={"photos": []}))
        assert fetch_photo(ICAO) is None

    @respx.mock
    def test_http_error_degrades_silently(self):
        # Une vignette manquante ne doit jamais empecher l'affichage de la fiche.
        respx.get(URL).mock(return_value=httpx.Response(503))
        assert fetch_photo(ICAO) is None

    @respx.mock
    def test_network_failure_degrades_silently(self):
        respx.get(URL).mock(side_effect=httpx.ConnectTimeout("delai depasse"))
        assert fetch_photo(ICAO) is None

    def test_empty_identifier_short_circuits(self):
        # Aucun appel reseau ne doit partir sans identifiant.
        assert fetch_photo("") is None


class TestLooksMilitary:
    def test_detects_common_operators(self):
        assert looks_military("United States Air Force")
        assert looks_military("Royal Navy")
        assert looks_military(None, "French Army")

    def test_civil_operator_is_not_military(self):
        assert not looks_military("Air France")
        assert not looks_military("Shenzhen Airlines", "BOC Aviation")

    def test_missing_operator_is_not_military(self):
        assert not looks_military(None, None)

    def test_pandas_missing_value_is_not_military(self):
        # Un champ vide lu dans un tableau pandas vaut NaN, pas None - et NaN
        # est "vrai" au sens booleen. La fonction doit le traiter comme absent
        # plutot que d'echouer a la concatenation.
        assert not looks_military(float("nan"), float("nan"))
        assert looks_military(float("nan"), "Royal Air Force")
