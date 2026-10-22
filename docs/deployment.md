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
gunicorn --bind 0.0.0.0:8080 --workers 4 --no-control-socket "wsgi:create_app()"
```

The [FOCA image](getting-started.md#docker) does not include a WSGI server;
install one in your app's image, e.g., from a requirements file that pins
its version (`gunicorn==X.Y.Z`). The image's working directory is `/app`:

```dockerfile title="Dockerfile"
FROM elixircloud/foca:latest
USER root
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
USER 1000
COPY ./ /app
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "4", "--no-control-socket", "wsgi:create_app()"]
```

Things to keep in mind:

- **Do not preload the app** (e.g., Gunicorn's `--preload`): MongoDB
  clients must not be shared across forked worker processes, so each worker
  needs to create its own app.
- **Disable Gunicorn's control socket** (`--no-control-socket`, Gunicorn
  25.1 or later) unless you use `gunicornc`: Gunicorn creates it in
  `$XDG_RUNTIME_DIR` or `$HOME/.gunicorn` and logs an error at startup if
  that is not writable, e.g., on the read-only root file system of the
  [Helm chart](helm-chart.md).
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
release 0.17.0:

```bash
gh attestation verify oci://docker.io/elixircloud/foca:0.17.0 \
    --repo elixir-cloud-aai/foca \
    --signer-workflow elixir-cloud-aai/foca/.github/workflows/release.yml \
    --source-ref refs/tags/v0.17.0
cosign verify docker.io/elixircloud/foca:0.17.0 \
    --certificate-oidc-issuer https://token.actions.githubusercontent.com \
    --certificate-identity https://github.com/elixir-cloud-aai/foca/.github/workflows/release.yml@refs/tags/v0.17.0
```

Both fail unless the image was signed by that workflow for that tag;
replace `0.17.0` with the version to verify (and add the Python version, e.g.,
`0.17.0-py3.12`, for other images than the default 3.13 one). Images
built from `dev` are signed by `.github/workflows/checks.yml` with the ref
`refs/heads/dev`; the chart by `.github/workflows/release-chart.yml` with the
ref `refs/tags/chart-vX.Y.Z`, e.g.:

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
(e.g., `elixircloud/foca:0.17.0@sha256:...`; `cosign verify` prints it). To
read an image's SBOM (the packages it contains) or its provenance (how it was
built, from which commit), run:

```bash
docker buildx imagetools inspect elixircloud/foca:0.17.0 \
    --format '{{ json .SBOM }}'
docker buildx imagetools inspect elixircloud/foca:0.17.0 \
    --format '{{ json .Provenance }}'
```

Images are scanned for known vulnerabilities with
[Trivy](https://trivy.dev/) in CI and again before a release is published
(both fail on fixable critical and high vulnerabilities), and the images of
the latest release weekly (see [Image scan](contributing.md#image-scan)); the
maintainers see the results in the repository's code scanning alerts.
Images derived from FOCA's image (e.g., apps) need their own scans, as they
add packages.
