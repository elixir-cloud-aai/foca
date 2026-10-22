{{/*
Deployment of the Celery worker, if `worker.enabled`: `worker.replicas` pods
with the app's image, run with `worker.command`, and the app's config
overlay, MongoDB credentials, security contexts and `/tmp`, and the broker
settings of the app (see `foca-common.celeryEnv`). Call with the context
dict.
*/}}
{{- define "foca-common.workerDeployment" -}}
{{- include "foca-common.checkSettings" . -}}
{{- $root := .root -}}
{{- if include "foca-common.workerEnabled" $root -}}
{{- $values := index $root.Values .settings.valuesKey -}}
{{- $worker := $root.Values.worker -}}
{{- $configDir := .settings.configDir | default (printf "/etc/%s" $root.Chart.Name) -}}
{{- $command := $worker.command -}}
{{- if not (and (kindIs "slice" $command) $command) -}}
{{- fail "worker.command must list the command of the Celery worker (e.g., [celery, --app, my_app.worker:celery_app, worker]) if worker.enabled" -}}
{{- end -}}
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ include "foca-common.workerFullname" $root | quote }}
  labels:
    {{- include "foca-common.labels" (dict "root" $root "component" "worker") | nindent 4 }}
spec:
  progressDeadlineSeconds: 600
  replicas: {{ int ($worker.replicas | default 1) }}
  revisionHistoryLimit: 10
  selector:
    matchLabels:
      {{- include "foca-common.selectorLabels" (dict "root" $root "component" "worker") | nindent 6 }}
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
        {{- include "foca-common.selectorLabels" (dict "root" $root "component" "worker") | nindent 8 }}
    spec:
      # The pod doesn't call the Kubernetes API
      automountServiceAccountToken: false
      # No `<SERVICE>_PORT` etc. variables of the Services in the namespace,
      # which FOCA might read as configuration (see the app's Deployment)
      enableServiceLinks: false
      containers:
      - image: {{ include "foca-common.image" . | quote }}
        imagePullPolicy: {{ $values.imagePullPolicy | default "Always" }}
        name: worker
        command:
        {{- toYaml $command | nindent 8 }}
        env:
        - name: FOCA_CONFIG_OVERLAY
          value: {{ printf "%s/overlay.yaml" $configDir }}
        {{- if $root.Values.mongodb.auth.enabled }}
        {{- include "foca-common.mongodbSecretEnv" (dict "root" $root "vars" (dict
          "MONGO_USERNAME" "username"
          "MONGO_PASSWORD" "password"
        )) | trim | nindent 8 }}
        {{- end }}
        {{- include "foca-common.celeryEnv" $root | trim | nindent 8 }}
        resources: {{- toYaml ($worker.resources | default dict) | nindent 10 }}
        securityContext:
          {{- include "foca-common.securityContext" (dict "root" $root "context" $values.securityContext) | nindent 10 }}
        terminationMessagePath: /dev/termination-log
        terminationMessagePolicy: File
        volumeMounts:
        - mountPath: {{ $configDir }}
          name: config
          readOnly: true
        # Writable, as the root file system is read-only by default, up to
        # `worker.tmpSizeLimit`
        - mountPath: /tmp
          name: tmp
      dnsPolicy: ClusterFirst
      restartPolicy: Always
      schedulerName: default-scheduler
      securityContext:
        {{- include "foca-common.securityContext" (dict "root" $root "context" $values.podSecurityContext) | nindent 8 }}
      # Celery finishes running tasks on SIGTERM (warm shutdown) until killed
      terminationGracePeriodSeconds: {{ int (dig "terminationGracePeriodSeconds" 30 $worker) }}
      volumes:
      - configMap:
          name: {{ printf "%s-config" (include "foca-common.fullname" $root) | quote }}
        name: config
      - name: tmp
        {{- include "foca-common.emptyDir" $worker.tmpSizeLimit | nindent 8 }}
{{ end -}}
{{- end -}}
