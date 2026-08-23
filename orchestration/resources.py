"""Ressources Dagster : dependances externes injectees dans les assets.

Le principe des ressources est de sortir les acces au monde exterieur du
code des assets. Un asset ne sait pas comment on joint OpenSky : il recoit
une ressource qui sait le faire. En test, on injecte une fausse ressource
et l'asset devient verifiable sans reseau.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from dagster import ConfigurableResource
from dagster_dbt import DbtCliResource, DbtProject

from skytrace.config import PROJECT_ROOT, Settings, get_settings
from skytrace.ingestion import IngestedReference, IngestedSnapshot, ingest_airports, ingest_states

# ---------------------------------------------------------------------------
# Projet dbt
# ---------------------------------------------------------------------------
DBT_PROJECT_DIR = PROJECT_ROOT / "dbt" / "skytrace"

# dbt est un sous-process : il ne voit ni la configuration Python ni le
# fichier `.env`. Les chemins doivent donc etre pousses dans l'environnement
# du process avant que Dagster ne lance quoi que ce soit. `setdefault` pour
# ne jamais ecraser une valeur deja fournie par l'operateur ou par Docker.
for _key, _value in get_settings().dbt_env().items():
    os.environ.setdefault(_key, _value)

dbt_project = DbtProject(
    project_dir=DBT_PROJECT_DIR,
    profiles_dir=DBT_PROJECT_DIR,
)

# En developpement, regenere `manifest.json` a chaque demarrage pour que
# l'interface Dagster reflete les modeles en cours d'ecriture. En production
# le manifeste est genere au build de l'image (voir Dockerfile).
dbt_project.prepare_if_dev()


def ensure_manifest() -> None:
    """Genere `manifest.json` s'il est absent.

    `prepare_if_dev()` ne fait rien hors mode developpement. Sur un depot
    fraichement clone (CI, machine d'un collegue), le manifeste n'existe donc
    pas et `@dbt_assets` echoue avec DagsterDbtManifestNotFoundError. On le
    fabrique ici, une fois, plutot que d'imposer un `dbt parse` prealable a
    quiconque veut simplement lancer l'orchestrateur.
    """
    if dbt_project.manifest_path.exists():
        return

    import subprocess
    import sys as _sys

    settings = get_settings()
    subprocess.run(
        [
            _sys.executable,
            "-m",
            "dbt.cli.main",
            "parse",
            "--project-dir",
            str(DBT_PROJECT_DIR),
            "--profiles-dir",
            str(DBT_PROJECT_DIR),
            "--target",
            settings.dbt_target,
        ],
        env={**os.environ, **settings.dbt_env()},
        cwd=str(DBT_PROJECT_DIR),
        check=True,
    )


ensure_manifest()


class OpenSkyResource(ConfigurableResource):
    """Acces a l'API OpenSky, configurable depuis l'interface Dagster.

    `region` permet de relancer un asset sur une autre zone geographique
    sans toucher au code ni redeployer : c'est de la configuration
    d'execution, pas du parametrage en dur.
    """

    region: str | None = None

    @property
    def settings(self) -> Settings:
        base = get_settings()
        if self.region and self.region != base.region:
            return base.model_copy(update={"region": self.region})
        return base

    def ingest_snapshot(self) -> IngestedSnapshot:
        return ingest_states(self.settings)

    def ingest_reference(self) -> IngestedReference:
        return ingest_airports(self.settings)


def resolve_dbt_executable() -> str:
    """Localise le binaire dbt a utiliser.

    Dagster cherche `dbt` dans le PATH. Or un environnement virtuel non
    active - le cas quand on lance `python -m dagster` - n'expose pas son
    repertoire `Scripts/` (ou `bin/`). On resout donc l'executable a cote
    de l'interpreteur courant, ce qui garantit que dbt et le code Python
    partagent bien le meme environnement.
    """
    scripts_dir = Path(sys.executable).parent
    for candidate in ("dbt.exe", "dbt"):
        local = scripts_dir / candidate
        if local.exists():
            return str(local)

    found = shutil.which("dbt")
    if found:
        return found

    raise RuntimeError(
        "Executable dbt introuvable. Installer les dependances de "
        "transformation : pip install -e .[transform]"
    )


def build_resources() -> dict[str, object]:
    return {
        "opensky": OpenSkyResource(),
        "dbt": DbtCliResource(
            project_dir=dbt_project,
            profiles_dir=str(DBT_PROJECT_DIR),
            dbt_executable=resolve_dbt_executable(),
            # Cible explicite : `r2` quand le lac est sur R2, `dev` sinon.
            # Sans cela dbt retomberait sur la cible par defaut (`dev`), qui
            # n'a pas la configuration S3 - et echouerait a lire des sources
            # en s3://.
            target=get_settings().dbt_target,
        ),
    }
