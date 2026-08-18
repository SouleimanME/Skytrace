"""Point d'entree Dagster : assemble assets, controles, jobs et ressources.

Lancement de l'interface :

    dagster dev -m orchestration.definitions
"""

from __future__ import annotations

from dagster import Definitions

from orchestration.assets import (
    check_snapshot_is_not_empty,
    raw_opensky_states,
    raw_ourairports_airports,
    skytrace_dbt_assets,
)
from orchestration.resources import build_resources
from orchestration.schedules import all_jobs, all_schedules

defs = Definitions(
    assets=[
        raw_opensky_states,
        raw_ourairports_airports,
        skytrace_dbt_assets,
    ],
    asset_checks=[check_snapshot_is_not_empty],
    jobs=all_jobs,
    schedules=all_schedules,
    resources=build_resources(),
)
