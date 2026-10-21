"""Tests of the behavior of the templates of the library chart foca-common."""

import json
import re
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from chart_helpers import (
    APP_CHART,
    HELM,
    LIBRARY,
    NO_HOOKS,
    REQUIRED_VALUES,
    Docs,
    Manifest,
    helm,
    index,
    template,
    template_error,
)

APP = "petstore"
MONGODB = "petstore-mongodb"
CONFIG = "petstore-config"
INIT_SCRIPT = "petstore-mongodb-init-script"
NO_INGRESS = ("--set", "petstore.ingress.enabled=false")
ROUTE_API = ("--api-versions", "route.openshift.io/v1")
OPENSHIFT_API = ("--api-versions", "security.openshift.io/v1")
BODY_SIZE = "nginx.ingress.kubernetes.io/proxy-body-size"
# Settings of the app exercising all settings of the library chart
FULL_SETTINGS = """
valuesKey: petstore
containerName: store
configDir: /etc/store
maxContentLength: 1048576
requireAudience: true
mongodb:
  databases: [pets, owners]
config:
  custom:
    public_url: {{ printf "https://%s/" .Values.hostName | quote }}
"""
AUDIENCE = ("--set-json", 'config.security.auth.audience=["petstore"]')

pytestmark = pytest.mark.skipif(HELM is None, reason="helm not installed")


def _container(docs: Docs, name: str = APP) -> dict[str, Any]:
    spec = docs[("Deployment", name)]["spec"]["template"]["spec"]
    container: dict[str, Any] = spec["containers"][0]
    return container


def _env(container: dict[str, Any]) -> dict[str, Any]:
    return {
        var["name"]: var.get("value", var.get("valueFrom"))
        for var in container.get("env", [])
    }


def _overlay(docs: Docs) -> dict[str, Any]:
    overlay: dict[str, Any] = yaml.safe_load(
        docs[("ConfigMap", CONFIG)]["data"]["overlay.yaml"]
    )
    return overlay


def _checksum(docs: Docs) -> str:
    pod = docs[("Deployment", APP)]["spec"]["template"]
    checksum: str = pod["metadata"]["annotations"]["checksum/config"]
    return checksum


def _pod_spec(docs: Docs, name: str = APP) -> Manifest:
    spec: Manifest = docs[("Deployment", name)]["spec"]["template"]["spec"]
    return spec


# Resources


def test_default_resources(render: Callable[..., Docs]) -> None:
    assert set(render()) == {
        ("ConfigMap", CONFIG),
        ("ConfigMap", INIT_SCRIPT),
        ("Deployment", APP),
        ("Deployment", MONGODB),
        ("Ingress", APP),
        ("NetworkPolicy", MONGODB),
        ("PersistentVolumeClaim", MONGODB),
        ("Service", APP),
        ("Service", MONGODB),
    }


def test_labels(render: Callable[..., Docs]) -> None:
    for (_, name), doc in render().items():
        component = "mongodb" if name.startswith(MONGODB) else "app"
        assert doc["metadata"]["labels"] == {
            "helm.sh/chart": "petstore-0.1.0",
            "app.kubernetes.io/name": "petstore",
            "app.kubernetes.io/instance": "petstore",
            "app.kubernetes.io/component": component,
            "app.kubernetes.io/version": "latest",
            "app.kubernetes.io/managed-by": "Helm",
        }


@pytest.mark.parametrize(
    ("release", "args", "app"),
    [
        ("petstore", (), "petstore"),
        ("prod", (), "prod-petstore"),
        ("prod", ("--set", "nameOverride=shop"), "prod-shop"),
        ("prod", ("--set", "fullnameOverride=shop"), "shop"),
    ],
)
def test_names(chart: Path, release: str, args: tuple[str, ...], app: str):
    rendered = helm(
        "template",
        release,
        str(chart),
        NO_HOOKS,
        "--set",
        "hostName=x",
        "--set",
        "mongodb.auth.existingSecret=s",
        *args,
    )
    names = {name for _, name in index(rendered)}
    assert names == {
        app,
        f"{app}-config",
        f"{app}-mongodb",
        f"{app}-mongodb-init-script",
    }


def test_name_collision_fails(render_error: Callable[..., str]) -> None:
    long_name = "a" * 55 + "-mongodb"
    error = render_error("--set", f"fullnameOverride={long_name}")
    assert "would both be named" in error


def test_external_mongodb_needs_host(
    render_error: Callable[..., str],
) -> None:
    error = render_error("--set", "mongodb.enabled=false")
    assert "config.db.host must be set" in error


def test_settings_must_be_yaml(
    chart_with_settings: Callable[[str], Path],
) -> None:
    chart = chart_with_settings("valuesKey: [")
    assert "settings are not valid YAML" in template_error(chart)


def test_mongodb_disabled(render: Callable[..., Docs]) -> None:
    docs = render(
        "--set",
        "mongodb.enabled=false",
        "--set",
        "config.db.host=mongodb.example.org",
    )
    assert {name for _, name in docs} == {APP, CONFIG}
    assert _overlay(docs) == {"db": {"host": "mongodb.example.org"}}
    env = _env(_container(docs))
    assert env["MONGO_USERNAME"]["secretKeyRef"]["key"] == "username"


# Settings of the app


def test_settings_required(
    chart_with_settings: Callable[[str], Path],
) -> None:
    chart = chart_with_settings("containerName: x")
    assert re.search("must set valuesKey", template_error(chart))


def test_values_key_must_be_map(
    chart_with_settings: Callable[[str], Path],
) -> None:
    chart = chart_with_settings("valuesKey: store")
    assert re.search("store must be a map", template_error(chart))


def test_all_settings(chart_with_settings: Callable[[str], Path]) -> None:
    docs = index(template(chart_with_settings(FULL_SETTINGS), *AUDIENCE))
    container = _container(docs)
    assert container["name"] == "store"
    assert _env(container)["FOCA_CONFIG_OVERLAY"] == "/etc/store/overlay.yaml"
    assert container["volumeMounts"][0]["mountPath"] == "/etc/store"
    overlay = _overlay(docs)
    assert overlay["custom"] == {"public_url": "https://petstore.example.org/"}
    annotations = docs[("Ingress", APP)]["metadata"]["annotations"]
    assert annotations[BODY_SIZE] == "1048576"
    script = docs[("ConfigMap", INIT_SCRIPT)]["data"]["create-app-user.js"]
    assert "db.getSiblingDB('pets')" in script
    assert (
        "roles: [{ role: 'readWrite', db: 'pets' }, "
        "{ role: 'readWrite', db: 'owners' }]"
    ) in script


@pytest.mark.parametrize(
    "config",
    [
        {},
        {"security": {"auth": {"audience": []}}},
        {"security": {"auth": {"audience": [" "]}}},
        {"security": {"auth": {"audience": None}}},
        {"security": {"auth": {"audience": "petstore"}}},
        # `null` doesn't disable the validation (FOCA refuses it)
        {"security": {"auth": {"verify_audience": None}}},
        {"security": {"auth": None}},
        {"security": None},
    ],
)
def test_audience_required(
    chart_with_settings: Callable[[str], Path],
    tmp_path: Path,
    config: dict[str, Any],
) -> None:
    chart = chart_with_settings(FULL_SETTINGS)
    # A values file, as `--set` removes keys set to `null`
    values = tmp_path / "values.yaml"
    values.write_text(yaml.safe_dump({"config": config}))
    error = template_error(chart, "-f", str(values))
    assert "audience must list" in error


@pytest.mark.parametrize("value", ["false", '"False"', '"off"', '"0"'])
def test_audience_validation_disabled(
    chart_with_settings: Callable[[str], Path], value: str
) -> None:
    chart = chart_with_settings(FULL_SETTINGS)
    template(chart, "--set-json", f"config.security.auth.verify_audience={value}")


def test_audience_not_required_by_default(
    render: Callable[..., Docs],
) -> None:
    render()


def test_init_script_needs_databases(
    chart_with_settings: Callable[[str], Path],
) -> None:
    chart = chart_with_settings("valuesKey: petstore")
    assert re.search(r"mongodb\.databases", template_error(chart))
    template(chart, "--set", "mongodb.auth.enabled=false")


# Config overlay


def test_overlay_merges_config(render: Callable[..., Docs]) -> None:
    config = {
        "db": {"port": 1234},
        "log": {"handlers": {"console": {"level": 20}}},
    }
    docs = render("--set-json", f"config={json.dumps(config)}")
    assert _overlay(docs) == {
        "db": {"host": MONGODB, "port": 1234},
        "log": {"handlers": {"console": {"level": 20}}},
    }


def test_overlay_config_takes_precedence(render: Callable[..., Docs]):
    docs = render("--set", "config.db.host=other")
    assert _overlay(docs) == {"db": {"host": "other"}}


@pytest.mark.parametrize("section", ["db", "server"])
def test_overlay_drops_null_sections(render: Callable[..., Docs], section: str) -> None:
    docs = render("--set-json", f"config.{section}=null")
    assert _overlay(docs) == {"db": {"host": MONGODB}}


def test_checksum_follows_config(render: Callable[..., Docs]) -> None:
    default = _checksum(render())
    assert _checksum(render()) == default
    assert _checksum(render("--set", "config.log.root.level=10")) != default
    # Other values don't roll the pods
    assert _checksum(render("--set", "petstore.tag=1.0.0")) == default


# App Deployment


def test_container(render: Callable[..., Docs]) -> None:
    docs = render()
    container = _container(docs)
    assert container["image"] == "foca-petstore:latest"
    assert container["imagePullPolicy"] == "IfNotPresent"
    assert container["name"] == "petstore"
    env = _env(container)
    assert env["FOCA_CONFIG_OVERLAY"] == "/etc/petstore/overlay.yaml"
    assert env["GUNICORN_CMD_ARGS"] == (
        "--workers 1 --threads 4 --timeout 30 --graceful-timeout 20 --keep-alive 75"
    )
    for var, key in [
        ("MONGO_USERNAME", "username"),
        ("MONGO_PASSWORD", "password"),
    ]:
        assert env[var] == {"secretKeyRef": {"name": "mongodb-credentials", "key": key}}
    for probe in ("startupProbe", "readinessProbe", "livenessProbe"):
        assert container[probe]["httpGet"] == {"path": "/ui/", "port": 8080}
    assert container["lifecycle"]["preStop"]["exec"]["command"] == [
        "python",
        "-c",
        "import time; time.sleep(5)",
    ]
    spec = _pod_spec(docs)
    assert spec["enableServiceLinks"] is False
    assert spec["automountServiceAccountToken"] is False
    assert spec["terminationGracePeriodSeconds"] == 35
    assert spec["volumes"][1] == {
        "name": "tmp",
        "emptyDir": {"sizeLimit": "64Mi"},
    }


@pytest.mark.parametrize(
    ("args", "image"),
    [
        (("--set", "petstore.tag=1.2.3"), "foca-petstore:1.2.3"),
        (
            ("--set", "petstore.image=registry:5000/pets"),
            "registry:5000/pets:latest",
        ),
        (("--set", "petstore.image=pets:2"), "pets:2"),
        (("--set", "petstore.image=pets@sha256:abc"), "pets@sha256:abc"),
    ],
)
def test_image(render: Callable[..., Docs], args: tuple[str, ...], image: str) -> None:
    assert _container(render(*args))["image"] == image


def test_image_with_tag_and_tag_fails(render_error: Callable[..., str]):
    error = render_error(
        "--set", "petstore.image=pets:2", "--set-string", "petstore.tag=3"
    )
    assert "petstore.tag must not be set" in error


def test_image_pull_policy(render: Callable[..., Docs]) -> None:
    docs = render("--set", "petstore.imagePullPolicy=Never")
    assert _container(docs)["imagePullPolicy"] == "Never"
    docs = render("--set-json", "petstore.imagePullPolicy=null")
    assert _container(docs)["imagePullPolicy"] == "Always"


def test_without_gunicorn(render: Callable[..., Docs]) -> None:
    docs = render(
        "--set",
        "petstore.gunicorn=null",
        "--set",
        "petstore.preStopSleepSeconds=0",
    )
    container = _container(docs)
    assert "GUNICORN_CMD_ARGS" not in _env(container)
    assert "lifecycle" not in container
    assert _pod_spec(docs)["terminationGracePeriodSeconds"] == 30


def test_gunicorn_extra_args_quoted(render: Callable[..., Docs]) -> None:
    docs = render("--set-json", 'petstore.gunicorn.extraArgs=["--access-logfile", "-"]')
    args = _env(_container(docs))["GUNICORN_CMD_ARGS"]
    assert args.endswith(" '--access-logfile' '-'")


@pytest.mark.parametrize(
    "arg",
    [
        "--bind=0.0.0.0:80",
        "--bi",
        "-b0.0.0.0:80",
        "-Db",
        "--preload",
        "--graceful-timeout=1",
    ],
)
def test_gunicorn_reserved_args_fail(
    render_error: Callable[..., str], arg: str
) -> None:
    error = render_error(
        "--set-json", f"petstore.gunicorn.extraArgs={json.dumps([arg])}"
    )
    assert "petstore.gunicorn.extraArgs must not set" in error


@pytest.mark.parametrize("arg", ["--bind x", "--backlog=1", "-D"])
def test_gunicorn_other_args_pass(render: Callable[..., Docs], arg: str) -> None:
    render("--set-json", f"petstore.gunicorn.extraArgs={json.dumps([arg])}")


@pytest.mark.parametrize(
    ("value", "size"),
    [("null", None), ("1000000", "1000000"), ('"1Gi"', "1Gi")],
)
def test_tmp_size_limit(
    render: Callable[..., Docs], value: str, size: str | None
) -> None:
    docs = render("--set-json", f"petstore.tmpSizeLimit={value}")
    expected = {"sizeLimit": size} if size else {}
    assert _pod_spec(docs)["volumes"][1]["emptyDir"] == expected


def test_security_contexts(render: Callable[..., Docs]) -> None:
    docs = render()
    assert _pod_spec(docs)["securityContext"]["runAsNonRoot"] is True
    mongodb = _pod_spec(docs, MONGODB)["securityContext"]
    assert mongodb["runAsUser"] == 999
    assert mongodb["fsGroup"] == 999


def test_security_contexts_openshift(render: Callable[..., Docs]) -> None:
    docs = render(*OPENSHIFT_API)
    for name in (APP, MONGODB):
        context = _pod_spec(docs, name)["securityContext"]
        assert not {"runAsUser", "runAsGroup", "fsGroup"} & set(context)
        assert context["runAsNonRoot"] is True


def test_mongodb_without_auth(render: Callable[..., Docs]) -> None:
    docs = render(
        "--set",
        "mongodb.auth.enabled=false",
        "--set",
        "hostName=x",
        required=False,
    )
    assert ("ConfigMap", INIT_SCRIPT) not in docs
    assert "MONGO_USERNAME" not in _env(_container(docs))
    assert "env" not in _container(docs, MONGODB)


def test_mongodb_secret_required(render_error: Callable[..., str]) -> None:
    error = render_error("--set", "hostName=x", required=False)
    assert "mongodb.auth.existingSecret must be set" in error


def test_mongodb_credentials(render: Callable[..., Docs]) -> None:
    env = _env(_container(render(), MONGODB))
    assert {var: ref["secretKeyRef"]["key"] for var, ref in env.items()} == {
        "MONGO_APP_PASSWORD": "password",
        "MONGO_APP_USERNAME": "username",
        "MONGO_INITDB_ROOT_PASSWORD": "root-password",
        "MONGO_INITDB_ROOT_USERNAME": "root-username",
    }


def test_probe_path(render: Callable[..., Docs]) -> None:
    container = _container(render("--set", "petstore.probePath=/health"))
    for probe in ("startupProbe", "readinessProbe", "livenessProbe"):
        assert container[probe]["httpGet"]["path"] == "/health"


@pytest.mark.parametrize(
    ("args", "seconds"),
    [
        ((), 35),
        (
            (
                "--set",
                "petstore.preStopSleepSeconds=7",
                "--set",
                "petstore.gunicorn.gracefulTimeout=60",
            ),
            77,
        ),
        (("--set", "petstore.gunicorn=null"), 35),
        (
            (
                "--set",
                "petstore.gunicorn=null",
                "--set",
                "petstore.preStopSleepSeconds=0",
            ),
            30,
        ),
    ],
)
def test_termination_grace_period(
    render: Callable[..., Docs], args: tuple[str, ...], seconds: int
) -> None:
    spec = _pod_spec(render(*args))
    assert spec["terminationGracePeriodSeconds"] == seconds


def test_gunicorn_settings(render: Callable[..., Docs]) -> None:
    docs = render(
        "--set",
        "petstore.gunicorn.workers=2",
        "--set",
        "petstore.gunicorn.threads=8",
        "--set",
        "petstore.gunicorn.timeout=60",
        "--set",
        "petstore.gunicorn.gracefulTimeout=15",
        "--set",
        "petstore.gunicorn.keepalive=0",
    )
    assert _env(_container(docs))["GUNICORN_CMD_ARGS"] == (
        "--workers 2 --threads 8 --timeout 60 --graceful-timeout 15 --keep-alive 0"
    )


def test_service(render: Callable[..., Docs]) -> None:
    docs = render()
    for name, component, port in [
        (APP, "app", {"name": "http", "port": 8080, "targetPort": "http"}),
        (
            MONGODB,
            "mongodb",
            {"name": "mongodb", "port": 27017, "targetPort": "mongodb"},
        ),
    ]:
        spec = docs[("Service", name)]["spec"]
        assert spec["type"] == "ClusterIP"
        assert spec["ports"] == [{**port, "protocol": "TCP"}]
        assert spec["selector"] == {
            "app.kubernetes.io/name": "petstore",
            "app.kubernetes.io/instance": "petstore",
            "app.kubernetes.io/component": component,
        }
        # The Service selects the pods of its Deployment
        selector = docs[("Deployment", name)]["spec"]["selector"]
        assert selector["matchLabels"] == spec["selector"]


def test_overlay_db_host_is_mongodb_service(
    render: Callable[..., Docs],
) -> None:
    docs = render("--set", "fullnameOverride=shop")
    overlay = yaml.safe_load(docs[("ConfigMap", "shop-config")]["data"]["overlay.yaml"])
    assert overlay["db"]["host"] == "shop-mongodb"
    assert ("Service", "shop-mongodb") in docs


def test_mongodb_settings(render: Callable[..., Docs]) -> None:
    docs = render(
        "--set",
        "mongodb.volumeSize=5Gi",
        "--set",
        "mongodb.image=mongo:7.0.1",
        "--set",
        "mongodb.imagePullPolicy=IfNotPresent",
    )
    pvc = docs[("PersistentVolumeClaim", MONGODB)]["spec"]
    assert pvc["resources"]["requests"]["storage"] == "5Gi"
    assert pvc["accessModes"] == ["ReadWriteOnce"]
    container = _container(docs, MONGODB)
    assert container["image"] == "mongo:7.0.1"
    assert container["imagePullPolicy"] == "IfNotPresent"
    assert _container(render(), MONGODB)["imagePullPolicy"] == "Always"


def test_mongodb_values_required(
    chart_with_settings: Callable[[str], Path],
) -> None:
    chart = chart_with_settings("valuesKey: petstore")
    (chart / "values.schema.json").unlink()
    values = yaml.safe_load((chart / "values.yaml").read_text())
    del values["mongodb"]
    (chart / "values.yaml").write_text(yaml.safe_dump(values))
    error = template_error(chart, "--set", "hostName=x", required=False)
    assert "mongodb must be a map" in error


# Ingress and Route


def test_ingress(render: Callable[..., Docs]) -> None:
    ingress = render()[("Ingress", APP)]
    assert "annotations" not in ingress["metadata"]
    assert "ingressClassName" not in ingress["spec"]
    assert "tls" not in ingress["spec"]
    (rule,) = ingress["spec"]["rules"]
    assert rule["host"] == "petstore.example.org"


def test_ingress_https(render: Callable[..., Docs]) -> None:
    ingress = render("--set", "petstore.ingress.https.enabled=true")[("Ingress", APP)]
    assert ingress["metadata"]["annotations"] == {
        "cert-manager.io/cluster-issuer": "letsencrypt-prod",
        "kubernetes.io/tls-acme": "true",
    }
    assert ingress["spec"]["tls"] == [
        {
            "hosts": ["petstore.example.org"],
            "secretName": "petstore-ingress-secret",
        }
    ]


def test_ingress_annotations(render: Callable[..., Docs]) -> None:
    annotations = {"a": "b", "kubernetes.io/tls-acme": None}
    ingress = render(
        "--set",
        "petstore.ingress.https.enabled=true",
        "--set",
        "config.server.max_content_length=2048",
        "--set-json",
        f"petstore.ingress.annotations={json.dumps(annotations)}",
    )[("Ingress", APP)]
    assert ingress["metadata"]["annotations"] == {
        "a": "b",
        "cert-manager.io/cluster-issuer": "letsencrypt-prod",
        BODY_SIZE: "2048",
    }


@pytest.mark.parametrize("value", ["0", "1.5", '"1m"'])
def test_max_content_length_invalid(chart: Path, value: str, tmp_path: Path) -> None:
    # Bypasses the schema, which rejects these values already
    chart_copy = tmp_path / "chart"
    shutil.copytree(chart, chart_copy)
    (chart_copy / "values.schema.json").unlink()
    error = template_error(
        chart_copy, "--set-json", f"config.server.max_content_length={value}"
    )
    assert "must be a positive integer" in error


def test_ingress_host_required(render_error: Callable[..., str]) -> None:
    error = render_error("--set", "mongodb.auth.existingSecret=s", required=False)
    assert "hostName must be set" in error


def test_no_ingress_no_route(render: Callable[..., Docs]) -> None:
    docs = render(*NO_INGRESS)
    assert not {kind for kind, _ in docs} & {"Ingress", "Route"}


def test_route(render: Callable[..., Docs]) -> None:
    docs = render(*NO_INGRESS, *ROUTE_API)
    route = docs[("Route", APP)]
    assert route["spec"]["host"] == "petstore.example.org"
    assert route["spec"]["to"]["name"] == APP
    assert route["spec"]["tls"]["termination"] == "edge"
    # An Ingress takes precedence
    assert ("Route", APP) not in render(*ROUTE_API)


# NetworkPolicies


def test_app_network_policy(render: Callable[..., Docs]) -> None:
    docs = render("--set", "petstore.networkPolicy.enabled=true")
    policy = docs[("NetworkPolicy", APP)]["spec"]
    assert policy["podSelector"]["matchLabels"]["app.kubernetes.io/component"] == "app"
    (rule,) = policy["ingress"]
    assert rule["from"] == [
        {
            "namespaceSelector": {
                "matchLabels": {"kubernetes.io/metadata.name": "ingress-nginx"}
            }
        }
    ]
    assert rule["ports"] == [{"port": 8080, "protocol": "TCP"}]


def test_app_network_policy_needs_peers(
    render_error: Callable[..., str],
) -> None:
    error = render_error(
        "--set",
        "petstore.networkPolicy.enabled=true",
        "--set-json",
        "petstore.networkPolicy.ingressFrom=null",
    )
    assert "ingressFrom must list at least one" in error


def test_mongodb_network_policy(render: Callable[..., Docs]) -> None:
    policy = render()[("NetworkPolicy", MONGODB)]["spec"]
    (rule,) = policy["ingress"]
    (peer,) = rule["from"]
    labels = peer["podSelector"]["matchLabels"]
    assert labels["app.kubernetes.io/component"] == "app"
    assert labels["app.kubernetes.io/instance"] == "petstore"
    assert rule["ports"] == [{"port": 27017, "protocol": "TCP"}]
    docs = render("--set", "mongodb.networkPolicy.enabled=false")
    assert ("NetworkPolicy", MONGODB) not in docs


# Schema


def test_app_schema_matches_library() -> None:
    library = json.loads((LIBRARY / "app-values.schema.json").read_text())
    app = json.loads((APP_CHART / "values.schema.json").read_text())
    assert app["properties"].pop("petstore") == {"$ref": "#/$defs/app"}
    for schema in (library, app):
        schema.pop("$comment")
    assert app == library


def test_schema_rejects_invalid_values(render_error: Callable[..., str]):
    error = render_error("--set", "petstore.imagePullPolicy=Sometimes")
    assert "schema" in error


def test_library_lints() -> None:
    helm("lint", "--strict", str(LIBRARY))


def test_app_chart_lints(chart: Path) -> None:
    values = chart / "ci" / "test-values.yaml"
    helm("lint", "--strict", str(chart), "-f", str(values))


def test_app_chart_test_hook(chart: Path) -> None:
    rendered = helm(
        "template",
        "petstore",
        str(chart),
        "--show-only",
        "templates/tests/test-api.yaml",
        *REQUIRED_VALUES,
    )
    (pod,) = index(rendered).values()
    assert pod["metadata"]["annotations"]["helm.sh/hook"] == "test"
    (container,) = pod["spec"]["containers"]
    app_version = yaml.safe_load((APP_CHART / "Chart.yaml").read_text())["appVersion"]
    assert container["image"] == f"foca-petstore:{app_version}"
    assert _env(container)["PETSTORE_URL"] == "http://petstore:8080"
    command = container["command"]
    assert command[:2] == ["python", "-c"]
    compile(command[2], "test-api", "exec")


def test_hooks_not_rendered_by_default(render: Callable[..., Docs]) -> None:
    assert "Pod" not in {kind for kind, _ in render()}
