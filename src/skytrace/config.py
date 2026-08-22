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
    # Retention du lac : les snapshots plus vieux sont purges pour borner le
    # stockage (rester sous les 10 Go gratuits de R2). 180 j = ~6 mois
    # d'historique, largement sous le palier gratuit meme en zone monde.
    retention_days: int = Field(default=180, alias="SKYTRACE_RETENTION_DAYS")
    request_timeout: float = Field(default=30.0, alias="SKYTRACE_REQUEST_TIMEOUT")
    max_retries: int = Field(default=4, alias="SKYTRACE_MAX_RETRIES")
    # Multiplicateur de l'attente exponentielle entre deux tentatives.
    # Reglable pour que la suite de tests n'attende pas reellement.
    retry_backoff_seconds: float = Field(default=2.0, alias="SKYTRACE_RETRY_BACKOFF_SECONDS")

    # --- Stockage --------------------------------------------------------
    data_dir: Path | None = Field(default=None, alias="SKYTRACE_DATA_DIR")
    duckdb_path: Path | None = Field(default=None, alias="SKYTRACE_DUCKDB_PATH")

    # --- Stockage objet R2 (optionnel) -----------------------------------
    # Renseignes -> le lac brut vit sur Cloudflare R2 (s3://) au lieu du
    # disque local. L'entrepot DuckDB, lui, reste local (reconstruit depuis
    # R2). Absents -> comportement local inchange.
    r2_account_id: str | None = Field(default=None, alias="SKYTRACE_R2_ACCOUNT_ID")
    r2_bucket: str | None = Field(default=None, alias="SKYTRACE_R2_BUCKET")
    r2_access_key_id: str | None = Field(default=None, alias="SKYTRACE_R2_ACCESS_KEY_ID")
    r2_secret_access_key: str | None = Field(default=None, alias="SKYTRACE_R2_SECRET_ACCESS_KEY")

    # --- Divers ----------------------------------------------------------
    log_level: str = Field(default="INFO", alias="SKYTRACE_LOG_LEVEL")

    # -- Stockage objet ---------------------------------------------------
    @property
    def uses_r2(self) -> bool:
        return bool(
            self.r2_account_id
            and self.r2_bucket
            and self.r2_access_key_id
            and self.r2_secret_access_key
        )

    @property
    def r2_endpoint(self) -> str | None:
        """Endpoint S3-compatible de R2 (sans schema)."""
        return f"{self.r2_account_id}.r2.cloudflarestorage.com" if self.r2_account_id else None

    @property
    def lake_uri(self) -> str:
        """Racine du lac brut, pour la lecture (dbt / DuckDB).

        `s3://bucket/raw` en mode R2, chemin local en mode disque.
        """
        if self.uses_r2:
            return f"s3://{self.r2_bucket}/raw"
        return self.raw_dir.as_posix()

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
    def air_quality_dir(self) -> Path:
        return self.raw_dir / "open_meteo_air_quality"

    @property
    def aircraft_db_dir(self) -> Path:
        return self.raw_dir / "opensky_aircraft_db"

    @property
    def airlines_dir(self) -> Path:
        return self.raw_dir / "openflights_airlines"

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
        for path in (
            self.states_dir,
            self.airports_dir,
            self.air_quality_dir,
            self.aircraft_db_dir,
            self.airlines_dir,
            self.resolved_duckdb_path.parent,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def dbt_env(self) -> dict[str, str]:
        """Variables d'environnement a injecter dans les commandes dbt.

        dbt ne partage ni le cwd ni le `.env` du process Python : les chemins
        doivent lui etre passes explicitement et en absolu.
        """
        # `as_posix()` est indispensable : DuckDB interprete les motifs glob
        # avec des slash avant, y compris sous Windows. Un chemin en
        # antislash casse silencieusement `read_parquet('.../**/*.parquet')`.
        env = {
            "SKYTRACE_DATA_DIR": self.resolved_data_dir.as_posix(),
            "SKYTRACE_DUCKDB_PATH": self.resolved_duckdb_path.as_posix(),
            # Racine du lac (local ou s3://) : les sources dbt lisent ici.
            "SKYTRACE_LAKE_URI": self.lake_uri,
        }
        if self.uses_r2:
            env.update(
                {
                    "SKYTRACE_R2_ENDPOINT": self.r2_endpoint or "",
                    "SKYTRACE_R2_ACCESS_KEY_ID": self.r2_access_key_id or "",
                    "SKYTRACE_R2_SECRET_ACCESS_KEY": self.r2_secret_access_key or "",
                }
            )
        return env

    @property
    def dbt_target(self) -> str:
        """Cible dbt : `r2` quand le lac est sur R2, `dev` en local."""
        return "r2" if self.uses_r2 else "dev"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Instance unique, mise en cache pour tout le process."""
    return Settings()
