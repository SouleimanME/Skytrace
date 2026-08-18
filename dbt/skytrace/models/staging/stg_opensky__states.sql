{{
    config(
        materialized = "view"
    )
}}

/*
    Nettoyage et typage des positions brutes.

    Regles appliquees ici, et nulle part ailleurs :
      * renommage en vocabulaire metier (`velocity` -> `ground_speed_ms`) ;
      * conversion des horodatages Unix en timestamps ;
      * normalisation des chaines (l'indicatif OpenSky est complete a droite
        par des espaces : "AFR23   ") ;
      * rejet des lignes sans position exploitable.

    Ce qui n'est PAS fait ici : aucune agregation, aucune jointure. Le
    staging reste une correspondance 1 pour 1 avec la source, ce qui rend
    les anomalies faciles a localiser.
*/

with source as (

    select * from {{ source('raw', 'opensky_states') }}

),

cleaned as (

    select
        -- identifiants
        lower(trim(icao24))                                as aircraft_icao24,
        nullif(trim(callsign), '')                         as callsign,
        nullif(trim(origin_country), '')                   as origin_country,

        -- temps
        to_timestamp(snapshot_ts)                          as snapshot_at,
        to_timestamp(time_position)                        as position_at,
        to_timestamp(last_contact)                         as last_contact_at,

        -- position
        longitude,
        latitude,
        baro_altitude                                      as barometric_altitude_m,
        geo_altitude                                       as geometric_altitude_m,
        coalesce(on_ground, false)                         as is_on_ground,

        -- dynamique de vol
        velocity                                           as ground_speed_ms,
        true_track                                         as heading_deg,
        vertical_rate                                      as vertical_rate_ms,

        -- transpondeur
        nullif(trim(squawk), '')                           as squawk,
        coalesce(spi, false)                               as has_special_purpose_indicator,
        position_source                                    as position_source_code,
        category                                           as aircraft_category_code,

        -- metadonnees de pipeline
        region                                             as ingestion_region,
        ingested_at,
        cast(ingest_date as date)                          as ingest_date,
        cast(ingest_hour as integer)                       as ingest_hour

    from source

)

select *
from cleaned
where aircraft_icao24 is not null
  -- Une position hors bornes n'est pas une donnee manquante mais une
  -- donnee fausse : on l'ecarte plutot que de la propager.
  and latitude is not null
  and longitude is not null
  and latitude between -90 and 90
  and longitude between -180 and 180
