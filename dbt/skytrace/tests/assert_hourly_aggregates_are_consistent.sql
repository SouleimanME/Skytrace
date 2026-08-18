/*
    Coherence interne de l'agregat horaire.

    Deux invariants qui doivent tenir par construction :
      1. le nombre d'appareils distincts ne peut pas depasser le nombre de
         positions (un appareil produit au minimum une position) ;
      2. les sous-ensembles (au sol, en croisiere) ne peuvent pas depasser
         le total d'appareils distincts.

    Si l'un des deux saute, c'est qu'une jointure a duplique des lignes en
    amont de l'agregation - le bug le plus courant et le plus discret d'un
    modele dimensionnel.
*/

select
    traffic_hour,
    origin_country,
    distinct_aircraft,
    position_count,
    aircraft_on_ground,
    aircraft_cruising
from {{ ref('fct_traffic_hourly') }}
where distinct_aircraft > position_count
   or aircraft_on_ground > distinct_aircraft
   or aircraft_cruising > distinct_aircraft
