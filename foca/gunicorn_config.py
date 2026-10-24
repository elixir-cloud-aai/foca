"""Default Gunicorn settings for FOCA apps in containers.

A Gunicorn config file as a Python module, for apps served with Gunicorn
(the production WSGI server), e.g., in the image of the app::

    gunicorn --config python:foca.gunicorn_config "wsgi:create_app()"

The defaults suit a small container (e.g., the 128 MiB memory limit of the
Helm chart) behind a reverse proxy. Override them via ``GUNICORN_CMD_ARGS``
(e.g., ``--workers 2 --threads 8``), which takes precedence over this module
(cf. https://gunicorn.org/reference/settings/), or with a module of the app
that imports this one (``from foca.gunicorn_config import *``) and sets other
values.

The app is not preloaded (no ``preload_app``): each worker creates it after
forking, as MongoDB clients must not be shared across forks, and runs the
startup checks; if they fail, the worker fails to boot and Gunicorn exits.
``forwarded_allow_ips`` is not set either (Gunicorn's default), as which
proxies to trust depends on the deployment.

The module does not import Gunicorn, which FOCA does not depend on; apps
that use it install Gunicorn themselves.
"""

import os
from pathlib import Path

__all__ = [
    "accesslog",
    "bind",
    "control_socket_disable",
    "errorlog",
    "graceful_timeout",
    "keepalive",
    "limit_request_field_size",
    "limit_request_fields",
    "limit_request_line",
    "threads",
    "timeout",
    "worker_class",
    "worker_tmp_dir",
    "workers",
]

# Address of the app; the Helm chart's Service and probes expect port 8080
# (the app config's `server` settings apply to the development server only)
bind = ["0.0.0.0:8080"]

# One process with a few threads, as each worker loads the whole app
worker_class = "gthread"
workers = 1
threads = 4

# Workers whose main loop does not report to Gunicorn for `timeout` seconds
# (e.g., because it is blocked) are killed and restarted; with gthread
# workers, requests run in threads, so a slow or stuck request does not
# count. On shutdown (e.g., SIGTERM from Kubernetes), workers get
# `graceful_timeout` seconds to finish requests, i.e., less than the
# termination grace period of the pod (30 seconds by default)
timeout = 30
graceful_timeout = 20
# Idle keep-alive connections are closed after this many seconds; longer than
# the keep-alive timeout of proxies in front of the app (60 seconds for
# ingress-nginx, 90 seconds for Traefik), so that a proxy does not reuse a
# connection just as it is closed (failing the request with `502`). Idle
# connections wait in the worker's poller, not in its threads (up to
# `worker_connections`, 1000)
keepalive = 100

# Size limits of the request line and headers (Gunicorn's defaults, set
# explicitly); larger requests are rejected (`400`). The size limit of request
# bodies is set by the app (`server.max_content_length`)
limit_request_line = 4094
limit_request_fields = 100
limit_request_field_size = 8190

# Error log to stderr, as for the app's logs; no access log, as FOCA logs
# each request and response (enable it with `--access-logfile -`)
accesslog = None
errorlog = "-"

# No control socket (`gunicornc`), which would be created in the home
# directory, i.e., on the (possibly read-only) root file system
control_socket_disable = True


# `/dev/shm` is memory-backed, not a directory for temporary files
def _worker_tmp_dir(shm: Path = Path("/dev/shm")) -> str | None:  # noqa: S108
    """Get the directory for the heartbeat files of workers.

    The files are updated on each heartbeat, so a memory-backed directory
    (``/dev/shm``) is preferred; otherwise, the default temporary directory
    (``TMPDIR``, or ``/tmp``, an ``emptyDir`` in the Helm chart) is used.

    Args:
        shm: Memory-backed directory.

    Returns:
        ``shm`` if it is a writable directory, else ``None`` (Gunicorn's
        default).
    """
    if shm.is_dir() and os.access(shm, os.W_OK | os.X_OK):
        return str(shm)
    return None


worker_tmp_dir = _worker_tmp_dir()
