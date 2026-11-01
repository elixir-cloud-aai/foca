{{/*
Deployment of the app: `<valuesKey>.replicas` pods (1 if not set) with the
app's container (port 8080, `http`), the config overlay mounted at
`configDir` (see `foca-common.checkSettings`), MongoDB credentials from the
existing Secret if authentication is enabled, and a writable `emptyDir` at
`/tmp`. Call with the context dict.
*/}}
{{- define "foca-common.deployment" -}}
{{- include "foca-common.checkSettings" . -}}
{{- $root := .root -}}
{{- $values := index $root.Values .settings.valuesKey -}}
{{- $configDir := .settings.configDir | default (printf "/etc/%s" $root.Chart.Name) -}}
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ include "foca-common.fullname" $root | quote }}
  labels:
    {{- include "foca-common.labels" (dict "root" $root "component" "app") | nindent 4 }}
spec:
  progressDeadlineSeconds: 600
  replicas: {{ include "foca-common.replicas" . }}
  revisionHistoryLimit: 10
  selector:
    matchLabels:
      {{- include "foca-common.selectorLabels" (dict "root" $root "component" "app") | nindent 6 }}
  strategy:
    rollingUpdate:
      maxSurge: 25%
      maxUnavailable: 25%
    type: RollingUpdate
  template:
    metadata:
      annotations:
        # Roll the pods when the config changes
        checksum/config: {{ include "foca-common.configMap" . | sha256sum }}
      labels:
        {{- include "foca-common.selectorLabels" (dict "root" $root "component" "app") | nindent 8 }}
    spec:
      # The pod doesn't call the Kubernetes API
      automountServiceAccountToken: false
      # No `<SERVICE>_PORT` etc. variables of the Services in the namespace:
      # FOCA reads some as configuration (e.g., `MONGO_PORT` of a Service
      # `mongo`, which isn't a port)
      enableServiceLinks: false
      containers:
      - image: {{ include "foca-common.image" . | quote }}
        imagePullPolicy: {{ $values.imagePullPolicy | default "Always" }}
        name: {{ .settings.containerName | default $root.Chart.Name }}
        env:
        - name: FOCA_CONFIG_OVERLAY
          value: {{ printf "%s/overlay.yaml" $configDir }}
        {{- if $values.gunicorn }}
        # Settings of Gunicorn (the image's command), overriding the image's
        # defaults; options are split like shell words, so `extraArgs` are
        # quoted
        - name: GUNICORN_CMD_ARGS
          value: {{ include "foca-common.gunicornArgs" . | quote }}
        {{- end }}
        {{- if $root.Values.mongodb.auth.enabled }}
        {{- include "foca-common.mongodbSecretEnv" (dict "root" $root "vars" (dict
          "MONGO_USERNAME" "username"
          "MONGO_PASSWORD" "password"
        )) | trim | nindent 8 }}
        {{- end }}
        {{- with include "foca-common.celeryEnv" $root | trim }}
        {{- . | nindent 8 }}
        {{- end }}
        ports:
        - name: http
          containerPort: 8080
          protocol: TCP
        # Probes request `{{ .settings.valuesKey }}.probePath`, e.g., the Swagger UI
        # (no authentication, no database access)
        # Up to 5 minutes are allowed for the start before liveness checks
        startupProbe:
          httpGet:
            path: {{ $values.probePath | quote }}
            port: 8080
          periodSeconds: 10
          timeoutSeconds: 5
          failureThreshold: 30
        readinessProbe:
          httpGet:
            path: {{ $values.probePath | quote }}
            port: 8080
          periodSeconds: 10
          timeoutSeconds: 5
          failureThreshold: 3
        livenessProbe:
          httpGet:
            path: {{ $values.probePath | quote }}
            port: 8080
          periodSeconds: 20
          timeoutSeconds: 5
          failureThreshold: 3
        {{- with int ($values.preStopSleepSeconds | default 0) }}
        # Keep serving until the ingress controller and Service have removed
        # the pod from their endpoints, as Gunicorn stops accepting
        # connections on SIGTERM (Python, as the image need not have `sleep`)
        lifecycle:
          preStop:
            exec:
              command: ["python", "-c", {{ printf "import time; time.sleep(%d)" . | quote }}]
        {{- end }}
        resources: {{- toYaml ($values.resources | default dict) | nindent 10 }}
        securityContext:
          {{- include "foca-common.securityContext" (dict "root" $root "context" $values.securityContext) | nindent 10 }}
        terminationMessagePath: /dev/termination-log
        terminationMessagePolicy: File
        volumeMounts:
        - mountPath: {{ $configDir }}
          name: config
          readOnly: true
        # Writable, as the root file system is read-only by default (e.g., for
        # temporary files of uploads), up to `{{ .settings.valuesKey }}.tmpSizeLimit`
        - mountPath: /tmp
          name: tmp
      dnsPolicy: ClusterFirst
      restartPolicy: Always
      schedulerName: default-scheduler
      securityContext:
        {{- include "foca-common.securityContext" (dict "root" $root "context" $values.podSecurityContext) | nindent 8 }}
      terminationGracePeriodSeconds: {{ include "foca-common.terminationGracePeriod" . }}
      volumes:
      - configMap:
          name: {{ printf "%s-config" (include "foca-common.fullname" $root) | quote }}
        name: config
      - name: tmp
        {{- include "foca-common.emptyDir" $values.tmpSizeLimit | nindent 8 }}
{{ end -}}
