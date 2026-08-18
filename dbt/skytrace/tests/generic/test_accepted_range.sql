{#
    Test generique reutilisable : verifie qu'une colonne numerique reste
    dans un intervalle physiquement plausible.

    dbt ne fournit en standard que `unique`, `not_null`, `accepted_values`
    et `relationships`. Le controle d'intervalle vient habituellement du
    package `dbt_utils` / `dbt_expectations` ; on l'implemente ici pour
    garder le projet sans dependance externe (une installation de moins a
    gerer en CI, et le comportement reste lisible dans le depot).

    Les valeurs nulles sont ignorees : c'est le role de `not_null` de les
    detecter, pas celui d'un test d'intervalle.

    Exemple d'utilisation dans un fichier de proprietes :

        - name: latitude
          tests:
            - accepted_range:
                min_value: -90
                max_value: 90
#}

{% test accepted_range(model, column_name, min_value=none, max_value=none, inclusive=true) %}

with validation as (

    select {{ column_name }} as value_under_test
    from {{ model }}
    where {{ column_name }} is not null

)

select value_under_test
from validation
where false
    {% if min_value is not none %}
        or value_under_test {{ '<' if inclusive else '<=' }} {{ min_value }}
    {% endif %}
    {% if max_value is not none %}
        or value_under_test {{ '>' if inclusive else '>=' }} {{ max_value }}
    {% endif %}

{% endtest %}
