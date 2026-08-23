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

import math
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pandas as pd
import plotly.express as px
import pydeck as pdk
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

#: Palette, reutilisee par les graphes et le theme CSS. Teintes moins
#: saturees que du neon pur : sur fond sombre, elles restent distinctes sans
#: vibrer, et supportent d'etre superposees en semi-transparence.
NEON = ["#22d3ee", "#3b82f6", "#34d399", "#fbbf24", "#fb7185", "#a78bfa", "#67e8f9"]

#: Une phase de vol = une couleur, choisie pour rester distinguable meme
#: pour un daltonisme rouge-vert (le vert monte / l'ambre descend different
#: aussi par la luminosite).
PHASE_COLOURS = {
    "croisiere": "#22d3ee",
    "montee": "#34d399",
    "descente": "#fbbf24",
    "sol": "#64748b",
    "inconnu": "#475569",
}

#: Cadence NOMINALE du collecteur deploye (cron GitHub Actions).
SCHEDULE_MINUTES = 30

#: Seuils de fraicheur, calibres sur le comportement observe et non sur la
#: cadence nominale : GitHub differe frequemment les crons (ecarts mesures
#: ici entre 50 min et 3 h). Alerter des 35 min reviendrait a alerter en
#: permanence, donc a ne plus rien signaler du tout.
NOMINAL_MAX_MINUTES = 75
DEGRADED_MAX_MINUTES = 240

#: Couleurs des phases de vol en RGB, pour deck.gl (qui ne lit pas
#: l'hexadecimal). Memes teintes que PHASE_COLOURS, pour que la carte et les
#: graphes racontent la meme chose.
PHASE_RGB = {
    "croisiere": (34, 211, 238),
    "montee": (52, 211, 153),
    "descente": (251, 191, 36),
    "sol": (120, 140, 170),
    "inconnu": (90, 105, 130),
}

#: Taille des silhouettes d'avion, en pixels (constante quel que soit le
#: zoom). Volontairement petite : en vue monde, 8 000 appareils se recouvrent
#: des que l'icone depasse une dizaine de pixels, et la carte devient une
#: tache illisible. Le detail se lit en zoomant.
AIRCRAFT_ICON_SIZE = 9


# ---------------------------------------------------------------------------
# Theme (cockpit / HUD)
# ---------------------------------------------------------------------------
THEME_CSS = """
/* Typographie : deux familles seulement, choisies pour la lisibilite plutot
   que pour l'effet. Inter pour le texte (standard des interfaces modernes),
   JetBrains Mono pour les chiffres et identifiants - une vraie police de
   developpeur, dont les chiffres sont concus pour s'aligner en colonne.
   Orbitron ne sert QUE au logotype : partout ailleurs il fatigue l'oeil. */
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;700&family=Orbitron:wght@700;900&display=swap');

:root{
  --cyan:#22d3ee; --blue:#3b82f6; --green:#34d399; --amber:#fbbf24; --red:#fb7185;
  /* Contraste releve : l'ancien gris (#6b7ea6) tombait sous le seuil de
     lisibilite WCAG sur fond sombre et donnait un rendu terne. */
  --text:#e6edf7; --muted:#9fb3d1; --dim:#7d93b5;
  --line:rgba(34,211,238,0.18);
  --panel:rgba(13,20,35,0.92); --grid:rgba(70,130,200,0.07);
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
  color:var(--text); font-family:'Inter','Segoe UI',sans-serif;
  font-size:0.94rem; line-height:1.55;
}
/* Titres de section : Inter en graisse forte, sans ornement. La hierarchie
   passe par la graisse, la casse et l'espacement des lettres - un filet
   colore n'apportait rien et se comportait mal dans les conteneurs imbriques
   de Streamlit. */
h1,h2,h3{ font-family:'Inter','Segoe UI',sans-serif !important; }
h2,h3{
  color:#dff6fb; font-weight:600; letter-spacing:0.08em; text-transform:uppercase;
  text-shadow:none; line-height:1.35; margin-bottom:.35rem;
}
h2{ font-size:1.02rem;} h3{ font-size:0.92rem;}

/* -- bandeau titre HUD -- */
.hud{
  border:1px solid var(--line); border-radius:14px; padding:18px 22px; margin-bottom:6px;
  background: linear-gradient(120deg, rgba(0,229,255,0.06), rgba(43,107,255,0.04));
  box-shadow: inset 0 0 40px rgba(0,229,255,0.05), 0 0 30px rgba(2,8,20,0.6);
  display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:10px;
}
/* Halo fixe plutot qu'anime : une animation de `text-shadow` force le
   navigateur a repeindre le titre en continu, ce qui saccade le defilement
   pour un gain visuel nul. */
.hud-title{ font-family:'Orbitron',sans-serif; font-weight:900; font-size:2.1rem; letter-spacing:6px;
  color:#eaf9ff; text-shadow:0 0 20px rgba(34,211,238,0.50), 0 0 3px rgba(255,255,255,0.5); margin:0;}
.hud-sub{ font-family:'JetBrains Mono',monospace; color:var(--muted); font-size:.76rem;
  letter-spacing:.12em; font-weight:400;}
.hud-badge{ font-family:'JetBrains Mono',monospace; font-size:.68rem; letter-spacing:.1em;
  border:1px solid var(--line); border-radius:20px; padding:6px 14px; color:var(--cyan);
  box-shadow:inset 0 0 14px rgba(34,211,238,0.08); font-weight:500;}

/* -- puce de statut -- */
.hud-status{ font-family:'JetBrains Mono',monospace; font-size:.79rem; letter-spacing:.04em;
  border-radius:10px; padding:11px 16px; margin:2px 0 6px; border:1px solid var(--line);
  background:var(--panel); display:flex; align-items:center; gap:10px;}
/* Le point clignote via `opacity`, propriete composee par le GPU : contrairement
   a une animation d'ombre ou de couleur, elle ne declenche aucun repaint. */
.hud-status .dot{ width:9px; height:9px; border-radius:50%; box-shadow:0 0 10px currentColor;
  animation:blink 2.4s ease-in-out infinite; will-change:opacity;}
@keyframes blink{ 0%,100%{opacity:1;} 50%{opacity:.45;} }
.hud-status.ok{ color:var(--green);} .hud-status.ok .dot{ background:var(--green);}
.hud-status.warn{ color:var(--amber);} .hud-status.warn .dot{ background:var(--amber);}
.hud-status.err{ color:var(--red);} .hud-status.err .dot{ background:var(--red);}

/* -- metriques = panneaux HUD, chiffres mis en avant -- */
[data-testid="stMetric"]{
  background: linear-gradient(160deg, rgba(0,229,255,0.07), var(--panel) 62%);
  border:1px solid var(--line); border-radius:12px; padding:16px 18px 12px;
  box-shadow: inset 0 0 26px rgba(0,229,255,0.06), 0 0 22px rgba(2,8,20,0.55);
   position:relative; overflow:hidden;
  transition: box-shadow .2s ease, transform .2s ease;
}
[data-testid="stMetric"]:hover{
  transform: translateY(-2px);
  box-shadow: inset 0 0 32px rgba(0,229,255,0.11), 0 0 28px rgba(0,229,255,0.20);
}
[data-testid="stMetric"]::after{ content:""; position:absolute; left:0; top:0; width:100%; height:3px;
  background:linear-gradient(90deg,transparent,var(--cyan),transparent); opacity:.9;
  box-shadow:0 0 10px var(--cyan);}
/* Chiffres en JetBrains Mono : chasse fixe et tabular-nums, donc les valeurs
   restent alignees d'une carte a l'autre et ne "sautent" pas au rafraichissement. */
[data-testid="stMetricValue"]{ font-family:'JetBrains Mono',monospace !important; color:#f4fbff;
  font-size:2.15rem; font-weight:500; line-height:1.12; letter-spacing:-0.01em;
  font-variant-numeric: tabular-nums; text-shadow:0 0 22px rgba(34,211,238,0.35);}
[data-testid="stMetricLabel"] p{ color:var(--muted) !important; text-transform:uppercase;
  letter-spacing:.14em; font-size:.66rem; font-family:'Inter',sans-serif; font-weight:600;}

hr{ border:none; height:1px; background:linear-gradient(90deg,transparent,var(--line),transparent); margin:1.1rem 0;}

[data-testid="stSidebar"]{ background:linear-gradient(180deg,#060b16,#04070e); border-right:1px solid var(--line);}
[data-testid="stSidebar"] *{ color:var(--text);}

[data-testid="stDataFrame"]{ border:1px solid var(--line); border-radius:12px; overflow:hidden;
  box-shadow:0 0 20px rgba(2,8,20,0.5);}

.stButton>button{ background:transparent; border:1px solid var(--line); color:var(--cyan);
  text-transform:uppercase; letter-spacing:.1em; font-family:'Inter',sans-serif;
  font-weight:600; font-size:.74rem; border-radius:9px; transition:all .2s;}
.stButton>button:hover{ box-shadow:0 0 18px rgba(34,211,238,0.35);
  background:rgba(34,211,238,0.07); color:#eaf9ff; border-color:var(--cyan);}

[data-testid="stAlert"]{ background:var(--panel) !important; border:1px solid var(--line);
  border-radius:12px;}
/* Legendes : Inter et non monospace. Le monospace sur du texte courant est
   plus lent a lire ; on le reserve aux chiffres et aux identifiants. */
[data-testid="stCaptionContainer"] p{
  color:var(--dim); font-family:'Inter',sans-serif; font-size:.78rem; line-height:1.5;}
[data-testid="stCaptionContainer"] code{
  font-family:'JetBrains Mono',monospace; font-size:.74rem; color:var(--muted);
  background:rgba(34,211,238,0.07); padding:1px 5px; border-radius:4px;}
[data-testid="stElementToolbar"]{ display:none;}

/* Barre laterale : hierarchie plus nette, texte lisible. */
[data-testid="stSidebar"] h2{ font-size:.82rem; letter-spacing:.16em; color:var(--cyan);}
[data-testid="stSidebar"] [data-testid="stCaptionContainer"] p{ font-size:.75rem; color:var(--dim);}
"""


def inject_theme() -> None:
    st.markdown(f"<style>{THEME_CSS}</style>", unsafe_allow_html=True)


@st.cache_resource(show_spinner=False)
def aircraft_icon() -> dict:
    """Silhouette d'avion, generee puis embarquee en data URI.

    Dessinee a la volee plutot que chargee depuis un fichier ou un CDN : pas
    d'actif binaire a versionner, et rien a telecharger au rendu (une regle
    de securite stricte cote Streamlit Cloud bloquerait un hote externe).

    L'icone est peinte en blanc et declaree `mask: true` : deck.gl s'en sert
    alors comme pochoir et applique la couleur de chaque appareil, ce qui
    evite de generer une icone par phase de vol.
    """
    import base64
    from io import BytesIO

    from PIL import Image, ImageDraw

    size = 128
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    # Nez vers le HAUT : deck.gl considere 0 degre comme pointant vers le nord.
    draw.polygon(
        [
            (64, 5),
            (69, 26),
            (71, 48),
            (124, 78),
            (124, 92),
            (71, 78),
            (69, 101),
            (83, 115),
            (83, 123),
            (64, 116),
            (45, 123),
            (45, 115),
            (59, 101),
            (57, 78),
            (4, 92),
            (4, 78),
            (57, 48),
            (59, 26),
        ],
        fill=(255, 255, 255, 255),
    )

    buffer = BytesIO()
    image.save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return {
        "url": f"data:image/png;base64,{encoded}",
        "width": size,
        "height": size,
        "anchorX": size // 2,
        "anchorY": size // 2,
        "mask": True,
    }


AIRCRAFT_ICON = aircraft_icon()


def style_fig(fig, height: int | None = None):
    """Applique le theme du tableau de bord a une figure Plotly.

    Choix de lisibilite plutot que d'effet : grille discrete (elle guide sans
    concurrencer la donnee), axes en Inter, valeurs en JetBrains Mono avec
    chiffres tabulaires, et infobulle contrastee.
    """
    axis = {
        "gridcolor": "rgba(90,140,200,0.10)",
        "zerolinecolor": "rgba(90,140,200,0.18)",
        "linecolor": "rgba(90,140,200,0.22)",
        "tickfont": {"family": "JetBrains Mono, monospace", "size": 11, "color": "#9fb3d1"},
        "title": {"font": {"family": "Inter, sans-serif", "size": 12, "color": "#7d93b5"}},
    }
    fig.update_layout(
        template="plotly_dark",
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        colorway=NEON,
        font={"family": "Inter, sans-serif", "color": "#9fb3d1", "size": 12},
        margin={"l": 8, "r": 8, "t": 14, "b": 6},
        xaxis=axis,
        yaxis=axis,
        legend={
            "bgcolor": "rgba(0,0,0,0)",
            "font": {"family": "Inter, sans-serif", "size": 11, "color": "#9fb3d1"},
        },
        hoverlabel={
            "bgcolor": "rgba(10,17,32,0.96)",
            "bordercolor": "#22d3ee",
            "font": {"family": "JetBrains Mono, monospace", "size": 12, "color": "#eaf9ff"},
        },
    )
    # Le titre n'est style QUE s'il existe. Toucher au titre d'une figure qui
    # n'en a pas (via `title_font` ou `title={"text": None}`) fait afficher a
    # Plotly le litteral "undefined" au-dessus du graphe : cote JavaScript,
    # l'absence de texte est serialisee en `undefined` puis rendue telle quelle.
    if fig.layout.title.text:
        fig.update_layout(
            title_font={"family": "Inter, sans-serif", "size": 13, "color": "#dff6fb"}
        )

    if height:
        fig.update_layout(height=height)
    return fig


# ---------------------------------------------------------------------------
# Acces aux donnees
# ---------------------------------------------------------------------------
@st.cache_data(ttl=90, show_spinner=False)
def load(sql: str) -> pd.DataFrame:
    """Execute une requete sur l'entrepot en lecture seule.

    Lecture seule volontairement : DuckDB n'autorise qu'un seul ecrivain,
    et le tableau de bord ne doit jamais bloquer un run dbt en cours.

    Cache de 90 s : la donnee amont ne change qu'a chaque collecte (30 min au
    mieux), donc interroger DuckDB a chaque interaction de widget etait du
    gaspillage pur - c'etait la principale source de latence percue.

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
    # Deux familles d'intervalles : les courts (15 s a 5 min) servent au
    # developpement et a la surveillance rapprochee ; les longs (30 min a 24 h)
    # correspondent a la cadence reelle de la collecte, au-dela de laquelle
    # recharger plus souvent n'apporte aucune donnee nouvelle.
    interval_choices = {
        "15 s": 15,
        "30 s": 30,
        "1 min": 60,
        "5 min": 300,
        "30 min": 1800,
        "60 min": 3600,
        "6 h": 21600,
        "12 h": 43200,
        "24 h": 86400,
    }
    interval_label = st.selectbox(
        "Intervalle",
        options=list(interval_choices),
        index=2,
        disabled=not auto_refresh,
        help=(
            "Cadence de rechargement de la page. La collecte, elle, tourne "
            "toutes les 30 min : au-dela, aucune donnee nouvelle n'arrive."
        ),
    )
    interval_seconds = interval_choices[interval_label]

    if st.button("Recharger maintenant", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

    st.divider()
    st.caption(
        f"Collecte planifiee toutes les {SCHEDULE_MINUTES} min "
        "(GitHub Actions en production, Dagster en local)."
    )

# Construction INITIALE, indispensable avant la verification d'etat qui suit :
# sur un environnement neuf (Streamlit Cloud), l'entrepot n'existe pas encore
# et la page s'arreterait avant meme d'avoir tente de le batir. Le
# rafraichissement periodique, lui, se fait dans le fragment `render()`.
# `st.cache_resource` rend ce second appel gratuit tant que le creneau de
# reconstruction n'a pas change.
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

    C'est aussi ICI que l'entrepot est rafraichi, et non dans le corps du
    script. Un `run_every` ne rejoue QUE le fragment : place au niveau du
    script, la reconstruction n'aurait lieu qu'au chargement initial de la
    page, et le tableau de bord afficherait indefiniment des donnees figees
    pendant que le collecteur, lui, continue d'alimenter le lac.
    """
    import time as _time

    try:
        ensure_warehouse_built(int(_time.time() // REBUILD_INTERVAL_SECONDS))
    except Exception as exc:  # noqa: BLE001 - une reconstruction ratee ne doit
        # pas vider la page : on garde l'affichage precedent et on le signale.
        st.warning(f"Rafraichissement de l'entrepot impossible : {exc}")

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

    # Seuils calibres sur le comportement REEL du cron GitHub Actions, pas sur
    # sa cadence theorique : mesure faite sur ce depot, les ecarts entre deux
    # collectes vont de 50 min a plus de 3 h (GitHub execute les crons "au
    # mieux"). Des seuils calques sur les 30 min nominales afficheraient une
    # alerte en permanence, ce qui reviendrait a n'alerter sur rien.
    if age_minutes <= NOMINAL_MAX_MINUTES:
        status_chip(
            "ok",
            f"SIGNAL NOMINAL // dernier releve il y a {age_minutes:.0f} min "
            f"({last_seen:%H:%M:%S} UTC)",
        )
    elif age_minutes <= DEGRADED_MAX_MINUTES:
        status_chip(
            "warn",
            f"COLLECTE RETARDEE // dernier releve il y a {age_minutes:.0f} min "
            "(le cron GitHub est frequemment differe)",
        )
    else:
        status_chip(
            "err",
            f"SIGNAL PERDU // aucune donnee depuis {age_minutes / 60:.1f} h - "
            "verifier le workflow Collecte planifiee (onglet Actions)",
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
        # Pas de "spike" vertical : `hovermode="x unified"` designe deja le
        # releve survole et en affiche les valeurs. Une barre en plus est
        # redondante, et Plotly la dessine large et opaque.
        series.update_xaxes(showspikes=False)
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
            latitude, longitude, callsign, origin_country, heading_deg,
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
            # Chaque appareil est dessine comme une silhouette d'avion orientee
            # selon son cap reel : l'image donne alors les flux (couloirs
            # transatlantiques, approches d'aeroport) qu'un simple point ne
            # montre pas. La couleur reste la phase de vol.
            #
            # deck.gl dessine sur GPU : les 8 000 aeronefs d'un releve mondial
            # passent sans peine, la ou Plotly (rendu SVG) imposait un
            # echantillonnage. On affiche donc la totalite du releve.
            plotted = latest.copy()
            plotted["angle"] = -plotted["heading_deg"].fillna(0)
            plotted["colour"] = [PHASE_RGB.get(p, (150, 160, 180)) for p in plotted["flight_phase"]]
            plotted["icon"] = [AIRCRAFT_ICON] * len(plotted)
            plotted["callsign"] = plotted["callsign"].fillna("(sans indicatif)")
            plotted["altitude_txt"] = plotted["barometric_altitude_ft"].map(
                lambda v: "-" if pd.isna(v) else f"{v:,.0f} ft".replace(",", " ")
            )
            plotted["vitesse_txt"] = plotted["ground_speed_kt"].map(
                lambda v: "-" if pd.isna(v) else f"{v:.0f} kt"
            )

            # Cadrage automatique sur la donnee reelle plutot qu'un zoom fixe :
            # la meme page reste lisible que la zone soit la France ou le monde.
            lat_span = plotted["latitude"].max() - plotted["latitude"].min()
            lon_span = plotted["longitude"].max() - plotted["longitude"].min()
            span = max(lat_span, lon_span / 1.8, 1.0)
            zoom = max(1.0, min(6.5, 7.2 - math.log2(span)))

            # Attention : pydeck transforme toute CHAINE de caracteres en
            # accesseur de colonne. Passer `size_units="pixels"` produisait
            # `sizeUnits: @@=pixels`, soit "lis la colonne pixels" - colonne
            # inexistante, d'ou un dimensionnement aberrant. Les unites en
            # pixels etant deja le defaut de deck.gl, on ne les precise pas ;
            # seules des valeurs NUMERIQUES sont passees ici.
            layer = pdk.Layer(
                "IconLayer",
                data=plotted,
                get_icon="icon",
                get_position=["longitude", "latitude"],
                get_angle="angle",
                get_color="colour",
                get_size=AIRCRAFT_ICON_SIZE,
                size_min_pixels=5,
                size_max_pixels=AIRCRAFT_ICON_SIZE,
                opacity=0.85,
                pickable=True,
            )
            st.pydeck_chart(
                pdk.Deck(
                    layers=[layer],
                    initial_view_state=pdk.ViewState(
                        latitude=float(plotted["latitude"].median()),
                        longitude=float(plotted["longitude"].median()),
                        zoom=zoom,
                    ),
                    map_style=pdk.map_styles.CARTO_DARK,
                    tooltip={
                        "html": (
                            "<b>{callsign}</b><br/>{origin_country}"
                            "<br/>{altitude_txt} &middot; {vitesse_txt}"
                            "<br/><span style='opacity:.7'>{flight_phase}</span>"
                        ),
                        "style": {
                            "backgroundColor": "rgba(10,17,32,0.96)",
                            "color": "#eaf9ff",
                            "fontFamily": "Inter, sans-serif",
                            "fontSize": "12px",
                            "border": "1px solid #22d3ee",
                            "borderRadius": "8px",
                        },
                    },
                ),
                height=560,
            )
            st.caption(
                f"{len(plotted):,} aeronefs du dernier releve. ".replace(",", " ")
                + "Chaque silhouette est orientee selon le cap reel de "
                "l'appareil ; la couleur indique la phase de vol. "
                "**Les zones vides ne sont pas des zones sans trafic** : le "
                "reseau OpenSky repose sur des recepteurs benevoles, denses en "
                "Europe et en Amerique du Nord, rares ailleurs. Les volumes ne "
                "sont pas comparables d'une region a l'autre."
            )

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
            donut.update_layout(
                showlegend=False,
                title={"text": "Constructeurs (Airbus vs Boeing...)", "y": 0.97},
                margin={"l": 8, "r": 8, "t": 46, "b": 6},
            )
            st.plotly_chart(donut, use_container_width=True)
            st.caption(
                "Type et constructeur issus de la base aeronefs OpenSky ; "
                "compagnie deduite du prefixe d'indicatif (OpenFlights)."
            )

        # Top modeles d'avions (tous les types repertories dans la base).
        models = load(
            """
            select
                aircraft_type,
                any_value(manufacturer) as manufacturer,
                count(*)                as aeronefs
            from marts.dim_aircraft
            where aircraft_type is not null
            group by aircraft_type
            order by aeronefs desc
            limit 15
            """
        )
        if not models.empty:
            # Etiquette lisible : "Boeing B738" plutot que le code seul.
            models["label"] = [
                f"{m} {t}" if m else t
                for m, t in zip(models["manufacturer"], models["aircraft_type"], strict=False)
            ]
            model_chart = px.bar(
                models.sort_values("aeronefs"),
                x="aeronefs",
                y="label",
                orientation="h",
                labels={"aeronefs": "Aeronefs distincts", "label": ""},
                text="aeronefs",
            )
            model_chart.update_traces(
                marker={"color": "#b388ff", "opacity": 0.9},
                textposition="outside",
                textfont={"color": "#dbe7ff", "family": "Share Tech Mono, monospace"},
                cliponaxis=False,
            )
            style_fig(model_chart, height=480)
            model_chart.update_layout(
                title={"text": "Modeles d'avions les plus vus", "y": 0.97},
                margin={"l": 8, "r": 44, "t": 48, "b": 6},
            )
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
