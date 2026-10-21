"""Snapshot tests of the library chart foca-common.

Each case renders the thin chart of the petstore example with `helm template`
and compares the output to a stored snapshot in `__snapshots__/`. After
intended changes of the output, update the snapshots with
`UPDATE_SNAPSHOTS=1 pytest charts/foca-common/tests` and review their diff.
"""

import os
from pathlib import Path

import pytest
from chart_helpers import HELM, template

pytestmark = pytest.mark.skipif(HELM is None, reason="helm not installed")

SNAPSHOTS = Path(__file__).parent / "__snapshots__"

# Arguments of `helm template`, by snapshot name
CASES: dict[str, tuple[str, ...]] = {
    "default": (),
    "mongodb-external": (
        "--set",
        "mongodb.enabled=false",
        "--set",
        "config.db.host=mongodb.example.org",
    ),
    "route": (
        "--set",
        "petstore.ingress.enabled=false",
        "--api-versions",
        "route.openshift.io/v1",
        "--api-versions",
        "security.openshift.io/v1",
    ),
    "ingress-https": (
        "--set",
        "petstore.ingress.https.enabled=true",
        "--set",
        "petstore.ingress.className=nginx",
        "--set",
        "config.server.max_content_length=1048576",
    ),
}


@pytest.mark.parametrize("name", CASES)
def test_snapshot(chart: Path, name: str) -> None:
    """Rendered manifests equal the stored snapshot."""
    rendered = template(chart, *CASES[name])
    snapshot = SNAPSHOTS / f"{name}.yaml"
    if os.environ.get("UPDATE_SNAPSHOTS"):
        SNAPSHOTS.mkdir(exist_ok=True)
        snapshot.write_text(rendered)
    assert snapshot.exists(), f"no snapshot {snapshot.name}"
    assert rendered == snapshot.read_text(), (
        f"output differs from {snapshot.name}; if intended, update the "
        "snapshots (with the Helm version of CI, see charts.yml)"
    )


def test_no_stale_snapshots() -> None:
    """Every stored snapshot belongs to a case."""
    stored = {path.stem for path in SNAPSHOTS.glob("*.yaml")}
    assert stored == set(CASES)
