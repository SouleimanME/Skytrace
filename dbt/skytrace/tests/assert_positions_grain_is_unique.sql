/*
    Le grain annonce de la table de faits est (appareil, instant).

    Ce test est le garde-fou du modele : si la deduplication amont ou la
    strategie incrementale `delete+insert` regressent, les doublons
    reapparaissent silencieusement et TOUS les comptages d'aeronefs
    deviennent faux sans qu'aucune requete ne tombe en erreur.

    dbt considere le test en echec s'il renvoie au moins une ligne.
*/

select
    aircraft_icao24,
    snapshot_at,
    count(*) as row_count
from {{ ref('fct_aircraft_positions') }}
group by aircraft_icao24, snapshot_at
having count(*) > 1
