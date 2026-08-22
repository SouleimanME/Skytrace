"""Tests de l'ingestion qualite de l'air (Open-Meteo), reseau simule."""

from __future__ import annotations

import httpx
import pyarrow.parquet as pq
import pytest
import respx

from skytrace.ingestion.air_quality import (
    AIR_QUALITY_URL,
    Airport,
    ingest_air_quality,
)

TWO_AIRPORTS = (
    Airport("LFPG", "CDG", 49.0128, 2.55),
    Airport("LSZH", "ZRH", 47.4647, 8.5492),
)


def hourly_payload(n: int = 24) -> dict:
    times = [f"2026-08-19T{h:02d}:00" for h in range(n)]
    return {
        "hourly": {
            "time": times,
            "nitrogen_dioxide": [10.0 + h for h in range(n)],
            "pm2_5": [5.0 + h * 0.5 for h in range(n)],
            "pm10": [8.0 for _ in range(n)],
            "ozone": [40.0 for _ in range(n)],
        }
    }


class TestIngestAirQuality:
    @respx.mock
    def test_end_to_end_writes_one_row_per_airport_hour(self, settings):
        respx.get(AIR_QUALITY_URL).mock(return_value=httpx.Response(200, json=hourly_payload(24)))
        result = ingest_air_quality(settings, airports=TWO_AIRPORTS, lookback_days=1)

        # 2 aeroports x 24 heures.
        assert result.rows == 48
        assert result.airports == 2

        table = pq.read_table(result.path)
        assert set(table.column("airport_icao").to_pylist()) == {"LFPG", "LSZH"}

    @respx.mock
    def test_bounding_dates_are_sent(self, settings):
        route = respx.get(AIR_QUALITY_URL).mock(
            return_value=httpx.Response(200, json=hourly_payload(2))
        )
        ingest_air_quality(settings, airports=TWO_AIRPORTS[:1], lookback_days=7)

        params = route.calls.last.request.url.params
        assert "start_date" in params
        assert "end_date" in params
        assert params["timezone"] == "UTC"
        assert "nitrogen_dioxide" in params["hourly"]

    @respx.mock
    def test_timestamps_are_utc_aware(self, settings):
        respx.get(AIR_QUALITY_URL).mock(return_value=httpx.Response(200, json=hourly_payload(3)))
        result = ingest_air_quality(settings, airports=TWO_AIRPORTS[:1], lookback_days=1)
        row = pq.read_table(result.path).to_pylist()[0]
        assert row["measured_at"].tzinfo is not None
        assert row["source"] == "open-meteo/air-quality"

    @respx.mock
    def test_missing_values_become_null(self, settings):
        payload = hourly_payload(3)
        payload["hourly"]["nitrogen_dioxide"][1] = None
        respx.get(AIR_QUALITY_URL).mock(return_value=httpx.Response(200, json=payload))

        result = ingest_air_quality(settings, airports=TWO_AIRPORTS[:1], lookback_days=1)
        no2 = pq.read_table(result.path).column("nitrogen_dioxide").to_pylist()
        assert no2[1] is None

    @respx.mock
    def test_http_error_is_surfaced(self, settings):
        respx.get(AIR_QUALITY_URL).mock(return_value=httpx.Response(500))
        with pytest.raises(httpx.HTTPStatusError):
            ingest_air_quality(settings, airports=TWO_AIRPORTS[:1], lookback_days=1)
