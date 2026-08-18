{{
    config(
        materialized = "table"
    )
}}

/*
    Activite aeroportuaire. Grain : un aeroport, une heure.

    Limite assumee : ADS-B ne publie pas de plan de vol. On ne sait donc
    pas si un appareil atterrit ou decolle, on l'infere de son taux de
    montee au moment ou il est detecte a proximite. C'est un indicateur
    d'activite fiable en tendance, pas un comptage officiel de mouvements.
    Cette approximation est documentee ici plutot que dissimulee dans un
    nom de colonne trompeur.
*/

with proximity as (

    select * from {{ ref('int_positions_near_airports') }}

),

hourly as (

    select
        airport_id,
        airport_icao_code,
        airport_iata_code,
        airport_label,
        airport_iso_country,
        airport_municipality,

        date_trunc('hour', snapshot_at)                        as activity_hour,
        ingest_date,

        count(distinct aircraft_icao24)                        as distinct_aircraft,
        count(*)                                               as position_count,

        count(distinct case
            when flight_phase = 'montee' then aircraft_icao24
        end)                                                   as climbing_aircraft,
        count(distinct case
            when flight_phase = 'descente' then aircraft_icao24
        end)                                                   as descending_aircraft,
        count(distinct case
            when is_on_ground then aircraft_icao24
        end)                                                   as ground_aircraft,

        round(avg(distance_km), 2)                             as avg_distance_km,
        round(avg(barometric_altitude_m), 0)                   as avg_altitude_m,
        max(ground_speed_kt)                                   as max_ground_speed_kt

    from proximity
    group by
        airport_id,
        airport_icao_code,
        airport_iata_code,
        airport_label,
        airport_iso_country,
        airport_municipality,
        date_trunc('hour', snapshot_at),
        ingest_date

)

select * from hourly
