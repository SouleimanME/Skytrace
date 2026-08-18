{{
    config(
        materialized = "view"
    )
}}

/*
    Deduplication des positions.

    Pourquoi c'est necessaire : l'ingestion est volontairement idempotente
    mais pas exactement-une-fois. Une relance d'un snapshot deja ingere, ou
    deux workers qui se chevauchent, produisent deux fichiers Parquet
    contenant le meme couple (appareil, instant). Sans cette etape, le
    comptage d'aeronefs serait gonfle a chaque incident d'ordonnancement.

    Regle de resolution : en cas de doublon on garde la ligne ingeree le
    plus tard, c'est-a-dire la version la plus recente de la verite.
*/

with positions as (

    select * from {{ ref('stg_opensky__states') }}

),

deduplicated as (

    select *
    from positions
    qualify row_number() over (
        partition by aircraft_icao24, snapshot_at
        order by ingested_at desc
    ) = 1

)

select * from deduplicated
