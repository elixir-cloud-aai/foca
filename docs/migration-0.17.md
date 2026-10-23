# Migrating to 0.17.0

FOCA 0.17.0 lets operators adapt an app's API specifications and access
control policies in config overlays, and comes with version 0.2.0 of the
[Helm library chart](helm-chart.md), which adds the Gateway API and keeps the
bundled broker's data and MongoDB's settings. Most apps need no changes to
run with FOCA 0.17.0; check the breaking change below if your app uses
access control, and the other changes if your app's image builds packages,
your app uses the resource ownership helpers, or you deploy it with the Helm
chart. Upgrading from 0.15 or earlier? Start with [Migrating to
0.16.0](migration-0.16.md).

## Breaking changes

### Configured policies

Configured policy rules ([`security.access_control.policies`](access-control.md#configuring-policies))
are now replaced atomically on startup: requests are checked against either
the previous or the new rules, never a mix, also during rolling updates or
after a startup that failed midway. FOCA stores the rules in generations,
with a marker document (`_id: configured_policies`) in the policy
collection.

- **Stored configured rules are removed if `policies` is not set.** FOCA
  removes them on startup, with a warning; before, they were left in
  effect. To remove a rule that an overlay added (e.g., a [named
  entry](configuration/index.md#named-entries)), remove it from the overlay.
  If removing them fails, the app refuses to start, as it does if storing
  configured rules fails.
- Apps that share a policy collection must all configure the same rules.
  By default, all FOCA apps on the same MongoDB server share one
  (`access_control_db.policy_rules`): apps on a shared external MongoDB
  must set their own `db_name`.
- Rules added via the permissions API with the values of a configured rule
  are no longer turned into configured rules: they stay deletable via the
  API, and stay stored when the rule is removed from the configuration.
- Configured rules stored by 0.16.0 stay in effect until the first startup
  of 0.17.0 replaces them. During a rolling update from 0.16, the old
  instances do not get the atomicity guarantee: upgrade all instances before
  changing configured rules, and do not start 0.16 instances again
  afterwards.
- To downgrade to 0.16, first delete the document `_id: configured_policies`
  and all rules with `configured: true`; 0.16 stores its configured rules
  again on startup.

## Images without setuptools and wheel, with the latest pip

The FOCA images no longer contain setuptools and wheel. Only the Python 3.11
base image shipped them (with known vulnerabilities); FOCA needs neither, as
pip builds packages in isolated environments. Images derived from FOCA's that
build packages without build isolation (`pip install --no-build-isolation`)
or run `python setup.py` must install setuptools (and wheel) themselves.

The images of Python 3.11 and 3.12 also have the latest pip (at least 26.2)
instead of the old one of their base image (24.0 and 25.0.1, with known
vulnerabilities), as those of Python 3.13 and 3.14 already did. Images
derived from them that use pip features removed since must adapt (see
[pip's changelog](https://pip.pypa.io/en/stable/news/)).

## Helm chart 0.2.0

The library chart `foca-common` is released separately (`chart-v0.2.0`).
App charts written for 0.1.0 render with 0.2.0 as they are, with the broker
as a StatefulSet (see [below](#broker-statefulset-and-persistence)) but
without its persistence, with MongoDB unchanged except for the keep
annotation on its claim (see [MongoDB persistence](#mongodb-persistence)), without the ingress-nginx annotation
for body sizes (see [No ingress-nginx
defaults](#no-ingress-nginx-defaults)), and without the new features. To
upgrade an app chart:

1. Set `version: 0.2.0` for `foca-common` in `Chart.yaml` and run `helm
   dependency update`.
2. Copy the new values from the petstore example's `values.yaml`:
   `<valuesKey>.httpRoute`, `<valuesKey>.route` (with `enabled` and
   `annotations`), `mongodb.persistence` (instead of `mongodb.volumeSize`)
   and, with the bundled broker, `broker.persistence`; copy the library's
   new `app-values.schema.json` as `values.schema.json`, too, and add the
   app's key again (see [Values](helm-chart.md#values)). Drop defaults for
   a particular ingress controller from the app chart's values, as the
   petstore example did (see [below](#no-ingress-nginx-defaults)).
3. Add template files (see [Templates](helm-chart.md#templates)) for
    - `foca-common.httpRoute` and `foca-common.httpsRedirectRoute`, to expose
      the app via the [Gateway API](helm-chart.md#gateway-api) (the petstore
      example's `httproute.yaml` and `httproute-https-redirect.yaml`);
    - `foca-common.brokerPersistentVolumeClaim`, if the app chart has the
      broker (`broker-pvc.yaml`): with `broker.persistence.enabled` but
      without this template, the broker's pod waits for its claim, and the
      worker for the broker.
4. Optionally, rename `foca-common.brokerDeployment` to
   `foca-common.brokerStatefulSet` (`broker-statefulset.yaml`); the old name
   is a deprecated alias, which renders the StatefulSet, too.

### No ingress-nginx defaults

foca-common 0.2.0 has no defaults for a particular ingress controller
(ingress-nginx is
[retired](https://kubernetes.io/blog/2025/11/11/ingress-nginx-retirement/));
annotations and NetworkPolicy peers for one go into the values, with
examples per setup in [Exposing the app](helm-chart.md#exposing-the-app):

- **Body size:** the Ingress no longer sets
  `nginx.ingress.kubernetes.io/proxy-body-size` to the app's limit
  (`config.server.max_content_length`). With ingress-nginx, its default
  limit of 1 MiB then applies at the proxy; for apps that accept larger
  bodies, set the annotation in `<valuesKey>.ingress.annotations`, e.g.,
  `nginx.ingress.kubernetes.io/proxy-body-size: 8m` (see
  [ingress-nginx](helm-chart.md#ingress-nginx)). Other controllers ignored
  the annotation. The app's own limit applies as before; rendering still
  fails if it is not a positive integer, now also without an Ingress. The
  setting `maxContentLength` and the helper `foca-common.maxContentLength`
  stay, for resources of the app chart's own.
- **NetworkPolicy peers:** the petstore example's
  `<valuesKey>.networkPolicy.ingressFrom` is empty instead of the namespace
  `ingress-nginx`, and the schema allows an empty list (as a default);
  enabling the policy without peers still fails to render. App charts that
  copied the old default should drop it too, and deployments that enable
  the policy set their peers (see [NetworkPolicy
  peers](helm-chart.md#networkpolicy-peers)).
- **OpenShift Route:** `<valuesKey>.route.enabled: false` turns off the
  Route, which serves all paths, e.g., for a Route or HTTPRoute of your own
  that serves only some paths. Values without it (as for 0.1.0) keep the
  Route.

### Broker StatefulSet and persistence

From 0.2.0 on, the bundled RabbitMQ is a StatefulSet (named like the
Deployment of 0.1.0, `<app name>-broker`), which keeps its data in a
PersistentVolumeClaim if `broker.persistence.enabled` (the default of the
petstore example's values; values without a `broker.persistence` map keep
the `emptyDir` of 0.1.0). The upgrade from 0.1.0 replaces the broker's
Deployment with the StatefulSet: Helm creates the StatefulSet, with a new,
empty claim, before it deletes the Deployment, whose data was in an
`emptyDir`.

!!! warning "Queued tasks are lost once"
    Tasks queued in the broker of 0.1.0 are lost on this upgrade, as are
    results of the `rpc://` result backend. Upgrade when no tasks are
    queued, e.g., stop submitting tasks and wait until the queues are empty:

    ```bash
    kubectl exec deployment/my-app-broker -- \
        rabbitmqctl list_queues name messages
    ```

    The app and the worker reconnect to the new broker. Later upgrades keep
    queued tasks, as the new pod starts only once the old one is gone and
    uses the same claim.

The claim is part of the release:

- `helm uninstall` deletes it, and with it the queued tasks (and, depending
  on the reclaim policy of its storage class, the volume). To keep the data
  across installations, create a claim outside the release and set
  `broker.persistence.existingClaim`.
- A rollback to a revision with foca-common 0.1.0, or without
  `broker.persistence.enabled`, deletes the claim, too, and brings back the
  broker's `emptyDir`; rollbacks between revisions with persistence keep it.
- Kubernetes does not allow changing `accessModes` or `storageClass` of an
  existing claim, and allows a larger `size` only if its storage class
  allows volume expansion; such upgrades fail.
- RabbitMQ creates its user from the Secret only on its first start: after
  rotating the broker's credentials, change them in RabbitMQ, too (see the
  [deployment notes](helm-chart.md#deployment-notes)).
- RabbitMQ now keeps its data across updates of `broker.image`; follow
  RabbitMQ's [upgrade guide](https://www.rabbitmq.com/docs/upgrade), e.g.,
  for the versions it can upgrade from.

### MongoDB persistence

`mongodb.persistence` replaces `mongodb.volumeSize`, with the same keys as
`broker.persistence`: `enabled`, `size`, `accessModes`, `storageClass` and
`existingClaim`. Persistence stays enabled by default (also without the
map), and MongoDB's claim keeps its name (`<app name>-mongodb`), so upgrades
keep the data; values written for 0.1.0 render MongoDB's resources
unchanged, except for the keep annotation on the claim (see below). App charts replace `volumeSize` with the map in their
`values.yaml` (see the petstore example); the template file with
`foca-common.mongodbPersistentVolumeClaim` (`mongodb-pvc.yaml`) stays as it
is:

```yaml title="values.yaml"
mongodb:
  persistence:
    enabled: true
    size: 1Gi
    accessModes: ["ReadWriteOnce"]
    storageClass: ""
    existingClaim: ""
```

In deployments, `mongodb.volumeSize` still works, but it is deprecated, and
rendering fails if `mongodb.persistence.size` is set, too, as one of them
would be ignored: rename it to `mongodb.persistence.size` in your values. If
the app chart's own values still set `volumeSize`, unset it with `null`,
e.g., `--set-json mongodb.volumeSize=null`.

MongoDB's claim is annotated with `helm.sh/resource-policy: keep`
(`mongodb.persistence.resourcePolicy: keep`, also if not set; empty to turn
it off): uninstalling the release keeps MongoDB's claim, and with it the
database; delete it by hand to remove the data (`kubectl delete pvc <app
name>-mongodb`). Reinstalling a release of the same name in the same
namespace adopts the kept claim, with its data and the app user. A release
of another name gets a new, empty claim, or fails on the kept one if its
resource names are the same (e.g., with the same `fullnameOverride`); set
the kept claim as its `existingClaim` to use the data. The broker's claim is still deleted with
the release (`broker.persistence.resourcePolicy`).

!!! warning "Upgrade first, then switch to `existingClaim`"
    On upgrades, Helm checks the annotation on the claim in the cluster,
    which releases installed with 0.1.0 do not have yet. Disabling
    persistence, or switching to an `existingClaim`, in the same upgrade as
    the one to 0.2.0 deletes the chart's claim, and, with the usual reclaim
    policy `Delete`, its data. Upgrade to 0.2.0 first and change these
    settings in a later upgrade, or annotate the claim first (`kubectl
    annotate pvc <app name>-mongodb helm.sh/resource-policy=keep`).

## Resource ownership helpers

`grant_role()` stores the new role before it removes the subject's other
roles on the resource (in 0.16.0, it removed them first), so that the
subject keeps a role throughout, e.g., a maintainer promoted to owner.
Concurrent grants for the same subject and resource leave it with one role,
that of the grant stored last, ordered by a timestamp of the database server
in the new field `granted` of stored rules (rules stored by earlier versions
count as the oldest). If removing the other roles fails, the new role is
removed again. Code that reads or writes the policy collection directly may
see the new field; `Adapter.replace_role_rule()` is the new adapter method
behind `grant_role()` ([details](access-control.md#resource-ownership)).

## New features

- **Named entries:** `api.specs` and `security.access_control.policies` can
  be given as mappings of names to entries, which config overlays add,
  change or remove (`null`) by name, without restating the others
  ([details](configuration/index.md#named-entries)). Lists keep working as
  before; FOCA refuses to start if an overlay gives named entries for a
  section that earlier files set to a non-empty list. Give your app's
  specifications and policies as named entries, as the [app
  template](https://github.com/elixir-cloud-aai/foca/blob/dev/templates/config.yaml)
  and the examples do, so that operators can adapt them.
- **Spec patches:** `api.specs[].patches`, files or inline mappings
  deep-merged into a specification, e.g., to require login for public reads
  ([details](configuration/api.md#patching-specifications),
  [recipe](access-control.md#requiring-login-for-reads)).
  With the Helm chart, give them inline in the `config` value.
- **Gateway API:** the Helm chart exposes the app via an `HTTPRoute`, with an
  optional redirect from HTTP to HTTPS, as an alternative to the Ingress
  ([details](helm-chart.md#exposing-the-app)), sets annotations of the
  OpenShift Route (`<valuesKey>.route.annotations`) and can turn it off
  (`<valuesKey>.route.enabled`).
- **Supply chain:** images and the Helm chart are published with attestations
  and signatures; see [Verifying images and
  charts](deployment.md#verifying-images-and-charts) for how to check them
  before deploying.
- **Citation:** cite FOCA with the metadata in
  [`CITATION.cff`](https://github.com/elixir-cloud-aai/foca/blob/dev/CITATION.cff),
  e.g., via GitHub's "Cite this repository"; Zenodo archives each release.

## Dependencies

Raised lower bounds, for known vulnerabilities:

- `pymongo>=4.18.2` (fixes CVE-2026-88029, CVE-2026-96747, CVE-2026-96748
  and CVE-2026-96749);
- `requests>=2.33.0` (fixes CVE-2026-25645);
- `Werkzeug>=3.1.9` (fixes CVE-2026-27199 and CVE-2026-102598).

New: `certifi>=2026.10.19` (used via requests; fixes GHSA-6q48-6c7w-9gw2,
compromised intermediate CAs of the emSign root CAs).

Building FOCA from source requires `setuptools>=83` (fixes CVE-2026-59890).
