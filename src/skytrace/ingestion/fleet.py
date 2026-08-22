"""Ingestion des referentiels flotte : base aeronefs et compagnies.

Troisieme source du projet. Elle enrichit la dimension aeronef avec des
metadonnees reelles (type, constructeur, operateur) et permet d'identifier
la compagnie d'un vol a partir de l'indicatif.

Deux referentiels, tous deux gratuits et sans authentification :

  * Base aeronefs OpenSky (~500 000 appareils) : pour chaque adresse OACI
    24 bits, le type d'avion, le constructeur, le modele, l'operateur. Le
    fichier brut fait ~95 Mo ; on ne conserve que les colonnes utiles et on
    ecarte les lignes sans information exploitable, ce qui reduit fortement
    le volume ecrit.

  * OpenFlights airlines.dat : code OACI de compagnie -> nom et pays. Le
    prefixe de trois lettres de l'indicatif (AFR, BAW, DLH...) est ce code
    OACI, ce qui permet de rattacher chaque vol a sa compagnie.

Ce sont des dimensions a evolution lente : un rafraichissement mensuel
suffit.
"""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO

import httpx
import pyarrow as pa
from pyarrow import csv as pa_csv

from skytrace.config import Settings, get_settings
from skytrace.logging_conf import get_logger
from skytrace.storage import write_parquet

logger = get_logger(__name__)

AIRCRAFT_DB_URL = "https://opensky-network.org/datasets/metadata/aircraftDatabase.csv"
AIRLINES_URL = "https://raw.githubusercontent.com/jpatokal/openflights/master/data/airlines.dat"

#: Colonnes conservees de la base aeronefs (les autres sont ignorees).
AIRCRAFT_DB_COLUMNS = (
    "icao24",
    "registration",
    "typecode",
    "manufacturername",
    "model",
    "operator",
    "operatoricao",
    "owner",
    "built",
    "categoryDescription",
)

#: Colonnes de airlines.dat (fichier sans en-tete).
AIRLINES_COLUMNS = (
    "airline_id",
    "name",
    "alias",
    "iata",
    "icao",
    "callsign",
    "country",
    "active",
)


@dataclass(frozen=True)
class IngestedReference:
    uri: str
    rows: int

    @property
    def path(self):
        from pathlib import Path

        return Path(self.uri)


def _download(url: str, http: httpx.Client, timeout: float) -> bytes:
    response = http.get(url, timeout=timeout, follow_redirects=True)
    response.raise_for_status()
    return response.content


def ingest_aircraft_db(
    settings: Settings | None = None,
    *,
    url: str = AIRCRAFT_DB_URL,
    http: httpx.Client | None = None,
) -> IngestedReference:
    """Telecharge la base aeronefs OpenSky, filtree et reduite en colonnes."""
    settings = settings or get_settings()
    settings.ensure_directories()

    owns_http = http is None
    http = http or httpx.Client(timeout=settings.request_timeout, follow_redirects=True)
    try:
        logger.info("Telechargement de la base aeronefs OpenSky (~95 Mo)...")
        payload = _download(url, http, settings.request_timeout * 6)
    finally:
        if owns_http:
            http.close()

    # Lecture en ne gardant que les colonnes utiles, toutes en texte : la
    # couche bronze reste fidele a la source, dbt typera en staging.
    table = pa_csv.read_csv(
        BytesIO(payload),
        convert_options=pa_csv.ConvertOptions(
            include_columns=list(AIRCRAFT_DB_COLUMNS),
            column_types=dict.fromkeys(AIRCRAFT_DB_COLUMNS, pa.string()),
            strings_can_be_null=True,
        ),
    )

    # On ecarte les lignes sans identifiant, et celles totalement vides de
    # metadonnees (la base contient beaucoup d'entrees fantomes).
    import pyarrow.compute as pc

    icao = pc.utf8_trim_whitespace(table["icao24"])
    has_id = pc.and_(pc.is_valid(icao), pc.not_equal(icao, ""))
    has_info = pc.or_(
        pc.or_(
            pc.not_equal(pc.coalesce(table["typecode"], ""), ""),
            pc.not_equal(pc.coalesce(table["manufacturername"], ""), ""),
        ),
        pc.not_equal(pc.coalesce(table["operator"], ""), ""),
    )
    table = table.filter(pc.and_(has_id, has_info))

    result = write_parquet("opensky_aircraft_db/aircraft_database.parquet", table, settings)
    logger.info(
        "Base aeronefs ecrite : %d appareils utiles -> %s (%.1f Mo)",
        table.num_rows,
        result.uri,
        result.size_bytes / 1_048_576,
    )
    return IngestedReference(uri=result.uri, rows=table.num_rows)


def ingest_airlines(
    settings: Settings | None = None,
    *,
    url: str = AIRLINES_URL,
    http: httpx.Client | None = None,
) -> IngestedReference:
    """Telecharge le referentiel compagnies OpenFlights (airlines.dat)."""
    settings = settings or get_settings()
    settings.ensure_directories()

    owns_http = http is None
    http = http or httpx.Client(timeout=settings.request_timeout, follow_redirects=True)
    try:
        logger.info("Telechargement du referentiel compagnies OpenFlights...")
        payload = _download(url, http, settings.request_timeout)
    finally:
        if owns_http:
            http.close()

    # Fichier sans en-tete, valeurs nulles notees "\N".
    table = pa_csv.read_csv(
        BytesIO(payload),
        read_options=pa_csv.ReadOptions(column_names=list(AIRLINES_COLUMNS)),
        parse_options=pa_csv.ParseOptions(newlines_in_values=False),
        convert_options=pa_csv.ConvertOptions(
            column_types=dict.fromkeys(AIRLINES_COLUMNS, pa.string()),
            null_values=["\\N", ""],
            strings_can_be_null=True,
        ),
    )

    result = write_parquet("openflights_airlines/airlines.parquet", table, settings)
    logger.info("Referentiel compagnies ecrit : %d lignes -> %s", table.num_rows, result.uri)
    return IngestedReference(uri=result.uri, rows=table.num_rows)
