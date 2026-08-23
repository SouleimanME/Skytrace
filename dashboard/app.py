"""Tableau de bord SkyTrace (interface radar / HUD).

Le tableau de bord ne connaît que les tables `marts`. Il n'ouvre jamais un
fichier Parquet et ne recalcule jamais une agrégation : c'est le contrat de
la couche gold. Conséquence pratique - si une definition métier change, on
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
from contextlib import contextmanager
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
    """Vrai si la page est servie a un telephone ou une tablette.

    La mise en forme se regle en CSS, mais deux choses ne s'y pretent pas :
    le zoom initial de la carte et sa hauteur sont des valeurs passees a
    deck.gl, pas des proprietes de style. L'agent utilisateur est une
    approximation - il se falsifie et se trompe sur les cas limites - mais
    l'erreur reste sans consequence : au pire, un cadrage un peu large.
    """
    try:
        agent = (st.context.headers.get("User-Agent") or "").lower()
    except Exception:  # noqa: BLE001 - hors contexte de requete
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


@contextmanager
def section(title: str, *, collapsible: bool = False, heading: bool = True):
    """Section de page, repliable sur petit ecran.

    Sur telephone, la page fait huit ecrans de haut : replier les sections
    secondaires evite d'imposer un defilement interminable pour atteindre le
    pied de page, sans rien retirer a personne - tout reste a une tape. Sur
    grand ecran, la mise en page ne change pas.
    """
    st.divider()
    if collapsible and is_handheld():
        with st.expander(title, expanded=False):
            yield
    else:
        if heading:
            st.subheader(title)
        yield


def chart_config() -> dict:
    """Options du rendu Plotly, adaptees au toucher.

    La barre d'outils n'apparait qu'au survol sur un ecran de bureau, mais
    reste affichee en permanence sur un ecran tactile, ou elle recouvre la
    legende. Aucun de ses boutons - zoom, lasso, capture - n'a de sens au
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
        # Les hauteurs sont calibrees pour un ecran large. Sur telephone,
        # les conserver telles quelles ferait de la page un couloir : chaque
        # graphe occuperait la moitie de l'ecran, et il y en a huit.
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

    try:
        _render_body()
    except duckdb.IOException:
        st.info("Mise à jour de l'entrepôt en cours, affichage dans un instant.")


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
    # ecran, ses cinq colonnes se rangent par deux au lieu de s'empiler, ce
    # qui rend 300 pixels de defilement.
    with st.container(key="indicateurs"):
        columns = st.columns(5)
        columns[0].metric("Positions", f"{int(overview['positions']):,}".replace(",", " "))
        columns[1].metric("Aéronefs", f"{int(overview['aeronefs']):,}".replace(",", " "))
        columns[2].metric("Snapshots", f"{int(overview['snapshots']):,}".replace(",", " "))
        columns[3].metric("Aéroports", int(airports_active))
        columns[4].metric("Historique", f"{span_hours:.1f} h")

    st.divider()

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

    # -- Carte du dernier relevé -------------------------------------------
    st.subheader("Radar // dernier relevé")

    # Jointure avec la dimension aéronef : elle apporte le constructeur et la
    # compagnie, qui servent de dimensions de filtrage sur la carte.
    latest_all = load(
        """
        select
            p.latitude, p.longitude, p.callsign, p.origin_country, p.heading_deg,
            p.barometric_altitude_ft, p.ground_speed_kt, p.flight_phase,
            p.aircraft_icao24, p.snapshot_at,
            coalesce(a.manufacturer_group, 'Inconnu') as manufacturer_group,
            a.airline_name, a.registration, a.aircraft_type, a.manufacturer,
            a.model, a.operator, a.built_year, a.airline_country
        from marts.fct_aircraft_positions p
        left join marts.dim_aircraft a using (aircraft_icao24)
        where p.snapshot_at = (select max(snapshot_at) from marts.fct_aircraft_positions)
        """
    )
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

    st.divider()

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

    with section("Pays et aéroports", collapsible=True, heading=False):
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
                "de montée : ADS-B ne publie pas de plan de vol."
            )

    with section("Compagnies et flotte", collapsible=True):
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
                    "Type et constructeur issus de la base aéronefs OpenSky ; "
                    "compagnie déduite du préfixe d'indicatif (OpenFlights)."
                )

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

    with section("Trafic et qualité de l'air", collapsible=True):
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
                    "Corrélation r (brute)",
                    f"{air_quality['r_naive']:+.2f}",
                    help=(
                        "Coefficient de corrélation de Pearson entre le nombre "
                        "d'avions et le NO2, par heure. Sans unité, de -1 à +1 "
                        "(0 = aucun lien). Le NO2 est mesuré en µg/m³."
                    ),
                )
                st.metric(
                    "Corrélation r (intra-aéroport)",
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

    # -- Pied de page ------------------------------------------------------
    st.divider()
    st.caption(
        f"Dernier relevé : {last_seen:%Y-%m-%d %H:%M:%S} UTC | "
        f"Page rafraîchie à {datetime.now(UTC):%H:%M:%S} UTC | "
        f"Entrepôt : {SETTINGS.resolved_duckdb_path.name} | "
        "Sources : OpenSky Network + OurAirports + Open-Météo + OpenFlights"
    )


render()
