"""Garde le tableau de bord public eveille sur Streamlit Community Cloud.

L'offre gratuite arrete le conteneur d'une application restee sans visiteur,
et affiche a la place une page "Zzzz" avec un bouton de reveil. Pour un lien
de portfolio c'est genant : le premier visiteur tombe sur une page qui a
l'air cassee.

Deux details de la plateforme, decouverts en la sondant, sans lesquels ce
script ne fonctionne pas :

  * **L'application n'est PAS servie a la racine.** L'adresse publique rend
    une coquille Streamlit Cloud qui embarque l'application dans une iframe,
    sous le prefixe `/~/+/`. C'est donc la-dessous que repond le point de
    sante ; a la racine, il renvoie du HTML meme quand tout va bien.

  * **La page de veille met du temps a apparaitre.** Elle est rendue par du
    JavaScript : au chargement du document, le bouton de reveil n'existe pas
    encore. Le chercher immediatement revient a conclure qu'il n'y en a pas.

Pourquoi un navigateur et non un simple `curl` : une requete HTTP sur la
racine est servie par le proxy sans jamais toucher au conteneur, et ne remet
donc pas le compteur d'inactivite a zero. Seule une vraie session - websocket
ouvert par un navigateur - compte comme une visite.

Ce script fait donc deux choses :

  1. il ouvre la page comme le ferait un visiteur, ce qui repousse la mise
     en veille ;
  2. si l'application dort deja, il clique le bouton de reveil et attend
     qu'elle reponde, ce qui borne la duree d'indisponibilite a l'intervalle
     entre deux executions.

A savoir : l'offre gratuite est dimensionnee pour des applications peu
visitees. Maintenir la sienne eveillee par une visite programmee va contre
cet esprit, meme si rien ne l'interdit explicitement. L'intervalle est donc
volontairement large - quelques visites par jour - plutot que de battre a la
minute.

Usage : python scripts/keepalive.py https://mon-app.streamlit.app
"""

from __future__ import annotations

import sys
import time
from urllib.parse import urljoin

import httpx
from playwright.sync_api import Frame, Page, sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeout

#: Chemin du point de sante de l'APPLICATION. Le prefixe `/~/+/` n'est pas
#: decoratif : a la racine repond la coquille Streamlit Cloud, qui renvoie sa
#: page HTML avec un code 200 que l'application tourne ou non. Sonder la
#: racine ne dit donc rien de l'etat reel - c'est l'erreur qui faisait
#: conclure a un echec alors que le reveil avait marche.
HEALTH_PATH = "/~/+/_stcore/health"

#: Libelle du bouton de reveil. Streamlit sert son interface en anglais quelle
#: que soit la langue du visiteur.
WAKE_LABEL = "get this app back up"

#: Delai d'apparition du bouton de reveil. La page de veille est rendue en
#: JavaScript : elle n'existe pas au chargement du document.
WAKE_BUTTON_TIMEOUT_S = 45

#: Delai de demarrage du conteneur, une fois le bouton clique.
WAKE_TIMEOUT_S = 240

PAGE_TIMEOUT_MS = 60_000

#: Duree pendant laquelle la session reste ouverte apres affichage. C'est
#: elle qui constitue la "visite" ; partir aussitot la rendrait inutile.
DWELL_MS = 10_000


def is_awake(url: str, *, timeout: float = 20.0) -> bool:
    """Vrai si l'application repond elle-meme, faux si elle dort.

    On regarde le CORPS de la reponse et non son statut : la coquille
    renvoie 200 dans tous les cas.
    """
    try:
        response = httpx.get(urljoin(url, HEALTH_PATH), timeout=timeout, follow_redirects=True)
    except httpx.HTTPError:
        return False
    return response.status_code == 200 and response.text.strip().lower() == "ok"


def find_wake_button(page: Page):
    """Cherche le bouton de reveil dans la page et dans ses iframes.

    Les locators de Playwright ne traversent pas les iframes : il faut
    parcourir les cadres un a un. Le bouton vit dans la coquille, mais rien
    ne garantit que ce soit toujours le cas.
    """
    cadres: list[Page | Frame] = [page, *page.frames]
    for cadre in cadres:
        try:
            bouton = cadre.get_by_role("button", name=WAKE_LABEL, exact=False)
            if bouton.count() and bouton.first.is_visible():
                return bouton.first
        except PlaywrightTimeout:
            continue
        except Exception:  # noqa: BLE001 - cadre detache pendant le parcours
            continue
    return None


def wake_up(page: Page, url: str) -> bool:
    """Clique le bouton de reveil et attend que l'application reponde."""
    echeance = time.monotonic() + WAKE_BUTTON_TIMEOUT_S
    bouton = None
    while time.monotonic() < echeance:
        bouton = find_wake_button(page)
        if bouton is not None:
            break
        page.wait_for_timeout(2_000)

    if bouton is None:
        print("bouton de reveil introuvable", file=sys.stderr)
        return False

    bouton.click()
    print("bouton de reveil clique, demarrage du conteneur")

    echeance = time.monotonic() + WAKE_TIMEOUT_S
    while time.monotonic() < echeance:
        if is_awake(url):
            return True
        time.sleep(5)
    print(f"toujours sans reponse apres {WAKE_TIMEOUT_S} s", file=sys.stderr)
    return False


def visit(url: str, *, endormie: bool) -> bool:
    """Ouvre la page en vrai navigateur ; reveille l'application au besoin."""
    with sync_playwright() as playwright:
        navigateur = playwright.chromium.launch()
        try:
            page = navigateur.new_page()
            page.set_default_timeout(PAGE_TIMEOUT_MS)
            page.goto(url, wait_until="domcontentloaded")

            if endormie and not wake_up(page, url):
                return False

            if endormie:
                # Recharger : la page de veille n'ouvre aucune session vers le
                # conteneur qui vient de demarrer.
                page.goto(url, wait_until="domcontentloaded")

            # Laisser le websocket s'etablir. C'est CETTE session qui compte
            # comme une visite, pas la requete HTTP qui l'a precedee.
            page.wait_for_timeout(DWELL_MS)
            return is_awake(url)
        finally:
            navigateur.close()


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"usage : {argv[0]} <url de l'application>", file=sys.stderr)
        return 2

    url = argv[1].rstrip("/") + "/"
    endormie = not is_awake(url)
    print(f"{url} : {'endormie' if endormie else 'eveillee'}")

    if visit(url, endormie=endormie):
        print("l'application repond")
        return 0

    print("l'application ne repond pas", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
