{{
    config(
        materialized = "table"
    )
}}

/*
    Trafic agrege. Grain : une heure, un pays d'immatriculation.

    Table de faits agregee : elle repond en quelques millisecondes aux
    questions du tableau de bord ("courbe du trafic sur 24 h", "top 10 des
    pays") la ou la table de faits detaillee devrait rescanner des
    millions de lignes a chaque rafraichissement.

    `distinct_aircraft` compte des appareils uniques : cette mesure n'est
    pas additive dans le temps (sommer 24 heures ne donne PAS le nombre
    d'appareils de la journee, un meme avion volant plusieurs heures).
    D'ou la presence de `position_count`, elle additive, a cote.
*/

with positions as (

    select * from {{ ref('fct_aircraft_positions') }}

),

hourly as (

    select
        date_trunc('hour', snapshot_at)                     as traffic_hour,
        ingest_date,
        coalesce(origin_country, 'Inconnu')                 as origin_country,
        ingestion_region,

        count(distinct aircraft_icao24)                     as distinct_aircraft,
        count(*)                                            as position_count,
        count(distinct snapshot_at)                         as snapshot_count,

        count(distinct case when is_on_ground then aircraft_icao24 end)
                                                            as aircraft_on_ground,
        count(distinct case when flight_phase = 'croisiere' then aircraft_icao24 end)
                                                            as aircraft_cruising,

        round(avg(barometric_altitude_m), 0)                as avg_altitude_m,
        round(
            quantile_cont(barometric_altitude_m, 0.5), 0
        )                                                   as median_altitude_m,
        round(avg(ground_speed_kt), 1)                      as avg_ground_speed_kt,
        max(ground_speed_kt)                                as max_ground_speed_kt

    from positions
    group by
        date_trunc('hour', snapshot_at),
        ingest_date,
        coalesce(origin_country, 'Inconnu'),
        ingestion_region

),

final as (

    select
        *,
        -- Nombre moyen d'appareils vus par snapshot : neutralise l'effet
        -- d'une heure ou l'ordonnanceur a tourne plus ou moins souvent.
        round(position_count::double / nullif(snapshot_count, 0), 1)
            as avg_aircraft_per_snapshot
    from hourly

)

select * from final
