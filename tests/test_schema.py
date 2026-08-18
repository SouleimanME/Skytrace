"""Tests de la conversion des vecteurs d'etat OpenSky vers Arrow."""

from __future__ import annotations

from datetime import UTC, datetime

import pyarrow as pa

from skytrace.opensky.schema import STATES_SCHEMA, state_vector_to_row, states_to_arrow


class TestStateVectorToRow:
    def test_positional_fields_are_named_correctly(self, state_vector):
        row = state_vector_to_row(state_vector)
        assert row["icao24"] == "3944ef"
        assert row["origin_country"] == "France"
        assert row["latitude"] == 48.8566
        assert row["longitude"] == 2.3522
        assert row["baro_altitude"] == 10668.0
        assert row["on_ground"] is False
        assert row["category"] == 3

    def test_short_vectors_are_padded(self, state_vector):
        # Sans `extended=1`, OpenSky renvoie 17 champs au lieu de 18.
        # Le parseur doit completer plutot que lever IndexError.
        row = state_vector_to_row(state_vector[:17])
        assert row["category"] is None
        assert row["position_source"] == 0

    def test_malformed_numbers_become_null(self):
        vector = ["abc123", "TEST", "France", "pas-un-entier", None, "x", None]
        row = state_vector_to_row(vector)
        assert row["time_position"] is None
        assert row["longitude"] is None
        assert row["latitude"] is None

    def test_sensors_list_is_preserved(self, state_vector):
        vector = list(state_vector)
        vector[12] = [101, 202]
        assert state_vector_to_row(vector)["sensors"] == [101, 202]


class TestStatesToArrow:
    def test_schema_is_stable(self, state_vector):
        table = states_to_arrow([state_vector], snapshot_ts=1755441600, region="france")
        assert table.schema.equals(STATES_SCHEMA)
        assert table.num_rows == 1

    def test_empty_snapshot_keeps_the_schema(self):
        # Une zone sans trafic ne doit pas produire un fichier au schema
        # different : sinon la lecture globale du lac casse au premier
        # creux de trafic nocturne.
        table = states_to_arrow([], snapshot_ts=1755441600, region="france")
        assert table.num_rows == 0
        assert table.schema.equals(STATES_SCHEMA)

    def test_metadata_columns_are_attached(self, state_vector):
        moment = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)
        table = states_to_arrow(
            [state_vector],
            snapshot_ts=1755441600,
            region="europe",
            ingested_at=moment,
        )
        record = table.to_pylist()[0]
        assert record["snapshot_ts"] == 1755441600
        assert record["region"] == "europe"
        assert record["source"] == "opensky/states/all"
        assert record["ingested_at"] == moment

    def test_rows_without_aircraft_id_are_dropped(self, state_vector):
        orphan = list(state_vector)
        orphan[0] = None
        table = states_to_arrow([state_vector, orphan], snapshot_ts=1755441600, region="france")
        assert table.num_rows == 1

    def test_columns_that_are_entirely_null_keep_their_type(self, state_vector):
        # Piege classique : laisser Arrow inferer les types produit une
        # colonne `null` quand toutes les valeurs sont vides, et le schema
        # devient incompatible avec les autres fichiers du lac.
        vector = list(state_vector)
        vector[14] = None  # squawk
        table = states_to_arrow([vector], snapshot_ts=1755441600, region="france")
        assert table.schema.field("squawk").type == pa.string()
