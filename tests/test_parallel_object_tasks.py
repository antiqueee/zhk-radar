import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from queue import Empty, Queue
from threading import Event
from time import monotonic, sleep

import pytest

from radar.reports.pipeline_v8 import generate
from radar.web.store import Store
from radar.web.tasks import TaskRunner, check_task_cancelled


@pytest.fixture
def store(tmp_path):
    root = Path(__file__).resolve().parents[1]
    shutil.copytree(root / "config", tmp_path / "config")
    return Store(tmp_path)


def wait_for_statuses(runner, expected):
    deadline = monotonic() + 5
    while True:
        tasks = {ident: runner.get(ident) for ident in expected}
        statuses = {ident: task["status"] for ident, task in tasks.items()}
        if statuses == expected:
            return tasks
        if monotonic() >= deadline:
            pytest.fail(f"Expected task states {expected}, got {statuses}")
        sleep(0.01)


def test_object_tasks_run_in_parallel_with_per_object_exclusion(store):
    with store.connect() as db:
        db.executescript("""
            CREATE TABLE object_tasks (
                id TEXT PRIMARY KEY,kind TEXT,record_id TEXT,status TEXT,result TEXT,
                error TEXT,created_at TEXT,updated_at TEXT);
            CREATE UNIQUE INDEX one_object_task ON object_tasks((1))
            WHERE status IN ('queued','running');
        """)

    entered = Queue()
    release = {record: Event() for record in ("demo-1", "demo-2", "demo-3")}

    def handle(record_id, task_id):
        entered.put(task_id)
        assert release[record_id].wait(10)
        return {"record_id": record_id}

    runner = TaskRunner(store, {"author": handle, "research": handle})
    first = runner.submit("author", "demo-1")["id"]
    assert runner.submit("author", "demo-1")["id"] == first
    with pytest.raises(ValueError):
        runner.submit("research", "demo-1")
    second = runner.submit("research", "demo-2")["id"]
    third = runner.submit("author", "demo-3")["id"]

    runner.start()
    try:
        assert {entered.get(timeout=5), entered.get(timeout=5)} == {first, second}
        with pytest.raises(Empty):
            entered.get(timeout=0.1)
        assert runner.get(first)["status"] == runner.get(second)["status"] == "running"
        assert runner.get(third)["status"] == "queued"
        assert runner.submit("author", "demo-1")["id"] == first
        with pytest.raises(ValueError):
            runner.submit("research", "demo-1")

        release["demo-1"].set()
        assert entered.get(timeout=5) == third
        assert runner.get(second)["status"] == "running"

        for gate in release.values():
            gate.set()
        finished = wait_for_statuses(runner, dict.fromkeys((first, second, third), "completed"))
        assert [task["result"] for task in finished.values()] == [
            {"record_id": "demo-1"},
            {"record_id": "demo-2"},
            {"record_id": "demo-3"},
        ]
        assert runner.submit("research", "demo-1")["id"] != first
    finally:
        for gate in release.values():
            gate.set()
        runner.close()


def test_cancelling_one_running_object_task_leaves_other_record_active(store):
    entered = Queue()
    release = {ident: Event() for ident in ("demo-1", "demo-2")}

    def handle(record_id, task_id):
        entered.put(task_id)
        assert release[record_id].wait(10)
        check_task_cancelled()
        return {"record_id": record_id}

    runner = TaskRunner(store, {"author": handle})
    first = runner.submit("author", "demo-1")["id"]
    second = runner.submit("author", "demo-2")["id"]
    runner.start()
    try:
        assert {entered.get(timeout=5), entered.get(timeout=5)} == {first, second}
        runner.cancel(first)
        assert runner.submit("author", "demo-1")["id"] == first
        release["demo-1"].set()
        tasks = wait_for_statuses(runner, {first: "cancelled", second: "running"})
        assert tasks[first]["result"] is None

        release["demo-2"].set()
        tasks = wait_for_statuses(runner, {first: "cancelled", second: "completed"})
        assert tasks[second]["result"] == {"record_id": "demo-2"}
    finally:
        for gate in release.values():
            gate.set()
        runner.close()


def test_v8_reports_on_different_objects_enter_research_concurrently(store):
    entered = Queue()
    release = Event()

    class ResearchReached(Exception):
        pass

    def research(_store, record_id, _run_id, *, client):
        entered.put(record_id)
        assert release.wait(10)
        raise ResearchReached

    def run(record_id, run_id):
        generate(store, record_id, run_id, client=object(), researcher=research)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(run, "demo-1", "run-a")
        try:
            assert entered.get(timeout=5) == "demo-1"
            with pytest.raises(ValueError, match="уже выполняется"):
                run("demo-1", "run-a-duplicate")
            second = pool.submit(run, "demo-2", "run-b")
            assert entered.get(timeout=5) == "demo-2"
        finally:
            release.set()

        for result in (first, second):
            with pytest.raises(ResearchReached):
                result.result(timeout=5)
