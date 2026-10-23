{{- /*
Resources of the bundled MongoDB, deployed unless `mongodb.enabled` is false
(see `foca-common.mongodbEnabled`); each template renders nothing otherwise.
Call them with the context dict.
*/ -}}

{{/*
Deployment of MongoDB: one pod with the data volume (the PersistentVolumeClaim
if `mongodb.persistence.enabled`, see `foca-common.mongodbClaimName`; otherwise
an `emptyDir` limited to the size, whose data is lost when the pod is
replaced), credentials from the existing Secret and the init script that
creates the app user if authentication is enabled.
*/}}
{{- define "foca-common.mongodbDeployment" -}}
{{- include "foca-common.checkSettings" . -}}
{{- $root := .root -}}
{{- if include "foca-common.mongodbEnabled" $root -}}
{{- $mongodb := $root.Values.mongodb -}}
{{- $fullname := include "foca-common.mongodbFullname" $root -}}
{{- $persistence := include "foca-common.mongodbPersistence" $root | fromJson -}}
{{- $claimName := include "foca-common.claimName" $persistence -}}
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ $fullname | quote }}
  labels:
    {{- include "foca-common.labels" (dict "root" $root "component" "mongodb") | nindent 4 }}
spec:
  # Longer than the startup window (see `startupProbe`)
  progressDeadlineSeconds: 2400
  replicas: 1
  revisionHistoryLimit: 10
  selector:
    matchLabels:
      {{- include "foca-common.selectorLabels" (dict "root" $root "component" "mongodb") | nindent 6 }}
  # The data volume can only be used by one pod at a time
  strategy:
    type: Recreate
  template:
    metadata:
      labels:
        {{- include "foca-common.selectorLabels" (dict "root" $root "component" "mongodb") | nindent 8 }}
    spec:
      # The pod doesn't call the Kubernetes API
      automountServiceAccountToken: false
      # No `<SERVICE>_PORT` etc. variables of the Services in the namespace
      enableServiceLinks: false
      containers:
      - image: {{ required "mongodb.image must be set" $mongodb.image }}
        imagePullPolicy: {{ $mongodb.imagePullPolicy | default "Always" }}
        name: mongodb
        {{- if $mongodb.auth.enabled }}
        env:
        {{- include "foca-common.mongodbSecretEnv" (dict "root" $root "vars" (dict
          "MONGO_INITDB_ROOT_USERNAME" "root-username"
          "MONGO_INITDB_ROOT_PASSWORD" "root-password"
          "MONGO_APP_USERNAME" "username"
          "MONGO_APP_PASSWORD" "password"
        )) | trim | nindent 8 }}
        {{- end }}
        # TCP checks are cheaper than starting `mongosh` within the CPU limit.
        # MongoDB listens on localhost only while initializing an empty data
        # directory, and recovery after an unclean shutdown can take a while,
        # so allow up to 30 minutes before liveness checks start, as
        # interrupting the initialization skips it on the next start
        startupProbe:
          tcpSocket:
            port: 27017
          periodSeconds: 10
          failureThreshold: 180
        readinessProbe:
          tcpSocket:
            port: 27017
          periodSeconds: 10
        livenessProbe:
          tcpSocket:
            port: 27017
          periodSeconds: 20
        ports:
        - name: mongodb
          containerPort: 27017
          protocol: TCP
        resources: {{- toYaml ($mongodb.resources | default dict) | nindent 10 }}
        securityContext:
          {{- include "foca-common.securityContext" (dict "root" $root "context" $mongodb.securityContext) | nindent 10 }}
        terminationMessagePath: /dev/termination-log
        terminationMessagePolicy: File
        volumeMounts:
        - mountPath: /data/db
          name: mongodb
        # Writable, as the root file system is read-only by default: the
        # socket and the files of the entrypoint's first start (`/tmp`) and
        # the config database directory declared by the image
        - mountPath: /tmp
          name: tmp
        - mountPath: /data/configdb
          name: configdb
        {{- if $mongodb.auth.enabled }}
        - name: init-script
          mountPath: /docker-entrypoint-initdb.d
          readOnly: true
        {{- end }}
      dnsPolicy: ClusterFirst
      restartPolicy: Always
      schedulerName: default-scheduler
      securityContext:
        {{- include "foca-common.securityContext" (dict "root" $root "context" $mongodb.podSecurityContext) | nindent 8 }}
      terminationGracePeriodSeconds: 30
      volumes:
      - name: mongodb
        {{- with $claimName }}
        persistentVolumeClaim:
          claimName: {{ . | quote }}
        {{- else }}
        {{- include "foca-common.emptyDir" ($persistence.size | default nil) | nindent 8 }}
        {{- end }}
      - name: tmp
        {{- include "foca-common.emptyDir" $mongodb.tmpSizeLimit | nindent 8 }}
      - name: configdb
        emptyDir: {}
      {{- if $mongodb.auth.enabled }}
      - name: init-script
        configMap:
          name: {{ printf "%s-init-script" $fullname | quote }}
      {{- end }}
{{ end -}}
{{- end -}}

{{/*
Database of FOCA's access control (`security.access_control.db_name`), if
the app uses it; empty otherwise. In order of precedence: `db_name` of the
config overlay, `mongodb.accessControlDatabase` of the settings (the
`db_name` of the app's base config, if the base config enables access
control) and, if the overlay enables access control (as pydantic parses
`enabled`), FOCA's default database. Fails if the name is not valid (see
`foca-common.checkDatabaseName`). Call with the context dict.
*/}}
{{- define "foca-common.accessControlDatabase" -}}
{{- $accessControl := dict -}}
{{- $security := get (include "foca-common.configOverlay" . | fromYaml) "security" -}}
{{- if and (kindIs "map" $security) (kindIs "map" (get $security "access_control")) -}}
{{- $accessControl = get $security "access_control" -}}
{{- end -}}
{{- $enabled := get $accessControl "enabled" -}}
{{- if kindIs "string" $enabled -}}
{{- $enabled = has (lower $enabled) (list "1" "on" "t" "true" "y" "yes") -}}
{{- else if not (kindIs "bool" $enabled) -}}
{{- $enabled = eq (toString $enabled) "1" -}}
{{- end -}}
{{- if hasKey $accessControl "db_name" -}}
{{- $name := get $accessControl "db_name" -}}
{{- include "foca-common.checkDatabaseName" (dict "name" $name "key" "config.security.access_control.db_name") -}}
{{- $name -}}
{{- else if hasKey (dig "mongodb" dict .settings) "accessControlDatabase" -}}
{{- $name := .settings.mongodb.accessControlDatabase -}}
{{- include "foca-common.checkDatabaseName" (dict "name" $name "key" "mongodb.accessControlDatabase of the settings") -}}
{{- $name -}}
{{- else if $enabled -}}
access_control_db
{{- end -}}
{{- end -}}

{{/*
Fails unless a database name can be used by MongoDB (on any platform) and
in the init script: a non-empty string of fewer than 64 printable ASCII
characters other than `/\. "$*<>:|?` and single quotes. Call with a dict of
the name (`name`) and where it is set (`key`).
*/}}
{{- define "foca-common.checkDatabaseName" -}}
{{- $name := .name -}}
{{- if not (and (kindIs "string" $name) (lt (len $name) 64) (regexMatch `^[\x21-\x7e]+$` $name) (not (regexMatch `[/\\."$*<>:|?']` $name))) -}}
{{- fail (printf "%s: %v is not a valid MongoDB database name (fewer than 64 printable ASCII characters, none of /\\.\"$*<>:|?' or spaces)" .key $name) -}}
{{- end -}}
{{- end -}}

{{/*
Init script of MongoDB, if authentication is enabled: run by MongoDB on its
first start with an empty data directory, as the root user; FOCA creates the
app's collections and indexes on startup. Creates the app user in the first
of the app's databases (`mongodb.databases` of the settings), which FOCA
authenticates against, with access to all of them and to the database of
FOCA's access control, if the app uses it (see
`foca-common.accessControlDatabase`).
*/}}
{{- define "foca-common.mongodbInitScript" -}}
{{- include "foca-common.checkSettings" . -}}
{{- $root := .root -}}
{{- if and (include "foca-common.mongodbEnabled" $root) $root.Values.mongodb.auth.enabled -}}
{{- $databases := dig "mongodb" "databases" (list) .settings -}}
{{- if not (and (kindIs "slice" $databases) $databases) -}}
{{- fail "foca-common: the app's settings must list the app's databases (mongodb.databases) for the bundled MongoDB with authentication" -}}
{{- end -}}
{{- range $databases -}}
{{- include "foca-common.checkDatabaseName" (dict "name" . "key" "mongodb.databases of the settings") -}}
{{- end -}}
{{- with include "foca-common.accessControlDatabase" . -}}
{{- $databases = append $databases . | uniq -}}
{{- end -}}
{{- $roles := list -}}
{{- range $databases -}}
{{- $roles = append $roles (printf "{ role: 'readWrite', db: %s }" (squote .)) -}}
{{- end -}}
apiVersion: v1
kind: ConfigMap
metadata:
  name: {{ printf "%s-init-script" (include "foca-common.mongodbFullname" $root) | quote }}
  labels:
    {{- include "foca-common.labels" (dict "root" $root "component" "mongodb") | nindent 4 }}
data:
  create-app-user.js: |
    // Create the app user with access to the app's databases only; FOCA puts
    // the first database in the connection string, so the app authenticates
    // against it.
    db.getSiblingDB({{ first $databases | squote }}).createUser({
      user: process.env.MONGO_APP_USERNAME,
      pwd: process.env.MONGO_APP_PASSWORD,
      roles: [{{ join ", " $roles }}],
    });
{{ end -}}
{{- end -}}

{{/*
NetworkPolicy of MongoDB, if `mongodb.networkPolicy.enabled`: only the app's
pods of this release (and the worker's, if `worker.enabled`) may connect to
MongoDB; connections from the pod's node (the kubelet's probes) are always
allowed. Egress is not restricted.
*/}}
{{- define "foca-common.mongodbNetworkPolicy" -}}
{{- include "foca-common.checkSettings" . -}}
{{- $root := .root -}}
{{- if and (include "foca-common.mongodbEnabled" $root) (dig "networkPolicy" "enabled" false $root.Values.mongodb) -}}
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: {{ include "foca-common.mongodbFullname" $root | quote }}
  labels:
    {{- include "foca-common.labels" (dict "root" $root "component" "mongodb") | nindent 4 }}
spec:
  podSelector:
    matchLabels:
      {{- include "foca-common.selectorLabels" (dict "root" $root "component" "mongodb") | nindent 6 }}
  policyTypes:
  - Ingress
  ingress:
  - from:
    - podSelector:
        matchLabels:
          {{- include "foca-common.selectorLabels" (dict "root" $root "component" "app") | nindent 10 }}
    {{- if include "foca-common.workerEnabled" $root }}
    - podSelector:
        matchLabels:
          {{- include "foca-common.selectorLabels" (dict "root" $root "component" "worker") | nindent 10 }}
    {{- end }}
    ports:
    - port: 27017
      protocol: TCP
{{ end -}}
{{- end -}}

{{/*
Arguments of `foca-common.claimName` and `foca-common.persistentVolumeClaim`
for MongoDB, as JSON: persistence is enabled if `mongodb.persistence.enabled`
is not set (also without a `mongodb.persistence` map, as in values written
for foca-common 0.1.0, where MongoDB always had its claim). The size is
`mongodb.persistence.size` or the deprecated `mongodb.volumeSize` (of 0.1.0;
numbers of bytes as integers); fails if both are set. Call with the root
context.
*/}}
{{- define "foca-common.mongodbPersistence" -}}
{{- $mongodb := .Values.mongodb | default dict -}}
{{- $persistence := get $mongodb "persistence" | default dict -}}
{{- $size := get $persistence "size" -}}
{{- $volumeSize := get $mongodb "volumeSize" -}}
{{- if and (not (kindIs "invalid" $volumeSize)) (ne (toString $volumeSize) "") -}}
{{- if and (not (kindIs "invalid" $size)) (ne (toString $size) "") -}}
{{- fail "mongodb.volumeSize is deprecated: set only mongodb.persistence.size" -}}
{{- end -}}
{{- $size = ternary $volumeSize (int64 $volumeSize | toString) (kindIs "string" $volumeSize) -}}
{{- end -}}
{{- dict "persistence" $persistence "name" (include "foca-common.mongodbFullname" .) "enabledByDefault" true "size" $size "sizeError" "mongodb.persistence.size must be set if mongodb.persistence.enabled" | toJson -}}
{{- end -}}

{{/*
Name of the PersistentVolumeClaim with MongoDB's data if
`mongodb.persistence.enabled` (true if not set): `existingClaim`, or
MongoDB's own claim; empty otherwise. Call with the root context.
*/}}
{{- define "foca-common.mongodbClaimName" -}}
{{- include "foca-common.claimName" (include "foca-common.mongodbPersistence" . | fromJson) -}}
{{- end -}}

{{/*
PersistentVolumeClaim of MongoDB's data, if `mongodb.persistence.enabled`
(true if not set) without `mongodb.persistence.existingClaim` (see
`foca-common.persistentVolumeClaim`).
*/}}
{{- define "foca-common.mongodbPersistentVolumeClaim" -}}
{{- include "foca-common.checkSettings" . -}}
{{- $root := .root -}}
{{- if include "foca-common.mongodbEnabled" $root -}}
{{- include "foca-common.persistentVolumeClaim" (merge (dict "root" $root "component" "mongodb") (include "foca-common.mongodbPersistence" $root | fromJson)) -}}
{{- end -}}
{{- end -}}

{{/*
Service of MongoDB (port 27017), the host of the app's config (`db.host`).
*/}}
{{- define "foca-common.mongodbService" -}}
{{- include "foca-common.checkSettings" . -}}
{{- $root := .root -}}
{{- if include "foca-common.mongodbEnabled" $root -}}
apiVersion: v1
kind: Service
metadata:
  name: {{ include "foca-common.mongodbFullname" $root | quote }}
  labels:
    {{- include "foca-common.labels" (dict "root" $root "component" "mongodb") | nindent 4 }}
spec:
  ports:
  - name: mongodb
    port: 27017
    protocol: TCP
    targetPort: mongodb
  selector:
    {{- include "foca-common.selectorLabels" (dict "root" $root "component" "mongodb") | nindent 4 }}
  sessionAffinity: None
  type: ClusterIP
{{ end -}}
{{- end -}}
