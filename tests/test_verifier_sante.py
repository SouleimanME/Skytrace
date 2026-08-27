"""Le diagnostic du moniteur externe doit distinguer trois etats, pas deux.

Un moniteur mal configure est pire que pas de moniteur : il rassure. Ces
tests verrouillent la seule chose qui compte ici, la traduction d'une
reponse HTTP en verdict.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from verifier_sante import HEALTH_PATH, interpreter  # noqa: E402


class TestInterpreter:
    def test_deux_cents_ok_est_le_seul_succes(self):
        sain, _ = interpreter(200, "ok")
        assert sain

    def test_quatre_cents_signale_une_application_endormie(self):
        """LE cas qui motive tout : mesure sur le deploiement, endormi = 400."""
        sain, message = interpreter(400, "")
        assert not sain
        assert "ENDORMIE" in message

    @pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
    def test_une_redirection_est_un_echec_et_non_un_succes(self, code):
        """Le piege de configuration, et il est silencieux.

        La racine du deploiement redirige vers l'authentification Streamlit
        Cloud - avec le MEME code 303 que l'application dorme ou non. Mesure
        faite en suivant les redirections : la reponse part en boucle entre
        l'application et `/-/login`, cinquante sauts avant abandon du client.
        Un moniteur ainsi regle signale une panne en permanence, donc ne
        signale plus rien. C'est pour cela que `sonder` ne suit pas les
        redirections, et que le prefixe `/~/+/` est obligatoire.
        """
        sain, message = interpreter(code, "")
        assert not sain
        assert "/~/+/" in message

    def test_deux_cents_sans_ok_ne_passe_pas(self):
        """Un 200 qui rend du HTML est une page d'authentification, pas une sante."""
        sain, message = interpreter(200, "<!doctype html><html>")
        assert not sain
        assert "corps inattendu" in message

    def test_injoignable_est_signale_comme_tel(self):
        sain, message = interpreter(0, "Name or service not known")
        assert not sain
        assert "injoignable" in message


def test_le_chemin_porte_le_prefixe_de_l_application():
    """L'application n'est pas servie a la racine : sans ce prefixe, rien ne marche."""
    assert HEALTH_PATH.startswith("/~/+/")
