{{
    config(
        materialized = "table"
    )
}}

/*
    Panel horaire trafic x qualite de l'air. Grain : un aeroport, une heure.

    Jointure de l'activite aeroportuaire observee (fct_airport_activity) avec
    les polluants mesures au sol (stg_open_meteo__air_quality), sur l'aeroport
    et l'heure. Jointure interne : on ne garde que les couples ou les deux
    signaux existent, ce qui restreint aux 14 aeroports suivis en qualite de
    l'air.

    C'est la table qui alimente l'analyse "le trafic se lit-il dans le NO2 ?".
    On expose `hour_of_day` pour permettre le retrait du cycle journalier
    (le principal facteur de confusion) au moment de l'analyse.
*/

with activity as (

    select * from {{ ref('fct_airport_activity') }}

),

air_quality as (

    select * from {{ ref('stg_open_meteo__air_quality') }}

),

joined as (

    select
        activity.airport_id,
        activity.airport_icao_code,
        activity.airport_iata_code,
        activity.airport_label,
        activity.airport_iso_country,

        activity.activity_hour,
        extract('hour' from activity.activity_hour)       as hour_of_day,
        extract('dow' from activity.activity_hour)        as day_of_week,

        activity.distinct_aircraft,
        activity.position_count,
        activity.climbing_aircraft,
        activity.descending_aircraft,

        air_quality.no2_ugm3,
        air_quality.pm25_ugm3,
        air_quality.pm10_ugm3,
        air_quality.ozone_ugm3

    from activity
    inner join air_quality
        on activity.airport_icao_code = air_quality.airport_icao
        and activity.activity_hour = air_quality.measured_hour

)

select * from joined
