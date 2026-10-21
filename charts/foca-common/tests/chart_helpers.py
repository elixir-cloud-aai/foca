"""Helpers for the tests of the library chart foca-common.

The library chart is rendered via the thin chart of the petstore example
(`examples/petstore/chart`), copied to a temporary directory with its
dependency built (`helm dependency build`), as library charts cannot be
rendered by themselves.
"""

import shutil
import subprocess
from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).parents[3]
LIBRARY = REPO / "charts" / "foca-common"
APP_CHART = REPO / "examples" / "petstore" / "chart"
HELM = shutil.which("helm")
RELEASE = "petstore"
# Values required by default: the host name of the Ingress and the Secret
# with the MongoDB credentials
REQUIRED_VALUES = (
    "--set",
    "hostName=petstore.example.org",
    "--set",
    "mongodb.auth.existingSecret=mongodb-credentials",
)

# A rendered manifest (Kubernetes object)
Manifest = dict[str, Any]
# Rendered manifests by kind and name
Docs = dict[tuple[str, str], Manifest]


def helm(*args: str) -> str:
    """Run Helm.

    Args:
        args: Arguments of Helm.

    Returns:
        Standard output of Helm.

    Raises:
        subprocess.CalledProcessError: Helm failed; its standard error is in
            `stderr`.
    """
    assert HELM is not None
    return subprocess.run(
        [HELM, *args],
        capture_output=True,
        check=True,
        text=True,
    ).stdout


def build_chart(directory: Path, settings: str | None = None) -> Path:
    """Copy the app chart and build its dependency on the library chart.

    Args:
        directory: Directory to copy the chart to.
        settings: Template `petstore.settings` replacing that of the app
            chart (its body, YAML), if given.

    Returns:
        The directory of the copied chart.
    """
    chart = directory / "chart"
    shutil.copytree(APP_CHART, chart, ignore=shutil.ignore_patterns("charts"))
    # The relative path of the dependency must resolve from the copy
    chart_yaml = chart / "Chart.yaml"
    chart_yaml.write_text(
        chart_yaml.read_text().replace(
            "file://../../../charts/foca-common", f"file://{LIBRARY}"
        )
    )
    (chart / "Chart.lock").unlink(missing_ok=True)
    if settings is not None:
        (chart / "templates" / "_helpers.tpl").write_text(
            '{{- define "petstore.settings" -}}\n'
            f"{settings.strip()}\n"
            "{{- end -}}\n"
        )
    helm("dependency", "update", "--skip-refresh", str(chart))
    return chart


def template(chart: Path, *args: str, required: bool = True) -> str:
    """Render a chart with `helm template`.

    Args:
        chart: Chart directory.
        args: Further arguments of `helm template`.
        required: Whether to set the values required by default.

    Returns:
        The rendered manifests, as Helm prints them.
    """
    return helm(
        "template",
        RELEASE,
        str(chart),
        *(REQUIRED_VALUES if required else ()),
        *args,
    )


def template_error(chart: Path, *args: str, required: bool = True) -> str:
    """Render a chart with `helm template`, expecting Helm to fail.

    Args:
        chart: Chart directory.
        args: Further arguments of `helm template`.
        required: Whether to set the values required by default.

    Returns:
        The error of Helm (its standard error).
    """
    try:
        template(chart, *args, required=required)
    except subprocess.CalledProcessError as error:
        stderr: str = error.stderr
        return stderr
    raise AssertionError("rendering did not fail")


def index(rendered: str) -> Docs:
    """Index rendered manifests by kind and name.

    Args:
        rendered: Output of `helm template`.

    Returns:
        The manifests, by kind and name.
    """
    return {
        (doc["kind"], doc["metadata"]["name"]): doc
        for doc in yaml.safe_load_all(rendered)
        if doc
    }
