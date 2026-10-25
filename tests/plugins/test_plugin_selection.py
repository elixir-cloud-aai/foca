"""Tests of per-request pipelines and backends (`foca.plugins.selection`)."""

import json
import random
from typing import Any

import pytest

from foca.plugins import Backend, Headers, InboundRequest, Principal
from foca.plugins.config import PluginsConfig
from foca.plugins.engine import PluginHost
from foca.plugins.loader import load_plugins
from foca.plugins.selection import (
    BackendSelection,
    InvalidSelection,
    apply_backend_selection,
    parse_client_pipeline,
    parse_client_tags,
)
from tests.plugins.fakes import FAKES, fake_entry_points, make_host

BACKENDS = [
    Backend(id="a", url="https://a.example.org"),
    Backend(id="b", url="https://b.example.org"),
    Backend(id="c", url="https://c.example.org"),
]
ALICE = Principal(subject="alice", groups=("member",))


def request(tags: dict[str, str] | None = None) -> InboundRequest:
    """Build an inbound request with tags.

    Args:
        tags: Tags of the task.

    Returns:
        Request.
    """
    payload: dict[str, Any] = {"name": "t"}
    if tags is not None:
        payload["tags"] = tags
    return InboundRequest(
        method="POST", path="/tasks", payload=payload, headers=Headers()
    )


def pipeline_tag(steps: Any) -> dict[str, str]:
    """Build the pipeline tag.

    Args:
        steps: Pipeline.

    Returns:
        Tags.
    """
    return {"gateway.pipeline": steps if isinstance(steps, str) else json.dumps(steps)}


def host(**conf: Any) -> Any:
    """Create a host with fake plugins and presets.

    Args:
        conf: Configuration of `client_pipelines`, `available` etc.

    Returns:
        Host.
    """
    available = conf.pop("available", None) or {name: {} for name in FAKES}
    return make_host(
        [{"plugin": "test.reverse"}],
        available=available,
        presets=conf.pop(
            "presets",
            {"tagging": [{"plugin": "test.tag", "params": {"value": "preset"}}]},
        ),
        **conf,
    )


def test_default_pipeline_without_tags() -> None:
    """Requests without tags get the default pipeline."""
    run = host().start(request(), ALICE)
    assert run.pipeline.source == "default"
    assert run.selection.pipeline is None
    run = host().start(request({"other": "x"}), ALICE)
    assert [s.id for s in run.pipeline.steps] == ["test.reverse"]


def test_preset() -> None:
    """Clients choose presets by name; the frame applies."""
    h = host(
        client_pipelines={
            "prepend": [{"id": "policy", "plugin": "test.header"}],
            "append": [
                {"id": "route", "plugin": "test.reverse", "if_missing_phase": "route"}
            ],
        }
    )
    run = h.start(request(pipeline_tag("tagging")), ALICE)
    assert run.pipeline.source == "preset"
    assert run.pipeline.preset == "tagging"
    assert [(s.id, s.origin) for s in run.pipeline.steps] == [
        ("policy", "prepend"),
        ("test.tag", "preset"),
        ("route", "append"),
    ]
    assert run.request().payload["tags"]["tagged"] == "preset"


def test_inline_pipeline_and_frame() -> None:
    """Inline pipelines are validated and framed."""
    h = host(
        client_pipelines={
            "append": [
                {"id": "route", "plugin": "test.reverse", "if_missing_phase": "route"},
                {"id": "always", "plugin": "test.header"},
            ]
        }
    )
    steps = [
        {"plugin": "test.tag", "params": {"value": "mine"}},
        {
            "id": "second",
            "alternatives": [{"plugin": "test.fail"}, {"plugin": "test.skip"}],
        },
    ]
    run = h.start(request(pipeline_tag(steps)), ALICE)
    assert run.pipeline.source == "client"
    assert [s.id for s in run.pipeline.steps] == [
        "test.tag",
        "second",
        "route",
        "always",
    ]
    steps.append({"plugin": "test.bad-router"})
    run = h.start(request(pipeline_tag(steps)), ALICE)
    assert "route" not in [s.id for s in run.pipeline.steps]


@pytest.mark.parametrize(
    ("conf", "tags", "message", "slug"),
    [
        ({}, {"gateway.backend": "x"}, "reserved for the gateway", "reserved-tag"),
        ({}, {"gateway.pipeline": "x" * 9000}, "at most 8192", "invalid-pipeline"),
        (
            {"client_pipelines": {"enabled": False}},
            pipeline_tag("tagging"),
            "disabled",
            "invalid-pipeline",
        ),
        (
            {"client_pipelines": {"allowed": ["inline"]}},
            pipeline_tag("tagging"),
            "Presets are not allowed",
            "invalid-pipeline",
        ),
        (
            {"client_pipelines": {"allowed": ["presets"]}},
            pipeline_tag([]),
            "preset name only",
            "invalid-pipeline",
        ),
        ({}, pipeline_tag("unknown"), "Preset not available", "invalid-pipeline"),
        ({}, pipeline_tag("Nope!"), "not a preset name or JSON", "invalid-pipeline"),
        ({}, pipeline_tag({"plugin": "test.tag"}), "list of steps", "invalid-pipeline"),
        (
            {},
            pipeline_tag([{"plugin": "test.tag", "timeout": 1}]),
            "timeout",
            "invalid-pipeline",
        ),
        (
            {},
            pipeline_tag([{"plugin": "test.other"}]),
            "not available: test.other",
            "invalid-pipeline",
        ),
        (
            {
                "available": {
                    **{n: {} for n in FAKES},
                    "test.tag": {"client_selectable": False},
                }
            },
            pipeline_tag([{"plugin": "test.tag"}]),
            "not available: test.tag",
            "invalid-pipeline",
        ),
        (
            {"available": {**{n: {} for n in FAKES}, "test.tag": {"roles": ["admin"]}}},
            pipeline_tag([{"plugin": "test.tag"}]),
            "not available: test.tag",
            "invalid-pipeline",
        ),
        (
            {"available": {**{n: {} for n in FAKES}, "test.tag": {"enabled": False}}},
            pipeline_tag([{"plugin": "test.tag"}]),
            "not available: test.tag",
            "invalid-pipeline",
        ),
        (
            {"client_pipelines": {"roles": ["admin"]}},
            pipeline_tag("tagging"),
            "not allowed",
            "invalid-pipeline",
        ),
        (
            {"client_pipelines": {"max_steps": 1}},
            pipeline_tag([{"plugin": "test.tag"}, {"plugin": "test.skip"}]),
            "At most 1 steps",
            "invalid-pipeline",
        ),
        (
            {"client_pipelines": {"max_alternatives": 1}},
            pipeline_tag(
                [
                    {
                        "id": "s",
                        "alternatives": [
                            {"plugin": "test.tag"},
                            {"plugin": "test.skip"},
                        ],
                    }
                ]
            ),
            "At most 1 alternatives",
            "invalid-pipeline",
        ),
        (
            {},
            pipeline_tag([{"plugin": "test.tag", "params": {"x": 1}}]),
            "Invalid params",
            "invalid-pipeline",
        ),
        (
            {"client_pipelines": {"prepend": [{"id": "p", "plugin": "test.skip"}]}},
            pipeline_tag([{"id": "p", "plugin": "test.tag"}]),
            "reserved for the gateway's steps; set an explicit `id`",
            "invalid-pipeline",
        ),
        ({}, {"gateway.pipeline": "\ud800"}, "UTF-8", "invalid-pipeline"),
        ({}, {"gateway.backends": "\ud800"}, "UTF-8", "invalid-backends"),
        (
            {},
            pipeline_tag([{"plugin": "test.tag"}, {"plugin": "test.tag"}]),
            "Duplicate step",
            "invalid-pipeline",
        ),
        (
            {"client_backends": {"enabled": False}},
            {"gateway.backends": '{"type": "allow", "backends": []}'},
            "disabled",
            "invalid-backends",
        ),
    ],
)
def test_invalid_selections(
    conf: dict[str, Any], tags: dict[str, str], message: str, slug: str
) -> None:
    """Invalid or disallowed choices are refused with 400."""
    with pytest.raises(InvalidSelection, match=message) as exc_info:
        host(**conf).start(request(tags), ALICE)
    assert exc_info.value.code == 400
    assert exc_info.value.problem_type.endswith(f"#{slug}")


def test_roles_allow_choice() -> None:
    """Callers with an allowed role may choose."""
    h = host(
        client_pipelines={"roles": ["member"]},
        available={**{n: {} for n in FAKES}, "test.tag": {"roles": ["member"]}},
    )
    run = h.start(request(pipeline_tag([{"plugin": "test.tag"}])), ALICE)
    assert run.pipeline.source == "client"


@pytest.mark.parametrize(
    "raw",
    [
        "x",
        '["a"]',
        '{"type": "first", "backends": []}',
        '{"type": "allow", "backends": [""]}',
        '{"type": "allow", "backends": ["a"], "x": 1}',
    ],
)
def test_invalid_backends(raw: str) -> None:
    """Backend selections must be valid JSON objects."""
    with pytest.raises(InvalidSelection) as exc_info:
        parse_client_tags({"gateway.backends": raw}, "gateway.", 8192, 32)
    assert exc_info.value.problem_type.endswith("#invalid-backends")
    with pytest.raises(InvalidSelection):
        parse_client_tags({"gateway.backends": 1}, "gateway.", 8192, 32)


def test_too_many_backends() -> None:
    """The number of selected backends is limited."""
    raw = json.dumps({"type": "allow", "backends": ["a", "b"]})
    with pytest.raises(InvalidSelection, match="At most 1"):
        parse_client_tags({"gateway.backends": raw}, "gateway.", 8192, 1)
    assert parse_client_tags(None, "gateway.", 1, 1).pipeline is None
    with pytest.raises(InvalidSelection, match="strings"):
        parse_client_tags({"gateway.pipeline": 1}, "gateway.", 8192, 1)


@pytest.mark.parametrize(
    ("kind", "items", "expected", "discarded"),
    [
        ("order", ["c", "https://A.example.org/", "x"], ["c", "a", "b"], ["x"]),
        ("order", ["c", "c"], ["c", "a", "b"], []),
        ("allow", ["c", "a", "not a url"], ["a", "c"], ["not a url"]),
        ("forbid", ["b"], ["a", "c"], []),
    ],
)
def test_apply_backend_selection(
    kind: str, items: list[str], expected: list[str], discarded: list[str]
) -> None:
    """Selections reorder or filter candidates; others are discarded."""
    selection = BackendSelection.model_validate({"type": kind, "backends": items})
    result, rest = apply_backend_selection(BACKENDS, selection)
    assert [b.id for b in result] == expected
    assert rest == discarded


def test_route_applies_backend_selection() -> None:
    """The client's selection applies after the route phase."""
    tags = {"gateway.backends": json.dumps({"type": "order", "backends": ["c", "z"]})}
    run = host().start(request(tags), ALICE)
    assert [b.id for b in run.route(BACKENDS)] == ["c", "b", "a"]
    assert run.backends_discarded == ["z"]
    assert run.candidates_out == ["c", "b", "a"]


def test_parse_client_pipeline() -> None:
    """Client pipelines use the step schema without admin keys."""
    steps = parse_client_pipeline('[{"plugin": "a.b", "params": {"x": 1}}]')
    assert steps[0].alternatives[0].params == {"x": 1}
    steps = parse_client_pipeline('[{"alternatives": [{"plugin": "a.b"}]}]')
    assert steps[0].id == "a.b"
    with pytest.raises(InvalidSelection, match="on_failure"):
        parse_client_pipeline('[{"plugin": "a.b", "on_failure": "skip"}]')
    with pytest.raises(InvalidSelection, match="Invalid pipeline"):
        parse_client_pipeline('[{"plugin": "a.b", "alternatives": []}]')


def test_config_validation() -> None:
    """Presets and frames refer to allowlisted plugins."""
    with pytest.raises(ValueError, match=r"presets\.x"):
        PluginsConfig.model_validate({"presets": {"x": [{"plugin": "a.b"}]}})
    with pytest.raises(ValueError, match="pattern"):
        PluginsConfig.model_validate({"presets": {"Bad Name": []}})
    with pytest.raises(ValueError, match="prefix"):
        PluginsConfig.model_validate({"tags": {"prefix": "gateway"}})
    with pytest.raises(ValueError, match="Duplicate step"):
        PluginsConfig.model_validate(
            {
                "available": {"a.b": {}},
                "client_pipelines": {
                    "prepend": [{"id": "x", "plugin": "a.b"}],
                    "append": [{"id": "x", "plugin": "a.b"}],
                },
            }
        )


def test_presets_with_disabled_plugins() -> None:
    """Presets that need disabled plugins are unavailable."""
    available = {**{n: {} for n in FAKES}, "test.tag": {"enabled": False}}
    h = host(
        available=available,
        presets={
            "tagging": [{"plugin": "test.tag"}],
            "optional": [{"plugin": "test.tag", "on_failure": "skip"}],
        },
        client_pipelines={
            "append": [
                {"id": "gone", "plugin": "test.tag", "on_failure": "skip"},
                {"id": "route", "plugin": "test.reverse"},
            ]
        },
    )
    assert list(h.presets) == ["optional"]
    with pytest.raises(InvalidSelection, match="Preset not available"):
        h.start(request(pipeline_tag("tagging")), ALICE)
    run = h.start(request(pipeline_tag("optional")), ALICE)
    assert [s.id for s in run.pipeline.steps] == ["route"]
    with pytest.raises(ValueError, match="Invalid params"):
        host(presets={"bad": [{"plugin": "test.tag", "params": {"x": 1}}]})


def test_tags_not_a_mapping() -> None:
    """Tags that are not a map are refused, not ignored."""
    req = request()
    req.payload["tags"] = ["gateway.pipeline", "[]"]
    with pytest.raises(InvalidSelection, match="map of strings") as exc_info:
        host().start(req, ALICE)
    assert exc_info.value.code == 400


def test_tags_of_other_apis() -> None:
    """Apps of other APIs pass the tags explicitly."""
    conf = PluginsConfig.model_validate(
        {"available": {"test.tag": {}}, "presets": {"p": ["test.tag"]}}
    )
    plugins = load_plugins(conf, api="wes", find_entry_points=fake_entry_points())
    wes = PluginHost(conf, api="wes", plugins=plugins)
    with pytest.raises(TypeError, match="tags="):
        wes.start(request(), ALICE)
    run = wes.start(request(), ALICE, tags={"gateway.pipeline": "p"})
    assert run.pipeline.preset == "p"
    assert wes.start(request(), ALICE, tags=None).pipeline.source == "default"
    wes.close()


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (json.dumps({"type": "allow", "backends": ["a" * 2000] * 5}), "8192 bytes"),
        ('{"type": "allow", "backends": []}', "JSON"),
    ],
)
def test_backend_selection_limits(raw: str, message: str) -> None:
    """Backend selections are bounded in size and not empty."""
    with pytest.raises(InvalidSelection, match=message) as exc_info:
        parse_client_tags({"gateway.backends": raw}, "gateway.", 8192, 32)
    assert exc_info.value.problem_type.endswith("#invalid-backends")


def test_preset_roles() -> None:
    """Presets need the roles of their plugins, not `client_selectable`."""
    available = {
        **{n: {} for n in FAKES},
        "test.tag": {"client_selectable": False},
        "test.skip": {"roles": ["admin"]},
    }
    h = host(
        available=available,
        presets={"tagging": [{"plugin": "test.tag"}], "admins": ["test.skip"]},
    )
    assert h.start(request(pipeline_tag("tagging")), ALICE).pipeline.preset == "tagging"
    with pytest.raises(InvalidSelection, match="Preset not available: admins"):
        h.start(request(pipeline_tag("admins")), ALICE)
    admin = Principal(subject="root", groups=("admin",))
    assert h.start(request(pipeline_tag("admins")), admin).pipeline.preset == "admins"


@pytest.mark.parametrize(
    ("conf", "message"),
    [
        (
            {"client_pipelines": {"prepend": [{"plugin": "a.b"}]}},
            "'a.b' of prepend",
        ),
        ({"client_pipelines": {"allowed": []}}, "at least 1"),
        ({"presets": {"p": [{"plugin": "a.b"}]}}, r"'a\.b' of presets\.p"),
    ],
)
def test_invalid_frames(conf: dict[str, Any], message: str) -> None:
    """Frames, presets and forms are checked at startup."""
    with pytest.raises(ValueError, match=message):
        PluginsConfig.model_validate(conf)


ALLOWED = ["test.tag", "test.skip", "test.header"]
FORBIDDEN = ["test.fail", "test.reject", "test.slow", "test.none", "test.off"]


def test_random_pipelines_forbidden_plugins() -> None:
    """No random client pipeline gets a forbidden plugin through."""
    available = {
        **{n: {} for n in FAKES},
        "test.fail": {"client_selectable": False},
        "test.reject": {"roles": ["admin"]},
        "test.slow": {"enabled": False},
        "test.off": {"client_selectable": False, "enabled": False},
    }
    h = host(available=available, client_pipelines={"max_steps": 4})
    rng = random.Random(487)  # noqa: S311 (reproducible test data)
    for _ in range(300):
        steps: list[dict[str, Any]] = [
            {
                "id": f"s{index}",
                "alternatives": [
                    {"plugin": rng.choice(ALLOWED + FORBIDDEN)}
                    for _ in range(rng.randint(1, 3))
                ],
            }
            for index in range(rng.randint(0, 4))
        ]
        plugins = {alt["plugin"] for step in steps for alt in step["alternatives"]}
        tags = pipeline_tag(steps)
        if plugins & set(FORBIDDEN):
            with pytest.raises(InvalidSelection, match="not available"):
                h.start(request(tags), ALICE)
        else:
            run = h.start(request(tags), ALICE)
            used = {
                alt.plugin
                for step in run.pipeline.steps
                if step.origin == "client"
                for alt in step.alternatives
            }
            assert used <= set(ALLOWED)
    h.close()


def test_frame_clashes_with_kept_steps_only() -> None:
    """Identifiers clash only with frame steps that are kept."""
    append = [{"id": "route", "plugin": "test.reverse", "if_missing_phase": "route"}]
    h = host(client_pipelines={"append": append})
    own_router = [{"id": "route", "plugin": "test.bad-router"}]
    run = h.start(request(pipeline_tag(own_router)), ALICE)
    assert [s.id for s in run.pipeline.steps] == ["route"]
    with pytest.raises(InvalidSelection, match="set an explicit `id`"):
        h.start(request(pipeline_tag([{"id": "route", "plugin": "test.tag"}])), ALICE)
    h.close()
    with pytest.raises(ValueError, match=r"'route' of presets\.p.*explicit `id`"):
        host(
            client_pipelines={"append": append},
            presets={"p": [{"id": "route", "plugin": "test.tag"}]},
        )
    h = host(
        client_pipelines={"append": append},
        presets={"p": own_router},
    )
    assert [s.id for s in h.presets["p"]] == ["route"]
    h.close()
    prepend = [{"id": "x", "plugin": "test.skip"}]
    with pytest.raises(ValueError, match=r"'x' of presets\.p.*prepend"):
        host(
            client_pipelines={"prepend": prepend},
            presets={"p": [{"id": "x", "plugin": "test.tag"}]},
        )
    # frame steps without enabled plugins are dropped, so they reserve nothing
    available = {**{name: {} for name in FAKES}, "test.skip": {"enabled": False}}
    h = host(
        available=available,
        client_pipelines={"prepend": [{**prepend[0], "on_failure": "skip"}]},
        presets={"p": [{"id": "x", "plugin": "test.tag"}]},
    )
    assert [s.id for s in h.presets["p"]] == ["x"]
    run = h.start(request(pipeline_tag([{"id": "x", "plugin": "test.tag"}])), ALICE)
    assert [s.id for s in run.pipeline.steps] == ["x", "foca.routing.ordered"]
    h.close()


def test_choice_tags_must_be_strings() -> None:
    """Choice tags that are present count, whatever their value."""
    h = host(client_pipelines={"enabled": False}, client_backends={"enabled": False})
    for tag in ("gateway.pipeline", "gateway.backends"):
        with pytest.raises(InvalidSelection, match="disabled"):
            h.start(request({tag: None}), ALICE)  # type: ignore[dict-item]
    h.close()
    h = host()
    with pytest.raises(InvalidSelection, match="UTF-8 strings"):
        h.start(request({"gateway.pipeline": None}), ALICE)  # type: ignore[dict-item]
    with pytest.raises(InvalidSelection, match="selected as JSON"):
        h.start(request({"gateway.backends": None}), ALICE)  # type: ignore[dict-item]
    h.close()


def test_duplicate_keys() -> None:
    """Objects of client pipelines have unique keys."""
    with pytest.raises(InvalidSelection, match="duplicate keys"):
        parse_client_pipeline('[{"plugin": "a.b", "plugin": "c.d"}]')


def test_selection_namespaces() -> None:
    """URLs match URLs only, other items identifiers only."""
    candidates = [
        Backend(id="https://b.example.org", url="https://a.example.org"),
        Backend(id="b", url="https://b.example.org"),
    ]
    selection = BackendSelection.model_validate(
        {"type": "allow", "backends": ["https://B.example.org/"]}
    )
    assert [b.id for b in apply_backend_selection(candidates, selection)[0]] == ["b"]
    selection = BackendSelection.model_validate(
        {"type": "allow", "backends": ["a.example.org"]}
    )
    assert apply_backend_selection(candidates, selection) == ([], ["a.example.org"])
    selection = BackendSelection.model_validate(
        {"type": "allow", "backends": ["https://[bad"]}
    )
    assert apply_backend_selection(candidates, selection) == ([], ["https://[bad"])


@pytest.mark.parametrize(
    "value",
    [
        '[{"plugin": "a.b", "params": {"x": "\\ud800"}}]',
        '[{"plugin": "a.b", "params": {"\\udfff": 1}}]',
    ],
)
def test_client_pipelines_are_unicode(value: str) -> None:
    """Client pipelines have no unpaired surrogates."""
    with pytest.raises(InvalidSelection, match="valid Unicode"):
        parse_client_pipeline(value)


def test_client_pipelines_nesting() -> None:
    """Client pipelines are nested 32 levels deep at most."""
    # 29 levels, plus the pipeline array, the step and its params
    nested = "[" * 29 + "]" * 29
    parse_client_pipeline(f'[{{"plugin": "a.b", "params": {{"x": {nested}}}}}]')
    nested = "[" * 30 + "]" * 30
    with pytest.raises(InvalidSelection, match="at most 32 levels"):
        parse_client_pipeline(f'[{{"plugin": "a.b", "params": {{"x": {nested}}}}}]')


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ('[{"plugin": "a.b", "params": {"x": NaN}}]', "NaN is not allowed"),
        ('[{"plugin": "a.b", "params": {"x": -Infinity}}]', "-Infinity"),
        ('[{"plugin": "a.b", "params": {"x": 1e999}}]', "finite"),
        ('[{"plugin": "a.b", "params": {"x": [9007199254740992]}}]', "2\\^53"),
    ],
)
def test_client_pipelines_are_i_json(value: str, message: str) -> None:
    """Client pipelines have finite numbers and exact integers only."""
    with pytest.raises(InvalidSelection, match=message):
        parse_client_pipeline(value)
    steps = parse_client_pipeline('[{"plugin": "a.b", "params": {"x": [1.5, null]}}]')
    assert steps[0].alternatives[0].params == {"x": [1.5, None]}
