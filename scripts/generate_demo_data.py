"""Genere un lac de donnees synthetique, sans appel reseau.

Deux usages :

  * **CI** - permettre a l'integration continue d'executer la chaine dbt
    complete et ses 54 tests, sans identifiants ni quota consomme. Une CI
    qui ne testerait que le Python passerait a cote de l'essentiel : c'est
    le SQL qui porte la logique metier.
  * **Decouverte** - permettre a quelqu'un qui clone le depot de voir le
    tableau de bord rempli immediatement, sans attendre plusieurs heures
    de collecte reelle.

Les donnees sont deterministes (graine fixe) et physiquement plausibles :
elles satisfont les memes controles qualite que les donnees reelles, ce qui
serait faux avec du bruit aleatoire non contraint.

    python scripts/generate_demo_data.py --hours 12

Attention : le script ecrit dans le lac configure. Pour ne pas melanger
donnees synthetiques et donnees reelles, pointer `SKYTRACE_DATA_DIR` vers
un repertoire dedie avant de le lancer.
"""

from __future__ import annotations

import argparse
import random
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from skytrace.config import get_settings  # noqa: E402
from skytrace.ingestion.states import partition_path  # noqa: E402
from skytrace.opensky.schema import states_to_arrow  # noqa: E402

SEED = 20260817

# Aeroports reels, pour que la jointure spatiale produise des resultats
# reconnaissables plutot que des coordonnees arbitraires.
AIRPORTS: list[tuple[str, str, str, float, float, int, str, str, str]] = [
    (
        "LFPG",
        "Charles de Gaulle International Airport",
        "CDG",
        49.0128,
        2.5500,
        392,
        "FR",
        "FR-IDF",
        "Paris",
    ),
    ("LFPO", "Paris-Orly Airport", "ORY", 48.7233, 2.3794, 291, "FR", "FR-IDF", "Paris"),
    ("LFLL", "Lyon Saint-Exupery Airport", "LYS", 45.7256, 5.0811, 821, "FR", "FR-ARA", "Lyon"),
    ("LFMN", "Nice-Cote d'Azur Airport", "NCE", 43.6584, 7.2159, 12, "FR", "FR-PAC", "Nice"),
    ("LFBO", "Toulouse-Blagnac Airport", "TLS", 43.6291, 1.3638, 499, "FR", "FR-OCC", "Toulouse"),
    ("LFML", "Marseille Provence Airport", "MRS", 43.4393, 5.2214, 74, "FR", "FR-PAC", "Marseille"),
    ("LFSB", "EuroAirport Basel-Mulhouse", "BSL", 47.5900, 7.5299, 885, "FR", "FR-GES", "Bale"),
    ("LFRS", "Nantes Atlantique Airport", "NTE", 47.1532, -1.6107, 90, "FR", "FR-PDL", "Nantes"),
    ("LFBD", "Bordeaux-Merignac Airport", "BOD", 44.8283, -0.7156, 162, "FR", "FR-NAQ", "Bordeaux"),
    ("LFST", "Strasbourg Airport", "SXB", 48.5383, 7.6282, 505, "FR", "FR-GES", "Strasbourg"),
]

#: Colonnes du CSV OurAirports, dans l'ordre exact du fichier amont.
AIRPORT_COLUMNS = [
    "id",
    "ident",
    "type",
    "name",
    "latitude_deg",
    "longitude_deg",
    "elevation_ft",
    "continent",
    "iso_country",
    "iso_region",
    "municipality",
    "scheduled_service",
    "icao_code",
    "iata_code",
    "gps_code",
    "local_code",
    "home_link",
    "wikipedia_link",
    "keywords",
]


def write_airports(destination: Path) -> int:
    """Ecrit un referentiel aeroports synthetique.

    Toutes les colonnes sont en texte, comme le fait l'ingestion reelle :
    la couche bronze reste fidele au CSV, le typage appartient a dbt.
    """
    rows = []
    for index, (ident, name, iata, lat, lon, elevation, country, region, city) in enumerate(
        AIRPORTS, start=1
    ):
        rows.append(
            {
                "id": str(1000 + index),
                "ident": ident,
                "type": "large_airport",
                "name": name,
                "latitude_deg": f"{lat}",
                "longitude_deg": f"{lon}",
                "elevation_ft": str(elevation),
                "continent": "EU",
                "iso_country": country,
                "iso_region": region,
                "municipality": city,
                "scheduled_service": "yes",
                "icao_code": ident,
                "iata_code": iata,
                "gps_code": ident,
                "local_code": "",
                "home_link": "",
                "wikipedia_link": "",
                "keywords": "",
            }
        )

    schema = pa.schema([pa.field(name, pa.string()) for name in AIRPORT_COLUMNS])
    destination.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows, schema=schema), destination, compression="zstd")
    return len(rows)


def build_state_vector(rng: random.Random, index: int, snapshot_ts: int) -> list:
    """Fabrique un vecteur d'etat plausible.

    Un aeronef sur cinq est place a proximite immediate d'un aeroport en
    phase basse, de facon que `fct_airport_activity` soit alimentee. Les
    autres croisent en altitude quelque part sur la zone.
    """
    icao24 = f"{index:06x}"
    callsign = f"{rng.choice(['AFR', 'BAW', 'DLH', 'EZY', 'RYR', 'KLM'])}{rng.randint(10, 9999)}"
    country = rng.choice(["France", "United Kingdom", "Germany", "Spain", "Ireland", "Netherlands"])

    if index % 5 == 0:
        # Phase basse autour d'un aeroport : moins de 8 km du point de
        # reference, sous le plafond d'approche.
        # `index // 5` et non `index % len(AIRPORTS)` : comme seuls les
        # indices multiples de 5 arrivent ici, un modulo sur 10 ne
        # selectionnerait que deux aeroports sur les dix.
        _, _, _, lat, lon, *_ = AIRPORTS[(index // 5) % len(AIRPORTS)]
        latitude = lat + rng.uniform(-0.03, 0.03)
        longitude = lon + rng.uniform(-0.03, 0.03)
        on_ground = rng.random() < 0.45
        altitude = 0.0 if on_ground else rng.uniform(150.0, 1100.0)
        velocity = rng.uniform(2.0, 15.0) if on_ground else rng.uniform(70.0, 130.0)
        vertical_rate = 0.0 if on_ground else rng.choice([-4.5, -2.0, 2.0, 6.0])
    else:
        latitude = rng.uniform(42.5, 50.5)
        longitude = rng.uniform(-4.0, 8.0)
        on_ground = False
        altitude = rng.uniform(8000.0, 12000.0)
        velocity = rng.uniform(180.0, 260.0)
        vertical_rate = rng.uniform(-1.0, 1.0)

    return [
        icao24,
        f"{callsign:<8}",
        country,
        snapshot_ts - rng.randint(0, 20),  # position toujours anterieure au releve
        snapshot_ts,
        round(longitude, 5),
        round(latitude, 5),
        round(altitude, 1),
        on_ground,
        round(velocity, 1),
        round(rng.uniform(0.0, 359.9), 1),
        round(vertical_rate, 2),
        None,
        round(altitude + rng.uniform(-40, 40), 1) if altitude else 0.0,
        f"{rng.randint(1000, 7777)}",
        False,
        0,
        rng.choice([1, 2, 3, 4]),
    ]


def write_air_quality(settings, now, hours: int, rng: random.Random) -> int:
    """Ecrit une qualite de l'air synthetique pour les aeroports de demo.

    Valeurs horaires plausibles avec un cycle jour/nuit (le NO2 monte en
    journee), volontairement peu correlees au trafic : l'analyse retrouve
    ainsi, sur donnees synthetiques comme sur donnees reelles, l'absence de
    lien horaire net.
    """
    import math

    from skytrace.ingestion.air_quality import AIR_QUALITY_SCHEMA

    ingested_at = datetime.now(UTC)
    rows = []
    for ident, _name, iata, lat, lon, *_ in AIRPORTS:
        for step in range(hours + 1):
            moment = now - timedelta(hours=step)
            # Cycle diurne : creux la nuit, pic en milieu de journee.
            diurnal = math.sin((moment.hour / 24.0) * 2 * math.pi - math.pi / 2)
            no2 = max(1.0, 12 + 8 * diurnal + rng.uniform(-3, 3))
            pm25 = max(1.0, 9 + 4 * diurnal + rng.uniform(-2, 2))
            rows.append(
                {
                    "airport_icao": ident,
                    "airport_iata": iata,
                    "latitude": lat,
                    "longitude": lon,
                    "measured_at": moment,
                    "nitrogen_dioxide": round(no2, 1),
                    "pm2_5": round(pm25, 1),
                    "pm10": round(pm25 * 1.4, 1),
                    "ozone": round(45 + rng.uniform(-10, 10), 1),
                    "ingested_at": ingested_at,
                    "source": "demo/synthetic",
                }
            )

    table = pa.Table.from_pylist(rows, schema=AIR_QUALITY_SCHEMA)
    destination = settings.air_quality_dir / "air_quality.parquet"
    destination.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, destination, compression="zstd")
    return len(rows)


# Compagnies de demo (prefixe d'indicatif -> nom), alignees sur les callsigns
# generes par build_state_vector.
DEMO_AIRLINES = [
    ("AFR", "Air France", "AF", "France"),
    ("BAW", "British Airways", "BA", "United Kingdom"),
    ("DLH", "Lufthansa", "LH", "Germany"),
    ("EZY", "easyJet", "U2", "United Kingdom"),
    ("RYR", "Ryanair", "FR", "Ireland"),
    ("KLM", "KLM Royal Dutch Airlines", "KL", "Netherlands"),
]

DEMO_MANUFACTURERS = [
    ("Airbus", "A320", "A320-214"),
    ("Airbus", "A20N", "A320neo"),
    ("Boeing", "B738", "737-800"),
    ("Boeing", "B77W", "777-300ER"),
    ("Embraer", "E190", "ERJ 190-100"),
    ("ATR", "AT76", "ATR 72-600"),
]


def write_aircraft_db(settings, rng: random.Random, n: int = 300) -> int:
    """Base aeronefs synthetique couvrant les icao24 de demo."""
    from skytrace.ingestion.fleet import AIRCRAFT_DB_COLUMNS

    rows = []
    for i in range(n):
        maker, typecode, model = rng.choice(DEMO_MANUFACTURERS)
        operator = rng.choice(DEMO_AIRLINES)[1]
        rows.append(
            {
                "icao24": f"{i:06x}",
                "registration": f"F-{rng.choice('GH')}{rng.randint(100, 999)}",
                "typecode": typecode,
                "manufacturername": maker,
                "model": model,
                "operator": operator,
                "operatoricao": "",
                "owner": operator,
                "built": str(rng.randint(1998, 2023)),
                "categoryDescription": "",
            }
        )
    schema = pa.schema([pa.field(c, pa.string()) for c in AIRCRAFT_DB_COLUMNS])
    destination = settings.aircraft_db_dir / "aircraft_database.parquet"
    destination.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows, schema=schema), destination, compression="zstd")
    return len(rows)


def write_airlines(settings) -> int:
    """Referentiel compagnies synthetique (prefixes d'indicatif de demo)."""
    from skytrace.ingestion.fleet import AIRLINES_COLUMNS

    rows = [
        {
            "airline_id": str(index),
            "name": name,
            "alias": "",
            "iata": iata,
            "icao": icao,
            "callsign": name.upper(),
            "country": country,
            "active": "Y",
        }
        for index, (icao, name, iata, country) in enumerate(DEMO_AIRLINES, start=1)
    ]
    schema = pa.schema([pa.field(c, pa.string()) for c in AIRLINES_COLUMNS])
    destination = settings.airlines_dir / "airlines.parquet"
    destination.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows, schema=schema), destination, compression="zstd")
    return len(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hours", type=int, default=6, help="Profondeur d'historique simulee.")
    parser.add_argument(
        "--per-hour", type=int, default=4, help="Snapshots par heure (4 = toutes les 15 min)."
    )
    parser.add_argument("--aircraft", type=int, default=220, help="Aeronefs par snapshot.")
    args = parser.parse_args()

    settings = get_settings()
    settings.ensure_directories()
    rng = random.Random(SEED)

    airports = write_airports(settings.airports_dir / "airports.parquet")
    print(f"Referentiel : {airports} aeroports")

    # On remonte dans le passe depuis l'heure pleine courante : aucun
    # horodatage futur, sinon le test assert_no_future_snapshots echoue.
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    interval = timedelta(minutes=60 // args.per_hour)

    total_rows = 0
    total_files = 0
    for step in range(args.hours * args.per_hour):
        moment = now - interval * step
        snapshot_ts = int(moment.timestamp())

        # Le nombre d'aeronefs varie legerement d'un releve a l'autre :
        # une courbe parfaitement plate ne testerait aucune agregation.
        count = max(1, args.aircraft + rng.randint(-25, 25))
        vectors = [build_state_vector(rng, index, snapshot_ts) for index in range(count)]

        table = states_to_arrow(vectors, snapshot_ts=snapshot_ts, region=settings.region)
        destination = partition_path(settings.states_dir, snapshot_ts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(table, destination, compression="zstd")

        total_rows += table.num_rows
        total_files += 1

    aq_rows = write_air_quality(settings, now, args.hours, rng)
    ac_rows = write_aircraft_db(settings, rng)
    al_rows = write_airlines(settings)
    print(f"Trafic     : {total_files} snapshots, {total_rows} positions")
    print(f"Qualite air: {aq_rows} lignes horaires")
    print(f"Flotte     : {ac_rows} aeronefs, {al_rows} compagnies")
    print(f"Lac        : {settings.resolved_data_dir}")
    print("\nEtape suivante : skytrace dbt build")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
