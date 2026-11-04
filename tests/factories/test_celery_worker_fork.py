"""Test MongoDB clients across forks, with a MongoDB server.

The real Celery worker runs in its own process, with a broker on the file
system, so that no other service is needed.
"""

import os
import subprocess
import sys
import time
import warnings
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from pymongo import MongoClient

from foca.database.register_mongodb import register_mongodb, reopen_mongodb
from foca.models.config import CollectionConfig, DBConfig, MongoConfig

pytestmark = [
    pytest.mark.mongodb,
    # Forks on purpose, in the test of fork safety
    pytest.mark.filterwarnings("ignore:This process .* is multi-threaded"),
]

MONGO_PORT = 12345
COLLECTION = "runs"
# Seconds to wait for the tasks of the worker
TIMEOUT = 60
# As each task ends its process, it runs in a process forked for it, after
# the main process used its client
TASKS = 3
CONFIG = f"""
db:
  host: localhost
  port: {MONGO_PORT}
  dbs:
    fork_test:
      collections:
        {COLLECTION}:
          indexes:
            - keys:
                process: 1
jobs:
  include:
    - fork_worker
"""
WORKER = f"""
import os
from pathlib import Path

from celery import shared_task
from celery.signals import worker_ready
from foca import Foca, get_collection

foca = Foca(config_file=Path(__file__).parent / "config.yaml")
celery_app = foca.create_celery_app()
folder = os.environ["BROKER_FOLDER"]
celery_app.conf.broker_transport_options = {{
    "data_folder_in": folder,
    "data_folder_out": folder,
    "control_folder": folder,
    "polling_interval": 0.1,
}}


@shared_task(name="fork_worker.store")
def store() -> None:
    '''Store the identifier of the process.'''
    get_collection("fork_test", "{COLLECTION}").insert_one({{"process": os.getpid()}})


@worker_ready.connect
def use_client(**kwargs: object) -> None:
    '''Use the client in the main process, and send the tasks.'''
    foca.conf.db.dbs["fork_test"].collections["{COLLECTION}"].client.find_one()
    for _ in range({TASKS}):
        store.delay()
"""


@pytest.fixture
def database() -> Iterator[str]:
    """Name of a database of its own, dropped after the test."""
    name = f"fork_test_{uuid4().hex}"
    client: MongoClient[dict[str, object]] = MongoClient(
        f"mongodb://localhost:{MONGO_PORT}"
    )
    yield name
    client.drop_database(name)
    client.close()


def _wait_for_runs(
    database: str, worker: subprocess.Popen[bytes], expected: int
) -> list[int]:
    """Wait for the runs that the tasks of a worker store.

    Args:
        database: Name of the database.
        worker: Process of the worker.
        expected: Number of runs to wait for.

    Returns:
        Identifiers of the processes that ran the tasks.
    """
    client: MongoClient[dict[str, object]] = MongoClient(
        f"mongodb://localhost:{MONGO_PORT}"
    )
    try:
        deadline = time.monotonic() + TIMEOUT
        runs: list[int] = []
        while time.monotonic() < deadline and worker.poll() is None:
            runs = [
                int(str(doc["process"]))
                for doc in client[database][COLLECTION].find({})
            ]
            if len(runs) >= expected:
                break
            time.sleep(0.2)
        return runs
    finally:
        client.close()


def test_no_client_opened_before_fork(tmp_path: Path, database: str) -> None:
    """Processes of a worker use MongoDB clients opened after their fork.

    Tasks run in forked processes, including ones forked after the main
    process used its client, without a warning of PyMongo.
    """
    (tmp_path / "config.yaml").write_text(CONFIG)
    (tmp_path / "fork_worker.py").write_text(WORKER)
    broker = tmp_path / "broker"
    broker.mkdir()
    results = tmp_path / "results"
    results.mkdir()
    log = tmp_path / "worker.log"
    env = {
        **os.environ,
        "BROKER_FOLDER": str(broker),
        "CELERY_BROKER_URL": "filesystem://",
        "CELERY_RESULT_BACKEND": f"file://{results}",
        "MONGO_DBNAME": database,
        "PYTHONPATH": str(tmp_path),
        "PYTHONWARNINGS": "default",
    }
    with log.open("wb") as output:
        worker = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "celery",
                "--app",
                "fork_worker:celery_app",
                "worker",
                "--pool=prefork",
                "--concurrency=1",
                "--max-tasks-per-child=1",
                "--without-gossip",
                "--without-mingle",
                "--loglevel=INFO",
            ],
            cwd=tmp_path,
            env=env,
            stdout=output,
            stderr=subprocess.STDOUT,
        )
    try:
        runs = _wait_for_runs(database=database, worker=worker, expected=TASKS)
    finally:
        worker.terminate()
        try:
            worker.wait(timeout=20)
        except subprocess.TimeoutExpired:
            worker.kill()
            worker.wait()
    output_text = log.read_text()
    assert len(runs) == TASKS, output_text
    assert worker.pid not in runs
    assert len(set(runs)) == TASKS, output_text
    assert "opened before fork" not in output_text


def test_reopen_after_fork_keeps_client_of_parent(
    database: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A forked process replaces its client without affecting its parent's."""
    monkeypatch.setenv("MONGO_DBNAME", database)
    conf = MongoConfig(
        host="localhost",
        port=MONGO_PORT,
        dbs={"a": DBConfig(collections={COLLECTION: CollectionConfig()})},
    )
    register_mongodb(conf)
    assert conf.dbs is not None
    assert conf.dbs["a"].collections is not None
    parent = conf.dbs["a"].collections[COLLECTION].client
    assert parent is not None
    parent.insert_one({"process": "parent, before the fork"})
    pid = os.fork()
    if pid == 0:  # pragma: no cover
        status = 1
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error")
                reopen_mongodb(conf)
                child = conf.dbs["a"].collections[COLLECTION].client
                assert child is not None
                child.insert_one({"process": "child"})
            status = 0
        finally:
            os._exit(status)
    assert os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1]) == 0
    parent.insert_one({"process": "parent, after the fork"})
    assert parent.count_documents({}) == 3
    assert conf._client is not None
    conf._client.close()
