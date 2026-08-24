"""Garde-fou : les colonnes exigees par le tableau de bord existent en dbt.

Ce test existe a cause d'une panne en production.

`fct_aircraft_positions` est incremental. Y ajouter une colonne ne
retro-remplit pas les lignes deja chargees : elles restent a NULL. Un test
`not_null` sur cette colonne echoue alors sur l'historique entier,
`dbt build` s'arrete, et tout l'aval est ignore - dont les dimensions, qui
restent a l'ancien schema. Le tableau de bord, lui, avait ete deploye avec le
code qui lit ces colonnes : il s'est effondre sur une BinderException.

Le code se defend maintenant a l'execution en forcant une reconstruction
complete. Ce test attaque le probleme un cran plus tot : la liste
`REQUIRED_COLUMNS` du tableau de bord doit correspondre a des colonnes que
les modeles dbt declarent reellement. Une faute de frappe la-dedans
provoquerait une reconstruction complete a chaque affichage, en boucle et
sans jamais reussir.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
import yaml

RACINE = Path(__file__).resolve().parents[1]
APP = RACINE / "dashboard" / "app.py"
MARTS = RACINE / "dbt" / "skytrace" / "models" / "marts"


@pytest.fixture(scope="module")
def colonnes_exigees() -> dict[str, tuple[str, ...]]:
    """Lit REQUIRED_COLUMNS sans importer le tableau de bord.

    L'importer lancerait Streamlit et se connecterait a l'entrepot ; on se
    contente de lire l'affectation dans l'arbre syntaxique.
    """
    for noeud in ast.walk(ast.parse(APP.read_text(encoding="utf-8"))):
        est_affectation = (
            isinstance(noeud, ast.Assign)
            and len(noeud.targets) == 1
            and isinstance(noeud.targets[0], ast.Name)
            and noeud.targets[0].id == "REQUIRED_COLUMNS"
        )
        if est_affectation:
            return ast.literal_eval(noeud.value)
    pytest.fail("REQUIRED_COLUMNS introuvable dans dashboard/app.py")
    return {}


@pytest.fixture(scope="module")
def colonnes_declarees() -> dict[str, set[str]]:
    """Colonnes que les modeles marts produisent, selon le SQL et le schema."""
    par_modele: dict[str, set[str]] = {}
    schema = yaml.safe_load((MARTS / "_marts.yml").read_text(encoding="utf-8"))
    for modele in schema.get("models", []):
        par_modele[modele["name"]] = {c["name"] for c in modele.get("columns", [])}

    # Le schema ne documente pas tout : on complete avec les alias du SQL,
    # sinon le test exigerait de documenter chaque colonne pour passer.
    for fichier in MARTS.glob("*.sql"):
        alias = set(
            re.findall(r"\bas\s+([a-z_][a-z0-9_]*)\s*,", fichier.read_text(encoding="utf-8"), re.I)
        )
        par_modele.setdefault(fichier.stem, set()).update(a.lower() for a in alias)
    return par_modele


def test_les_modeles_marts_sont_bien_lus(colonnes_declarees):
    # Si l'extraction cassait, le test suivant passerait pour de mauvaises
    # raisons : il ne verifierait plus rien.
    assert "fct_aircraft_positions" in colonnes_declarees
    assert "dim_aircraft" in colonnes_declarees


def test_chaque_colonne_exigee_existe_en_dbt(colonnes_exigees, colonnes_declarees):
    introuvables = []
    for table, colonnes in colonnes_exigees.items():
        modele = table.split(".")[-1]
        connues = colonnes_declarees.get(modele)
        assert connues is not None, f"modele dbt inconnu : {modele}"
        introuvables += [f"{modele}.{c}" for c in colonnes if c not in connues]

    assert introuvables == [], (
        "colonnes exigees par le tableau de bord mais produites par aucun "
        f"modele marts : {introuvables}. Le code forcerait une reconstruction "
        "complete a chaque affichage, sans jamais y parvenir."
    )


def test_le_modele_incremental_avertit_du_piege():
    # La documentation du piege fait partie du correctif : sans elle, la
    # prochaine colonne ajoutee rejouera la meme panne.
    modele = (MARTS / "fct_aircraft_positions.sql").read_text(encoding="utf-8")
    assert "full-refresh" in modele.lower()
    assert "append_new_columns" in modele
