{#
    Un artefact du lac est-il deja ecrit ?

    POURQUOI CETTE MACRO EXISTE. `fct_aircraft_predictions` lit un fichier
    que dbt ne produit pas : il vient du classifieur, entraine et applique en
    dehors du graphe. L'ordre reel est donc `dbt build` (qui fabrique les
    marts dont le modele apprend), puis `model score`, puis `dbt build` a
    nouveau. Entre les deux, le fichier n'existe pas.

    Sans garde, `read_parquet` echoue sur fichier absent, le modele passe en
    ERROR et tout l'aval est ignore. Cela cassait trois situations qui ne
    sont pas des cas limites : la CI sur son lac synthetique, un clone neuf,
    et toute reconstruction complete anterieure au premier scoring.

    `glob()` repond 0 ligne sur un chemin inexistant au lieu de lever une
    erreur : c'est la seule primitive DuckDB qui permet de POSER la question
    plutot que de la decouvrir en echouant.
#}
{% macro artefact_present(chemin) %}
    {% if not execute %}
        {{ return(false) }}
    {% endif %}
    {% set trouve = run_query("select count(*) as n from glob('" ~ chemin ~ "')") %}
    {{ return(trouve.columns[0].values()[0] > 0) }}
{% endmacro %}
