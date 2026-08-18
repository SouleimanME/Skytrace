{{
    config(
        materialized = "table"
    )
}}

/*
    Dimension aeroport. Grain : un aerodrome.

    Dimension conforme, partagee par tous les faits geographiques du
    projet. La cle technique est l'identifiant OurAirports ; les codes
    OACI et IATA restent exposes car ce sont eux que lisent les humains.
*/

with airports as (

    select * from {{ ref('stg_ourairports__airports') }}

),

final as (

    select
        airport_id,
        airport_ident,
        coalesce(icao_code, gps_code, airport_ident)  as icao_code,
        iata_code,
        airport_name,
        airport_type,

        latitude,
        longitude,
        elevation_ft,

        continent,
        iso_country,
        iso_region,
        municipality,
        has_scheduled_service,

        -- Etiquette prete a l'affichage : "Paris Charles de Gaulle (CDG)".
        case
            when iata_code is not null
                then airport_name || ' (' || iata_code || ')'
            else airport_name
        end                                           as airport_label,

        -- Cellule de grille de 1 degre : sert de cle de pre-filtrage au
        -- rapprochement geographique (voir fct_airport_activity).
        {{ grid_cell('latitude', 'longitude') }}      as grid_cell

    from airports

)

select * from final
