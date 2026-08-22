"""Ingestion de la qualite de l'air autour des aeroports (Open-Meteo).

Deuxieme source du projet, choisie pour permettre une vraie question
analytique : le trafic aerien horaire autour d'un aeroport se lit-il dans
les polluants mesures au sol a proximite ?

Open-Meteo Air Quality est gratuite et sans cle. On interroge, pour chaque
aeroport suivi, les concentrations horaires de NO2, PM2.5, PM10 et ozone a
ses coordonnees, sur une fenetre glissante qui recouvre la periode de
collecte du trafic.

C'est une dimension a rafraichir une fois par jour : les valeurs passees ne
changent plus, seule la fenetre recente s'etend.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
import pyarrow as pa

from skytrace.config import Settings, get_settings
from skytrace.logging_conf import get_logger
from skytrace.storage import write_parquet

logger = get_logger(__name__)

AIR_QUALITY_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"

#: Polluants demandes a l'API, dans l'ordre.
POLLUTANTS = ("nitrogen_dioxide", "pm2_5", "pm10", "ozone")


@dataclass(frozen=True)
class Airport:
    """Aeroport suivi, avec ses coordonnees pour la requete Open-Meteo.

    L'`icao` est la cle de jointure avec `dim_airport` cote entrepot.
    """

    icao: str
    iata: str
    latitude: float
    longitude: float


#: Grands aeroports couverts par la fenetre France (rectangle qui attrape
#: aussi Geneve, Bale, Barcelone, Milan, Zurich, Francfort, Gatwick). Ce sont
#: ceux qui concentrent l'essentiel de l'activite observee.
TRACKED_AIRPORTS: tuple[Airport, ...] = (
    Airport("LFPG", "CDG", 49.0128, 2.5500),
    Airport("LFPO", "ORY", 48.7233, 2.3794),
    Airport("LFLL", "LYS", 45.7256, 5.0811),
    Airport("LFMN", "NCE", 43.6584, 7.2159),
    Airport("LFML", "MRS", 43.4393, 5.2214),
    Airport("LFBO", "TLS", 43.6291, 1.3638),
    Airport("LFBD", "BOD", 44.8283, -0.7156),
    Airport("LFRS", "NTE", 47.1532, -1.6107),
    Airport("LFSB", "BSL", 47.5896, 7.5299),
    Airport("LSZH", "ZRH", 47.4647, 8.5492),
    Airport("LEBL", "BCN", 41.2971, 2.0785),
    Airport("LIMC", "MXP", 45.6306, 8.7281),
    Airport("EGKK", "LGW", 51.1481, -0.1903),
    Airport("EDDF", "FRA", 50.0379, 8.5622),
)

AIR_QUALITY_SCHEMA = pa.schema(
    [
        pa.field("airport_icao", pa.string(), nullable=False),
        pa.field("airport_iata", pa.string()),
        pa.field("latitude", pa.float64()),
        pa.field("longitude", pa.float64()),
        pa.field("measured_at", pa.timestamp("us", tz="UTC"), nullable=False),
        pa.field("nitrogen_dioxide", pa.float64()),
        pa.field("pm2_5", pa.float64()),
        pa.field("pm10", pa.float64()),
        pa.field("ozone", pa.float64()),
        pa.field("ingested_at", pa.timestamp("us", tz="UTC"), nullable=False),
        pa.field("source", pa.string(), nullable=False),
    ]
)


@dataclass(frozen=True)
class IngestedAirQuality:
    uri: str
    rows: int
    airports: int
    start_date: str
    end_date: str

    @property
    def path(self):
        from pathlib import Path

        return Path(self.uri)


def _parse_hour(value: str) -> datetime:
    # Open-Meteo renvoie "2026-08-19T12:00" ; le fuseau UTC est impose par la
    # requete, on l'attache explicitement.
    return datetime.fromisoformat(value).replace(tzinfo=UTC)


def _fetch_airport(
    http: httpx.Client,
    airport: Airport,
    start_date: str,
    end_date: str,
) -> list[dict]:
    """Recupere les series horaires d'un aeroport et les met a plat."""
    response = http.get(
        AIR_QUALITY_URL,
        params={
            "latitude": airport.latitude,
            "longitude": airport.longitude,
            "hourly": ",".join(POLLUTANTS),
            "start_date": start_date,
            "end_date": end_date,
            "timezone": "UTC",
        },
    )
    response.raise_for_status()
    hourly = response.json().get("hourly") or {}
    times = hourly.get("time") or []

    rows: list[dict] = []
    for index, ts in enumerate(times):
        rows.append(
            {
                "airport_icao": airport.icao,
                "airport_iata": airport.iata,
                "latitude": airport.latitude,
                "longitude": airport.longitude,
                "measured_at": _parse_hour(ts),
                "nitrogen_dioxide": _at(hourly, "nitrogen_dioxide", index),
                "pm2_5": _at(hourly, "pm2_5", index),
                "pm10": _at(hourly, "pm10", index),
                "ozone": _at(hourly, "ozone", index),
            }
        )
    return rows


def _at(hourly: dict, key: str, index: int) -> float | None:
    series = hourly.get(key) or []
    if index < len(series) and series[index] is not None:
        return float(series[index])
    return None


def ingest_air_quality(
    settings: Settings | None = None,
    *,
    lookback_days: int = 10,
    airports: tuple[Airport, ...] = TRACKED_AIRPORTS,
    http: httpx.Client | None = None,
    end_date: datetime | None = None,
) -> IngestedAirQuality:
    """Recupere la qualite de l'air recente pour les aeroports suivis.

    La fenetre glissante (`lookback_days`) recouvre largement la periode de
    collecte du trafic. L'ecriture ecrase le fichier bronze : les valeurs
    passees etant stables, un rafraichissement complet de la fenetre est plus
    simple qu'une accumulation incrementale, et reste peu volumineux.
    """
    settings = settings or get_settings()
    settings.ensure_directories()

    end = (end_date or datetime.now(UTC)).date()
    start = end - timedelta(days=lookback_days)
    start_str, end_str = start.isoformat(), end.isoformat()

    owns_http = http is None
    http = http or httpx.Client(timeout=settings.request_timeout)
    ingested_at = datetime.now(UTC)
    try:
        all_rows: list[dict] = []
        for airport in airports:
            rows = _fetch_airport(http, airport, start_str, end_str)
            logger.info("Qualite de l'air %s : %d heures", airport.icao, len(rows))
            all_rows.extend(rows)
    finally:
        if owns_http:
            http.close()

    for row in all_rows:
        row["ingested_at"] = ingested_at
        row["source"] = "open-meteo/air-quality"

    table = pa.Table.from_pylist(all_rows, schema=AIR_QUALITY_SCHEMA)
    result = write_parquet("open_meteo_air_quality/air_quality.parquet", table, settings)

    logger.info(
        "Qualite de l'air ecrite : %d lignes, %d aeroports (%s -> %s) -> %s",
        table.num_rows,
        len(airports),
        start_str,
        end_str,
        result.uri,
    )
    return IngestedAirQuality(
        uri=result.uri,
        rows=table.num_rows,
        airports=len(airports),
        start_date=start_str,
        end_date=end_str,
    )
