"""Tests of the behavior of the templates of the library chart foca-common."""

import json
import re
import shutil
import subprocess
from collections.abc import Callable
from copy import deepcopy
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
    build_chart,
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
# Peers of the app's NetworkPolicy (the petstore example has none by default)
PEERS = [
    {
        "namespaceSelector": {
            "matchLabels": {"kubernetes.io/metadata.name": "envoy-gateway-system"}
        }
    }
]
# Template of an app chart rendering the app's size limit of request bodies
LIMIT_TEMPLATE = """
{{- $context := dict "root" . "settings" (include "petstore.settings" . | fromYaml) }}
apiVersion: v1
kind: ConfigMap
metadata:
  name: limit
data:
  limit: {{ include "foca-common.maxContentLength" $context | quote }}
"""
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
# Annotations of claims that Helm keeps when the release is uninstalled
KEEP = {"helm.sh/resource-policy": "keep"}

pytestmark = pytest.mark.skipif(HELM is None, reason="helm not installed")


def _workload(docs: Docs, name: str) -> Manifest:
    """The Deployment, or StatefulSet (the broker), of that name."""
    kind = "StatefulSet" if ("StatefulSet", name) in docs else "Deployment"
    return docs[(kind, name)]


def _container(docs: Docs, name: str = APP) -> dict[str, Any]:
    container: dict[str, Any] = _pod_spec(docs, name)["containers"][0]
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


def _example_values() -> dict[str, Any]:
    """Values of the petstore example's chart."""
    values: dict[str, Any] = yaml.safe_load((APP_CHART / "values.yaml").read_text())
    return values


def _pod_spec(docs: Docs, name: str = APP) -> Manifest:
    spec: Manifest = _workload(docs, name)["spec"]["template"]["spec"]
    return spec


# Resources


def test_default_resources(render: Callable[..., Docs]) -> None:
    """Default values render the expected resources."""
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
    """Resources carry the recommended labels."""
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
def test_names(chart: Path, release: str, args: tuple[str, ...], app: str) -> None:
    """Resource names follow the release and name overrides."""
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
    """Names that would collide after truncation fail."""
    long_name = "a" * 55 + "-mongodb"
    error = render_error("--set", f"fullnameOverride={long_name}")
    assert "would both be named" in error


def test_external_mongodb_needs_host(
    render_error: Callable[..., str],
) -> None:
    """An external MongoDB needs a database host."""
    error = render_error("--set", "mongodb.enabled=false")
    assert "config.db.host must be set" in error


def test_settings_must_be_yaml(
    chart_with_settings: Callable[[str], Path],
) -> None:
    """Settings that are not valid YAML fail."""
    chart = chart_with_settings("valuesKey: [")
    assert "settings are not valid YAML" in template_error(chart)


def test_mongodb_disabled(render: Callable[..., Docs]) -> None:
    """Without the bundled MongoDB, only the app renders."""
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
    """Settings without valuesKey fail."""
    chart = chart_with_settings("containerName: x")
    assert re.search("must set valuesKey", template_error(chart))


def test_values_key_must_be_map(
    chart_with_settings: Callable[[str], Path],
) -> None:
    """A valuesKey that names no map fails."""
    chart = chart_with_settings("valuesKey: store")
    assert re.search("store must be a map", template_error(chart))


def test_all_settings(chart_with_settings: Callable[[str], Path]) -> None:
    """All settings of the app take effect."""
    chart = chart_with_settings(FULL_SETTINGS)
    (chart / "templates" / "limit.yaml").write_text(LIMIT_TEMPLATE)
    docs = index(template(chart, *AUDIENCE))
    container = _container(docs)
    assert container["name"] == "store"
    assert _env(container)["FOCA_CONFIG_OVERLAY"] == "/etc/store/overlay.yaml"
    assert container["volumeMounts"][0]["mountPath"] == "/etc/store"
    overlay = _overlay(docs)
    assert overlay["custom"] == {"public_url": "https://petstore.example.org/"}
    assert docs[("ConfigMap", "limit")]["data"] == {"limit": "1048576"}
    # The size limit sets no annotation of a particular ingress controller
    assert "annotations" not in docs[("Ingress", APP)]["metadata"]
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
        # issuers without audiences of their own
        {"security": {"auth": {"issuers": ["https://a.example.org"]}}},
        {"security": {"auth": {"issuers": [{"issuer": "https://a.example.org"}]}}},
        {
            "security": {
                "auth": {
                    "issuers": [
                        {"issuer": "https://a.example.org", "audience": ["a"]},
                        {"issuer": "https://b.example.org"},
                    ]
                }
            }
        },
        {
            "security": {
                "auth": {
                    "issuers": [
                        {"issuer": "https://a.example.org", "audience": ["a"]},
                        "https://b.example.org",
                    ]
                }
            }
        },
        # blank or empty audiences of issuers, which FOCA refuses
        {
            "security": {
                "auth": {
                    "issuers": [{"issuer": "https://a.example.org", "audience": []}]
                }
            }
        },
        {
            "security": {
                "auth": {
                    "audience": ["petstore"],
                    "issuers": [{"issuer": "https://a.example.org", "audience": [" "]}],
                }
            }
        },
        {
            "security": {
                "auth": {
                    "issuers": [
                        {"issuer": "https://a.example.org", "audience": "a"},
                    ]
                }
            }
        },
        # blank global audience, even if no issuer needs it
        {
            "security": {
                "auth": {
                    "audience": [" "],
                    "issuers": [{"issuer": "https://a.example.org", "audience": ["a"]}],
                }
            }
        },
    ],
)
def test_audience_required(
    chart_with_settings: Callable[[str], Path],
    tmp_path: Path,
    config: dict[str, Any],
) -> None:
    """A missing or empty audience fails when required."""
    chart = chart_with_settings(FULL_SETTINGS)
    # A values file, as `--set` removes keys set to `null`
    values = tmp_path / "values.yaml"
    values.write_text(yaml.safe_dump({"config": config}))
    error = template_error(chart, "-f", str(values))
    assert "must list" in error


@pytest.mark.parametrize(
    "auth",
    [
        {"audience": ["petstore"]},
        {"audience": ["petstore"], "issuers": ["https://a.example.org"]},
        # the global audience applies to issuers without their own
        {
            "audience": ["petstore"],
            "issuers": [
                {"issuer": "https://a.example.org", "audience": ["a"]},
                {"issuer": "https://b.example.org"},
                {"issuer": "https://c.example.org", "audience": None},
            ],
        },
        # issuers with audiences of their own need no global one
        {
            "issuers": [
                {"issuer": "https://a.example.org", "audience": ["a"]},
                {"issuer": "https://b.example.org", "audience": ["b", "c"]},
            ]
        },
        {
            "audience": [],
            "issuers": [{"issuer": "https://a.example.org", "audience": ["a"]}],
        },
        {
            "audience": None,
            "issuers": [{"issuer": "https://a.example.org", "audience": ["a"]}],
        },
        # removes the issuers of the base config
        {"issuers": []},
    ],
)
def test_audience_accepted(
    chart_with_settings: Callable[[str], Path],
    tmp_path: Path,
    auth: dict[str, Any],
) -> None:
    """A global audience or audiences of all issuers are enough."""
    chart = chart_with_settings(FULL_SETTINGS)
    values = tmp_path / "values.yaml"
    values.write_text(yaml.safe_dump({"config": {"security": {"auth": auth}}}))
    template(chart, "-f", str(values))


@pytest.mark.parametrize("audience", [[], [" "], "a"])
def test_issuer_audience_invalid_also_without_validation(
    chart_with_settings: Callable[[str], Path], tmp_path: Path, audience: Any
) -> None:
    """Audiences of issuers must be valid, as FOCA checks them in any case."""
    chart = chart_with_settings(FULL_SETTINGS)
    values = tmp_path / "values.yaml"
    auth = {
        "verify_audience": False,
        "issuers": [{"issuer": "https://a.example.org", "audience": audience}],
    }
    values.write_text(yaml.safe_dump({"config": {"security": {"auth": auth}}}))
    assert "must list non-blank client IDs" in template_error(chart, "-f", str(values))


def test_audience_validation_disabled_with_issuers(
    chart_with_settings: Callable[[str], Path], tmp_path: Path
) -> None:
    """Disabling the audience validation also covers issuers."""
    chart = chart_with_settings(FULL_SETTINGS)
    values = tmp_path / "values.yaml"
    auth = {"verify_audience": False, "issuers": ["https://a.example.org"]}
    values.write_text(yaml.safe_dump({"config": {"security": {"auth": auth}}}))
    template(chart, "-f", str(values))


@pytest.mark.parametrize("probe", ["startupProbe", "readinessProbe", "livenessProbe"])
def test_app_probes_default(render: Callable[..., Docs], probe: str) -> None:
    """By default, probes request the probe path."""
    container = _container(render())
    assert container[probe]["httpGet"] == {"path": "/openapi.json", "port": 8080}


@pytest.mark.parametrize("probe", ["startupProbe", "readinessProbe", "livenessProbe"])
def test_app_probe_override(render: Callable[..., Docs], probe: str) -> None:
    """A probe that is set replaces the default entirely, the others stay."""
    defaults = _container(render())
    override = {"tcpSocket": {"port": 8080}, "periodSeconds": 7}
    container = _container(
        render(
            "--set-json",
            f"petstore.{probe}={json.dumps(override)}",
            "--set",
            "petstore.probePath=/health",
        )
    )
    assert container[probe] == override
    for other in {"startupProbe", "readinessProbe", "livenessProbe"} - {probe}:
        expected = {**defaults[other], "httpGet": {"path": "/health", "port": 8080}}
        assert container[other] == expected


def test_app_probe_empty_keeps_default(render: Callable[..., Docs]) -> None:
    """An empty map keeps the default probe."""
    container = _container(render("--set-json", "petstore.livenessProbe={}"))
    assert container["livenessProbe"]["periodSeconds"] == 20


@pytest.mark.parametrize("value", ["false", '"False"', '"off"', '"0"'])
def test_audience_validation_disabled(
    chart_with_settings: Callable[[str], Path], value: str
) -> None:
    """Disabling the audience validation lifts the audience requirement."""
    chart = chart_with_settings(FULL_SETTINGS)
    template(chart, "--set-json", f"config.security.auth.verify_audience={value}")


def test_audience_not_required_by_default(
    render: Callable[..., Docs],
) -> None:
    """Without requireAudience, no audience is needed."""
    render()


def test_init_script_needs_databases(
    chart_with_settings: Callable[[str], Path],
) -> None:
    """MongoDB authentication needs the app's databases."""
    chart = chart_with_settings("valuesKey: petstore")
    assert re.search(r"mongodb\.databases", template_error(chart))
    template(chart, "--set", "mongodb.auth.enabled=false")


ACCESS_CONTROL_SETTINGS = """
valuesKey: petstore
mongodb:
  databases: [petstore]
  accessControlDatabase: policies
"""


def _roles(docs: Docs) -> list[str]:
    """Databases of the app user's roles in the init script, in order."""
    script = docs[("ConfigMap", INIT_SCRIPT)]["data"]["create-app-user.js"]
    assert "db.getSiblingDB('petstore').createUser" in script
    return re.findall(r"\{ role: 'readWrite', db: '([^']*)' \}", script)


def test_init_script_without_access_control(render: Callable[..., Docs]) -> None:
    """Without access control, the app user may only use the app's databases."""
    assert _roles(render()) == ["petstore"]


def test_init_script_access_control_setting(
    chart_with_settings: Callable[[str], Path],
) -> None:
    """The access-control database of the settings is granted after the app's."""
    chart = chart_with_settings(ACCESS_CONTROL_SETTINGS)
    assert _roles(index(template(chart))) == ["petstore", "policies"]


def test_init_script_access_control_overlay_db_name(
    chart_with_settings: Callable[[str], Path],
) -> None:
    """The overlay's access-control database takes precedence."""
    chart = chart_with_settings(ACCESS_CONTROL_SETTINGS)
    docs = index(
        template(chart, "--set", "config.security.access_control.db_name=rules")
    )
    assert _roles(docs) == ["petstore", "rules"]


def test_init_script_access_control_setting_when_disabled(
    chart_with_settings: Callable[[str], Path],
) -> None:
    """The setting is granted even if the overlay disables access control."""
    chart = chart_with_settings(ACCESS_CONTROL_SETTINGS)
    docs = index(
        template(chart, "--set", "config.security.access_control.enabled=false")
    )
    assert _roles(docs) == ["petstore", "policies"]


def test_init_script_access_control_enabled_in_overlay(
    render: Callable[..., Docs],
) -> None:
    """Access control enabled by the overlay grants FOCA's default database."""
    docs = render("--set", "config.security.access_control.enabled=true")
    assert _roles(docs) == ["petstore", "access_control_db"]
    docs = render("--set", "config.security.access_control.enabled=false")
    assert _roles(docs) == ["petstore"]


def test_init_script_access_control_ci_values(chart: Path) -> None:
    """The access-control installation of CI grants the policy database."""
    values = chart / "ci" / "access-control-values.yaml"
    docs = index(template(chart, "-f", str(values), required=False))
    assert _roles(docs) == ["petstore", "access_control_db"]


@pytest.mark.parametrize(
    ("enabled", "granted"),
    [("Yes", True), ("on", True), (1, True), ("off", False), (0, False)],
)
def test_init_script_access_control_enabled_as_pydantic(
    chart: Path, tmp_path: Path, enabled: object, granted: bool
) -> None:
    """The overlay's `enabled` counts as pydantic parses it."""
    values = tmp_path / "values.yaml"
    access_control = {"enabled": enabled}
    values.write_text(
        yaml.safe_dump({"config": {"security": {"access_control": access_control}}})
    )
    roles = _roles(index(template(chart, "-f", str(values))))
    assert roles == ["petstore", "access_control_db"][: 2 if granted else 1]


def test_init_script_access_control_null_db_name(chart: Path, tmp_path: Path) -> None:
    """A `null` access-control database of the overlay fails."""
    values = tmp_path / "values.yaml"
    values.write_text(
        yaml.safe_dump({"config": {"security": {"access_control": {"db_name": None}}}})
    )
    error = template_error(chart, "-f", str(values))
    assert "not a valid MongoDB database name" in error


def test_init_script_access_control_in_app_database(
    render: Callable[..., Docs],
) -> None:
    """An access-control database that is an app database is granted once."""
    docs = render(
        "--set",
        "config.security.access_control.enabled=true",
        "--set",
        "config.security.access_control.db_name=petstore",
    )
    assert _roles(docs) == ["petstore"]


@pytest.mark.parametrize(
    "settings",
    [
        'mongodb:\n  databases: [petstore]\n  accessControlDatabase: "it\'s"',
        "mongodb:\n  databases: [petstore]\n  accessControlDatabase: 1",
        "mongodb:\n  databases: [petstore]\n  accessControlDatabase: ''",
        "mongodb:\n  databases: [pet.store]",
        'mongodb:\n  databases: [petstore, "a b"]',
        "mongodb:\n  databases: [" + "a" * 64 + "]",
        # Line separators of YAML (in the init script's block scalar), and
        # other non-ASCII characters
        'mongodb:\n  databases: ["a\\u2028b"]',
        'mongodb:\n  databases: ["caf\\u00e9"]',
    ],
)
def test_init_script_invalid_database_names(
    chart_with_settings: Callable[[str], Path], settings: str
) -> None:
    """Database names that MongoDB or the init script can't take fail."""
    chart = chart_with_settings(f"valuesKey: petstore\n{settings}")
    assert "not a valid MongoDB database name" in template_error(chart)


@pytest.mark.parametrize("db_name", ["a/b", "x'y", " "])
def test_init_script_invalid_overlay_db_name(
    render_error: Callable[..., str], db_name: str
) -> None:
    """An invalid access-control database name of the overlay fails."""
    error = render_error(
        "--set-string", f"config.security.access_control.db_name={db_name}"
    )
    assert "not a valid MongoDB database name" in error


# Config overlay


def test_overlay_merges_config(render: Callable[..., Docs]) -> None:
    """The overlay merges config into the defaults."""
    config = {
        "db": {"port": 1234},
        "log": {"handlers": {"console": {"level": 20}}},
    }
    docs = render("--set-json", f"config={json.dumps(config)}")
    assert _overlay(docs) == {
        "db": {"host": MONGODB, "port": 1234},
        "log": {"handlers": {"console": {"level": 20}}},
    }


def test_overlay_config_takes_precedence(render: Callable[..., Docs]) -> None:
    """Config overrides the default overlay."""
    docs = render("--set", "config.db.host=other")
    assert _overlay(docs) == {"db": {"host": "other"}}


@pytest.mark.parametrize("section", ["db", "server"])
def test_overlay_drops_null_sections(render: Callable[..., Docs], section: str) -> None:
    """Sections set to null are dropped."""
    docs = render("--set-json", f"config.{section}=null")
    assert _overlay(docs) == {"db": {"host": MONGODB}}


def test_checksum_follows_config(render: Callable[..., Docs]) -> None:
    """The config checksum changes only with config."""
    default = _checksum(render())
    assert _checksum(render()) == default
    assert _checksum(render("--set", "config.log.root.level=10")) != default
    # Other values don't roll the pods
    assert _checksum(render("--set", "petstore.tag=1.0.0")) == default


# App Deployment


def test_container(render: Callable[..., Docs]) -> None:
    """The app container has the expected defaults."""
    docs = render()
    container = _container(docs)
    assert container["image"] == "foca-petstore:latest"
    assert container["imagePullPolicy"] == "IfNotPresent"
    assert container["name"] == "petstore"
    env = _env(container)
    assert env["FOCA_CONFIG_OVERLAY"] == "/etc/petstore/overlay.yaml"
    assert env["GUNICORN_CMD_ARGS"] == (
        "--workers 1 --threads 4 --timeout 30 --graceful-timeout 20 --keep-alive 100"
    )
    for var, key in [
        ("MONGO_USERNAME", "username"),
        ("MONGO_PASSWORD", "password"),
    ]:
        assert env[var] == {"secretKeyRef": {"name": "mongodb-credentials", "key": key}}
    for probe in ("startupProbe", "readinessProbe", "livenessProbe"):
        assert container[probe]["httpGet"] == {"path": "/openapi.json", "port": 8080}
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
    """Image references are built from image and tag."""
    assert _container(render(*args))["image"] == image


def test_image_with_tag_and_tag_fails(render_error: Callable[..., str]) -> None:
    """A tag in both image and tag fails."""
    error = render_error(
        "--set", "petstore.image=pets:2", "--set-string", "petstore.tag=3"
    )
    assert "petstore.tag must not be set" in error


def test_image_pull_policy(render: Callable[..., Docs]) -> None:
    """The pull policy follows the values."""
    docs = render("--set", "petstore.imagePullPolicy=Never")
    assert _container(docs)["imagePullPolicy"] == "Never"
    docs = render("--set-json", "petstore.imagePullPolicy=null")
    assert _container(docs)["imagePullPolicy"] == "Always"


def test_without_gunicorn(render: Callable[..., Docs]) -> None:
    """Without Gunicorn, its settings are left out."""
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
    """Extra Gunicorn arguments are shell-quoted."""
    docs = render("--set-json", 'petstore.gunicorn.extraArgs=["--access-logfile", "-"]')
    assert _env(_container(docs))["GUNICORN_CMD_ARGS"] == (
        "--workers 1 --threads 4 --timeout 30 --graceful-timeout 20 "
        "--keep-alive 100 '--access-logfile' '-'"
    )


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
    """Extra Gunicorn arguments that the chart sets fail."""
    error = render_error(
        "--set-json", f"petstore.gunicorn.extraArgs={json.dumps([arg])}"
    )
    assert "petstore.gunicorn.extraArgs must not set" in error


@pytest.mark.parametrize("arg", ["--bind x", "--backlog=1", "-D"])
def test_gunicorn_other_args_pass(render: Callable[..., Docs], arg: str) -> None:
    """Other extra Gunicorn arguments pass."""
    render("--set-json", f"petstore.gunicorn.extraArgs={json.dumps([arg])}")


@pytest.mark.parametrize(
    ("value", "size"),
    [("null", None), ("1000000", "1000000"), ('"1Gi"', "1Gi")],
)
def test_tmp_size_limit(
    render: Callable[..., Docs], value: str, size: str | None
) -> None:
    """The size limit of /tmp follows the values."""
    docs = render("--set-json", f"petstore.tmpSizeLimit={value}")
    expected = {"sizeLimit": size} if size else {}
    assert _pod_spec(docs)["volumes"][1]["emptyDir"] == expected


def test_security_contexts(render: Callable[..., Docs]) -> None:
    """Pods run as non-root with fixed IDs."""
    docs = render()
    assert _pod_spec(docs)["securityContext"]["runAsNonRoot"] is True
    mongodb = _pod_spec(docs, MONGODB)["securityContext"]
    assert mongodb["runAsUser"] == 999
    assert mongodb["fsGroup"] == 999


def test_security_contexts_openshift(render: Callable[..., Docs]) -> None:
    """On OpenShift, pods leave the IDs unset."""
    docs = render(*OPENSHIFT_API)
    for name in (APP, MONGODB):
        context = _pod_spec(docs, name)["securityContext"]
        assert not {"runAsUser", "runAsGroup", "fsGroup"} & set(context)
        assert context["runAsNonRoot"] is True


def test_mongodb_without_auth(render: Callable[..., Docs]) -> None:
    """Without MongoDB authentication, no credentials are used."""
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
    """MongoDB authentication needs an existing Secret."""
    error = render_error("--set", "hostName=x", required=False)
    assert "mongodb.auth.existingSecret must be set" in error


def test_mongodb_credentials(render: Callable[..., Docs]) -> None:
    """MongoDB gets its credentials from the Secret."""
    env = _env(_container(render(), MONGODB))
    assert {var: ref["secretKeyRef"]["key"] for var, ref in env.items()} == {
        "MONGO_APP_PASSWORD": "password",
        "MONGO_APP_USERNAME": "username",
        "MONGO_INITDB_ROOT_PASSWORD": "root-password",
        "MONGO_INITDB_ROOT_USERNAME": "root-username",
    }


def test_probe_path(render: Callable[..., Docs]) -> None:
    """All probes use the probe path."""
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
    """The grace period covers the preStop sleep and Gunicorn's timeout."""
    spec = _pod_spec(render(*args))
    assert spec["terminationGracePeriodSeconds"] == seconds


def test_gunicorn_settings(render: Callable[..., Docs]) -> None:
    """Gunicorn settings become command-line arguments."""
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
    """Services expose and select their Deployments."""
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
    """The database host is the bundled MongoDB's Service."""
    docs = render("--set", "fullnameOverride=shop")
    overlay = yaml.safe_load(docs[("ConfigMap", "shop-config")]["data"]["overlay.yaml"])
    assert overlay["db"]["host"] == "shop-mongodb"
    assert ("Service", "shop-mongodb") in docs


def test_mongodb_settings(render: Callable[..., Docs]) -> None:
    """MongoDB settings take effect."""
    docs = render(
        "--set",
        "mongodb.persistence.size=5Gi",
        "--set",
        "mongodb.image=mongo:7.0.1",
        "--set",
        "mongodb.imagePullPolicy=Never",
    )
    pvc = docs[("PersistentVolumeClaim", MONGODB)]["spec"]
    assert pvc["resources"]["requests"]["storage"] == "5Gi"
    assert pvc["accessModes"] == ["ReadWriteOnce"]
    container = _container(docs, MONGODB)
    assert container["image"] == "mongo:7.0.1"
    assert container["imagePullPolicy"] == "Never"
    default = _container(render(), MONGODB)
    assert default["image"] == _example_values()["mongodb"]["image"]
    assert default["imagePullPolicy"] == "IfNotPresent"
    docs = render("--set-json", "mongodb.imagePullPolicy=null")
    assert _container(docs, MONGODB)["imagePullPolicy"] == "Always"


def _mongodb_volume(docs: Docs) -> dict[str, Any]:
    """The data volume of MongoDB's pod (without its name)."""
    (volume,) = [
        volume
        for volume in _pod_spec(docs, MONGODB)["volumes"]
        if volume["name"] == "mongodb"
    ]
    return {key: value for key, value in volume.items() if key != "name"}


def test_mongodb_persistence_default(render: Callable[..., Docs]) -> None:
    """MongoDB keeps its data in its own claim, named as up to 0.1.0."""
    docs = render()
    claim = docs[("PersistentVolumeClaim", MONGODB)]
    assert claim["metadata"]["labels"]["app.kubernetes.io/component"] == "mongodb"
    # Kept by Helm when the release is uninstalled
    assert claim["metadata"]["annotations"] == KEEP
    assert claim["spec"] == {
        "accessModes": ["ReadWriteOnce"],
        "resources": {"requests": {"storage": "1Gi"}},
    }
    assert _mongodb_volume(docs) == {"persistentVolumeClaim": {"claimName": MONGODB}}


def test_mongodb_persistence_settings(render: Callable[..., Docs]) -> None:
    """The size, access modes and storage class of MongoDB's claim take effect."""
    docs = render(
        "--set",
        "mongodb.persistence.size=5Gi",
        "--set-json",
        'mongodb.persistence.accessModes=["ReadWriteOncePod"]',
        "--set",
        "mongodb.persistence.storageClass=fast",
    )
    assert docs[("PersistentVolumeClaim", MONGODB)]["spec"] == {
        "accessModes": ["ReadWriteOncePod"],
        "resources": {"requests": {"storage": "5Gi"}},
        "storageClassName": "fast",
    }
    docs = render("--set", "mongodb.persistence.storageClass=-")
    spec = docs[("PersistentVolumeClaim", MONGODB)]["spec"]
    assert spec["storageClassName"] == ""


@pytest.mark.parametrize(
    ("args", "annotations"),
    [
        (("--set", "mongodb.persistence.resourcePolicy="), None),
        (("--set-json", "mongodb.persistence.resourcePolicy=null"), KEEP),
    ],
    ids=["empty", "not-set"],
)
def test_mongodb_resource_policy(
    render: Callable[..., Docs],
    args: tuple[str, ...],
    annotations: dict[str, str] | None,
) -> None:
    """An empty resource policy lets Helm delete the claim; keep if not set."""
    metadata = render(*args)[("PersistentVolumeClaim", MONGODB)]["metadata"]
    assert metadata.get("annotations") == annotations


def test_mongodb_existing_claim(render: Callable[..., Docs]) -> None:
    """An existing claim replaces MongoDB's own."""
    docs = render("--set", "mongodb.persistence.existingClaim=restored")
    assert ("PersistentVolumeClaim", MONGODB) not in docs
    assert _mongodb_volume(docs) == {"persistentVolumeClaim": {"claimName": "restored"}}


@pytest.mark.parametrize("existing_claim", ["", "restored"])
def test_mongodb_without_persistence(
    render: Callable[..., Docs], existing_claim: str
) -> None:
    """Without persistence, MongoDB keeps its data in an emptyDir of the size."""
    docs = render(
        "--set",
        "mongodb.persistence.enabled=false",
        "--set",
        f"mongodb.persistence.existingClaim={existing_claim}",
    )
    assert ("PersistentVolumeClaim", MONGODB) not in docs
    assert _mongodb_volume(docs) == {"emptyDir": {"sizeLimit": "1Gi"}}


def _chart_without_schema(chart: Path, tmp_path: Path) -> Path:
    """A copy of the chart without its schema (as with older app charts)."""
    chart_copy = tmp_path / "chart"
    shutil.copytree(chart, chart_copy)
    (chart_copy / "values.schema.json").unlink()
    return chart_copy


@pytest.mark.parametrize(
    ("volume_size", "storage"),
    [("5Gi", "5Gi"), (5368709120, 5368709120)],
)
def test_mongodb_volume_size_alias(
    chart: Path, tmp_path: Path, volume_size: str | int, storage: str | int
) -> None:
    """Values of 0.1.0 (`volumeSize`, no `persistence`) keep the same claim."""
    chart_copy = _chart_without_schema(chart, tmp_path)
    docs = index(
        template(
            chart_copy,
            "--set-json",
            "mongodb.persistence=null",
            "--set-json",
            f"mongodb.volumeSize={json.dumps(volume_size)}",
        )
    )
    claim = docs[("PersistentVolumeClaim", MONGODB)]
    # Kept on uninstall, as values of 0.1.0 have no resource policy
    assert claim["metadata"]["annotations"] == KEEP
    spec = claim["spec"]
    assert spec == {
        "accessModes": ["ReadWriteOnce"],
        "resources": {"requests": {"storage": storage}},
    }
    # Numbers of bytes are rendered as integers, not in scientific notation
    assert type(spec["resources"]["requests"]["storage"]) is type(storage)
    assert _mongodb_volume(docs) == {"persistentVolumeClaim": {"claimName": MONGODB}}


def test_mongodb_volume_size_sets_size(render: Callable[..., Docs]) -> None:
    """The deprecated `volumeSize` sets the size of the claim and the emptyDir."""
    alias = ("--set-json", "mongodb.persistence.size=null")
    docs = render(*alias, "--set", "mongodb.volumeSize=5Gi")
    spec = docs[("PersistentVolumeClaim", MONGODB)]["spec"]
    assert spec["resources"]["requests"]["storage"] == "5Gi"
    docs = render(
        *alias,
        "--set",
        "mongodb.volumeSize=5Gi",
        "--set",
        "mongodb.persistence.enabled=false",
    )
    assert _mongodb_volume(docs) == {"emptyDir": {"sizeLimit": "5Gi"}}
    # Without a size (`volumeSize` unset with null), the emptyDir has no limit
    docs = render(
        *alias,
        "--set-json",
        "mongodb.volumeSize=null",
        "--set",
        "mongodb.persistence.enabled=false",
    )
    assert _mongodb_volume(docs) == {"emptyDir": {}}


def test_mongodb_volume_size_with_persistence_size_fails(
    render_error: Callable[..., str],
) -> None:
    """The deprecated `volumeSize` fails with `persistence.size`."""
    error = render_error("--set", "mongodb.volumeSize=5Gi")
    assert "mongodb.persistence.size and the deprecated mongodb.volumeSize" in error
    assert "--set-json mongodb.volumeSize=null" in error


def test_mongodb_persistence_size_required(chart: Path, tmp_path: Path) -> None:
    """MongoDB's claim needs a size."""
    # Bypasses the schema, which rejects an empty size already
    chart_copy = _chart_without_schema(chart, tmp_path)
    error = template_error(chart_copy, "--set", "mongodb.persistence.size=")
    assert "mongodb.persistence.size must be set" in error


def test_schema_rejects_invalid_mongodb_persistence(
    render_error: Callable[..., str],
) -> None:
    """The schema rejects invalid persistence values of MongoDB."""
    assert "schema" in render_error("--set", "mongodb.persistence.size=0Gi")
    assert "schema" in render_error("--set", "mongodb.persistence.size=1x")
    assert "schema" in render_error(
        "--set-json", 'mongodb.persistence.accessModes=["ReadOnlyMany"]'
    )
    assert "schema" in render_error("--set", "mongodb.persistence.unknown=true")
    assert "schema" in render_error("--set", "mongodb.volumeSize=0Gi")
    assert "schema" in render_error("--set", "mongodb.persistence.resourcePolicy=no")
    assert "schema" in render_error("--set", "mongodb.volumeSize=1x")


def test_mongodb_values_required(
    chart_with_settings: Callable[[str], Path],
) -> None:
    """Missing mongodb values fail."""
    chart = chart_with_settings("valuesKey: petstore")
    (chart / "values.schema.json").unlink()
    values = yaml.safe_load((chart / "values.yaml").read_text())
    del values["mongodb"]
    (chart / "values.yaml").write_text(yaml.safe_dump(values))
    error = template_error(chart, "--set", "hostName=x", required=False)
    assert "mongodb and mongodb.auth must be maps" in error


# Replicas and PodDisruptionBudget

REPLICAS = ("--set", "petstore.replicas=3")


def test_single_replica_by_default(render: Callable[..., Docs]) -> None:
    """One pod by default, without a PodDisruptionBudget."""
    docs = render()
    assert docs[("Deployment", APP)]["spec"]["replicas"] == 1
    assert ("PodDisruptionBudget", APP) not in docs


def test_replicas(render: Callable[..., Docs]) -> None:
    """Several pods get a PodDisruptionBudget evicting one at a time."""
    docs = render(*REPLICAS)
    deployment = docs[("Deployment", APP)]
    assert deployment["spec"]["replicas"] == 3
    budget = docs[("PodDisruptionBudget", APP)]
    assert budget["apiVersion"] == "policy/v1"
    assert budget["metadata"]["labels"] == deployment["metadata"]["labels"]
    assert budget["spec"] == {
        "maxUnavailable": 1,
        "selector": deployment["spec"]["selector"],
        "unhealthyPodEvictionPolicy": "AlwaysAllow",
    }


@pytest.mark.parametrize(
    ("args", "spec"),
    [
        (("--set", "petstore.podDisruptionBudget.minAvailable=2"), {"minAvailable": 2}),
        (
            ("--set", "petstore.podDisruptionBudget.minAvailable=50%"),
            {"minAvailable": "50%"},
        ),
        (
            ("--set", "petstore.podDisruptionBudget.maxUnavailable=2"),
            {"maxUnavailable": 2},
        ),
        (
            ("--set", "petstore.podDisruptionBudget.maxUnavailable=0"),
            {"maxUnavailable": 0},
        ),
        (
            ("--set", "petstore.podDisruptionBudget.maxUnavailable=25%"),
            {"maxUnavailable": "25%"},
        ),
    ],
)
def test_pod_disruption_budget_settings(
    render: Callable[..., Docs], args: tuple[str, ...], spec: dict[str, Any]
) -> None:
    """`minAvailable` or `maxUnavailable` set the budget."""
    budget = render(*REPLICAS, *args)[("PodDisruptionBudget", APP)]["spec"]
    bounds = {"minAvailable", "maxUnavailable"}
    assert {key: value for key, value in budget.items() if key in bounds} == spec


def test_pod_disruption_budget_disabled(render: Callable[..., Docs]) -> None:
    """No PodDisruptionBudget if disabled, also with several pods."""
    docs = render(*REPLICAS, "--set", "petstore.podDisruptionBudget.enabled=false")
    assert docs[("Deployment", APP)]["spec"]["replicas"] == 3
    assert ("PodDisruptionBudget", APP) not in docs


def test_pod_disruption_budget_needs_replicas(render: Callable[..., Docs]) -> None:
    """No PodDisruptionBudget with one pod, which no budget keeps available."""
    docs = render("--set", "petstore.podDisruptionBudget.enabled=true")
    assert ("PodDisruptionBudget", APP) not in docs


@pytest.mark.parametrize(
    "args",
    [
        ("--set", "petstore.replicas=0"),
        ("--set", "petstore.replicas=1.5"),
        ("--set", "petstore.podDisruptionBudget.minAvailable=-1"),
        ("--set", "petstore.podDisruptionBudget.maxUnavailable=half"),
        ("--set", "petstore.podDisruptionBudget.minAvailable=150%"),
        ("--set", "petstore.podDisruptionBudget.other=1"),
        (
            "--set",
            "petstore.podDisruptionBudget.minAvailable=1",
            "--set",
            "petstore.podDisruptionBudget.maxUnavailable=1",
        ),
    ],
)
def test_replicas_schema(
    render_error: Callable[..., str], args: tuple[str, ...]
) -> None:
    """The schema rejects invalid replicas and budgets."""
    assert "schema" in render_error(*REPLICAS, *args)


def test_pod_disruption_budget_both_fail_without_schema(
    chart: Path, tmp_path: Path
) -> None:
    """Setting both `minAvailable` and `maxUnavailable` fails to render."""
    error = template_error(
        _chart_without_schema(chart, tmp_path),
        *REPLICAS,
        "--set",
        "petstore.podDisruptionBudget.minAvailable=1",
        "--set",
        "petstore.podDisruptionBudget.maxUnavailable=1",
    )
    assert "set either minAvailable or maxUnavailable, not both" in error


def test_replicas_without_values(chart: Path, tmp_path: Path) -> None:
    """Without `replicas` (e.g., values of 0.2.0), there is one pod."""
    docs = index(
        template(
            _chart_without_schema(chart, tmp_path),
            "--set-json",
            "petstore.replicas=null",
            "--set-json",
            "petstore.podDisruptionBudget=null",
        )
    )
    assert docs[("Deployment", APP)]["spec"]["replicas"] == 1
    assert ("PodDisruptionBudget", APP) not in docs


def test_pod_disruption_budget_without_values(chart: Path, tmp_path: Path) -> None:
    """Without `podDisruptionBudget`, several pods get the default budget."""
    docs = index(
        template(
            _chart_without_schema(chart, tmp_path),
            *REPLICAS,
            "--set-json",
            "petstore.podDisruptionBudget=null",
        )
    )
    assert docs[("PodDisruptionBudget", APP)]["spec"]["maxUnavailable"] == 1


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (("--set", "petstore.replicas=0"), "replicas must be a whole number"),
        (("--set", "petstore.replicas=-1"), "replicas must be a whole number"),
        (("--set-string", "petstore.replicas=abc"), "replicas must be a whole number"),
        (("--set-json", "petstore.replicas=1.5"), "replicas must be a whole number"),
        (
            ("--set-string", "petstore.podDisruptionBudget.enabled=false"),
            "podDisruptionBudget.enabled must be true or false",
        ),
        (
            ("--set-json", "petstore.podDisruptionBudget=true"),
            "podDisruptionBudget must be a map",
        ),
        (
            ("--set-json", "petstore.podDisruptionBudget=false"),
            "podDisruptionBudget must be a map",
        ),
        (
            (*REPLICAS, "--set", "petstore.podDisruptionBudget.maxUnavailable=half"),
            "podDisruptionBudget.maxUnavailable must be a whole number",
        ),
        (
            (*REPLICAS, "--set", "petstore.podDisruptionBudget.minAvailable=150%"),
            "podDisruptionBudget.minAvailable must be a whole number",
        ),
        (
            (*REPLICAS, "--set", "petstore.podDisruptionBudget.minAvailable=-1"),
            "podDisruptionBudget.minAvailable must be a whole number",
        ),
        (
            (
                *REPLICAS,
                "--set-json",
                "petstore.podDisruptionBudget.maxUnavailable=1.5",
            ),
            "podDisruptionBudget.maxUnavailable must be a whole number",
        ),
    ],
)
def test_replicas_invalid_without_schema(
    chart: Path, tmp_path: Path, args: tuple[str, ...], message: str
) -> None:
    """Invalid values fail to render also without the schema."""
    error = template_error(_chart_without_schema(chart, tmp_path), *args)
    assert message in error


# Ingress and Route


def test_ingress(render: Callable[..., Docs]) -> None:
    """The default Ingress routes the host without TLS."""
    ingress = render()[("Ingress", APP)]
    assert "annotations" not in ingress["metadata"]
    assert "ingressClassName" not in ingress["spec"]
    assert "tls" not in ingress["spec"]
    (rule,) = ingress["spec"]["rules"]
    assert rule["host"] == "petstore.example.org"


def test_ingress_https(render: Callable[..., Docs]) -> None:
    """HTTPS adds TLS and cert-manager annotations."""
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
    """Ingress annotations merge with the defaults."""
    annotations = {"a": "b", "kubernetes.io/tls-acme": None}
    ingress = render(
        "--set",
        "petstore.ingress.https.enabled=true",
        "--set-json",
        f"petstore.ingress.annotations={json.dumps(annotations)}",
    )[("Ingress", APP)]
    assert ingress["metadata"]["annotations"] == {
        "a": "b",
        "cert-manager.io/cluster-issuer": "letsencrypt-prod",
    }


def test_ingress_body_size_opt_in(render: Callable[..., Docs]) -> None:
    """The app's size limit sets no annotation; controllers' are opt-in."""
    limit = ("--set", "config.server.max_content_length=2048")
    assert "annotations" not in render(*limit)[("Ingress", APP)]["metadata"]
    ingress = render(
        *limit, "--set-json", f'petstore.ingress.annotations={{"{BODY_SIZE}": "1m"}}'
    )[("Ingress", APP)]
    assert ingress["metadata"]["annotations"] == {BODY_SIZE: "1m"}


@pytest.mark.parametrize("value", ["0", "1.5", '"1m"'])
def test_max_content_length_invalid(chart: Path, value: str, tmp_path: Path) -> None:
    """Invalid maximum content lengths fail, also without an Ingress."""
    # Bypasses the schema, which rejects these values already
    chart_copy = tmp_path / "chart"
    shutil.copytree(chart, chart_copy)
    (chart_copy / "values.schema.json").unlink()
    error = template_error(
        chart_copy,
        *NO_INGRESS,
        "--set-json",
        f"config.server.max_content_length={value}",
    )
    assert "must be a positive integer" in error


def test_max_content_length(chart_with_settings: Callable[[str], Path]) -> None:
    """The overlay's size limit takes precedence over that of the settings."""
    chart = chart_with_settings(FULL_SETTINGS)
    (chart / "templates" / "limit.yaml").write_text(LIMIT_TEMPLATE)
    docs = index(
        template(chart, *AUDIENCE, "--set", "config.server.max_content_length=2048")
    )
    assert docs[("ConfigMap", "limit")]["data"] == {"limit": "2048"}


def test_ingress_host_required(render_error: Callable[..., str]) -> None:
    """An Ingress needs a host name."""
    error = render_error("--set", "mongodb.auth.existingSecret=s", required=False)
    assert "hostName must be set" in error


def test_no_ingress_no_route(render: Callable[..., Docs]) -> None:
    """Without an Ingress or Route API, none renders."""
    docs = render(*NO_INGRESS)
    assert not {kind for kind, _ in docs} & {"Ingress", "Route"}


def test_route(render: Callable[..., Docs]) -> None:
    """A Route renders if the API exists and no Ingress does."""
    docs = render(*NO_INGRESS, *ROUTE_API)
    route = docs[("Route", APP)]
    assert route["spec"]["host"] == "petstore.example.org"
    assert route["spec"]["to"]["name"] == APP
    assert route["spec"]["tls"]["termination"] == "edge"
    # An Ingress takes precedence
    assert ("Route", APP) not in render(*ROUTE_API)


def test_route_disabled(render: Callable[..., Docs]) -> None:
    """route.enabled false renders no Route, also on OpenShift."""
    docs = render(*NO_INGRESS, *ROUTE_API, "--set", "petstore.route.enabled=false")
    assert ("Route", APP) not in docs


def test_route_without_enabled(render: Callable[..., Docs]) -> None:
    """Values without route.enabled (foca-common 0.1.0) keep the Route."""
    docs = render(*NO_INGRESS, *ROUTE_API, "--set-json", "petstore.route=null")
    assert ("Route", APP) in docs


def test_route_annotations(render: Callable[..., Docs]) -> None:
    """Route annotations are passed on."""
    annotations = {"haproxy.router.openshift.io/rate-limit-connections": "true"}
    docs = render(
        *NO_INGRESS,
        *ROUTE_API,
        "--set-json",
        f"petstore.route.annotations={json.dumps(annotations)}",
    )
    assert docs[("Route", APP)]["metadata"]["annotations"] == annotations
    assert (
        "annotations" not in render(*NO_INGRESS, *ROUTE_API)[("Route", APP)]["metadata"]
    )


# HTTPRoute (Gateway API)


HTTP_ROUTE = "petstore"
REDIRECT_ROUTE = "petstore-https-redirect"
HTTPS_LISTENER = {"name": "gateway", "namespace": "gateway", "sectionName": "https"}
HTTP_LISTENER = {"name": "gateway", "namespace": "gateway", "sectionName": "http"}
GATEWAY = (
    *NO_INGRESS,
    "--set",
    "petstore.httpRoute.enabled=true",
    "--set-json",
    f"petstore.httpRoute.parentRefs=[{json.dumps(HTTPS_LISTENER)}]",
)
HTTPS_REDIRECT = (
    "--set",
    "petstore.httpRoute.httpsRedirect.enabled=true",
    "--set-json",
    f"petstore.httpRoute.httpsRedirect.parentRefs=[{json.dumps(HTTP_LISTENER)}]",
)


def test_http_route(render: Callable[..., Docs]) -> None:
    """An HTTPRoute routes the host to the app's Service via the Gateway."""
    docs = render(*GATEWAY, *ROUTE_API)
    assert not {kind for kind, _ in docs} & {"Ingress", "Route"}
    route = docs[("HTTPRoute", HTTP_ROUTE)]
    assert route["apiVersion"] == "gateway.networking.k8s.io/v1"
    assert "annotations" not in route["metadata"]
    assert route["metadata"]["labels"]["app.kubernetes.io/component"] == "app"
    assert route["spec"] == {
        "parentRefs": [HTTPS_LISTENER],
        "hostnames": ["petstore.example.org"],
        "rules": [
            {
                "matches": [{"path": {"type": "PathPrefix", "value": "/"}}],
                "backendRefs": [{"name": APP, "port": 8080}],
            }
        ],
    }
    assert ("HTTPRoute", REDIRECT_ROUTE) not in docs


def test_http_route_disabled_by_default(render: Callable[..., Docs]) -> None:
    """Without httpRoute values, no HTTPRoute renders."""
    assert "HTTPRoute" not in {kind for kind, _ in render()}
    assert "HTTPRoute" not in {
        kind for kind, _ in render("--set-json", "petstore.httpRoute=null")
    }


def test_http_route_annotations_and_filters(render: Callable[..., Docs]) -> None:
    """Annotations and filters of the HTTPRoute are passed on."""
    filters = [
        {
            "type": "ExtensionRef",
            "extensionRef": {
                "group": "traefik.io",
                "kind": "Middleware",
                "name": "limits",
            },
        }
    ]
    docs = render(
        *GATEWAY,
        "--set",
        "petstore.httpRoute.annotations.a=b",
        "--set-json",
        f"petstore.httpRoute.filters={json.dumps(filters)}",
    )
    route = docs[("HTTPRoute", HTTP_ROUTE)]
    assert route["metadata"]["annotations"] == {"a": "b"}
    (rule,) = route["spec"]["rules"]
    assert rule["filters"] == filters


def test_http_route_needs_parent_refs(render_error: Callable[..., str]) -> None:
    """An HTTPRoute needs a Gateway to attach to."""
    error = render_error(*NO_INGRESS, "--set", "petstore.httpRoute.enabled=true")
    assert "httpRoute.parentRefs must list at least one" in error


def test_http_route_host_required(render_error: Callable[..., str]) -> None:
    """An HTTPRoute needs a host name."""
    error = render_error(
        *GATEWAY, "--set", "mongodb.auth.existingSecret=s", required=False
    )
    assert "hostName must be set" in error


def test_http_route_excludes_ingress(render_error: Callable[..., str]) -> None:
    """An HTTPRoute and an Ingress are mutually exclusive."""
    error = render_error(
        "--set",
        "petstore.httpRoute.enabled=true",
        "--set-json",
        f"petstore.httpRoute.parentRefs=[{json.dumps(HTTPS_LISTENER)}]",
    )
    assert "ingress.enabled and petstore.httpRoute.enabled" in error


def test_https_redirect_route(render: Callable[..., Docs]) -> None:
    """A second HTTPRoute redirects HTTP to HTTPS."""
    docs = render(*GATEWAY, *HTTPS_REDIRECT)
    route = docs[("HTTPRoute", REDIRECT_ROUTE)]
    assert route["metadata"]["labels"]["app.kubernetes.io/component"] == "app"
    assert route["spec"] == {
        "parentRefs": [HTTP_LISTENER],
        "hostnames": ["petstore.example.org"],
        "rules": [
            {
                "filters": [
                    {
                        "type": "RequestRedirect",
                        "requestRedirect": {"scheme": "https", "statusCode": 301},
                    }
                ],
            }
        ],
    }
    # Only with the app's HTTPRoute
    docs = render(*HTTPS_REDIRECT)
    assert "HTTPRoute" not in {kind for kind, _ in docs}


def test_https_redirect_needs_parent_refs(render_error: Callable[..., str]) -> None:
    """The redirect needs the Gateway's HTTP listener."""
    error = render_error(
        *GATEWAY, "--set", "petstore.httpRoute.httpsRedirect.enabled=true"
    )
    assert "httpsRedirect.parentRefs must list at least one" in error


def test_https_redirect_parent_refs_differ(render_error: Callable[..., str]) -> None:
    """The redirect must not attach where the app's HTTPRoute does."""
    error = render_error(
        *GATEWAY,
        "--set",
        "petstore.httpRoute.httpsRedirect.enabled=true",
        "--set-json",
        f"petstore.httpRoute.httpsRedirect.parentRefs=[{json.dumps(HTTPS_LISTENER)}]",
    )
    assert "would redirect all requests" in error


@pytest.mark.parametrize(
    ("app_ref", "redirect_ref", "overlap"),
    [
        # All listeners of the same Gateway
        ({"name": "gateway"}, {"name": "gateway", "sectionName": "http"}, True),
        ({"name": "gateway", "sectionName": "https"}, {"name": "gateway"}, True),
        # The release's namespace is the default
        (
            {"name": "gateway", "port": 443},
            {"name": "gateway", "namespace": "default", "port": 443},
            True,
        ),
        (
            {"name": "gateway", "sectionName": "https"},
            {"name": "gateway", "sectionName": "https", "port": 443},
            True,
        ),
        # A section name and a port may select the same listener
        (
            {"name": "gateway", "sectionName": "https"},
            {"name": "gateway", "port": 443},
            True,
        ),
        # Other listeners or Gateways
        ({"name": "gateway", "port": 443}, {"name": "gateway", "port": 80}, False),
        (
            {"name": "gateway", "sectionName": "https", "port": 443},
            {"name": "gateway", "sectionName": "http"},
            False,
        ),
        ({"name": "gateway"}, {"name": "gateway", "namespace": "other"}, False),
        ({"name": "gateway"}, {"name": "other"}, False),
        (
            {"name": "gateway"},
            {"name": "gateway", "kind": "ListenerSet"},
            False,
        ),
    ],
)
def test_https_redirect_overlap(
    chart: Path,
    app_ref: dict[str, Any],
    redirect_ref: dict[str, Any],
    overlap: bool,
) -> None:
    """The redirect fails only on listeners that the app's route may use."""
    args = (
        *NO_INGRESS,
        "--namespace",
        "default",
        "--set",
        "petstore.httpRoute.enabled=true",
        "--set-json",
        f"petstore.httpRoute.parentRefs=[{json.dumps(app_ref)}]",
        "--set",
        "petstore.httpRoute.httpsRedirect.enabled=true",
        "--set-json",
        f"petstore.httpRoute.httpsRedirect.parentRefs=[{json.dumps(redirect_ref)}]",
    )
    if overlap:
        assert "would redirect all requests" in template_error(chart, *args)
    else:
        assert ("HTTPRoute", REDIRECT_ROUTE) in index(template(chart, *args))


def test_https_redirect_name_collision_fails(render_error: Callable[..., str]) -> None:
    """Names that would collide with the redirect's fail."""
    suffix = "-https-redirect"
    long_name = "a" * (63 - len(suffix)) + suffix
    error = render_error(
        *GATEWAY, *HTTPS_REDIRECT, "--set", f"fullnameOverride={long_name}"
    )
    assert "would both be named" in error


@pytest.mark.parametrize(
    "args",
    [
        ("--set-json", 'petstore.httpRoute.parentRefs=[{"namespace": "x"}]'),
        ("--set-json", 'petstore.httpRoute.parentRefs=[{"name": "x", "y": "z"}]'),
        ("--set-json", 'petstore.httpRoute.filters=[{"requestRedirect": {}}]'),
        ("--set", "petstore.httpRoute.unknown=true"),
        ("--set", "petstore.route.unknown=true"),
        ("--set", "petstore.route.enabled=maybe"),
    ],
)
def test_schema_rejects_invalid_http_route_values(
    render_error: Callable[..., str], args: tuple[str, ...]
) -> None:
    """The schema rejects invalid HTTPRoute and Route values."""
    assert "schema" in render_error(*args)


# NetworkPolicies


def test_app_network_policy(render: Callable[..., Docs]) -> None:
    """The app's NetworkPolicy admits the given peers."""
    docs = render(
        "--set",
        "petstore.networkPolicy.enabled=true",
        "--set-json",
        f"petstore.networkPolicy.ingressFrom={json.dumps(PEERS)}",
    )
    policy = docs[("NetworkPolicy", APP)]["spec"]
    assert policy["podSelector"]["matchLabels"]["app.kubernetes.io/component"] == "app"
    (rule,) = policy["ingress"]
    assert rule["from"] == PEERS
    assert rule["ports"] == [{"port": 8080, "protocol": "TCP"}]


@pytest.mark.parametrize(
    "peers",
    [
        (),
        ("--set-json", "petstore.networkPolicy.ingressFrom=[]"),
        ("--set-json", "petstore.networkPolicy.ingressFrom=null"),
    ],
    ids=["default", "empty", "null"],
)
def test_app_network_policy_needs_peers(
    render_error: Callable[..., str], peers: tuple[str, ...]
) -> None:
    """The app's NetworkPolicy has no default peers and needs at least one."""
    error = render_error("--set", "petstore.networkPolicy.enabled=true", *peers)
    assert "ingressFrom must list at least one" in error


def test_mongodb_network_policy(render: Callable[..., Docs]) -> None:
    """MongoDB's NetworkPolicy admits only the app."""
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
    """The app chart's schema matches the library's."""
    library = json.loads((LIBRARY / "app-values.schema.json").read_text())
    app = json.loads((APP_CHART / "values.schema.json").read_text())
    assert app["properties"].pop("petstore") == {"$ref": "#/$defs/app"}
    for schema in (library, app):
        schema.pop("$comment")
    assert app == library


def test_schema_rejects_invalid_values(render_error: Callable[..., str]) -> None:
    """The schema rejects invalid values."""
    error = render_error("--set", "petstore.imagePullPolicy=Sometimes")
    assert "schema" in error


def test_library_lints() -> None:
    """The library chart passes a strict lint."""
    helm("lint", "--strict", str(LIBRARY))


@pytest.mark.parametrize(
    "values",
    [
        "test-values.yaml",
        "celery-values.yaml",
        "access-control-values.yaml",
        "gateway-values.yaml",
    ],
)
def test_app_chart_lints(chart: Path, values: str) -> None:
    """The app chart passes a strict lint with the values of CI."""
    helm("lint", "--strict", str(chart), "-f", str(chart / "ci" / values))


def test_app_chart_test_hook(chart: Path) -> None:
    """The test hook runs a Python check of the API against the app."""
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


GENERATED_CONFIG_SETTINGS = """
valuesKey: petstore
requireAudience: true
mongodb:
  databases: [petstore]
config:
  db:
    host: mongodb.example.org
  security:
    auth:
      audience: [petstore]
  server:
    max_content_length: 4096
"""


def test_checks_use_generated_config(
    chart_with_settings: Callable[[str], Path],
) -> None:
    """Checks use the config that the app's settings generate."""
    chart = chart_with_settings(GENERATED_CONFIG_SETTINGS)
    (chart / "templates" / "limit.yaml").write_text(LIMIT_TEMPLATE)
    docs = index(template(chart, "--set", "mongodb.enabled=false"))
    assert docs[("ConfigMap", "limit")]["data"] == {"limit": "4096"}
    assert _overlay(docs)["db"] == {"host": "mongodb.example.org"}


def test_version_label_without_build_metadata(chart: Path, tmp_path: Path) -> None:
    """The version label replaces the invalid character '+'."""
    chart_copy = tmp_path / "chart"
    shutil.copytree(chart, chart_copy)
    chart_yaml = chart_copy / "Chart.yaml"
    chart_yaml.write_text(
        chart_yaml.read_text().replace('appVersion: "latest"', 'appVersion: "1.0+b1"')
    )
    for doc in index(template(chart_copy)).values():
        assert doc["metadata"]["labels"]["app.kubernetes.io/version"] == "1.0_b1"


def test_mongodb_auth_required(
    chart_with_settings: Callable[[str], Path],
) -> None:
    """A missing mongodb.auth map fails."""
    chart = chart_with_settings("valuesKey: petstore")
    values = yaml.safe_load((chart / "values.yaml").read_text())
    del values["mongodb"]["auth"]
    (chart / "values.yaml").write_text(yaml.safe_dump(values))
    (chart / "values.schema.json").unlink()
    error = template_error(chart, "--set", "hostName=x", required=False)
    assert "mongodb and mongodb.auth must be maps" in error


# Celery worker and broker

WORKER = "petstore-worker"
BROKER = "petstore-broker"
BROKER_CONFIG = "petstore-broker-config"
WORKER_COMMAND = ["python", "-m", "celery", "--app", "worker:celery_app", "worker"]
CELERY = (
    "--set",
    "worker.enabled=true",
    "--set-json",
    f"worker.command={json.dumps(WORKER_COMMAND)}",
    "--set",
    "broker.enabled=true",
    "--set",
    "broker.existingSecret=celery",
)
CELERY_ENV = {
    "CELERY_BROKER_URL": {"secretKeyRef": {"name": "celery", "key": "broker-url"}},
    "CELERY_RESULT_BACKEND": {
        "secretKeyRef": {"name": "celery", "key": "result-backend", "optional": True}
    },
}


def _selector(component: str) -> dict[str, str]:
    return {
        "app.kubernetes.io/name": "petstore",
        "app.kubernetes.io/instance": "petstore",
        "app.kubernetes.io/component": component,
    }


def _celery_env(container: dict[str, Any]) -> dict[str, Any]:
    return {name: value for name, value in _env(container).items() if "CELERY" in name}


def test_celery_resources(render: Callable[..., Docs]) -> None:
    """The worker and the broker add their resources."""
    assert set(render(*CELERY)) == set(render()) | {
        ("Deployment", WORKER),
        ("StatefulSet", BROKER),
        ("ConfigMap", BROKER_CONFIG),
        ("NetworkPolicy", BROKER),
        ("PersistentVolumeClaim", BROKER),
        ("Service", BROKER),
    }


def test_celery_disabled_by_default(render: Callable[..., Docs]) -> None:
    """Without the worker and broker values, the app has no broker settings."""
    assert _celery_env(_container(render())) == {}


def test_celery_without_values(
    chart_with_settings: Callable[[str], Path],
) -> None:
    """App charts without worker and broker values render as before."""
    chart = chart_with_settings("valuesKey: petstore\nmongodb: {databases: [p]}")
    values = yaml.safe_load((chart / "values.yaml").read_text())
    for key in ("worker", "broker"):
        del values[key]
    (chart / "values.yaml").write_text(yaml.safe_dump(values))
    docs = index(template(chart))
    assert not [name for _, name in docs if name.endswith(("-worker", "-broker"))]
    assert _celery_env(_container(docs)) == {}


def test_worker(render: Callable[..., Docs]) -> None:
    """The worker runs the app's image with the worker command."""
    docs = render(*CELERY)
    app, worker = _container(docs), _container(docs, WORKER)
    assert worker["name"] == "worker"
    assert worker["image"] == app["image"]
    assert worker["imagePullPolicy"] == app["imagePullPolicy"]
    assert worker["command"] == WORKER_COMMAND
    assert worker["securityContext"] == app["securityContext"]
    assert worker["resources"]["limits"] == {"cpu": "100m", "memory": "256Mi"}
    assert worker["volumeMounts"] == app["volumeMounts"]
    assert not {"ports", "startupProbe", "readinessProbe", "livenessProbe"} & set(
        worker
    )
    env = _env(worker)
    assert {name: env[name] for name in CELERY_ENV} == CELERY_ENV
    assert env == {
        name: value
        for name, value in _env(app).items()
        if name not in ("GUNICORN_CMD_ARGS", "PORT")
    }
    spec = _pod_spec(docs, WORKER)
    assert spec["securityContext"] == _pod_spec(docs)["securityContext"]
    assert spec["volumes"] == _pod_spec(docs)["volumes"]
    assert spec["automountServiceAccountToken"] is False
    assert spec["enableServiceLinks"] is False
    assert spec["terminationGracePeriodSeconds"] == 30
    pod = docs[("Deployment", WORKER)]["spec"]
    assert pod["replicas"] == 1
    assert pod["template"]["metadata"]["annotations"] == {
        "checksum/config": _checksum(docs)
    }


def test_worker_settings(render: Callable[..., Docs]) -> None:
    """The worker's values take effect."""
    docs = render(
        *CELERY,
        "--set",
        "worker.replicas=3",
        "--set",
        "worker.terminationGracePeriodSeconds=0",
        "--set-json",
        "worker.tmpSizeLimit=null",
        "--set",
        "mongodb.auth.enabled=false",
    )
    assert docs[("Deployment", WORKER)]["spec"]["replicas"] == 3
    spec = _pod_spec(docs, WORKER)
    assert spec["terminationGracePeriodSeconds"] == 0
    assert spec["volumes"][1] == {"name": "tmp", "emptyDir": {}}
    assert "MONGO_USERNAME" not in _env(_container(docs, WORKER))


@pytest.mark.parametrize("command", ["[]", "null"])
def test_worker_command_required(
    render_error: Callable[..., str], command: str
) -> None:
    """The worker needs a command."""
    error = render_error(*CELERY, "--set-json", f"worker.command={command}")
    assert "worker.command must list" in error


@pytest.mark.parametrize("component", ["worker", "broker"])
def test_broker_secret_required(
    render_error: Callable[..., str], component: str
) -> None:
    """The worker and the bundled broker need the Secret with the broker URL."""
    error = render_error(
        "--set",
        f"{component}.enabled=true",
        "--set-json",
        f"worker.command={json.dumps(WORKER_COMMAND)}",
    )
    assert "broker.existingSecret must be set" in error


def test_external_broker(render: Callable[..., Docs]) -> None:
    """With only the Secret, the app gets the URL of an external broker."""
    docs = render("--set", "broker.existingSecret=celery")
    assert set(docs) == set(render())
    assert _celery_env(_container(docs)) == CELERY_ENV


def test_external_broker_with_worker(render: Callable[..., Docs]) -> None:
    """A worker can use an external broker."""
    docs = render(*CELERY, "--set", "broker.enabled=false")
    assert ("Deployment", WORKER) in docs
    assert not {name for _, name in docs if name == BROKER}
    assert _celery_env(_container(docs, WORKER)) == CELERY_ENV


def test_broker(render: Callable[..., Docs]) -> None:
    """The bundled broker runs RabbitMQ with the user from the Secret."""
    docs = render(*CELERY)
    broker = _container(docs, BROKER)
    assert broker["name"] == "broker"
    assert broker["image"] == _example_values()["broker"]["image"]
    assert broker["imagePullPolicy"] == "IfNotPresent"
    assert _env(broker) == {
        "RABBITMQ_LOGS": "-",
        "RABBITMQ_NODENAME": "rabbit@localhost",
        "RABBITMQ_DEFAULT_USER": {
            "secretKeyRef": {"name": "celery", "key": "username"}
        },
        "RABBITMQ_DEFAULT_PASS": {
            "secretKeyRef": {"name": "celery", "key": "password"}
        },
    }
    assert broker["ports"] == [
        {"name": "amqp", "containerPort": 5672, "protocol": "TCP"}
    ]
    for probe in ("startupProbe", "readinessProbe", "livenessProbe"):
        assert broker[probe]["tcpSocket"] == {"port": 5672}
    assert broker["securityContext"]["readOnlyRootFilesystem"] is True
    mounts = {mount["name"]: mount["mountPath"] for mount in broker["volumeMounts"]}
    # The Erlang cookie (in the home) is not on the claim, whose files the
    # kubelet makes group-readable (`fsGroup`), which Erlang refuses
    assert mounts == {
        "data": "/var/lib/rabbitmq",
        "persistent-data": "/var/lib/rabbitmq/mnesia",
        "tmp": "/tmp",  # noqa: S108
        "config": "/etc/rabbitmq/conf.d/90-foca.conf",
    }
    assert broker["volumeMounts"][3]["subPath"] == "90-foca.conf"
    config = docs[("ConfigMap", BROKER_CONFIG)]["data"]["90-foca.conf"]
    assert "deprecated_features.permit.transient_nonexcl_queues = true" in config
    pod = docs[("StatefulSet", BROKER)]["spec"]["template"]["metadata"]
    assert pod["annotations"]["checksum/config"]
    spec = _pod_spec(docs, BROKER)
    assert spec["securityContext"]["runAsUser"] == 999
    assert spec["securityContext"]["runAsNonRoot"] is True
    assert spec["enableServiceLinks"] is False
    assert spec["volumes"] == [
        {"name": "data", "emptyDir": {"sizeLimit": "1Gi"}},
        {"name": "persistent-data", "persistentVolumeClaim": {"claimName": BROKER}},
        {"name": "tmp", "emptyDir": {"sizeLimit": "64Mi"}},
        {"name": "config", "configMap": {"name": "petstore-broker-config"}},
    ]
    claim = docs[("PersistentVolumeClaim", BROKER)]
    assert claim["metadata"]["labels"]["app.kubernetes.io/component"] == "broker"
    # Deleted with the release, as queued tasks can be rebuilt
    assert "annotations" not in claim["metadata"]
    assert claim["spec"] == {
        "accessModes": ["ReadWriteOnce"],
        "resources": {"requests": {"storage": "1Gi"}},
    }
    # A new pod only once the old one is gone
    statefulset = docs[("StatefulSet", BROKER)]["spec"]
    assert statefulset["replicas"] == 1
    assert statefulset["serviceName"] == BROKER
    assert statefulset["updateStrategy"] == {"type": "RollingUpdate"}
    service = docs[("Service", BROKER)]["spec"]
    assert service["ports"] == [
        {"name": "amqp", "port": 5672, "protocol": "TCP", "targetPort": "amqp"}
    ]
    assert service["selector"]["app.kubernetes.io/component"] == "broker"


def test_broker_network_policy(render: Callable[..., Docs]) -> None:
    """The broker's NetworkPolicy admits only the app and the worker."""
    docs = render(*CELERY)
    policy = docs[("NetworkPolicy", BROKER)]["spec"]
    assert policy["podSelector"]["matchLabels"]["app.kubernetes.io/component"] == (
        "broker"
    )
    (rule,) = policy["ingress"]
    peers = [peer["podSelector"]["matchLabels"] for peer in rule["from"]]
    assert peers == [_selector("app"), _selector("worker")]
    assert rule["ports"] == [{"port": 5672, "protocol": "TCP"}]
    docs = render(*CELERY, "--set", "broker.networkPolicy.enabled=false")
    assert ("NetworkPolicy", BROKER) not in docs


def test_mongodb_network_policy_admits_worker(render: Callable[..., Docs]) -> None:
    """MongoDB's NetworkPolicy admits the worker if it is enabled."""
    (rule,) = render(*CELERY)[("NetworkPolicy", MONGODB)]["spec"]["ingress"]
    peers = [peer["podSelector"]["matchLabels"] for peer in rule["from"]]
    assert peers == [_selector("app"), _selector("worker")]


def test_celery_labels(render: Callable[..., Docs]) -> None:
    """The worker's and the broker's resources carry their component."""
    docs = render(*CELERY)
    resources = {
        ("Deployment", WORKER): "worker",
        ("StatefulSet", BROKER): "broker",
        ("Service", BROKER): "broker",
        ("NetworkPolicy", BROKER): "broker",
        ("ConfigMap", BROKER_CONFIG): "broker",
    }
    for resource, component in resources.items():
        labels = docs[resource]["metadata"]["labels"]
        assert labels["app.kubernetes.io/component"] == component
        assert labels["app.kubernetes.io/instance"] == "petstore"


def test_celery_security_contexts_openshift(render: Callable[..., Docs]) -> None:
    """On OpenShift, the worker and the broker leave the IDs unset."""
    docs = render(*CELERY, *OPENSHIFT_API)
    for name in (WORKER, BROKER):
        context = _pod_spec(docs, name)["securityContext"]
        assert not {"runAsUser", "runAsGroup", "fsGroup"} & set(context)
        assert context["runAsNonRoot"] is True


def test_celery_names(chart: Path) -> None:
    """The worker's and the broker's names follow the app's."""
    docs = index(template(chart, *CELERY, "--set", "fullnameOverride=shop"))
    assert {
        ("Deployment", "shop-worker"),
        ("StatefulSet", "shop-broker"),
        ("Service", "shop-broker"),
        ("NetworkPolicy", "shop-broker"),
        ("PersistentVolumeClaim", "shop-broker"),
    } <= set(docs)
    # Long names are truncated before the suffix
    long_name = "a" * 63
    docs = index(template(chart, *CELERY, "--set", f"fullnameOverride={long_name}"))
    assert ("Deployment", "a" * 56 + "-worker") in docs
    assert ("Service", "a" * 56 + "-broker") in docs


def test_worker_probes(render: Callable[..., Docs]) -> None:
    """The worker's probes are taken from the values."""
    probe = {"exec": {"command": ["celery", "inspect", "ping"]}, "periodSeconds": 60}
    docs = render(
        *CELERY,
        "--set-json",
        f"worker.readinessProbe={json.dumps(probe)}",
        "--set-json",
        f"worker.livenessProbe={json.dumps(probe)}",
    )
    worker = _container(docs, WORKER)
    assert worker["readinessProbe"] == probe
    assert worker["livenessProbe"] == probe


def test_broker_settings(render: Callable[..., Docs]) -> None:
    """The broker's values take effect."""
    docs = render(
        *CELERY,
        "--set",
        "broker.imagePullPolicy=Never",
        "--set",
        "broker.image=registry.example.org/rabbitmq:4.3",
        "--set",
        "broker.persistence.enabled=false",
        "--set-json",
        "broker.dataSizeLimit=null",
        "--set",
        "broker.tmpSizeLimit=32Mi",
        "--set-json",
        'broker.resources={"limits": {"memory": "1Gi"}}',
    )
    broker = _container(docs, BROKER)
    assert broker["image"] == "registry.example.org/rabbitmq:4.3"
    assert broker["imagePullPolicy"] == "Never"
    assert broker["resources"]["limits"] == {"cpu": "200m", "memory": "1Gi"}
    assert _pod_spec(docs, BROKER)["volumes"] == [
        {"name": "data", "emptyDir": {}},
        {"name": "tmp", "emptyDir": {"sizeLimit": "32Mi"}},
        {"name": "config", "configMap": {"name": "petstore-broker-config"}},
    ]


def _broker_volumes(docs: Docs) -> dict[str, dict[str, object]]:
    """The volumes of the broker's pod, by name."""
    return {volume.pop("name"): volume for volume in _pod_spec(docs, BROKER)["volumes"]}


def _assert_broker_without_claim(docs: Docs) -> None:
    """The broker keeps its data in its emptyDir, without a claim."""
    assert ("PersistentVolumeClaim", BROKER) not in docs
    volumes = _broker_volumes(docs)
    assert "persistent-data" not in volumes
    assert volumes["data"] == {"emptyDir": {"sizeLimit": "1Gi"}}
    mounts = [mount["mountPath"] for mount in _container(docs, BROKER)["volumeMounts"]]
    assert "/var/lib/rabbitmq/mnesia" not in mounts


@pytest.mark.parametrize("existing_claim", ["", "queues"])
def test_broker_without_persistence(
    render: Callable[..., Docs], existing_claim: str
) -> None:
    """Without persistence, the broker keeps its data in an emptyDir."""
    docs = render(
        *CELERY,
        "--set",
        "broker.persistence.enabled=false",
        "--set",
        f"broker.persistence.existingClaim={existing_claim}",
    )
    _assert_broker_without_claim(docs)


def test_broker_disabled_without_claim(render: Callable[..., Docs]) -> None:
    """Without the bundled broker, there is no claim for it."""
    docs = render()
    assert not [kind for kind, name in docs if name.startswith(BROKER)]


def test_broker_persistence_settings(render: Callable[..., Docs]) -> None:
    """The size and access modes of the broker's claim take effect."""
    docs = render(
        *CELERY,
        "--set",
        "broker.persistence.size=5Gi",
        "--set-json",
        'broker.persistence.accessModes=["ReadWriteOncePod"]',
    )
    assert docs[("PersistentVolumeClaim", BROKER)]["spec"] == {
        "accessModes": ["ReadWriteOncePod"],
        "resources": {"requests": {"storage": "5Gi"}},
    }


@pytest.mark.parametrize(
    ("storage_class", "expected"),
    [("fast", "fast"), ("-", "")],
)
def test_broker_storage_class(
    render: Callable[..., Docs], storage_class: str, expected: str
) -> None:
    """A storage class is set, or "-" disables dynamic provisioning."""
    docs = render(*CELERY, "--set", f"broker.persistence.storageClass={storage_class}")
    spec = docs[("PersistentVolumeClaim", BROKER)]["spec"]
    assert spec["storageClassName"] == expected


def test_broker_resource_policy(render: Callable[..., Docs]) -> None:
    """The broker's claim is kept on uninstall only with `resourcePolicy: keep`."""
    docs = render(*CELERY, "--set", "broker.persistence.resourcePolicy=keep")
    assert docs[("PersistentVolumeClaim", BROKER)]["metadata"]["annotations"] == KEEP
    docs = render(*CELERY, "--set-json", "broker.persistence.resourcePolicy=null")
    assert "annotations" not in docs[("PersistentVolumeClaim", BROKER)]["metadata"]


def test_broker_existing_claim(render: Callable[..., Docs]) -> None:
    """An existing claim replaces the broker's own."""
    docs = render(*CELERY, "--set", "broker.persistence.existingClaim=queues")
    assert ("PersistentVolumeClaim", BROKER) not in docs
    volume = _broker_volumes(docs)["persistent-data"]
    assert volume == {"persistentVolumeClaim": {"claimName": "queues"}}


def test_broker_persistence_size_required(chart: Path, tmp_path: Path) -> None:
    """The broker's claim needs a size."""
    # Bypasses the schema, which rejects an empty size already
    chart_copy = _chart_without_schema(chart, tmp_path)
    error = template_error(chart_copy, *CELERY, "--set", "broker.persistence.size=")
    assert "broker.persistence.size must be set" in error


def test_broker_without_persistence_values(chart: Path, tmp_path: Path) -> None:
    """Values without `broker.persistence` (as for 0.1.0) keep the emptyDir."""
    # Bypasses the schema, which requires the map in the app chart's values
    chart_copy = _chart_without_schema(chart, tmp_path)
    docs = index(template(chart_copy, *CELERY, "--set-json", "broker.persistence=null"))
    _assert_broker_without_claim(docs)


def test_broker_deployment_alias(
    render: Callable[..., Docs], chart: Path, tmp_path: Path
) -> None:
    """The template name of 0.1.0 (`brokerDeployment`) renders the StatefulSet."""
    chart_copy = tmp_path / "chart"
    shutil.copytree(chart, chart_copy)
    template_file = chart_copy / "templates" / "broker-statefulset.yaml"
    template_file.write_text(
        template_file.read_text().replace("brokerStatefulSet", "brokerDeployment")
    )
    docs = index(template(chart_copy, *CELERY))
    expected = render(*CELERY)[("StatefulSet", BROKER)]
    assert docs[("StatefulSet", BROKER)] == expected


def test_broker_image_required(render_error: Callable[..., str]) -> None:
    """The bundled broker needs an image."""
    error = render_error(*CELERY, "--set", "broker.image=")
    assert "broker.image must be set" in error


def test_worker_follows_app_values(render: Callable[..., Docs]) -> None:
    """The worker takes the app's image and pull policy."""
    docs = render(
        *CELERY,
        "--set",
        "petstore.tag=1.2.3",
        "--set",
        "petstore.imagePullPolicy=Always",
        "--set-json",
        'worker.resources={"limits": {"memory": "1Gi"}}',
    )
    worker = _container(docs, WORKER)
    assert worker["image"] == "foca-petstore:1.2.3"
    assert worker["imagePullPolicy"] == "Always"
    assert worker["resources"]["limits"] == {"cpu": "100m", "memory": "1Gi"}


@pytest.mark.parametrize("suffix", ["-worker", "-broker"])
def test_celery_name_collision_fails(
    render_error: Callable[..., str], suffix: str
) -> None:
    """Names that would collide with the worker's or broker's fail."""
    long_name = "a" * (63 - len(suffix)) + suffix
    error = render_error(*CELERY, "--set", f"fullnameOverride={long_name}")
    assert "would both be named" in error


def test_schema_rejects_invalid_celery_values(
    render_error: Callable[..., str],
) -> None:
    """The schema rejects invalid worker and broker values."""
    assert "schema" in render_error("--set", "worker.replicas=0")
    assert "schema" in render_error("--set", "broker.dataSizeLimit=0")
    assert "schema" in render_error("--set", "broker.persistence.size=1x")
    assert "schema" in render_error("--set", "broker.persistence.size=0Gi")
    assert "schema" in render_error(
        "--set-json", 'broker.persistence.accessModes=["ReadWriteSometimes"]'
    )
    assert "schema" in render_error(
        "--set-json", 'broker.persistence.accessModes=["ReadOnlyMany"]'
    )
    assert "schema" in render_error("--set", "broker.persistence.unknown=true")
    assert "schema" in render_error("--set", "broker.persistence.resourcePolicy=no")


# Plugin store

STORE_KEY_SECRET_ENV = {
    "FOCA_PLUGINS_STORE_KEY_SECRET": {
        "secretKeyRef": {"name": "plugins", "key": "store-key-secret"}
    }
}
PLUGINS_SECRET = ("--set", "pluginStore.existingSecret=plugins")


def _store_key_secret_env(container: dict[str, Any]) -> dict[str, Any]:
    """The container's variable with the key secret of the plugin store."""
    return {
        name: value
        for name, value in _env(container).items()
        if name == "FOCA_PLUGINS_STORE_KEY_SECRET"
    }


def test_store_key_secret_not_set_by_default(render: Callable[..., Docs]) -> None:
    """Without `pluginStore.existingSecret`, the app gets no key secret."""
    assert _store_key_secret_env(_container(render())) == {}


def test_store_key_secret(render: Callable[..., Docs]) -> None:
    """The app and the worker get the key secret from the Secret."""
    docs = render(*PLUGINS_SECRET, *CELERY)
    for name in (APP, WORKER):
        assert _store_key_secret_env(_container(docs, name)) == STORE_KEY_SECRET_ENV
    assert "FOCA_PLUGINS_STORE_KEY_SECRET" not in _env(_container(docs, BROKER))


def test_store_key_secret_without_values(
    chart_with_settings: Callable[[str], Path],
) -> None:
    """App charts without `pluginStore` values render as before."""
    chart = chart_with_settings("valuesKey: petstore\nmongodb: {databases: [p]}")
    values = yaml.safe_load((chart / "values.yaml").read_text())
    del values["pluginStore"]
    (chart / "values.yaml").write_text(yaml.safe_dump(values))
    assert _store_key_secret_env(_container(index(template(chart)))) == {}


def test_store_key_secret_not_read_from_plugins(render: Callable[..., Docs]) -> None:
    """Top-level `plugins` values belong to the app chart, e.g., proTES's."""
    docs = render("--set", "plugins.existingSecret=plugins")
    assert _store_key_secret_env(_container(docs)) == {}


@pytest.mark.parametrize(
    ("flag", "secret"),
    [
        ("--set", "s3cr3t"),
        ("--set", "12345"),
        ("--set-json", "false"),
        ("--set-json", "0"),
        ("--set-json", '{"a": "s3cr3t"}'),
        ("--set-json", '["s3cr3t"]'),
    ],
)
def test_store_key_secret_in_overlay_fails(
    render_error: Callable[..., str], flag: str, secret: str
) -> None:
    """Any non-empty key secret in the config overlay (a ConfigMap) fails."""
    error = render_error(flag, f"config.plugins.defaults.store.key_secret={secret}")
    assert "pluginStore.existingSecret" in error
    assert "s3cr3t" not in error


@pytest.mark.parametrize(
    "args",
    [
        ("--set", "config.plugins.defaults.store.key_secret="),
        ("--set-json", "config.plugins.defaults.store.key_secret=null"),
        ("--set-json", "config.plugins.defaults.store=null"),
        ("--set-json", "config.plugins=null"),
    ],
)
def test_store_key_secret_empty_in_overlay(
    render: Callable[..., Docs], args: tuple[str, ...]
) -> None:
    """An empty key secret in the overlay renders (FOCA uses the variable)."""
    docs = render(*PLUGINS_SECRET, *args)
    assert _store_key_secret_env(_container(docs)) == STORE_KEY_SECRET_ENV


def test_schema_rejects_invalid_plugin_store_values(
    render_error: Callable[..., str],
) -> None:
    """The schema rejects invalid plugin store values."""
    assert "schema" in render_error("--set-json", "pluginStore.existingSecret=1")
    assert "schema" in render_error("--set", "pluginStore.unknown=true")


EXTRA_ENV = [
    {"name": "MONGO_URI", "valueFrom": {"secretKeyRef": {"name": "db", "key": "uri"}}},
    {"name": "OTHER", "value": "x"},
]
EXTRA_ENV_FROM = [{"secretRef": {"name": "more"}}, {"configMapRef": {"name": "cm"}}]


def _deployment_container(docs: Docs, name: str) -> dict[str, Any]:
    """Get the container of a Deployment.

    Args:
        docs: Rendered manifests.
        name: Name of the Deployment.

    Returns:
        First container of its pods.
    """
    container: dict[str, Any] = docs["Deployment", name]["spec"]["template"]["spec"][
        "containers"
    ][0]
    return container


def test_extra_env_default(render: Callable[..., Docs]) -> None:
    """Without extra settings, containers have no `envFrom`."""
    docs = render(*CELERY)
    for name in (APP, WORKER):
        container = docs["Deployment", name]["spec"]["template"]["spec"]["containers"][
            0
        ]
        assert "envFrom" not in container
        assert "OTHER" not in _env(container)


def test_port_set(render: Callable[..., Docs]) -> None:
    """The app's port is set, so `extraEnvFrom` can't change it."""
    assert _env(_container(render()))["PORT"] == "8080"


def test_extra_env(render: Callable[..., Docs]) -> None:
    """Extra settings apply to the app and the worker, not to other containers."""
    docs = render(
        *CELERY,
        "--set-json",
        f"petstore.extraEnv={json.dumps(EXTRA_ENV)}",
        "--set-json",
        f"petstore.extraEnvFrom={json.dumps(EXTRA_ENV_FROM)}",
    )
    for name in (APP, WORKER):
        container = docs["Deployment", name]["spec"]["template"]["spec"]["containers"][
            0
        ]
        env = _env(container)
        assert env["OTHER"] == "x"
        assert env["MONGO_URI"] == EXTRA_ENV[0]["valueFrom"]
        assert container["envFrom"] == EXTRA_ENV_FROM
    for key, doc in docs.items():
        if key not in {("Deployment", APP), ("Deployment", WORKER)}:
            assert "OTHER" not in json.dumps(doc)


@pytest.mark.parametrize(
    "name",
    [
        "FOCA_CONFIG_OVERLAY",
        "PORT",
        "GUNICORN_CMD_ARGS",
        "MONGO_USERNAME",
        "MONGO_PASSWORD",
    ],
)
def test_extra_env_reserved(render_error: Callable[..., str], name: str) -> None:
    """Variables that the chart sets can't be set."""
    error = render_error(
        "--set-json", f"petstore.extraEnv={json.dumps([{'name': name, 'value': 'x'}])}"
    )
    assert f"petstore.extraEnv must not set {name}" in error


@pytest.mark.parametrize("name", ["CELERY_BROKER_URL", "CELERY_RESULT_BACKEND"])
def test_extra_env_reserved_broker(render_error: Callable[..., str], name: str) -> None:
    """The broker's variables can't be set if the chart sets them."""
    error = render_error(
        *CELERY,
        "--set-json",
        f"petstore.extraEnv={json.dumps([{'name': name, 'value': 'x'}])}",
    )
    assert f"petstore.extraEnv must not set {name}" in error


def test_extra_env_reserved_plugins(render_error: Callable[..., str]) -> None:
    """The key secret of the plugin store can't be set if the chart sets it."""
    name = "FOCA_PLUGINS_STORE_KEY_SECRET"
    error = render_error(
        "--set",
        "pluginStore.existingSecret=store",
        "--set-json",
        f"petstore.extraEnv={json.dumps([{'name': name, 'value': 'x'}])}",
    )
    assert f"petstore.extraEnv must not set {name}" in error


@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("GUNICORN_CMD_ARGS", ("--set-json", "petstore.gunicorn=null")),
        (
            "MONGO_USERNAME",
            ("--set", "mongodb.auth.enabled=false"),
        ),
        ("CELERY_BROKER_URL", ()),
        ("OTHER_SECRET", ()),
    ],
)
def test_extra_env_allowed(
    render: Callable[..., Docs], name: str, args: tuple[str, ...]
) -> None:
    """Variables are free if the chart doesn't set them."""
    docs = render(
        *args,
        "--set-json",
        f"petstore.extraEnv={json.dumps([{'name': name, 'value': 'x'}])}",
    )
    assert name in _env(_container(docs))


@pytest.mark.parametrize("extra_env", [{"value": "x"}, {"name": ""}, "x"])
def test_extra_env_schema(render_error: Callable[..., str], extra_env: object) -> None:
    """Items without a name are rejected."""
    assert "extraEnv" in render_error(
        "--set-json", f"petstore.extraEnv={json.dumps([extra_env])}"
    )


def test_rollout_on_secrets_template(render: Callable[..., Docs]) -> None:
    """`helm template` renders without an annotation, as it finds no Secrets."""
    docs = render(*CELERY, "--set-json", 'petstore.rolloutOnSecrets=["db"]')
    for name in (APP, WORKER):
        annotations = docs["Deployment", name]["spec"]["template"]["metadata"][
            "annotations"
        ]
        assert "checksum/secrets" not in annotations
        assert "checksum/config" in annotations


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("MONGO_PASSWORD", "hunter2"),
        ("FOCA_PLUGINS_STORE_KEY_SECRET", "hunter2"),
        ("MONGO_URI", "mongodb://u:hunter2@db/"),
        ("CELERY_BROKER_URL", "amqp://u:hunter2@broker//"),
        ("CELERY_RESULT_BACKEND", "rpc://u:hunter2@broker//"),
    ],
)
def test_extra_env_literal_secret(
    render_error: Callable[..., str], name: str, value: str
) -> None:
    """Secrets as literal values are refused, without showing them."""
    error = render_error(
        "--set",
        "mongodb.auth.enabled=false",
        "--set-json",
        f"petstore.extraEnv={json.dumps([{'name': name, 'value': value}])}",
    )
    assert f"must take {name} from a Secret" in error
    assert "hunter2" not in error


@pytest.mark.parametrize(
    "var",
    [
        {"name": "MONGO_URI", "value": "mongodb://db/"},
        {
            "name": "MONGO_URI",
            "valueFrom": {"secretKeyRef": {"name": "db", "key": "uri"}},
        },
        {
            "name": "FOCA_PLUGINS_STORE_KEY_SECRET",
            "valueFrom": {"secretKeyRef": {"name": "s", "key": "k"}},
        },
        {"name": "OTHER", "value": "a@b"},
        {"name": "NUMBER", "value": "1"},
    ],
)
def test_extra_env_no_literal_secret(
    render: Callable[..., Docs], var: dict[str, Any]
) -> None:
    """References and values without credentials pass."""
    docs = render("--set-json", f"petstore.extraEnv={json.dumps([var])}")
    assert var["name"] in _env(_container(docs))


def test_extra_env_duplicate(render_error: Callable[..., str]) -> None:
    """A variable can't be set twice."""
    extra_env = [{"name": "A", "value": "1"}, {"name": "A", "value": "2"}]
    error = render_error("--set-json", f"petstore.extraEnv={json.dumps(extra_env)}")
    assert "must not set A twice" in error


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("extraEnv", {"a": {"name": "B"}}, "extraEnv must be a list"),
        ("extraEnv", False, "extraEnv must be a list"),
        ("extraEnv", {}, "extraEnv must be a list"),
        ("extraEnvFrom", 0, "extraEnvFrom must be a list"),
        ("extraEnvFrom", "", "extraEnvFrom must be a list"),
        ("rolloutOnSecrets", False, "rolloutOnSecrets must be a list"),
        ("rolloutOnSecrets", {}, "rolloutOnSecrets must be a list"),
        ("extraEnv", [{}], "extraEnv must be a list"),
        ("extraEnv", ["x"], "extraEnv must be a list"),
        ("extraEnvFrom", {"secretRef": {"name": "x"}}, "extraEnvFrom must be a list"),
        ("extraEnvFrom", [3], "extraEnvFrom must be a list"),
        ("rolloutOnSecrets", "x", "rolloutOnSecrets must be a list"),
        ("rolloutOnSecrets", [""], "rolloutOnSecrets must be a list"),
        ("rolloutOnSecrets", [1], "rolloutOnSecrets must be a list"),
    ],
)
def test_extra_settings_without_schema(
    tmp_path: Path, key: str, value: object, message: str
) -> None:
    """Without the schema, wrong types fail on rendering."""
    chart = build_chart(tmp_path)
    (chart / "values.schema.json").unlink()
    error = template_error(chart, "--set-json", f"petstore.{key}={json.dumps(value)}")
    assert message in error


# Autoscaling and topology spread constraints


AUTOSCALING = ("--set", "petstore.autoscaling.enabled=true")


def _hpa(docs: Docs) -> dict[str, Any]:
    hpa: dict[str, Any] = docs[("HorizontalPodAutoscaler", APP)]
    return hpa


def test_no_autoscaling_by_default(render: Callable[..., Docs]) -> None:
    """Without autoscaling, nothing changes."""
    docs = render()
    assert ("HorizontalPodAutoscaler", APP) not in docs
    assert docs[("Deployment", APP)]["spec"]["replicas"] == 1
    pod = docs[("Deployment", APP)]["spec"]["template"]["spec"]
    assert "topologySpreadConstraints" not in pod


def test_autoscaling(render: Callable[..., Docs]) -> None:
    """With autoscaling, the autoscaler sets the number of pods."""
    docs = render(*AUTOSCALING, "--set", "petstore.replicas=7")
    assert "replicas" not in docs[("Deployment", APP)]["spec"]
    hpa = _hpa(docs)
    assert hpa["apiVersion"] == "autoscaling/v2"
    assert hpa["spec"]["scaleTargetRef"] == {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "name": APP,
    }
    assert hpa["spec"]["minReplicas"] == 2
    assert hpa["spec"]["maxReplicas"] == 4
    assert hpa["spec"]["metrics"] == [
        {
            "type": "Resource",
            "resource": {
                "name": "cpu",
                "target": {"type": "Utilization", "averageUtilization": 80},
            },
        }
    ]


def test_autoscaling_memory(render: Callable[..., Docs]) -> None:
    """Memory can be used instead of or with CPU."""
    only = _hpa(
        render(
            *AUTOSCALING,
            "--set-json",
            "petstore.autoscaling.targetCPUUtilizationPercentage=null",
            "--set",
            "petstore.autoscaling.targetMemoryUtilizationPercentage=70",
        )
    )
    assert [m["resource"]["name"] for m in only["spec"]["metrics"]] == ["memory"]
    both = _hpa(
        render(
            *AUTOSCALING,
            "--set",
            "petstore.autoscaling.targetMemoryUtilizationPercentage=70",
        )
    )
    assert [m["resource"]["name"] for m in both["spec"]["metrics"]] == [
        "cpu",
        "memory",
    ]


def test_autoscaling_no_target_means_cpu(render: Callable[..., Docs]) -> None:
    """Without a target, CPU at 80% is used, as in `helm create`."""
    hpa = _hpa(
        render(
            *AUTOSCALING,
            "--set-json",
            "petstore.autoscaling.targetCPUUtilizationPercentage=null",
        )
    )
    metric = hpa["spec"]["metrics"][0]["resource"]
    assert metric["name"] == "cpu"
    assert metric["target"]["averageUtilization"] == 80


@pytest.mark.parametrize(
    ("replicas", "minimum", "budget"),
    [(1, 2, True), (5, 1, False), (5, 2, True), (1, 1, False)],
)
def test_autoscaling_budget(
    render: Callable[..., Docs], replicas: int, minimum: int, budget: bool
) -> None:
    """The fewest pods decide whether a budget is rendered, not `replicas`."""
    docs = render(
        *AUTOSCALING,
        "--set",
        f"petstore.autoscaling.minReplicas={minimum}",
        "--set",
        f"petstore.replicas={replicas}",
    )
    assert (("PodDisruptionBudget", APP) in docs) is budget


def test_autoscaling_max_below_min(render_error: Callable[..., str]) -> None:
    """More pods at least than at most are refused."""
    error = render_error(*AUTOSCALING, "--set", "petstore.autoscaling.maxReplicas=1")
    assert "maxReplicas must not be less than minReplicas" in error


@pytest.mark.parametrize(
    ("values", "error"),
    [
        ('{"autoscaling": 1}', "autoscaling must be a map"),
        ('{"autoscaling": {"enabled": "yes"}}', "enabled must be true or false"),
        (
            '{"autoscaling": {"enabled": true, "minReplicas": 0}}',
            "minReplicas must be a whole number",
        ),
        (
            '{"autoscaling": {"enabled": true, "maxReplicas": 1.5}}',
            "maxReplicas must be a whole number",
        ),
        (
            '{"autoscaling": {"enabled": true, "targetCPUUtilizationPercentage": "a"}}',
            "targetCPUUtilizationPercentage must be a whole number",
        ),
        ('{"topologySpreadConstraints": {}}', "must be a list"),
        ('{"topologySpreadConstraints": ["a"]}', "must be a list"),
        ('{"topologySpreadConstraints": [{}]}', "must be a list"),
        (
            '{"topologySpreadConstraints": [{"maxSkew": 0, "topologyKey": "a", '
            '"whenUnsatisfiable": "DoNotSchedule"}]}',
            "maxSkew of at least 1",
        ),
        (
            '{"topologySpreadConstraints": [{"maxSkew": 1, "topologyKey": "a", '
            '"whenUnsatisfiable": "DoNotSchedule", "labelSelector": "x"}]}',
            "labelSelector must be a map",
        ),
    ],
)
def test_autoscaling_invalid_without_schema(
    chart: Path, tmp_path: Path, values: str, error: str
) -> None:
    """Invalid values are refused, also for app charts without the schema."""
    chart_copy = _chart_without_schema(chart, tmp_path)
    assert error in template_error(chart_copy, "--set-json", f"petstore={values}")


@pytest.mark.parametrize(
    "values",
    [
        '{"autoscaling": {"minReplicas": 0}}',
        '{"autoscaling": {"maxReplicas": 0}}',
        '{"autoscaling": {"targetCPUUtilizationPercentage": 0}}',
        '{"autoscaling": {"targetMemoryUtilizationPercentage": "a"}}',
        '{"autoscaling": {"unknown": 1}}',
        '{"autoscaling": {"enabled": "yes"}}',
        '{"topologySpreadConstraints": {}}',
        '{"topologySpreadConstraints": ["a"]}',
        '{"topologySpreadConstraints": [{}]}',
        *(
            '{"topologySpreadConstraints": [{"maxSkew": 1, "topologyKey": "a", '
            f'"whenUnsatisfiable": "DoNotSchedule", {field}}}]}}'
            for field in (
                '"maxSkew": 0',
                '"maxSkew": 1.5',
                '"maxSkew": "1"',
                '"topologyKey": ""',
                '"whenUnsatisfiable": "Nope"',
                '"labelSelector": "x"',
            )
        ),
    ],
)
def test_autoscaling_schema(render_error: Callable[..., str], values: str) -> None:
    """The schema refuses invalid values."""
    assert "schema" in render_error("--set-json", f"petstore={values}")


def test_autoscaling_without_values(chart: Path, tmp_path: Path) -> None:
    """Older app charts without the values are unchanged."""
    chart_copy = _chart_without_schema(chart, tmp_path)
    docs = index(
        template(
            chart_copy,
            "--set-json",
            "petstore.autoscaling=null",
            "--set-json",
            "petstore.topologySpreadConstraints=null",
        )
    )
    assert docs[("Deployment", APP)]["spec"]["replicas"] == 1
    assert ("HorizontalPodAutoscaler", APP) not in docs


def test_topology_spread_selector(render: Callable[..., Docs]) -> None:
    """The selector of the app's pods is filled in; given ones are kept."""
    constraint = {
        "maxSkew": 1,
        "topologyKey": "zone",
        "whenUnsatisfiable": "ScheduleAnyway",
    }
    given = {**constraint, "labelSelector": {"matchLabels": {"a": "b"}}}
    expressions = {
        **constraint,
        "labelSelector": {
            "matchExpressions": [{"key": "a", "operator": "Exists"}],
        },
    }
    empties = [
        {**constraint, "labelSelector": selector}
        for selector in ({}, {"matchLabels": {}}, {"matchExpressions": []})
    ]
    constraints = [constraint, given, expressions, *empties]
    docs = render(
        "--set-json",
        f"petstore.topologySpreadConstraints={json.dumps(constraints)}",
    )
    template_spec = docs[("Deployment", APP)]["spec"]["template"]
    default = {
        **constraint,
        "labelSelector": {"matchLabels": template_spec["metadata"]["labels"]},
    }
    assert template_spec["spec"]["topologySpreadConstraints"] == [
        default,
        given,
        expressions,
        default,
        default,
        default,
    ]


# Gateway apps


def _gateway_settings(*options: str) -> str:
    """Get settings of an app with options for gateway apps.

    Args:
        *options: Lines of options.

    Returns:
        Body of the settings template.
    """
    lines = "\n".join(options)
    return f"valuesKey: petstore\nmongodb:\n  databases: [petstore]\n{lines}\n"


GATEWAY_SETTINGS = _gateway_settings(
    "plugins: true", "backendsKey: tes", "requireAccessControl: true"
)
# Security settings that the check of access control accepts
SECURE = (
    "--set-json",
    "config.security="
    + json.dumps(
        {
            "auth": {"issuers": ["https://login.example.org/"]},
            "access_control": {
                "claim_groups": "groups",
                "group_mapping": {"admins": "admin"},
            },
        }
    ),
)
SECURITY_DEFAULTS: dict[str, Any] = {
    "auth": {"issuers": ["https://login.example.org/"]},
    "access_control": {"claim_groups": "groups", "group_mapping": {"a": "admin"}},
}


def _security(**access_control: Any) -> tuple[str, str]:
    """Get arguments for security settings that differ from accepted ones.

    Args:
        **access_control: Settings of `security.access_control` to change.

    Returns:
        Arguments of Helm.
    """
    security = deepcopy(SECURITY_DEFAULTS)
    security["access_control"].update(access_control)
    return ("--set-json", f"config.security={json.dumps(security)}")


def _issuers(issuers: Any, **auth: Any) -> tuple[str, str]:
    """Get arguments for issuers that differ from accepted ones.

    Args:
        issuers: Issuers.
        **auth: Further settings of `security.auth`.

    Returns:
        Arguments of Helm.
    """
    security = deepcopy(SECURITY_DEFAULTS)
    security["auth"] = {"issuers": issuers, **auth}
    return ("--set-json", f"config.security={json.dumps(security)}")


def test_gateway_options_off_by_default(render: Callable[..., Docs]) -> None:
    """Without the options, the values have no effect on the overlay."""
    docs = render("--set-json", 'plugins={"pipeline":[{"plugin":"x"}]}')
    overlay = _overlay(docs)
    assert "plugins" not in overlay
    assert "server" not in overlay
    assert "custom" not in overlay
    # No checks either
    assert not any(kind == "Deployment" and "flower" in name for kind, name in docs)


def test_plugins_rendered(chart_with_settings: Callable[[str], Path]) -> None:
    """Plugin settings are rendered with the keys that FOCA expects."""
    chart = chart_with_settings(_gateway_settings("plugins: true"))
    plugins = {
        "pipeline": [
            {
                "plugin": "router",
                "onFailure": "abort",
                "ifMissingPhase": "skip",
                "params": {"keptAsIs": 1},
            }
        ],
        "available": {
            "myPlugin.Router": {
                "clientSelectable": True,
                "config": {"keptAsIs": 1},
                "store": {"maxTtl": 1000000},
            }
        },
        "presets": {"myPreset": [{"plugin": "router", "maxSteps": 1}]},
        "clientPipelines": {"maxAlternatives": 2},
        "tags": {},
    }
    docs = index(template(chart, "--set-json", f"plugins={json.dumps(plugins)}"))
    assert _overlay(docs)["plugins"] == {
        "pipeline": [
            {
                "plugin": "router",
                "on_failure": "abort",
                "if_missing_phase": "skip",
                "params": {"keptAsIs": 1},
            }
        ],
        "available": {
            "myPlugin.Router": {
                "client_selectable": True,
                "config": {"keptAsIs": 1},
                "store": {"max_ttl": 1000000},
            }
        },
        "presets": {"myPreset": [{"plugin": "router", "max_steps": 1}]},
        "client_pipelines": {"max_alternatives": 2},
    }


def test_no_plugin_section_without_values(
    chart_with_settings: Callable[[str], Path],
) -> None:
    """Without settings, the base config's section stays."""
    chart = chart_with_settings(_gateway_settings("plugins: true"))
    assert "plugins" not in _overlay(index(template(chart)))


def test_plugins_in_both_fail(chart_with_settings: Callable[[str], Path]) -> None:
    """Plugin settings can't be set in both places."""
    chart = chart_with_settings(_gateway_settings("plugins: true"))
    error = template_error(
        chart,
        "--set-json",
        'plugins={"clientPipelines":{"maxSteps":1}}',
        "--set-json",
        'config.plugins={"client_pipelines":{"max_steps":2}}',
    )
    assert "either in plugins.clientPipelines or in config.plugins" in error


def test_plugins_only_in_config(chart_with_settings: Callable[[str], Path]) -> None:
    """Settings in the config alone are kept, as well as other ones in the values."""
    chart = chart_with_settings(_gateway_settings("plugins: true"))
    docs = index(
        template(
            chart,
            "--set-json",
            'plugins={"tags":{"maxRouteEntries":1}}',
            "--set-json",
            'config.plugins={"client_pipelines":{"max_steps":2}}',
        )
    )
    assert _overlay(docs)["plugins"] == {
        "tags": {"max_route_entries": 1},
        "client_pipelines": {"max_steps": 2},
    }


def test_store_key_secret_in_plugin_values_fails(
    chart_with_settings: Callable[[str], Path],
) -> None:
    """The key secret of the store doesn't get into the ConfigMap via the values."""
    chart = chart_with_settings(_gateway_settings("plugins: true"))
    error = template_error(
        chart,
        "--set-json",
        'plugins={"defaults":{"store":{"keySecret":"x"}}}',
    )
    assert "key_secret" in error


BACKENDS = [
    {
        "type": "ga4gh-tes",
        "url": "https://tes.example.org",
        "auth": "basic",
        "basic": {"usernameEnv": "USER", "passwordEnv": "PASSWORD"},
        "labels": {"myRegion": "eu"},
        "urlSchemes": ["https"],
        "apiPath": "/ga4gh/tes/v1",
    },
    {
        "type": "ga4gh-service-registry",
        "url": "https://registry.example.org/",
        "refreshInterval": 1,
        "maxStale": 2,
        "allowPrivateAddresses": False,
        "registryAllowPrivateAddresses": False,
    },
]


def test_backends_rendered(chart_with_settings: Callable[[str], Path]) -> None:
    """Backends and the public URL are rendered with the keys that FOCA expects."""
    chart = chart_with_settings(_gateway_settings("backendsKey: tes"))
    docs = index(
        template(chart, "--set-json", f"tes={json.dumps({'backends': BACKENDS})}")
    )
    overlay = _overlay(docs)
    assert overlay["server"]["public_url"] == "https://petstore.example.org/"
    assert overlay["custom"]["tes"]["backends"] == [
        {
            "type": "ga4gh-tes",
            "url": "https://tes.example.org",
            "auth": "basic",
            "basic": {"username_env": "USER", "password_env": "PASSWORD"},
            "labels": {"myRegion": "eu"},
            "url_schemes": ["https"],
            "api_path": "/ga4gh/tes/v1",
        },
        {
            "type": "ga4gh-service-registry",
            "url": "https://registry.example.org/",
            "refresh_interval": 1,
            "max_stale": 2,
            "allow_private_addresses": False,
            "registry_allow_private_addresses": False,
        },
    ]


def test_public_url_value(chart_with_settings: Callable[[str], Path]) -> None:
    """`publicUrl` takes precedence over `hostName`."""
    chart = chart_with_settings(_gateway_settings("backendsKey: tes"))
    docs = index(template(chart, "--set", "publicUrl=https://example.org/a/"))
    assert _overlay(docs)["server"]["public_url"] == "https://example.org/a/"
    assert _overlay(docs)["custom"] == {}


def test_null_custom_with_backends(chart_with_settings: Callable[[str], Path]) -> None:
    """`config.custom: null` is like no custom config."""
    chart = chart_with_settings(_gateway_settings("backendsKey: tes"))
    docs = index(template(chart, "--set", "config.custom=null"))
    assert _overlay(docs)["custom"] == {}


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (
            (
                "--set-json",
                f"tes={json.dumps({'backends': BACKENDS})}",
                "--set-json",
                'config.custom={"tes":{"backends":[]}}',
            ),
            "either in tes.backends or in config.custom.tes.backends",
        ),
        (
            ("--set-json", 'config.custom={"tes":1}'),
            "config.custom.tes must be a map",
        ),
    ],
)
def test_backends_invalid(
    chart_with_settings: Callable[[str], Path],
    args: tuple[str, ...],
    message: str,
) -> None:
    """Backends that the app would refuse or lose fail."""
    chart = chart_with_settings(_gateway_settings("backendsKey: tes"))
    assert message in template_error(chart, *args)


def _registry_without_url(chart: Path, *args: str) -> str:
    """Render with a registry, without a host name of the app.

    Args:
        chart: Chart.
        *args: Further arguments of Helm.

    Returns:
        Rendered manifests.
    """
    return template(
        chart,
        "--set",
        "mongodb.auth.existingSecret=mongodb-credentials",
        "--set",
        "petstore.ingress.enabled=false",
        "--set-json",
        f"tes={json.dumps({'backends': BACKENDS})}",
        *args,
        required=False,
    )


def test_registry_needs_public_url(
    chart_with_settings: Callable[[str], Path],
) -> None:
    """Registries need a public URL, in any of its places."""
    chart = chart_with_settings(_gateway_settings("backendsKey: tes"))
    with pytest.raises(subprocess.CalledProcessError) as info:
        _registry_without_url(chart)
    assert "so that the app never discovers itself" in info.value.stderr
    assert _registry_without_url(chart, "--set", "publicUrl=https://example.org/")
    assert _registry_without_url(
        chart, "--set", "config.server.public_url=https://example.org/"
    )


def test_registry_with_public_url_of_settings(
    chart_with_settings: Callable[[str], Path],
) -> None:
    """A public URL from the app's settings counts."""
    settings = _gateway_settings(
        "backendsKey: tes",
        "config:",
        "  server:",
        "    public_url: https://example.org/",
    )
    assert _registry_without_url(chart_with_settings(settings))


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        (
            {"type": "ga4gh-tes", "url": "https://u:hunter2@tes.example.org/"},
            "must not hold credentials",
        ),
        (
            {"type": "ga4gh-tes", "url": " https://u:hunter2@tes.example.org/"},
            "must not hold credentials",
        ),
        (
            {"type": "ga4gh-tes", "url": "https://t", "basic": {"password": "hunter2"}},
            "not password",
        ),
        (
            {"type": "ga4gh-tes", "url": "https://t", "basic": "u:hunter2"},
            "must only name the environment variables",
        ),
        (
            {"type": "ga4gh-tes", "url": "https://t", "password": "hunter2"},
            "must not hold credentials (password)",
        ),
        (
            {"type": "ga4gh-tes", "url": "https://t/?a=1&Access_Token=hunter2"},
            "must not hold credentials",
        ),
        (
            {"type": "ga4gh-tes", "url": "https://t/#api-key=hunter2"},
            "must not hold credentials",
        ),
        (
            {"type": "ga4gh-tes", "url": "https://t", "token_env": "hunter2"},
            "must not hold credentials (token_env)",
        ),
    ],
)
def test_backend_credentials_fail(
    chart_with_settings: Callable[[str], Path], entry: dict[str, Any], message: str
) -> None:
    """Credentials don't get into the ConfigMap, and aren't named in the error."""
    chart = chart_with_settings(_gateway_settings("backendsKey: tes"))
    for args in (
        ("--set-json", f"tes={json.dumps({'backends': [entry]})}"),
        (
            "--set-json",
            f"config.custom={json.dumps({'tes': {'backends': [entry]}})}",
        ),
    ):
        error = template_error(chart, *args)
        assert message in error
        assert "hunter2" not in error


def test_backend_keys_with_underscore_stay(
    chart_with_settings: Callable[[str], Path],
) -> None:
    """Keys that are in snake_case already aren't converted."""
    chart = chart_with_settings(_gateway_settings("backendsKey: tes"))
    entry = {"type": "ga4gh-tes", "url": "https://t", "ga4gh_id": "a"}
    docs = index(
        template(chart, "--set-json", f"tes={json.dumps({'backends': [entry]})}")
    )
    assert _overlay(docs)["custom"]["tes"]["backends"] == [entry]


def test_registry_in_config_needs_public_url(
    chart_with_settings: Callable[[str], Path],
) -> None:
    """Registries in the config are checked, too, with its public URL first."""
    chart = chart_with_settings(_gateway_settings("backendsKey: tes"))
    custom = json.dumps({"tes": {"backends": [BACKENDS[1]]}})
    error = template_error(
        chart,
        "--set-json",
        f"config.custom={custom}",
        "--set-json",
        'config.server={"public_url":null}',
    )
    assert "so that the app never discovers itself" in error


def test_backend_variable_names_accepted(
    chart_with_settings: Callable[[str], Path],
) -> None:
    """Keys ending in `_env` may name environment variables."""
    chart = chart_with_settings(_gateway_settings("backendsKey: tes"))
    entry = {"type": "ga4gh-tes", "url": "https://t/?a=1", "token_env": "API_TOKEN"}
    docs = index(
        template(chart, "--set-json", f"tes={json.dumps({'backends': [entry]})}")
    )
    assert _overlay(docs)["custom"]["tes"]["backends"] == [entry]


@pytest.mark.parametrize(
    ("backends", "message"),
    [
        ({"url": "https://u:hunter2@t/"}, "must be a list"),
        ("https://u:hunter2@t/", "must be a list"),
        (["https://u:hunter2@t/"], "must be a list of maps"),
    ],
)
def test_backends_malformed_fail(
    chart_with_settings: Callable[[str], Path], backends: Any, message: str
) -> None:
    """Malformed backends fail, as their credentials wouldn't be recognized."""
    chart = chart_with_settings(_gateway_settings("backendsKey: tes"))
    for args in (
        ("--set-json", f"tes={json.dumps({'backends': backends})}"),
        (
            "--set-json",
            f"config.custom={json.dumps({'tes': {'backends': backends}})}",
        ),
    ):
        error = template_error(chart, *args)
        assert message in error
        assert "hunter2" not in error


@pytest.mark.parametrize(
    "args",
    [
        ("--set", "publicUrl=https://u:hunter2@example.org/"),
        ("--set", "config.server.public_url=https://u:hunter2@example.org/"),
    ],
)
def test_public_url_credentials_fail(
    chart_with_settings: Callable[[str], Path], args: tuple[str, ...]
) -> None:
    """Credentials in the public URL don't get into the ConfigMap."""
    chart = chart_with_settings(_gateway_settings("backendsKey: tes"))
    error = template_error(chart, *args)
    assert "must not hold credentials" in error
    assert "hunter2" not in error


def test_access_control_of_settings_counts(
    chart_with_settings: Callable[[str], Path],
) -> None:
    """Access control turned off by the settings of the app fails, too."""
    chart = chart_with_settings(
        _gateway_settings(
            "requireAccessControl: true",
            "config:\n  security:\n    access_control:\n      enforce: false",
        )
    )
    assert "enabled and enforce must be true" in template_error(chart, *SECURE)


def _group_rule(**access_control: Any) -> tuple[str, str]:
    """Get arguments for an admin role that only a rule of a group grants.

    Args:
        **access_control: Further settings of `security.access_control`.

    Returns:
        Arguments of Helm.
    """
    security = {
        "auth": {"issuers": ["https://login.example.org/"]},
        "access_control": {
            "claim_groups": "groups",
            "policies": [["g", "group:admins", "admin"]],
            **access_control,
        },
    }
    return ("--set-json", f"config.security={json.dumps(security)}")


def test_group_rule_counts_without_group_mapping(
    chart_with_settings: Callable[[str], Path],
) -> None:
    """Without a group mapping, as in the app, rules of groups grant roles."""
    chart = chart_with_settings(_gateway_settings("requireAccessControl: true"))
    assert index(template(chart, *_group_rule()))


def test_group_rule_ignored_with_group_mapping(
    chart_with_settings: Callable[[str], Path],
) -> None:
    """With a group mapping, as in the app, rules of groups don't count."""
    chart = chart_with_settings(_gateway_settings("requireAccessControl: true"))
    error = template_error(chart, *_group_rule(group_mapping={"a": "reader"}))
    assert "must grant the role admin" in error


def test_access_control_accepted(chart_with_settings: Callable[[str], Path]) -> None:
    """Valid security settings render."""
    chart = chart_with_settings(_gateway_settings("requireAccessControl: true"))
    assert index(template(chart, *SECURE))
    no_admin = _security(require_admin="no", group_mapping=None)
    assert index(template(chart, *no_admin))
    issuers = _issuers(
        [
            {
                "issuer": "https://login.example.org/",
                "audience": ["a"],
                "claim_groups": "groups",
                "namespace": "a",
                "userinfo_claims": ["groups"],
            },
            {"issuer": "https://other.example.org/", "audience": None},
        ],
        audience=None,
    )
    assert index(template(chart, *issuers))
    numeric = _security(enabled=1, enforce="on")
    assert index(template(chart, *numeric))
    client = _security(group_mapping=None, client_mapping={"svc": "admin"})
    assert index(template(chart, *client))
    rule = _security(group_mapping=None, policies=[["g", "user:alice", "admin"]])
    assert index(template(chart, *rule))
    group = _security(group_mapping=None, policies=[["g", "group:a", "admin"]])
    assert index(template(chart, *group))
    named = _security(
        group_mapping=None,
        policies={"admins": ["g", "user:alice", "admin"], "other": None},
    )
    assert index(template(chart, *named))
    kept = _security(
        group_mapping={"a": "user"},
        keep_group_subjects=True,
        policies=[["g", "group:a", "admin"]],
    )
    assert index(template(chart, *kept))
    no_claim = _security(claim_groups=None)
    with_issuer = json.loads(no_claim[1].split("=", 1)[1])
    with_issuer["auth"]["issuers"] = [
        {"issuer": "https://login.example.org/", "claim_groups": "groups"}
    ]
    assert index(
        template(chart, "--set-json", f"config.security={json.dumps(with_issuer)}")
    )


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ((), "must list the trusted token issuers"),
        (_security(enabled=False), "enabled and enforce must be true"),
        (_security(enforce="off"), "enabled and enforce must be true"),
        (_security(enforce=0), "enabled and enforce must be true"),
        (_security(enabled="maybe"), "enabled must be true or false, not maybe"),
        (_security(enforce=None), "enforce must be true or false, not null"),
        (_security(require_admin="x"), "require_admin must be true or false"),
        (_security(group_mapping={}), "must grant the role admin"),
        (_security(group_mapping={"a": "user"}), "must grant the role admin"),
        (_security(group_mapping=[]), "must be a map of groups to roles"),
        (
            _security(group_mapping=None, client_mapping={"svc": "user"}),
            "must grant the role admin",
        ),
        (
            _security(group_mapping=None, policies=[["g", "user:a", "user"]]),
            "must grant the role admin",
        ),
        (
            _security(group_mapping=None, policies=[["g2", "user:a", "admin"]]),
            "must grant the role admin",
        ),
        (
            _security(
                group_mapping={"a": "user"}, policies=[["g", "group:a", "admin"]]
            ),
            "must grant the role admin",
        ),
        (
            _security(
                group_mapping=None,
                claim_groups="",
                policies=[["g", "group:a", "admin"]],
            ),
            "must grant the role admin",
        ),
        (
            _security(keep_group_subjects="x"),
            "keep_group_subjects must be true or false",
        ),
        (_security(claim_groups=""), "must name the claim with the groups"),
        (_security(claim_groups=" a"), "without surrounding whitespace"),
        (_security(claim_groups=1), "must be the name of a claim (a string)"),
        (_issuers([]), "must list the trusted token issuers"),
        (_issuers("x"), "must list the trusted token issuers"),
        (_issuers([" https://a/"]), "must list the trusted token issuers"),
        (_issuers([{"url": "https://a/"}]), "url of issuer"),
        (_issuers([{"audience": []}]), "must list the trusted token issuers"),
        (
            _issuers([{"issuer": "https://a/", "audience": []}]),
            "must list the accepted audiences",
        ),
        (
            _issuers([{"issuer": "https://a/", "audience": [" "]}]),
            "must list the accepted audiences",
        ),
        (
            _issuers([{"issuer": "https://a/", "claim_groups": " a"}]),
            "must be the name of a claim",
        ),
        (
            _issuers(["https://a/"], audience="a"),
            "must list the accepted audiences (strings",
        ),
        (
            _issuers(["https://a/"], audience=[1]),
            "must list the accepted audiences (strings",
        ),
    ],
)
def test_access_control_invalid(
    chart_with_settings: Callable[[str], Path],
    args: tuple[str, ...],
    message: str,
) -> None:
    """Security settings that the app would refuse fail."""
    chart = chart_with_settings(_gateway_settings("requireAccessControl: true"))
    assert message in template_error(chart, *args)


def test_access_control_not_required_by_default(render: Callable[..., Docs]) -> None:
    """Without the option, security settings aren't required."""
    assert render()


def test_access_control_null_claim(chart_with_settings: Callable[[str], Path]) -> None:
    """A null group claim is no group claim."""
    chart = chart_with_settings(_gateway_settings("requireAccessControl: true"))
    error = template_error(chart, *_security(claim_groups=None))
    assert "must name the claim with the groups" in error


# Flower


FLOWER_TEMPLATES = {
    "flower-deployment.yaml": "foca-common.flowerDeployment",
    "flower-service.yaml": "foca-common.flowerService",
    "flower-networkpolicy.yaml": "foca-common.flowerNetworkPolicy",
    "flower-broker-networkpolicy.yaml": "foca-common.flowerBrokerNetworkPolicy",
}
FLOWER = (
    "--set",
    "flower.enabled=true",
    "--set",
    "flower.existingSecret=flower",
    "--set",
    "flower.image=flower:2.0.1",
    "--set",
    "broker.existingSecret=celery",
)


@pytest.fixture
def flower_chart(chart: Path, tmp_path: Path) -> Path:
    """A copy of the app chart with Flower's templates."""
    chart_copy = tmp_path / "chart"
    shutil.copytree(chart, chart_copy)
    for name, template_name in FLOWER_TEMPLATES.items():
        (chart_copy / "templates" / name).write_text(
            f'{{{{- include "{template_name}" '
            '(dict "root" . "settings" (include "petstore.settings" . | fromYaml)) }}\n'
        )
    return chart_copy


def test_flower_off_by_default(flower_chart: Path) -> None:
    """Flower isn't deployed by default."""
    docs = index(template(flower_chart))
    assert not [name for _, name in docs if "flower" in name]


def test_flower(flower_chart: Path) -> None:
    """Flower is deployed with its secrets, within the cluster only."""
    docs = index(
        template(
            flower_chart,
            *FLOWER,
            "--set",
            "flower.networkPolicy.enabled=true",
            "--set-json",
            'flower.securityContext={"readOnlyRootFilesystem":true}',
            "--set",
            "flower.tmpSizeLimit=64Mi",
        )
    )
    name = f"{APP}-flower"
    deployment = docs["Deployment", name]
    spec = deployment["spec"]["template"]["spec"]
    container = spec["containers"][0]
    assert spec["automountServiceAccountToken"] is False
    assert spec["enableServiceLinks"] is False
    assert container["image"] == "flower:2.0.1"
    assert container["securityContext"] == {"readOnlyRootFilesystem": True}
    assert spec["volumes"] == [{"name": "tmp", "emptyDir": {"sizeLimit": "64Mi"}}]
    assert _env(container) == {
        "CELERY_BROKER_URL": {"secretKeyRef": {"name": "celery", "key": "broker-url"}},
        "FLOWER_BASIC_AUTH": {"secretKeyRef": {"name": "flower", "key": "basic-auth"}},
    }
    assert docs["Service", name]["spec"]["type"] == "ClusterIP"
    assert docs["NetworkPolicy", name]["spec"]["ingress"] == []
    assert ("NetworkPolicy", f"{APP}-broker-from-flower") not in docs


def test_flower_network_policy_peers(flower_chart: Path) -> None:
    """Peers can be admitted, and the policy turned off."""
    peers = [{"podSelector": {"matchLabels": {"app": "proxy"}}}]
    docs = index(
        template(
            flower_chart,
            *FLOWER,
            "--set",
            "flower.networkPolicy.enabled=true",
            "--set-json",
            f"flower.networkPolicy.ingressFrom={json.dumps(peers)}",
        )
    )
    ingress = docs["NetworkPolicy", f"{APP}-flower"]["spec"]["ingress"]
    assert ingress == [{"from": peers, "ports": [{"port": 5555, "protocol": "TCP"}]}]
    docs = index(
        template(flower_chart, *FLOWER, "--set", "flower.networkPolicy.enabled=false")
    )
    assert ("NetworkPolicy", f"{APP}-flower") not in docs


def test_flower_safe_by_default(flower_chart: Path) -> None:
    """With what it needs only, Flower is unprivileged and admits no one."""
    docs = index(template(flower_chart, *FLOWER))
    name = f"{APP}-flower"
    assert docs["NetworkPolicy", name]["spec"]["ingress"] == []
    spec = docs["Deployment", name]["spec"]["template"]["spec"]
    assert spec["securityContext"]["runAsNonRoot"] is True
    assert spec["containers"][0]["securityContext"] == {
        "allowPrivilegeEscalation": False,
        "capabilities": {"drop": ["ALL"]},
        "readOnlyRootFilesystem": True,
    }


def test_flower_reaches_bundled_broker(flower_chart: Path) -> None:
    """The bundled broker's NetworkPolicy admits Flower."""
    docs = index(
        template(
            flower_chart,
            *FLOWER,
            "--set",
            "broker.enabled=true",
            "--set",
            "broker.networkPolicy.enabled=true",
        )
    )
    policy = docs["NetworkPolicy", f"{APP}-broker-from-flower"]
    peer = policy["spec"]["ingress"][0]["from"][0]["podSelector"]["matchLabels"]
    assert peer["app.kubernetes.io/component"] == "flower"
    assert policy["spec"]["ingress"][0]["ports"] == [{"port": 5672, "protocol": "TCP"}]


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (("--set", "flower.enabled=true"), "flower.image must be set"),
        (
            ("--set", "flower.enabled=true", "--set", "flower.image=x"),
            "flower.existingSecret must be set",
        ),
        (
            (
                "--set",
                "flower.enabled=true",
                "--set",
                "flower.image=x",
                "--set",
                "flower.existingSecret=x",
            ),
            "broker.existingSecret must be set if flower.enabled",
        ),
        (
            (*FLOWER, "--set", f"fullnameOverride={'a' * 56}-flower"),
            "does not end in -flower",
        ),
    ],
)
def test_flower_invalid(
    flower_chart: Path, args: tuple[str, ...], message: str
) -> None:
    """Flower without what it needs fails."""
    assert message in template_error(flower_chart, *args)


def test_flower_rollout_on_secrets(flower_chart: Path) -> None:
    """Flower's pods use the same checksum as those of the app."""
    for name in (flower_chart / "templates").iterdir():
        if name.name != "flower-deployment.yaml" and name.suffix == ".yaml":
            name.unlink()
    error = template_error(
        flower_chart,
        *FLOWER,
        "--skip-schema-validation",
        "--set-json",
        "petstore.rolloutOnSecrets=[1]",
    )
    assert "rolloutOnSecrets must be a list" in error


def test_flower_values_of_apps_without_its_templates(
    render: Callable[..., Docs],
) -> None:
    """Apps may have `flower` values of their own, which aren't checked."""
    assert render("--set", "flower.enabled=true")
