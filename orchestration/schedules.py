"""Jobs et declencheurs temporels.

Deux cadences, parce que les deux sources n'ont pas la meme nature :

  * les positions sont un flux : elles n'existent qu'au moment ou on les
    demande, une donnee non collectee est perdue pour toujours. D'ou une
    collecte frequente ;
  * le referentiel aeroports est un etat : il est toujours disponible en
    entier, et bouge de quelques lignes par mois. Le rafraichir toutes les
    15 minutes serait du gaspillage pur.
"""

from __future__ import annotations

from dagster import AssetSelection, ScheduleDefinition, define_asset_job

from orchestration.assets import AIRPORTS_ASSET_KEY

# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------
#: Chemin chaud : un snapshot de trafic, puis toute la chaine dbt.
#: Le referentiel est exclu de la selection - il est deja a jour.
traffic_pipeline_job = define_asset_job(
    name="traffic_pipeline_job",
    selection=AssetSelection.all() - AssetSelection.assets(AIRPORTS_ASSET_KEY),
    description="Ingestion d'un snapshot de trafic, puis transformation et tests dbt.",
)

#: Chemin froid : rafraichissement du referentiel et de tout ce qui en depend.
reference_refresh_job = define_asset_job(
    name="reference_refresh_job",
    selection=AssetSelection.assets(AIRPORTS_ASSET_KEY).downstream(),
    description="Rafraichissement du referentiel aeroports et reconstruction en aval.",
)


# ---------------------------------------------------------------------------
# Planification
# ---------------------------------------------------------------------------
#: Toutes les 15 minutes : 96 executions par jour.
#: En mode anonyme, une zone "france" coute 3 credits par appel, soit 288
#: credits sur les 400 alloues - la marge absorbe les relances manuelles.
#: Elargir la zone ou accelerer la cadence exige des identifiants OAuth2.
traffic_schedule = ScheduleDefinition(
    name="traffic_every_15_minutes",
    job=traffic_pipeline_job,
    cron_schedule="*/15 * * * *",
    execution_timezone="UTC",
    description="Collecte du trafic toutes les 15 minutes (compatible quota anonyme).",
)

#: Tous les jours a 04:00 UTC : creux de trafic, et le referentiel amont
#: est publie quotidiennement.
reference_schedule = ScheduleDefinition(
    name="reference_daily",
    job=reference_refresh_job,
    cron_schedule="0 4 * * *",
    execution_timezone="UTC",
    description="Rafraichissement quotidien du referentiel aeroports.",
)

all_jobs = [traffic_pipeline_job, reference_refresh_job]
all_schedules = [traffic_schedule, reference_schedule]
