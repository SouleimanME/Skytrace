"""Couche d'ingestion : ecrit la donnee brute dans le lac (couche bronze)."""

from skytrace.ingestion.air_quality import IngestedAirQuality, ingest_air_quality
from skytrace.ingestion.reference import IngestedReference, ingest_airports
from skytrace.ingestion.states import IngestedSnapshot, ingest_states

__all__ = [
    "IngestedAirQuality",
    "IngestedReference",
    "IngestedSnapshot",
    "ingest_air_quality",
    "ingest_airports",
    "ingest_states",
]
