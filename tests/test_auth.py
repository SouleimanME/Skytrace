"""Tests de l'authentification OAuth2 (aucun appel reseau reel)."""

from __future__ import annotations

import httpx
import pytest
import respx

from skytrace.opensky.auth import TOKEN_URL, AuthenticationError, OpenSkyAuth


@pytest.fixture
def auth() -> OpenSkyAuth:
    return OpenSkyAuth("un-client", "un-secret")


class TestAnonymousMode:
    def test_no_credentials_means_no_token(self):
        anonymous = OpenSkyAuth(None, None)
        assert anonymous.anonymous
        assert anonymous.bearer_token() is None
        assert anonymous.auth_headers() == {}

    def test_missing_secret_falls_back_to_anonymous(self):
        assert OpenSkyAuth("un-client", None).anonymous


class TestTokenRetrieval:
    @respx.mock
    def test_token_is_requested_and_returned(self, auth):
        route = respx.post(TOKEN_URL).mock(
            return_value=httpx.Response(200, json={"access_token": "jeton-abc", "expires_in": 1800})
        )
        assert auth.bearer_token() == "jeton-abc"
        assert auth.auth_headers() == {"Authorization": "Bearer jeton-abc"}
        assert route.called

    @respx.mock
    def test_token_is_cached_between_calls(self, auth):
        route = respx.post(TOKEN_URL).mock(
            return_value=httpx.Response(200, json={"access_token": "jeton-abc", "expires_in": 1800})
        )
        for _ in range(5):
            auth.bearer_token()

        # Un jeton vit 30 minutes : le redemander a chaque appel API
        # gaspillerait un aller-retour reseau sur chaque snapshot.
        assert route.call_count == 1

    @respx.mock
    def test_invalidate_forces_a_new_token(self, auth):
        route = respx.post(TOKEN_URL).mock(
            return_value=httpx.Response(200, json={"access_token": "jeton-abc", "expires_in": 1800})
        )
        auth.bearer_token()
        auth.invalidate()
        auth.bearer_token()
        assert route.call_count == 2

    @respx.mock
    def test_a_token_about_to_expire_is_renewed(self, auth):
        # `expires_in` inferieur a la marge de securite : le jeton doit
        # etre considere comme deja perime.
        route = respx.post(TOKEN_URL).mock(
            return_value=httpx.Response(200, json={"access_token": "jeton-court", "expires_in": 10})
        )
        auth.bearer_token()
        auth.bearer_token()
        assert route.call_count == 2


class TestErrorHandling:
    @respx.mock
    @pytest.mark.parametrize("status", [400, 401, 403])
    def test_rejected_credentials_raise_a_clear_error(self, auth, status):
        respx.post(TOKEN_URL).mock(return_value=httpx.Response(status, json={}))
        with pytest.raises(AuthenticationError, match="OPENSKY_CLIENT_ID"):
            auth.bearer_token()

    @respx.mock
    def test_response_without_token_is_rejected(self, auth):
        respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"token_type": "Bearer"}))
        with pytest.raises(AuthenticationError, match="access_token"):
            auth.bearer_token()

    @respx.mock
    def test_server_error_is_propagated(self, auth):
        respx.post(TOKEN_URL).mock(return_value=httpx.Response(503, json={}))
        with pytest.raises(httpx.HTTPStatusError):
            auth.bearer_token()
