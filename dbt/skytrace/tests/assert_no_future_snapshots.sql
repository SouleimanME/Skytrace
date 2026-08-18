/*
    Aucun releve ne peut etre date du futur.

    Un horodatage futur trahit soit une derive d'horloge sur un recepteur
    ADS-B, soit une confusion secondes/millisecondes lors du parsing.
    Cette seconde erreur est particulierement vicieuse : elle ne fait pas
    planter le pipeline, elle projette juste toutes les donnees en l'an
    58000 et vide tous les tableaux de bord filtres sur "24 dernieres heures".

    Tolerance de 5 minutes pour absorber les desynchronisations d'horloge
    benignes entre le serveur OpenSky et la machine d'execution.
*/

select
    aircraft_icao24,
    snapshot_at,
    ingested_at
from {{ ref('fct_aircraft_positions') }}
where snapshot_at > now() + interval 5 minute
