"""Tableau de bord SkyTrace (interface radar / HUD).

Le tableau de bord ne connaît que les tables `marts`. Il n'ouvre jamais un
fichier Parquet et ne recalcule jamais une agrégation : c'est le contrat de
la couche gold. Conséquence pratique - si une définition métier change, on
la corrige dans un modèle dbt, testé et versionné, pas dans une page.

Le pipeline est batch, pas streaming : la série temporelle ne se
"rafraîchit" pas, elle s'accumule. Chaque exécution planifiée ajoute un
point. La page se recharge donc périodiquement pour afficher les points
nouvellement arrivés, et signale explicitement si plus rien n'arrive.

Le rendu vise une esthétique "cockpit" : fond sombre, grille technique,
néons cyan / vert, typographie Orbitron. Le style vit dans THEME_CSS ; la
logique de données est identique à une version sobre.

Lancement : `skytrace dashboard` (ou `streamlit run dashboard/app.py`).
"""

from __future__ import annotations

import html
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
from pydeck.types import String as PdkString

# Permet un lancement direct par Streamlit, qui ne connaît pas `src/`.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# Streamlit Community Cloud fournit les secrets via st.secrets (TOML), pas via
# l'environnement. On les recopie dans os.environ AVANT get_settings() pour que
# la configuration (région, R2...) les prenne en compte.
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
from skytrace.photos import fetch_photo, looks_military  # noqa: E402
from skytrace.stats import estimate  # noqa: E402
from skytrace.warehouse.duck import WAREHOUSE_TIMEZONE  # noqa: E402

SETTINGS = get_settings()

st.set_page_config(
    page_title="SkyTrace - radar ADS-B",
    layout="wide",
    initial_sidebar_state="expanded",
)

#: Palette, réutilisée par les graphes et le thème CSS. Teintes moins
#: saturées que du néon pur : sur fond sombre, elles restent distinctes sans
#: vibrer, et supportent d'être superposées en semi-transparence.
NEON = ["#22d3ee", "#3b82f6", "#34d399", "#fbbf24", "#fb7185", "#a78bfa", "#67e8f9"]

#: Une phase de vol = une couleur, choisie pour rester distinguable même
#: pour un daltonisme rouge-vert (le vert monte / l'ambre descend différent
#: aussi par la luminosité).
PHASE_COLOURS = {
    "croisiere": "#22d3ee",
    "montee": "#34d399",
    "descente": "#fbbf24",
    "sol": "#64748b",
    "inconnu": "#475569",
}

#: Cadence NOMINALE du collecteur déployé (cron GitHub Actions).
SCHEDULE_MINUTES = 30

#: Seuils de fraîcheur, calibrés sur le comportement observé et non sur la
#: cadence nominale : GitHub diffère fréquemment les crons (écarts mesurés
#: ici entre 50 min et 3 h). Alerter dès 35 min reviendrait à alerter en
#: permanence, donc à ne plus rien signaler du tout.
NOMINAL_MAX_MINUTES = 75
DEGRADED_MAX_MINUTES = 240

#: Les phases de vol restent en ASCII dans l'entrepôt : ce sont des clés de
#: jointure entre le modèle dbt, les tests de données et l'interface. Un
#: accent dans une valeur stockée finirait tôt ou tard par ne plus
#: correspondre. L'accent appartient donc à l'affichage, et à lui seul.
PHASE_LABELS = {
    "croisiere": "croisière",
    "montee": "montée",
    "descente": "descente",
    "sol": "sol",
    "inconnu": "inconnu",
}


def phase_label(value: str) -> str:
    """Libellé affichable d'une phase de vol (identité si inconnue)."""
    return PHASE_LABELS.get(value, value)


#: Couleurs des phases de vol en RGB, pour deck.gl (qui ne lit pas
#: l'hexadécimal). Mêmes teintes que PHASE_COLOURS, pour que la carte et les
#: graphes racontent la même chose.
PHASE_RGB = {
    "croisiere": (34, 211, 238),
    "montee": (52, 211, 153),
    "descente": (251, 191, 36),
    "sol": (120, 140, 170),
    "inconnu": (90, 105, 130),
}

#: Taille des silhouettes d'avion, en pixels (constante quel que soit le
#: zoom). Volontairement petite : en vue monde, 8 000 appareils se recouvrent
#: dès que l'icône dépasse une dizaine de pixels, et la carte devient une
#: tache illisible. Le détail se lit en zoomant.
AIRCRAFT_ICON_SIZE = 9

#: Valeurs qui signalent une absence de donnée. La fiche détaillée ne
#: consacre pas une ligne à dire qu'elle ne sait pas : elle omet la ligne.
UNKNOWN = {"-", "Inconnu"}


# ---------------------------------------------------------------------------
# Thème (cockpit / HUD)
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
  /* Contraste relevé : l'ancien gris (#6b7ea6) tombait sous le seuil de
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
/* Streamlit anime le repli de la barre laterale sur 300 ms. Si un
   rafraichissement automatique tombe pendant cette transition, l'ancien et le
   nouveau contenu se superposent et le texte parait dedouble. Un fond opaque
   sur le conteneur interne masque la couche du dessous pendant l'animation. */
[data-testid="stSidebar"] > div:first-child{ background:#060b16;}
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

/* -- barre d'onglets -- */
/* Les onglets par defaut de Streamlit sont un texte gris souligne : ils
   passent pour du texte et non pour une navigation. Ici ils portent la
   meme typographie que les titres de section, et l'onglet actif est
   marque par la couleur ET par un filet, pas par la couleur seule. */
[data-testid="stTabs"] [role="tablist"]{
  gap:4px; border-bottom:1px solid var(--line); margin-bottom:.5rem;
}
[data-testid="stTabs"] [role="tab"]{
  font-family:'Inter','Segoe UI',sans-serif; font-weight:600;
  letter-spacing:.1em; text-transform:uppercase; font-size:.78rem;
  color:var(--dim); padding:8px 16px; border-radius:8px 8px 0 0;
}
[data-testid="stTabs"] [role="tab"]:hover{ color:var(--text); background:rgba(34,211,238,0.05); }
[data-testid="stTabs"] [role="tab"][aria-selected="true"]{
  color:var(--cyan); background:rgba(34,211,238,0.08);
}
/* Le surlignage de l'onglet actif : Streamlit le dessine en rouge par
   defaut, ce qui jure et suggere une erreur. */
[data-testid="stTabs"] [data-baseweb="tab-highlight"]{ background:var(--cyan); }
[data-testid="stTabs"] [data-baseweb="tab-border"]{ background:transparent; }

/* ------------------------------------------------------------------ */
/* Petits ecrans                                                       */
/*                                                                     */
/* Streamlit fait passer chaque colonne en pleine largeur des que la   */
/* place manque. Empilees telles quelles, les cinq cartes d'indicateurs*/
/* occupaient a elles seules 470 pixels - plus de la moitie d'un ecran */
/* de telephone avant d'avoir montre la moindre donnee. On les range   */
/* donc par deux, on resserre les marges et on reduit la typographie   */
/* d'affichage, dimensionnee pour un ecran large.                      */
/* ------------------------------------------------------------------ */
@media (max-width: 640px){

  .block-container{ padding-top:.7rem; padding-left:.7rem; padding-right:.7rem; }

  /* La grille de fond coute un repaint a chaque defilement pour un
     effet invisible sur un ecran de cette taille. */
  .stApp::before{ display:none; }

  .hud{ padding:12px 14px; border-radius:12px; }
  .hud-title{ font-size:1.6rem; letter-spacing:3px; }
  .hud-sub{ font-size:.66rem; letter-spacing:.08em; }
  .hud-badge{ font-size:.6rem; padding:4px 9px; }

  h2{ font-size:.92rem; } h3{ font-size:.84rem; }

  /* Deux indicateurs par ligne : `flex-basis` a 47 % laisse la place a
     l'espacement, et `min-width` doit etre abaisse sinon Streamlit
     impose sa propre largeur minimale et la ligne retombe a un. */
  .st-key-indicateurs [data-testid="stColumn"]{
    flex: 1 1 47% !important;
    min-width: 47% !important;
    width: 47% !important;
  }
  .st-key-indicateurs [data-testid="stMetricValue"]{ font-size:1.25rem !important; }
  .st-key-indicateurs [data-testid="stMetricLabel"] p{ font-size:.62rem !important; }
  .st-key-indicateurs [data-testid="stMetric"]{ padding:10px 12px !important; }

  /* Les legendes sous les graphes sont des paragraphes d'explication :
     lisibles au calme sur un grand ecran, envahissants ici. */
  [data-testid="stCaptionContainer"] p{ font-size:.72rem; line-height:1.45; }

  [data-testid="stMetricValue"]{ font-size:1.4rem !important; }

  /* Le tableau des aeroports a six colonnes : il defile lateralement
     dans son propre cadre plutot que d'etirer la page. */
  [data-testid="stDataFrame"]{ overflow-x:auto; }

  /* Cinq onglets ne tiennent pas sur 375 pixels : ils defilent
     horizontalement plutot que de se comprimer illisiblement. */
  [data-testid="stTabs"] [role="tablist"]{ overflow-x:auto; flex-wrap:nowrap; }
  [data-testid="stTabs"] [role="tab"]{
    padding:7px 11px; font-size:.7rem; letter-spacing:.06em; white-space:nowrap;
  }

  /* Les sections repliees sont des titres de section : elles doivent en
     avoir l'allure, pas celle d'un widget Streamlit par defaut. */
  [data-testid="stExpander"] summary p{
    font-family:'Inter','Segoe UI',sans-serif; font-weight:600;
    letter-spacing:.08em; text-transform:uppercase; font-size:.9rem;
    color:#dff6fb;
  }
  [data-testid="stExpander"] details{
    border:1px solid var(--line); border-radius:12px;
    background:linear-gradient(120deg, rgba(0,229,255,0.05), rgba(43,107,255,0.03));
  }
  /* Un separateur AVANT chaque section repliee suffit a les detacher :
     celui que Streamlit ajoute autour creait deux respirations. */
  [data-testid="stExpander"] + hr, hr + [data-testid="stExpander"]{ margin-top:.4rem; }
}
"""


#: Fragments d'agent utilisateur des terminaux tenus en main.
HANDHELD_HINTS = ("android", "iphone", "ipod", "ipad", "windows phone", "mobile")


def is_handheld() -> bool:
    """Vrai si la page est servie à un telephone ou une tablette.

    La mise en forme se règle en CSS, mais deux choses ne s'y pretent pas :
    le zoom initial de la carte et sa hauteur sont des valeurs passées a
    deck.gl, pas des propriétés de style. L'agent utilisateur est une
    approximation - il se falsifie et se trompe sur les cas limites - mais
    l'erreur reste sans conséquence : au pire, un cadrage un peu large.
    """
    try:
        agent = (st.context.headers.get("User-Agent") or "").lower()
    except Exception:  # noqa: BLE001 - hors contexte de requête
        return False
    return any(hint in agent for hint in HANDHELD_HINTS)


def inject_theme() -> None:
    st.markdown(f"<style>{THEME_CSS}</style>", unsafe_allow_html=True)


@st.cache_resource(show_spinner=False)
def aircraft_icon() -> tuple[str, dict]:
    """Silhouette d'avion, générée puis embarquée en data URI.

    Dessinée à la volée plutôt que chargée depuis un fichier ou un CDN : pas
    d'actif binaire à versionner, et rien à télécharger au rendu (une règle
    de sécurité stricte côté Streamlit Cloud bloquerait un hôte externe).

    L'icône est peinte en blanc et déclarée `mask: true` : deck.gl s'en sert
    alors comme pochoir et applique la couleur de chaque appareil, ce qui
    évite de générer une icône par phase de vol.

    Renvoie un PLANCHIER (`iconAtlas` + `iconMapping`) et non une icône par
    appareil. La nuance est tout sauf cosmétique : attachée à chaque ligne,
    l'image encodée - 1 130 caractères - repartait 13 000 fois à chaque
    rechargement, soit 15 Mo de JSON pour redessiner la même silhouette.
    Déclarée une seule fois au niveau du calque, chaque appareil ne
    transporte plus qu'un nom.
    """
    import base64
    from io import BytesIO

    from PIL import Image, ImageDraw

    size = 128
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    # Nez vers le HAUT : deck.gl considère 0 degré comme pointant vers le nord.
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
    atlas = f"data:image/png;base64,{encoded}"
    mapping = {
        ICON_NAME: {
            "x": 0,
            "y": 0,
            "width": size,
            "height": size,
            "anchorX": size // 2,
            "anchorY": size // 2,
            "mask": True,
        }
    }
    return atlas, mapping


#: Nom de l'unique icône du planchier. Chaque appareil ne transporte que ce
#: nom : le PNG, lui, n'est transmis qu'une fois (voir `aircraft_icon`).
ICON_NAME = "avion"

AIRCRAFT_ICON_ATLAS, AIRCRAFT_ICON_MAPPING = aircraft_icon()


def chart_config() -> dict:
    """Options du rendu Plotly, adaptees au toucher.

    La barre d'outils n'apparait qu'au survol sur un écran de bureau, mais
    reste affichee en permanence sur un écran tactile, ou elle recouvre la
    légende. Aucun de ses boutons - zoom, lasso, capture - n'a de sens au
    doigt : on la retire.
    """
    return {"displayModeBar": not is_handheld(), "responsive": True}


def style_fig(fig, height: int | None = None):
    """Applique le thème du tableau de bord à une figure Plotly.

    Choix de lisibilité plutôt que d'effet : grille discrète (elle guide sans
    concurrencer la donnée), axes en Inter, valeurs en JetBrains Mono avec
    chiffres tabulaires, et infobulle contrastée.
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
    # Le titre n'est stylé QUE s'il existe. Toucher au titre d'une figure qui
    # n'en a pas (via `title_font` ou `title={"text": None}`) fait afficher à
    # Plotly le littéral "undefined" au-dessus du graphe : côté JavaScript,
    # l'absence de texte est sérialisée en `undefined` puis rendue telle quelle.
    if fig.layout.title.text:
        fig.update_layout(
            title_font={"family": "Inter, sans-serif", "size": 13, "color": "#dff6fb"}
        )

    if height:
        # Les hauteurs sont calibrees pour un écran large. Sur telephone,
        # les conserver telles quelles ferait de la page un couloir : chaque
        # graphe occuperait la moitie de l'écran, et il y en a huit.
        fig.update_layout(height=int(height * 0.72) if is_handheld() else height)
    return fig


# ---------------------------------------------------------------------------
# Accès aux données
# ---------------------------------------------------------------------------
@st.cache_data(ttl=90, show_spinner=False)
def load(sql: str) -> pd.DataFrame:
    """Exécute une requête sur l'entrepôt en lecture seule.

    Lecture seule volontairement : DuckDB n'autorise qu'un seul écrivain,
    et le tableau de bord ne doit jamais bloquer un run dbt en cours.

    Cache de 90 s : la donnée amont ne change qu'à chaque collecte (30 min au
    mieux), donc interroger DuckDB à chaque interaction de widget était du
    gaspillage pur - c'était la principale source de latence perçue.

    Le fuseau est force en UTC : DuckDB rend sinon les TIMESTAMPTZ dans le
    fuseau de la machine, et la page afficherait des heures locales sous
    des libellés "UTC".
    """
    with duckdb.connect(str(SETTINGS.resolved_duckdb_path), read_only=True) as connection:
        connection.execute(f"SET TimeZone = '{WAREHOUSE_TIMEZONE}'")
        return connection.execute(sql).df()


def warehouse_is_ready() -> tuple[bool, str]:
    path = SETTINGS.resolved_duckdb_path
    if not path.exists():
        return False, f"Entrepôt introuvable : {path}"
    try:
        load("select 1 from marts.fct_aircraft_positions limit 1")
    except duckdb.IOException:
        return False, "Entrepôt verrouillé par une écriture en cours. Réessayer dans un instant."
    except duckdb.CatalogException:
        return False, "Les tables `marts` n'existent pas encore."
    return True, ""


#: Sur R2, le collecteur n'écrit pas dans git : Streamlit ne se redéploie
#: donc pas tout seul. Le dashboard reconstruit l'entrepôt depuis R2 à
#: intervalle régulier (le `bucket` temporel casse le cache).
REBUILD_INTERVAL_SECONDS = 600


#: Colonnes que le tableau de bord LIT et que l'entrepot doit donc porter.
#: Cette liste est la contrepartie de la promesse faite par les marts : si
#: l'une manque, ce n'est pas une requete a corriger, c'est un entrepot en
#: retard sur le code.
REQUIRED_COLUMNS = {
    "marts.fct_aircraft_positions": ("emergency_kind", "is_position_stale"),
    "marts.dim_aircraft": ("airline_source",),
}


def schema_drift() -> list[str]:
    """Colonnes attendues par le code et absentes de l'entrepot.

    POURQUOI CETTE VERIFICATION EXISTE. `fct_aircraft_positions` est
    incremental. Quand une colonne y est ajoutee, `on_schema_change =
    'append_new_columns'` l'ajoute bien a la table, mais laisse a NULL toutes
    les lignes deja presentes : dbt ne retro-remplit pas. Un test `not_null`
    sur cette colonne echoue alors sur l'historique entier, `dbt build`
    s'arrete, et TOUT l'aval est ignore - y compris les dimensions, qui
    restent a l'ancien schema.

    Le tableau de bord se retrouve alors devant un entrepot a moitie a jour et
    plante sur une colonne manquante. C'est arrive en production : le code
    etait deploye, l'entrepot ne l'etait pas.

    La reponse est une reconstruction COMPLETE, pas un rattrapage : le lac est
    immuable et tout rebatir coute une dizaine de secondes.
    """
    chemin = SETTINGS.resolved_duckdb_path
    if not chemin.exists():
        return []

    manquantes: list[str] = []
    try:
        with duckdb.connect(str(chemin), read_only=True) as connection:
            for table, colonnes in REQUIRED_COLUMNS.items():
                presentes = {
                    ligne[0]
                    for ligne in connection.execute(
                        "select column_name from information_schema.columns "
                        "where table_schema || '.' || table_name = ?",
                        [table],
                    ).fetchall()
                }
                if not presentes:
                    # Table absente : la reconstruction normale s'en charge.
                    continue
                manquantes += [f"{table}.{c}" for c in colonnes if c not in presentes]
    except duckdb.Error:
        # Entrepot verrouille ou illisible : ce n'est pas a cette fonction de
        # le diagnostiquer, elle repond simplement qu'elle ne sait pas.
        return []
    return manquantes


def _needs_rebuild() -> bool:
    import time

    duckdb_path = SETTINGS.resolved_duckdb_path
    if SETTINGS.uses_r2:
        # Lac distant : on ne peut pas se fier aux fichiers locaux. On
        # reconstruit si l'entrepôt est absent ou plus vieux que l'intervalle.
        if not duckdb_path.exists():
            return True
        return (time.time() - duckdb_path.stat().st_mtime) > REBUILD_INTERVAL_SECONDS

    # Lac local : reconstruire seulement si un fichier source est plus récent.
    states = sorted(SETTINGS.states_dir.rglob("*.parquet"))
    if not states:
        return False
    newest = max(path.stat().st_mtime for path in states)
    return (not duckdb_path.exists()) or duckdb_path.stat().st_mtime < newest


@st.cache_resource(show_spinner="Construction de l'entrepôt à partir du lac de données...")
def ensure_warehouse_built(bucket: int) -> None:
    """Reconstruit les marts à partir du lac (local ou R2) si nécessaire.

    `bucket` est un compteur temporel : quand il change (toutes les
    REBUILD_INTERVAL_SECONDS), st.cache_resource rejoue la fonction, ce qui
    permet au dashboard R2 de capter les nouvelles données sans redéploiement.
    Il fait partie de la clé de cache - ne pas le préfixer d'un underscore.
    """
    _ = bucket  # sert uniquement de clé de cache
    import os
    import subprocess
    import sys

    manquantes = schema_drift()
    if not manquantes and not _needs_rebuild():
        return

    # Une colonne attendue et absente signifie que l'entrepôt est en retard
    # sur le code. Une reconstruction incrémentale ne rattraperait pas :
    # l'historique resterait à NULL sur les nouvelles colonnes, et le test
    # `not_null` ferait échouer le build, donc ignorer tout l'aval. On
    # reconstruit entièrement - le lac est immuable, ça coûte une dizaine de
    # secondes.
    complet = ["--full-refresh"] if manquantes else []

    SETTINGS.ensure_directories()
    subprocess.run(
        [
            sys.executable,
            "-m",
            "dbt.cli.main",
            "build",
            *complet,
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
# En-tête et commandes
# ---------------------------------------------------------------------------
def display_region() -> str:
    """Zone du dernier relevé, lue dans la donnée plutôt que dans la config.

    Le collecteur peut changer de zone ; le header doit refléter ce qui a
    réellement ete collecte, pas un paramètre. Repli sur la config si
    l'entrepôt n'est pas encore lisible.
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
        "Rafraîchissement auto",
        value=True,
        help=(
            "Recharge la page périodiquement. Le pipeline étant batch, de "
            "nouvelles données n'apparaissent qu'après une exécution planifiée."
        ),
    )
    # Deux familles d'intervalles : les courts (15 s à 5 min) servent au
    # développement et à la surveillance rapprochée ; les longs (30 min à 24 h)
    # correspondent à la cadence réelle de la collecte, au-delà de laquelle
    # recharger plus souvent n'apporte aucune donnée nouvelle.
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
            "toutes les 30 min : au-delà, aucune donnée nouvelle n'arrive."
        ),
    )
    interval_seconds = interval_choices[interval_label]

    if st.button("Recharger maintenant", width="stretch"):
        st.cache_data.clear()
        st.rerun()

    st.divider()
    st.caption(
        f"Collecte planifiée toutes les {SCHEDULE_MINUTES} min "
        "(GitHub Actions en production, Dagster en local)."
    )

# Construction INITIALE, indispensable avant la vérification d'état qui suit :
# sur un environnement neuf (Streamlit Cloud), l'entrepôt n'existe pas encore
# et la page s'arrêterait avant même d'avoir tenté de le bâtir. Le
# rafraîchissement périodique, lui, se fait dans le fragment `render()`.
# `st.cache_resource` rend ce second appel gratuit tant que le créneau de
# reconstruction n'a pas changé.
try:
    import time as _time

    ensure_warehouse_built(int(_time.time() // REBUILD_INTERVAL_SECONDS))
except Exception as exc:  # noqa: BLE001 - dégradation douce, pas de trace
    st.warning(f"Entrepôt pas encore disponible : {exc}")

ready, message = warehouse_is_ready()
if not ready:
    st.warning(message)
    st.info(
        "En attente de la première collecte. Sur le déploiement public, le "
        "workflow *Collecte planifiée* remplit le lac dans les 30 minutes ; "
        "on peut aussi le déclencher manuellement depuis l'onglet Actions.",
    )
    st.stop()

# ---------------------------------------------------------------------------
# Filtres interactifs
# ---------------------------------------------------------------------------
#: Libellés lisibles des dimensions filtrables, affichés dans le bandeau.
FILTER_LABELS = {
    "flight_phase": "Phase",
    "manufacturer_group": "Constructeur",
    "origin_country": "Pays",
    "airline_name": "Compagnie",
}


def active_filters() -> dict[str, str]:
    """Filtres en cours, conserves entre deux rendus du fragment.

    Passer par `session_state` est indispensable : le fragment se rejoue tout
    seul (`run_every`), et l'événement de sélection d'un graphe est vide lors
    de ces rejeux automatiques. Sans état persistant, tout filtre disparaîtrait
    à la minute suivante.
    """
    return st.session_state.setdefault("filters", {})


def render_filter_panel() -> None:
    """Panneau de filtres, dans la barre latérale.

    Pourquoi des listes déroulantes plutôt qu'un clic direct sur les graphes :
    `st.plotly_chart(on_select=...)` ne reçoit d'événement que des traces que
    Plotly sait rendre sélectionnables (nuages, barres via lasso ou rectangle).
    Un camembert n'en fait pas partie - le clic n'émet rien, quel que soit le
    `selection_mode`. Des widgets natifs sont donc le seul moyen fiable, et ils
    ont l'avantage de montrer d'emblée les valeurs disponibles au lieu de
    laisser deviner que le graphe est cliquable.
    """
    with st.sidebar:
        st.divider()
        st.header("Filtres")

        options = load(
            """
            select
                list_sort(list_distinct(list(flight_phase)))          as phases,
                list_sort(list_distinct(list(origin_country)))        as pays
            from marts.fct_aircraft_positions
            where snapshot_at = (select max(snapshot_at) from marts.fct_aircraft_positions)
            """
        ).iloc[0]
        makers = load(
            "select distinct manufacturer_group from marts.dim_aircraft "
            "where manufacturer_group is not null order by 1"
        )["manufacturer_group"].tolist()
        airlines = load(
            "select airline_name, sum(distinct_aircraft) n "
            "from marts.fct_airline_airport_activity where airline_name is not null "
            "group by 1 order by n desc limit 40"
        )["airline_name"].tolist()

        filters = active_filters()
        choices = {
            "flight_phase": list(options["phases"]),
            "manufacturer_group": makers,
            "origin_country": list(options["pays"]),
            "airline_name": airlines,
        }

        for dimension, values in choices.items():
            values = [v for v in values if v is not None]
            current = filters.get(dimension)
            index = values.index(current) + 1 if current in values else 0
            chosen = st.selectbox(
                FILTER_LABELS[dimension],
                options=["(tous)", *values],
                index=index,
                key=f"filter_{dimension}",
                format_func=phase_label,
            )
            if chosen == "(tous)":
                filters.pop(dimension, None)
            else:
                filters[dimension] = chosen

        if filters and st.button("Réinitialiser les filtres", width="stretch"):
            for dimension in list(choices):
                st.session_state[f"filter_{dimension}"] = "(tous)"
            st.session_state["filters"] = {}
            st.rerun()


@st.cache_data(ttl=86400, show_spinner=False)
def aircraft_photo(icao24: str) -> dict | None:
    """Photo de l'appareil, mise en cache 24 h.

    Une photographie ne change pas d'un jour à l'autre : un cache long évite
    de solliciter Planespotters à chaque clic, et garde la fiche instantanée
    quand on revient sur le même appareil.
    """
    photo = fetch_photo(icao24)
    return None if photo is None else photo.__dict__


def selection_scope(positions: pd.DataFrame) -> str:
    """Empreinte du contenu affiché sur la carte.

    Sert de clé au composant. Streamlit mémorise une sélection comme un RANG
    dans le tableau transmis, pas comme un identifiant d'aéronef : dès que ce
    tableau change - nouveau relevé, ou filtre modifié - le même rang
    désignerait un autre appareil, et la fiche afficherait tranquillement les
    informations d'un avion que personne n'a cliqué. Deux contenus différents
    donnent donc deux composants distincts, et la sélection est remise à zéro
    plutôt que faussée.
    """
    snapshot = positions["snapshot_at"].iloc[0]
    return f"{snapshot:%Y%m%d%H%M%S}_{len(positions)}"


@st.cache_data(ttl=90, show_spinner=False)
def aircraft_history(icao24: str) -> pd.DataFrame:
    """Relevés successifs d'un appareil donné.

    Un appareil n'est pas vu une fois : la médiane est de cinq relevés, et le
    plus suivi en compte quarante-trois. La fiche montrait pourtant un
    instant isolé, alors que la profondeur temporelle est justement ce que ce
    projet accumule. La requête est bornée à l'adresse OACI, donc elle lit
    quelques dizaines de lignes.
    """
    return load(
        f"""
        select snapshot_at, barometric_altitude_ft, ground_speed_kt, flight_phase
        from marts.fct_aircraft_positions
        where aircraft_icao24 = '{icao24}'
        order by snapshot_at
        """  # noqa: S608 - l'adresse OACI est validee par l'appelant
    )


def render_history(icao24: str) -> None:
    """Courbe d'altitude de l'appareil, sur toute la fenêtre d'observation."""
    # Une adresse OACI 24 bits est SIX caractères hexadecimaux et rien
    # d'autre. La valeur vient d'une sélection sur la carte, donc de notre
    # propre donnée, mais elle finit dans une requête : on la valide plutôt
    # que de faire confiance a sa provenance.
    if len(icao24) != 6 or any(c not in "0123456789abcdef" for c in icao24.lower()):
        return

    historique = aircraft_history(icao24.lower())
    if len(historique) < 2:
        st.caption(
            "Un seul relevé pour cet appareil : la courbe apparaît dès le "
            "deuxième passage du collecteur."
        )
        return

    courbe = px.line(
        historique,
        x="snapshot_at",
        y="barometric_altitude_ft",
        markers=True,
        labels={"snapshot_at": "", "barometric_altitude_ft": "Altitude (ft)"},
    )
    courbe.update_traces(line={"width": 2.2, "color": "#22d3ee"}, marker={"size": 5})
    style_fig(courbe, height=190)
    courbe.update_layout(margin={"l": 8, "r": 8, "t": 6, "b": 4}, showlegend=False)
    st.plotly_chart(courbe, width="stretch", config=chart_config())
    st.caption(
        f"{len(historique)} relevés de cet appareil depuis le début de la "
        "collecte. Les points ne sont pas une trajectoire : entre deux "
        "relevés, l'appareil a parcouru des centaines de kilomètres dont rien "
        "n'est observé."
    )


def _picked_aircraft(selection) -> dict | None:
    """Extrait l'appareil clique d'un événement de sélection deck.gl."""
    try:
        objects = selection.selection["objects"]
    except (AttributeError, KeyError, TypeError):
        return None
    for rows in objects.values():
        if rows:
            return rows[0]
    return None


def render_aircraft_card(selection, positions: pd.DataFrame) -> None:
    """Fiche détaillée de l'appareil sélectionné sur la carte.

    Rien ne s'affiche tant qu'aucun appareil n'est cliqué : la fiche est un
    approfondissement, pas un élément permanent qui occuperait la page.

    Streamlit conserve l'objet tel qu'il était AU MOMENT du clic. On le
    rafraîchit donc depuis le dernier relevé : sans cela, altitude et vitesse
    resteraient figées à la valeur du clic alors que la page, elle, continue
    de se recharger.
    """
    aircraft = _picked_aircraft(selection)
    if not aircraft:
        return

    current = positions.loc[positions["aircraft_icao24"] == aircraft.get("aircraft_icao24")]
    if current.empty:
        # Sélection devenue caduque : le calque ne transporte que de quoi
        # dessiner et survoler, donc sans la ligne correspondante il n'y a
        # rien d'honnête à afficher.
        return
    aircraft = current.iloc[0].to_dict()

    # Une colonne vide devient NaN en sortie de pandas, et NaN est "vrai" au
    # sens booléen : sans cette normalisation, `airline or operator` renverrait
    # le NaN et la fiche afficherait "nan" comme nom de compagnie.
    aircraft = {
        key: None if isinstance(val, float) and pd.isna(val) else val
        for key, val in aircraft.items()
    }

    def value(key: str, fallback: str = "-") -> str:
        raw = aircraft.get(key)
        if raw is None or (isinstance(raw, float) and pd.isna(raw)) or raw == "":
            return fallback
        return str(raw)

    def number(key: str, unit: str, decimals: int = 0) -> str:
        raw = aircraft.get(key)
        if raw is None or (isinstance(raw, float) and pd.isna(raw)):
            # Une valeur absente s'affiche comme absente : écrire "0 ft" pour
            # une altitude non transmise inventerait une donnée.
            return "-"
        # Le degré se colle au nombre ; les autres unités s'en séparent.
        espace = "" if unit.startswith("°") else " "
        return f"{float(raw):,.{decimals}f}{espace}{unit}".replace(",", " ")

    def year() -> str:
        raw = aircraft.get("built_year")
        if raw is None or (isinstance(raw, float) and pd.isna(raw)):
            return "-"
        # La jointure avec les positions rend la colonne flottante dès qu'un
        # appareil n'a pas d'année connue : sans conversion, 2011 s'afficherait
        # "2011.0", et un séparateur de milliers en ferait "2 011".
        return str(int(raw))

    icao24 = value("aircraft_icao24", "")
    operator = aircraft.get("operator")
    airline = aircraft.get("airline_name")
    military = looks_military(operator, airline)

    st.divider()
    photo_column, info_column = st.columns([1, 2])

    with photo_column:
        photo = aircraft_photo(icao24) if icao24 else None
        if photo:
            st.image(photo["thumbnail_url"], width="stretch")
            credit = photo.get("photographer") or "photographe inconnu"
            link = photo.get("page_url")
            # Le crédit n'est pas optionnel : les photos ont un auteur.
            st.caption(
                f"Photo : {credit} - [Planespotters]({link})"
                if link
                else f"Photo : {credit} (Planespotters)"
            )
        else:
            st.caption(
                "Aucune photo de cet appareil dans la base Planespotters. "
                "La couverture est bonne pour les avions de ligne, plus rare "
                "pour l'aviation d'affaires et les appareils d'État."
            )

    with info_column:
        # Indicatif et nom d'exploitant viennent des sources externes et sont
        # insérés dans du HTML brut : ils sont échappés, faute de quoi une
        # valeur mal formée - ou malveillante - s'exécuterait dans la page.
        title = html.escape(value("callsign", "(sans indicatif)"))
        subtitle = html.escape(str(airline or operator or "Exploitant inconnu"))
        badge = (
            "<span style='color:#fbbf24;border:1px solid #fbbf24;border-radius:12px;"
            "padding:2px 9px;font-size:.7rem;margin-left:10px'>ÉTAT / MILITAIRE ?</span>"
            if military
            else ""
        )
        st.markdown(
            f"<div style='font-family:JetBrains Mono,monospace;font-size:1.5rem;"
            f"color:#eaf9ff'>{title}{badge}</div>"
            f"<div style='color:#9fb3d1;margin-bottom:.6rem'>{subtitle}</div>",
            unsafe_allow_html=True,
        )

        left, middle, right = st.columns(3)
        left.metric("Immatriculation", value("registration"))
        middle.metric("Type", value("aircraft_type"))
        right.metric("Année", year())

        left, middle, right = st.columns(3)
        left.metric("Altitude", number("barometric_altitude_ft", "ft"))
        middle.metric("Vitesse", number("ground_speed_kt", "kt"))
        right.metric("Cap", number("heading_deg", "°"))

        details = {
            # `manufacturer` est absent pour environ quatre appareils sur dix ;
            # le groupe constructeur, lui, est toujours renseigné.
            "Constructeur": value("manufacturer", "") or value("manufacturer_group"),
            "Modèle": value("model"),
            "Exploitant": operator or "-",
            "Pays d'immatriculation": value("origin_country"),
            "Phase de vol": phase_label(value("flight_phase")),
            "Adresse OACI 24 bits": icao24 or "-",
        }
        st.markdown("\n".join(f"- **{k}** : {v}" for k, v in details.items() if v not in UNKNOWN))
        if military:
            st.caption(
                "L'étiquette État / militaire est une heuristique fondée sur le "
                "nom de l'exploitant : elle peut se tromper dans les deux sens."
            )

    if icao24:
        render_history(icao24)


#: Ce que chaque code de detresse signifie, et la couleur qui va avec.
EMERGENCY_STYLE = {
    "urgence generale": ("Urgence", "#fb7185"),
    "panne radio": ("Panne radio", "#fbbf24"),
    "detournement": ("Détournement", "#a78bfa"),
}


def render_signal_section() -> None:
    """Détresses déclarées et qualité du signal reçu.

    Deux choses que la donnée porte depuis le début sans que rien ne les
    montre : le code transpondeur, et l'age de la position transmise. La
    seconde est une mesure de la qualité de notre propre donnée - c'est
    rarement affiche, et c'est pourtant ce qui dit si la carte est fiable.
    """
    gauche, droite = st.columns([1, 1])

    with gauche:
        st.subheader("Détresses déclarées")
        detresses = load(
            """
            select
                p.emergency_kind, p.snapshot_at, p.callsign, p.origin_country,
                p.aircraft_icao24, a.airline_name, a.aircraft_type
            from marts.fct_aircraft_positions p
            left join marts.dim_aircraft a using (aircraft_icao24)
            where p.emergency_kind is not null
            order by p.snapshot_at desc
            """
        )
        if detresses.empty:
            st.info("Aucun code de détresse observé sur la fenêtre de collecte.")
        else:
            compte = detresses.groupby("emergency_kind")["aircraft_icao24"].nunique()
            colonnes = st.columns(len(EMERGENCY_STYLE))
            for colonne, (code, (libelle, _)) in zip(
                colonnes, EMERGENCY_STYLE.items(), strict=True
            ):
                colonne.metric(libelle, int(compte.get(code, 0)))

            table = detresses.assign(
                Code=detresses["emergency_kind"].map(lambda k: EMERGENCY_STYLE[k][0]),
                Indicatif=detresses["callsign"].fillna("(sans indicatif)"),
                Compagnie=detresses["airline_name"].fillna("-"),
            )[["Code", "Indicatif", "Compagnie", "snapshot_at"]]
            st.dataframe(
                table.rename(columns={"snapshot_at": "Relevé"}),
                width="stretch",
                hide_index=True,
                height=210,
            )
        st.caption(
            "Les codes 7500, 7600 et 7700 sont les trois codes de détresse "
            "normalisés par l'OACI. **À lire comme un signal, pas comme un "
            "fait** : un 7500 résulte presque toujours d'une erreur de "
            "sélection sur le transpondeur, pas d'un détournement."
        )

    with droite:
        st.subheader("Qualité du signal reçu")
        fraicheur = load(
            """
            select
                median(position_age_seconds)              as mediane,
                quantile(position_age_seconds, 0.90)      as p90,
                quantile(position_age_seconds, 0.99)      as p99,
                max(position_age_seconds)                 as maximum,
                avg(case when is_position_stale then 1.0 else 0.0 end) as part_perimee
            from marts.fct_aircraft_positions
            """
        ).iloc[0]

        haut, bas = st.columns(2)
        haut.metric("Âge médian", f"{fraicheur['mediane']:.0f} s")
        bas.metric("9e décile", f"{fraicheur['p90']:.0f} s")
        haut, bas = st.columns(2)
        haut.metric("99e centile", f"{fraicheur['p99']:.0f} s")
        bas.metric("Écartées", f"{100 * fraicheur['part_perimee']:.2f} %")

        distribution = load(
            """
            select
                case
                    when position_age_seconds <= 5   then '5 s'
                    when position_age_seconds <= 30  then '30 s'
                    when position_age_seconds <= 60  then '1 min'
                    when position_age_seconds <= 300 then '5 min'
                    else '> 5 min'
                end as tranche,
                count(*) as positions
            from marts.fct_aircraft_positions
            group by 1
            """
        )
        ordre = ["5 s", "30 s", "1 min", "5 min", "> 5 min"]
        distribution["tranche"] = pd.Categorical(distribution["tranche"], ordre, ordered=True)
        barres = px.bar(
            distribution.sort_values("tranche"),
            x="tranche",
            y="positions",
            labels={"tranche": "Position transmise il y à moins de", "positions": ""},
        )
        # La dernière tranche est celle qu'on ecarte de la carte : elle se
        # distingue, sinon le graphe ne dit pas ce qui a ete decide.
        barres.update_traces(
            marker={
                "color": [
                    "#fb7185" if t == "> 5 min" else "#22d3ee"
                    for t in distribution.sort_values("tranche")["tranche"]
                ]
            }
        )
        style_fig(barres, height=240)
        st.plotly_chart(barres, width="stretch", config=chart_config())
        st.caption(
            f"Écart entre l'instant du relevé et la dernière position émise par "
            f"l'appareil. La médiane est d'une seconde, mais la queue de "
            f"distribution monte à {fraicheur['maximum'] / 3600:.0f} h : OpenSky "
            "conserve le dernier point connu d'un appareil sorti de couverture. "
            "Ces positions sont **écartées de la carte** plutôt que dessinées "
            "comme du trafic courant."
        )


def render_fleet_age() -> None:
    """Age des flottes par compagnie.

    Deuxième question chiffree du projet, après la corrélation trafic / NO2.
    Elle n'était pas exploitable tant que le rattachement aux compagnies
    reposait sur le seul préfixe d'indicatif : le classement sortait des
    compagnies disparues depuis 1989.

    Le biais de sélection est réel et affiche : l'année de construction n'est
    connue que pour la moitie de la base aéronefs, et rien ne garantit que
    les appareils dates soient representatifs des autres.
    """
    ages = load(
        """
        select
            airline_name                            as compagnie,
            count(*)                                as appareils,
            round(2026 - avg(built_year), 1)        as age_moyen
        from marts.dim_aircraft
        where airline_name is not null and built_year is not null
        group by 1
        having count(*) >= 25
        order by age_moyen desc
        """
    )
    if len(ages) < 6:
        st.caption(
            "Pas encore assez d'appareils datés par compagnie pour comparer "
            "les flottes. La base se remplit avec la collecte."
        )
        return

    st.subheader("Âge des flottes")

    extremes = pd.concat([ages.head(6), ages.tail(6)]).drop_duplicates(subset="compagnie")
    graphe = px.bar(
        extremes.sort_values("age_moyen"),
        x="age_moyen",
        y="compagnie",
        orientation="h",
        labels={"age_moyen": "Âge moyen de la flotte (années)", "compagnie": ""},
        text="appareils",
    )
    # Les deux extremes racontent l'histoire : le fret vole vieux, le
    # low-cost vole neuf. Une couleur par groupe le rend lisible d'un coup.
    mediane = ages["age_moyen"].median()
    graphe.update_traces(
        marker={
            "color": [
                "#fbbf24" if a > mediane else "#34d399"
                for a in extremes.sort_values("age_moyen")["age_moyen"]
            ]
        },
        texttemplate="%{text} appareils",
        textposition="outside",
        textfont={"color": "#9fb3d1", "size": 10},
        cliponaxis=False,
    )
    style_fig(graphe, height=430)
    graphe.update_layout(margin={"l": 8, "r": 70, "t": 8, "b": 6})
    st.plotly_chart(graphe, width="stretch", config=chart_config())

    plus_vieille = ages.iloc[0]
    plus_jeune = ages.iloc[-1]
    st.caption(
        f"Sur {len(ages)} compagnies d'au moins 25 appareils datés, l'écart va "
        f"de **{plus_jeune['age_moyen']:.0f} ans** ({plus_jeune['compagnie']}) à "
        f"**{plus_vieille['age_moyen']:.0f} ans** ({plus_vieille['compagnie']}), "
        f"soit un facteur {plus_vieille['age_moyen'] / plus_jeune['age_moyen']:.1f}. "
        "Le fret et le régional exploitent des appareils convertis en fin de "
        "vie ; le low-cost renouvelle pour la consommation de carburant. "
        "**Biais à connaître** : l'année de construction n'est renseignée que "
        "pour la moitié de la base aéronefs, et les appareils datés ne sont "
        "pas forcément représentatifs des autres."
    )


def apply_filters(frame: pd.DataFrame) -> pd.DataFrame:
    """Restreint un tableau de positions aux filtres actifs."""
    for dimension, value in active_filters().items():
        if dimension in frame.columns:
            frame = frame[frame[dimension] == value]
    return frame


def render_filter_bar() -> None:
    """Rappelle les filtres actifs et permet de les lever."""
    filters = active_filters()
    if not filters:
        st.caption(
            "Astuce : les filtres de la barre latérale (phase, constructeur, "
            "pays, compagnie) restreignent la carte et les indicateurs du "
            "dernier relevé."
        )
        return

    resume = " · ".join(
        f"{FILTER_LABELS.get(k, k)} : **{phase_label(v)}**" for k, v in filters.items()
    )
    st.markdown(f"Filtres actifs : {resume}")


# Le panneau de filtres interroge l'entrepôt pour proposer les valeurs
# existantes : il ne peut donc être construit qu'une fois celui-ci prêt.
# Il vit hors du fragment, dans le corps du script, pour qu'un changement de
# filtre rejoue la page entière et non le seul bloc de rendu.
render_filter_panel()


# ---------------------------------------------------------------------------
# Rendu
# ---------------------------------------------------------------------------
def status_chip(level: str, text: str) -> None:
    st.markdown(
        f'<div class="hud-status {level}"><span class="dot"></span>{text}</div>',
        unsafe_allow_html=True,
    )


def render_snapshot_series() -> None:
    """Trafic relevé par relevé : la granularité réelle de la collecte."""
    # -- Série par snapshot ------------------------------------------------
    st.subheader("Trafic relevé par relevé")

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
            "Un seul relevé pour l'instant. La courbe apparaît dès le "
            f"deuxième, soit environ {SCHEDULE_MINUTES} min après le premier.",
        )
    else:
        # px.line nomme les traces d'après les COLONNES : `labels` ne les
        # renomme pas. On renomme donc les colonnes, sur une copie qui ne
        # sert qu'à l'affichage.
        courbe = per_snapshot.rename(columns={"aeronefs": "tous appareils", "au_sol": "au sol"})
        series = px.line(
            courbe,
            x="snapshot_at",
            y=["tous appareils", "au sol"],
            markers=True,
            labels={
                "snapshot_at": "Instant du relevé (UTC)",
                "value": "Aéronefs",
                "variable": "",
            },
            color_discrete_map={"tous appareils": "#00e5ff", "au sol": "#3b5a8a"},
        )
        series.update_traces(
            line={"width": 2.6},
            hovertemplate="<b>%{y:.0f}</b> %{fullData.name}<extra></extra>",
        )
        style_fig(series, height=340)
        series.update_layout(legend={"orientation": "h", "y": 1.14, "x": 0}, hovermode="x unified")
        # Pas de "spike" vertical : `hovermode="x unified"` désigne déjà le
        # relevé survolé et en affiche les valeurs. Une barre en plus est
        # redondante, et Plotly la dessine large et opaque.
        series.update_xaxes(showspikes=False)
        st.plotly_chart(series, width="stretch", config=chart_config())
        st.caption(
            f"{len(per_snapshot)} relevés. Chaque point est une exécution du "
            "pipeline : la granularité réelle de la collecte."
        )

    st.divider()


def render_radar() -> None:
    """Carte du dernier relevé, fiche appareil et répartition des phases."""
    # -- Carte du dernier relevé -------------------------------------------
    st.subheader("Radar // dernier relevé")

    # Jointure avec la dimension aéronef : elle apporte le constructeur et la
    # compagnie, qui servent de dimensions de filtrage sur la carte.
    # Les positions perimees sont ECARTEES de la carte. OpenSky conserve le
    # dernier point connu d'un appareil sorti de couverture : dessiner sur une
    # carte du trafic courant un point vieux de plusieurs heures, c'est
    # affirmer une presence qui n'a pas ete observée. Le compte des positions
    # ecartees est affiche sous la carte plutôt que passe sous silence.
    latest_all = load(
        """
        select
            p.latitude, p.longitude, p.callsign, p.origin_country, p.heading_deg,
            p.barometric_altitude_ft, p.ground_speed_kt, p.flight_phase,
            p.aircraft_icao24, p.snapshot_at, p.emergency_kind,
            coalesce(a.manufacturer_group, 'Inconnu') as manufacturer_group,
            a.airline_name, a.registration, a.aircraft_type, a.manufacturer,
            a.model, a.operator, a.built_year, a.airline_country, a.airline_source
        from marts.fct_aircraft_positions p
        left join marts.dim_aircraft a using (aircraft_icao24)
        where p.snapshot_at = (select max(snapshot_at) from marts.fct_aircraft_positions)
          and not p.is_position_stale
        """
    )
    ecartees = load(
        """
        select count(*) as n
        from marts.fct_aircraft_positions
        where snapshot_at = (select max(snapshot_at) from marts.fct_aircraft_positions)
          and is_position_stale
        """
    ).iloc[0]["n"]
    latest = apply_filters(latest_all)

    render_filter_bar()

    map_column, phase_column = st.columns([3, 1])

    with map_column:
        if latest.empty:
            st.info("Aucune position sur le dernier snapshot.")
        else:
            # Chaque appareil est dessine comme une silhouette d'avion orientée
            # selon son cap réel : l'image donne alors les flux (couloirs
            # transatlantiques, approches d'aéroport) qu'un simple point ne
            # montre pas. La couleur reste la phase de vol.
            #
            # deck.gl dessine sur GPU : les 8 000 aéronefs d'un relevé mondial
            # passent sans peine, là où Plotly (rendu SVG) imposait un
            # échantillonnage. On affiche donc la totalité du relevé.
            # Le calque ne transporte QUE le strict nécessaire au rendu et à
            # l'infobulle. Tout le reste - immatriculation, modèle, année... -
            # est relu côté serveur dans `latest` au moment du clic : inutile
            # d'expédier au navigateur, treize mille fois, des informations
            # dont une seule ligne servira.
            plotted = pd.DataFrame(
                {
                    "longitude": latest["longitude"],
                    "latitude": latest["latitude"],
                    "angle": -latest["heading_deg"].fillna(0),
                    "colour": [PHASE_RGB.get(p, (150, 160, 180)) for p in latest["flight_phase"]],
                    "icon": ICON_NAME,
                    "callsign": latest["callsign"].fillna("(sans indicatif)"),
                    "origin_country": latest["origin_country"],
                    "flight_phase": latest["flight_phase"].map(phase_label),
                    "aircraft_icao24": latest["aircraft_icao24"],
                    "altitude_txt": latest["barometric_altitude_ft"].map(
                        lambda v: "-" if pd.isna(v) else f"{v:,.0f} ft".replace(",", " ")
                    ),
                    "vitesse_txt": latest["ground_speed_kt"].map(
                        lambda v: "-" if pd.isna(v) else f"{v:.0f} kt"
                    ),
                }
            )

            # deck.gl reçoit le calque sous forme de JSON strict, analyse par le
            # navigateur. Or une colonne vide en base devient un NaN pandas, que
            # Python sérialise en littéral `NaN` - refusé par JSON.parse, ce qui
            # ferait disparaître la carte entière. On repasse donc les colonnes
            # facultatives en `null` avant l'envoi.
            for column in ("callsign", "origin_country", "flight_phase", "aircraft_icao24"):
                plotted[column] = (
                    plotted[column].astype(object).where(plotted[column].notna(), None)
                )

            # Cadrage automatique sur la donnée réelle plutôt qu'un zoom fixe :
            # la même page reste lisible que la zone soit la France ou le monde.
            #
            # Le plancher de zoom dépend de la largeur disponible. Une tuile
            # deck.gl fait 512 pixels : au zoom 1 le globe en occupe 1 024, ce
            # qui tient sur un écran de bureau mais déborde très largement des
            # 343 pixels d'un téléphone - on n'y voyait que les Amériques,
            # l'Europe, pourtant la zone la plus dense, restait hors champ.
            handheld = is_handheld()
            lat_span = plotted["latitude"].max() - plotted["latitude"].min()
            lon_span = plotted["longitude"].max() - plotted["longitude"].min()
            span = max(lat_span, lon_span / 1.8, 1.0)
            zoom_floor = -0.6 if handheld else 1.0
            zoom = max(zoom_floor, min(6.5, 7.2 - math.log2(span)))
            map_height = 400 if handheld else 560

            # Attention : pydeck transforme toute CHAÎNE de caractères en
            # accesseur de colonne. Passer `size_units="pixels"` produisait
            # `sizeUnits: @@=pixels`, soit "lis la colonne pixels" - colonne
            # inexistante, d'où un dimensionnement aberrant. Les unités en
            # pixels étant déjà le défaut de deck.gl, on ne les précise pas ;
            # seules des valeurs NUMÉRIQUES sont passées ici.
            layer = pdk.Layer(
                "IconLayer",
                data=plotted,
                get_icon="icon",
                # `PdkString` marque une chaîne LITTÉRALE. Sans elle, pydeck
                # applique sa règle habituelle - toute chaîne devient un
                # accesseur de colonne - et émet `iconAtlas: @@=data:image/png...`,
                # que deck.gl tente alors d'évaluer comme une expression. Même
                # piège que `size_units="pixels"` en son temps.
                icon_atlas=PdkString(AIRCRAFT_ICON_ATLAS),
                icon_mapping=AIRCRAFT_ICON_MAPPING,
                get_position=["longitude", "latitude"],
                get_angle="angle",
                get_color="colour",
                # Identifiant FIXE. pydeck en génère un aléatoire à chaque
                # construction ; la sélection étant rattachée à l'identifiant
                # du calque, elle serait perdue à chaque rechargement de page.
                id="aeronefs",
                get_size=AIRCRAFT_ICON_SIZE,
                size_min_pixels=5,
                size_max_pixels=AIRCRAFT_ICON_SIZE,
                opacity=0.85,
                pickable=True,
                # Surbrillance au survol, calculée sur le GPU : le retour est
                # immédiat, sans aller-retour avec le serveur. On sait ce que
                # l'on s'apprête à sélectionner avant de cliquer.
                auto_highlight=True,
                highlight_color=[255, 255, 255, 220],
            )
            # `on_select` rend le calque cliquable : contrairement aux
            # camemberts Plotly, deck.gl expose bien l'objet sélectionné.
            deck = pdk.Deck(
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
            )
            # Tolérance de visée. Une silhouette fait neuf pixels : exiger le
            # pixel exact rend la sélection frustrante. deck.gl cherche dans un
            # rayon autour du point désigné et retient l'appareil le plus
            # proche. Un doigt couvre une quarantaine de pixels contre deux ou
            # trois pour un curseur : viser au pixel près y est impossible, le
            # rayon est donc nettement plus large sur écran tactile.
            deck.picking_radius = 20 if handheld else 8

            selection = st.pydeck_chart(
                deck,
                height=map_height,
                on_select="rerun",
                selection_mode="single-object",
                key=f"carte_{selection_scope(latest)}",
            )
            compte = f"{len(plotted):,} aéronefs du dernier relevé. ".replace(",", " ")
            if ecartees:
                compte += (
                    f"{int(ecartees)} position(s) écartée(s), transmises il y a "
                    "plus de cinq minutes. "
                )
            biais = (
                "**Les zones vides ne sont pas des zones sans trafic** : le "
                "réseau OpenSky repose sur des récepteurs bénévoles, denses en "
                "Europe et en Amérique du Nord, rares ailleurs. Les volumes ne "
                "sont pas comparables d'une région à l'autre."
            )
            if handheld:
                # Six lignes de légende sous une carte de 400 pixels, c'est un
                # mur de texte. On garde l'avertissement sur le biais - il
                # conditionne la lecture de la carte, on ne peut pas le
                # supprimer - et on renvoie le reste à la version large.
                st.caption(compte + "Toucher un appareil affiche sa fiche.")
                st.caption(biais)
            else:
                st.caption(
                    compte + "Cliquer sur un appareil affiche sa fiche. Chaque "
                    "silhouette est orientée selon le cap réel ; la couleur "
                    "indique la phase de vol. " + biais
                )
            render_aircraft_card(selection, latest)

    with phase_column:
        if not latest.empty:
            # Le camembert se calculé sur les données NON filtrées par phase :
            # sinon, cliquer sur "croisiere" ne laisserait qu'une seule part et
            # l'on ne pourrait plus revenir en arriere depuis le graphe.
            phase_source = latest_all.copy()
            for dimension, value in active_filters().items():
                if dimension != "flight_phase" and dimension in phase_source.columns:
                    phase_source = phase_source[phase_source[dimension] == value]

            phases = phase_source.groupby("flight_phase").size().reset_index(name="aeronefs")
            phases["libelle"] = phases["flight_phase"].map(phase_label)
            selected_phase = active_filters().get("flight_phase")
            donut = px.pie(
                phases,
                names="libelle",
                values="aeronefs",
                hole=0.62,
                color="flight_phase",
                color_discrete_map=PHASE_COLOURS,
            )
            donut.update_traces(
                textinfo="label+value",
                textfont={"family": "Inter, sans-serif", "size": 11},
                marker={"line": {"color": "#04070e", "width": 2}},
                # La part filtrée est détachée : le filtre actif se voit sur le
                # graphe lui-même, pas seulement dans le bandeau.
                pull=[0.08 if p == selected_phase else 0 for p in phases["flight_phase"]],
            )
            style_fig(donut, height=240)
            donut.update_layout(showlegend=False, margin={"l": 0, "r": 0, "t": 6, "b": 0})
            st.plotly_chart(donut, width="stretch", config=chart_config())

            st.metric(
                "Altitude médiane",
                f"{latest['barometric_altitude_ft'].median():,.0f} ft".replace(",", " "),
            )
            st.metric("Vitesse médiane", f"{latest['ground_speed_kt'].median():.0f} kt")


def render_hourly_trend() -> None:
    """Agrégat horaire : le creux nocturne et le pic du matin."""
    # -- Série horaire -----------------------------------------------------
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
            "L'agrégat horaire demande au moins deux heures de collecte. "
            "En attendant, la courbe relevé par relevé ci-dessus fait le travail.",
        )
    else:
        trend = px.area(
            hourly,
            x="traffic_hour",
            y="positions",
            labels={"traffic_hour": "Heure (UTC)", "positions": "Positions collectées"},
        )
        trend.update_traces(
            line={"color": "#00e5ff", "width": 2.2},
            fillcolor="rgba(0,229,255,0.14)",
        )
        style_fig(trend, height=300)
        st.plotly_chart(trend, width="stretch", config=chart_config())
        st.caption(
            "Agrégat issu de `fct_traffic_hourly`. C'est ici que le creux "
            "nocturne et le pic du matin deviennent visibles."
        )


def render_rankings() -> None:
    """Classements par pays d'immatriculation et par aéroport."""
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
        selected_country = active_filters().get("origin_country")
        chart = px.bar(
            countries.sort_values("positions"),
            x="positions",
            y="origin_country",
            orientation="h",
            labels={"positions": "Positions", "origin_country": ""},
        )
        # La barre filtrée passe en vert : le filtre actif se repère d'un
        # coup d'œil sur le graphe, sans lire le bandeau.
        chart.update_traces(
            marker={
                "color": [
                    "#34d399" if c == selected_country else "#22d3ee"
                    for c in countries.sort_values("positions")["origin_country"]
                ],
                "opacity": 0.9,
            }
        )
        style_fig(chart, height=420)
        st.plotly_chart(chart, width="stretch", config=chart_config())

    with airports_column:
        st.subheader("Aéroports les plus actifs")
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
        # Les en-tetes viennent des alias SQL : on les renomme pour
        # l'affichage plutôt que d'accentuer la requête, dont les noms de
        # colonnes servent aussi de clés côté Python.
        st.dataframe(
            airports.rename(
                columns={
                    "aeroport": "Aéroport",
                    "ville": "Ville",
                    "aeronefs": "Aéronefs",
                    "en_approche": "En approche",
                    "en_montee": "En montée",
                    "distance_moy_km": "Distance moy. (km)",
                }
            ),
            width="stretch",
            hide_index=True,
            height=420,
        )
        st.caption(
            "Les colonnes *en approche* et *en montée* sont inférées du taux "
            "de montée : ADS-B ne publié pas de plan de vol."
        )


def render_fleet() -> None:
    """Compagnies, constructeurs, âge des flottes et modèles."""
    st.subheader("Compagnies et flotte")
    airline_rows = load(
        "select count(*) as n from marts.fct_airline_airport_activity "
        "where airline_name is not null"
    ).iloc[0]["n"]

    if not airline_rows:
        st.info(
            "Pas encore de données compagnies. La troisième source (base "
            "aéronefs OpenSky + compagnies OpenFlights) se remplit avec le pipeline.",
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
                "Part de marché des compagnies à l'aéroport",
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
                labels={"aeronefs": "Aéronefs distincts", "airline_name": ""},
            )
            selected_airline = active_filters().get("airline_name")
            bar.update_traces(
                marker={
                    "color": [
                        "#22d3ee" if a == selected_airline else "#34d399"
                        for a in here.sort_values("aeronefs")["airline_name"]
                    ],
                    "opacity": 0.9,
                }
            )
            style_fig(bar, height=360)
            st.plotly_chart(bar, width="stretch", config=chart_config())

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
            selected_maker = active_filters().get("manufacturer_group")
            donut.update_traces(
                textinfo="label+percent",
                textfont={"family": "Inter, sans-serif", "size": 11},
                marker={"line": {"color": "#04070e", "width": 2}},
                pull=[0.08 if m == selected_maker else 0 for m in makers["manufacturer_group"]],
            )
            style_fig(donut, height=360)
            donut.update_layout(
                showlegend=False,
                title={"text": "Constructeurs (Airbus vs Boeing...)", "y": 0.97},
                margin={"l": 8, "r": 8, "t": 46, "b": 6},
            )
            st.plotly_chart(donut, width="stretch", config=chart_config())
            st.caption(
                "Type et constructeur issus de la base aéronefs OpenSky. La "
                "compagnie vient du code d'exploitant déclaré quand il "
                "existe, du préfixe d'indicatif sinon, et seulement si ce "
                "préfixe est attesté ailleurs comme code d'exploitant."
            )

        render_fleet_age()

        # Top modèles d'avions (tous les types répertoriés dans la base).
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
            # Étiquette lisible : "Boeing B738" plutôt que le code seul.
            models["label"] = [
                f"{m} {t}" if m else t
                for m, t in zip(models["manufacturer"], models["aircraft_type"], strict=False)
            ]
            model_chart = px.bar(
                models.sort_values("aeronefs"),
                x="aeronefs",
                y="label",
                orientation="h",
                labels={"aeronefs": "Aéronefs distincts", "label": ""},
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
                title={"text": "Modèles d'avions les plus vus", "y": 0.97},
                margin={"l": 8, "r": 44, "t": 48, "b": 6},
            )
            st.plotly_chart(model_chart, width="stretch", config=chart_config())


@st.cache_data(ttl=600, show_spinner=False)
def air_quality_uncertainty() -> dict | None:
    """Intervalle et p-valeur du resultat phare, par bootstrap sur grappes.

    Deux mille reechantillonnages sur un panel de quelques centaines de
    lignes : moins de trois secondes, mise en cache dix minutes.
    """
    panel = load(
        """
        select airport_iata_code, distinct_aircraft, no2_ugm3
        from marts.fct_airport_hourly_air_quality
        where no2_ugm3 is not null
        """
    )
    if len(panel) < 30 or panel["airport_iata_code"].nunique() < 3:
        return None
    res = estimate(panel, "distinct_aircraft", "no2_ugm3", "airport_iata_code")
    return {
        "r": res.correlation,
        "bas": res.low,
        "haut": res.high,
        "p": res.p_value,
        "grappes": res.clusters,
        "observations": res.observations,
        "exclut_zero": res.excludes_zero,
    }


def render_uncertainty() -> None:
    """Ce que vaut le resultat phare, au-dela du nombre.

    Un coefficient publie nu ne dit pas s'il est distinguable du hasard.
    Deux precautions sont imposees par la structure du panel : les heures
    d'un meme aeroport ne sont pas independantes, et il n'y a qu'une
    quinzaine d'aeroports. On reechantillonne donc les AEROPORTS, et on teste
    en permutant le polluant a l'interieur de chacun.
    """
    mesure = air_quality_uncertainty()
    if mesure is None:
        return

    st.markdown("**Ce que vaut ce chiffre**")

    gauche, milieu, droite = st.columns(3)
    gauche.metric(
        "IC à 95 %",
        f"[{mesure['bas']:+.2f} ; {mesure['haut']:+.2f}]",
        help=(
            "Bootstrap par grappes : on retire des AÉROPORTS avec remise, pas "
            "des heures. Les heures d'un même aéroport se ressemblent trop "
            "pour compter comme des témoignages indépendants."
        ),
    )
    milieu.metric(
        "p (permutation)",
        f"{mesure['p']:.3f}" if mesure["p"] >= 0.001 else "< 0,001",
        help=(
            "Le NO2 est permuté À L'INTÉRIEUR de chaque aéroport, ce qui "
            "conserve la structure et ne détruit que l'appariement heure par "
            "heure. Proportion des tirages où le hasard fait au moins aussi "
            "bien que la donnée réelle."
        ),
    )
    droite.metric("Grappes", int(mesure["grappes"]))

    verdict = (
        "L'intervalle **exclut zéro** et la permutation ne reproduit "
        "quasiment jamais un lien aussi marqué : l'inversion de signe n'est "
        "pas un accident d'échantillonnage."
        if mesure["exclut_zero"] and mesure["p"] < 0.05
        else "L'intervalle **contient zéro** : à ce stade de la collecte, on "
        "ne peut pas distinguer ce lien du hasard."
    )
    st.caption(
        f"{verdict} Sur {int(mesure['observations'])} heures-aéroport réparties "
        f"en {int(mesure['grappes'])} aéroports. **La limite à garder en tête** : "
        f"{int(mesure['grappes'])} grappes, c'est peu, et un intervalle calculé "
        "sur si peu de groupes est lui-même incertain. Il vaut mieux que le "
        "chiffre nu qui figurait ici auparavant, il ne vaut pas une étude."
    )


def render_air_quality() -> None:
    """Deuxième source : le trafic se lit-il dans le NO2 au sol ?"""
    st.subheader("Trafic et qualité de l'air")
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
            "Pas encore assez de données croisées trafic / qualité de l'air. "
            "La deuxième source (Open-Météo) se remplit avec le pipeline.",
        )
    else:
        left, right = st.columns([1, 2])
        with left:
            st.metric(
                "r brut",
                f"{air_quality['r_naive']:+.2f}",
                help=(
                    "Coefficient de corrélation de Pearson entre le nombre "
                    "d'avions et le NO2, par heure. Sans unité, de -1 à +1 "
                    "(0 = aucun lien). Le NO2 est mesuré en µg/m³."
                ),
            )
            st.metric(
                "r intra-aéroport",
                f"{air_quality['r_within']:+.2f}",
                help=(
                    "Même coefficient, après retrait de la moyenne de chaque "
                    "aéroport. La corrélation brute, positive, s'inverse : le "
                    "lien n'est qu'un artefact 'entre aéroports'."
                ),
            )
            st.caption(
                "r = coefficient de corrélation de Pearson (sans unité, -1 à +1). "
                "À l'échelle horaire, le trafic aérien n'est pas un prédicteur "
                "détectable du NO2 au sol. Analyse complète : "
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
                    "no2_ugm3": "NO2 au sol (µg/m³)",
                    "airport_iata_code": "Aéroport",
                },
            )
            scatter.update_traces(marker={"size": 7, "opacity": 0.75})
            style_fig(scatter, height=360)
            st.plotly_chart(scatter, width="stretch", config=chart_config())

        render_uncertainty()


def render_diurnal_cycle() -> None:
    """Cycle jour/nuit du trafic, en heure solaire locale.

    La tendance horaire est en UTC, ce qui melange tous les fuseaux : le
    matin japonais y tombe au même endroit que la nuit americaine, et le
    cycle s'annule. En ramenant chaque position à l'heure SOLAIRE de sa
    longitude - quinze degrés par heure - le rythme reapparait.

    C'est une approximation : l'heure solaire ignore les fuseaux
    administratifs et l'heure d'été. Pour lire un cycle jour/nuit, c'est
    justement ce qu'il faut, puisque le soleil ne connaît pas les decrets.
    """
    st.subheader("Cycle jour / nuit")

    cycle = load(
        """
        select
            cast(floor(((extract(hour from snapshot_at) + longitude / 15.0) + 24) % 24) as int)
                     as heure_locale,
            count(*) as positions
        from marts.fct_aircraft_positions
        where not is_position_stale
        group by 1
        order by 1
        """
    )
    if len(cycle) < 12:
        st.caption("Pas encore assez d'heures couvertes pour dessiner le cycle.")
        return

    courbe = px.area(
        cycle,
        x="heure_locale",
        y="positions",
        labels={"heure_locale": "Heure solaire locale", "positions": "Positions"},
    )
    courbe.update_traces(
        line={"color": "#22d3ee", "width": 2.4},
        fillcolor="rgba(34,211,238,0.14)",
    )
    style_fig(courbe, height=300)
    courbe.update_xaxes(dtick=3, ticksuffix=" h")
    st.plotly_chart(courbe, width="stretch", config=chart_config())

    pic = cycle.loc[cycle["positions"].idxmax()]
    creux = cycle.loc[cycle["positions"].idxmin()]
    st.caption(
        f"Le trafic culmine vers **{int(pic['heure_locale'])} h** locale et "
        f"tombe au plus bas vers **{int(creux['heure_locale'])} h**, dans un "
        f"rapport de **{pic['positions'] / creux['positions']:.1f} pour 1**. "
        "L'heure est déduite de la longitude (quinze degrés par heure), pas "
        "du fuseau administratif. Comme la couverture ADS-B est très "
        "majoritairement européenne et nord-américaine, ce cycle est d'abord "
        "celui de ces deux régions."
    )


def render_collection_punctuality() -> None:
    """Ponctualité réelle de la collecte, mesurée et non supposée.

    Le cron est déclaré toutes les trente minutes. GitHub exécute les tâches
    planifiées "au mieux", et l'écart entre deux relevés est une donnée que
    le pipeline produit sur lui-même. L'afficher plutôt que la cadence
    nominale, c'est la difference entre un tableau de bord qui decrit ce qui
    devrait arriver et un qui decrit ce qui arrive.
    """
    st.subheader("Ponctualité de la collecte")

    ecarts = load(
        """
        with releves as (
            select distinct snapshot_at from marts.fct_aircraft_positions
        )
        select epoch(snapshot_at - lag(snapshot_at) over (order by snapshot_at)) / 60 as minutes
        from releves
        qualify minutes is not null
        """
    )
    if len(ecarts) < 5:
        st.caption("Trop peu de relevés pour mesurer la régularité.")
        return

    gauche, milieu, droite = st.columns(3)
    gauche.metric("Écart médian", f"{ecarts['minutes'].median():.0f} min")
    milieu.metric("9e décile", f"{ecarts['minutes'].quantile(0.90):.0f} min")
    droite.metric("Dans les 45 min", f"{100 * (ecarts['minutes'] <= 45).mean():.0f} %")

    # Des TRANCHES et non un histogramme. Les écarts vont de la minute à
    # plusieurs dizaines d'heures : une échelle linéaire écrase tout le corps
    # de la distribution contre l'axe, et un axe logarithmique ne sauve rien
    # puisque Plotly bine en linéaire AVANT de tracer - les barres tombent
    # alors à côté de leur place, jusqu'à disparaître.
    bornes = [0, SCHEDULE_MINUTES + 5, 60, 120, 240, float("inf")]
    etiquettes = [
        f"moins de {SCHEDULE_MINUTES + 5} min",
        f"{SCHEDULE_MINUTES + 5} min - 1 h",
        "1 - 2 h",
        "2 - 4 h",
        "plus de 4 h",
    ]
    tranches = (
        pd.cut(ecarts["minutes"], bins=bornes, labels=etiquettes, right=False)
        .value_counts()
        .reindex(etiquettes)
        .reset_index()
    )
    tranches.columns = ["tranche", "intervalles"]

    barres = px.bar(
        tranches,
        x="tranche",
        y="intervalles",
        labels={"tranche": "Écart entre deux relevés", "intervalles": "Intervalles"},
        text="intervalles",
    )
    # La première tranche est la seule conforme à la cadence annoncee : elle
    # se distingue, sinon le graphe ne dit pas ou est la cible.
    barres.update_traces(
        marker={
            "color": ["#34d399"] + ["#3b82f6"] * (len(etiquettes) - 1),
            "opacity": 0.9,
        },
        textposition="outside",
        textfont={"color": "#9fb3d1", "size": 10},
        cliponaxis=False,
    )
    style_fig(barres, height=280)
    barres.update_layout(margin={"l": 8, "r": 8, "t": 18, "b": 6})
    st.plotly_chart(barres, width="stretch", config=chart_config())
    st.caption(
        f"Cadence déclarée : {SCHEDULE_MINUTES} min. Écart médian réellement "
        f"observé : **{ecarts['minutes'].median():.0f} min**, soit plus du "
        "double. GitHub exécute les tâches planifiées au mieux, et dépriorise "
        "les dépôts publics peu actifs. Ce n'est pas une panne, c'est le prix "
        "d'un ordonnanceur gratuit : les seuils du bandeau de fraîcheur sont "
        "calibrés sur cette distribution et non sur la cadence théorique."
    )


def render_coverage() -> None:
    """Couverture geographique du réseau ADS-B, chiffree.

    Le biais d'observation est repete en légende sous la carte, mais une
    phrase ne se vérifie pas. Ces chiffres-la, si.
    """
    st.subheader("Couverture du réseau")

    couverture = load(
        """
        select
            case
                when longitude between -30 and 45   and latitude between 35 and 72  then 'Europe'
                when longitude between -170 and -50 and latitude between 15 and 72  then 'Amerique du Nord'
                when longitude between 45 and 150   and latitude between 0 and 55   then 'Asie'
                when longitude between -90 and -30  and latitude between -60 and 15 then 'Amerique du Sud'
                when longitude between 110 and 180  and latitude between -50 and 0  then 'Oceanie'
                when longitude between -20 and 55   and latitude between -40 and 35 then 'Afrique'
                else 'Autre'
            end      as region,
            count(*) as positions
        from marts.fct_aircraft_positions
        where not is_position_stale
        group by 1
        order by positions desc
        """
    )
    barres = px.bar(
        couverture.sort_values("positions"),
        x="positions",
        y="region",
        orientation="h",
        labels={"positions": "Positions collectées", "region": ""},
    )
    barres.update_traces(marker={"color": "#22d3ee", "opacity": 0.9})
    style_fig(barres, height=290)
    st.plotly_chart(barres, width="stretch", config=chart_config())

    total = couverture["positions"].sum()
    deux_premieres = couverture.head(2)["positions"].sum()
    st.caption(
        f"**{100 * deux_premieres / total:.0f} % des positions viennent de deux "
        "régions**, l'Europe et l'Amérique du Nord. OpenSky repose sur des "
        "récepteurs installes par des bénévoles : la carte mesure autant la "
        "densite des récepteurs que celle du trafic. **Conséquence directe** : "
        "comparer des volumes entre régions n'a pas de sens ici, et les "
        "analyses de ce projet restent valables à l'interieur d'une région, "
        "pas entre elles."
    )


ARCHITECTURE = """
Le tableau de bord ne lit que la couche **marts** d'un entrepôt DuckDB. Il
n'ouvre jamais un fichier brut et ne recalcule jamais une agrégation : si une
définition métier change, elle est corrigée dans un modèle dbt, testée et
versionnée, pas dans une page.

```
OpenSky      ---.                bronze         silver             gold
OurAirports  ---|  ingestion --> Parquet --> staging --> intermediate --> marts --> ici
Open-Météo   ---|  (Python)      sur R2      (vues)        (vues)       (tables)
OpenFlights  ---'
```

- **Ingestion** en Python, un instantané par exécution, écrit en Parquet sur
  un stockage objet compatible S3. Le lac est immuable : on peut rejouer
  n'importe quelle transformation sans rappeler les sources.
- **Transformation** avec dbt. Les couches intermédiaires sont des vues, qui
  ne coûtent rien en stockage ; seules les tables de faits et de dimensions
  sont matérialisées, car le tableau de bord les lit en boucle.
- **La table de faits est incrémentale**, en `delete+insert` sur la clé
  (appareil, instant) : rejouer un relevé remplace ses lignes au lieu de les
  dupliquer, donc chaque exécution est idempotente.
- **Orchestration** par Dagster en local, par GitHub Actions en production.
  Le graphe de lignée va du fichier Parquet jusqu'aux marts.
"""

CONTROLES = """
- unicité et non-nullité des clés à chaque couche ;
- plages de plausibilité physique (altitude, vitesse, coordonnées), en
  avertissement et non en erreur : une valeur aberrante de capteur est un
  fait, pas un bogue du pipeline ;
- intégrité référentielle entre les faits et les dimensions ;
- fraîcheur des sources, qui alerte si l'ingestion s'est arrêtée.

Un test dur en échec fait passer le workflow au rouge et interrompt la
publication : la donnée fausse ne remplace pas la donnée juste.
"""

LIMITES = """
- **Ce n'est pas du temps réel.** Le pipeline est en lots : la série ne se
  rafraîchit pas, elle s'accumule. Chaque point est une exécution.
- **Ce ne sont pas des trajectoires.** Entre deux relevés, un appareil
  parcourt des centaines de kilomètres dont rien n'est observé.
- **Ce n'est pas un recensement.** La couverture ADS-B est très inégale, et
  les volumes ne se comparent pas d'une région à l'autre.

Chacune de ces limites est mesurée ailleurs dans cet onglet plutôt
qu'affirmee.
"""


def render_method() -> None:
    """Ce qu'il y a sous le tableau de bord.

    Un tableau de bord montre des resultats ; il ne montre pas comment ils
    sont produits. Pour un projet dont l'objet EST la chaîne de données,
    c'est l'essentiel qui manque.
    """
    st.subheader("Comment c'est construit")
    st.markdown(ARCHITECTURE)

    gauche, droite = st.columns(2)
    with gauche:
        st.markdown("**Ce qui est vérifie à chaque exécution**")
        st.markdown(CONTROLES)
    with droite:
        st.markdown("**Ce que ce tableau de bord ne prétend pas être**")
        st.markdown(LIMITES)


@st.fragment(run_every=interval_seconds if auto_refresh else None)
def render() -> None:
    """Corps du tableau de bord, réexécute à chaque rafraîchissement.

    Isole dans un fragment : Streamlit ne rejoue que cette fonction, sans
    réinitialiser les commandes de la barre latérale.

    C'est aussi ICI que l'entrepôt est rafraîchi, et non dans le corps du
    script. Un `run_every` ne rejoue QUE le fragment : place au niveau du
    script, la reconstruction n'aurait lieu qu'au chargement initial de la
    page, et le tableau de bord afficherait indéfiniment des données figées
    pendant que le collecteur, lui, continue d'alimenter le lac.
    """
    import time as _time

    try:
        ensure_warehouse_built(int(_time.time() // REBUILD_INTERVAL_SECONDS))
    except Exception as exc:  # noqa: BLE001 - une reconstruction ratée ne doit
        # pas vider la page : on garde l'affichage précédent et on le signale.
        st.warning(f"Rafraîchissement de l'entrepôt impossible : {exc}")

    # Deuxième filet. Si la reconstruction a échoué, l'entrepôt peut être à
    # moitié à jour : les colonnes lues par le code n'existent pas encore, et
    # la requête lèverait une BinderException brute au visiteur. Un message
    # qui dit ce qui se passe vaut mieux qu'une trace d'exception.
    manquantes = schema_drift()
    if manquantes:
        st.warning(
            "L'entrepôt est en retard sur le code : "
            f"{', '.join(manquantes)} manque(nt). Une reconstruction complète "
            "est nécessaire ; elle se déclenche au prochain cycle."
        )
        return

    try:
        _render_body()
    except duckdb.IOException:
        st.info("Mise à jour de l'entrepôt en cours, affichage dans un instant.")
    except duckdb.BinderException as exc:
        # Colonne absente malgré la vérification : le schéma a bougé entre les
        # deux. On le dit, on ne montre pas la trace.
        st.warning(f"Schéma inattendu dans l'entrepôt : {exc}")


def _render_body() -> None:
    # -- Fraîcheur ---------------------------------------------------------
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

    # Seuils calibrés sur le comportement RÉEL du cron GitHub Actions, pas sur
    # sa cadence théorique : mesure faite sur ce dépôt, les écarts entre deux
    # collectés vont de 50 min à plus de 3 h (GitHub exécute les crons "au
    # mieux"). Des seuils calqués sur les 30 min nominales afficheraient une
    # alerte en permanence, ce qui reviendrait à n'alerter sur rien.
    if age_minutes <= NOMINAL_MAX_MINUTES:
        status_chip(
            "ok",
            f"SIGNAL NOMINAL // dernier relevé il y a {age_minutes:.0f} min "
            f"({last_seen:%H:%M:%S} UTC)",
        )
    elif age_minutes <= DEGRADED_MAX_MINUTES:
        status_chip(
            "warn",
            f"COLLECTE RETARDÉE // dernier relevé il y a {age_minutes:.0f} min "
            "(le cron GitHub est fréquemment différé)",
        )
    else:
        status_chip(
            "err",
            f"SIGNAL PERDU // aucune donnée depuis {age_minutes / 60:.1f} h - "
            "vérifier le workflow Collecte planifiée (onglet Actions)",
        )

    # -- Indicateurs clés --------------------------------------------------
    # Le conteneur nomme produit une classe `st-key-indicateurs` : c'est le
    # seul point d'accroche CSS fiable pour ne viser que ce bloc. Sur petit
    # écran, ses cinq colonnes se rangent par deux au lieu de s'empiler, ce
    # qui rend 300 pixels de défilement.
    with st.container(key="indicateurs"):
        columns = st.columns(5)
        columns[0].metric("Positions", f"{int(overview['positions']):,}".replace(",", " "))
        columns[1].metric("Aéronefs", f"{int(overview['aeronefs']):,}".replace(",", " "))
        columns[2].metric("Snapshots", f"{int(overview['snapshots']):,}".replace(",", " "))
        columns[3].metric("Aéroports", int(airports_active))
        columns[4].metric("Historique", f"{span_hours:.1f} h")

    # -- Onglets -----------------------------------------------------------
    # La page atteignait six mille pixels de haut : tout y était, mais il
    # fallait faire défiler huit écrans pour atteindre le résultat chiffré du
    # projet. Les onglets ne retirent rien, ils rendent la profondeur
    # atteignable - le plus lourd fait désormais 2 400 pixels.
    #
    # Le Python de TOUS les onglets s'exécute à chaque passage ; seul le
    # panneau actif est installé dans le navigateur. Le coût de calcul est
    # donc inchangé, et c'est voulu : les requêtes sont mises en cache, et
    # découvrir un onglet vide le temps qu'il se remplisse serait pire que
    # de tout préparer d'avance.
    radar, trafic, flotte, analyse, coulisses = st.tabs(
        ["Radar", "Trafic", "Flotte", "Analyse", "Coulisses"]
    )

    with radar:
        render_radar()

    with trafic:
        render_snapshot_series()
        st.divider()
        render_hourly_trend()
        st.divider()
        render_diurnal_cycle()
        st.divider()
        render_rankings()

    with flotte:
        render_fleet()

    with analyse:
        render_air_quality()

    with coulisses:
        render_signal_section()
        st.divider()
        render_collection_punctuality()
        st.divider()
        render_coverage()
        st.divider()
        render_method()

    # -- Pied de page ------------------------------------------------------
    st.divider()
    st.caption(
        f"Dernier relevé : {last_seen:%Y-%m-%d %H:%M:%S} UTC | "
        f"Page rafraîchie à {datetime.now(UTC):%H:%M:%S} UTC | "
        f"Entrepôt : {SETTINGS.resolved_duckdb_path.name} | "
        "Sources : OpenSky Network + OurAirports + Open-Météo + OpenFlights"
    )


render()
