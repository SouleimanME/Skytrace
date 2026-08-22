"""Tableau de bord SkyTrace.

Le tableau de bord ne connait que les tables `marts`. Il n'ouvre jamais un
fichier Parquet et ne recalcule jamais une agregation : c'est le contrat de
la couche gold. Consequence pratique - si une definition metier change, on
la corrige dans un modele dbt, teste et versionne, pas dans une page.

Le pipeline est batch, pas streaming : la serie temporelle ne se
"rafraichit" pas, elle s'accumule. Chaque execution planifiee ajoute un
point. La page se recharge donc periodiquement pour afficher les points
nouvellement arrives, et signale explicitement si plus rien n'arrive.

Lancement : `skytrace dashboard` (ou `streamlit run dashboard/app.py`).
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pandas as pd
import plotly.express as px
import streamlit as st

# Permet un lancement direct par Streamlit, qui ne connait pas `src/`.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from skytrace.config import get_settings  # noqa: E402
from skytrace.warehouse.duck import WAREHOUSE_TIMEZONE  # noqa: E402

SETTINGS = get_settings()

st.set_page_config(
    page_title="SkyTrace - trafic aerien",
    layout="wide",
)

PHASE_COLOURS = {
    "croisiere": "#2563eb",
    "montee": "#16a34a",
    "descente": "#ea580c",
    "sol": "#64748b",
    "inconnu": "#a1a1aa",
}

#: Cadence du planning Dagster. Sert de reference pour juger si la donnee
#: est fraiche : au-dela de deux cycles manques, quelque chose ne tourne pas.
SCHEDULE_MINUTES = 15


# ---------------------------------------------------------------------------
# Acces aux donnees
# ---------------------------------------------------------------------------
@st.cache_data(ttl=10, show_spinner=False)
def load(sql: str) -> pd.DataFrame:
    """Execute une requete sur l'entrepot en lecture seule.

    Lecture seule volontairement : DuckDB n'autorise qu'un seul ecrivain,
    et le tableau de bord ne doit jamais bloquer un run dbt en cours.

    Le cache est volontairement tres court (10 s) : il ne sert qu'a
    dedupliquer les appels d'un meme rendu, pas a garder la donnee. Un
    cache long irait a l'encontre du rafraichissement automatique.

    Le fuseau est force en UTC : DuckDB rend sinon les TIMESTAMPTZ dans le
    fuseau de la machine, et la page afficherait des heures locales sous
    des libelles "UTC".
    """
    with duckdb.connect(str(SETTINGS.resolved_duckdb_path), read_only=True) as connection:
        connection.execute(f"SET TimeZone = '{WAREHOUSE_TIMEZONE}'")
        return connection.execute(sql).df()


def warehouse_is_ready() -> tuple[bool, str]:
    path = SETTINGS.resolved_duckdb_path
    if not path.exists():
        return False, f"Entrepot introuvable : {path}"
    try:
        load("select 1 from marts.fct_aircraft_positions limit 1")
    except duckdb.IOException:
        return False, "Entrepot verrouille par une ecriture en cours. Reessayer dans un instant."
    except duckdb.CatalogException:
        return False, "Les tables `marts` n'existent pas encore."
    return True, ""


@st.cache_resource(show_spinner="Construction de l'entrepot a partir du lac de donnees...")
def ensure_warehouse_built() -> None:
    """Reconstruit les marts a partir du lac Parquet si necessaire.

    Sur un environnement neuf - typiquement Streamlit Community Cloud - seul
    le lac Parquet est versionne ; la base DuckDB, elle, est regeneree. Cette
    fonction lance `dbt build` quand la base est absente ou plus ancienne que
    le dernier fichier ingere. En local, ou la base existe deja et est a
    jour, elle ne fait rien.

    `st.cache_resource` garantit une seule execution par demarrage de l'app.
    Comme chaque publication de donnees redeploie l'app sur Streamlit Cloud,
    le cache repart a froid et la reconstruction capte la donnee fraiche.
    """
    import os
    import subprocess
    import sys

    states = sorted(SETTINGS.states_dir.rglob("*.parquet"))
    if not states:
        # Aucune donnee encore collectee (juste avant la premiere execution
        # du workflow). warehouse_is_ready() affichera l'invite appropriee.
        return

    duckdb_path = SETTINGS.resolved_duckdb_path
    newest_source = max(path.stat().st_mtime for path in states)
    if duckdb_path.exists() and duckdb_path.stat().st_mtime >= newest_source:
        return  # deja a jour, rien a reconstruire

    SETTINGS.ensure_directories()
    subprocess.run(
        [
            sys.executable,
            "-m",
            "dbt.cli.main",
            "build",
            "--project-dir",
            str(SETTINGS.dbt_project_dir),
            "--profiles-dir",
            str(SETTINGS.dbt_project_dir),
        ],
        env={**os.environ, **SETTINGS.dbt_env()},
        cwd=str(SETTINGS.dbt_project_dir),
        check=True,
    )


# ---------------------------------------------------------------------------
# En-tete et commandes
# ---------------------------------------------------------------------------
st.title("SkyTrace")
st.caption(
    "Trafic aerien observe via ADS-B (OpenSky Network) - "
    f"zone **{SETTINGS.region}**, donnees agregees par la couche `marts`."
)

with st.sidebar:
    st.header("Rafraichissement")
    auto_refresh = st.toggle(
        "Automatique",
        value=True,
        help=(
            "Recharge la page periodiquement. Le pipeline etant batch, "
            "de nouvelles donnees n'apparaissent qu'apres une execution "
            "du planning Dagster."
        ),
    )
    interval_label = st.selectbox(
        "Intervalle",
        options=["15 s", "30 s", "1 min", "5 min"],
        index=2,
        disabled=not auto_refresh,
    )
    interval_seconds = {"15 s": 15, "30 s": 30, "1 min": 60, "5 min": 300}[interval_label]

    if st.button("Recharger maintenant", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

    st.divider()
    st.caption(
        f"Collecte planifiee toutes les {SCHEDULE_MINUTES} min "
        "(`traffic_every_15_minutes` dans Dagster)."
    )

# Sur un environnement neuf, construire l'entrepot avant toute lecture. Une
# reconstruction en echec (ex : lac pas encore publie) n'interrompt pas la
# page : warehouse_is_ready() prend le relais avec un message clair.
try:
    ensure_warehouse_built()
except Exception as exc:  # noqa: BLE001 - on veut une degradation douce, pas une trace
    st.warning(f"Entrepot pas encore disponible : {exc}")

ready, message = warehouse_is_ready()
if not ready:
    st.warning(message)
    st.info(
        "En attente de la premiere collecte. Sur le deploiement public, le "
        "workflow *Collecte planifiee* remplit le lac dans les 30 minutes ; "
        "on peut aussi le declencher manuellement depuis l'onglet Actions.",
    )
    st.stop()


# ---------------------------------------------------------------------------
# Rendu
# ---------------------------------------------------------------------------
@st.fragment(run_every=interval_seconds if auto_refresh else None)
def render() -> None:
    """Corps du tableau de bord, reexecute a chaque rafraichissement.

    Isole dans un fragment : Streamlit ne rejoue que cette fonction, sans
    reinitialiser les commandes de la barre laterale.
    """
    try:
        _render_body()
    except duckdb.IOException:
        # Un run dbt tient le verrou d'ecriture pendant une seconde ou deux.
        # C'est transitoire : on l'annonce au lieu d'afficher une trace.
        st.info("Mise a jour de l'entrepot en cours, affichage dans un instant.")


def _render_body() -> None:
    # -- Fraicheur ---------------------------------------------------------
    overview = load(
        """
        select
            count(*)                        as positions,
            count(distinct aircraft_icao24) as aeronefs,
            count(distinct snapshot_at)     as snapshots,
            min(snapshot_at)                as debut,
            max(snapshot_at)                as fin
        from marts.fct_aircraft_positions
        """
    ).iloc[0]

    airports_active = load(
        "select count(distinct airport_id) as n from marts.fct_airport_activity"
    ).iloc[0]["n"]

    last_seen = overview["fin"]
    age_minutes = (datetime.now(UTC) - last_seen.to_pydatetime()).total_seconds() / 60
    span_hours = (overview["fin"] - overview["debut"]).total_seconds() / 3600

    # Deux cycles manques : le planning est probablement a l'arret. C'est la
    # question que se pose vraiment quelqu'un devant un graphique plat.
    if age_minutes <= SCHEDULE_MINUTES + 5:
        st.success(
            f"Donnees a jour - dernier releve il y a {age_minutes:.0f} min "
            f"({last_seen:%H:%M:%S} UTC).",
        )
    elif age_minutes <= SCHEDULE_MINUTES * 3:
        st.warning(
            f"Dernier releve il y a {age_minutes:.0f} min - un cycle de "
            f"collecte semble avoir ete manque.",
        )
    else:
        st.error(
            f"Aucune donnee nouvelle depuis {age_minutes / 60:.1f} h. "
            "Le planning `traffic_every_15_minutes` est probablement inactif : "
            "l'activer dans l'onglet Automation de Dagster, sans quoi la serie "
            "temporelle ne se remplira pas.",
        )

    # -- Indicateurs cles --------------------------------------------------
    columns = st.columns(5)
    columns[0].metric("Positions collectees", f"{int(overview['positions']):,}".replace(",", " "))
    columns[1].metric("Aeronefs distincts", f"{int(overview['aeronefs']):,}".replace(",", " "))
    columns[2].metric("Snapshots", f"{int(overview['snapshots']):,}".replace(",", " "))
    columns[3].metric("Aeroports actifs", int(airports_active))
    columns[4].metric("Profondeur d'historique", f"{span_hours:.1f} h")

    st.divider()

    # -- Serie par snapshot ------------------------------------------------
    # Granularite native de la collecte : un point par execution. Contrairement
    # a l'agregat horaire, elle devient lisible des le deuxieme releve.
    st.subheader("Trafic releve par releve")

    per_snapshot = load(
        """
        select
            snapshot_at,
            count(*)                                                       as positions,
            count(distinct aircraft_icao24)                                as aeronefs,
            count(distinct case when is_on_ground then aircraft_icao24 end) as au_sol,
            round(avg(barometric_altitude_ft))                             as altitude_moyenne_ft
        from marts.fct_aircraft_positions
        group by snapshot_at
        order by snapshot_at
        """
    )

    if len(per_snapshot) < 2:
        st.info(
            "Un seul releve pour l'instant. La courbe apparait des le "
            f"deuxieme, soit environ {SCHEDULE_MINUTES} min apres le premier.",
        )
    else:
        series = px.line(
            per_snapshot,
            x="snapshot_at",
            y=["aeronefs", "au_sol"],
            markers=True,
            labels={
                "snapshot_at": "Instant du releve (UTC)",
                "value": "Aeronefs",
                "variable": "",
            },
            height=340,
            color_discrete_map={"aeronefs": "#2563eb", "au_sol": "#64748b"},
        )
        series.update_layout(
            margin={"l": 0, "r": 0, "t": 10, "b": 0},
            legend={"orientation": "h", "y": 1.12, "x": 0},
            hovermode="x unified",
        )
        st.plotly_chart(series, use_container_width=True)
        st.caption(
            f"{len(per_snapshot)} releves. Chaque point est une execution du "
            "pipeline : c'est la granularite reelle de la collecte."
        )

    st.divider()

    # -- Carte du dernier releve -------------------------------------------
    st.subheader("Dernier releve")

    latest = load(
        """
        select
            latitude, longitude, callsign, origin_country,
            barometric_altitude_ft, ground_speed_kt, flight_phase, aircraft_icao24
        from marts.fct_aircraft_positions
        where snapshot_at = (select max(snapshot_at) from marts.fct_aircraft_positions)
        """
    )

    map_column, phase_column = st.columns([3, 1])

    with map_column:
        if latest.empty:
            st.info("Aucune position sur le dernier snapshot.")
        else:
            figure = px.scatter_map(
                latest,
                lat="latitude",
                lon="longitude",
                color="flight_phase",
                color_discrete_map=PHASE_COLOURS,
                size_max=9,
                zoom=4,
                height=520,
                hover_name="callsign",
                hover_data={
                    "origin_country": True,
                    "barometric_altitude_ft": ":,.0f",
                    "ground_speed_kt": ":.0f",
                    "latitude": False,
                    "longitude": False,
                },
                labels={"flight_phase": "Phase"},
            )
            figure.update_layout(
                map_style="carto-darkmatter",
                margin={"l": 0, "r": 0, "t": 0, "b": 0},
                legend={"orientation": "h", "y": -0.05},
            )
            st.plotly_chart(figure, use_container_width=True)

    with phase_column:
        if not latest.empty:
            phases = latest.groupby("flight_phase").size().reset_index(name="aeronefs")
            donut = px.pie(
                phases,
                names="flight_phase",
                values="aeronefs",
                hole=0.55,
                color="flight_phase",
                color_discrete_map=PHASE_COLOURS,
                title="Phases de vol",
            )
            donut.update_layout(margin={"l": 0, "r": 0, "t": 40, "b": 0}, showlegend=False)
            donut.update_traces(textinfo="label+value")
            st.plotly_chart(donut, use_container_width=True)

            st.metric(
                "Altitude mediane",
                f"{latest['barometric_altitude_ft'].median():,.0f} ft".replace(",", " "),
            )
            st.metric("Vitesse mediane", f"{latest['ground_speed_kt'].median():.0f} kt")

    st.divider()

    # -- Serie horaire -----------------------------------------------------
    st.subheader("Tendance horaire")

    hourly = load(
        """
        select
            traffic_hour,
            sum(position_count)           as positions,
            round(avg(avg_altitude_m), 0) as altitude_moyenne_m
        from marts.fct_traffic_hourly
        group by traffic_hour
        order by traffic_hour
        """
    )

    if len(hourly) < 2:
        st.info(
            "L'agregat horaire demande au moins deux heures de collecte. "
            "En attendant, la courbe releve par releve ci-dessus fait le travail.",
        )
    else:
        trend = px.area(
            hourly,
            x="traffic_hour",
            y="positions",
            labels={"traffic_hour": "Heure (UTC)", "positions": "Positions collectees"},
            height=320,
        )
        trend.update_traces(line_color="#2563eb", fillcolor="rgba(37, 99, 235, 0.18)")
        trend.update_layout(margin={"l": 0, "r": 0, "t": 10, "b": 0})
        st.plotly_chart(trend, use_container_width=True)
        st.caption(
            "Agregat issu de `fct_traffic_hourly`. C'est ici que le creux "
            "nocturne et le pic du matin deviennent visibles."
        )

    st.divider()

    # -- Classements -------------------------------------------------------
    countries_column, airports_column = st.columns(2)

    with countries_column:
        st.subheader("Pays d'immatriculation")
        countries = load(
            """
            select origin_country, sum(position_count) as positions
            from marts.fct_traffic_hourly
            group by origin_country
            order by positions desc
            limit 12
            """
        )
        chart = px.bar(
            countries.sort_values("positions"),
            x="positions",
            y="origin_country",
            orientation="h",
            labels={"positions": "Positions", "origin_country": ""},
            height=420,
        )
        chart.update_traces(marker_color="#2563eb")
        chart.update_layout(margin={"l": 0, "r": 0, "t": 10, "b": 0})
        st.plotly_chart(chart, use_container_width=True)

    with airports_column:
        st.subheader("Aeroports les plus actifs")
        airports = load(
            """
            select
                airport_label                   as aeroport,
                airport_municipality            as ville,
                sum(distinct_aircraft)          as aeronefs,
                sum(descending_aircraft)        as en_approche,
                sum(climbing_aircraft)          as en_montee,
                round(avg(avg_distance_km), 2)  as distance_moy_km
            from marts.fct_airport_activity
            group by airport_label, airport_municipality
            order by aeronefs desc
            limit 15
            """
        )
        st.dataframe(airports, use_container_width=True, hide_index=True, height=420)
        st.caption(
            "Les colonnes *en approche* et *en montee* sont inferees du taux "
            "de montee : ADS-B ne publie pas de plan de vol."
        )

    # -- Deuxieme source : trafic et qualite de l'air ----------------------
    st.divider()
    st.subheader("Trafic et qualite de l'air")

    air_quality = load(
        """
        with panel as (
            select
                distinct_aircraft,
                no2_ugm3,
                distinct_aircraft
                    - avg(distinct_aircraft) over (partition by airport_id) as ac_within,
                no2_ugm3
                    - avg(no2_ugm3) over (partition by airport_id) as no2_within
            from marts.fct_airport_hourly_air_quality
            where no2_ugm3 is not null
        )
        select
            count(*)                          as n,
            corr(distinct_aircraft, no2_ugm3) as r_naive,
            corr(ac_within, no2_within)       as r_within
        from panel
        """
    ).iloc[0]

    if not air_quality["n"] or air_quality["n"] < 10:
        st.info(
            "Pas encore assez de donnees croisees trafic / qualite de l'air. "
            "La deuxieme source (Open-Meteo) se remplit avec le pipeline.",
        )
    else:
        left, right = st.columns([1, 2])
        with left:
            st.metric("Corr. brute avions ~ NO2", f"{air_quality['r_naive']:+.2f}")
            st.metric(
                "Corr. intra-aeroport",
                f"{air_quality['r_within']:+.2f}",
                help=(
                    "Apres retrait de la moyenne de chaque aeroport. La "
                    "correlation brute, positive, s'inverse : le lien n'est "
                    "qu'un artefact 'entre aeroports'."
                ),
            )
            st.caption(
                "A l'echelle horaire, le trafic aerien n'est pas un predicteur "
                "detectable du NO2 au sol. Analyse complete dans "
                "`docs/analyse_trafic_qualite_air.md`."
            )
        with right:
            panel = load(
                """
                select distinct_aircraft, no2_ugm3, airport_iata_code
                from marts.fct_airport_hourly_air_quality
                where no2_ugm3 is not null
                """
            )
            scatter = px.scatter(
                panel,
                x="distinct_aircraft",
                y="no2_ugm3",
                color="airport_iata_code",
                labels={
                    "distinct_aircraft": "Avions distincts par heure",
                    "no2_ugm3": "NO2 au sol (ug/m3)",
                    "airport_iata_code": "Aeroport",
                },
                height=360,
            )
            scatter.update_layout(margin={"l": 0, "r": 0, "t": 10, "b": 0})
            st.plotly_chart(scatter, use_container_width=True)

    # -- Pied de page ------------------------------------------------------
    st.divider()
    st.caption(
        f"Dernier releve : **{last_seen:%Y-%m-%d %H:%M:%S} UTC** | "
        f"Page rafraichie a {datetime.now(UTC):%H:%M:%S} UTC | "
        f"Entrepot : `{SETTINGS.resolved_duckdb_path.name}` | "
        "Sources : OpenSky Network + OurAirports + Open-Meteo"
    )


render()
