{{
    config(
        materialized = "view"
    )
}}

/*
    Referentiel compagnies OpenFlights, typé et restreint.

    Grain : un code OACI de compagnie (3 lettres) - c'est le prefixe des
    indicatifs de vol. On ecarte les lignes sans code OACI valide et on
    deduplique en privilegiant les compagnies actives.
*/

with source as (

    select * from {{ source('raw', 'openflights_airlines') }}

),

typed as (

    select
        nullif(upper(trim(icao)), '')       as airline_icao,
        nullif(trim(name), '')              as airline_name,
        nullif(upper(trim(iata)), '')       as airline_iata,
        nullif(trim(country), '')           as country,
        coalesce(trim(active), '') = 'Y'    as is_active

    from source

)

select *
from typed
where airline_icao is not null
  and length(airline_icao) = 3
  and airline_name is not null
qualify row_number() over (
    partition by airline_icao
    order by (case when is_active then 0 else 1 end), airline_name
) = 1
