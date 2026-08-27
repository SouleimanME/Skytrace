"""Verifie la cible du moniteur externe, et imprime sa configuration.

POURQUOI CE SCRIPT EXISTE. La surveillance du projet vivait entierement dans
GitHub Actions : la collecte, le maintien en eveil, et la veille censee
detecter l'arret de la collecte. Le 26 aout 2026, GitHub a cesse d'executer
les taches planifiees du compte. Les trois se sont arretees ensemble, y
compris la veille - un cron GitHub qui surveille des crons GitHub partage
leur sort, et se tait precisement quand il aurait servi. La panne a dure
32 heures et c'est un test manuel en navigation privee qui l'a revelee.

Un moniteur EXTERNE ne partage pas ce sort. Il vit chez un tiers, interroge
l'application depuis l'exterieur, et alerte quand elle ne repond plus - que
la cause soit Streamlit, GitHub ou le reseau.

CE QUE CE SCRIPT FAIT, ET CE QU'IL NE FAIT PAS. Il ne cree aucun compte et
ne configure rien a distance : cela reste a faire dans l'interface du
prestataire. Il verifie que la cible se comporte comme la documentation
l'affirme, et imprime les valeurs exactes a y reporter. Une documentation de
surveillance qui n'a jamais ete verifiee est une hypothese.

LE POINT D'ACCES, ET POURQUOI CELUI-LA. Mesure sur le deploiement public,
dans les deux etats :

    adresse                      endormie   reveillee
    /                            303        303        inutilisable
    /_stcore/health              303        303        inutilisable
    /~/+/_stcore/health          400        200 "ok"   discriminant

La racine redirige vers l'authentification Streamlit Cloud dans les deux
cas : un moniteur pointe dessus repondrait "en ligne" alors que l'utilisateur
voit une page de veille. L'application n'est pas servie a la racine mais
sous le prefixe `/~/+/`, et c'est la que le point de sante dit la verite.

CE QUE LE MONITEUR NE FERA PAS. Il n'empechera pas la mise en veille. Une
requete HTTP est servie par le proxy sans jamais toucher au conteneur, donc
elle ne remet pas le compteur d'inactivite a zero ; seule une vraie session
navigateur compte comme une visite. Le workflow de maintien en eveil reste
donc necessaire. Le moniteur previent, il ne soigne pas.

Usage :
    python scripts/verifier_sante.py https://mon-app.streamlit.app
"""

from __future__ import annotations

import sys
import urllib.error
import urllib.request
from urllib.parse import urljoin

#: L'application est servie sous ce prefixe, pas a la racine.
HEALTH_PATH = "/~/+/_stcore/health"

#: Intervalle conseille. Assez frequent pour prevenir dans l'heure, assez
#: espace pour tenir dans les offres gratuites (UptimeRobot en autorise 50 a
#: 5 minutes) et pour ne pas marteler un hebergeur gratuit.
INTERVAL_MINUTES = 5

TIMEOUT = 30.0


def sonder(url: str) -> tuple[int, str]:
    """Interroge le point de sante et rend (code HTTP, corps).

    Les redirections ne sont PAS suivies : c'est justement une redirection
    qui distingue la racine, inutilisable, du point de sante. Les suivre
    effacerait le signal qu'on vient chercher.
    """
    requete = urllib.request.Request(  # noqa: S310 - schema verifie par l'appelant
        urljoin(url, HEALTH_PATH),
        headers={"User-Agent": "skytrace-verification-sante"},
    )

    class SansRedirection(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *_args, **_kwargs):
            return None

    ouvreur = urllib.request.build_opener(SansRedirection)
    try:
        with ouvreur.open(requete, timeout=TIMEOUT) as reponse:
            return reponse.status, reponse.read(200).decode("utf-8", "replace").strip()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(200).decode("utf-8", "replace").strip()
    except urllib.error.URLError as exc:
        return 0, str(exc.reason)


def interpreter(code: int, corps: str) -> tuple[bool, str]:
    """Traduit le code en diagnostic. Un 200 vide ne vaut pas un 200 "ok"."""
    if code == 200 and corps.lower().startswith("ok"):
        return True, "application eveillee et repondante"
    if code == 200:
        return False, f"code 200 mais corps inattendu : {corps[:60]!r}"
    if code == 400:
        return False, "application ENDORMIE (page Zzzz servie aux visiteurs)"
    if code in (301, 302, 303, 307, 308):
        return False, "redirection : adresse probablement incomplete, le prefixe /~/+/ manque"
    if code == 0:
        return False, f"injoignable : {corps}"
    return False, f"code inattendu : {code}"


def main(argv: list[str]) -> int:
    if len(argv) != 2 or not argv[1].startswith(("http://", "https://")):
        print(__doc__.strip().splitlines()[-2].strip(), file=sys.stderr)
        return 2

    url = argv[1].rstrip("/") + "/"
    cible = urljoin(url, HEALTH_PATH)

    code, corps = sonder(url)
    sain, diagnostic = interpreter(code, corps)

    print(f"Cible interrogee : {cible}")
    print(f"  reponse        : HTTP {code}" + (f"  {corps[:40]!r}" if corps else ""))
    print(f"  diagnostic     : {diagnostic}")
    print()
    print("A reporter dans le moniteur externe :")
    print("  type           : HTTP(s), sur le CODE de reponse")
    print(f"  adresse        : {cible}")
    print(f"  intervalle     : {INTERVAL_MINUTES} minutes")
    print("  alerte si      : le code n'est pas 200")
    print("  suivre redirs. : NON (une redirection est un echec, pas un succes)")
    print()
    print("Rappel : ce moniteur previent, il ne reveille pas. Une requete HTTP")
    print("n'annule pas la mise en veille ; le workflow keepalive reste utile.")

    return 0 if sain else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
