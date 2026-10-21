# Deployment

FOCA apps typically run with Docker Compose during development and on
Kubernetes (e.g., with a Helm chart) in production. Both work best if the
configuration is split into layers, each owned by whoever knows its values:

| Layer | Contains | Owned by | Lives in |
| --- | --- | --- | --- |
| Base configuration | Everything the app needs to run, e.g., API specs, databases, collections and indexes, exception mapping, and defaults for local development | App developers | App repository, baked into the image |
| Overlay | Only what differs in a deployment, e.g., trusted issuers and audiences, public host name, log levels, settings in the `custom` section | Whoever deploys the app | Deployment repository or values file, mounted into the container |
| Secrets | Credentials, e.g., for MongoDB or the Celery broker | Whoever deploys the app | Secret store, passed as environment variables |

The base configuration is complete and runs as is (e.g., with `docker compose
up`), so that developers do not need any extra files. A deployment overrides
only what it needs in an overlay, which FOCA merges on top of the base
configuration on startup. List the overlay in the environment variable
`FOCA_CONFIG_OVERLAY` (see [environment-specific
configuration](configuration/index.md#environment-specific-configuration)).
Secrets never go into configuration files; FOCA reads database and broker
credentials from [environment variables](configuration/index.md#environment-variables).

This way, the configuration of a particular deployment is kept with that
deployment, not in the app repository, and every value has exactly one place
where it is set.

## Running in production

`app.run()` (e.g., in `python app.py`) starts Flask's development server,
which is not meant for production. Serve the app with a WSGI server such as
[Gunicorn](https://gunicorn.org/) instead, via an app factory, i.e., a
function that creates the app:

```python title="wsgi.py"
from pathlib import Path

from foca import Foca


def create_app():
    """Create the app; called once per worker process."""
    config_file = Path(__file__).parent / "config.yaml"
    return Foca(config_file=config_file).create_app()
```

```bash
gunicorn --bind 0.0.0.0:8080 --workers 4 "wsgi:create_app()"
```

The [FOCA image](getting-started.md#docker) does not include a WSGI server;
install one in your app's image (pin its version, e.g., in a requirements
file):

```dockerfile title="Dockerfile"
FROM elixircloud/foca:latest
USER root
RUN pip install --no-cache-dir -r requirements.txt  # e.g., gunicorn==X.Y.Z
USER 1000
COPY ./ /app
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "4", "wsgi:create_app()"]
```

Things to keep in mind:

- **Do not preload the app** (e.g., Gunicorn's `--preload`): MongoDB
  clients must not be shared across forked worker processes, so each worker
  needs to create its own app.
- **Settings `server.host` and `server.port` do not apply** to the WSGI
  server, which binds to the address and port it is given. Keep
  `server.debug` off in production.
- **Limit request body sizes**, either via
  [`server.max_content_length`](configuration/server.md) or in the reverse
  proxy or ingress in front of the app (e.g., `client_max_body_size` in
  NGINX): FOCA does not limit them by default, and bodies may be read before
  requests are authenticated.
- **Do not expose MongoDB publicly.** Only make it reachable by the app
  (e.g., within the Compose or cluster network), and enable authentication
  (cf. [environment variables](configuration/index.md#environment-variables)).
  The example apps' Compose files publish MongoDB on `127.0.0.1` only, for
  local development.

## Kubernetes and Helm

With Helm, let the chart render the overlay from a free-form `config` value
into a ConfigMap, mount it into the app container and point
`FOCA_CONFIG_OVERLAY` to it. A minimal chart could contain:

```yaml title="templates/configmap.yaml"
apiVersion: v1
kind: ConfigMap
metadata:
  name: {{ .Release.Name }}-config
data:
  overlay.yaml: |
    {{- toYaml .Values.config | nindent 4 }}
```

```yaml title="templates/deployment.yaml (excerpt)"
spec:
  template:
    metadata:
      annotations:
        # restart pods when the configuration changes
        checksum/config: {{ include (print $.Template.BasePath "/configmap.yaml") . | sha256sum }}
    spec:
      containers:
        - name: app
          image: {{ .Values.image }}
          env:
            - name: FOCA_CONFIG_OVERLAY
              value: /etc/foca/overlay.yaml
            - name: MONGO_URI
              valueFrom:
                secretKeyRef:
                  name: {{ .Values.mongodb.existingSecret }}
                  key: uri
          volumeMounts:
            - name: config
              mountPath: /etc/foca
              readOnly: true
      volumes:
        - name: config
          configMap:
            name: {{ .Release.Name }}-config
```

```yaml title="values.yaml"
image: example/my-app:1.0.0
# merged into the app's base configuration
config: {}
mongodb:
  # Secret with key `uri`, e.g., mongodb://user:password@mongodb:27017/
  existingSecret: my-app-mongodb
```

Each deployment then keeps its own values file, e.g., in its infrastructure
repository:

```yaml title="values-prod.yaml"
image: example/my-app:1.2.0
config:
  security:
    auth:
      issuers:
        - https://login.example.org/oidc/
      audience:
        - my-app-client-id
  log:
    root:
      level: 30
  custom:
    public_url: https://my-app.example.org/
```

and applies changes with:

```bash
helm upgrade --install my-app ./chart -f values-prod.yaml --wait
```

FOCA reads its configuration on startup only. The `checksum/config`
annotation makes `helm upgrade` restart the pods whenever the rendered
configuration changes, so new values take effect without further steps. With
`--wait`, Helm waits until the new pods are ready and reports an error if they
are not, e.g., because FOCA rejects an invalid overlay; give the app container
a readiness probe for this to work.

Things to keep in mind:

- **Change configuration through Helm values only**, not with `kubectl edit`
  on the ConfigMap or in the image: Helm overwrites manual changes on the next
  upgrade.
- **Mount the ConfigMap as a directory**, not with `subPath`: files mounted
  with `subPath` are not updated, and replacing the base configuration file
  hides all of its other settings.
- **Lists are replaced, not merged.** An overlay that sets, e.g.,
  `security.auth.issuers` or `security.access_control.policies` must contain
  the complete list.
- **Check key paths.** Unknown keys in FOCA's sections are rejected, but other
  top-level sections are accepted as they are, so a misplaced key (e.g.,
  `endpoints` instead of `custom.endpoints`) is silently ignored. Validate
  app-specific settings with a [custom configuration model](configuration/custom.md).
- **Relative paths** in an overlay are resolved against the directory of the
  base configuration (see [relative paths](configuration/index.md#relative-paths)),
  i.e., they refer to files in the image.
- **Missing overlays fail fast:** FOCA raises an error on startup if a file
  listed in `FOCA_CONFIG_OVERLAY` does not exist.
- **Rolling updates mix configurations:** while new pods replace old ones,
  requests are served by pods with the old and the new configuration. Keep
  configuration changes backward compatible, or use the `Recreate` deployment
  strategy if that is not acceptable.
- **Secrets are read on startup, too:** after rotating credentials in a
  Secret, restart the pods (e.g., `kubectl rollout restart deployment/my-app`),
  or reference a version of the Secret in the pod template, so that a change
  restarts them.
- The [FOCA image](getting-started.md#docker) runs as a non-root user that
  also works with arbitrary user IDs, e.g., on OpenShift.

## Docker Compose

For local development, `docker compose up` uses the base configuration as is.
To try other settings without changing the repository, put them into a
`config.local.yaml` and mount it with an override file, which Docker Compose
picks up automatically if it is named after the Compose file, e.g.,
`docker-compose.override.yaml` for `docker-compose.yaml` (add both files to
`.gitignore`; replace `app` with the name of your app's service):

```yaml title="docker-compose.override.yaml"
services:
  app:
    environment:
      FOCA_CONFIG_OVERLAY: /overlay/config.local.yaml
    volumes:
      - ./config.local.yaml:/overlay/config.local.yaml:ro
```

This is the same mechanism as on Kubernetes, so overlays can be tested
locally before they are deployed.
