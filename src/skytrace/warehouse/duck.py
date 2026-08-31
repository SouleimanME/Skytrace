"""Connexion a l'entrepot DuckDB.

DuckDB est ici l'equivalent local d'un entrepot cloud : meme SQL analytique,
meme modele colonnaire, meme lecture directe de Parquet - sans facture ni
compte a provisionner. Le passage a BigQuery ou Snowflake se ferait en
changeant l'adaptateur dbt et ce module, pas les modeles (cf. ADR 0002).
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import duckdb

from skytrace.config import Settings, get_settings

if TYPE_CHECKING:
    from collections.abc import Iterator

#: Fuseau impose a toute session DuckDB.
#:
#: Un TIMESTAMPTZ stocke un instant absolu, mais DuckDB l'affiche - et le
#: decoupe, via `date_trunc` - dans le fuseau de la session, qui vaut par
#: defaut celui de la machine. Sans ce verrouillage, la meme requete rend
#: des horaires differents selon le poste, et un pipeline qui partitionne
#: son lac en UTC afficherait des heures locales sans le dire.
WAREHOUSE_TIMEZONE = "UTC"


@dataclass(frozen=True)
class TableInfo:
    schema: str
    name: str
    rows: int


@contextmanager
def connect(
    settings: Settings | None = None,
    *,
    read_only: bool = False,
) -> Iterator[duckdb.DuckDBPyConnection]:
    """Ouvre une connexion a l'entrepot, fermee automatiquement.

    `read_only=True` permet au tableau de bord de lire pendant qu'un run
    dbt ecrit : DuckDB n'autorise qu'un seul ecrivain, mais plusieurs
    lecteurs concurrents.
    """
    settings = settings or get_settings()
    path = settings.resolved_duckdb_path
    path.parent.mkdir(parents=True, exist_ok=True)

    connection = duckdb.connect(str(path), read_only=read_only)
    try:
        connection.execute(f"SET TimeZone = '{WAREHOUSE_TIMEZONE}'")
        yield connection
    finally:
        connection.close()


def query(
    sql: str,
    params: list[Any] | None = None,
    *,
    settings: Settings | None = None,
) -> list[tuple[Any, ...]]:
    """Execute une requete et renvoie les lignes."""
    with connect(settings, read_only=True) as connection:
        return connection.execute(sql, params or []).fetchall()


def describe_warehouse(settings: Settings | None = None) -> list[TableInfo]:
    """Inventaire des tables et vues presentes, avec leur volumetrie."""
    settings = settings or get_settings()
    if not settings.resolved_duckdb_path.exists():
        return []

    with connect(settings, read_only=True) as connection:
        objects = connection.execute(
            """
            select table_schema, table_name
            from information_schema.tables
            where table_schema not in ('information_schema', 'pg_catalog')
            order by table_schema, table_name
            """
        ).fetchall()

        infos: list[TableInfo] = []
        for schema, name in objects:
            # Les identifiants viennent du catalogue DuckDB lui-meme, mais
            # on les cite malgre tout : un nom de table ne se concatene
            # jamais nu dans du SQL.
            quoted = f'"{schema}"."{name}"'
            try:
                # Un identifiant de table ne peut pas etre lie comme un
                # parametre : il est donc cite, et il vient du catalogue.
                (rows,) = connection.execute(
                    f"select count(*) from {quoted}"  # noqa: S608
                ).fetchone()
            except duckdb.Error:
                rows = -1
            infos.append(TableInfo(schema=schema, name=name, rows=int(rows)))

    return infos
