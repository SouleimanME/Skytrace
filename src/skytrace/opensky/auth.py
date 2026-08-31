"""Authentification OAuth2 (client_credentials) aupres d'OpenSky Network.

Depuis mars 2025 OpenSky a supprime le basic auth : le seul mode
d'identification programmatique est le flow `client_credentials` contre
leur serveur Keycloak. Le jeton obtenu vit environ 30 minutes.

Le mode anonyme reste possible (aucun identifiant) au prix de quotas plus
serres : 400 credits/jour, pas d'historique, resolution 10s au lieu de 5s.
"""

from __future__ import annotations

import time

import httpx

from skytrace.logging_conf import get_logger

logger = get_logger(__name__)

# C'est l'ADRESSE du serveur de jetons, pas un jeton.
TOKEN_URL = (
    "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token"  # noqa: S105
)

#: Marge de securite : on renouvelle le jeton avant son expiration reelle
#: pour eviter qu'il expire pendant le vol de la requete.
_EXPIRY_LEEWAY_SECONDS = 60


class AuthenticationError(RuntimeError):
    """Les identifiants ont ete refuses par le serveur d'autorisation."""


class OpenSkyAuth:
    """Fournit un jeton Bearer valide, en le renouvelant a la demande.

    Utilisable meme sans identifiants : `bearer_token()` renvoie alors
    `None` et l'appelant part en mode anonyme.
    """

    def __init__(
        self,
        client_id: str | None,
        client_secret: str | None,
        *,
        http: httpx.Client | None = None,
        token_url: str = TOKEN_URL,
        timeout: float = 30.0,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._token_url = token_url
        self._timeout = timeout
        self._http = http
        self._owns_http = http is None
        self._access_token: str | None = None
        self._expires_at: float = 0.0

    # -- cycle de vie ------------------------------------------------------
    @property
    def anonymous(self) -> bool:
        return not (self._client_id and self._client_secret)

    def close(self) -> None:
        if self._owns_http and self._http is not None:
            self._http.close()
            self._http = None

    def __enter__(self) -> OpenSkyAuth:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- jeton -------------------------------------------------------------
    def bearer_token(self) -> str | None:
        """Renvoie un jeton valide, ou `None` en mode anonyme."""
        if self.anonymous:
            return None
        if self._access_token and time.monotonic() < self._expires_at:
            return self._access_token
        return self._fetch_token()

    def auth_headers(self) -> dict[str, str]:
        token = self.bearer_token()
        return {"Authorization": f"Bearer {token}"} if token else {}

    def invalidate(self) -> None:
        """Force le renouvellement au prochain appel (utile apres un 401)."""
        self._access_token = None
        self._expires_at = 0.0

    # -- interne -----------------------------------------------------------
    def _http_client(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(timeout=self._timeout)
        return self._http

    def _fetch_token(self) -> str:
        logger.info("Demande d'un nouveau jeton OAuth2 a OpenSky")
        response = self._http_client().post(
            self._token_url,
            data={
                "grant_type": "client_credentials",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if response.status_code in (400, 401, 403):
            raise AuthenticationError(
                "OpenSky a refuse les identifiants "
                f"(HTTP {response.status_code}). Verifier OPENSKY_CLIENT_ID "
                "et OPENSKY_CLIENT_SECRET."
            )
        response.raise_for_status()

        payload = response.json()
        token = payload.get("access_token")
        if not token:
            raise AuthenticationError("Reponse du serveur d'autorisation sans access_token")

        expires_in = float(payload.get("expires_in", 1800))
        self._access_token = token
        self._expires_at = time.monotonic() + max(expires_in - _EXPIRY_LEEWAY_SECONDS, 0)
        logger.info("Jeton obtenu, valable %.0f s", expires_in)
        return token
