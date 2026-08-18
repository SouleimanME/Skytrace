{#
    Distance orthodromique (grand cercle) entre deux points, en kilometres.

    On implemente la formule de haversine en SQL pur plutot que d'activer
    l'extension `spatial` de DuckDB : une dependance de moins a installer
    en CI, et la precision (~0,5 %) est tres largement suffisante pour
    rattacher un avion a l'aeroport qu'il survole.
#}
{% macro haversine_km(lat_a, lon_a, lat_b, lon_b) -%}
    (
        2 * 6371.0088 * asin(
            sqrt(
                pow(sin(radians({{ lat_b }} - {{ lat_a }}) / 2), 2)
                + cos(radians({{ lat_a }}))
                * cos(radians({{ lat_b }}))
                * pow(sin(radians({{ lon_b }} - {{ lon_a }}) / 2), 2)
            )
        )
    )
{%- endmacro %}


{#
    Cellule de grille de 1 degre, utilisee pour pre-filtrer les
    rapprochements avion/aeroport avant le calcul de distance exact.
#}
{% macro grid_cell(lat, lon) -%}
    (
        cast(floor({{ lat }}) as integer) || '_' || cast(floor({{ lon }}) as integer)
    )
{%- endmacro %}
