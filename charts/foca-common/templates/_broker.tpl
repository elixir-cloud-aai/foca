{{- /*
Resources of the bundled message broker of Celery (RabbitMQ), deployed if
`broker.enabled` (see `foca-common.brokerEnabled`); each template renders
nothing otherwise. Call them with the context dict.
*/ -}}

{{/*
Deployment of RabbitMQ: one pod whose user is created from the credentials
in the existing Secret (`broker.existingSecret`), as each new pod starts with
empty data: its data (queued tasks) is kept in an `emptyDir`, so it is lost
when the pod is replaced.
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
        # RabbitMQ's data directory and home (with the Erlang cookie), up to
        # `broker.dataSizeLimit`
        - mountPath: /var/lib/rabbitmq
          name: data
        # Writable, as the root file system is read-only by default
        - mountPath: /tmp
          name: tmp
      dnsPolicy: ClusterFirst
      restartPolicy: Always
      schedulerName: default-scheduler
      securityContext:
        {{- include "foca-common.securityContext" (dict "root" $root "context" $broker.podSecurityContext) | nindent 8 }}
      terminationGracePeriodSeconds: 30
      volumes:
      - name: data
        {{- include "foca-common.emptyDir" $broker.dataSizeLimit | nindent 8 }}
      - name: tmp
        {{- include "foca-common.emptyDir" $broker.tmpSizeLimit | nindent 8 }}
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
