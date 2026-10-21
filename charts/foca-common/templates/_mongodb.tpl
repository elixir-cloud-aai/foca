{{- /*
Resources of the bundled MongoDB, deployed unless `mongodb.enabled` is false
(see `foca-common.mongodbEnabled`); each template renders nothing otherwise.
Call them with the context dict.
*/ -}}

{{/*
Deployment of MongoDB: one pod with the data volume (the PersistentVolumeClaim),
credentials from the existing Secret and the init script that creates the app
user if authentication is enabled.
*/}}
{{- define "foca-common.mongodbDeployment" -}}
{{- include "foca-common.checkSettings" . -}}
{{- $root := .root -}}
{{- if include "foca-common.mongodbEnabled" $root -}}
{{- $mongodb := $root.Values.mongodb -}}
{{- $fullname := include "foca-common.mongodbFullname" $root -}}
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
      - image: {{ $mongodb.image }}
        imagePullPolicy: Always
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
        persistentVolumeClaim:
          claimName: {{ $fullname | quote }}
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
Init script of MongoDB, if authentication is enabled: run by MongoDB on its
first start with an empty data directory, as the root user; FOCA creates the
app's collections and indexes on startup. Creates the app user in the first
of the app's databases (`mongodb.databases` of the settings), which FOCA
authenticates against, with access to all of them.
*/}}
{{- define "foca-common.mongodbInitScript" -}}
{{- include "foca-common.checkSettings" . -}}
{{- $root := .root -}}
{{- if and (include "foca-common.mongodbEnabled" $root) $root.Values.mongodb.auth.enabled -}}
{{- $databases := dig "mongodb" "databases" (list) .settings -}}
{{- if not (and (kindIs "slice" $databases) $databases) -}}
{{- fail "foca-common: the app's settings must list the app's databases (mongodb.databases) for the bundled MongoDB with authentication" -}}
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
pods of this release may connect to MongoDB; connections from the pod's node
(the kubelet's probes) are always allowed. Egress is not restricted.
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
    ports:
    - port: 27017
      protocol: TCP
{{ end -}}
{{- end -}}

{{/*
PersistentVolumeClaim of MongoDB's data volume (`mongodb.volumeSize`).
*/}}
{{- define "foca-common.mongodbPersistentVolumeClaim" -}}
{{- include "foca-common.checkSettings" . -}}
{{- $root := .root -}}
{{- if include "foca-common.mongodbEnabled" $root -}}
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: {{ include "foca-common.mongodbFullname" $root | quote }}
  labels:
    {{- include "foca-common.labels" (dict "root" $root "component" "mongodb") | nindent 4 }}
spec:
  accessModes:
  - ReadWriteOnce
  resources:
    requests:
      storage: {{ required "mongodb.volumeSize must be set" $root.Values.mongodb.volumeSize }}
{{ end -}}
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
