"""Fixtures partagees.

Toutes les fixtures pointent vers des repertoires temporaires : la suite de
tests n'ecrit jamais dans le lac de donnees reel et ne consomme jamais de
credits OpenSky.
"""

from __future__ import annotations

import pytest

from skytrace.config import Settings


@pytest.fixture
def settings(tmp_path) -> Settings:
    """Configuration isolee, sans identifiants (mode anonyme)."""
    return Settings(
        OPENSKY_CLIENT_ID=None,
        OPENSKY_CLIENT_SECRET=None,
        SKYTRACE_REGION="france",
        SKYTRACE_DATA_DIR=tmp_path / "data",
        SKYTRACE_DUCKDB_PATH=tmp_path / "data" / "warehouse" / "test.duckdb",
        SKYTRACE_DAILY_CREDIT_BUDGET=400,
        SKYTRACE_MAX_RETRIES=3,
    )


@pytest.fixture
def authenticated_settings(tmp_path) -> Settings:
    """Configuration avec identifiants OAuth2 factices."""
    return Settings(
        OPENSKY_CLIENT_ID="client-de-test",
        OPENSKY_CLIENT_SECRET="secret-de-test",
        SKYTRACE_REGION="france",
        SKYTRACE_DATA_DIR=tmp_path / "data",
        SKYTRACE_DUCKDB_PATH=tmp_path / "data" / "warehouse" / "test.duckdb",
    )


@pytest.fixture
def state_vector() -> list:
    """Un vecteur d'etat OpenSky realiste (vol Air France au-dessus de Paris)."""
    return [
        "3944ef",  # icao24
        "AFR23   ",  # callsign, complete par des espaces cote source
        "France",  # origin_country
        1755441600,  # time_position
        1755441605,  # last_contact
        2.3522,  # longitude
        48.8566,  # latitude
        10668.0,  # baro_altitude
        False,  # on_ground
        231.5,  # velocity
        275.3,  # true_track
        0.0,  # vertical_rate
        None,  # sensors
        10700.0,  # geo_altitude
        "1000",  # squawk
        False,  # spi
        0,  # position_source
        3,  # category
    ]
