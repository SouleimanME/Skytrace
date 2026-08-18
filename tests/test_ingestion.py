"""Tests de la couche d'ingestion : partitionnement, idempotence, formats."""

from __future__ import annotations

import httpx
import pyarrow.parquet as pq
import pytest
import respx

from skytrace.ingestion.reference import AIRPORTS_URL, ingest_airports
from skytrace.ingestion.states import ingest_states, partition_path, write_snapshot
from skytrace.opensky.client import BASE_URL, OpenSkyClient, StatesSnapshot

STATES_URL = f"{BASE_URL}/states/all"

AIRPORTS_CSV = (
    '"id","ident","type","name","latitude_deg","longitude_deg","elevation_ft",'
    '"continent","iso_country","iso_region","municipality","scheduled_service",'
    '"icao_code","iata_code","gps_code","local_code","home_link","wikipedia_link","keywords"\n'
    '"1382","LFPG","large_airport","Charles de Gaulle International Airport",'
    '"49.012798","2.55","392","EU","FR","FR-IDF","Paris","yes",'
    '"LFPG","CDG","LFPG","","","",""\n'
    '"1381","LFPO","large_airport","Paris-Orly Airport",'
    '"48.7233333","2.37944","291","EU","FR","FR-IDF","Paris","yes",'
    '"LFPO","ORY","LFPO","","","",""\n'
)


class TestPartitionPath:
    def test_layout_is_hive_compatible(self, tmp_path):
        # 2026-08-17 14:00:00 UTC
        path = partition_path(tmp_path, 1786968000)
        assert path.parent.parent.name.startswith("ingest_date=")
        assert path.parent.name.startswith("ingest_hour=")
        assert path.name == "states_1786968000.parquet"

    def test_same_timestamp_always_maps_to_the_same_file(self, tmp_path):
        # C'est ce qui rend l'ingestion rejouable : relancer un snapshot
        # deja collecte ecrase le fichier au lieu d'en creer un second.
        assert partition_path(tmp_path, 1786968000) == partition_path(tmp_path, 1786968000)


class TestWriteSnapshot:
    def test_writes_a_readable_parquet_file(self, tmp_path, state_vector):
        snapshot = StatesSnapshot(
            snapshot_ts=1786968000, region="france", vectors=[state_vector], credits_spent=3
        )
        result = write_snapshot(snapshot, tmp_path)

        assert result.rows == 1
        assert result.path.exists()

        table = pq.read_table(result.path)
        assert table.num_rows == 1
        assert table.to_pylist()[0]["icao24"] == "3944ef"

    def test_uses_zstd_compression(self, tmp_path, state_vector):
        snapshot = StatesSnapshot(
            snapshot_ts=1786968000, region="france", vectors=[state_vector], credits_spent=3
        )
        result = write_snapshot(snapshot, tmp_path)
        metadata = pq.ParquetFile(result.path).metadata
        assert metadata.row_group(0).column(0).compression == "ZSTD"

    def test_an_empty_snapshot_still_produces_a_file(self, tmp_path):
        snapshot = StatesSnapshot(
            snapshot_ts=1786968000, region="france", vectors=[], credits_spent=3
        )
        result = write_snapshot(snapshot, tmp_path)
        assert result.rows == 0
        assert result.path.exists()


class TestIngestStates:
    @respx.mock
    def test_end_to_end_writes_into_the_lake(self, settings):
        respx.get(STATES_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "time": 1786968000,
                    "states": [
                        [
                            "3944ef",
                            "AFR23   ",
                            "France",
                            1786968000,
                            1786968000,
                            2.35,
                            48.85,
                            10000.0,
                            False,
                            230.0,
                            180.0,
                            0.0,
                            None,
                            10100.0,
                            "1000",
                            False,
                            0,
                            3,
                        ]
                    ],
                },
            )
        )
        result = ingest_states(settings)

        assert result.rows == 1
        assert result.path.is_relative_to(settings.states_dir)
        assert result.size_bytes > 0

    @respx.mock
    def test_replaying_a_snapshot_does_not_duplicate_files(self, settings):
        respx.get(STATES_URL).mock(
            return_value=httpx.Response(
                200, json={"time": 1786968000, "states": [["3944ef"] + [None] * 17]}
            )
        )
        ingest_states(settings)
        # Budget remis a plat pour isoler l'effet teste.
        ingest_states(settings.model_copy(update={"daily_credit_budget": 1000}))

        assert len(list(settings.states_dir.rglob("*.parquet"))) == 1


class TestIngestAirports:
    @respx.mock
    def test_downloads_and_converts_the_reference(self, settings):
        respx.get(AIRPORTS_URL).mock(
            return_value=httpx.Response(200, content=AIRPORTS_CSV.encode("utf-8"))
        )
        result = ingest_airports(settings)

        assert result.rows == 2
        assert "iata_code" in result.columns
        assert len(result.checksum) == 64

        table = pq.read_table(result.path)
        assert set(table.column("iata_code").to_pylist()) == {"CDG", "ORY"}

    @respx.mock
    def test_every_column_is_read_as_text(self, settings):
        # La couche bronze reste fidele au CSV : le typage appartient a dbt.
        # Laisser Arrow inferer produirait des schemas instables d'un
        # telechargement a l'autre.
        respx.get(AIRPORTS_URL).mock(
            return_value=httpx.Response(200, content=AIRPORTS_CSV.encode("utf-8"))
        )
        result = ingest_airports(settings)
        schema = pq.read_schema(result.path)
        assert all(str(field.type) == "string" for field in schema)

    @respx.mock
    def test_metadata_sidecar_is_written(self, settings):
        respx.get(AIRPORTS_URL).mock(
            return_value=httpx.Response(200, content=AIRPORTS_CSV.encode("utf-8"))
        )
        ingest_airports(settings)
        assert (settings.airports_dir / "_ingestion_metadata.json").exists()

    @respx.mock
    def test_an_http_failure_is_surfaced(self, settings):
        respx.get(AIRPORTS_URL).mock(return_value=httpx.Response(500))
        with pytest.raises(httpx.HTTPStatusError):
            ingest_airports(settings)


class TestClientLifecycle:
    @respx.mock
    def test_context_manager_closes_transports(self, settings):
        respx.get(STATES_URL).mock(
            return_value=httpx.Response(200, json={"time": 1786968000, "states": []})
        )
        with OpenSkyClient(settings) as client:
            client.get_states()
        # Une seconde fermeture ne doit pas lever : la CLI et Dagster
        # peuvent tous deux fermer le client sur le meme chemin d'erreur.
        client.close()
