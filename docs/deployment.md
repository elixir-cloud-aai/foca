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

FOCA comes with default Gunicorn settings for containers, in the module
`foca.gunicorn_config` (a [Gunicorn config
file](https://gunicorn.org/reference/settings/#config) as a Python module).
Pass it with `--config`:

```bash
gunicorn --config python:foca.gunicorn_config "wsgi:create_app()"
```

The [FOCA image](getting-started.md#docker) does not include a WSGI server,
and FOCA does not depend on Gunicorn; install it in your app's image, e.g.,
from a requirements file that pins its version (`gunicorn==X.Y.Z`; the
settings are tested with Gunicorn 26). The image's working directory is
`/app`:

```dockerfile title="Dockerfile"
FROM elixircloud/foca:latest
USER root
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
USER 1000
COPY ./ /app
CMD ["gunicorn", "--config", "python:foca.gunicorn_config", "wsgi:create_app()"]
```

The settings, and why they are set:

| Setting | Value | Why |
| --- | --- | --- |
| `bind` | `["0.0.0.0:8080"]` | The port of the [Helm chart](helm-chart.md)'s Service and probes; `server.host` and `server.port` apply to the development server only |
| `worker_class`, `workers`, `threads` | `"gthread"`, `1`, `4` | One process with a few threads, as each worker process loads the whole app (e.g., within the chart's memory limit of 128 MiB) |
| `timeout` | `30` | Workers whose main loop is blocked for 30 seconds are restarted; requests run in threads, so slow requests do not count |
| `graceful_timeout` | `20` | Seconds that workers get to finish requests on shutdown (e.g., on `SIGTERM` from Kubernetes); keep it below the pod's termination grace period (30 seconds by default), which the chart derives from it |
| `keepalive` | `100` | Seconds after which idle keep-alive connections are closed; longer than the idle timeouts of proxies in front of the app (60 seconds for ingress-nginx, 90 seconds for Traefik), which would otherwise reuse connections just as Gunicorn closes them (failing requests with `502`) |
| `limit_request_line`, `limit_request_fields`, `limit_request_field_size` | `4094`, `100`, `8190` | Size limits of the request line and headers (Gunicorn's defaults, set explicitly); the size of bodies is limited by FOCA (`server.max_content_length`, see below) |
| `accesslog`, `errorlog` | `None`, `"-"` | No access log, as apps log requests themselves if needed (FOCA logs those of controllers decorated with [`log_traffic`](utilities.md#logging-requests-and-responses)); for a log of all requests, add `--access-logfile -`. Errors go to stderr, like the app's logs |
| `control_socket_disable` | `True` | No control socket (`gunicornc`, Gunicorn 25.1 or later), which would be created in `$XDG_RUNTIME_DIR` or `$HOME/.gunicorn`, i.e., on the possibly read-only root file system (as in the chart), and log an error at startup if that is not writable |
| `worker_tmp_dir` | `/dev/shm` if it is a writable directory, else Gunicorn's default | Workers update their heartbeat files often, so they are kept in memory if possible; otherwise in the temporary directory (`TMPDIR` or `/tmp`) |

Deliberately not set:

- **`preload_app`:** do not preload the app (e.g., with Gunicorn's
  `--preload`): MongoDB clients must not be shared across forked worker
  processes, so each worker needs to create its own app. Each worker also
  runs FOCA's startup checks; if they fail, the worker fails to boot and
  Gunicorn exits.
- **`forwarded_allow_ips`:** which proxies to trust with `X-Forwarded-*`
  headers depends on the deployment, so Gunicorn's default applies.

To change settings, either:

- set `GUNICORN_CMD_ARGS` (e.g., `--workers 2 --threads 8`), which takes
  precedence over the module. The [Helm chart](helm-chart.md#deployment-notes)
  sets it from the app chart's `gunicorn` values (workers, threads, timeouts
  and keep-alive), which then apply instead of the module's; e.g., the
  petstore chart's keep-alive is 75 seconds (raise it behind Traefik, see
  [Traefik](helm-chart.md#traefik)); or
- for other defaults of your app, write a config module of your own that
  imports FOCA's and overrides some names, and pass that one with
  `--config`:

    ```python title="myapp/gunicorn_config.py"
    from foca.gunicorn_config import *  # noqa: F403

    threads = 8
    ```

    ```bash
    gunicorn --config python:myapp.gunicorn_config "wsgi:create_app()"
    ```

Apps that pass their own options instead of the module are not affected by
it.

Things to keep in mind:

- **Settings `server.host` and `server.port` do not apply** to the WSGI
  server, which binds to the address and port it is given (`bind`). Keep
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

FOCA's [Helm library chart](helm-chart.md) implements the setup described
here, so that each app only needs a thin chart. In a chart of your own, let it
render the overlay from a free-form `config` value into a ConfigMap, mount it
into the app container and point `FOCA_CONFIG_OVERLAY` to it. A minimal chart
could contain:

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
- **Lists are replaced, not merged.** An overlay that sets a list, e.g.,
  `security.auth.issuers`, must contain the complete list. `api.specs` and
  `security.access_control.policies` can instead be given as [named
  entries](configuration/index.md#named-entries) (as in the [app
  template](https://github.com/elixir-cloud-aai/foca/blob/dev/templates/config.yaml)),
  which overlays add, change or remove (`null`) one at a time, by name. If
  the app gives one of them as a non-empty list, an overlay with named
  entries makes FOCA refuse to start; restate the complete list instead, or
  have the app switch to named entries. A list in an overlay replaces all
  named entries of the app, including those that later versions of the app
  add.
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
  restarts them. Databases and brokers with persistent data keep their users,
  though: change the credentials there first (see the [deployment
  notes](helm-chart.md#deployment-notes) of the Helm chart for the bundled
  MongoDB and RabbitMQ).
- The [FOCA image](getting-started.md#docker) runs as a non-root user that
  also works with arbitrary user IDs, e.g., on OpenShift.
- **Expose the app via the Gateway API** on new clusters: ingress-nginx, a
  common ingress controller, is
  [retired](https://kubernetes.io/blog/2025/11/11/ingress-nginx-retirement/).
  Proxies differ in how they limit body sizes and rates, redirect to HTTPS
  and which pods a NetworkPolicy must admit; see [Exposing the
  app](helm-chart.md#exposing-the-app) for the common setups.

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

## Verifying images and charts

From FOCA 0.17.0 on, every published FOCA image (`elixircloud/foca` on Docker
Hub, releases and `dev` builds) comes with:

- an SBOM ([SPDX](https://spdx.dev/)) and a [SLSA](https://slsa.dev/)
  provenance attestation, created by BuildKit and stored with the image;
- a [GitHub artifact
  attestation](https://docs.github.com/en/actions/concepts/security/artifact-attestations)
  of its build provenance, stored on GitHub and with the image;
- a keyless [Sigstore](https://www.sigstore.dev/) signature, made with the
  identity of the GitHub Actions workflow that built the image.

The Helm library chart (`oci://ghcr.io/elixir-cloud-aai/charts/foca-common`)
gets the same attestation and signature from version 0.2.0 on.

To check that an image was built from this repository by its release
workflow, use the [GitHub CLI](https://cli.github.com/) or
[cosign](https://docs.sigstore.dev/cosign/system_config/installation/) 3 or
later (which reads the signatures in the Sigstore bundle format), e.g., for
release {{ version }}:

```bash
gh attestation verify oci://docker.io/elixircloud/foca:{{ version }} \
    --repo elixir-cloud-aai/foca \
    --signer-workflow elixir-cloud-aai/foca/.github/workflows/release.yml \
    --source-ref refs/tags/v{{ version }}
cosign verify docker.io/elixircloud/foca:{{ version }} \
    --certificate-oidc-issuer https://token.actions.githubusercontent.com \
    --certificate-identity https://github.com/elixir-cloud-aai/foca/.github/workflows/release.yml@refs/tags/v{{ version }}
```

Both fail unless the image was signed by that workflow for that tag;
replace `{{ version }}` with the version to verify (and add the Python
version, e.g., `{{ version }}-py3.12`, for other images than the default 3.13
one). Images built from `dev` are signed by `.github/workflows/checks.yml`
with the ref `refs/heads/dev`; the chart by
`.github/workflows/release-chart.yml` with the ref `refs/tags/chart-vX.Y.Z`,
e.g.:

```bash
gh attestation verify oci://ghcr.io/elixir-cloud-aai/charts/foca-common:0.2.0 \
    --repo elixir-cloud-aai/foca \
    --signer-workflow elixir-cloud-aai/foca/.github/workflows/release-chart.yml \
    --source-ref refs/tags/chart-v0.2.0
cosign verify ghcr.io/elixir-cloud-aai/charts/foca-common:0.2.0 \
    --certificate-oidc-issuer https://token.actions.githubusercontent.com \
    --certificate-identity https://github.com/elixir-cloud-aai/foca/.github/workflows/release-chart.yml@refs/tags/chart-v0.2.0
```

Tags can be moved to other images, so deploy the digest that you verified
(e.g., `elixircloud/foca:{{ version }}@sha256:...`; `cosign verify` prints
it). To read an image's SBOM (the packages it contains) or its provenance
(how it was built, from which commit), run:

```bash
docker buildx imagetools inspect elixircloud/foca:{{ version }} \
    --format '{{ json .SBOM }}'
docker buildx imagetools inspect elixircloud/foca:{{ version }} \
    --format '{{ json .Provenance }}'
```

Images are scanned for known vulnerabilities with [Trivy](https://trivy.dev/):
CI scans the images it builds for every pull request and push, the release
workflow the release images before anything is published (both fail on fixable
critical and high vulnerabilities), and a weekly workflow the published images
of the latest release (see [Image scan](contributing.md#image-scan)). The
repository's code scanning alerts show the findings that have a fix, which the
maintainers can act on; findings without a fix (often OS packages of the base
image) appear there once a fix is released. The weekly workflow also keeps a
full report, those findings included, as a workflow artifact of each run.
From FOCA 0.17.0 on, the published images (release and `dev` images) are the
scanned ones: the workflows push each image by digest, untagged, scan exactly
that digest, and only if the scan passes (no fixable critical or high
vulnerabilities, except accepted ones) sign and tag it. Images derived from
FOCA's image (e.g., apps) need their own scans, as they add packages.
