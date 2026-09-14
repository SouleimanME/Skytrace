"""Les actions GitHub doivent rester sur un socle encore execute.

POURQUOI CE FICHIER. Chaque execution des workflows portait cet avertissement :

    Node.js 20 is deprecated. The following actions target Node.js 20 but are
    being forced to run on Node.js 24: actions/checkout@v4, actions/setup-python@v5

« Forced to run » dit l'essentiel : GitHub executait deja ces actions sur un
runtime pour lequel elles n'ont pas ete publiees. Cela a tenu, jusqu'au jour ou
cela ne tient plus - et ce jour-la, le symptome serait celui qu'on vient de
passer dix jours a eteindre : une collecte horaire en echec, un courriel par
heure.

CE QUI A ETE VERIFIE AVANT DE MONTER, plutot que de sauter trois majeures a
l'aveugle. Les ruptures annoncees par chaque version majeure, confrontees a ce
depot :

  * checkout v5 / setup-python v6 / upload-artifact v5 et v6 : passage a
    Node 24, runner minimum 2.327.1. Les runners heberges par GitHub sont tres
    au-dela, et l'avertissement ci-dessus prouve qu'ils executent deja Node 24.
  * checkout v6 : les identifiants ne sont plus ecrits dans `.git/config` mais
    dans un fichier separe. `anti-inactivite.yml` pousse un commit ; il le fait
    par le remote standard, donc par le chemin que ce changement preserve.
  * checkout v7 : le retrait d'une PR issue d'un fork est bloque pour
    `pull_request_target` et `workflow_run`. Aucun workflow d'ici n'utilise ces
    deux declencheurs - le test le verrouille ci-dessous.
  * setup-python v7 : l'entree `pip-install` est supprimee. Aucun workflow ne
    s'en sert - verrouille aussi.

CE QUE CE FICHIER NE FAIT PAS. Il ne verifie pas que la version existe encore
en ligne : un test qui interroge le reseau echouerait pour une panne de GitHub
plutot que pour un defaut du depot. Le plancher est donc une valeur mesuree le
15 septembre 2026, a relever en meme temps que les versions.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"

#: Version majeure minimale par action. En dessous, l'action cible un runtime
#: que GitHub n'execute plus nativement.
PLANCHERS = {
    "actions/checkout": 7,
    "actions/setup-python": 7,
    "actions/upload-artifact": 7,
}

#: Declencheurs pour lesquels `checkout@v7` refuse de retirer la tete d'une PR
#: de fork. Les employer demanderait de revoir la montee, pas de la subir.
DECLENCHEURS_INCOMPATIBLES = ("pull_request_target", "workflow_run")

_USES = re.compile(r"uses:\s*([\w.-]+/[\w.-]+)@v(\d+)")


def usages():
    """Rend (fichier, action, majeure) pour chaque `uses:` des workflows."""
    trouves = []
    for chemin in sorted(WORKFLOWS.glob("*.yml")):
        for action, majeure in _USES.findall(chemin.read_text(encoding="utf-8")):
            trouves.append((chemin.name, action, int(majeure)))
    return trouves


def test_il_existe_bien_des_actions_a_verifier():
    """Un test qui ne trouve rien passerait pour de bonnes raisons imaginaires."""
    assert usages(), "aucun `uses:` trouve : l'emplacement des workflows a change"


@pytest.mark.parametrize("usage", usages(), ids=lambda u: f"{u[0]}:{u[1]}@v{u[2]}")
def test_aucune_action_sous_son_plancher(usage):
    """Une seule ligne oubliee suffit a ramener l'avertissement sur tous les runs."""
    fichier, action, majeure = usage
    plancher = PLANCHERS.get(action)
    if plancher is None:
        pytest.skip(f"{action} n'a pas de plancher declare")

    assert majeure >= plancher, (
        f"{fichier} epingle {action}@v{majeure}, sous le plancher v{plancher}. "
        "L'action cible alors un runtime que GitHub n'execute plus nativement."
    )


def test_une_action_porte_la_meme_version_partout():
    """Le vrai mode de defaillance : monter cinq fichiers sur six.

    L'oubli ne casse rien tout de suite - il laisse un seul workflow en arriere,
    donc un seul a tomber le jour ou le runtime disparait. Et c'est le workflow
    horaire qui coute le plus cher en courriels.
    """
    versions: dict[str, dict[int, list[str]]] = {}
    for fichier, action, majeure in usages():
        versions.setdefault(action, {}).setdefault(majeure, []).append(fichier)

    divergents = {a: v for a, v in versions.items() if len(v) > 1}

    assert not divergents, f"versions divergentes selon le fichier : {divergents}"


@pytest.mark.parametrize("declencheur", DECLENCHEURS_INCOMPATIBLES)
def test_aucun_declencheur_incompatible_avec_checkout_v7(declencheur):
    """`checkout@v7` bloque le retrait d'une PR de fork sur ces deux declencheurs.

    Aucun workflow n'en utilise aujourd'hui. En ajouter un sans le savoir
    donnerait un retrait de code refuse, symptome qui ne nomme pas sa cause.
    """
    for chemin in sorted(WORKFLOWS.glob("*.yml")):
        contenu = yaml.safe_load(chemin.read_text(encoding="utf-8")) or {}
        # `on` est lu par YAML comme le booleen vrai : c'est la regle YAML 1.1,
        # et l'oublier ferait passer ce test sans rien regarder.
        declencheurs = contenu.get("on", contenu.get(True)) or {}
        if isinstance(declencheurs, dict):
            declencheurs = set(declencheurs)
        elif isinstance(declencheurs, str):
            declencheurs = {declencheurs}
        else:
            declencheurs = set(declencheurs)

        assert declencheur not in declencheurs, (
            f"{chemin.name} utilise `{declencheur}`, que `checkout@v7` traite "
            "differemment. Revoir la montee de version avant de l'employer."
        )


def test_lentree_supprimee_en_setup_python_v7_nest_pas_utilisee():
    """`pip-install` a disparu en v7 : l'employer ferait echouer l'etape."""
    for chemin in sorted(WORKFLOWS.glob("*.yml")):
        contenu = chemin.read_text(encoding="utf-8")

        assert "pip-install:" not in contenu, (
            f"{chemin.name} utilise `pip-install`, supprime dans setup-python v7."
        )
