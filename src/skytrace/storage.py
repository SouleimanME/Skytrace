"""Ecriture dans le lac de donnees : disque local ou Cloudflare R2.

Un seul point de passage pour ecrire un fichier Parquet dans la couche
bronze. Selon la configuration, la destination est le disque local ou un
bucket R2 (stockage objet compatible S3). Le reste du code d'ingestion
ignore ce choix : il fournit une cle relative (ex.
"opensky_states/ingest_date=.../states_123.parquet") et une table Arrow.

L'entrepot DuckDB, lui, reste toujours local : seul le lac brut peut vivre
sur R2. C'est ce qui permet de passer a l'echelle (monde, haute frequence)
sans faire exploser le depot git.
"""

from __future__ import annotations

from dataclasses import dataclass

import pyarrow as pa
import pyarrow.parquet as pq

from skytrace.config import Settings, get_settings
from skytrace.logging_conf import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class WriteResult:
    uri: str
    size_bytes: int


def _s3_filesystem(settings: Settings):
    from pyarrow.fs import S3FileSystem

    return S3FileSystem(
        access_key=settings.r2_access_key_id,
        secret_key=settings.r2_secret_access_key,
        endpoint_override=settings.r2_endpoint,
        scheme="https",
        region="auto",
    )


def write_parquet(
    key: str,
    table: pa.Table,
    settings: Settings | None = None,
    *,
    compression: str = "zstd",
) -> WriteResult:
    """Ecrit une table Arrow dans le lac sous la cle donnee.

    `key` est relatif a la racine du lac (couche bronze), par exemple
    "ourairports/airports.parquet". Renvoie l'URI ecrite et la taille.
    """
    settings = settings or get_settings()

    if settings.uses_r2:
        fs = _s3_filesystem(settings)
        # pyarrow attend "bucket/cle" (sans schema s3://).
        path = f"{settings.r2_bucket}/raw/{key}"
        pq.write_table(table, path, filesystem=fs, compression=compression)
        info = fs.get_file_info(path)
        uri = f"s3://{settings.r2_bucket}/raw/{key}"
        logger.info("Ecrit %d lignes -> %s (%.1f Ko)", table.num_rows, uri, info.size / 1024)
        return WriteResult(uri=uri, size_bytes=info.size)

    local = settings.raw_dir / key
    local.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, str(local), compression=compression)
    size = local.stat().st_size
    logger.info("Ecrit %d lignes -> %s (%.1f Ko)", table.num_rows, local, size / 1024)
    return WriteResult(uri=str(local), size_bytes=size)


def object_age_seconds(key: str, settings: Settings | None = None) -> float | None:
    """Age (en secondes) d'un objet du lac, ou None s'il n'existe pas.

    Fonctionne en local comme sur R2 : indispensable pour les verifications
    de fraicheur des dimensions a evolution lente, sinon un runner ephemere
    (sans fichiers locaux) re-telechargerait tout a chaque execution.
    """
    import time

    settings = settings or get_settings()
    if settings.uses_r2:
        from pyarrow.fs import FileType

        fs = _s3_filesystem(settings)
        info = fs.get_file_info(f"{settings.r2_bucket}/raw/{key}")
        if info.type == FileType.NotFound or info.mtime is None:
            return None
        return time.time() - info.mtime.timestamp()

    local = settings.raw_dir / key
    if not local.exists():
        return None
    return time.time() - local.stat().st_mtime


def newest_snapshot_age_seconds(settings: Settings | None = None) -> float | None:
    """Age du releve de trafic le plus recent du lac, ou None s'il n'y en a pas.

    `object_age_seconds` interroge une cle precise ; ici on cherche la plus
    recente d'un prefixe entier, ce qui est la question que pose la veille :
    "quand la collecte a-t-elle ecrit pour la derniere fois ?".

    On regarde le LAC et non l'entrepot : c'est le lac qui recoit la
    collecte. Un entrepot frais reconstruit a partir d'un lac fige donnerait
    l'illusion que tout va bien.
    """
    import time

    settings = settings or get_settings()
    if settings.uses_r2:
        from pyarrow.fs import FileSelector, FileType

        fs = _s3_filesystem(settings)
        prefixe = f"{settings.r2_bucket}/raw/opensky_states"
        try:
            fichiers = [
                info
                for info in fs.get_file_info(FileSelector(prefixe, recursive=True))
                if info.type == FileType.File and info.mtime is not None
            ]
        except OSError:
            # Prefixe absent : aucune collecte n'a encore eu lieu. Ce n'est
            # pas une erreur de connexion, c'est une absence de donnee.
            return None
        if not fichiers:
            return None
        return time.time() - max(info.mtime.timestamp() for info in fichiers)

    fichiers = list(settings.states_dir.rglob("*.parquet"))
    if not fichiers:
        return None
    return time.time() - max(chemin.stat().st_mtime for chemin in fichiers)


def _date_from_key(path: str):
    """Extrait la date de partition (ingest_date=YYYY-MM-DD) d'un chemin."""
    import re
    from datetime import date

    match = re.search(r"ingest_date=(\d{4})-(\d{2})-(\d{2})", path)
    if not match:
        return None
    return date(int(match[1]), int(match[2]), int(match[3]))


def prune_old_states(keep_days: int, settings: Settings | None = None) -> int:
    """Supprime les snapshots de trafic plus vieux que `keep_days` jours.

    Borne le stockage pour rester sous le palier gratuit R2. Ne touche qu'aux
    positions (le gros du volume) ; les referentiels sont petits et rafraichis
    par ecrasement. Renvoie le nombre de fichiers supprimes.
    """
    from datetime import UTC, datetime, timedelta

    settings = settings or get_settings()
    if keep_days <= 0:
        return 0
    cutoff = (datetime.now(UTC) - timedelta(days=keep_days)).date()
    deleted = 0

    if settings.uses_r2:
        from pyarrow.fs import FileSelector

        fs = _s3_filesystem(settings)
        base = f"{settings.r2_bucket}/raw/opensky_states"
        try:
            infos = fs.get_file_info(FileSelector(base, recursive=True))
        except OSError:
            return 0
        for info in infos:
            date_part = _date_from_key(info.path)
            if info.path.endswith(".parquet") and date_part and date_part < cutoff:
                fs.delete_file(info.path)
                deleted += 1
    else:
        base = settings.raw_dir / "opensky_states"
        for parquet in base.rglob("*.parquet"):
            date_part = _date_from_key(str(parquet))
            if date_part and date_part < cutoff:
                parquet.unlink()
                deleted += 1

    if deleted:
        logger.info("Retention : %d snapshots de plus de %d j supprimes", deleted, keep_days)
    return deleted


def check_connectivity(settings: Settings | None = None) -> str:
    """Valide l'acces au lac : ecrit puis relit un petit objet temoin.

    Sert a verifier des identifiants R2 fraichement crees.
    """
    settings = settings or get_settings()
    probe = pa.table({"ok": [1]})
    result = write_parquet("_healthcheck/probe.parquet", probe, settings)

    if settings.uses_r2:
        fs = _s3_filesystem(settings)
        back = pq.read_table(f"{settings.r2_bucket}/raw/_healthcheck/probe.parquet", filesystem=fs)
    else:
        back = pq.read_table(result.uri)

    assert back.num_rows == 1  # noqa: S101 - controle interne de sante
    return result.uri
