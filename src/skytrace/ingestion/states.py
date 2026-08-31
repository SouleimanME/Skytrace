"""Ingestion des positions d'aeronefs vers la couche bronze.

Chaque appel a l'API produit un fichier Parquet immuable, range dans une
arborescence partitionnee facon Hive :

    data/raw/opensky_states/ingest_date=2026-08-17/ingest_hour=14/states_1755441600.parquet

Pourquoi ce decoupage :
  * `ingest_date` / `ingest_hour` permettent a DuckDB d'elaguer les
    partitions (`WHERE ingest_date = ...` ne lit que les fichiers utiles) ;
  * un fichier par snapshot rend l'ingestion idempotente et rejouable :
    relancer un snapshot deja ecrit ne duplique rien, il ecrase le meme nom ;
  * Parquet + compression zstd divise le volume par ~10 face a du JSON brut.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pyarrow.parquet as pq

from skytrace.config import Settings, get_settings
from skytrace.logging_conf import get_logger
from skytrace.opensky.client import OpenSkyClient, StatesSnapshot
from skytrace.opensky.schema import states_to_arrow
from skytrace.storage import write_parquet

logger = get_logger(__name__)


@dataclass(frozen=True)
class IngestedSnapshot:
    """Resultat d'une ingestion, retourne a l'ordonnanceur."""

    uri: str
    rows: int
    snapshot_ts: int
    region: str
    credits_spent: int
    size_bytes: int = 0

    @property
    def path(self) -> Path:
        """Chemin local (mode disque). Sur R2, `uri` est un s3://..."""
        return Path(self.uri)

    @property
    def snapshot_at(self) -> datetime:
        return datetime.fromtimestamp(self.snapshot_ts, tz=UTC)


def partition_key(snapshot_ts: int) -> str:
    """Cle relative (dans le lac) du fichier Parquet d'un snapshot."""
    moment = datetime.fromtimestamp(snapshot_ts, tz=UTC)
    return (
        f"opensky_states/ingest_date={moment:%Y-%m-%d}"
        f"/ingest_hour={moment:%H}/states_{snapshot_ts}.parquet"
    )


def partition_path(root: Path, snapshot_ts: int) -> Path:
    """Chemin local du fichier Parquet pour un horodatage donne."""
    return root / partition_key(snapshot_ts).removeprefix("opensky_states/")


def write_snapshot(snapshot: StatesSnapshot, root: Path) -> IngestedSnapshot:
    """Serialise un snapshot en Parquet LOCAL (helper de test)."""
    table = states_to_arrow(
        snapshot.vectors,
        snapshot_ts=snapshot.snapshot_ts,
        region=snapshot.region,
    )
    destination = partition_path(root, snapshot.snapshot_ts)
    destination.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, destination, compression="zstd")
    return IngestedSnapshot(
        uri=str(destination),
        rows=table.num_rows,
        snapshot_ts=snapshot.snapshot_ts,
        region=snapshot.region,
        credits_spent=snapshot.credits_spent,
        size_bytes=destination.stat().st_size,
    )


def ingest_states(
    settings: Settings | None = None,
    *,
    client: OpenSkyClient | None = None,
) -> IngestedSnapshot:
    """Recupere un snapshot du trafic et l'ecrit dans la couche bronze."""
    settings = settings or get_settings()
    settings.ensure_directories()

    owns_client = client is None
    client = client or OpenSkyClient(settings)
    try:
        snapshot = client.get_states()
    finally:
        if owns_client:
            client.close()

    if not snapshot.vectors:
        logger.warning(
            "Aucun aeronef renvoye pour la zone %s : snapshot vide.",
            snapshot.region,
        )

    table = states_to_arrow(
        snapshot.vectors, snapshot_ts=snapshot.snapshot_ts, region=snapshot.region
    )
    result = write_parquet(partition_key(snapshot.snapshot_ts), table, settings)
    return IngestedSnapshot(
        uri=result.uri,
        rows=table.num_rows,
        snapshot_ts=snapshot.snapshot_ts,
        region=snapshot.region,
        credits_spent=snapshot.credits_spent,
        size_bytes=result.size_bytes,
    )


#: Profondeur d'historique reellement servie par OpenSky, MESUREE.
#:
#: Le rattrapage a d'abord ete ecrit en supposant qu'un compte authentifie
#: donnait acces a l'historique. C'etait faux, et la mesure l'a montre :
#:
#:     t-5 min   200   t-55 min  200
#:     t-65 min  403   t-2 h     403   t-24 h  403
#:     "Historical data more than 1 hour ago can only be retrieved with /states/own"
#:
#: Au-dela d'une heure, OpenSky ne sert que `/states/own`, reserve a ceux qui
#: alimentent le reseau avec leur propre recepteur. Un simple compte ne suffit
#: pas.
#:
#: CE QUE CELA CHANGE POUR CE PROJET. Les trous de plusieurs heures - dont les
#: 35 heures de la panne d'ordonnanceur - sont DEFINITIFS. Aucun reglage n'y
#: change rien. Le rattrapage ne peut servir qu'un cas : une collecte en
#: retard de moins d'une heure, rejouee a l'instant manque. C'est modeste, et
#: il vaut mieux le dire que laisser croire le contraire.
HISTORICAL_WINDOW_SECONDS = 3600


@dataclass(frozen=True)
class BackfillResult:
    """Bilan d'un rattrapage : ce qui a ete comble, ce qui ne l'a pas ete."""

    gaps_found: int
    snapshots_written: int
    hours_recovered: float
    skipped_reason: str | None = None


def find_gaps(
    settings: Settings | None = None,
    *,
    min_gap_hours: float = 3.0,
    horizon_hours: float = 168.0,
) -> list[tuple[int, int]]:
    """Trouve les intervalles sans releve dans le lac.

    Rend des couples (debut, fin) en secondes Unix. Un trou compte a partir de
    `min_gap_hours` : en deca, l'irregularite du cron GitHub suffit a
    l'expliquer et il n'y a rien a rattraper.

    `horizon_hours` borne la recherche. OpenSky ne sert pas d'historique
    illimite, et remonter indefiniment couterait des credits pour des heures
    que plus personne ne regardera.
    """
    settings = settings or get_settings()
    instants = sorted(_snapshot_timestamps(settings))
    if len(instants) < 2:
        return []

    plancher = time.time() - horizon_hours * 3600
    seuil = min_gap_hours * 3600

    trous = []
    for precedent, suivant in zip(instants, instants[1:], strict=False):
        if suivant < plancher:
            continue
        if suivant - precedent > seuil:
            trous.append((max(precedent, int(plancher)), suivant))
    return trous


def _snapshot_timestamps(settings: Settings) -> list[int]:
    """Instants des releves deja presents, lus depuis les noms de fichiers.

    On lit les NOMS et non le contenu : le lac peut peser des centaines de
    mega-octets, et l'instant est deja dans la cle - le relire depuis les
    Parquet serait payer cher une information gratuite.
    """
    import re

    motif = re.compile(r"states_(\d+)\.parquet$")
    instants = []

    if settings.uses_r2:
        from pyarrow.fs import FileSelector, FileType

        from skytrace.storage import _s3_filesystem

        fs = _s3_filesystem(settings)
        prefixe = f"{settings.r2_bucket}/raw/opensky_states"
        try:
            entrees = fs.get_file_info(FileSelector(prefixe, recursive=True))
        except OSError:
            return []
        noms = [i.path for i in entrees if i.type == FileType.File]
    else:
        noms = [str(p) for p in settings.states_dir.rglob("states_*.parquet")]

    for nom in noms:
        trouve = motif.search(nom.replace("\\", "/"))
        if trouve:
            instants.append(int(trouve.group(1)))
    return instants


def backfill_gaps(
    settings: Settings | None = None,
    *,
    step_hours: float = 1.0,
    max_snapshots: int = 24,
    min_gap_hours: float = 3.0,
) -> BackfillResult:
    """Comble les trous de la serie en rejouant l'historique OpenSky.

    CE QU'ELLE PEUT, ET CE QU'ELLE NE PEUT PAS. OpenSky ne sert d'historique
    que sur UNE HEURE (voir `HISTORICAL_WINDOW_SECONDS`, mesure). Cette
    fonction ne rattrape donc qu'une collecte recemment manquee, jamais un
    trou de plusieurs heures. Les 102 heures perdues sur ce depot restent
    perdues, et le resteront : c'est une limite de la source, pas un defaut
    de configuration.

    Elle exige tout de meme un compte : sans identifiants, meme la derniere
    heure est refusee. Sans compte comme au-dela de la fenetre, elle
    s'abstient en le disant plutot que d'echouer.

    `max_snapshots` borne le nombre d'APPELS, et non d'ecritures : un appel
    coute des credits qu'il rende des donnees ou non. Un trou de plusieurs
    jours en demanderait des centaines, donc on rattrape par tranches,
    execution apres execution, plutot que d'un coup.
    """
    settings = settings or get_settings()
    settings.ensure_directories()

    client = OpenSkyClient(settings)
    try:
        if client.anonymous:
            return BackfillResult(
                gaps_found=0,
                snapshots_written=0,
                hours_recovered=0.0,
                skipped_reason=(
                    "identifiants OpenSky absents : meme la derniere heure "
                    "d'historique est refusee aux requetes anonymes. Definir "
                    "OPENSKY_CLIENT_ID et OPENSKY_CLIENT_SECRET."
                ),
            )

        trous = find_gaps(settings, min_gap_hours=min_gap_hours)
        if not trous:
            return BackfillResult(0, 0, 0.0)

        # LA FENETRE SERVIE PAR OPENSKY EST D'UNE HEURE, pas davantage. Tout
        # instant plus ancien recevrait un 403 : l'interroger gaspillerait des
        # credits pour une reponse connue d'avance.
        plancher_servi = time.time() - HISTORICAL_WINDOW_SECONDS
        trous = [(max(d, int(plancher_servi)), f) for d, f in trous if f > plancher_servi]
        if not trous:
            return BackfillResult(
                gaps_found=0,
                snapshots_written=0,
                hours_recovered=0.0,
                skipped_reason=(
                    "les trous reperes sont plus vieux qu'une heure. OpenSky ne "
                    "sert au-dela que `/states/own`, reserve aux contributeurs "
                    "du reseau : ces positions sont definitivement perdues."
                ),
            )

        pas = int(step_hours * 3600)
        appels = 0
        ecrits = 0
        heures = 0.0

        # LE PLAFOND BORNE LES APPELS, PAS LES ECRITURES.
        #
        # Un appel coute des credits qu'il rende des donnees ou non. Compter
        # les ecritures laissait une periode sans archive consommer le budget
        # sans limite : chaque instant interroge, aucun n'ecrivant, la boucle
        # tournait jusqu'au bout du trou. Un test l'a montre avant que cela
        # n'arrive en production.
        for debut, fin in trous:
            instant = debut + pas
            while instant < fin and appels < max_snapshots:
                snapshot = client.get_states(at=instant)
                appels += 1
                if snapshot.vectors:
                    table = states_to_arrow(
                        snapshot.vectors,
                        snapshot_ts=snapshot.snapshot_ts,
                        region=snapshot.region,
                    )
                    write_parquet(partition_key(snapshot.snapshot_ts), table, settings)
                    ecrits += 1
                    heures += step_hours
                else:
                    logger.warning(
                        "Aucun aeronef a %s : OpenSky n'a pas d'archive pour cet instant.",
                        datetime.fromtimestamp(instant, tz=UTC),
                    )
                instant += pas
            if appels >= max_snapshots:
                break

        return BackfillResult(len(trous), ecrits, heures)
    finally:
        client.close()
