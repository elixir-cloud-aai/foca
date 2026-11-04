{{/*
Settings of the app for the templates of the library chart foca-common, as in
the petstore example's chart.
*/}}
{{- define "gateway.settings" -}}
valuesKey: gateway
mongodb:
  # `db.dbs` of the base config (`config.yaml`)
  databases: [gateway]
{{- end -}}
