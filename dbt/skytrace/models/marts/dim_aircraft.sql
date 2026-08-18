{{
    config(
        materialized = "table"
    )
}}

/*
    Dimension aeronef. Grain : une adresse OACI 24 bits (= un appareil).

    Construite par agregation des faits plutot que depuis un referentiel
    externe : OpenSky ne publie pas de base immatriculations exploitable
    librement, mais l'observation repetee suffit a caracteriser un
    appareil (pays d'enregistrement, indicatifs utilises, plafond atteint).
*/

with positions as (

    select * from {{ ref('fct_aircraft_positions') }}

),

aggregated as (

    select
        aircraft_icao24,

        -- Le pays est stable pour un appareil donne : `any_value` evite un
        -- group by inutile tout en restant explicite sur l'intention.
        any_value(origin_country)                          as origin_country,

        min(snapshot_at)                                   as first_seen_at,
        max(snapshot_at)                                   as last_seen_at,

        count(*)                                           as observation_count,
        count(distinct callsign)                           as distinct_callsign_count,
        count(distinct ingest_date)                        as active_day_count,

        max(barometric_altitude_m)                         as max_altitude_m,
        max(ground_speed_kt)                               as max_ground_speed_kt,

        -- Part du temps passe au sol : discrimine un avion de ligne (proche
        -- de 0) d'un appareil qui n'a fait que rouler sur le tarmac.
        avg(case when is_on_ground then 1.0 else 0.0 end)  as on_ground_ratio

    from positions
    group by aircraft_icao24

),

-- Indicatif le plus frequemment utilise par l'appareil : sert d'etiquette
-- lisible dans les tableaux de bord. Calcule par fenetrage plutot que par
-- sous-requete correlee, qui serait recalculee ligne a ligne.
callsign_usage as (

    select
        aircraft_icao24,
        callsign,
        count(*) as usage_count
    from positions
    where callsign is not null
    group by aircraft_icao24, callsign
    qualify row_number() over (
        partition by aircraft_icao24
        order by count(*) desc, callsign
    ) = 1

),

final as (

    select
        aggregated.*,
        callsign_usage.callsign                                     as most_frequent_callsign,
        date_diff(
            'second', aggregated.first_seen_at, aggregated.last_seen_at
        )                                                           as observed_span_seconds

    from aggregated
    left join callsign_usage
        on aggregated.aircraft_icao24 = callsign_usage.aircraft_icao24

)

select * from final
