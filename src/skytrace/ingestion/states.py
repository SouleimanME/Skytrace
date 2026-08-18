"""Ingestion des positions d'aeronefs vers la couche bronze.

Chaque appel a l'API produit un fichier Parquet immuable, range dans une
arborescence partitionnee facon Hive :

    data/raw/opensky_states/ingest_date=2026-08-17/ingest_hour=14/states_1755441600.parquet

Pourquoi ce decoupage :
  * `ingest_date` / `ingest_hour` permettent a DuckDB d'elaguer les
    partitions (`WHERE ingest_date = ...` ne lit que les fichiers utiles) ;
  * un fichier par snapshot rend l'ingestion idempotente et rejouable :
    relancer un snapshot deja ecrit ne duplique rien, il ecrase le meme nom ;
  * Parquet + compression zstd divise le volume par ~10 face a du JSON brut.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pyarrow.parquet as pq

from skytrace.config import Settings, get_settings
from skytrace.logging_conf import get_logger
from skytrace.opensky.client import OpenSkyClient, StatesSnapshot
from skytrace.opensky.schema import states_to_arrow

logger = get_logger(__name__)


@dataclass(frozen=True)
class IngestedSnapshot:
    """Resultat d'une ingestion, retourne a l'ordonnanceur."""

    path: Path
    rows: int
    snapshot_ts: int
    region: str
    credits_spent: int

    @property
    def snapshot_at(self) -> datetime:
        return datetime.fromtimestamp(self.snapshot_ts, tz=UTC)

    @property
    def size_bytes(self) -> int:
        return self.path.stat().st_size if self.path.exists() else 0


def partition_path(root: Path, snapshot_ts: int) -> Path:
    """Chemin du fichier Parquet pour un horodatage donne."""
    moment = datetime.fromtimestamp(snapshot_ts, tz=UTC)
    return (
        root
        / f"ingest_date={moment:%Y-%m-%d}"
        / f"ingest_hour={moment:%H}"
        / f"states_{snapshot_ts}.parquet"
    )


def write_snapshot(snapshot: StatesSnapshot, root: Path) -> IngestedSnapshot:
    """Serialise un snapshot en Parquet et renvoie ses metadonnees."""
    table = states_to_arrow(
        snapshot.vectors,
        snapshot_ts=snapshot.snapshot_ts,
        region=snapshot.region,
    )
    destination = partition_path(root, snapshot.snapshot_ts)
    destination.parent.mkdir(parents=True, exist_ok=True)

    pq.write_table(table, destination, compression="zstd")
    logger.info(
        "Ecrit %d lignes -> %s (%.1f Ko)",
        table.num_rows,
        destination,
        destination.stat().st_size / 1024,
    )
    return IngestedSnapshot(
        path=destination,
        rows=table.num_rows,
        snapshot_ts=snapshot.snapshot_ts,
        region=snapshot.region,
        credits_spent=snapshot.credits_spent,
    )


def ingest_states(
    settings: Settings | None = None,
    *,
    client: OpenSkyClient | None = None,
) -> IngestedSnapshot:
    """Recupere un snapshot du trafic et l'ecrit dans la couche bronze."""
    settings = settings or get_settings()
    settings.ensure_directories()

    owns_client = client is None
    client = client or OpenSkyClient(settings)
    try:
        snapshot = client.get_states()
    finally:
        if owns_client:
            client.close()

    if not snapshot.vectors:
        logger.warning(
            "Aucun aeronef renvoye pour la zone %s : snapshot vide, rien n'est ecrit sur disque.",
            snapshot.region,
        )

    return write_snapshot(snapshot, settings.states_dir)
