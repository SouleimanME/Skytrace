"""Client HTTP OpenSky : quotas, resilience reseau, snapshots typés."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from tenacity import (
    RetryCallState,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from skytrace.config import BoundingBox, Settings
from skytrace.logging_conf import get_logger
from skytrace.opensky.auth import OpenSkyAuth

logger = get_logger(__name__)

BASE_URL = "https://opensky-network.org/api"

#: Codes HTTP pour lesquels un nouvel essai a un sens.
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


class CreditBudgetExceededError(RuntimeError):
    """Le budget de credits journalier serait depasse par cet appel."""


class RetryableHTTPError(RuntimeError):
    """Erreur HTTP transitoire, susceptible de reussir au prochain essai."""


@dataclass(frozen=True)
class StatesSnapshot:
    """Photo instantanee du trafic sur une fenetre geographique."""

    snapshot_ts: int
    region: str
    vectors: list[list[Any]]
    credits_spent: int

    @property
    def snapshot_at(self) -> datetime:
        return datetime.fromtimestamp(self.snapshot_ts, tz=UTC)

    def __len__(self) -> int:
        return len(self.vectors)


class CreditLedger:
    """Compteur de credits OpenSky consommes, persiste sur disque.

    OpenSky facture chaque appel en credits et coupe l'acces au-dela du
    quota. Le compteur vit dans un fichier JSON pour survivre au
    redemarrage du process : sans ca, un ordonnanceur qui relance un
    worker toutes les 15 minutes reperdrait le compte a chaque fois.
    """

    def __init__(self, path: Path, daily_budget: int) -> None:
        self._path = path
        self._daily_budget = daily_budget

    def _today(self) -> str:
        return datetime.now(UTC).strftime("%Y-%m-%d")

    def _read(self) -> dict[str, int]:
        if not self._path.exists():
            return {}
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            logger.warning("Compteur de credits illisible, remise a zero : %s", self._path)
            return {}
        return {str(k): int(v) for k, v in data.items() if isinstance(v, int | float)}

    def spent_today(self) -> int:
        return self._read().get(self._today(), 0)

    def remaining_today(self) -> int:
        return max(self._daily_budget - self.spent_today(), 0)

    def check_affordable(self, cost: int) -> None:
        spent = self.spent_today()
        if spent + cost > self._daily_budget:
            raise CreditBudgetExceededError(
                f"Budget OpenSky epuise : {spent}/{self._daily_budget} credits "
                f"consommes aujourd'hui, cet appel en coute {cost}. "
                "Reduire la frequence, restreindre la zone (SKYTRACE_REGION) "
                "ou fournir des identifiants OAuth2 pour un quota superieur."
            )

    def record(self, cost: int) -> int:
        data = self._read()
        today = self._today()
        data[today] = data.get(today, 0) + cost
        # On ne garde que 30 jours d'historique : le fichier reste minuscule.
        recent = dict(sorted(data.items())[-30:])
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(recent, indent=2), encoding="utf-8")
        return recent[today]


def _log_retry(state: RetryCallState) -> None:
    logger.warning(
        "Appel OpenSky en echec (tentative %d) : %s - nouvel essai",
        state.attempt_number,
        state.outcome.exception() if state.outcome else "?",
    )


class OpenSkyClient:
    """Acces haut niveau a l'API OpenSky.

    Gere pour l'appelant : l'authentification OAuth2 (ou son absence), les
    quotas de credits, les nouvelles tentatives sur erreurs transitoires.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        http: httpx.Client | None = None,
        auth: OpenSkyAuth | None = None,
        ledger: CreditLedger | None = None,
    ) -> None:
        self._settings = settings
        self._http = http or httpx.Client(
            base_url=BASE_URL,
            timeout=settings.request_timeout,
            headers={"User-Agent": "skytrace/0.1 (portfolio data pipeline)"},
        )
        self._auth = auth or OpenSkyAuth(
            settings.opensky_client_id,
            settings.opensky_client_secret,
            timeout=settings.request_timeout,
        )
        self._ledger = ledger or CreditLedger(
            settings.resolved_data_dir / ".credit_ledger.json",
            settings.resolved_credit_budget,
        )

    # -- cycle de vie ------------------------------------------------------
    def close(self) -> None:
        self._http.close()
        self._auth.close()

    def __enter__(self) -> OpenSkyClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @property
    def ledger(self) -> CreditLedger:
        return self._ledger

    # -- API publique ------------------------------------------------------
    def get_states(
        self,
        bbox: BoundingBox | None = None,
        *,
        extended: bool = True,
        at: int | None = None,
    ) -> StatesSnapshot:
        """Recupere l'etat des avions de la fenetre, maintenant ou dans le passe.

        `extended=True` ajoute la categorie d'aeronef (gros porteur, planeur,
        helicoptere...) sans surcout en credits.

        `at` est un instant Unix passe. Il sert au RATTRAPAGE DES TROUS : quand
        l'ordonnanceur se tait pendant deux jours, la serie garde un vide que
        rien ne comblait, et les positions ADS-B de ces heures-la etaient
        perdues pour toujours.

        Attention : ce parametre exige un compte OpenSky. Sans identifiants,
        l'API repond `403 Authenticate to get historical data`. L'appelant doit
        donc verifier `anonymous` avant de s'en servir, et le message d'erreur
        doit dire cela plutot que "acces refuse".
        """
        bbox = bbox or self._settings.bbox
        cost = bbox.credit_cost
        self._ledger.check_affordable(cost)

        params: dict[str, Any] = {"extended": 1 if extended else 0}
        if bbox.name != "world":
            params.update(bbox.as_params())
        if at is not None:
            params["time"] = int(at)

        payload = self._get_json("/states/all", params)

        total = self._ledger.record(cost)
        vectors = payload.get("states") or []
        snapshot_ts = int(payload.get("time") or datetime.now(UTC).timestamp())

        logger.info(
            "Snapshot %s%s : %d aeronefs | %d credits (cumul du jour : %d/%d)",
            bbox.name,
            "" if at is None else f" a {datetime.fromtimestamp(at, tz=UTC):%Y-%m-%d %H:%M} UTC",
            len(vectors),
            cost,
            total,
            self._settings.resolved_credit_budget,
        )
        return StatesSnapshot(
            snapshot_ts=snapshot_ts,
            region=bbox.name,
            vectors=vectors,
            credits_spent=cost,
        )

    @property
    def anonymous(self) -> bool:
        """Vrai si aucun identifiant OpenSky n'est configure.

        Determine ce que le client peut faire : sans compte, l'historique est
        refuse et le budget quotidien est dix fois plus bas.
        """
        return self._auth.anonymous

    # -- interne -----------------------------------------------------------
    def _get_json(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        @retry(
            retry=retry_if_exception_type((RetryableHTTPError, httpx.TransportError)),
            stop=stop_after_attempt(self._settings.max_retries),
            wait=wait_exponential(multiplier=self._settings.retry_backoff_seconds, max=60),
            before_sleep=_log_retry,
            reraise=True,
        )
        def _call() -> dict[str, Any]:
            response = self._http.get(path, params=params, headers=self._auth.auth_headers())

            if response.status_code == 401 and not self._auth.anonymous:
                # Jeton expire cote serveur : on le jette et on retente.
                self._auth.invalidate()
                raise RetryableHTTPError("401 : jeton OAuth2 rejete, renouvellement")

            if response.status_code in _RETRYABLE_STATUS:
                raise RetryableHTTPError(f"HTTP {response.status_code} sur {path}")

            response.raise_for_status()
            return response.json()

        return _call()
