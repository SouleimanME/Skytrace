"""Declenche la collecte via l'API GitHub, sans dependre de son ordonnanceur.

POURQUOI CE SCRIPT EXISTE. Le 26 aout 2026, GitHub a cesse d'executer les
taches planifiees du compte. Les executions declenchees par `push` ont
continue de fonctionner normalement, et un depot voisin dont les calendriers
n'avaient pas ete touches est reparti de lui-meme. Celui-ci non : plus de
quarante heures sans un seul declenchement planifie, et pousser n'y a rien
change.

La lecon n'est pas qu'il faut mieux configurer le cron. C'est que
**l'ordonnanceur de GitHub n'est pas un composant sur lequel on peut fonder
la disponibilite d'un pipeline**. Sa documentation le dit d'ailleurs
elle-meme : les taches planifiees s'executent "au mieux", sans garantie.

Le declenchement par API, lui, n'emprunte pas ce chemin. C'est la meme voie
que le bouton "Run workflow" de l'interface, celle qui a continue de marcher
pendant toute la panne. Un cron EXTERNE appelle ce point d'entree, et la
collecte cesse de dependre d'un composant qui a prouve qu'il pouvait se
taire pendant deux jours sans prevenir.

CE QUE CE SCRIPT FAIT. Il declenche le workflow, puis il VERIFIE qu'une
execution est reellement apparue. L'API repond 204 sans corps : elle accuse
reception de la demande, elle ne promet pas qu'un travail a demarre.
Confondre les deux, c'est reproduire l'erreur qui a coute une soiree - un
declenchement cru effectif, mais dont GitHub n'avait aucune trace.

LE JETON. Il est lu dans la variable d'environnement `SKYTRACE_GITHUB_TOKEN`,
jamais passe en argument (l'historique du terminal le conserverait) et jamais
affiche. Un jeton a portee fine suffit :

    github.com/settings/personal-access-tokens/new
      depot          : SouleimanME/Skytrace uniquement
      permission     : Actions -> Read and write
      expiration     : la plus courte qui vous convienne

Usage :
    export SKYTRACE_GITHUB_TOKEN=...        # ou $env: sous PowerShell
    python scripts/declencher_collecte.py SouleimanME/Skytrace
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

#: Workflow declenche par defaut.
WORKFLOW = "collect.yml"

#: Branche depuis laquelle l'executer. Les workflows planifies comme les
#: declenchements manuels ne s'executent que depuis la branche par defaut.
BRANCHE = "main"

API = "https://api.github.com"
TIMEOUT = 30.0

#: Combien de temps attendre l'apparition de l'execution. GitHub accuse
#: reception immediatement mais met quelques secondes a creer le run.
ATTENTE_MAX_SECONDES = 45
INTERVALLE_SECONDES = 5


def _requete(url: str, jeton: str, *, donnees: bytes | None = None) -> tuple[int, str]:
    """Appelle l'API GitHub. Le jeton ne transite que dans l'en-tete."""
    requete = urllib.request.Request(  # noqa: S310 - hote constant, schema https
        url,
        data=donnees,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {jeton}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "skytrace-declencheur",
        },
        method="POST" if donnees is not None else "GET",
    )
    try:
        with urllib.request.urlopen(requete, timeout=TIMEOUT) as reponse:  # noqa: S310
            return reponse.status, reponse.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except urllib.error.URLError as exc:
        return 0, str(exc.reason)


def diagnostiquer(code: int, corps: str) -> tuple[bool, str]:
    """Traduit la reponse en verdict. Le detail compte : chaque code a une cause."""
    if code == 204:
        return True, "demande acceptee par GitHub"
    if code == 401:
        return False, "jeton refuse : absent, expire, ou mal copie"
    if code == 403:
        return False, "jeton valide mais sans la permission Actions (Read and write)"
    if code == 404:
        return False, (
            "depot ou workflow introuvable - verifier le nom, et que le jeton "
            "donne acces a CE depot"
        )
    if code == 422:
        return False, f"demande refusee : {corps[:120]}"
    if code == 0:
        return False, f"injoignable : {corps}"
    return False, f"code inattendu {code} : {corps[:120]}"


def derniere_execution(depot: str, jeton: str) -> str | None:
    """Horodatage de l'execution la plus recente, ou None."""
    code, corps = _requete(
        f"{API}/repos/{depot}/actions/workflows/{WORKFLOW}/runs?per_page=1", jeton
    )
    if code != 200:
        return None
    executions = json.loads(corps).get("workflow_runs", [])
    return executions[0]["created_at"] if executions else None


def main(argv: list[str]) -> int:
    if len(argv) != 2 or "/" not in argv[1]:
        print(
            "usage : python scripts/declencher_collecte.py <proprietaire>/<depot>", file=sys.stderr
        )
        return 2

    depot = argv[1]
    jeton = os.environ.get("SKYTRACE_GITHUB_TOKEN", "").strip()
    if not jeton:
        print(
            "SKYTRACE_GITHUB_TOKEN absent de l'environnement.\n"
            "Le jeton n'est jamais passe en argument : l'historique du terminal "
            "le conserverait.",
            file=sys.stderr,
        )
        return 2

    avant = derniere_execution(depot, jeton)

    code, corps = _requete(
        f"{API}/repos/{depot}/actions/workflows/{WORKFLOW}/dispatches",
        jeton,
        donnees=json.dumps({"ref": BRANCHE}).encode("utf-8"),
    )
    accepte, diagnostic = diagnostiquer(code, corps)
    print(f"Declenchement de {WORKFLOW} sur {depot} ({BRANCHE})")
    print(f"  reponse    : HTTP {code}")
    print(f"  diagnostic : {diagnostic}")
    if not accepte:
        return 1

    # UN 204 N'EST PAS UNE EXECUTION. GitHub accuse reception de la demande ;
    # il ne promet pas qu'un travail demarre. On verifie donc qu'une nouvelle
    # execution est apparue, plutot que de faire confiance a l'accuse.
    print(f"  verification (jusqu'a {ATTENTE_MAX_SECONDES} s) ...")
    limite = time.monotonic() + ATTENTE_MAX_SECONDES
    while time.monotonic() < limite:
        time.sleep(INTERVALLE_SECONDES)
        apres = derniere_execution(depot, jeton)
        if apres and apres != avant:
            print(f"  execution creee : {apres}")
            return 0

    print(
        "  AUCUNE execution creee malgre l'accuse de reception.\n"
        "  C'est le cas le plus trompeur : la demande est acceptee et rien ne\n"
        "  demarre. Regarder l'onglet Actions du depot, qui affiche un bandeau\n"
        "  quand les executions sont restreintes.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
