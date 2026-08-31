"""Point d'entree en ligne de commande de SkyTrace.

Une seule commande pour tout le pipeline, de facon que le projet se pilote
de la meme maniere depuis un terminal, depuis Dagster ou depuis la CI.

    skytrace ingest-states       # un snapshot du trafic -> couche bronze
    skytrace ingest-airports     # rafraichit le referentiel aeroports
    skytrace ingest-air-quality  # qualite de l'air autour des aeroports
    skytrace ingest-fleet        # base aeronefs + compagnies (type, operateur)
    skytrace dbt build           # transforme + teste (dbt est appele ici)
    skytrace pipeline            # ingestion + transformation, en une fois
    skytrace dagster             # orchestrateur, avec etat persistant
    skytrace info                # etat de l'entrepot et des quotas
    skytrace dashboard           # lance le tableau de bord Streamlit
    skytrace watchdog            # echoue si la collecte s'est arretee
    skytrace publier-sante       # publie l'etat du pipeline en JSON sur le lac
    skytrace backfill            # comble les trous de la serie (compte OpenSky requis)
    skytrace model train         # entraine et enregistre le classifieur
    skytrace model info          # decrit la version en service
    skytrace model predict <adresses OACI>
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


def cmd_ingest_fleet(_: argparse.Namespace) -> int:
    from skytrace.ingestion import ingest_aircraft_db, ingest_airlines

    settings = get_settings()
    airlines = ingest_airlines(settings)
    aircraft = ingest_aircraft_db(settings)
    print(f"{aircraft.rows} aeronefs | {airlines.rows} compagnies")
    return 0


def cmd_storage_check(_: argparse.Namespace) -> int:
    from skytrace.storage import check_connectivity

    settings = get_settings()
    backend = "R2" if settings.uses_r2 else "disque local"
    try:
        uri = check_connectivity(settings)
    except Exception as exc:  # noqa: BLE001 - message clair pour l'operateur
        logger.error("Acces au lac (%s) impossible : %s", backend, exc)
        return 1
    print(f"Acces au lac OK ({backend}) : ecriture/lecture reussie -> {uri}")
    return 0


def cmd_watchdog(args: argparse.Namespace) -> int:
    """Echoue si la collecte s'est arretee. Destine a une tache planifiee.

    POURQUOI CETTE COMMANDE EXISTE. GitHub previent deja par courriel quand un
    workflow ECHOUE. Il ne dit rien de trois pannes silencieuses, pourtant les
    plus probables :

      * le workflow reussit mais n'ecrit rien (source muette, quota epuise) ;
      * GitHub desactive les taches planifiees d'un depot public reste
        soixante jours sans commit ;
      * les identifiants du stockage expirent.

    Dans ces trois cas tout reste vert et la donnee cesse d'arriver. La seule
    question qui les couvre toutes est donc : "quand le lac a-t-il ete ecrit
    pour la derniere fois ?".

    Le seuil par defaut est large a dessein. Les ecarts mesures entre deux
    collectes vont de la minute a plus de trois heures - le cron GitHub n'est
    pas ponctuel - et un seuil serre alerterait en permanence, c'est-a-dire
    n'alerterait plus.
    """
    from skytrace.storage import newest_snapshot_age_seconds

    settings = get_settings()
    try:
        age = newest_snapshot_age_seconds(settings)
    except Exception as exc:  # noqa: BLE001 - message clair pour l'operateur
        logger.error("Lac inaccessible : %s", exc)
        return 1

    if age is None:
        logger.error("Aucun releve dans le lac : la collecte n'a jamais abouti.")
        return 1

    heures = age / 3600
    if heures > args.max_age_hours:
        logger.error(
            "Collecte a l'arret : dernier releve il y a %.1f h (seuil %.1f h).",
            heures,
            args.max_age_hours,
        )
        return 1

    print(f"Collecte active : dernier releve il y a {heures:.1f} h.")
    return 0


def cmd_backfill(args: argparse.Namespace) -> int:
    """Comble les trous de la serie en rejouant l'historique OpenSky.

    NE FAIT RIEN SANS IDENTIFIANTS, et sort en 0 dans ce cas. C'est
    volontaire : cette commande tourne dans une tache planifiee, et faire
    echouer un workflow parce qu'une capacite optionnelle n'est pas
    configuree enverrait une alerte qui ne demande aucune action.
    """
    from skytrace.ingestion.states import backfill_gaps

    resultat = backfill_gaps(
        step_hours=args.step_hours,
        max_snapshots=args.max_snapshots,
        min_gap_hours=args.min_gap_hours,
    )

    if resultat.skipped_reason:
        logger.info("Rattrapage inactif : %s", resultat.skipped_reason)
        return 0

    if not resultat.gaps_found:
        print("Aucun trou a combler.")
        return 0

    print(
        f"{resultat.gaps_found} trou(s) reperes, "
        f"{resultat.snapshots_written} releve(s) rattrape(s), "
        f"{resultat.hours_recovered:.0f} h recuperees."
    )
    return 0


def cmd_publier_sante(_: argparse.Namespace) -> int:
    """Publie l'etat du pipeline en JSON, lisible sans identifiants.

    A APPELER APRES CHAQUE COLLECTE, jamais seul dans une tache a part : le
    document doit refleter la derniere ecriture reelle, et un publieur qui
    tourne quand la collecte echoue publierait une fraicheur mensongere.
    """
    from skytrace.storage import build_health_document, publish_health

    document = build_health_document()
    try:
        resultat = publish_health()
    except Exception as exc:  # noqa: BLE001 - message clair pour l'operateur
        logger.error("Publication de la sante impossible : %s", exc)
        return 1

    print(f"Etat : {document['etat']} -> {resultat.uri}")
    if document["dernier_releve_il_y_a_heures"] is not None:
        print(f"  dernier releve il y a {document['dernier_releve_il_y_a_heures']:.2f} h")
    return 0


def cmd_model(args: argparse.Namespace) -> int:
    """Entraine, inspecte ou interroge le classifieur d'appareils."""
    from skytrace.ml import (
        OBSERVATION_COHORTS,
        build_dataset,
        list_versions,
        load_model,
        model_dir,
        predict_aircraft,
        save_model,
        train_and_evaluate,
    )
    from skytrace.warehouse.duck import connect

    if args.action == "train":
        with connect() as connection:
            jeu = build_dataset(connection)
        if len(jeu) < 200:
            logger.error("Pas assez d'appareils etiquetes (%d) pour entrainer.", len(jeu))
            return 1

        modele, fiche = train_and_evaluate(jeu)
        chemin = save_model(modele, fiche)

        print(f"Modele entraine sur {fiche.n_train} appareils, teste sur {fiche.n_test}.")
        print(f"  ligne de base ({fiche.baseline_feature}) : {fiche.baseline_score:.4f}")
        print(f"  modele                                    : {fiche.model_score:.4f}")
        print(f"  gain reel                                 : {fiche.gain:+.4f}")
        if not fiche.is_worth_it():
            # On enregistre quand meme - le refus doit etre visible et
            # decide par un humain, pas silencieusement impose.
            logger.warning(
                "Gain inferieur au seuil : une regle d'une ligne ferait presque aussi bien."
            )
        print(f"\nEnregistre : {chemin}")
        return 0

    if args.action == "info":
        try:
            _, fiche = load_model()
        except FileNotFoundError as exc:
            logger.error("%s", exc)
            return 1
        print(f"Modele courant, entraine le {fiche.trained_at}")
        print(f"  variables      : {', '.join(fiche.features)}")
        print(f"  appareils      : {fiche.n_train} entrainement / {fiche.n_test} test")
        print(f"  score          : {fiche.model_score:.4f}")
        print(f"  ligne de base  : {fiche.baseline_score:.4f} ({fiche.baseline_feature})")
        print(f"  gain           : {fiche.gain:+.4f}")
        if fiche.scores_by_observations:
            # Le score global est une moyenne sur des populations tres
            # inegales. Le detailler evite de promettre a un appareil vu une
            # fois ce qui n'a ete mesure que sur des appareils bien suivis.
            print("  fiabilite par nombre de releves :")
            for _, _, libelle in OBSERVATION_COHORTS:
                mesure = fiche.scores_by_observations.get(libelle)
                if mesure:
                    print(
                        f"    {libelle:<20} {mesure['score']:.4f}"
                        f"  ({mesure['n_test']} appareils de test)"
                    )
        versions = list_versions()
        print(f"\n{len(versions)} version(s) dans {model_dir()} :")
        for v in versions[:5]:
            print(f"  {v}")
        return 0

    if args.action == "score":
        from skytrace.ml import score_all
        from skytrace.storage import write_parquet

        try:
            modele, fiche = load_model()
        except FileNotFoundError as exc:
            logger.error("%s", exc)
            return 1

        with connect() as connection:
            scores = score_all(connection, modele, fiche)

        # Les scores vont dans le LAC, pas dans l'entrepot. Celui-ci est
        # reconstruit a intervalle regulier sur le deploiement public : une
        # table ecrite hors dbt disparaitrait au premier reveil. Dans le lac,
        # elle devient une source que dbt relit comme les autres.
        import pyarrow as pa

        resultat = write_parquet(
            "model_predictions/aircraft_class.parquet",
            pa.Table.from_pandas(scores, preserve_index=False),
            get_settings(),
        )
        commerciaux = int(scores["predicted_commercial"].sum())
        print(f"{len(scores)} appareils scores -> {resultat.uri}")
        print(f"  transport commercial : {commerciaux} ({commerciaux / len(scores):.0%})")
        print(f"  aviation generale    : {len(scores) - commerciaux}")
        return 0

    # action == "predict"
    try:
        modele, fiche = load_model()
    except FileNotFoundError as exc:
        logger.error("%s", exc)
        return 1

    with connect() as connection:
        resultats = predict_aircraft(connection, modele, args.icao24)

    if resultats.empty:
        logger.error(
            "Aucun de ces appareils n'apparait dans la fenetre de collecte. "
            "Verifier l'adresse OACI, ou lancer `skytrace pipeline`."
        )
        return 1

    for _, ligne in resultats.iterrows():
        declare = ligne["manufacturer_group"]
        connu = declare not in (None, "Inconnu") and declare == declare  # ecarte NaN
        print(f"\n{ligne['aircraft_icao24']}  {ligne['registration'] or ''}".rstrip())
        if ligne["most_frequent_callsign"]:
            print(f"  indicatif habituel  : {ligne['most_frequent_callsign']}")
        print(f"  classe predite      : {ligne['classe_predite']}")
        print(
            f"  confiance           : {max(ligne['probabilite_commercial'], 1 - ligne['probabilite_commercial']):.0%}"
        )
        print(
            f"  constructeur declare: {declare}"
            if connu
            else "  constructeur declare: inconnu (c'est le cas que le modele comble)"
        )
        vues = int(ligne["observations"])
        print(
            f"  observe             : {vues} releve{'s' if vues > 1 else ''}, "
            f"{ligne['altitude_mediane_ft']:,.0f} ft median, "
            f"{ligne['vitesse_max_kt']:,.0f} kt max".replace(",", " ")
        )
        # La fiabilite annoncee est celle de la cohorte de l'appareil. Servir
        # le score global a un appareil vu une seule fois lui promettrait une
        # exactitude mesuree sur d'autres que lui.
        for plancher, plafond, libelle in OBSERVATION_COHORTS:
            if vues < plancher or (plafond is not None and vues > plafond):
                continue
            mesure = fiche.scores_by_observations.get(libelle)
            if mesure:
                print(
                    f"  fiabilite ici       : {mesure['score']:.3f} "
                    f"(mesuree sur les appareils {libelle})"
                )
            break
    print(
        f"\nModele du {fiche.trained_at[:10]}, exactitude equilibree "
        f"{fiche.model_score:.3f} toutes cohortes confondues. Un appareil peu vu "
        "est classe malgre tout, mais sa fiabilite propre est rappelee ci-dessus."
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
    # Cible r2 quand le lac est sur R2, dev sinon. On ne l'ajoute que si
    # l'appelant ne l'a pas deja precisee.
    if "--target" not in args.dbt_args and "-t" not in args.dbt_args:
        command += ["--target", settings.dbt_target]

    logger.info("dbt %s (cible %s)", " ".join(args.dbt_args), settings.dbt_target)
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
        settings.resolved_credit_budget,
    )

    print("=== Configuration ===")
    print(f"  zone          : {settings.region} ({settings.bbox.credit_cost} credits/appel)")
    print(f"  authentifie   : {'non (mode anonyme)' if settings.is_anonymous else 'oui (OAuth2)'}")
    print(f"  lac de donnees: {settings.resolved_data_dir}")
    print(f"  entrepot      : {settings.resolved_duckdb_path}")

    print("\n=== Quotas OpenSky (aujourd'hui) ===")
    print(
        f"  {ledger.spent_today()} / {settings.resolved_credit_budget} credits consommes"
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

    from skytrace.storage import object_age_seconds

    airports_age = object_age_seconds("ourairports/airports.parquet", get_settings())
    airports_stale = airports_age is None or airports_age > 86400
    # Absent ou vieux de plus d'un jour : (re)telecharge le referentiel.
    if (args.with_reference or airports_stale) and (code := cmd_ingest_airports(args)) != 0:
        return code

    # Qualite de l'air : dimension a evolution lente. On la rafraichit si elle
    # est absente ou perimee (plus de 6 h), pas a chaque cycle de 15/30 min -
    # les valeurs passees ne changent plus. Un echec ici n'interrompt pas le
    # pipeline : la source air quality est secondaire.
    _maybe_refresh_air_quality(get_settings())

    # Referentiels flotte (base aeronefs + compagnies) : evolution tres lente,
    # rafraichissement mensuel. Le telechargement de la base fait ~95 Mo, on
    # evite donc de le refaire a chaque cycle.
    _maybe_refresh_fleet(get_settings())

    # Retention : borne le stockage (rester sous le palier gratuit R2).
    from skytrace.storage import prune_old_states

    settings = get_settings()
    try:
        prune_old_states(settings.retention_days, settings)
    except Exception as exc:  # noqa: BLE001 - la purge ne doit pas casser la collecte
        logger.warning("Retention ignoree : %s", exc)

    build_args = argparse.Namespace(dbt_args=["build"])
    return cmd_dbt(build_args)


def _maybe_refresh_air_quality(settings, *, max_age_hours: float = 6.0) -> None:
    from skytrace.ingestion import ingest_air_quality
    from skytrace.storage import object_age_seconds

    age = object_age_seconds("open_meteo_air_quality/air_quality.parquet", settings)
    if age is not None and age < max_age_hours * 3600:
        return
    try:
        result = ingest_air_quality(settings)
        logger.info("Qualite de l'air rafraichie : %d lignes", result.rows)
    except Exception as exc:  # noqa: BLE001 - source secondaire, on n'interrompt pas
        logger.warning("Rafraichissement qualite de l'air ignore : %s", exc)


def _maybe_refresh_fleet(settings, *, max_age_days: float = 25.0) -> None:
    from skytrace.ingestion import ingest_aircraft_db, ingest_airlines
    from skytrace.storage import object_age_seconds

    aircraft_age = object_age_seconds("opensky_aircraft_db/aircraft_database.parquet", settings)
    airlines_age = object_age_seconds("openflights_airlines/airlines.parquet", settings)
    fresh = (
        aircraft_age is not None
        and airlines_age is not None
        and aircraft_age < max_age_days * 86400
    )
    if fresh:
        return
    try:
        ingest_airlines(settings)
        result = ingest_aircraft_db(settings)
        logger.info("Referentiels flotte rafraichis : %d aeronefs", result.rows)
    except Exception as exc:  # noqa: BLE001 - source secondaire, on n'interrompt pas
        logger.warning("Rafraichissement flotte ignore : %s", exc)


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

    fleet = subparsers.add_parser(
        "ingest-fleet",
        help="Rafraichit la base aeronefs (OpenSky) et les compagnies (OpenFlights).",
    )
    fleet.set_defaults(handler=cmd_ingest_fleet)

    storage = subparsers.add_parser(
        "storage-check",
        help="Verifie l'acces au lac de donnees (local ou R2).",
    )
    storage.set_defaults(handler=cmd_storage_check)

    watchdog = subparsers.add_parser(
        "watchdog",
        help="Echoue si la collecte s'est arretee (pour une tache planifiee).",
    )
    watchdog.add_argument(
        "--max-age-hours",
        type=float,
        default=10.0,
        help=(
            "Age maximal tolere du dernier releve, en heures. Large a dessein : "
            "le cron GitHub n'est pas ponctuel et les ecarts mesures depassent "
            "regulierement trois heures, et la collecte est passee a une par "
            "heure (defaut : 10)."
        ),
    )
    watchdog.set_defaults(handler=cmd_watchdog)

    sante = subparsers.add_parser(
        "publier-sante",
        help="Publie l'etat du pipeline en JSON a la racine du lac.",
    )
    sante.set_defaults(handler=cmd_publier_sante)

    rattrapage = subparsers.add_parser(
        "backfill",
        help="Comble les trous de la serie en rejouant l'historique OpenSky.",
    )
    rattrapage.add_argument(
        "--min-gap-hours",
        type=float,
        default=3.0,
        help=(
            "Taille minimale d'un trou a combler, en heures. En deca, "
            "l'irregularite du cron suffit a l'expliquer (defaut : 3)."
        ),
    )
    rattrapage.add_argument(
        "--step-hours",
        type=float,
        default=1.0,
        help="Pas de rattrapage, en heures (defaut : 1, la cadence nominale).",
    )
    rattrapage.add_argument(
        "--max-snapshots",
        type=int,
        default=24,
        help=(
            "Plafond de releves rattrapes par execution. Borne la depense en "
            "credits : on rattrape par tranches (defaut : 24)."
        ),
    )
    rattrapage.set_defaults(handler=cmd_backfill)

    modele = subparsers.add_parser(
        "model",
        help="Entraine, inspecte ou interroge le classifieur d'appareils.",
    )
    modele.add_argument(
        "action",
        choices=("train", "info", "score", "predict"),
        help=(
            "train : entraine et enregistre une version datee. "
            "info : decrit la version courante. "
            "score : classe tous les appareils et ecrit dans le lac. "
            "predict : classe des appareils designes par leur adresse OACI."
        ),
    )
    modele.add_argument(
        "icao24",
        nargs="*",
        help="Adresses OACI 24 bits a classer (pour `predict`).",
    )
    modele.set_defaults(handler=cmd_model)

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
