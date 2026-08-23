"""Photos d'aeronefs (Planespotters).

Planespotters expose une API publique qui, a partir de l'adresse OACI 24 bits
d'un appareil, renvoie une photographie de CET appareil precis - pas une
image generique du type d'avion. C'est ce qui permet d'afficher, comme le
font les traceurs de vol grand public, la machine reellement observee.

Deux contraintes de la source, respectees ici :

  * l'API exige un User-Agent comportant une URL de contact ; sans elle, elle
    repond 403 avec un message explicite ;
  * les photos sont l'oeuvre de photographes identifies. Le nom de l'auteur
    et le lien vers la page d'origine sont donc systematiquement renvoyes,
    pour etre affiches a cote de l'image.

Aucune cle d'API n'est necessaire. L'usage reste non commercial.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import httpx

from skytrace.logging_conf import get_logger

logger = get_logger(__name__)

PHOTOS_URL = "https://api.planespotters.net/pub/photos/hex/{icao24}"

#: L'URL de contact est surchargeable : quiconque reprend le projet doit
#: pouvoir se declarer aupres de la source plutot que d'usurper la nôtre.
CONTACT_URL = os.environ.get("SKYTRACE_CONTACT_URL", "https://github.com/SouleimanME/Skytrace")
USER_AGENT = f"skytrace/0.1 (+{CONTACT_URL})"


@dataclass(frozen=True)
class AircraftPhoto:
    """Photographie d'un appareil, avec ce qu'il faut pour la crediter."""

    thumbnail_url: str
    photographer: str | None
    page_url: str | None


def fetch_photo(
    icao24: str,
    *,
    http: httpx.Client | None = None,
    timeout: float = 12.0,
) -> AircraftPhoto | None:
    """Renvoie une photo de l'appareil, ou `None` s'il n'en existe pas.

    L'absence de photo est un cas NORMAL (la base ne couvre pas tous les
    appareils), pas une erreur : elle ne doit ni lever, ni etre journalisee
    comme un incident. Un probleme reseau est traite de la meme facon - une
    vignette manquante ne doit jamais empecher l'affichage d'une fiche.
    """
    if not icao24:
        return None

    owns_http = http is None
    http = http or httpx.Client(timeout=timeout, follow_redirects=True)
    try:
        response = http.get(
            PHOTOS_URL.format(icao24=icao24.lower()),
            headers={"User-Agent": USER_AGENT},
        )
        if response.status_code != 200:
            logger.debug("Photo indisponible pour %s (HTTP %s)", icao24, response.status_code)
            return None
        photos = response.json().get("photos") or []
    except (httpx.HTTPError, ValueError) as exc:
        logger.debug("Photo indisponible pour %s : %s", icao24, exc)
        return None
    finally:
        if owns_http:
            http.close()

    if not photos:
        return None

    photo = photos[0]
    thumbnail = (photo.get("thumbnail_large") or photo.get("thumbnail") or {}).get("src")
    if not thumbnail:
        return None

    return AircraftPhoto(
        thumbnail_url=thumbnail,
        photographer=photo.get("photographer"),
        page_url=photo.get("link"),
    )


#: Fragments de nom d'exploitant trahissant un appareil d'Etat ou militaire.
#: Heuristique volontairement lisible : la base aeronefs ne porte aucun
#: indicateur "militaire", et les plages d'adresses reservees ne sont pas
#: publiques de maniere fiable. On se contente donc du nom de l'operateur.
MILITARY_HINTS = (
    "air force",
    "airforce",
    "navy",
    "army",
    "military",
    "armee",
    "armée",
    "gendarmerie",
    "coast guard",
    "luftwaffe",
    "raf ",
    "royal air force",
    "aeronautica militare",
    "police",
    "customs",
    "securite civile",
)


def looks_military(operator: str | None, owner: str | None = None) -> bool:
    """Indique si l'exploitant evoque un appareil d'Etat ou militaire.

    Heuristique, donc faillible dans les deux sens : elle rate un appareil
    militaire sans exploitant renseigne, et signale un avion de police civile.
    A presenter comme une indication, jamais comme une certitude.
    """
    # Les appelants lisent souvent ces champs dans un tableau pandas, ou une
    # valeur absente vaut NaN - un flottant, donc "vrai" au sens booleen. On
    # ne retient donc que les vraies chaines, plutot que de filtrer sur la
    # veracite : sinon un NaN ferait echouer la concatenation.
    haystack = " ".join(v for v in (operator, owner) if isinstance(v, str)).lower()
    return any(hint in haystack for hint in MILITARY_HINTS)
