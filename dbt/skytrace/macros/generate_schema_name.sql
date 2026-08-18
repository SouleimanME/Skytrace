{#
    Par defaut dbt prefixe les schemas personnalises par le schema cible
    (`main_staging`, `main_marts`...). On preferre des noms propres :
    `staging`, `intermediate`, `marts`. Un modele sans `+schema` retombe
    sur le schema cible du profil.
#}
{% macro generate_schema_name(custom_schema_name, node) -%}
    {%- set default_schema = target.schema -%}
    {%- if custom_schema_name is none -%}
        {{ default_schema }}
    {%- else -%}
        {{ custom_schema_name | trim }}
    {%- endif -%}
{%- endmacro %}
