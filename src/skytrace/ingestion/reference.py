"""Ingestion du referentiel aeroports (OurAirports).

OpenSky ne renvoie que des positions : sans referentiel, impossible de dire
qu'un avion a 300 m d'altitude survole Roissy. OurAirports fournit ~80 000
aerodromes mondiaux avec coordonnees et codes IATA/OACI, en CSV libre.

C'est une dimension a evolution lente : un rafraichissement quotidien
suffit largement, la ou les positions sont ingerees toutes les 15 minutes.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

import httpx
import pyarrow as pa
import pyarrow.parquet as pq
from pyarrow import csv as pa_csv

from skytrace.config import Settings, get_settings
from skytrace.logging_conf import get_logger

logger = get_logger(__name__)

AIRPORTS_URL = "https://davidmegginson.github.io/ourairports-data/airports.csv"


@dataclass(frozen=True)
class IngestedReference:
    path: Path
    rows: int
    columns: list[str]
    checksum: str
    downloaded_at: datetime


def _string_convert_options(header_line: str) -> pa_csv.ConvertOptions:
    """Force toutes les colonnes en texte.

    L'inference de types d'Arrow n'est pas stable d'une version du fichier
    a l'autre (une colonne vide un jour devient `null`, numerique le
    lendemain), ce qui casserait la lecture. La couche bronze reste donc
    fidele au CSV, et c'est dbt qui typera en staging.
    """
    columns = [name.strip().strip('"') for name in header_line.split(",")]
    return pa_csv.ConvertOptions(
        column_types=dict.fromkeys(columns, pa.string()),
        strings_can_be_null=True,
    )


def ingest_airports(
    settings: Settings | None = None,
    *,
    url: str = AIRPORTS_URL,
    http: httpx.Client | None = None,
) -> IngestedReference:
    """Telecharge le referentiel aeroports et l'ecrit en Parquet."""
    settings = settings or get_settings()
    settings.ensure_directories()

    owns_http = http is None
    http = http or httpx.Client(timeout=settings.request_timeout, follow_redirects=True)
    try:
        logger.info("Telechargement du referentiel aeroports : %s", url)
        response = http.get(url)
        response.raise_for_status()
        payload = response.content
    finally:
        if owns_http:
            http.close()

    checksum = hashlib.sha256(payload).hexdigest()
    header_line = payload.split(b"\n", 1)[0].decode("utf-8-sig")

    table = pa_csv.read_csv(
        BytesIO(payload),
        convert_options=_string_convert_options(header_line),
    )

    downloaded_at = datetime.now(UTC)
    destination = settings.airports_dir / "airports.parquet"
    metadata_path = settings.airports_dir / "_ingestion_metadata.json"

    pq.write_table(table, destination, compression="zstd")
    metadata_path.write_text(
        json.dumps(
            {
                "source_url": url,
                "downloaded_at": downloaded_at.isoformat(),
                "sha256": checksum,
                "rows": table.num_rows,
                "columns": table.column_names,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    logger.info(
        "Referentiel ecrit : %d aeroports, %d colonnes -> %s",
        table.num_rows,
        table.num_columns,
        destination,
    )
    return IngestedReference(
        path=destination,
        rows=table.num_rows,
        columns=list(table.column_names),
        checksum=checksum,
        downloaded_at=downloaded_at,
    )
