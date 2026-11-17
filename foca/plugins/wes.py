"""Run requests of the GA4GH WES API, as plugins see them.

WES clients submit runs (`POST /runs`) as `multipart/form-data`. Plugins
get them as plain data, the same in every gateway: the payload of the
:py:class:`foca.plugins.model.InboundRequest` is a `dict` of the form
fields, with

- `workflow_url`, `workflow_type`, `workflow_type_version` (and, from WES
  1.1 on, `workflow_engine`, `workflow_engine_version`) as strings;
- `workflow_params`, `tags` and `workflow_engine_parameters` parsed from
  JSON, as `dict`s;
- `workflow_attachment`: the files, as a `list` of
  :py:class:`foca.plugins.model.Attachment`, which plugins may read, add,
  replace or remove (always present, empty if there are none).

Fields that the client did not send are absent. Apps build the payload
from the multipart request (`parse_run_request()`), check it
(`check_run_request()`) before the plugins run, and turn it back into a
multipart request when forwarding it (`run_request_form()`). After each
`request` and `forward` hook of a WES gateway that got a run, the
engine checks it (`check_run_request()`, with the limits of
`plugins.attachments`): output with unsafe filenames, or more or larger
files than allowed, counts as invalid.

Pure functions on plain data; no web framework.

Examples:
    >>> payload = parse_run_request(
    ...     {"workflow_url": "main.cwl", "workflow_type": "CWL", "tags": "{}"},
    ...     [Attachment("main.cwl", b"class: Workflow", "text/plain")],
    ... )
    >>> check_run_request(payload)
    >>> payload["workflow_attachment"].append(Attachment("../x", b""))
    >>> try:
    ...     check_run_request(payload)
    ... except InvalidRunRequest as exc:
    ...     print(exc)
    workflow_attachment[1]: filename has a '.' or '..' segment
"""

import json
import math
import re
import unicodedata
from collections.abc import Iterable, Mapping
from typing import Any, ClassVar

from foca.plugins.config import AttachmentsConfig
from foca.plugins.model import Attachment

ATTACHMENT_FIELD = "workflow_attachment"
"""Field of the files of a run request."""

STRING_FIELDS = (
    "workflow_url",
    "workflow_type",
    "workflow_type_version",
    "workflow_engine",
    "workflow_engine_version",
)
"""Fields of run requests with string values (other unknown fields too)."""

JSON_FIELDS = ("workflow_params", "tags", "workflow_engine_parameters")
"""Fields of run requests with JSON objects, serialized in the form."""

MAX_FILENAME_SIZE = 1024
"""Maximum size of attachment filenames, in UTF-8 bytes."""

MAX_SEGMENT_SIZE = 255
"""Maximum size of each segment of attachment filenames, in UTF-8 bytes."""

MAX_JSON_DEPTH = 64
"""Maximum depth of the values of JSON fields of run requests."""

# RFC 9110, sections 8.3.1 and 5.6.6: type "/" subtype, then parameters
# (`;` with an optional name "=" value, the value a token or quoted string)
_TCHARS = r"[!#$%&'*+.^_`|~0-9A-Za-z-]+"
_QUOTED = r'"(?:[\t \x21\x23-\x5b\x5d-\x7e]|\\[\t\x20-\x7e])*"'
_PARAMETER = rf"[ \t]*;[ \t]*(?:{_TCHARS}=(?:{_TCHARS}|{_QUOTED}))?"
_MEDIA_TYPE = re.compile(rf"{_TCHARS}/{_TCHARS}(?:{_PARAMETER})*")
_DRIVE = re.compile(r"[A-Za-z]:")
# control, format (e.g., bidirectional overrides), surrogate characters, and
# line and paragraph separators
_CONTROL_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})


class InvalidRunRequest(ValueError):
    """A run request is invalid (e.g., an unsafe filename).

    Messages name fields and positions, never values, so that apps may pass
    them on to clients.

    Attributes:
        status: HTTP status for clients.
    """

    status: ClassVar[int] = 400


class AttachmentsTooLarge(InvalidRunRequest):
    """A run request has more or larger files than allowed.

    Attributes:
        status: HTTP status for clients.
    """

    status: ClassVar[int] = 413


def _check_path(path: str) -> None:
    """Check that a path is relative and stays in its directory.

    Args:
        path: Path.

    Raises:
        InvalidRunRequest: The path is absolute, has a drive, a backslash,
            or an empty, `.` or `..` segment, or a segment ending in a dot or
            space (which Windows drops, e.g., turning `.. ` into `..`).
    """
    if "\\" in path:
        raise InvalidRunRequest("filename has a backslash")
    if path.startswith("/"):
        raise InvalidRunRequest("filename is absolute")
    if _DRIVE.match(path):
        raise InvalidRunRequest("filename starts with a drive")
    for segment in path.split("/"):
        if not segment:
            raise InvalidRunRequest("filename has an empty segment")
        if segment in (".", ".."):
            raise InvalidRunRequest("filename has a '.' or '..' segment")
        if segment.endswith((".", " ")):
            raise InvalidRunRequest("filename has a segment ending in a dot or space")


def check_filename(filename: object) -> str:
    """Check that an attachment filename is a safe relative path.

    Safe filenames are relative paths below the run's directory, with `/`
    as separator: not empty, not absolute (no leading `/`, no drive such as
    `C:`), without backslashes, without empty, `.` or `..` segments or
    segments ending in a dot or space (which Windows drops), without
    control or format characters (e.g., NUL, line breaks and line
    separators, bidirectional overrides), valid UTF-8 of at most
    :py:data:`MAX_FILENAME_SIZE` bytes, with segments of at most
    :py:data:`MAX_SEGMENT_SIZE` bytes. Their NFKC normalization (as some
    servers apply it) must be safe as well, also in size, so that, e.g.,
    fullwidth dots cannot turn into `..`.

    Args:
        filename: Filename.

    Returns:
        The filename.

    Raises:
        InvalidRunRequest: The filename is unsafe; the message does not
            contain it.
    """
    # exact type: subclasses could override the methods used below
    if type(filename) is not str:
        raise InvalidRunRequest("filename is no string")
    if not filename:
        raise InvalidRunRequest("filename is empty")
    try:
        size = len(filename.encode("utf-8"))
    except UnicodeEncodeError:
        raise InvalidRunRequest("filename is no valid UTF-8") from None
    if size > MAX_FILENAME_SIZE:
        raise InvalidRunRequest(
            f"filename is longer than {MAX_FILENAME_SIZE} bytes (UTF-8)"
        )
    if any(unicodedata.category(char) in _CONTROL_CATEGORIES for char in filename):
        raise InvalidRunRequest("filename has control or format characters")
    normalized = unicodedata.normalize("NFKC", filename)
    _check_path(filename)
    _check_path(normalized)
    if len(normalized.encode("utf-8")) > MAX_FILENAME_SIZE:
        raise InvalidRunRequest(
            f"filename is longer than {MAX_FILENAME_SIZE} bytes (UTF-8) after NFKC"
        )
    segments = filename.split("/") + normalized.split("/")
    if any(len(part.encode("utf-8")) > MAX_SEGMENT_SIZE for part in segments):
        raise InvalidRunRequest(
            f"filename has a segment longer than {MAX_SEGMENT_SIZE} bytes (UTF-8)"
        )
    return filename


def _check_files(attachments: object, limits: AttachmentsConfig | None) -> list[Any]:
    """Check the attachments of a run request.

    Args:
        attachments: Attachments.
        limits: Limits of number and total size; none if `None`.

    Returns:
        The attachments, as a list.

    Raises:
        AttachmentsTooLarge: The attachments exceed the limits.
        InvalidRunRequest: The attachments are invalid.
    """
    # a plain list, so that subclasses cannot misreport their items
    if type(attachments) is not list:
        raise InvalidRunRequest(f"{ATTACHMENT_FIELD}: no list")
    items = list(attachments)
    if limits is not None and len(items) > limits.max_count:
        raise AttachmentsTooLarge(
            f"{ATTACHMENT_FIELD}: more than {limits.max_count} files"
        )
    files: set[str] = set()
    directories: set[str] = set()
    total = 0
    for index, attachment in enumerate(items):
        field = f"{ATTACHMENT_FIELD}[{index}]"
        # not subclasses, which could override the checked fields
        if type(attachment) is not Attachment:
            raise InvalidRunRequest(f"{field}: no Attachment")
        try:
            name = check_filename(attachment.filename)
        except InvalidRunRequest as exc:
            raise InvalidRunRequest(f"{field}: {exc}") from None
        if not _MEDIA_TYPE.fullmatch(attachment.media_type):
            raise InvalidRunRequest(f"{field}: invalid media type")
        # as servers may normalize filenames, or ignore their case
        key = unicodedata.normalize("NFKC", name).casefold()
        if key in files:
            raise InvalidRunRequest(f"{field}: filename used twice")
        if key in directories:
            raise InvalidRunRequest(f"{field}: filename is a directory of another")
        parents = [key[:i] for i, char in enumerate(key) if char == "/"]
        if any(parent in files for parent in parents):
            raise InvalidRunRequest(f"{field}: filename is in a file of another")
        files.add(key)
        directories.update(parents)
        total += len(attachment.content)
        if limits is not None and total > limits.max_size:
            raise AttachmentsTooLarge(
                f"{ATTACHMENT_FIELD}: more than {limits.max_size} bytes"
            )
    return items


def check_attachments(
    attachments: object, limits: AttachmentsConfig | None = None
) -> None:
    """Check the attachments of a run request.

    Args:
        attachments: Attachments (a `list` of
            :py:class:`foca.plugins.model.Attachment`).
        limits: Limits of number and total size (`plugins.attachments`, e.g.,
            `host.conf.attachments`); defaults to those of
            :py:class:`foca.plugins.config.AttachmentsConfig`.

    Raises:
        AttachmentsTooLarge: There are more files, or more bytes, than the
            limits allow (`413`).
        InvalidRunRequest: The attachments are no `list`, or an item is no
            attachment, has an unsafe filename (cf. :py:func:`check_filename`)
            or an invalid media type, or names a file twice, or as a
            directory of another file, or inside another file, also when
            compared after NFKC normalization and ignoring case (`400`).
    """
    _check_files(attachments, limits or AttachmentsConfig())


def _no_constant(constant: str) -> Any:
    """Refuse the non-standard JSON constants `NaN` and `Infinity`.

    Args:
        constant: Constant.

    Raises:
        ValueError: Always.
    """
    raise ValueError(f"no JSON: {constant}")


_JSON_SCALARS = (str, int, float, bool, type(None))


def _check_text(name: str, value: str) -> None:
    """Check that a string can be encoded in UTF-8.

    Args:
        name: Name of the field.
        value: String.

    Raises:
        InvalidRunRequest: The string has unpaired surrogates.
    """
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise InvalidRunRequest(f"{name}: no valid UTF-8") from None


def _check_json(name: str, value: Any, depth: int = 0) -> None:
    """Check that a value is JSON, unchanged by serialization.

    Args:
        name: Name of the field.
        value: Value.
        depth: Depth of the value in the field.

    Raises:
        InvalidRunRequest: The value, or one in it, is no `dict` with string
            keys, `list`, string, number, boolean or `None` (e.g., a tuple,
            a set or bytes), a non-finite number, or a string with unpaired
            surrogates, or the value is nested too deeply.
    """
    if depth > MAX_JSON_DEPTH:
        raise InvalidRunRequest(f"{name}: nested too deeply")
    kind = type(value)
    if kind is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise InvalidRunRequest(f"{name}: no valid JSON (key no string)")
            _check_text(name, key)
            _check_json(name, item, depth + 1)
    elif kind is list:
        for item in value:
            _check_json(name, item, depth + 1)
    elif kind not in _JSON_SCALARS or (kind is float and not math.isfinite(value)):
        raise InvalidRunRequest(f"{name}: no valid JSON")
    elif kind is str:
        _check_text(name, value)


def _to_json(value: Any) -> str:
    """Serialize a JSON field of a run request, checked by `_check_json()`.

    Args:
        value: Value of the field.

    Returns:
        Compact JSON.
    """
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _check_fields(payload: object) -> Mapping[Any, Any]:
    """Check the form fields of a run request, except the attachments.

    Args:
        payload: Run request.

    Returns:
        The run request.

    Raises:
        InvalidRunRequest: The run request is no mapping, or a field has a
            value of the wrong type.
    """
    if not isinstance(payload, Mapping):
        raise InvalidRunRequest("payload: no map of form fields")
    for name, value in payload.items():
        if type(name) is not str:
            raise InvalidRunRequest("field name: no string")
        if name == ATTACHMENT_FIELD or value is None:
            continue
        if name in JSON_FIELDS:
            if type(value) is not dict:
                raise InvalidRunRequest(f"{name}: no JSON object")
            _check_json(name, value)
        elif type(value) is not str:
            raise InvalidRunRequest(f"{name}: no string")
        else:
            _check_text(name, value)
    return payload


def check_run_request(payload: object, limits: AttachmentsConfig | None = None) -> None:
    """Check a run request, as the engine does after each plugin.

    Apps call it before the plugins run, on the client's request (cf.
    :py:func:`parse_run_request`), and answer with the error's `status`.

    Args:
        payload: Run request.
        limits: Limits of the attachments (`plugins.attachments`, e.g.,
            `host.conf.attachments`); defaults to those of
            :py:class:`foca.plugins.config.AttachmentsConfig`.

    Raises:
        AttachmentsTooLarge: The attachments exceed the limits (`413`).
        InvalidRunRequest: The run request is no mapping, the JSON fields
            are no `dict`s of JSON values (with string keys, finite numbers,
            no tuples or sets, at most :py:data:`MAX_JSON_DEPTH` levels),
            other fields (`None` aside) no strings, a string is no valid
            UTF-8, `workflow_attachment` is missing, or the attachments are
            invalid (cf. :py:func:`check_attachments`) (`400`).
    """
    fields = _check_fields(payload)
    if ATTACHMENT_FIELD not in fields:
        raise InvalidRunRequest(f"{ATTACHMENT_FIELD}: missing")
    check_attachments(fields[ATTACHMENT_FIELD], limits)


def parse_run_request(
    fields: Mapping[str, str], attachments: Iterable[Attachment] = ()
) -> dict[str, Any]:
    """Build the payload of a run request from its form.

    Args:
        fields: Text fields of the form, by name (one value each).
        attachments: File parts of `workflow_attachment`, e.g., built
            from the uploaded files of the web framework.

    Returns:
        Run request: the JSON fields (:py:data:`JSON_FIELDS`) parsed, other
        fields as strings, and the attachments as a new list under
        `workflow_attachment`. Neither filenames nor limits are checked:
        check the result with :py:func:`check_run_request`.

    Raises:
        InvalidRunRequest: A JSON field is no valid JSON (also with
            `NaN` or `Infinity`) or no object,
            another field no string, or `workflow_attachment` is a text
            field.
    """
    payload: dict[str, Any] = {}
    for name, value in fields.items():
        if name == ATTACHMENT_FIELD:
            raise InvalidRunRequest(f"{ATTACHMENT_FIELD}: send files as file parts")
        if not isinstance(value, str):
            raise InvalidRunRequest(f"{name}: no string")
        parsed: Any = value
        if name in JSON_FIELDS:
            try:
                parsed = json.loads(value, parse_constant=_no_constant)
            except (ValueError, RecursionError):
                raise InvalidRunRequest(f"{name}: no valid JSON") from None
            if not isinstance(parsed, dict):
                raise InvalidRunRequest(f"{name}: no JSON object")
        payload[name] = parsed
    payload[ATTACHMENT_FIELD] = list(attachments)
    return payload


def run_request_form(
    payload: Mapping[str, Any],
) -> tuple[dict[str, str], list[Attachment]]:
    """Turn a run request back into a form, e.g., to forward it.

    Args:
        payload: Run request, e.g., the payload of an outbound request.

    Returns:
        Text fields (JSON fields as compact JSON; fields that are `None` left
        out) and the attachments, as a new list, for the file parts of
        `workflow_attachment`. The fields and filenames are checked again,
        not the limits, which the engine checked after each plugin.

    Raises:
        InvalidRunRequest: The run request is invalid (cf.
            :py:func:`check_run_request`).
    """
    _check_fields(payload)
    if ATTACHMENT_FIELD not in payload:
        raise InvalidRunRequest(f"{ATTACHMENT_FIELD}: missing")
    files = _check_files(payload[ATTACHMENT_FIELD], None)
    fields: dict[str, str] = {}
    for name, value in payload.items():
        if name == ATTACHMENT_FIELD or value is None:
            continue
        fields[name] = _to_json(value) if name in JSON_FIELDS else value
    return fields, files
