"""Tests de la veille de collecte.

La veille repond a une question et une seule : quand le lac a-t-il ete ecrit
pour la derniere fois ? Elle existe parce que GitHub previent des workflows
qui ECHOUENT, pas de ceux qui reussissent sans rien produire.

Un moniteur se teste dans les deux sens. Celui qui n'alerte jamais est
inutile ; celui qui alerte toujours l'est tout autant, puisqu'on apprend a
l'ignorer. Les deux cas sont donc couverts, plus les deux facons de ne rien
savoir : lac vide et lac injoignable.
"""

from __future__ import annotations

import time

import pytest

from skytrace.cli import main
from skytrace.storage import newest_snapshot_age_seconds


@pytest.fixture
def lac(tmp_path, monkeypatch):
    """Un lac local, avec de quoi y deposer des releves dates."""
    monkeypatch.setenv("SKYTRACE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SKYTRACE_R2_BUCKET", "")

    from skytrace.config import get_settings

    get_settings.cache_clear()
    reglages = get_settings()
    reglages.ensure_directories()

    def deposer(*, age_heures: float) -> None:
        fichier = reglages.states_dir / f"states_{int(age_heures * 3600)}.parquet"
        fichier.parent.mkdir(parents=True, exist_ok=True)
        fichier.write_bytes(b"releve")
        quand = time.time() - age_heures * 3600
        import os

        os.utime(fichier, (quand, quand))

    yield deposer
    get_settings.cache_clear()


class TestAgeDuDernierReleve:
    def test_lac_vide(self, lac):
        assert newest_snapshot_age_seconds() is None

    def test_retient_le_plus_recent(self, lac):
        lac(age_heures=10)
        lac(age_heures=2)
        lac(age_heures=30)
        age = newest_snapshot_age_seconds()
        # C'est bien le plus RECENT qui compte : chercher le plus ancien
        # declencherait une alerte des le premier jour de collecte.
        assert age is not None
        assert 1.5 * 3600 < age < 2.5 * 3600


class TestCommandeWatchdog:
    def test_collecte_active(self, lac, capsys):
        lac(age_heures=1)
        assert main(["watchdog", "--max-age-hours", "6"]) == 0
        assert "Collecte active" in capsys.readouterr().out

    def test_collecte_arretee(self, lac):
        lac(age_heures=20)
        # Le contre-test : sans lui, un moniteur qui renvoie toujours zero
        # passerait pour fonctionnel.
        assert main(["watchdog", "--max-age-hours", "6"]) == 1

    def test_lac_vide_alerte(self, lac):
        # Aucun releve n'est un cas d'alerte, pas un cas neutre : c'est
        # exactement ce qu'on veut apprendre.
        assert main(["watchdog"]) == 1

    def test_seuil_par_defaut_tolere_les_retards_du_cron(self, lac):
        # Les ecarts mesures entre deux collectes depassent regulierement
        # trois heures. Le seuil doit les absorber, sinon la veille alerte en
        # permanence et plus personne ne la lit.
        lac(age_heures=3.5)
        assert main(["watchdog"]) == 0

    def test_lac_injoignable(self, lac, monkeypatch):
        def tomber(*_args, **_kwargs):
            raise OSError("stockage injoignable")

        monkeypatch.setattr("skytrace.storage.newest_snapshot_age_seconds", tomber)
        # Une panne d'acces au stockage doit alerter, pas remonter une trace.
        assert main(["watchdog"]) == 1
