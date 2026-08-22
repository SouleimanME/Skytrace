{{
    config(
        materialized = "table"
    )
}}

/*
    Dimension compagnie. Grain : un code OACI de compagnie (3 lettres).

    Dimension conforme : partagee par dim_aircraft (compagnie de l'appareil)
    et par les faits d'activite par compagnie. La cle de jointure avec les
    vols est le prefixe de l'indicatif.
*/

select
    airline_icao,
    airline_name,
    airline_iata,
    country,
    is_active
from {{ ref('stg_openflights__airlines') }}
