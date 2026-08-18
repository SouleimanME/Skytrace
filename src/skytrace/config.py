"""Configuration centralisee du pipeline.

Toute la configuration passe par des variables d'environnement (12-factor),
chargees depuis un fichier `.env` a la racine du projet quand il existe.
Aucun secret n'est jamais ecrit en dur dans le code.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# src/skytrace/config.py -> src/skytrace -> src -> <racine du projet>
PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class BoundingBox:
    """Fenetre geographique interrogee sur l'API OpenSky.

    OpenSky facture ses requetes en "credits" et le tarif depend de la
    surface demandee. Modeliser ca ici permet au client de refuser une
    requete qui ferait exploser le budget journalier.
    """

    name: str
    lamin: float
    lomin: float
    lamax: float
    lomax: float

    @property
    def area_sq_deg(self) -> float:
        return (self.lamax - self.lamin) * (self.lomax - self.lomin)

    @property
    def credit_cost(self) -> int:
        """Cout en credits d'un appel `/states/all` sur cette fenetre.

        Bareme publie par OpenSky (surface en degres carres).
        """
        area = self.area_sq_deg
        if area <= 25:
            return 1
        if area <= 100:
            return 2
        if area <= 400:
            return 3
        return 4

    def as_params(self) -> dict[str, float]:
        return {
            "lamin": self.lamin,
            "lomin": self.lomin,
            "lamax": self.lamax,
            "lomax": self.lomax,
        }

    def contains(self, latitude: float, longitude: float) -> bool:
        return self.lamin <= latitude <= self.lamax and self.lomin <= longitude <= self.lomax


#: Fenetres pre-configurees. `world` n'envoie aucun parametre de bbox.
BOUNDING_BOXES: dict[str, BoundingBox] = {
    "france": BoundingBox("france", lamin=41.0, lomin=-5.5, lamax=51.5, lomax=9.8),
    "benelux": BoundingBox("benelux", lamin=49.4, lomin=2.4, lamax=53.6, lomax=7.3),
    "europe": BoundingBox("europe", lamin=35.0, lomin=-12.0, lamax=62.0, lomax=32.0),
    "world": BoundingBox("world", lamin=-90.0, lomin=-180.0, lamax=90.0, lomax=180.0),
}


class Settings(BaseSettings):
    """Parametres du pipeline, resolus depuis l'environnement."""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    # --- OpenSky ---------------------------------------------------------
    opensky_client_id: str | None = Field(default=None, alias="OPENSKY_CLIENT_ID")
    opensky_client_secret: str | None = Field(default=None, alias="OPENSKY_CLIENT_SECRET")

    region: str = Field(default="france", alias="SKYTRACE_REGION")
    daily_credit_budget: int = Field(default=400, alias="SKYTRACE_DAILY_CREDIT_BUDGET")
    request_timeout: float = Field(default=30.0, alias="SKYTRACE_REQUEST_TIMEOUT")
    max_retries: int = Field(default=4, alias="SKYTRACE_MAX_RETRIES")
    # Multiplicateur de l'attente exponentielle entre deux tentatives.
    # Reglable pour que la suite de tests n'attende pas reellement.
    retry_backoff_seconds: float = Field(default=2.0, alias="SKYTRACE_RETRY_BACKOFF_SECONDS")

    # --- Stockage --------------------------------------------------------
    data_dir: Path | None = Field(default=None, alias="SKYTRACE_DATA_DIR")
    duckdb_path: Path | None = Field(default=None, alias="SKYTRACE_DUCKDB_PATH")

    # --- Divers ----------------------------------------------------------
    log_level: str = Field(default="INFO", alias="SKYTRACE_LOG_LEVEL")

    @field_validator("region")
    @classmethod
    def _known_region(cls, value: str) -> str:
        value = value.strip().lower()
        if value not in BOUNDING_BOXES:
            known = ", ".join(sorted(BOUNDING_BOXES))
            raise ValueError(f"region inconnue : {value!r} (attendu : {known})")
        return value

    @field_validator("data_dir", "duckdb_path", mode="before")
    @classmethod
    def _blank_is_none(cls, value: object) -> object:
        # Une variable d'environnement vide ("SKYTRACE_DATA_DIR=") doit se
        # comporter comme une variable absente, pas comme le chemin "".
        if isinstance(value, str) and not value.strip():
            return None
        return value

    # --- Chemins derives -------------------------------------------------
    @property
    def resolved_data_dir(self) -> Path:
        return (self.data_dir or PROJECT_ROOT / "data").resolve()

    @property
    def resolved_duckdb_path(self) -> Path:
        if self.duckdb_path is not None:
            return self.duckdb_path.resolve()
        return self.resolved_data_dir / "warehouse" / "skytrace.duckdb"

    @property
    def raw_dir(self) -> Path:
        return self.resolved_data_dir / "raw"

    @property
    def states_dir(self) -> Path:
        return self.raw_dir / "opensky_states"

    @property
    def airports_dir(self) -> Path:
        return self.raw_dir / "ourairports"

    @property
    def dbt_project_dir(self) -> Path:
        return PROJECT_ROOT / "dbt" / "skytrace"

    @property
    def bbox(self) -> BoundingBox:
        return BOUNDING_BOXES[self.region]

    @property
    def is_anonymous(self) -> bool:
        """Vrai si aucune identification OAuth2 n'est disponible."""
        return not (self.opensky_client_id and self.opensky_client_secret)

    def ensure_directories(self) -> None:
        for path in (self.states_dir, self.airports_dir, self.resolved_duckdb_path.parent):
            path.mkdir(parents=True, exist_ok=True)

    def dbt_env(self) -> dict[str, str]:
        """Variables d'environnement a injecter dans les commandes dbt.

        dbt ne partage ni le cwd ni le `.env` du process Python : les chemins
        doivent lui etre passes explicitement et en absolu.
        """
        # `as_posix()` est indispensable : DuckDB interprete les motifs glob
        # avec des slash avant, y compris sous Windows. Un chemin en
        # antislash casse silencieusement `read_parquet('.../**/*.parquet')`.
        return {
            "SKYTRACE_DATA_DIR": self.resolved_data_dir.as_posix(),
            "SKYTRACE_DUCKDB_PATH": self.resolved_duckdb_path.as_posix(),
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Instance unique, mise en cache pour tout le process."""
    return Settings()
