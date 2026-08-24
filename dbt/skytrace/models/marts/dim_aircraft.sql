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
        modele, operateur) et par le referentiel compagnies OpenFlights.

    C'est ce qui permet les analyses "part de marche des compagnies" et
    "Airbus vs Boeing par pays".

    RATTACHEMENT A UNE COMPAGNIE : deux cles, pas une.

    La premiere version deduisait la compagnie du seul prefixe d'indicatif
    (les trois premieres lettres, qui sont le code OACI de l'exploitant).
    C'est la cle la plus couvrante, et la plus bruyante : les codes se
    reutilisent, et OpenFlights - fige depuis des annees - contient des
    entrees perimees. Le tableau de bord affichait ainsi "Tiphook PLC", un
    loueur de conteneurs, parmi les compagnies de Francfort.

    Trois corrections, verifiees sur la donnee reelle :

      1. Le code d'exploitant declare dans la base aeronefs prime sur le
         prefixe d'indicatif. Il est moins couvrant (un tiers des appareils
         observes) mais il est declare, pas devine. Exemple : les appareils
         etiquetes "O Air" declarent DLH, ce sont des Lufthansa.

      2. Un prefixe d'indicatif n'est retenu que s'il est ATTESTE, c'est-a-dire
         s'il apparait comme code d'exploitant d'au moins un appareil de la
         base. Un code que personne ne declare n'est pas un code : c'est une
         collision. Cette regle ecarte environ un rattachement sur cinq, tous
         du meme genre - "12 North", "Regional Air Iceland", "All Spain".

      3. Quand OpenFlights donne une compagnie qu'il declare lui-meme
         disparue, et que la base aeronefs nomme un exploitant, c'est ce
         dernier qui gagne : une entree perimee contredite par la donnee
         courante ne fait pas le poids. C'est ce qui transforme
         "Tiphook PLC" en "AeroLogic".

    Le nom canonique reste celui d'OpenFlights partout ailleurs : les deux
    sources s'accordent dans 90 % des cas, et les 10 % restants sont des
    variantes d'ecriture ("Klm", "Scandinavian Airlines") qui fragmenteraient
    les agregats si on les melangeait.

    `airline_source` dit laquelle des deux cles a tranche, pour que le
    tableau de bord puisse distinguer un rattachement declare d'un
    rattachement deduit.
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

-- Codes d'exploitant reellement declares dans la base aeronefs. Sert de
-- liste blanche : un prefixe d'indicatif absent d'ici est une collision,
-- pas une compagnie.
attested_codes as (

    select distinct operator_icao as airline_icao
    from metadata
    where operator_icao is not null

),

-- Resolution de la compagnie, cle par cle, avant de choisir.
resolved as (

    select
        aggregated.aircraft_icao24,

        -- cle 1 : declaree dans la base aeronefs
        metadata.operator_icao                                      as declared_icao,

        -- cle 2 : deduite du prefixe d'indicatif, retenue seulement si
        -- attestee par ailleurs
        case
            when attested_codes.airline_icao is not null
            then upper(left(callsign_usage.callsign, 3))
        end                                                         as inferred_icao,

        metadata.operator                                           as declared_operator

    from aggregated
    left join callsign_usage
        on aggregated.aircraft_icao24 = callsign_usage.aircraft_icao24
    left join metadata
        on aggregated.aircraft_icao24 = metadata.aircraft_icao24
    left join attested_codes
        on upper(left(callsign_usage.callsign, 3)) = attested_codes.airline_icao

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

        -- Code OACI de l'exploitant : le declare l'emporte sur le deduit.
        coalesce(resolved.declared_icao, resolved.inferred_icao)    as airline_icao,

        -- D'ou vient le rattachement. Un tableau de bord honnete doit pouvoir
        -- distinguer une compagnie declaree d'une compagnie devinee.
        case
            when resolved.declared_icao is not null then 'code exploitant'
            when resolved.inferred_icao is not null then 'indicatif'
        end                                                         as airline_source,

        -- Nom canonique OpenFlights, sauf quand OpenFlights se declare
        -- lui-meme perime et que la base aeronefs nomme un exploitant.
        case
            when airlines.airline_name is null      then resolved.declared_operator
            when airlines.is_active                 then airlines.airline_name
            else coalesce(resolved.declared_operator, airlines.airline_name)
        end                                                         as airline_name,
        airlines.country                                            as airline_country

    from aggregated
    left join callsign_usage
        on aggregated.aircraft_icao24 = callsign_usage.aircraft_icao24
    left join metadata
        on aggregated.aircraft_icao24 = metadata.aircraft_icao24
    left join resolved
        on aggregated.aircraft_icao24 = resolved.aircraft_icao24
    left join airlines
        on coalesce(resolved.declared_icao, resolved.inferred_icao) = airlines.airline_icao

)

select * from final
