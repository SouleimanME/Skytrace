{{
    config(
        materialized = "table"
    )
}}

/*
    Activite par compagnie et par aeroport. Grain : un aeroport, une compagnie.

    Construit a partir du rapprochement spatial (int_positions_near_airports) :
    chaque appareil detecte pres d'un aeroport est rattache a sa compagnie.

    LE RATTACHEMENT N'EST PAS REFAIT ICI. Cette table lisait auparavant le
    prefixe d'indicatif pour son propre compte, ce qui dupliquait une regle
    metier deja portee par `dim_aircraft`. La duplication a coute cher : quand
    la regle a ete corrigee dans la dimension, ce modele a continue d'afficher
    "Tiphook PLC" - un loueur de conteneurs - parmi les compagnies de
    Francfort. Une definition metier vit a UN endroit ; ailleurs on la lit.

    `airline_name` est nul quand aucune des deux cles de `dim_aircraft` ne
    designe une compagnie connue (aviation d'affaires, militaire, indicatifs
    non standard) : le tableau de bord peut alors filtrer sur les compagnies
    identifiees.
*/

with proximity as (

    select * from {{ ref('int_positions_near_airports') }}

),

aircraft as (

    select
        aircraft_icao24,
        airline_icao,
        airline_name,
        airline_country,
        airline_source
    from {{ ref('dim_aircraft') }}

)

select
    proximity.airport_id,
    proximity.airport_icao_code,
    proximity.airport_iata_code,
    proximity.airport_label,
    proximity.airport_iso_country,
    aircraft.airline_icao,
    aircraft.airline_name,
    aircraft.airline_country,

    -- D'ou vient le rattachement, conserve jusqu'ici : un classement de parts
    -- de marche n'a pas le meme poids selon qu'il repose sur des exploitants
    -- declares ou sur des indicatifs interpretes.
    aircraft.airline_source,

    count(distinct proximity.aircraft_icao24) as distinct_aircraft,
    count(*)                                  as observation_count

from proximity
inner join aircraft
    on proximity.aircraft_icao24 = aircraft.aircraft_icao24
group by
    proximity.airport_id,
    proximity.airport_icao_code,
    proximity.airport_iata_code,
    proximity.airport_label,
    proximity.airport_iso_country,
    aircraft.airline_icao,
    aircraft.airline_name,
    aircraft.airline_country,
    aircraft.airline_source
