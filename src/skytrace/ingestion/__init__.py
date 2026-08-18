"""Couche d'ingestion : ecrit la donnee brute dans le lac (couche bronze)."""

from skytrace.ingestion.reference import IngestedReference, ingest_airports
from skytrace.ingestion.states import IngestedSnapshot, ingest_states

__all__ = [
    "IngestedReference",
    "IngestedSnapshot",
    "ingest_airports",
    "ingest_states",
]
