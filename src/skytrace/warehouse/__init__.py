"""Acces a l'entrepot DuckDB."""

from skytrace.warehouse.duck import connect, describe_warehouse, query

__all__ = ["connect", "describe_warehouse", "query"]
