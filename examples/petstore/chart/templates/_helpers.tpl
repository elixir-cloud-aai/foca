{{/*
Settings of the app for the templates of the library chart foca-common (see
`foca-common.checkSettings`), as YAML; the templates are called with a dict of
the root context (`root`) and these settings (`settings`).
*/}}
{{- define "petstore.settings" -}}
valuesKey: petstore
mongodb:
  # `db.dbs` of the base config (`config.yaml`)
  databases: [petstore]
  # For apps whose base config enables access control: its database
  # (`security.access_control.db_name`), e.g., FOCA's default
  # accessControlDatabase: access_control_db
{{- end -}}
