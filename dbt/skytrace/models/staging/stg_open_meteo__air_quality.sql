{{
    config(
        materialized = "view"
    )
}}

/*
    Qualite de l'air horaire, nettoyee et typee.

    Correspondance 1 pour 1 avec la source. On tronque l'horodatage a l'heure
    (par securite : la source est deja horaire) pour garantir la cle de
    jointure avec l'activite aeroportuaire, elle aussi horaire.
*/

with source as (

    select * from {{ source('raw', 'open_meteo_air_quality') }}

),

cleaned as (

    select
        upper(trim(airport_icao))            as airport_icao,
        nullif(trim(airport_iata), '')       as airport_iata,
        latitude,
        longitude,
        date_trunc('hour', measured_at)      as measured_hour,
        nitrogen_dioxide                     as no2_ugm3,
        pm2_5                                as pm25_ugm3,
        pm10                                 as pm10_ugm3,
        ozone                                as ozone_ugm3,
        ingested_at

    from source

)

select *
from cleaned
where airport_icao is not null
  and measured_hour is not null
