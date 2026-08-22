{{
    config(
        materialized = "table"
    )
}}

/*
    Activite par compagnie et par aeroport. Grain : un aeroport, une compagnie.

    Construit a partir du rapprochement spatial (int_positions_near_airports) :
    chaque appareil detecte pres d'un aeroport est rattache a sa compagnie via
    le prefixe de son indicatif. Repond a "part de marche des compagnies a
    CDG / Orly / ...".

    `airline_name` est nul quand le prefixe ne correspond a aucune compagnie
    connue (aviation d'affaires, militaire, indicatifs non standard) : le
    tableau de bord peut alors filtrer sur les compagnies identifiees.
*/

with proximity as (

    select * from {{ ref('int_positions_near_airports') }}

),

airlines as (

    select * from {{ ref('dim_airline') }}

),

tagged as (

    select
        airport_id,
        airport_icao_code,
        airport_iata_code,
        airport_label,
        airport_iso_country,
        upper(left(trim(callsign), 3))       as airline_icao,
        aircraft_icao24
    from proximity
    where callsign is not null
      and length(trim(callsign)) >= 3

)

select
    tagged.airport_id,
    tagged.airport_icao_code,
    tagged.airport_iata_code,
    tagged.airport_label,
    tagged.airport_iso_country,
    tagged.airline_icao,
    airlines.airline_name,
    airlines.country                          as airline_country,

    count(distinct tagged.aircraft_icao24)    as distinct_aircraft,
    count(*)                                  as observation_count

from tagged
left join airlines
    on tagged.airline_icao = airlines.airline_icao
group by
    tagged.airport_id,
    tagged.airport_icao_code,
    tagged.airport_iata_code,
    tagged.airport_label,
    tagged.airport_iso_country,
    tagged.airline_icao,
    airlines.airline_name,
    airlines.country
