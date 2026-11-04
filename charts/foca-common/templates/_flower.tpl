{{/*
Whether Flower is deployed ("true") or not (empty): `flower.enabled`, false if not
set (also without a `flower` map). Call with the root context.
*/}}
{{- define "foca-common.flowerEnabled" -}}
{{- if dig "enabled" false (.Values.flower | default dict) -}}
true
{{- end -}}
{{- end -}}

{{/*
Name of Flower's resources; truncated like `foca-common.mongodbFullname`.
*/}}
{{- define "foca-common.flowerFullname" -}}
{{- printf "%s-flower" (include "foca-common.fullname" . | trunc 56 | trimSuffix "-") -}}
{{- end -}}

{{/*
Fails unless Flower, if enabled, has an image, its credentials
(`flower.existingSecret`) and the broker's (`broker.existingSecret`), and a name of
its own; renders nothing. Call with the root context.
*/}}
{{- define "foca-common.checkFlower" -}}
{{- if include "foca-common.flowerEnabled" . -}}
{{- if not .Values.flower.image -}}
{{- fail "flower.image must be set if flower.enabled" -}}
{{- end -}}
{{- if not .Values.flower.existingSecret -}}
{{- fail "flower.existingSecret must be set if flower.enabled: a Secret with the key basic-auth (<user>:<password>), as Flower shows the arguments of tasks" -}}
{{- end -}}
{{- if not (dig "existingSecret" "" (.Values.broker | default dict)) -}}
{{- fail "broker.existingSecret must be set if flower.enabled: Flower connects to the broker with its URL (broker-url)" -}}
{{- end -}}
{{- if eq (include "foca-common.fullname" .) (include "foca-common.flowerFullname" .) -}}
{{- fail (printf "the app and Flower resources would both be named %s: choose a fullnameOverride that does not end in -flower" (include "foca-common.fullname" .)) -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/*
Deployment of Flower, the monitoring UI of the Celery worker, if `flower.enabled`: one
pod, connected to the broker with the URL of `broker.existingSecret` (key
`broker-url`), with basic authentication from `flower.existingSecret` (key
`basic-auth`, `<user>:<password>`), as it shows the arguments of tasks. It is not
exposed outside the cluster. Without `flower.podSecurityContext` and
`flower.securityContext`, it runs unprivileged. Also checks Flower's values, in this
template only, as apps may have their own `flower` values without it. Call with the
context dict.
*/}}
{{- define "foca-common.flowerDeployment" -}}
{{- include "foca-common.checkSettings" . -}}
{{- $root := .root -}}
{{- if include "foca-common.flowerEnabled" $root -}}
{{- include "foca-common.checkFlower" $root -}}
{{- $flower := $root.Values.flower -}}
{{- /* The image runs as user `flower` (1000) */ -}}
{{- $podSecurityContext := $flower.podSecurityContext | default (dict
  "runAsNonRoot" true
  "runAsUser" 1000
  "runAsGroup" 1000
  "seccompProfile" (dict "type" "RuntimeDefault")
) -}}
{{- $securityContext := $flower.securityContext | default (dict
  "allowPrivilegeEscalation" false
  "capabilities" (dict "drop" (list "ALL"))
  "readOnlyRootFilesystem" true
) -}}
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ include "foca-common.flowerFullname" $root | quote }}
  labels:
    {{- include "foca-common.labels" (dict "root" $root "component" "flower") | nindent 4 }}
spec:
  replicas: 1
  revisionHistoryLimit: 10
  selector:
    matchLabels:
      {{- include "foca-common.selectorLabels" (dict "root" $root "component" "flower") | nindent 6 }}
  template:
    metadata:
      {{- with include "foca-common.rolloutChecksum" . }}
      annotations:
        # Roll the pods when the Secrets in `rolloutOnSecrets` change
        checksum/secrets: {{ . }}
      {{- end }}
      labels:
        {{- include "foca-common.selectorLabels" (dict "root" $root "component" "flower") | nindent 8 }}
    spec:
      # The pod doesn't call the Kubernetes API
      automountServiceAccountToken: false
      # No `<SERVICE>_PORT` etc. variables of the Services in the namespace,
      # which Flower might read as options (`FLOWER_PORT` of a Service
      # `flower`)
      enableServiceLinks: false
      containers:
      - name: flower
        image: {{ $flower.image | quote }}
        imagePullPolicy: {{ $flower.imagePullPolicy | default "Always" }}
        command: ["celery", "flower", "--port=5555"]
        env:
        - name: CELERY_BROKER_URL
          valueFrom:
            secretKeyRef:
              name: {{ $root.Values.broker.existingSecret | quote }}
              key: broker-url
        - name: FLOWER_BASIC_AUTH
          valueFrom:
            secretKeyRef:
              name: {{ $flower.existingSecret | quote }}
              key: basic-auth
        ports:
        - name: http
          containerPort: 5555
          protocol: TCP
        # The UI requires authentication, so the probes only connect
        readinessProbe:
          tcpSocket:
            port: 5555
          periodSeconds: 10
          timeoutSeconds: 5
          failureThreshold: 3
        livenessProbe:
          tcpSocket:
            port: 5555
          initialDelaySeconds: 30
          periodSeconds: 20
          timeoutSeconds: 5
          failureThreshold: 3
        resources: {{- toYaml ($flower.resources | default dict) | nindent 10 }}
        securityContext:
          {{- include "foca-common.securityContext" (dict "root" $root "context" $securityContext) | nindent 10 }}
        volumeMounts:
        # Writable, as the root file system is read-only by default
        - mountPath: /tmp
          name: tmp
      securityContext:
        {{- include "foca-common.securityContext" (dict "root" $root "context" $podSecurityContext) | nindent 8 }}
      volumes:
      - name: tmp
        {{- include "foca-common.emptyDir" $flower.tmpSizeLimit | nindent 8 }}
{{ end -}}
{{- end -}}

{{/*
Service of Flower (port 5555), if `flower.enabled`; within the cluster only. Call with
the context dict.
*/}}
{{- define "foca-common.flowerService" -}}
{{- include "foca-common.checkSettings" . -}}
{{- $root := .root -}}
{{- if include "foca-common.flowerEnabled" $root -}}
apiVersion: v1
kind: Service
metadata:
  name: {{ include "foca-common.flowerFullname" $root | quote }}
  labels:
    {{- include "foca-common.labels" (dict "root" $root "component" "flower") | nindent 4 }}
spec:
  ports:
  - name: http
    port: 5555
    protocol: TCP
    targetPort: http
  selector:
    {{- include "foca-common.selectorLabels" (dict "root" $root "component" "flower") | nindent 4 }}
  type: ClusterIP
{{ end -}}
{{- end -}}

{{/*
NetworkPolicy of Flower, if `flower.enabled`, unless `flower.networkPolicy.enabled` is
false: only the peers in `flower.networkPolicy.ingressFrom` may connect to Flower (none by default,
so it is reached via `kubectl port-forward` only). Connections from the pods' node (the
kubelet's probes) are always allowed. Call with the context dict.
*/}}
{{- define "foca-common.flowerNetworkPolicy" -}}
{{- include "foca-common.checkSettings" . -}}
{{- $root := .root -}}
{{- $flower := $root.Values.flower | default dict -}}
{{- if and (include "foca-common.flowerEnabled" $root) (dig "networkPolicy" "enabled" true $flower) -}}
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: {{ include "foca-common.flowerFullname" $root | quote }}
  labels:
    {{- include "foca-common.labels" (dict "root" $root "component" "flower") | nindent 4 }}
spec:
  podSelector:
    matchLabels:
      {{- include "foca-common.selectorLabels" (dict "root" $root "component" "flower") | nindent 6 }}
  policyTypes:
  - Ingress
  {{- with dig "networkPolicy" "ingressFrom" list $flower }}
  ingress:
  - from:
      {{- toYaml . | nindent 4 }}
    ports:
    - port: 5555
      protocol: TCP
  {{- else }}
  ingress: []
  {{- end }}
{{ end -}}
{{- end -}}

{{/*
NetworkPolicy allowing Flower's pods to connect to the bundled broker, if
`flower.enabled`, `broker.enabled` and `broker.networkPolicy.enabled`, in addition to
the app's and the worker's (NetworkPolicies add up). Call with the context dict.
*/}}
{{- define "foca-common.flowerBrokerNetworkPolicy" -}}
{{- include "foca-common.checkSettings" . -}}
{{- $root := .root -}}
{{- if and (include "foca-common.flowerEnabled" $root) (include "foca-common.brokerEnabled" $root) (dig "networkPolicy" "enabled" false ($root.Values.broker | default dict)) -}}
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: {{ printf "%s-from-flower" (include "foca-common.brokerFullname" $root | trunc 51 | trimSuffix "-") | quote }}
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
    - podSelector:
        matchLabels:
          {{- include "foca-common.selectorLabels" (dict "root" $root "component" "flower") | nindent 10 }}
    ports:
    - port: 5672
      protocol: TCP
{{ end -}}
{{- end -}}
