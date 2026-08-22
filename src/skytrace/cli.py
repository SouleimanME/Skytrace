"""Point d'entree en ligne de commande de SkyTrace.

Une seule commande pour tout le pipeline, de facon que le projet se pilote
de la meme maniere depuis un terminal, depuis Dagster ou depuis la CI.

    skytrace ingest-states       # un snapshot du trafic -> couche bronze
    skytrace ingest-airports     # rafraichit le referentiel aeroports
    skytrace ingest-air-quality  # qualite de l'air autour des aeroports
    skytrace dbt build           # transforme + teste (dbt est appele ici)
    skytrace pipeline            # ingestion + transformation, en une fois
    skytrace dagster             # orchestrateur, avec etat persistant
    skytrace info                # etat de l'entrepot et des quotas
    skytrace dashboard           # lance le tableau de bord Streamlit
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

from skytrace.config import PROJECT_ROOT, get_settings
from skytrace.logging_conf import configure_logging, get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Commandes
# ---------------------------------------------------------------------------
def cmd_ingest_states(_: argparse.Namespace) -> int:
    from skytrace.ingestion import ingest_states
    from skytrace.opensky.client import CreditBudgetExceededError

    settings = get_settings()
    try:
        result = ingest_states(settings)
    except CreditBudgetExceededError as exc:
        logger.error("%s", exc)
        return 2

    print(
        f"{result.rows} aeronefs | {result.snapshot_at:%Y-%m-%d %H:%M:%S} UTC "
        f"| {result.size_bytes / 1024:.1f} Ko -> {result.path}"
    )
    return 0


def cmd_ingest_airports(_: argparse.Namespace) -> int:
    from skytrace.ingestion import ingest_airports

    result = ingest_airports(get_settings())
    print(f"{result.rows} aeroports -> {result.path}")
    return 0


def cmd_ingest_air_quality(_: argparse.Namespace) -> int:
    from skytrace.ingestion import ingest_air_quality

    result = ingest_air_quality(get_settings())
    print(
        f"{result.rows} lignes | {result.airports} aeroports "
        f"| {result.start_date} -> {result.end_date} -> {result.path}"
    )
    return 0


def cmd_dbt(args: argparse.Namespace) -> int:
    """Passe-plat vers dbt, avec l'environnement correctement prepare.

    dbt ne lit ni le `.env` du projet ni la configuration Python : les
    chemins du lac et de l'entrepot doivent lui etre transmis en absolu,
    et le repertoire de travail doit etre celui du projet dbt.
    """
    settings = get_settings()
    settings.ensure_directories()

    env = {**os.environ, **settings.dbt_env()}
    command = [
        sys.executable,
        "-m",
        "dbt.cli.main",
        *args.dbt_args,
        "--project-dir",
        str(settings.dbt_project_dir),
        "--profiles-dir",
        str(settings.dbt_project_dir),
    ]

    logger.info("dbt %s", " ".join(args.dbt_args))
    return subprocess.run(command, env=env, cwd=str(settings.dbt_project_dir)).returncode


def cmd_dagster(args: argparse.Namespace) -> int:
    """Lance l'interface Dagster avec un etat persistant.

    `dagster dev` lance sans `DAGSTER_HOME` cree un repertoire temporaire
    qu'il supprime en sortant : l'historique des materialisations, l'etat
    actif/inactif des plannings et les journaux de run sont perdus a chaque
    redemarrage. On force donc un repertoire du projet.
    """
    settings = get_settings()
    settings.ensure_directories()

    dagster_home = PROJECT_ROOT / ".dagster_home"
    dagster_home.mkdir(parents=True, exist_ok=True)

    env = {
        **os.environ,
        **settings.dbt_env(),
        "DAGSTER_HOME": str(dagster_home),
        # `orchestration` est un paquet a la racine, pas dans `src/`.
        "PYTHONPATH": os.pathsep.join(
            filter(None, [str(PROJECT_ROOT), os.environ.get("PYTHONPATH", "")])
        ),
    }
    # Sous Windows, Dagster n'archive pas la sortie des etapes sans cette
    # variable - les journaux restent alors introuvables depuis l'interface.
    env.setdefault("PYTHONLEGACYWINDOWSSTDIO", "1")

    command = [
        sys.executable,
        "-m",
        "dagster",
        "dev",
        "-m",
        "orchestration.definitions",
        "--port",
        str(args.port),
    ]

    logger.info("Interface Dagster : http://localhost:%d", args.port)
    logger.info("Etat persistant   : %s", dagster_home)
    return subprocess.run(command, env=env, cwd=str(PROJECT_ROOT)).returncode


def cmd_info(_: argparse.Namespace) -> int:
    from skytrace.opensky.client import CreditLedger
    from skytrace.warehouse import describe_warehouse

    settings = get_settings()
    ledger = CreditLedger(
        settings.resolved_data_dir / ".credit_ledger.json",
        settings.daily_credit_budget,
    )

    print("=== Configuration ===")
    print(f"  zone          : {settings.region} ({settings.bbox.credit_cost} credits/appel)")
    print(f"  authentifie   : {'non (mode anonyme)' if settings.is_anonymous else 'oui (OAuth2)'}")
    print(f"  lac de donnees: {settings.resolved_data_dir}")
    print(f"  entrepot      : {settings.resolved_duckdb_path}")

    print("\n=== Quotas OpenSky (aujourd'hui) ===")
    print(
        f"  {ledger.spent_today()} / {settings.daily_credit_budget} credits consommes"
        f"  ({ledger.remaining_today()} restants)"
    )

    snapshots = sorted(settings.states_dir.rglob("*.parquet"))
    total_bytes = sum(path.stat().st_size for path in snapshots)
    print("\n=== Couche bronze ===")
    print(f"  {len(snapshots)} snapshots | {total_bytes / 1_048_576:.1f} Mo")
    if snapshots:
        print(f"  plus ancien : {snapshots[0].name}")
        print(f"  plus recent : {snapshots[-1].name}")

    tables = describe_warehouse(settings)
    print("\n=== Entrepot DuckDB ===")
    if not tables:
        print("  (vide - lancer `skytrace dbt build`)")
    else:
        width = max(len(f"{t.schema}.{t.name}") for t in tables)
        for table in tables:
            label = f"{table.schema}.{table.name}"
            print(f"  {label:<{width}}  {table.rows:>12,} lignes".replace(",", " "))
    return 0


def cmd_dashboard(args: argparse.Namespace) -> int:
    app = PROJECT_ROOT / "dashboard" / "app.py"
    if not app.exists():
        logger.error("Tableau de bord introuvable : %s", app)
        return 1

    command = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(app),
        "--server.port",
        str(args.port),
    ]

    # Le mode headless (voir .streamlit/config.toml) supprime l'invite
    # e-mail de Streamlit mais n'ouvre plus le navigateur : on affiche
    # l'adresse nous-memes.
    logger.info("Tableau de bord : http://localhost:%d", args.port)
    return subprocess.run(command, cwd=str(PROJECT_ROOT)).returncode


def cmd_pipeline(args: argparse.Namespace) -> int:
    """Enchaine ingestion + transformation : le pipeline complet en une commande."""
    if (code := cmd_ingest_states(args)) != 0:
        return code

    if args.with_reference:
        if (code := cmd_ingest_airports(args)) != 0:
            return code
    elif not (get_settings().airports_dir / "airports.parquet").exists():
        logger.info("Referentiel aeroports absent : premier telechargement")
        if (code := cmd_ingest_airports(args)) != 0:
            return code

    # Qualite de l'air : dimension a evolution lente. On la rafraichit si elle
    # est absente ou perimee (plus de 6 h), pas a chaque cycle de 15/30 min -
    # les valeurs passees ne changent plus. Un echec ici n'interrompt pas le
    # pipeline : la source air quality est secondaire.
    _maybe_refresh_air_quality(get_settings())

    build_args = argparse.Namespace(dbt_args=["build"])
    return cmd_dbt(build_args)


def _maybe_refresh_air_quality(settings, *, max_age_hours: float = 6.0) -> None:
    import time

    from skytrace.ingestion import ingest_air_quality

    path = settings.air_quality_dir / "air_quality.parquet"
    fresh = path.exists() and (time.time() - path.stat().st_mtime) < max_age_hours * 3600
    if fresh:
        return
    try:
        result = ingest_air_quality(settings)
        logger.info("Qualite de l'air rafraichie : %d lignes", result.rows)
    except Exception as exc:  # noqa: BLE001 - source secondaire, on n'interrompt pas
        logger.warning("Rafraichissement qualite de l'air ignore : %s", exc)


# ---------------------------------------------------------------------------
# Analyse des arguments
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="skytrace",
        description="Pipeline de donnees sur le trafic aerien (OpenSky Network).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--log-level",
        default=None,
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Verbosite des journaux (defaut : SKYTRACE_LOG_LEVEL).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    states = subparsers.add_parser(
        "ingest-states", help="Recupere un snapshot du trafic vers la couche bronze."
    )
    states.set_defaults(handler=cmd_ingest_states)

    airports = subparsers.add_parser(
        "ingest-airports", help="Rafraichit le referentiel aeroports OurAirports."
    )
    airports.set_defaults(handler=cmd_ingest_airports)

    air_quality = subparsers.add_parser(
        "ingest-air-quality",
        help="Rafraichit la qualite de l'air (Open-Meteo) autour des aeroports.",
    )
    air_quality.set_defaults(handler=cmd_ingest_air_quality)

    dbt = subparsers.add_parser(
        "dbt",
        help="Execute dbt avec l'environnement du projet (ex : skytrace dbt build).",
    )
    dbt.add_argument("dbt_args", nargs=argparse.REMAINDER, help="Arguments passes a dbt.")
    dbt.set_defaults(handler=cmd_dbt)

    pipeline = subparsers.add_parser(
        "pipeline", help="Ingestion puis transformation et tests, en une commande."
    )
    pipeline.add_argument(
        "--with-reference",
        action="store_true",
        help="Force le rafraichissement du referentiel aeroports.",
    )
    pipeline.set_defaults(handler=cmd_pipeline)

    dagster = subparsers.add_parser(
        "dagster",
        help="Lance l'orchestrateur Dagster avec un etat persistant.",
    )
    dagster.add_argument("--port", type=int, default=3000)
    dagster.set_defaults(handler=cmd_dagster)

    info = subparsers.add_parser("info", help="Etat du lac, de l'entrepot et des quotas.")
    info.set_defaults(handler=cmd_info)

    dashboard = subparsers.add_parser("dashboard", help="Lance le tableau de bord Streamlit.")
    dashboard.add_argument("--port", type=int, default=8501)
    dashboard.set_defaults(handler=cmd_dashboard)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(args.log_level or settings.log_level)

    return int(args.handler(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
