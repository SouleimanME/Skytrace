{{
    config(
        materialized = "view"
    )
}}

/*
    Rapprochement geographique avion <-> aeroport.

    Le probleme : associer chaque position basse a l'aeroport qu'elle
    survole revient a un produit cartesien positions x aeroports, soit
    des milliards de calculs de distance des que le lac grossit.

    La parade classique est le "blocking" spatial : on decoupe le monde en
    cellules de 1 degre, et on ne compare une position qu'aux aeroports de
    sa cellule et des 8 cellules voisines. Le voisinage est indispensable,
    sinon un avion situe juste de l'autre cote d'une frontiere de cellule
    ne serait jamais rattache a l'aeroport tout proche. Le calcul exact de
    haversine ne s'applique ensuite qu'aux couples survivants.

    Filtre amont : seules les positions en phase basse sont candidates.
    Un long-courrier en croisiere a 11 000 m au-dessus d'Orly n'a rien a
    voir avec l'activite d'Orly.
*/

{% set radius_km = var('airport_activity_radius_km') %}
{% set approach_altitude_m = var('approach_altitude_m') %}

with low_positions as (

    select *
    from {{ ref('fct_aircraft_positions') }}
    where is_on_ground
       or barometric_altitude_m <= {{ approach_altitude_m }}

),

-- Les 9 cellules a explorer autour de chaque position.
neighbour_offsets as (

    select *
    from (
        values (-1, -1), (-1, 0), (-1, 1),
               ( 0, -1), ( 0, 0), ( 0, 1),
               ( 1, -1), ( 1, 0), ( 1, 1)
    ) as offsets(d_lat, d_lon)

),

candidates as (

    select
        low_positions.*,
        (
            (cast(floor(low_positions.latitude) as integer) + neighbour_offsets.d_lat)
            || '_'
            || (cast(floor(low_positions.longitude) as integer) + neighbour_offsets.d_lon)
        ) as probe_cell
    from low_positions
    cross join neighbour_offsets

),

airports as (

    select * from {{ ref('dim_airport') }}

),

matched as (

    select
        candidates.aircraft_icao24,
        candidates.snapshot_at,
        candidates.callsign,
        candidates.origin_country,
        candidates.latitude,
        candidates.longitude,
        candidates.barometric_altitude_m,
        candidates.ground_speed_kt,
        candidates.vertical_rate_ms,
        candidates.is_on_ground,
        candidates.flight_phase,
        candidates.ingest_date,
        candidates.ingest_hour,

        airports.airport_id,
        airports.icao_code       as airport_icao_code,
        airports.iata_code       as airport_iata_code,
        airports.airport_name,
        airports.airport_label,
        airports.iso_country     as airport_iso_country,
        airports.municipality    as airport_municipality,

        {{ haversine_km(
            'candidates.latitude', 'candidates.longitude',
            'airports.latitude', 'airports.longitude'
        ) }} as distance_km

    from candidates
    inner join airports
        on candidates.probe_cell = airports.grid_cell

),

nearest as (

    -- Un avion peut tomber dans le rayon de plusieurs aeroports (Orly et
    -- Villacoublay se chevauchent). On ne conserve que le plus proche pour
    -- ne pas compter deux fois le meme mouvement.
    select *
    from matched
    where distance_km <= {{ radius_km }}
    qualify row_number() over (
        partition by aircraft_icao24, snapshot_at
        order by distance_km
    ) = 1

)

select * from nearest
