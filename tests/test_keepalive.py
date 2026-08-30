"""Tests du maintien en eveil du tableau de bord public.

Le vrai piege de ce script n'est pas le clic : c'est que le bouton de reveil
n'existe pas au chargement du document. Il est ajoute par du JavaScript, et
le chercher trop tot revient a conclure qu'il n'y en a pas - c'est ce qui
faisait echouer la premiere version. On reproduit donc exactement ce
comportement : une page qui n'affiche son bouton qu'apres un delai.

Playwright n'est pas une dependance du projet (il embarque un navigateur de
plusieurs centaines de mega-octets). Ces tests sont donc ignores s'il est
absent, plutot que d'alourdir l'integration continue.
"""

from __future__ import annotations

import sys
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

playwright_api = pytest.importorskip(
    "playwright.sync_api", reason="Playwright absent : test du maintien en eveil ignore"
)
import keepalive  # noqa: E402

#: Reproduction de la page de veille : le bouton apparait APRES coup, comme
#: sur la vraie, et le libelle est celui que sert Streamlit.
PAGE_DE_VEILLE = """<!doctype html>
<html><body>
  <h1>Zzzz</h1>
  <div id="cible"></div>
  <script>
    setTimeout(function () {
      var b = document.createElement('button');
      b.textContent = 'Yes, get this app back up!';
      b.onclick = function () { document.title = 'CLIQUE'; };
      document.getElementById('cible').appendChild(b);
    }, 1200);
  </script>
</body></html>
"""

PAGE_SANS_BOUTON = "<!doctype html><html><body><h1>Tableau de bord</h1></body></html>"


class _Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, corps: str, **kwargs):
        self._corps = corps
        super().__init__(*args, **kwargs)

    def do_GET(self):  # noqa: N802 - nom impose par http.server
        contenu = self._corps.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(contenu)))
        self.end_headers()
        self.wfile.write(contenu)

    def log_message(self, *args):
        pass


@pytest.fixture
def serveur():
    """Sert une page donnee sur un port libre, le temps du test."""

    def _servir(corps: str) -> str:
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), partial(_Handler, corps=corps))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        fermetures.append(httpd)
        return f"http://127.0.0.1:{httpd.server_address[1]}/"

    fermetures: list[ThreadingHTTPServer] = []
    yield _servir
    for httpd in fermetures:
        httpd.shutdown()


@pytest.fixture
def page():
    with playwright_api.sync_playwright() as p:
        navigateur = p.chromium.launch()
        onglet = navigateur.new_page()
        yield onglet
        navigateur.close()


class TestFindWakeButton:
    def test_attend_que_le_bouton_soit_rendu(self, page, serveur):
        # Le bouton n'existe pas au chargement : c'est tout l'interet du test.
        page.goto(serveur(PAGE_DE_VEILLE), wait_until="domcontentloaded")
        assert keepalive.find_wake_button(page) is None

        page.wait_for_timeout(2_000)
        bouton = keepalive.find_wake_button(page)
        assert bouton is not None

        bouton.click()
        assert page.title() == "CLIQUE"

    def test_page_sans_bouton(self, page, serveur):
        page.goto(serveur(PAGE_SANS_BOUTON), wait_until="domcontentloaded")
        page.wait_for_timeout(1_500)
        assert keepalive.find_wake_button(page) is None


class TestIsAwake:
    def test_corps_ok_signifie_eveillee(self, serveur, monkeypatch):
        base = serveur("ok")
        monkeypatch.setattr(keepalive, "HEALTH_PATH", "/")
        assert keepalive.is_awake(base) is True

    def test_page_html_signifie_endormie(self, serveur, monkeypatch):
        # Le piege : la coquille repond 200 avec du HTML. Un test sur le seul
        # code de statut conclurait a tort que tout va bien.
        base = serveur(PAGE_DE_VEILLE)
        monkeypatch.setattr(keepalive, "HEALTH_PATH", "/")
        assert keepalive.is_awake(base) is False

    def test_hote_injoignable(self):
        # Port ferme : l'absence de reponse n'est pas une exception a remonter.
        assert keepalive.is_awake("http://127.0.0.1:9/", timeout=2.0) is False


def test_le_chemin_de_sante_vise_l_application_et_non_la_coquille():
    # Regression : sonder la racine renvoyait du HTML meme application en
    # marche, ce qui faisait conclure a un echec apres un reveil reussi.
    assert keepalive.HEALTH_PATH.startswith("/~/+/")


class TestResilience:
    """Une alerte fausse coute plus cher qu'une alerte manquante.

    Le 30 aout 2026, le workflow de maintien en eveil a echoue et envoye un
    courriel d'alarme alors que l'application repondait parfaitement. Piloter
    un navigateur sans fil est capricieux : un clic peut manquer sa cible,
    une page peut mettre trop longtemps. Ces aleas ne disent rien de la sante
    du service, et faire crier le workflow dessus apprend a ignorer les
    alertes suivantes.
    """

    def test_un_alea_de_pilotage_ne_fait_pas_echouer(self, monkeypatch, capsys):
        """LE cas qui produisait la fausse alerte."""
        monkeypatch.setattr(keepalive, "is_awake", lambda *_a, **_k: True)
        monkeypatch.setattr(keepalive, "PAUSE_ENTRE_TENTATIVES_S", 0)

        def pilotage_capricieux(*_a, **_k):
            raise keepalive.PlaywrightTimeout("Timeout 60000ms exceeded")

        monkeypatch.setattr(keepalive, "visit", pilotage_capricieux)

        code = keepalive.main(["prog", "https://exemple.streamlit.app"])

        assert code == 0, "l'application repond : le workflow ne doit pas echouer"
        assert "malgre l'echec du pilotage" in capsys.readouterr().out

    def test_une_application_muette_fait_bien_echouer(self, monkeypatch, capsys):
        """La resilience ne doit pas devenir de la complaisance."""
        monkeypatch.setattr(keepalive, "is_awake", lambda *_a, **_k: False)
        monkeypatch.setattr(keepalive, "visit", lambda *_a, **_k: False)
        monkeypatch.setattr(keepalive, "PAUSE_ENTRE_TENTATIVES_S", 0)

        assert keepalive.main(["prog", "https://exemple.streamlit.app"]) == 1
        assert "ne repond pas" in capsys.readouterr().err

    def test_une_reussite_au_second_essai_suffit(self, monkeypatch):
        """Un demarrage de conteneur plus lent que d'habitude n'est pas une panne."""
        monkeypatch.setattr(keepalive, "is_awake", lambda *_a, **_k: False)
        monkeypatch.setattr(keepalive, "PAUSE_ENTRE_TENTATIVES_S", 0)
        essais = iter([False, True])
        monkeypatch.setattr(keepalive, "visit", lambda *_a, **_k: next(essais))

        assert keepalive.main(["prog", "https://exemple.streamlit.app"]) == 0

    def test_le_nombre_de_tentatives_est_borne(self, monkeypatch):
        """Sans borne, le workflow tournerait jusqu'a son propre delai maximal."""
        monkeypatch.setattr(keepalive, "is_awake", lambda *_a, **_k: False)
        monkeypatch.setattr(keepalive, "PAUSE_ENTRE_TENTATIVES_S", 0)
        appels = []
        monkeypatch.setattr(keepalive, "visit", lambda *a, **k: appels.append(1) or False)

        keepalive.main(["prog", "https://exemple.streamlit.app"])

        assert len(appels) == keepalive.TENTATIVES
