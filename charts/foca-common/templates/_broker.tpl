{{- /*
Resources of the bundled message broker of Celery (RabbitMQ), deployed if
`broker.enabled` (see `foca-common.brokerEnabled`); each template renders
nothing otherwise. Call them with the context dict.
*/ -}}

{{/*
Deployment of RabbitMQ: one pod, whose data (queued tasks) is kept in a
PersistentVolumeClaim if `broker.persistence.enabled` (see
`foca-common.brokerPersistence`), otherwise in an `emptyDir`, which is lost
when the pod is replaced. RabbitMQ creates its user from the credentials in
the existing Secret (`broker.existingSecret`) when it starts with empty
data, i.e., only once with persistence. The node name is fixed, as RabbitMQ
keeps its data in a directory named after it (by default after the pod's
host name, which changes with every pod).
*/}}
{{- define "foca-common.brokerDeployment" -}}
{{- include "foca-common.checkSettings" . -}}
{{- $root := .root -}}
{{- if include "foca-common.brokerEnabled" $root -}}
{{- $broker := $root.Values.broker -}}
{{- $secret := include "foca-common.brokerSecret" $root -}}
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ include "foca-common.brokerFullname" $root | quote }}
  labels:
    {{- include "foca-common.labels" (dict "root" $root "component" "broker") | nindent 4 }}
spec:
  progressDeadlineSeconds: 600
  replicas: 1
  revisionHistoryLimit: 10
  selector:
    matchLabels:
      {{- include "foca-common.selectorLabels" (dict "root" $root "component" "broker") | nindent 6 }}
  # One broker at a time, so that tasks are not queued on a broker that is
  # being replaced
  strategy:
    type: Recreate
  template:
    metadata:
      annotations:
        # Restart RabbitMQ when its config changes
        checksum/config: {{ include "foca-common.brokerConfigMap" . | sha256sum }}
      labels:
        {{- include "foca-common.selectorLabels" (dict "root" $root "component" "broker") | nindent 8 }}
    spec:
      # The pod doesn't call the Kubernetes API
      automountServiceAccountToken: false
      # No `<SERVICE>_PORT` etc. variables of the Services in the namespace
      # (RabbitMQ reads `RABBITMQ_*` variables as configuration)
      enableServiceLinks: false
      containers:
      - image: {{ required "broker.image must be set" $broker.image }}
        imagePullPolicy: {{ $broker.imagePullPolicy | default "Always" }}
        name: broker
        env:
        # Logs to standard output only, as the root file system is read-only
        - name: RABBITMQ_LOGS
          value: "-"
        # Fixed, as RabbitMQ keeps its data in a directory named after it
        - name: RABBITMQ_NODENAME
          value: rabbit@localhost
        - name: RABBITMQ_DEFAULT_USER
          valueFrom:
            secretKeyRef:
              name: {{ $secret | quote }}
              key: username
        - name: RABBITMQ_DEFAULT_PASS
          valueFrom:
            secretKeyRef:
              name: {{ $secret | quote }}
              key: password
        # TCP checks are cheaper than starting `rabbitmq-diagnostics`;
        # RabbitMQ accepts AMQP connections once it has booted
        startupProbe:
          tcpSocket:
            port: 5672
          periodSeconds: 10
          failureThreshold: 30
        readinessProbe:
          tcpSocket:
            port: 5672
          periodSeconds: 10
        livenessProbe:
          tcpSocket:
            port: 5672
          periodSeconds: 20
        ports:
        - name: amqp
          containerPort: 5672
          protocol: TCP
        resources: {{- toYaml ($broker.resources | default dict) | nindent 10 }}
        securityContext:
          {{- include "foca-common.securityContext" (dict "root" $root "context" $broker.securityContext) | nindent 10 }}
        terminationMessagePath: /dev/termination-log
        terminationMessagePolicy: File
        volumeMounts:
        # RabbitMQ's data directory and home (with the Erlang cookie)
        - mountPath: /var/lib/rabbitmq
          name: data
        # Writable, as the root file system is read-only by default
        - mountPath: /tmp
          name: tmp
        # Added to the image's config files
        - mountPath: /etc/rabbitmq/conf.d/90-foca.conf
          name: config
          subPath: 90-foca.conf
          readOnly: true
      dnsPolicy: ClusterFirst
      restartPolicy: Always
      schedulerName: default-scheduler
      securityContext:
        {{- include "foca-common.securityContext" (dict "root" $root "context" $broker.podSecurityContext) | nindent 8 }}
      terminationGracePeriodSeconds: 30
      volumes:
      - name: data
        {{- with include "foca-common.brokerPersistence" $root | fromJson }}
        persistentVolumeClaim:
          claimName: {{ .claimName | quote }}
        {{- else }}
        {{- include "foca-common.emptyDir" $broker.dataSizeLimit | nindent 8 }}
        {{- end }}
      - name: tmp
        {{- include "foca-common.emptyDir" $broker.tmpSizeLimit | nindent 8 }}
      - name: config
        configMap:
          name: {{ printf "%s-config" (include "foca-common.brokerFullname" $root) | quote }}
{{ end -}}
{{- end -}}

{{/*
Persistence of the broker's data, as JSON: a dict with the name of the
PersistentVolumeClaim (`claimName`; `broker.persistence.existingClaim`, or
the broker's own claim) and whether the chart creates it (`create`), if
`broker.persistence.enabled`; otherwise an empty dict. Without a
`broker.persistence` map (values written for foca-common 0.1.0), persistence
is disabled. Call with the root context.
*/}}
{{- define "foca-common.brokerPersistence" -}}
{{- $persistence := dig "persistence" dict (.Values.broker | default dict) | default dict -}}
{{- if $persistence.enabled -}}
{{- $existing := $persistence.existingClaim | default "" -}}
{{- dict "claimName" ($existing | default (include "foca-common.brokerFullname" .)) "create" (not $existing) | toJson -}}
{{- else -}}
{{- dict | toJson -}}
{{- end -}}
{{- end -}}

{{/*
PersistentVolumeClaim of the broker's data, if `broker.persistence.enabled`
without `broker.persistence.existingClaim`: of `size`, with `accessModes`
(by default `ReadWriteOnce`) and `storageClass` (the cluster's default
class if empty; "-" for none, i.e., no dynamic provisioning).
*/}}
{{- define "foca-common.brokerPersistentVolumeClaim" -}}
{{- include "foca-common.checkSettings" . -}}
{{- $root := .root -}}
{{- if include "foca-common.brokerEnabled" $root -}}
{{- if (include "foca-common.brokerPersistence" $root | fromJson).create -}}
{{- $persistence := $root.Values.broker.persistence -}}
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: {{ include "foca-common.brokerFullname" $root | quote }}
  labels:
    {{- include "foca-common.labels" (dict "root" $root "component" "broker") | nindent 4 }}
spec:
  accessModes:
    {{- toYaml ($persistence.accessModes | default (list "ReadWriteOnce")) | nindent 2 }}
  resources:
    requests:
      storage: {{ required "broker.persistence.size must be set if broker.persistence.enabled" $persistence.size | quote }}
  {{- with $persistence.storageClass }}
  storageClassName: {{ ternary "" . (eq . "-") | quote }}
  {{- end }}
{{ end -}}
{{- end -}}
{{- end -}}

{{/*
ConfigMap with RabbitMQ's config file for Celery (`90-foca.conf`), mounted by
its Deployment, whose pod is restarted when it changes. Celery declares
transient non-exclusive queues (e.g., for the `rpc://` result backend, FOCA's
default, and for remote control of the workers), a feature that RabbitMQ
deprecated and denies by default from 4.3 on, so it is permitted here.
*/}}
{{- define "foca-common.brokerConfigMap" -}}
{{- include "foca-common.checkSettings" . -}}
{{- $root := .root -}}
{{- if include "foca-common.brokerEnabled" $root -}}
apiVersion: v1
kind: ConfigMap
metadata:
  name: {{ printf "%s-config" (include "foca-common.brokerFullname" $root) | quote }}
  labels:
    {{- include "foca-common.labels" (dict "root" $root "component" "broker") | nindent 4 }}
data:
  90-foca.conf: |
    # Celery declares transient non-exclusive queues (e.g., for the rpc://
    # result backend and remote control), which RabbitMQ 4.3 denies by default
    deprecated_features.permit.transient_nonexcl_queues = true
{{ end -}}
{{- end -}}

{{/*
NetworkPolicy of RabbitMQ, if `broker.networkPolicy.enabled`: only the app's
and the worker's pods of this release may connect to RabbitMQ; connections
from the pod's node (the kubelet's probes) are always allowed. Egress is not
restricted.
*/}}
{{- define "foca-common.brokerNetworkPolicy" -}}
{{- include "foca-common.checkSettings" . -}}
{{- $root := .root -}}
{{- if and (include "foca-common.brokerEnabled" $root) (dig "networkPolicy" "enabled" false $root.Values.broker) -}}
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: {{ include "foca-common.brokerFullname" $root | quote }}
  labels:
    {{- include "foca-common.labels" (dict "root" $root "component" "broker") | nindent 4 }}
spec:
  podSelector:
    matchLabels:
      {{- include "foca-common.selectorLabels" (dict "root" $root "component" "broker") | nindent 6 }}
  policyTypes:
  - Ingress
  ingress:
  - from:
    {{- range $component := list "app" "worker" }}
    - podSelector:
        matchLabels:
          {{- include "foca-common.selectorLabels" (dict "root" $root "component" $component) | nindent 10 }}
    {{- end }}
    ports:
    - port: 5672
      protocol: TCP
{{ end -}}
{{- end -}}

{{/*
Service of RabbitMQ (port 5672), the host of the broker URL
(`broker-url` of `broker.existingSecret`).
*/}}
{{- define "foca-common.brokerService" -}}
{{- include "foca-common.checkSettings" . -}}
{{- $root := .root -}}
{{- if include "foca-common.brokerEnabled" $root -}}
apiVersion: v1
kind: Service
metadata:
  name: {{ include "foca-common.brokerFullname" $root | quote }}
  labels:
    {{- include "foca-common.labels" (dict "root" $root "component" "broker") | nindent 4 }}
spec:
  ports:
  - name: amqp
    port: 5672
    protocol: TCP
    targetPort: amqp
  selector:
    {{- include "foca-common.selectorLabels" (dict "root" $root "component" "broker") | nindent 4 }}
  sessionAffinity: None
  type: ClusterIP
{{ end -}}
{{- end -}}
