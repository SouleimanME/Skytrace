"""Tableau de bord SkyTrace (interface radar / HUD).

Le tableau de bord ne connait que les tables `marts`. Il n'ouvre jamais un
fichier Parquet et ne recalcule jamais une agregation : c'est le contrat de
la couche gold. Consequence pratique - si une definition metier change, on
la corrige dans un modele dbt, teste et versionne, pas dans une page.

Le pipeline est batch, pas streaming : la serie temporelle ne se
"rafraichit" pas, elle s'accumule. Chaque execution planifiee ajoute un
point. La page se recharge donc periodiquement pour afficher les points
nouvellement arrives, et signale explicitement si plus rien n'arrive.

Le rendu vise une esthetique "cockpit" : fond sombre, grille technique,
neons cyan / vert, typographie Orbitron. Le style vit dans THEME_CSS ; la
logique de donnees est identique a une version sobre.

Lancement : `skytrace dashboard` (ou `streamlit run dashboard/app.py`).
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pandas as pd
import plotly.express as px
import streamlit as st

# Permet un lancement direct par Streamlit, qui ne connait pas `src/`.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# Streamlit Community Cloud fournit les secrets via st.secrets (TOML), pas via
# l'environnement. On les recopie dans os.environ AVANT get_settings() pour que
# la configuration (region, R2...) les prenne en compte.
for _key in (
    "SKYTRACE_REGION",
    "SKYTRACE_R2_ACCOUNT_ID",
    "SKYTRACE_R2_BUCKET",
    "SKYTRACE_R2_ACCESS_KEY_ID",
    "SKYTRACE_R2_SECRET_ACCESS_KEY",
):
    try:
        if _key in st.secrets:
            os.environ.setdefault(_key, str(st.secrets[_key]))
    except Exception:  # noqa: BLE001 - pas de fichier secrets en local : sans effet
        break

from skytrace.config import get_settings  # noqa: E402
from skytrace.warehouse.duck import WAREHOUSE_TIMEZONE  # noqa: E402

SETTINGS = get_settings()

st.set_page_config(
    page_title="SkyTrace - radar ADS-B",
    layout="wide",
    initial_sidebar_state="expanded",
)

#: Palette neon, reutilisee par les graphes et le theme CSS.
NEON = ["#00e5ff", "#2b6bff", "#31f2a0", "#ffb020", "#ff4d6d", "#b388ff", "#8be9fd"]

PHASE_COLOURS = {
    "croisiere": "#00e5ff",
    "montee": "#31f2a0",
    "descente": "#ffb020",
    "sol": "#3b5a8a",
    "inconnu": "#6b7ea6",
}

#: Cadence du planning. Sert de reference pour juger de la fraicheur.
SCHEDULE_MINUTES = 15


# ---------------------------------------------------------------------------
# Theme (cockpit / HUD)
# ---------------------------------------------------------------------------
THEME_CSS = """
@import url('https://fonts.googleapis.com/css2?family=Orbitron:wght@500;700;900&family=Share+Tech+Mono&family=Rajdhani:wght@400;500;600;700&display=swap');

:root{
  --cyan:#00e5ff; --blue:#2b6bff; --green:#31f2a0; --amber:#ffb020; --red:#ff4d6d;
  --text:#dbe7ff; --muted:#6b7ea6; --line:rgba(0,229,255,0.20);
  --panel:rgba(12,20,38,0.72); --grid:rgba(60,130,210,0.09);
}

.stApp{
  background:
    radial-gradient(1200px 700px at 82% -12%, rgba(43,107,255,0.12), transparent 60%),
    radial-gradient(900px 650px at -5% 105%, rgba(0,229,255,0.09), transparent 55%),
    linear-gradient(180deg,#04070e 0%, #060b16 100%);
  background-attachment: fixed;
}
.stApp::before{
  content:""; position:fixed; inset:0; pointer-events:none; z-index:0;
  background-image:
    linear-gradient(var(--grid) 1px, transparent 1px),
    linear-gradient(90deg, var(--grid) 1px, transparent 1px);
  background-size: 42px 42px;
  -webkit-mask-image: radial-gradient(ellipse at 50% 35%, black 35%, transparent 82%);
          mask-image: radial-gradient(ellipse at 50% 35%, black 35%, transparent 82%);
}
.block-container{position:relative; z-index:1; padding-top:1.4rem; max-width:1500px;}

body, .stApp, p, span, label, li, div[data-testid="stMarkdownContainer"]{
  color:var(--text); font-family:'Rajdhani','Segoe UI',sans-serif;
}
h1,h2,h3{ font-family:'Orbitron','Segoe UI',sans-serif !important; letter-spacing:2px; text-transform:uppercase;}
h2,h3{ color:var(--cyan); text-shadow:0 0 16px rgba(0,229,255,0.30); font-weight:700;}
h2{ font-size:1.15rem;} h3{ font-size:1.0rem;}

/* -- bandeau titre HUD -- */
.hud{
  border:1px solid var(--line); border-radius:14px; padding:18px 22px; margin-bottom:6px;
  background: linear-gradient(120deg, rgba(0,229,255,0.06), rgba(43,107,255,0.04));
  box-shadow: inset 0 0 40px rgba(0,229,255,0.05), 0 0 30px rgba(2,8,20,0.6);
  backdrop-filter: blur(6px);
  display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:10px;
}
.hud-title{ font-family:'Orbitron',sans-serif; font-weight:900; font-size:2.1rem; letter-spacing:6px;
  color:#eaf9ff; text-shadow:0 0 18px rgba(0,229,255,0.55), 0 0 3px #fff; margin:0;
  animation: pulse 3.2s ease-in-out infinite;}
@keyframes pulse{ 0%,100%{text-shadow:0 0 14px rgba(0,229,255,0.40);} 50%{text-shadow:0 0 26px rgba(0,229,255,0.75);} }
.hud-sub{ font-family:'Share Tech Mono',monospace; color:var(--muted); font-size:.82rem; letter-spacing:2px;}
.hud-badge{ font-family:'Share Tech Mono',monospace; font-size:.75rem; letter-spacing:2px;
  border:1px solid var(--line); border-radius:20px; padding:6px 14px; color:var(--cyan);
  box-shadow:inset 0 0 14px rgba(0,229,255,0.10);}

/* -- puce de statut -- */
.hud-status{ font-family:'Share Tech Mono',monospace; font-size:.85rem; letter-spacing:1px;
  border-radius:10px; padding:10px 16px; margin:2px 0 6px; border:1px solid var(--line);
  background:var(--panel); display:flex; align-items:center; gap:10px; backdrop-filter:blur(6px);}
.hud-status .dot{ width:10px; height:10px; border-radius:50%; box-shadow:0 0 12px currentColor; animation:blink 1.6s infinite;}
@keyframes blink{ 0%,100%{opacity:1;} 50%{opacity:.35;} }
.hud-status.ok{ color:var(--green);} .hud-status.ok .dot{ background:var(--green);}
.hud-status.warn{ color:var(--amber);} .hud-status.warn .dot{ background:var(--amber);}
.hud-status.err{ color:var(--red);} .hud-status.err .dot{ background:var(--red);}

/* -- metriques = panneaux HUD, chiffres mis en avant -- */
[data-testid="stMetric"]{
  background: linear-gradient(160deg, rgba(0,229,255,0.07), var(--panel) 62%);
  border:1px solid var(--line); border-radius:12px; padding:16px 18px 12px;
  box-shadow: inset 0 0 26px rgba(0,229,255,0.06), 0 0 22px rgba(2,8,20,0.55);
  backdrop-filter: blur(6px); position:relative; overflow:hidden;
  transition: box-shadow .2s ease, transform .2s ease;
}
[data-testid="stMetric"]:hover{
  transform: translateY(-2px);
  box-shadow: inset 0 0 32px rgba(0,229,255,0.11), 0 0 28px rgba(0,229,255,0.20);
}
[data-testid="stMetric"]::after{ content:""; position:absolute; left:0; top:0; width:100%; height:3px;
  background:linear-gradient(90deg,transparent,var(--cyan),transparent); opacity:.9;
  box-shadow:0 0 10px var(--cyan);}
[data-testid="stMetricValue"]{ font-family:'Share Tech Mono',monospace !important; color:#f2fbff;
  font-size:2.45rem; font-weight:400; line-height:1.08; text-shadow:0 0 20px rgba(0,229,255,0.60);}
[data-testid="stMetricLabel"] p{ color:var(--cyan) !important; text-transform:uppercase;
  letter-spacing:2px; font-size:.68rem; font-family:'Share Tech Mono',monospace; opacity:.85;}

hr{ border:none; height:1px; background:linear-gradient(90deg,transparent,var(--line),transparent); margin:1.1rem 0;}

[data-testid="stSidebar"]{ background:linear-gradient(180deg,#060b16,#04070e); border-right:1px solid var(--line);}
[data-testid="stSidebar"] *{ color:var(--text);}

[data-testid="stDataFrame"]{ border:1px solid var(--line); border-radius:12px; overflow:hidden;
  box-shadow:0 0 20px rgba(2,8,20,0.5);}

.stButton>button{ background:transparent; border:1px solid var(--cyan); color:var(--cyan);
  text-transform:uppercase; letter-spacing:1.5px; font-family:'Share Tech Mono',monospace;
  border-radius:9px; transition:all .2s;}
.stButton>button:hover{ box-shadow:0 0 18px rgba(0,229,255,0.45); background:rgba(0,229,255,0.08); color:#eaf9ff;}

[data-testid="stAlert"]{ background:var(--panel) !important; border:1px solid var(--line);
  border-radius:12px; backdrop-filter:blur(6px);}
[data-testid="stCaptionContainer"] p{ color:var(--muted); font-family:'Share Tech Mono',monospace; font-size:.74rem;}
[data-testid="stElementToolbar"]{ display:none;}
"""


def inject_theme() -> None:
    st.markdown(f"<style>{THEME_CSS}</style>", unsafe_allow_html=True)


def style_fig(fig, height: int | None = None):
    """Applique le theme cockpit a une figure Plotly."""
    fig.update_layout(
        template="plotly_dark",
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        colorway=NEON,
        font={"family": "Share Tech Mono, monospace", "color": "#9fb4d8", "size": 12},
        margin={"l": 8, "r": 8, "t": 14, "b": 6},
        xaxis={"gridcolor": "rgba(70,130,210,0.12)", "zerolinecolor": "rgba(70,130,210,0.2)"},
        yaxis={"gridcolor": "rgba(70,130,210,0.12)", "zerolinecolor": "rgba(70,130,210,0.2)"},
        legend={"bgcolor": "rgba(0,0,0,0)"},
        hoverlabel={
            "bgcolor": "#0a1120",
            "bordercolor": "#00e5ff",
            "font": {"family": "Share Tech Mono, monospace", "color": "#eaf9ff"},
        },
    )
    if height:
        fig.update_layout(height=height)
    return fig


# ---------------------------------------------------------------------------
# Acces aux donnees
# ---------------------------------------------------------------------------
@st.cache_data(ttl=10, show_spinner=False)
def load(sql: str) -> pd.DataFrame:
    """Execute une requete sur l'entrepot en lecture seule.

    Lecture seule volontairement : DuckDB n'autorise qu'un seul ecrivain,
    et le tableau de bord ne doit jamais bloquer un run dbt en cours.

    Le cache est volontairement tres court (10 s) : il ne sert qu'a
    dedupliquer les appels d'un meme rendu, pas a garder la donnee.

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


#: Sur R2, le collecteur n'ecrit pas dans git : Streamlit ne se redeploie
#: donc pas tout seul. Le dashboard reconstruit l'entrepot depuis R2 a
#: intervalle regulier (le `bucket` temporel casse le cache).
REBUILD_INTERVAL_SECONDS = 600


def _needs_rebuild() -> bool:
    import time

    duckdb_path = SETTINGS.resolved_duckdb_path
    if SETTINGS.uses_r2:
        # Lac distant : on ne peut pas se fier aux fichiers locaux. On
        # reconstruit si l'entrepot est absent ou plus vieux que l'intervalle.
        if not duckdb_path.exists():
            return True
        return (time.time() - duckdb_path.stat().st_mtime) > REBUILD_INTERVAL_SECONDS

    # Lac local : reconstruire seulement si un fichier source est plus recent.
    states = sorted(SETTINGS.states_dir.rglob("*.parquet"))
    if not states:
        return False
    newest = max(path.stat().st_mtime for path in states)
    return (not duckdb_path.exists()) or duckdb_path.stat().st_mtime < newest


@st.cache_resource(show_spinner="Construction de l'entrepot a partir du lac de donnees...")
def ensure_warehouse_built(bucket: int) -> None:
    """Reconstruit les marts a partir du lac (local ou R2) si necessaire.

    `bucket` est un compteur temporel : quand il change (toutes les
    REBUILD_INTERVAL_SECONDS), st.cache_resource rejoue la fonction, ce qui
    permet au dashboard R2 de capter les nouvelles donnees sans redeploiement.
    Il fait partie de la cle de cache - ne pas le prefixer d'un underscore.
    """
    _ = bucket  # sert uniquement de cle de cache
    import os
    import subprocess
    import sys

    if not _needs_rebuild():
        return

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
            "--target",
            SETTINGS.dbt_target,
        ],
        env={**os.environ, **SETTINGS.dbt_env()},
        cwd=str(SETTINGS.dbt_project_dir),
        check=True,
    )


# ---------------------------------------------------------------------------
# En-tete et commandes
# ---------------------------------------------------------------------------
def display_region() -> str:
    """Zone du dernier releve, lue dans la donnee plutot que dans la config.

    Le collecteur peut changer de zone ; le header doit refleter ce qui a
    reellement ete collecte, pas un parametre. Repli sur la config si
    l'entrepot n'est pas encore lisible.
    """
    try:
        df = load(
            "select ingestion_region from marts.fct_aircraft_positions "
            "order by snapshot_at desc limit 1"
        )
        if not df.empty and df.iloc[0]["ingestion_region"]:
            return str(df.iloc[0]["ingestion_region"])
    except Exception:  # noqa: BLE001 - repli silencieux
        pass
    return SETTINGS.region


inject_theme()

st.markdown(
    f"""
    <div class="hud">
      <div>
        <div class="hud-title">SKYTRACE</div>
        <div class="hud-sub">ADS-B LIVE RADAR // ZONE {display_region().upper()} // COUCHE MARTS</div>
      </div>
      <div class="hud-badge">OPENSKY &middot; OURAIRPORTS &middot; OPEN-METEO &middot; OPENFLIGHTS</div>
    </div>
    """,
    unsafe_allow_html=True,
)

with st.sidebar:
    st.header("Console")
    auto_refresh = st.toggle(
        "Rafraichissement auto",
        value=True,
        help=(
            "Recharge la page periodiquement. Le pipeline etant batch, de "
            "nouvelles donnees n'apparaissent qu'apres une execution planifiee."
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

# Sur un environnement neuf, construire l'entrepot avant toute lecture.
try:
    import time as _time

    ensure_warehouse_built(int(_time.time() // REBUILD_INTERVAL_SECONDS))
except Exception as exc:  # noqa: BLE001 - degradation douce, pas de trace
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
def status_chip(level: str, text: str) -> None:
    st.markdown(
        f'<div class="hud-status {level}"><span class="dot"></span>{text}</div>',
        unsafe_allow_html=True,
    )


@st.fragment(run_every=interval_seconds if auto_refresh else None)
def render() -> None:
    """Corps du tableau de bord, reexecute a chaque rafraichissement.

    Isole dans un fragment : Streamlit ne rejoue que cette fonction, sans
    reinitialiser les commandes de la barre laterale.
    """
    try:
        _render_body()
    except duckdb.IOException:
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

    if age_minutes <= SCHEDULE_MINUTES + 5:
        status_chip(
            "ok",
            f"SIGNAL NOMINAL // dernier releve il y a {age_minutes:.0f} min "
            f"({last_seen:%H:%M:%S} UTC)",
        )
    elif age_minutes <= SCHEDULE_MINUTES * 3:
        status_chip("warn", f"CYCLE MANQUE // dernier releve il y a {age_minutes:.0f} min")
    else:
        status_chip(
            "err",
            f"SIGNAL PERDU // aucune donnee depuis {age_minutes / 60:.1f} h - "
            "activer le planning dans Dagster",
        )

    # -- Indicateurs cles --------------------------------------------------
    columns = st.columns(5)
    columns[0].metric("Positions collectees", f"{int(overview['positions']):,}".replace(",", " "))
    columns[1].metric("Aeronefs distincts", f"{int(overview['aeronefs']):,}".replace(",", " "))
    columns[2].metric("Snapshots", f"{int(overview['snapshots']):,}".replace(",", " "))
    columns[3].metric("Aeroports actifs", int(airports_active))
    columns[4].metric("Historique", f"{span_hours:.1f} h")

    st.divider()

    # -- Serie par snapshot ------------------------------------------------
    st.subheader("Trafic releve par releve")

    per_snapshot = load(
        """
        select
            snapshot_at,
            count(*)                                                       as positions,
            count(distinct aircraft_icao24)                                as aeronefs,
            count(distinct case when is_on_ground then aircraft_icao24 end) as au_sol
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
            labels={"snapshot_at": "Instant du releve (UTC)", "value": "Aeronefs", "variable": ""},
            color_discrete_map={"aeronefs": "#00e5ff", "au_sol": "#3b5a8a"},
        )
        series.update_traces(
            line={"width": 2.6},
            hovertemplate="<b>%{y:.0f}</b> %{fullData.name}<extra></extra>",
        )
        style_fig(series, height=340)
        series.update_layout(legend={"orientation": "h", "y": 1.14, "x": 0}, hovermode="x unified")
        # Spike propre : un fin trait cyan continu au lieu du pointille par defaut.
        series.update_xaxes(
            showspikes=True,
            spikemode="across",
            spikethickness=1,
            spikedash="solid",
            spikecolor="rgba(0,229,255,0.45)",
        )
        st.plotly_chart(series, use_container_width=True)
        st.caption(
            f"{len(per_snapshot)} releves. Chaque point est une execution du "
            "pipeline : la granularite reelle de la collecte."
        )

    st.divider()

    # -- Carte du dernier releve -------------------------------------------
    st.subheader("Radar // dernier releve")

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
            style_fig(figure)
            figure.update_traces(marker={"size": 7, "opacity": 0.85})
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
                hole=0.62,
                color="flight_phase",
                color_discrete_map=PHASE_COLOURS,
            )
            donut.update_traces(
                textinfo="label+value",
                textfont={"family": "Share Tech Mono, monospace", "size": 11},
                marker={"line": {"color": "#04070e", "width": 2}},
            )
            style_fig(donut, height=240)
            donut.update_layout(showlegend=False, margin={"l": 0, "r": 0, "t": 6, "b": 0})
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
        select traffic_hour, sum(position_count) as positions
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
        )
        trend.update_traces(
            line={"color": "#00e5ff", "width": 2.2},
            fillcolor="rgba(0,229,255,0.14)",
        )
        style_fig(trend, height=300)
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
        )
        chart.update_traces(marker={"color": "#00e5ff", "opacity": 0.85})
        style_fig(chart, height=420)
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

    # -- Troisieme source : compagnies et flotte ---------------------------
    st.divider()
    st.subheader("Compagnies et flotte")

    airline_rows = load(
        "select count(*) as n from marts.fct_airline_airport_activity "
        "where airline_name is not null"
    ).iloc[0]["n"]

    if not airline_rows:
        st.info(
            "Pas encore de donnees compagnies. La troisieme source (base "
            "aeronefs OpenSky + compagnies OpenFlights) se remplit avec le pipeline.",
        )
    else:
        fleet_left, fleet_right = st.columns(2)

        with fleet_left:
            top_airports = load(
                """
                select airport_iata_code, airport_label, sum(distinct_aircraft) as n
                from marts.fct_airline_airport_activity
                where airline_name is not null and airport_iata_code is not null
                group by 1, 2
                order by n desc
                limit 12
                """
            )
            labels = dict(
                zip(
                    top_airports["airport_iata_code"],
                    top_airports["airport_label"],
                    strict=False,
                )
            )
            choice = st.selectbox(
                "Part de marche des compagnies a...",
                options=list(labels.keys()),
                format_func=lambda code: labels.get(code, code),
            )
            here = load(
                "select airline_name, sum(distinct_aircraft) as aeronefs "
                "from marts.fct_airline_airport_activity "
                f"where airport_iata_code = '{choice}' and airline_name is not null "
                "group by 1 order by 2 desc limit 10"
            )
            bar = px.bar(
                here.sort_values("aeronefs"),
                x="aeronefs",
                y="airline_name",
                orientation="h",
                labels={"aeronefs": "Aeronefs distincts", "airline_name": ""},
            )
            bar.update_traces(marker={"color": "#31f2a0", "opacity": 0.85})
            style_fig(bar, height=360)
            st.plotly_chart(bar, use_container_width=True)

        with fleet_right:
            makers = load(
                """
                select manufacturer_group, count(*) as aeronefs
                from marts.dim_aircraft
                where manufacturer_group <> 'Inconnu'
                group by 1
                order by 2 desc
                """
            )
            donut = px.pie(
                makers,
                names="manufacturer_group",
                values="aeronefs",
                hole=0.58,
                color_discrete_sequence=NEON,
            )
            donut.update_traces(
                textinfo="label+percent",
                textfont={"family": "Share Tech Mono, monospace", "size": 11},
                marker={"line": {"color": "#04070e", "width": 2}},
            )
            style_fig(donut, height=360)
            donut.update_layout(showlegend=False, title="Constructeurs (Airbus vs Boeing...)")
            st.plotly_chart(donut, use_container_width=True)
            st.caption(
                "Type et constructeur issus de la base aeronefs OpenSky ; "
                "compagnie deduite du prefixe d'indicatif (OpenFlights)."
            )

        # Top modeles d'avions (tous les types repertories dans la base).
        models = load(
            """
            select aircraft_type, count(*) as aeronefs
            from marts.dim_aircraft
            where aircraft_type is not null
            group by 1
            order by 2 desc
            limit 15
            """
        )
        if not models.empty:
            model_chart = px.bar(
                models.sort_values("aeronefs"),
                x="aeronefs",
                y="aircraft_type",
                orientation="h",
                labels={"aeronefs": "Aeronefs distincts", "aircraft_type": ""},
            )
            model_chart.update_traces(marker={"color": "#b388ff", "opacity": 0.85})
            style_fig(model_chart, height=420)
            model_chart.update_layout(title="Modeles d'avions les plus vus")
            st.plotly_chart(model_chart, use_container_width=True)

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
            st.metric(
                "Correlation r (brute)",
                f"{air_quality['r_naive']:+.2f}",
                help=(
                    "Coefficient de correlation de Pearson entre le nombre "
                    "d'avions et le NO2, par heure. Sans unite, de -1 a +1 "
                    "(0 = aucun lien). Le NO2 est mesure en microgrammes/m3."
                ),
            )
            st.metric(
                "Correlation r (intra-aeroport)",
                f"{air_quality['r_within']:+.2f}",
                help=(
                    "Meme coefficient, apres retrait de la moyenne de chaque "
                    "aeroport. La correlation brute, positive, s'inverse : le "
                    "lien n'est qu'un artefact 'entre aeroports'."
                ),
            )
            st.caption(
                "r = coefficient de correlation de Pearson (sans unite, -1 a +1). "
                "A l'echelle horaire, le trafic aerien n'est pas un predicteur "
                "detectable du NO2 au sol. Analyse complete : "
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
            )
            scatter.update_traces(marker={"size": 7, "opacity": 0.75})
            style_fig(scatter, height=360)
            st.plotly_chart(scatter, use_container_width=True)

    # -- Pied de page ------------------------------------------------------
    st.divider()
    st.caption(
        f"Dernier releve : {last_seen:%Y-%m-%d %H:%M:%S} UTC | "
        f"Page rafraichie a {datetime.now(UTC):%H:%M:%S} UTC | "
        f"Entrepot : {SETTINGS.resolved_duckdb_path.name} | "
        "Sources : OpenSky Network + OurAirports + Open-Meteo + OpenFlights"
    )


render()
