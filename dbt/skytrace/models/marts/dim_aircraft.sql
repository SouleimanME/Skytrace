{{
    config(
        materialized = "table"
    )
}}

/*
    Dimension aeronef. Grain : une adresse OACI 24 bits (= un appareil).

    Deux origines combinees :
      * l'observation repetee (pays d'enregistrement, indicatifs, plafond) ;
      * l'enrichissement par la base aeronefs OpenSky (type reel, constructeur,
        modele, operateur) et par le referentiel compagnies OpenFlights
        (nom de la compagnie deduite du prefixe d'indicatif).

    C'est ce qui permet les analyses "part de marche des compagnies" et
    "Airbus vs Boeing par pays".
*/

with positions as (

    select * from {{ ref('fct_aircraft_positions') }}

),

aggregated as (

    select
        aircraft_icao24,

        -- Le pays est stable pour un appareil donne : `any_value` evite un
        -- group by inutile tout en restant explicite sur l'intention.
        any_value(origin_country)                          as origin_country,

        min(snapshot_at)                                   as first_seen_at,
        max(snapshot_at)                                   as last_seen_at,

        count(*)                                           as observation_count,
        count(distinct callsign)                           as distinct_callsign_count,
        count(distinct ingest_date)                        as active_day_count,

        max(barometric_altitude_m)                         as max_altitude_m,
        max(ground_speed_kt)                               as max_ground_speed_kt,

        -- Part du temps passe au sol : discrimine un avion de ligne (proche
        -- de 0) d'un appareil qui n'a fait que rouler sur le tarmac.
        avg(case when is_on_ground then 1.0 else 0.0 end)  as on_ground_ratio

    from positions
    group by aircraft_icao24

),

-- Indicatif le plus frequemment utilise par l'appareil : sert d'etiquette
-- lisible dans les tableaux de bord. Calcule par fenetrage plutot que par
-- sous-requete correlee, qui serait recalculee ligne a ligne.
callsign_usage as (

    select
        aircraft_icao24,
        callsign,
        count(*) as usage_count
    from positions
    where callsign is not null
    group by aircraft_icao24, callsign
    qualify row_number() over (
        partition by aircraft_icao24
        order by count(*) desc, callsign
    ) = 1

),

metadata as (

    select * from {{ ref('stg_opensky__aircraft') }}

),

airlines as (

    select * from {{ ref('dim_airline') }}

),

final as (

    select
        aggregated.*,
        callsign_usage.callsign                                     as most_frequent_callsign,
        date_diff(
            'second', aggregated.first_seen_at, aggregated.last_seen_at
        )                                                           as observed_span_seconds,

        -- enrichissement base aeronefs (peut etre nul si appareil inconnu)
        metadata.registration,
        metadata.aircraft_type,
        metadata.manufacturer,
        metadata.model,
        metadata.operator,
        metadata.built_year,

        -- constructeur regroupe : discrimine les deux grands avionneurs du
        -- reste, pour l'analyse "Airbus vs Boeing".
        case
            when metadata.manufacturer ilike '%airbus%'  then 'Airbus'
            when metadata.manufacturer ilike '%boeing%'  then 'Boeing'
            when metadata.manufacturer ilike '%embraer%' then 'Embraer'
            when metadata.manufacturer ilike '%bombardier%'
              or metadata.manufacturer ilike '%canadair%' then 'Bombardier'
            when metadata.manufacturer ilike '%atr%'     then 'ATR'
            when metadata.manufacturer is null           then 'Inconnu'
            else 'Autre'
        end                                                         as manufacturer_group,

        -- compagnie deduite du prefixe (3 lettres) de l'indicatif le plus
        -- frequent : c'est le code OACI de la compagnie.
        upper(left(callsign_usage.callsign, 3))                     as airline_icao,
        airlines.airline_name,
        airlines.country                                            as airline_country

    from aggregated
    left join callsign_usage
        on aggregated.aircraft_icao24 = callsign_usage.aircraft_icao24
    left join metadata
        on aggregated.aircraft_icao24 = metadata.aircraft_icao24
    left join airlines
        on upper(left(callsign_usage.callsign, 3)) = airlines.airline_icao

)

select * from final
