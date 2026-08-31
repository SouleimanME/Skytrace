"""Verifie que les messages de commit respectent la convention du depot.

POURQUOI. Les messages de ce depot sont en francais NON ACCENTUE, comme les
commentaires et les docstrings du code. C'est un choix : un message de commit
traverse des terminaux, des interfaces web et des clients de courriel dont
l'encodage n'est pas garanti, et un accent mal rendu y devient illisible pour
toujours - on ne reecrit pas l'historique pour une cedille.

L'audit a trouve un seul ecart sur soixante-quatre commits, et il etait
incoherent avec lui-meme : "fiabilite" et "derive" sans accent, "modele" avec,
dans la meme phrase. Une convention qu'on applique de memoire finit toujours
ainsi.

CE QUI EST VERIFIE :
  * pas de caractere hors ASCII ;
  * un prefixe de type conventionnel (feat, fix, docs, ...) ;
  * un sujet ni vide ni interminable ;
  * pas de tiret cadratin, absent de tout le depot.

Usage :
    python scripts/verifier_commits.py HEAD~5..HEAD
    python scripts/verifier_commits.py HEAD -1     # depot peu profond
"""

from __future__ import annotations

import re
import subprocess
import sys

#: Types acceptes, convention Conventional Commits.
TYPES = ("feat", "fix", "docs", "test", "refactor", "perf", "chore", "build", "ci", "data")

PREFIXE = re.compile(rf"^({'|'.join(TYPES)})(\([a-z0-9_-]+\))?!?: .+")

#: Un sujet plus long ne s'affiche entier nulle part.
SUJET_MAX = 100


def verifier(sujet: str) -> list[str]:
    """Rend la liste des reproches. Vide si le message est conforme."""
    reproches = []

    hors_ascii = sorted({c for c in sujet if ord(c) > 127})
    if hors_ascii:
        reproches.append(f"caracteres hors ASCII : {' '.join(hors_ascii)}")

    if "\u2014" in sujet or "\u2013" in sujet:
        reproches.append("tiret cadratin ou demi-cadratin")

    if not PREFIXE.match(sujet):
        reproches.append(f"prefixe attendu parmi {', '.join(TYPES)} suivi de ': '")

    if len(sujet) > SUJET_MAX:
        reproches.append(f"sujet de {len(sujet)} caracteres, maximum {SUJET_MAX}")

    return reproches


def sujets(arguments: list[str]) -> list[tuple[str, str]]:
    """Couples (empreinte, sujet) pour les arguments git-log donnes.

    Une LISTE et non une seule chaine : l'appelant peut avoir besoin de
    `HEAD -1` quand le depot est clone en profondeur 1 et que `HEAD~1`
    n'existe pas. Refuser ce cas ferait echouer la verification la ou elle
    devrait simplement se restreindre.
    """
    # `git` est resolu par le PATH : c'est voulu, l'outil doit marcher
    # partout ou git est installe, sans chemin en dur.
    sortie = subprocess.run(  # noqa: S603
        ["git", "log", "--format=%H%x00%s", *arguments],  # noqa: S607
        capture_output=True,
        check=True,
    ).stdout.decode("utf-8", "replace")
    couples = []
    for ligne in sortie.splitlines():
        if "\x00" in ligne:
            sha, sujet = ligne.split("\x00", 1)
            couples.append((sha, sujet))
    return couples


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage : python scripts/verifier_commits.py <arguments git-log>", file=sys.stderr)
        return 2

    try:
        couples = sujets(argv[1:])
    except subprocess.CalledProcessError as exc:
        print(f"plage git invalide : {exc}", file=sys.stderr)
        return 2

    if not couples:
        print("Aucun commit dans cette plage.")
        return 0

    fautifs = 0
    for sha, sujet in couples:
        reproches = verifier(sujet)
        if reproches:
            fautifs += 1
            print(f"\n{sha[:8]} : {sujet}", file=sys.stderr)
            for r in reproches:
                print(f"    - {r}", file=sys.stderr)

    if fautifs:
        print(f"\n{fautifs} commit(s) non conforme(s) sur {len(couples)}.", file=sys.stderr)
        return 1

    print(f"{len(couples)} commit(s) conforme(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
