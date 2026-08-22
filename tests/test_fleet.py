"""Tests de l'ingestion des referentiels flotte (aeronefs + compagnies)."""

from __future__ import annotations

import httpx
import pyarrow.parquet as pq
import pytest
import respx

from skytrace.ingestion.fleet import (
    AIRCRAFT_DB_URL,
    AIRLINES_URL,
    ingest_aircraft_db,
    ingest_airlines,
)

AIRCRAFT_CSV = (
    '"icao24","registration","typecode","manufacturername","model","operator",'
    '"operatoricao","owner","built","categoryDescription"\n'
    '"abc123","F-GABC","A320","Airbus","A320-214","Air France","AFR","Air France","2005","Large"\n'
    '"def456","","","","","","","","",""\n'  # aucun info : doit etre ecarte
    '"abc789","D-AIMA","A388","Airbus","A380-800","Lufthansa","DLH","Lufthansa","2010","Large"\n'
)

AIRLINES_DAT = (
    '137,"Air France",\\N,"AF","AFR","AIRFRANS","France","Y"\n'
    '-1,"Unknown",\\N,"-","N/A",\\N,\\N,"Y"\n'
    '3320,"Lufthansa",\\N,"LH","DLH","LUFTHANSA","Germany","Y"\n'
)


class TestIngestAircraftDb:
    @respx.mock
    def test_keeps_rows_with_info_and_drops_empty(self, settings):
        respx.get(AIRCRAFT_DB_URL).mock(
            return_value=httpx.Response(200, content=AIRCRAFT_CSV.encode("utf-8"))
        )
        result = ingest_aircraft_db(settings)

        # def456 (sans metadonnee) est ecarte ; abc123 et abc789 restent.
        assert result.rows == 2
        table = pq.read_table(result.path)
        makers = set(table.column("manufacturername").to_pylist())
        assert makers == {"Airbus"}
        assert "typecode" in table.column_names

    @respx.mock
    def test_only_useful_columns_are_kept(self, settings):
        respx.get(AIRCRAFT_DB_URL).mock(
            return_value=httpx.Response(200, content=AIRCRAFT_CSV.encode("utf-8"))
        )
        result = ingest_aircraft_db(settings)
        cols = pq.read_schema(result.path).names
        assert "serialnumber" not in cols  # colonne source non conservee
        assert "operator" in cols

    @respx.mock
    def test_http_error_is_surfaced(self, settings):
        respx.get(AIRCRAFT_DB_URL).mock(return_value=httpx.Response(503))
        with pytest.raises(httpx.HTTPStatusError):
            ingest_aircraft_db(settings)


class TestIngestAirlines:
    @respx.mock
    def test_downloads_and_parses_dat(self, settings):
        respx.get(AIRLINES_URL).mock(
            return_value=httpx.Response(200, content=AIRLINES_DAT.encode("utf-8"))
        )
        result = ingest_airlines(settings)

        assert result.rows == 3
        table = pq.read_table(result.path)
        icaos = table.column("icao").to_pylist()
        assert "AFR" in icaos and "DLH" in icaos

    @respx.mock
    def test_backslash_n_becomes_null(self, settings):
        respx.get(AIRLINES_URL).mock(
            return_value=httpx.Response(200, content=AIRLINES_DAT.encode("utf-8"))
        )
        result = ingest_airlines(settings)
        table = pq.read_table(result.path)
        # La ligne "Unknown" a un alias "\N" -> doit devenir nul.
        aliases = table.column("alias").to_pylist()
        assert all(a is None for a in aliases)
