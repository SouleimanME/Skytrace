"""Assets Dagster : le graphe de production de donnees.

Dagster raisonne en "assets" (les tables et fichiers produits) plutot qu'en
taches. La difference compte : le graphe affiche ce qui existe, sa
fraicheur, sa volumetrie et son historique de production. Les modeles dbt
sont importes automatiquement, si bien que la lignee va du fichier Parquet
brut jusqu'a la table de faits, sans description manuelle.
"""

# Pas de `from __future__ import annotations` dans ce module : Dagster
# inspecte les annotations reelles des parametres pour reconnaitre le
# contexte d'execution et les ressources. Des annotations differees
# (chaines de caracteres) empechent cette resolution.

from collections.abc import Iterator
from typing import Any

import pyarrow.parquet as pq
from dagster import (
    AssetCheckExecutionContext,
    AssetCheckResult,
    AssetExecutionContext,
    AssetKey,
    MaterializeResult,
    MetadataValue,
    asset,
    asset_check,
)
from dagster_dbt import DbtCliResource, dbt_assets

from orchestration.resources import OpenSkyResource, dbt_project
from skytrace.config import get_settings

# Cles d'asset alignees sur les sources declarees dans dbt. C'est ce qui
# raccorde les deux moities du graphe : dagster-dbt traduit la source
# `raw.opensky_states` en AssetKey(["raw", "opensky_states"]), donc en
# nommant l'asset Python de la meme facon, la lignee devient continue.
STATES_ASSET_KEY = AssetKey(["raw", "opensky_states"])
AIRPORTS_ASSET_KEY = AssetKey(["raw", "ourairports_airports"])


# ---------------------------------------------------------------------------
# Couche bronze : ingestion
# ---------------------------------------------------------------------------
@asset(
    key=STATES_ASSET_KEY,
    group_name="ingestion",
    compute_kind="python",
    description=(
        "Snapshot des positions d'aeronefs recupere sur l'API OpenSky et "
        "ecrit en Parquet partitionne (couche bronze)."
    ),
)
def raw_opensky_states(
    context: AssetExecutionContext,
    opensky: OpenSkyResource,
) -> MaterializeResult:
    snapshot = opensky.ingest_snapshot()

    context.log.info("%d aeronefs ecrits dans %s", snapshot.rows, snapshot.path)

    # Ces metadonnees sont historisees par Dagster : on obtient gratuitement
    # des courbes de volumetrie et de consommation de quota dans l'interface.
    return MaterializeResult(
        metadata={
            "aeronefs": MetadataValue.int(snapshot.rows),
            "region": MetadataValue.text(snapshot.region),
            "instant_du_releve": MetadataValue.text(
                snapshot.snapshot_at.strftime("%Y-%m-%d %H:%M:%S UTC")
            ),
            "credits_consommes": MetadataValue.int(snapshot.credits_spent),
            "taille_ko": MetadataValue.float(round(snapshot.size_bytes / 1024, 1)),
            "fichier": MetadataValue.path(str(snapshot.path)),
        }
    )


@asset(
    key=AIRPORTS_ASSET_KEY,
    group_name="ingestion",
    compute_kind="python",
    description=(
        "Referentiel mondial des aerodromes (OurAirports). Dimension a "
        "evolution lente, rafraichie quotidiennement."
    ),
)
def raw_ourairports_airports(
    context: AssetExecutionContext,
    opensky: OpenSkyResource,
) -> MaterializeResult:
    reference = opensky.ingest_reference()

    context.log.info("%d aeroports ecrits dans %s", reference.rows, reference.path)

    return MaterializeResult(
        metadata={
            "aeroports": MetadataValue.int(reference.rows),
            "colonnes": MetadataValue.int(len(reference.columns)),
            # L'empreinte permet de voir d'un coup d'oeil si le fichier
            # amont a reellement change depuis la veille.
            "sha256": MetadataValue.text(reference.checksum[:16]),
            "fichier": MetadataValue.path(str(reference.path)),
        }
    )


# ---------------------------------------------------------------------------
# Controles qualite au niveau de l'asset
# ---------------------------------------------------------------------------
@asset_check(
    asset=STATES_ASSET_KEY,
    name="snapshot_non_vide",
    description=(
        "Un snapshot vide n'est pas une erreur technique : l'API repond 200. "
        "C'est pourtant le symptome typique d'une zone mal configuree ou "
        "d'une panne du reseau de recepteurs, et sans ce controle le "
        "pipeline continuerait a tourner en produisant du vide."
    ),
)
def check_snapshot_is_not_empty(context: AssetCheckExecutionContext) -> AssetCheckResult:
    settings = get_settings()
    snapshots = sorted(settings.states_dir.rglob("*.parquet"))
    if not snapshots:
        return AssetCheckResult(
            passed=False,
            metadata={"raison": MetadataValue.text("aucun fichier dans la couche bronze")},
        )

    latest = snapshots[-1]
    rows = pq.ParquetFile(latest).metadata.num_rows

    return AssetCheckResult(
        passed=rows > 0,
        metadata={
            "dernier_fichier": MetadataValue.path(str(latest)),
            "lignes": MetadataValue.int(rows),
            "total_snapshots": MetadataValue.int(len(snapshots)),
        },
    )


# ---------------------------------------------------------------------------
# Couches silver / gold : dbt
# ---------------------------------------------------------------------------
@dbt_assets(
    manifest=dbt_project.manifest_path,
    project=dbt_project,
)
def skytrace_dbt_assets(
    context: AssetExecutionContext,
    dbt: DbtCliResource,
) -> Iterator[Any]:
    """Tous les modeles et tests dbt, exposes un a un dans le graphe.

    `dbt build` plutot que `dbt run` : chaque modele est immediatement suivi
    de ses tests, et un modele en aval n'est jamais construit sur des
    donnees qui viennent d'echouer a un controle qualite.
    """
    yield from dbt.cli(["build"], context=context).stream()
