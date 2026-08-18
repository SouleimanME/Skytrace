"""Schema des "state vectors" OpenSky et conversion vers Arrow.

L'API renvoie chaque avion sous forme de tableau positionnel non nomme :
`["3c6444", "DLH9LF  ", "Germany", 1458564120, ...]`. Ce module est le seul
endroit du projet qui connait ces indices. Tout le reste manipule des
colonnes nommees.

Principe de la couche bronze : on conserve les champs tels que fournis par
la source, sans nettoyage. Le nettoyage appartient a la couche staging dbt,
ce qui permet de rejouer une transformation sans re-appeler l'API.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pyarrow as pa

#: Nom des colonnes dans l'ordre du tableau positionnel renvoye par OpenSky.
#: `category` (index 17) n'est present que si la requete passe `extended=1`.
STATE_VECTOR_FIELDS: tuple[str, ...] = (
    "icao24",
    "callsign",
    "origin_country",
    "time_position",
    "last_contact",
    "longitude",
    "latitude",
    "baro_altitude",
    "on_ground",
    "velocity",
    "true_track",
    "vertical_rate",
    "sensors",
    "geo_altitude",
    "squawk",
    "spi",
    "position_source",
    "category",
)

#: Schema Arrow de la couche bronze. Types explicites : laisser Arrow inferer
#: produit des schemas incoherents d'un fichier a l'autre des qu'une colonne
#: est entierement nulle sur un snapshot, ce qui casse la lecture globale.
STATES_SCHEMA = pa.schema(
    [
        # --- champs sources ---
        pa.field("icao24", pa.string(), nullable=False),
        pa.field("callsign", pa.string()),
        pa.field("origin_country", pa.string()),
        pa.field("time_position", pa.int64()),
        pa.field("last_contact", pa.int64()),
        pa.field("longitude", pa.float64()),
        pa.field("latitude", pa.float64()),
        pa.field("baro_altitude", pa.float64()),
        pa.field("on_ground", pa.bool_()),
        pa.field("velocity", pa.float64()),
        pa.field("true_track", pa.float64()),
        pa.field("vertical_rate", pa.float64()),
        pa.field("sensors", pa.list_(pa.int32())),
        pa.field("geo_altitude", pa.float64()),
        pa.field("squawk", pa.string()),
        pa.field("spi", pa.bool_()),
        pa.field("position_source", pa.int32()),
        pa.field("category", pa.int32()),
        # --- metadonnees d'ingestion (tracabilite) ---
        pa.field("snapshot_ts", pa.int64(), nullable=False),
        pa.field("ingested_at", pa.timestamp("us", tz="UTC"), nullable=False),
        pa.field("region", pa.string(), nullable=False),
        pa.field("source", pa.string(), nullable=False),
    ]
)

_N_SOURCE_FIELDS = len(STATE_VECTOR_FIELDS)


def _coerce_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_sensors(value: Any) -> list[int] | None:
    if value is None:
        return None
    if isinstance(value, list):
        return [int(item) for item in value if item is not None]
    return None


def state_vector_to_row(vector: list[Any]) -> dict[str, Any]:
    """Convertit un tableau positionnel OpenSky en dictionnaire nomme.

    Les vecteurs plus courts qu'attendu (absence de `category` quand
    `extended` n'est pas demande) sont completes par des valeurs nulles.
    """
    padded = list(vector) + [None] * (_N_SOURCE_FIELDS - len(vector))
    raw = dict(zip(STATE_VECTOR_FIELDS, padded, strict=False))

    return {
        "icao24": str(raw["icao24"]) if raw["icao24"] is not None else None,
        "callsign": raw["callsign"],
        "origin_country": raw["origin_country"],
        "time_position": _coerce_int(raw["time_position"]),
        "last_contact": _coerce_int(raw["last_contact"]),
        "longitude": _coerce_float(raw["longitude"]),
        "latitude": _coerce_float(raw["latitude"]),
        "baro_altitude": _coerce_float(raw["baro_altitude"]),
        "on_ground": bool(raw["on_ground"]) if raw["on_ground"] is not None else None,
        "velocity": _coerce_float(raw["velocity"]),
        "true_track": _coerce_float(raw["true_track"]),
        "vertical_rate": _coerce_float(raw["vertical_rate"]),
        "sensors": _coerce_sensors(raw["sensors"]),
        "geo_altitude": _coerce_float(raw["geo_altitude"]),
        "squawk": raw["squawk"],
        "spi": bool(raw["spi"]) if raw["spi"] is not None else None,
        "position_source": _coerce_int(raw["position_source"]),
        "category": _coerce_int(raw["category"]),
    }


def states_to_arrow(
    vectors: list[list[Any]],
    *,
    snapshot_ts: int,
    region: str,
    source: str = "opensky/states/all",
    ingested_at: datetime | None = None,
) -> pa.Table:
    """Materialise un snapshot complet en table Arrow prete a ecrire."""
    ingested_at = ingested_at or datetime.now(UTC)

    rows: list[dict[str, Any]] = []
    for vector in vectors:
        row = state_vector_to_row(vector)
        if row["icao24"] is None:
            # Sans identifiant appareil la ligne n'est rattachable a rien :
            # elle est ecartee des la lecture plutot que polluer le bronze.
            continue
        row["snapshot_ts"] = snapshot_ts
        row["ingested_at"] = ingested_at
        row["region"] = region
        row["source"] = source
        rows.append(row)

    return pa.Table.from_pylist(rows, schema=STATES_SCHEMA)
