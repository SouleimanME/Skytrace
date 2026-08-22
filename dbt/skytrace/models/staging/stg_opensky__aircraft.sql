{{
    config(
        materialized = "view"
    )
}}

/*
    Base aeronefs OpenSky, typee et dedupliquee.

    Grain : une adresse OACI 24 bits. La source contient quelques doublons ;
    on ne garde qu'une ligne par appareil (celle avec le plus de champs
    renseignes, approximee par la presence d'une immatriculation).
*/

with source as (

    select * from {{ source('raw', 'opensky_aircraft_db') }}

),

typed as (

    select
        lower(trim(icao24))                            as aircraft_icao24,
        nullif(trim(registration), '')                 as registration,
        nullif(trim(typecode), '')                     as aircraft_type,
        nullif(trim(manufacturername), '')             as manufacturer,
        nullif(trim(model), '')                        as model,
        nullif(trim(operator), '')                     as operator,
        nullif(upper(trim(operatoricao)), '')          as operator_icao,
        nullif(trim(owner), '')                        as owner,
        try_cast(nullif(left(trim(built), 4), '') as integer) as built_year,
        nullif(trim("categoryDescription"), '')        as category_description

    from source

)

select *
from typed
where aircraft_icao24 is not null
  and length(aircraft_icao24) = 6
qualify row_number() over (
    partition by aircraft_icao24
    order by (case when registration is not null then 0 else 1 end), registration
) = 1
