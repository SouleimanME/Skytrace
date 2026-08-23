{{
    config(
        materialized = "incremental",
        unique_key = ["aircraft_icao24", "snapshot_at"],
        incremental_strategy = "delete+insert",
        on_schema_change = "append_new_columns"
    )
}}

/*
    Table de faits centrale. Grain : un aeronef, un instant.

    Materialisation incrementale : le lac accumule des millions de lignes
    au fil des semaines, mais chaque execution ne traite que les snapshots
    posterieurs au dernier deja charge. Reconstruire integralement a chaque
    run couterait de plus en plus cher pour un resultat identique.

    Strategie `delete+insert` plutot que `append` : si un snapshot est
    rejoue (incident, backfill), ses lignes sont remplacees et non
    dupliquees. L'execution reste donc idempotente.

    Pour tout reconstruire de zero : `dbt run --full-refresh`.
*/

with positions as (

    select * from {{ ref('int_positions_deduplicated') }}

    {% if is_incremental() %}
    -- Ne relire que ce qui est arrive depuis le dernier chargement.
    where snapshot_at > (
        select coalesce(max(snapshot_at), '1970-01-01'::timestamptz)
        from {{ this }}
    )
    {% endif %}

),

enriched as (

    select
        -- cles
        aircraft_icao24,
        snapshot_at,
        callsign,
        origin_country,

        -- position
        latitude,
        longitude,
        -- Les deux systemes d'unites sont exposes cote a cote. Le tableau de
        -- bord affiche les unites AERONAUTIQUES (pieds, noeuds) : ce sont les
        -- unites de reference de l'OACI, utilisees en vol partout dans le
        -- monde, France comprise. Les colonnes SI (metres, m/s, km/h) restent
        -- disponibles pour toute analyse hors contexte aeronautique.
        barometric_altitude_m,
        geometric_altitude_m,
        barometric_altitude_m * 3.28084             as barometric_altitude_ft,

        -- dynamique
        ground_speed_ms,
        ground_speed_ms * 3.6                       as ground_speed_kmh,
        ground_speed_ms * 1.94384                   as ground_speed_kt,
        heading_deg,
        vertical_rate_ms,

        -- etat
        is_on_ground,
        squawk,
        has_special_purpose_indicator,

        -- Phase de vol deduite du taux de montee. Le seuil de 1,5 m/s
        -- (~300 ft/min) filtre le bruit de mesure du transpondeur : en
        -- croisiere stabilisee le taux oscille en permanence autour de 0.
        case
            when is_on_ground                       then 'sol'
            when vertical_rate_ms is null           then 'inconnu'
            when vertical_rate_ms >  1.5            then 'montee'
            when vertical_rate_ms < -1.5            then 'descente'
            else 'croisiere'
        end                                         as flight_phase,

        -- Ecart entre l'instant du snapshot et la derniere position connue :
        -- au-dela de quelques dizaines de secondes, la position est extrapolee.
        case
            when position_at is null then null
            else epoch(snapshot_at - position_at)
        end                                         as position_age_seconds,

        -- audit
        ingestion_region,
        ingested_at,
        ingest_date,
        ingest_hour

    from positions

)

select * from enriched
