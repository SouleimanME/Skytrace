{{
    config(
        materialized = "table"
    )
}}

/*
    Predictions du classifieur d'appareils. Grain : un aeronef.

    CE MODELE NE REMPLACE JAMAIS LA DONNEE DECLAREE. Il vit dans une table
    separee, jointe a la lecture. `dim_aircraft` continue de porter le
    constructeur tel que la base aeronefs le donne - nul, le cas echeant.

    C'est une regle et non une precaution : une valeur predite qui prend la
    place d'une valeur observee devient indiscernable d'un fait au bout de
    quelques semaines, et plus personne ne sait ce qui a ete mesure et ce qui
    a ete devine.

    La table porte donc aussi la valeur declaree, precisement pour que la
    comparaison reste possible - et c'est ce qui permet de surveiller la
    derive du modele sans etiquette fraiche.

    COUVERTURE. Le classifieur exigeait trois releves par appareil, ce qui
    ecartait 43 % de la flotte. Il accepte desormais un seul releve, et la
    table couvre tout ce qui a ete observe. La contrepartie est que la
    fiabilite n'est plus uniforme : elle est portee ligne a ligne par
    `score_for_this_aircraft`, et toute lecture qui l'ignore surestime ce que
    la prediction vaut sur les appareils peu vus.
*/

with predictions as (

    select * from {{ source('raw', 'model_predictions') }}

),

aircraft as (

    select
        aircraft_icao24,
        manufacturer_group,
        registration,
        model,
        most_frequent_callsign
    from {{ ref('dim_aircraft') }}

)

select
    predictions.aircraft_icao24,
    predictions.predicted_commercial,
    predictions.probability_commercial,
    predictions.model_trained_at,
    predictions.model_score,
    predictions.training_commercial_share,
    predictions.scored_at,

    -- Combien de fois l'appareil a ete vu, et ce que le modele vaut REELLEMENT
    -- sur les appareils vus autant de fois. Le classifieur accepte desormais
    -- un appareil apercu une seule fois : lui appliquer le score global
    -- (0.94, mesure surtout sur des appareils bien suivis) lui promettrait
    -- une fiabilite qu'il n'a pas - la sienne est plutot de 0.82.
    predictions.observations,
    predictions.score_for_this_aircraft,

    -- La verite declaree, a cote et jamais a la place.
    aircraft.manufacturer_group                                as declared_group,
    aircraft.registration,
    aircraft.model,
    aircraft.most_frequent_callsign,

    -- Le modele est-il d'accord avec ce que la base declare ? Nul quand la
    -- base ne declare rien, c'est-a-dire dans le cas que le modele comble.
    case
        when aircraft.manufacturer_group is null
             or aircraft.manufacturer_group = 'Inconnu' then null
        when aircraft.manufacturer_group in ('Airbus', 'Boeing', 'Embraer', 'Bombardier', 'ATR')
            then (predictions.predicted_commercial = 1)
        else (predictions.predicted_commercial = 0)
    end                                                        as agrees_with_declared

from predictions
left join aircraft using (aircraft_icao24)
