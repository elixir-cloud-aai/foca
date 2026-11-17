"""Tests of WES run requests for plugins (`foca.plugins.wes`)."""

import copy
import doctest
from typing import Any

import pytest

from foca.plugins import (
    Attachment,
    Context,
    Headers,
    InboundRequest,
    OutboundRequest,
    Plugin,
    model,
    wes,
)
from foca.plugins.base import EmptyModel
from foca.plugins.config import AttachmentsConfig, PluginsConfig
from foca.plugins.errors import InvalidSelection
from foca.plugins.testing import (
    PluginHarness,
    check_plugin_contract,
    make_inbound,
    make_run_request,
)
from foca.plugins.wes import (
    ATTACHMENT_FIELD,
    AttachmentsTooLarge,
    InvalidRunRequest,
    check_attachments,
    check_filename,
    check_run_request,
    parse_run_request,
    run_request_form,
)

CWL = b"cwlVersion: v1.2\nclass: Workflow\ninputs: []\noutputs: []\nsteps: []\n"


class AttachPlugin(Plugin[EmptyModel, EmptyModel]):
    """Attach a workflow and point `workflow_url` to it.

    As plugins that fetch workflows (e.g., from TRS) do.
    """

    apis = frozenset({"wes"})
    filename = "wf/main.cwl"

    def on_request(self, request: InboundRequest, ctx: Context) -> None:
        """Attach the workflow.

        Args:
            request: Inbound request.
            ctx: Context.
        """
        payload = request.payload
        payload[ATTACHMENT_FIELD].append(Attachment(self.filename, CWL, "text/plain"))
        payload["workflow_url"] = self.filename

    def on_forward(self, outbound: OutboundRequest, ctx: Context) -> None:
        """Attach another file per attempt.

        Args:
            outbound: Outbound request.
            ctx: Context.
        """
        outbound.payload[ATTACHMENT_FIELD].append(
            Attachment(f"extra/{self.filename}", b"x")
        )


class UnsafePlugin(AttachPlugin):
    """Attach a workflow outside the run's directory."""

    apis = frozenset({"*"})
    filename = "../main.cwl"


class Name(str):
    """String that claims to be a safe path."""

    def split(self, *args: Any, **kwargs: Any) -> list[str]:
        """Claim to have one safe segment.

        Args:
            args: Ignored.
            kwargs: Ignored.

        Returns:
            One safe segment.
        """
        return ["ok"]


class ShortBytes(bytes):
    """Bytes that claim to be empty."""

    def __len__(self) -> int:
        """Claim to be empty.

        Returns:
            0.
        """
        return 0


class ShortList(list[Any]):
    """List that claims to be empty."""

    def __len__(self) -> int:
        """Claim to be empty.

        Returns:
            0.
        """
        return 0


class SmallAttachment(Attachment):
    """Attachment that claims to be empty."""

    @property
    def size(self) -> int:
        """Claim to be empty.

        Returns:
            0.
        """
        return 0


class NoopPlugin(Plugin[EmptyModel, EmptyModel]):
    """Change nothing."""

    apis = frozenset({"wes"})

    def on_request(self, request: InboundRequest, ctx: Context) -> None:
        """Change nothing.

        Args:
            request: Inbound request.
            ctx: Context.
        """


def test_doctests() -> None:
    """The examples of the model and of the WES module work."""
    assert doctest.testmod(model).failed == 0
    assert doctest.testmod(wes).failed == 0


# Attachments


def test_attachment() -> None:
    """Attachments are immutable values without content in their repr."""
    attachment = Attachment("main.cwl", b"abc", "text/plain")
    assert attachment.size == 3
    assert attachment.digest.startswith("sha256:ba7816bf")
    assert repr(attachment) == (
        "Attachment(filename='main.cwl', media_type='text/plain', size=3)"
    )
    assert copy.deepcopy(attachment) is attachment
    assert Attachment("a", b"").media_type == "application/octet-stream"
    with pytest.raises(AttributeError):
        attachment.filename = "other"  # type: ignore[misc]
    with pytest.raises(TypeError, match="bytes"):
        Attachment("a", "text")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="bytes"):
        Attachment("a", bytearray(b"x"))  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="bytes"):
        Attachment("a", ShortBytes(b"xyz"))
    with pytest.raises(TypeError, match="filenames"):
        Attachment(Name("../x"), b"")
    with pytest.raises(TypeError, match="filenames"):
        Attachment(1, b"")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="media types"):
        Attachment("a", b"", None)  # type: ignore[arg-type]
    assert attachment == Attachment("main.cwl", b"abc", "text/plain")


@pytest.mark.parametrize(
    "filename",
    ["main.cwl", "a/b/c.wdl", ".hidden", "a..b", "ä/ü.cwl", "x" * 255, "a b.txt"],
)
def test_safe_filenames(filename: str) -> None:
    """Relative paths in the run's directory are safe."""
    assert check_filename(filename) == filename


@pytest.mark.parametrize(
    ("filename", "reason"),
    [
        (1, "string"),
        ("", "empty"),
        ("/etc/passwd", "absolute"),
        ("C:/x", "drive"),
        ("c:x", "drive"),
        ("../x", r"'\.' or '\.\.'"),
        ("a/../../x", r"'\.' or '\.\.'"),
        ("a/./x", r"'\.' or '\.\.'"),
        ("..", r"'\.' or '\.\.'"),
        ("a//b", "empty"),
        ("a/", "empty"),
        ("a\\b", "backslash"),
        ("..\\x", "backslash"),
        ("a\x00b", "control"),
        ("a\nb", "control"),
        ("a\u202eb", "control"),
        ("a\u2028b", "control"),  # line separator
        ("a\u2029b", "control"),  # paragraph separator
        ("a\udc80", "UTF-8"),
        ("x" * 256, "255"),
        ("a/" * 600 + "b", "1024"),
        ("\uff0e\uff0e/x", r"'\.' or '\.\.'"),  # fullwidth dots: NFKC `..`
        ("\uff0fetc", "absolute"),  # fullwidth solidus: NFKC `/`
        ("\ufdfa" * 9, "255"),  # 27 bytes, but over 255 after NFKC
        ("/".join(["\ufdfa" * 7] * 40), "after NFKC"),  # 879 bytes
        (Name("/../x"), "string"),  # subclasses could fake checks
        ("a\uff3cb", "backslash"),  # fullwidth reverse solidus
    ],
)
def test_unsafe_filenames(filename: Any, reason: str) -> None:
    """Absolute paths, `..`, backslashes and controls are refused."""
    with pytest.raises(InvalidRunRequest, match=reason) as info:
        check_filename(filename)
    if isinstance(filename, str) and len(filename) > 2:
        assert filename not in str(info.value)


def test_check_attachments() -> None:
    """Attachments are checked for type, names, media types and limits."""
    limits = AttachmentsConfig(max_count=2, max_size=4)
    check_attachments([], limits)
    check_attachments(None, limits)
    check_attachments((Attachment("a", b"12"), Attachment("b/c", b"34")), limits)
    cases: list[tuple[Any, type[Exception], str]] = [
        ("a", InvalidRunRequest, "list"),
        ([b"a"], InvalidRunRequest, r"workflow_attachment\[0\]: no Attachment"),
        ([SmallAttachment("a", b"12345")], InvalidRunRequest, "no Attachment"),
        (
            [Attachment("a", b""), Attachment("../b", b"")],
            InvalidRunRequest,
            r"workflow_attachment\[1\]: filename",
        ),
        (
            [Attachment("a", b""), Attachment("a", b"")],
            InvalidRunRequest,
            r"\[1\]: filename used twice",
        ),
        (
            [Attachment("a/b", b""), Attachment("a", b"")],
            InvalidRunRequest,
            r"\[1\]: filename is a directory of another",
        ),
        (
            [Attachment("a", b""), Attachment("a/b", b"")],
            InvalidRunRequest,
            r"\[1\]: filename is in a file of another",
        ),
        (
            [Attachment("a", b"", "text/plain\r\nX-A: 1")],
            InvalidRunRequest,
            r"\[0\]: invalid media type",
        ),
        ([Attachment("a", b"", "")], InvalidRunRequest, "media type"),
        (
            [Attachment("main.cwl", b""), Attachment("\uff4dain.cwl", b"")],
            InvalidRunRequest,
            "used twice",
        ),
        (
            [Attachment("main.cwl", b""), Attachment("MAIN.cwl", b"")],
            InvalidRunRequest,
            "used twice",
        ),
        (
            [Attachment("A/b", b""), Attachment("a", b"")],
            InvalidRunRequest,
            "directory of another",
        ),
        (
            ShortList([Attachment(str(i), b"") for i in range(3)]),
            AttachmentsTooLarge,
            "more than 2",
        ),
        (
            [Attachment(str(i), b"") for i in range(3)],
            AttachmentsTooLarge,
            "more than 2",
        ),
        ([Attachment("a", b"12345")], AttachmentsTooLarge, "more than 4 bytes"),
        (
            [Attachment("a", b"123"), Attachment("b", b"45")],
            AttachmentsTooLarge,
            "more than 4 bytes",
        ),
    ]
    for attachments, error, message in cases:
        with pytest.raises(error, match=message):
            check_attachments(attachments, limits)
    for media_type in ("text/plain", "text/plain; charset=utf-8", "a/b+json"):
        check_attachments([Attachment("a", b"", media_type)], limits)
    assert AttachmentsTooLarge.status == 413
    assert InvalidRunRequest.status == 400
    assert issubclass(AttachmentsTooLarge, InvalidRunRequest)
    assert issubclass(InvalidRunRequest, ValueError)


def test_check_attachments_default_limits() -> None:
    """Without limits, those of `AttachmentsConfig()` apply."""
    conf = AttachmentsConfig()
    assert (conf.max_count, conf.max_size) == (256, 32 * 1024 * 1024)
    check_attachments([Attachment("a", b"x")])
    with pytest.raises(AttachmentsTooLarge):
        check_attachments([Attachment(str(i), b"") for i in range(257)])
    assert PluginsConfig().attachments == conf


# Run requests


def test_check_run_request() -> None:
    """Run requests are maps with string and JSON object fields."""
    limits = AttachmentsConfig(max_count=1)
    check_run_request(make_run_request(), limits)
    check_run_request({"workflow_url": "https://x/y.cwl"}, limits)
    check_run_request({"workflow_url": None, "workflow_attachment": None}, limits)
    cases: list[tuple[Any, str]] = [
        ([], "payload: no map"),
        ({"workflow_url": 1}, "workflow_url: no string"),
        ({"tags": "{}"}, "tags: no JSON object"),
        ({"workflow_params": []}, "workflow_params: no JSON object"),
        ({"workflow_params": {"a": float("nan")}}, "workflow_params: no valid"),
        ({"workflow_params": {"a": {1, 2}}}, "workflow_params: no valid JSON"),
        ({"tags": Headers()}, "tags: no JSON object"),
        ({"workflow_engine_parameters": 1}, "workflow_engine_parameters"),
        ({1: "a"}, "field name: no string"),
        ({"workflow_url": Name("a")}, "workflow_url: no string"),
        ({"other": 1}, "other: no string"),
    ]
    for payload, message in cases:
        with pytest.raises(InvalidRunRequest, match=message):
            check_run_request(payload, limits)
    with pytest.raises(AttachmentsTooLarge):
        check_run_request(
            {ATTACHMENT_FIELD: [Attachment("a", b""), Attachment("b", b"")]}, limits
        )


def test_parse_run_request() -> None:
    """Form fields are parsed, and attachments added as a list."""
    files = [Attachment("main.cwl", CWL, "text/plain")]
    payload = parse_run_request(
        {
            "workflow_url": "main.cwl",
            "workflow_type": "CWL",
            "workflow_type_version": "v1.2",
            "workflow_params": '{"x": {"class": "File", "path": "a"}}',
            "tags": '{"gateway.pipeline": "p"}',
            "workflow_engine_parameters": "{}",
            "workflow_engine": "cwltool",
        },
        files,
    )
    assert payload == {
        "workflow_url": "main.cwl",
        "workflow_type": "CWL",
        "workflow_type_version": "v1.2",
        "workflow_params": {"x": {"class": "File", "path": "a"}},
        "tags": {"gateway.pipeline": "p"},
        "workflow_engine_parameters": {},
        "workflow_engine": "cwltool",
        ATTACHMENT_FIELD: files,
    }
    assert payload[ATTACHMENT_FIELD] is not files
    assert parse_run_request({}) == {ATTACHMENT_FIELD: []}
    cases: list[tuple[dict[str, Any], str]] = [
        ({"tags": "{"}, "tags: no valid JSON"),
        ({"tags": "[]"}, "tags: no JSON object"),
        ({"workflow_params": "[" * 100_000}, "workflow_params: no valid JSON"),
        ({"workflow_params": '{"a": NaN}'}, "workflow_params: no valid JSON"),
        ({"workflow_params": '{"a": -Infinity}'}, "workflow_params: no valid"),
        ({"workflow_url": 1}, "workflow_url: no string"),
        ({ATTACHMENT_FIELD: "a"}, "file parts"),
    ]
    for fields, message in cases:
        with pytest.raises(InvalidRunRequest, match=message):
            parse_run_request(fields)
    # attachments are checked by check_run_request, not parsed
    unchecked = parse_run_request({}, [b"a"])  # type: ignore[list-item]
    with pytest.raises(InvalidRunRequest, match="no Attachment"):
        check_run_request(unchecked)


def test_run_request_form() -> None:
    """Run requests turn back into form fields and files."""
    payload = make_run_request(tags={"a": "ü"}, workflow_engine="cwltool")
    payload["workflow_engine_parameters"] = None
    fields, files = run_request_form(payload)
    assert fields == {
        "workflow_url": "main.cwl",
        "workflow_type": "CWL",
        "workflow_type_version": "v1.2",
        "workflow_params": "{}",
        "tags": '{"a":"ü"}',
        "workflow_engine": "cwltool",
    }
    assert files == payload[ATTACHMENT_FIELD]
    assert files is not payload[ATTACHMENT_FIELD]
    round_trip = parse_run_request(fields, files)
    del payload["workflow_engine_parameters"]
    assert round_trip == payload
    with pytest.raises(InvalidRunRequest, match="filename"):
        run_request_form({ATTACHMENT_FIELD: [Attachment("/a", b"")]})
    with pytest.raises(InvalidRunRequest, match="workflow_params"):
        run_request_form({"workflow_params": {"x": float("nan")}})
    # no limits: the gateway checked them after each plugin
    many = [Attachment(str(i), b"") for i in range(300)]
    assert len(run_request_form({ATTACHMENT_FIELD: many})[1]) == 300


# Engine and test kit


def test_make_inbound_wes() -> None:
    """The kit builds WES run requests with attachments."""
    inbound = make_inbound(api="wes")
    assert inbound.path == "/ga4gh/wes/v1/runs"
    assert inbound.route == "/runs"
    assert inbound.media_type == "multipart/form-data"
    payload = inbound.payload
    assert payload["workflow_url"] == "main.cwl"
    assert [a.filename for a in payload[ATTACHMENT_FIELD]] == ["main.cwl"]
    check_run_request(payload)
    assert make_inbound().path == "/ga4gh/tes/v1/tasks"
    assert make_inbound(api="tes").media_type == "application/json"
    request = make_run_request(
        "trs://trs.example.org/tool/1",
        attachments=[],
        workflow_params={"a": 1},
        tags={"t": "1"},
    )
    assert request == {
        "workflow_url": "trs://trs.example.org/tool/1",
        "workflow_type": "CWL",
        "workflow_type_version": "v1.2",
        "workflow_params": {"a": 1},
        "tags": {"t": "1"},
        ATTACHMENT_FIELD: [],
    }
    custom = make_inbound(api="wes", path="/runs", route="/r", media_type="x/y")
    assert (custom.path, custom.route, custom.media_type) == ("/runs", "/r", "x/y")


def test_engine_accepts_attachments() -> None:
    """Plugins add attachments; the engine checks and records them."""
    harness = PluginHarness(AttachPlugin, api="wes")
    result = harness.run_request()
    files = result.payload[ATTACHMENT_FIELD]
    assert [a.filename for a in files] == ["main.cwl", "wf/main.cwl"]
    assert result.payload["workflow_url"] == "wf/main.cwl"
    assert harness.executions[0].outcome == "ok"
    changes = harness.executions[0].changes
    assert changes is not None
    assert "error" not in changes
    assert changes["paths"] == [
        f"/payload/{ATTACHMENT_FIELD}",
        "/payload/workflow_url",
    ]
    # the original request is unchanged
    assert harness.run is not None
    assert len(harness.run.original[ATTACHMENT_FIELD]) == 1
    outbound = harness.run_forward()
    assert outbound is not None
    assert [a.filename for a in outbound.payload[ATTACHMENT_FIELD]] == [
        "main.cwl",
        "wf/main.cwl",
        "extra/wf/main.cwl",
    ]
    harness.close()
    check_plugin_contract(AttachPlugin, api="wes")


def test_engine_refuses_unsafe_attachments() -> None:
    """Unsafe filenames and exceeded limits are invalid output."""
    harness = PluginHarness(UnsafePlugin, api="wes", on_failure="skip")
    result = harness.run_request()
    assert [a.filename for a in result.payload[ATTACHMENT_FIELD]] == ["main.cwl"]
    assert harness.executions[0].outcome == "invalid_output"
    # the unsafe file added per attempt is refused too: the step is skipped
    outbound = harness.run_forward()
    assert outbound is not None
    assert [a.filename for a in outbound.payload[ATTACHMENT_FIELD]] == ["main.cwl"]
    assert [(e.phase, e.outcome) for e in harness.executions] == [
        ("request", "invalid_output"),
        ("request", "skipped"),
        ("forward", "invalid_output"),
        ("forward", "skipped"),
    ]
    harness.close()
    harness = PluginHarness(AttachPlugin, api="wes", on_failure="skip")
    harness.host.conf = harness.host.conf.model_copy(
        update={"attachments": AttachmentsConfig(max_count=1)}
    )
    harness.run_request()
    assert harness.executions[0].outcome == "invalid_output"
    harness.close()


def test_engine_reads_wes_tags_from_payload() -> None:
    """The client's choices of WES runs are read from the payload's tags."""
    harness = PluginHarness(NoopPlugin, api="wes")
    payload = make_run_request(tags={"gateway.pipeline": "preset"})
    with pytest.raises(InvalidSelection):
        harness.run_request(make_inbound(payload, api="wes"))
    harness.close()


def test_engine_checks_wes_run_requests_only() -> None:
    """Other payloads of WES, and payloads of other APIs, are not checked."""
    harness = PluginHarness(NoopPlugin, api="wes")
    run = harness.start(make_inbound(None, api="wes"), tags=None)
    assert run.request().payload is None
    assert harness.executions[0].outcome == "ok"
    harness.close()
    harness = PluginHarness(UnsafePlugin, api="tes")
    payload: dict[str, Any] = {"executors": [], ATTACHMENT_FIELD: []}
    result = harness.run_request(make_inbound(payload))
    assert result.payload[ATTACHMENT_FIELD][0].filename == "../main.cwl"
    harness.close()
