{{
    config(
        materialized = "view"
    )
}}

/*
    Referentiel aeroports typé et restreint.

    Le fichier source contient 85 000 lignes dont l'immense majorite sont
    des helisurfaces, des terrains prives ou des aerodromes fermes. Pour
    rattacher un vol commercial a un aeroport on ne garde que les pistes
    grandes et moyennes : ~5 000 lignes, soit une dimension qui tient en
    memoire et rend le rapprochement geographique trivial.

    Tout est en `try_cast` : le CSV amont est lu en texte pur (couche
    bronze fidele) et une valeur aberrante isolee ne doit pas faire
    echouer la construction complete.
*/

with source as (

    select * from {{ source('raw', 'ourairports_airports') }}

),

typed as (

    select
        try_cast(id as integer)                      as airport_id,
        nullif(trim(ident), '')                      as airport_ident,
        nullif(trim(type), '')                       as airport_type,
        nullif(trim(name), '')                       as airport_name,

        try_cast(latitude_deg as double)             as latitude,
        try_cast(longitude_deg as double)            as longitude,
        try_cast(elevation_ft as integer)            as elevation_ft,

        nullif(trim(continent), '')                  as continent,
        nullif(trim(iso_country), '')                as iso_country,
        nullif(trim(iso_region), '')                 as iso_region,
        nullif(trim(municipality), '')               as municipality,

        lower(trim(coalesce(scheduled_service, ''))) = 'yes' as has_scheduled_service,

        nullif(trim(icao_code), '')                  as icao_code,
        nullif(trim(iata_code), '')                  as iata_code,
        nullif(trim(gps_code), '')                   as gps_code

    from source

)

select *
from typed
-- Tous les aeroports a voilure fixe (grands, moyens, petits). On ecarte les
-- helisurfaces, hydrobases et terrains fermes : sans trafic ADS-B a voilure
-- fixe, ils n'ajouteraient que du bruit au rapprochement spatial.
where airport_type in ('large_airport', 'medium_airport', 'small_airport')
  and airport_id is not null
  and latitude is not null
  and longitude is not null
