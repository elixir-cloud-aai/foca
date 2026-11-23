{{/*
Ingress for `hostName`, if `<valuesKey>.ingress.enabled`, for any ingress
controller: the chart sets no controller-specific annotations (e.g., for
body sizes or rate limits), which `ingress.annotations` add. These take
precedence over the annotations requesting a certificate from cert-manager
(`ingress.https`: the `issuer` of kind `issuerKind`, a ClusterIssuer by
default or a namespaced Issuer); `null` removes them. Fails if
`ingress.https.issuerKind` is set to another kind (`null` means the
default), if the annotations name both a ClusterIssuer and an Issuer, and
if ingress-nginx's size limit of request bodies is below the app's (see
`foca-common.checkProxyBodySize`). Call with the context dict.
*/}}
{{- define "foca-common.ingress" -}}
{{- include "foca-common.checkSettings" . -}}
{{- $root := .root -}}
{{- $key := .settings.valuesKey -}}
{{- $ingress := (index $root.Values $key).ingress | default dict -}}
{{- if $ingress.enabled -}}
{{- $hostName := required (printf "hostName must be set if %s.ingress.enabled" $key) $root.Values.hostName -}}
{{- $https := $ingress.https | default dict -}}
{{- $annotations := dict -}}
{{- $issuerKind := "ClusterIssuer" -}}
{{- if not (kindIs "invalid" $https.issuerKind) -}}
{{- $issuerKind = $https.issuerKind -}}
{{- if not (has $issuerKind (list "ClusterIssuer" "Issuer")) -}}
{{- fail (printf "%s.ingress.https.issuerKind must be ClusterIssuer or Issuer, not %q" $key (toString $issuerKind)) -}}
{{- end -}}
{{- end -}}
{{- if $https.enabled -}}
{{- $issuerAnnotation := ternary "cert-manager.io/issuer" "cert-manager.io/cluster-issuer" (eq $issuerKind "Issuer") -}}
{{- $_ := set $annotations $issuerAnnotation (required (printf "%s.ingress.https.issuer must be set if %s.ingress.https.enabled" $key $key) $https.issuer) -}}
{{- $_ := set $annotations "kubernetes.io/tls-acme" "true" -}}
{{- end -}}
{{- range $name, $value := $ingress.annotations | default dict -}}
{{- if kindIs "invalid" $value -}}
{{- $_ := unset $annotations $name -}}
{{- else -}}
{{- $_ := set $annotations $name $value -}}
{{- end -}}
{{- end -}}
{{- if and (hasKey $annotations "cert-manager.io/issuer") (hasKey $annotations "cert-manager.io/cluster-issuer") -}}
{{- fail (printf "%s.ingress: the Ingress must not have both annotations cert-manager.io/issuer and cert-manager.io/cluster-issuer, which cert-manager refuses: set %s.ingress.https.issuerKind instead, or remove one with null in %s.ingress.annotations" $key $key $key) -}}
{{- end -}}
{{- include "foca-common.checkProxyBodySize" (dict "root" $root "settings" .settings "annotations" $annotations) }}
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  {{- with $annotations }}
  annotations:
    {{- toYaml . | nindent 4 }}
  {{- end }}
  name: {{ include "foca-common.fullname" $root | quote }}
  labels:
    {{- include "foca-common.labels" (dict "root" $root "component" "app") | nindent 4 }}
spec:
  {{- with $ingress.className }}
  ingressClassName: {{ . | quote }}
  {{- end }}
  rules:
  - host: {{ $hostName | quote }}
    http:
      paths:
      - path: /
        pathType: Prefix
        backend:
          service:
            name: {{ include "foca-common.fullname" $root | quote }}
            port:
              name: http
{{- if $https.enabled }}
  tls:
  - hosts:
    - {{ $hostName | quote }}
    secretName: {{ printf "%s-ingress-secret" (include "foca-common.fullname" $root) | quote }}
{{- end }}
{{ end -}}
{{- end -}}

{{/*
Fails if ingress-nginx's size limit of request bodies, the annotation
`nginx.ingress.kubernetes.io/proxy-body-size` of the Ingress (in
`annotations`: `<valuesKey>.ingress.annotations` merged with the chart's),
is below the app's limit (`config.server.max_content_length`, see
`foca-common.maxContentLength`): ingress-nginx would reject the bodies in
between with its own `413` page instead of the app's problem details. Sizes
are those of nginx: bytes, or with the suffix `k`, `m` or `g` (either case),
here with at most 9 digits and no leading zeros (so that they are parsed as
decimals and do not overflow); `0` disables the limit. Other values fail,
also without an app limit, which only skips the comparison. Checked only if the
Ingress has the annotation, so the chart stays neutral to ingress
controllers and sets no default: other controllers ignore the annotation,
and without it, the chart can't know the controller's limit (app charts
whose limit exceeds ingress-nginx's default, 1 MiB, set the annotation in
their values). Call with the context dict plus `annotations`; renders
nothing.
*/}}
{{- define "foca-common.checkProxyBodySize" -}}
{{- $key := .settings.valuesKey -}}
{{- $name := "nginx.ingress.kubernetes.io/proxy-body-size" -}}
{{- if hasKey .annotations $name -}}
{{- $size := toString (index .annotations $name) -}}
{{- if not (regexMatch "^(0|[1-9][0-9]{0,8})[kKmMgG]?$" $size) -}}
{{- fail (printf "%s.ingress.annotations: %s must be a size of ingress-nginx (at most 9 digits without leading zeros, bytes or with the suffix k, m or g, e.g., 10m; \"0\" for no limit), not %q" $key $name $size) -}}
{{- end -}}
{{- $number := regexFind "^[0-9]+" $size -}}
{{- $factor := get (dict "" 1 "k" 1024 "m" 1048576 "g" 1073741824) (lower (trimPrefix $number $size)) -}}
{{- $bytes := mul (int64 $number) $factor -}}
{{- $limit := include "foca-common.maxContentLength" . -}}
{{- if and $limit (ne $bytes 0) (lt $bytes (int64 $limit)) -}}
{{- fail (printf "%s.ingress.annotations: %s (%s) must allow at least the app's config.server.max_content_length (%s bytes), or ingress-nginx rejects larger request bodies with its own 413 page: set it to %dm or more (\"0\" for no limit)" $key $name $size $limit (div (add (int64 $limit) 1048575) 1048576)) -}}
{{- end -}}
{{- end -}}
{{- end -}}
