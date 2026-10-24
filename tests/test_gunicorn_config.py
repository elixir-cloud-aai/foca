"""Tests for FOCA's default Gunicorn settings (`foca.gunicorn_config`)."""

import json
import os
import re
import shlex
import sys
from pathlib import Path

import pytest
from gunicorn.app.wsgiapp import WSGIApplication
from gunicorn.config import Config

from foca import gunicorn_config
from foca.gunicorn_config import _worker_tmp_dir

ROOT = Path(__file__).resolve().parents[1]
PETSTORE_DOCKERFILE = ROOT / "examples" / "petstore" / "Dockerfile"
COMMAND = [
    "gunicorn",
    "--config",
    "python:foca.gunicorn_config",
    "wsgi:create_app()",
]


def dockerfile_instruction(dockerfile: Path, name: str) -> str:
    """Get the arguments of the last instruction of a Dockerfile by name.

    Continued lines are joined.

    Args:
        dockerfile: Path of the Dockerfile.
        name: Name of the instruction (e.g., `CMD`).

    Returns:
        Arguments of the instruction.
    """
    text = dockerfile.read_text().replace("\\\n", " ")
    (*_, args) = re.findall(rf"^{name}\s+(.+)$", text, flags=re.MULTILINE)
    return args


def image_command(dockerfile: Path) -> list[str]:
    """Get the command of an image (`CMD`, in exec form).

    Args:
        dockerfile: Path of the Dockerfile.

    Returns:
        Command and arguments.
    """
    command: list[str] = json.loads(dockerfile_instruction(dockerfile, "CMD"))
    return command


def load_settings(
    cmd_args: str | None = None, command: list[str] | None = None
) -> WSGIApplication:
    """Load Gunicorn's settings as a command does, without loading the app.

    Optionally with `GUNICORN_CMD_ARGS`; restores the arguments, environment,
    module search path and current directory afterwards (Gunicorn changes to
    its `chdir` setting and prepends it to the search path).

    Args:
        cmd_args: Value of `GUNICORN_CMD_ARGS`; unset if `None`.
        command: Command and arguments; `COMMAND` if `None`.

    Returns:
        Gunicorn application with its settings (`cfg`), without loading the
        app.
    """
    argv, cwd, path = sys.argv, os.getcwd(), sys.path[:]
    env = os.environ.pop("GUNICORN_CMD_ARGS", None)
    sys.argv = list(COMMAND if command is None else command)
    if cmd_args is not None:
        os.environ["GUNICORN_CMD_ARGS"] = cmd_args
    try:
        return WSGIApplication("%(prog)s [OPTIONS] [APP_MODULE]")
    finally:
        sys.argv = argv
        sys.path[:] = path
        os.chdir(cwd)
        os.environ.pop("GUNICORN_CMD_ARGS", None)
        if env is not None:
            os.environ["GUNICORN_CMD_ARGS"] = env


def test_settings() -> None:
    """Gunicorn loads the settings, without preloading the app."""
    cfg = load_settings().cfg
    assert cfg.bind == ["0.0.0.0:8080"]
    assert cfg.worker_class_str == "gthread"
    assert (cfg.workers, cfg.threads) == (1, 4)
    assert (cfg.timeout, cfg.graceful_timeout) == (30, 20)
    assert cfg.limit_request_line == 4094
    assert cfg.limit_request_fields == 100
    assert cfg.limit_request_field_size == 8190
    assert (cfg.accesslog, cfg.errorlog) == (None, "-")
    assert cfg.control_socket_disable
    assert not cfg.preload_app
    # Not set: Gunicorn's defaults
    assert cfg.forwarded_allow_ips == Config().forwarded_allow_ips
    assert cfg.worker_tmp_dir == _worker_tmp_dir()


def test_no_gunicorn_import() -> None:
    """The module does not import Gunicorn, which FOCA does not depend on."""
    source = Path(gunicorn_config.__file__).read_text()
    assert not re.search(r"^\s*(?:import|from)\s+gunicorn\b", source, re.M)


def test_keepalive() -> None:
    """Idle keep-alive connections are kept longer than by proxies.

    That is, longer than by proxies in front of the app (60 seconds for
    ingress-nginx, 90 seconds for Traefik), so that proxies don't reuse
    connections that Gunicorn closes; they wait in the worker's poller, not
    in its threads, up to Gunicorn's limit of connections.
    """
    cfg = load_settings().cfg
    assert cfg.keepalive == 100
    assert cfg.keepalive > 90
    assert cfg.worker_connections - cfg.threads >= 100


def test_cmd_args() -> None:
    """`GUNICORN_CMD_ARGS` overrides the settings."""
    args = shlex.join(["--workers", "2", "--threads", "8", "--timeout", "60"])
    cfg = load_settings(args).cfg
    assert (cfg.workers, cfg.threads, cfg.timeout) == (2, 8, 60)
    assert cfg.graceful_timeout == 20


def test_petstore_command() -> None:
    """The petstore image serves the app with the settings."""
    command = image_command(PETSTORE_DOCKERFILE)
    assert command == COMMAND
    app = load_settings(command=command)
    assert app.app_uri == "wsgi:create_app()"
    assert app.cfg.bind == ["0.0.0.0:8080"]


def test_star_import() -> None:
    """A star import (for overrides in app modules) exports the settings."""
    namespace: dict[str, object] = {}
    exec("from foca.gunicorn_config import *", namespace)  # noqa: S102
    del namespace["__builtins__"]
    assert namespace == {
        name: getattr(gunicorn_config, name) for name in gunicorn_config.__all__
    }
    assert set(gunicorn_config.__all__) == {
        name
        for name in vars(gunicorn_config)
        if not name.startswith("_") and name not in {"os", "Path"}
    }


def test_load_settings_restores(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Loading the settings restores the arguments and the environment.

    That is, the arguments, `GUNICORN_CMD_ARGS`, the module search path and
    the current directory.
    """
    path = sys.path[:]
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["pytest"])
    monkeypatch.setenv("GUNICORN_CMD_ARGS", "--workers 3")
    assert load_settings("--chdir /").cfg.workers == 1
    assert load_settings().cfg.workers == 1
    assert (os.getcwd(), sys.argv) == (str(tmp_path), ["pytest"])
    assert sys.path == path
    assert os.environ["GUNICORN_CMD_ARGS"] == "--workers 3"
    monkeypatch.delenv("GUNICORN_CMD_ARGS")
    load_settings("--workers 2")
    assert "GUNICORN_CMD_ARGS" not in os.environ


def test_worker_tmp_dir_shm(tmp_path: Path) -> None:
    """A writable memory-backed directory is used for heartbeat files."""
    assert _worker_tmp_dir(tmp_path) == str(tmp_path)


@pytest.mark.parametrize("mode", [0o500, None])
def test_worker_tmp_dir_default(tmp_path: Path, mode: int | None) -> None:
    """Without a writable memory-backed directory, Gunicorn's default is used.

    That is, `None`, the temporary directory; also if the directory is
    missing.

    Args:
        tmp_path: Temporary directory.
        mode: Permissions of the memory-backed directory; missing if `None`.
    """
    shm = tmp_path / "shm"
    if mode is not None:
        shm.mkdir(mode=mode)
    try:
        if mode is not None and os.access(shm, os.W_OK):
            pytest.skip("directory permissions are not enforced (e.g., root)")
        assert _worker_tmp_dir(shm) is None
    finally:
        if mode is not None:
            shm.chmod(0o700)


def test_worker_tmp_dir_file(tmp_path: Path) -> None:
    """A file instead of a directory falls back to Gunicorn's default."""
    shm = tmp_path / "shm"
    shm.touch()
    assert _worker_tmp_dir(shm) is None
