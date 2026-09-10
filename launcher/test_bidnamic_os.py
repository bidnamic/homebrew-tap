#!/usr/bin/env python3
"""Self-check: run `python3 launcher/test_bidnamic_os.py`."""

import os
import subprocess
import sys
from unittest import mock

import bidnamic_os as b

ARGS = ("bidnamic-os-live", "cluster-1", "arn:aws:ecs:eu-west-1:1:task/cluster-1/abc123", "rob-e")


def test_exec_argv():
    argv = b.exec_argv(*ARGS, "echo hi")
    assert argv[argv.index("--task") + 1] == "abc123", argv
    assert argv[argv.index("--container") + 1] == "bidnamic-os-rob-e", argv
    assert argv[argv.index("--command") + 1] == "echo hi", argv
    assert "--interactive" in argv


def test_as_user_quotes_script():
    # The whole script must reach bash -c as one argument, or `&&` would be
    # interpreted by the launching shell instead of the remote one.
    assert b.as_user("rob-e", "claude auth status && echo X") == (
        "gosu rob-e bash -lc 'claude auth status && echo X'"
    )


def test_claude_authed_reads_sentinel():
    def run(argv, **kw):
        assert b.CLAUDE_AUTH_SENTINEL in argv[-1]
        return subprocess.CompletedProcess(argv, 0, stdout=f"Logged in\n{b.CLAUDE_AUTH_SENTINEL}\n", stderr="")

    with mock.patch("subprocess.run", run):
        assert b.claude_authed(*ARGS) is True

    def run_unauthed(argv, **kw):
        # ECS Exec exits 0 even when the remote command failed — output only.
        return subprocess.CompletedProcess(argv, 0, stdout="Not logged in\n", stderr="")

    with mock.patch("subprocess.run", run_unauthed):
        assert b.claude_authed(*ARGS) is False


def test_claude_authed_gives_the_session_a_tty():
    # DEVNULL stdin kills session-manager-plugin with "Cannot perform start
    # session: EOF" before the command runs, so the check must never see one.
    seen = {}

    def run(argv, **kw):
        seen["tty"] = os.isatty(kw["stdin"])  # fd is closed by the time we assert
        return subprocess.CompletedProcess(argv, 0, stdout=b.CLAUDE_AUTH_SENTINEL, stderr="")

    with mock.patch("subprocess.run", run):
        assert b.claude_authed(*ARGS) is True
    assert seen["tty"]


ENV = {
    "cluster": "cluster-1",
    "subnets": ["subnet-a"],
    "security_groups": ["sg-a"],
}
IDENTITY = ("rob@bidnamic.com", "rob-e")
TAGS = [
    {"key": "Service", "value": "bidnamic-os"},
    {"key": "User", "value": "rob@bidnamic.com"},
]


def access_denied():
    return b.ClientError({"Error": {"Code": "AccessDeniedException"}}, "DescribeServices")


class FakeEcs:
    """Records mutating calls; doubles as the boto3 session.

    `services` is what describe_services returns; `denied` makes it raise
    AccessDenied, which is what an SSO permission set predating the migration
    does.
    """

    def __init__(self, services=None, denied=False, tasks=None):
        self.services = services or []
        self.denied = denied
        self.tasks = tasks or []
        self.updates = []
        self.run_tasks = []
        self.stopped = []

    def client(self, _name):
        return self

    def describe_services(self, **kwargs):
        if self.denied:
            raise access_denied()
        return {"services": self.services}

    def update_service(self, **kwargs):
        self.updates.append(kwargs)

    def run_task(self, **kwargs):
        self.run_tasks.append(kwargs)
        return {"tasks": [{"taskArn": "arn:from-run-task"}]}

    def stop_task(self, **kwargs):
        self.stopped.append(kwargs)

    def get_paginator(self, _name):
        outer = self

        class Paginator:
            def paginate(self, **kwargs):
                return [{"taskArns": [t["taskArn"] for t in outer.tasks]}]

        return Paginator()

    def describe_tasks(self, **kwargs):
        return {"tasks": self.tasks}


ACTIVE_STOPPED = [{"status": "ACTIVE", "desiredCount": 0}]
ACTIVE_RUNNING = [{"status": "ACTIVE", "desiredCount": 1}]


def test_find_service_returns_none_when_not_permitted():
    # A launcher released before the permission set is updated cannot describe
    # services; that has to mean "fall back", not "crash".
    assert b.find_service(FakeEcs(denied=True), "cluster-1", "rob-e") is None


def test_find_service_ignores_an_inactive_service():
    ecs = FakeEcs(services=[{"status": "INACTIVE", "desiredCount": 1}])
    assert b.find_service(ecs, "cluster-1", "rob-e") is None


def test_find_running_task_prefers_the_service_owned_task():
    orphan = {"taskArn": "arn:orphan", "startedBy": "rob", "tags": TAGS}
    owned = {"taskArn": "arn:owned", "startedBy": "ecs-svc/123", "tags": TAGS}

    class Ecs(FakeEcs):
        def get_paginator(self, _):
            outer = self

            class P:
                def paginate(self, **kw):
                    return [{"taskArns": [t["taskArn"] for t in outer.tasks]}]

            return P()

        def describe_tasks(self, **kw):
            return {"tasks": self.tasks}

    found = b.find_running_task(Ecs(tasks=[orphan, owned]), "c", "rob@bidnamic.com")
    assert found["taskArn"] == "arn:owned"


def test_find_running_task_falls_back_to_a_standalone_task():
    # Mid-migration this may be the user's only environment; connecting to it
    # beats starting a second one.
    orphan = {"taskArn": "arn:orphan", "startedBy": "rob", "tags": TAGS}

    class Ecs(FakeEcs):
        def get_paginator(self, _):
            class P:
                def paginate(self, **kw):
                    return [{"taskArns": ["arn:orphan"]}]

            return P()

        def describe_tasks(self, **kw):
            return {"tasks": [orphan]}

    found = b.find_running_task(Ecs(), "c", "rob@bidnamic.com")
    assert found["taskArn"] == "arn:orphan"


def test_ensure_environment_scales_a_stopped_service_up():
    ecs = FakeEcs(services=ACTIVE_STOPPED)
    with mock.patch.object(b, "find_running_task", return_value=None), mock.patch.object(
        b, "wait_for_service_task", return_value=ARGS[2]
    ):
        assert b.ensure_environment_running(ecs, ENV, *IDENTITY) == ARGS[2]
    assert ecs.updates == [
        {"cluster": "cluster-1", "service": "bidnamic-os-rob-e", "desiredCount": 1}
    ]
    assert ecs.run_tasks == [], "must not fall back when a service exists"


def test_ensure_environment_does_not_rescale_a_service_already_at_one():
    ecs = FakeEcs(services=ACTIVE_RUNNING)
    with mock.patch.object(b, "find_running_task", return_value=None), mock.patch.object(
        b, "wait_for_service_task", return_value=ARGS[2]
    ):
        b.ensure_environment_running(ecs, ENV, *IDENTITY)
    assert ecs.updates == []


def test_ensure_environment_reuses_a_running_task():
    ecs = FakeEcs(services=ACTIVE_RUNNING)
    task = {"taskArn": ARGS[2], "lastStatus": "RUNNING"}
    with mock.patch.object(b, "find_running_task", return_value=task):
        assert b.ensure_environment_running(ecs, ENV, *IDENTITY) == ARGS[2]
    assert ecs.updates == [] and ecs.run_tasks == []


def test_ensure_environment_falls_back_to_run_task_with_no_service():
    # The pre-migration stack: this launcher ships before the services exist.
    ecs = FakeEcs(services=[])
    with mock.patch.object(b, "find_running_task", return_value=None), mock.patch.object(
        b, "wait_for_task"
    ):
        assert b.ensure_environment_running(ecs, ENV, *IDENTITY) == "arn:from-run-task"
    assert len(ecs.run_tasks) == 1
    assert ecs.run_tasks[0]["taskDefinition"] == "bidnamic-os-rob-e"
    assert ecs.updates == []


def test_ensure_environment_falls_back_when_describe_is_denied():
    ecs = FakeEcs(denied=True)
    with mock.patch.object(b, "find_running_task", return_value=None), mock.patch.object(
        b, "wait_for_task"
    ):
        assert b.ensure_environment_running(ecs, ENV, *IDENTITY) == "arn:from-run-task"
    assert len(ecs.run_tasks) == 1


def test_stop_scales_the_service_to_zero():
    # Stopping the task alone is pointless — the service would replace it.
    ecs = FakeEcs(services=ACTIVE_RUNNING)
    with mock.patch.object(b, "get_user_identity", return_value=IDENTITY):
        b.cmd_stop(ecs, "profile", ENV)
    assert ecs.updates == [
        {"cluster": "cluster-1", "service": "bidnamic-os-rob-e", "desiredCount": 0}
    ]
    assert ecs.stopped == []


def test_as_user_quotes_the_username():
    # It comes from an email local part, so it is not ours to assume is safe.
    assert b.as_user("odd name", "echo hi") == "gosu 'odd name' bash -lc 'echo hi'"


def test_stop_also_stops_a_standalone_task_alongside_a_service():
    # A standalone task is owned by no service, so scaling to zero leaves it
    # running. Mid-migration a user can have both.
    ecs = FakeEcs(services=ACTIVE_RUNNING)
    task = {"taskArn": ARGS[2], "lastStatus": "RUNNING", "startedBy": "rob"}
    with mock.patch.object(b, "get_user_identity", return_value=IDENTITY), mock.patch.object(
        b, "find_running_task", return_value=task
    ):
        b.cmd_stop(ecs, "profile", ENV)
    assert ecs.updates and ecs.updates[0]["desiredCount"] == 0
    assert ecs.stopped and ecs.stopped[0]["task"] == ARGS[2]


def test_stop_scales_when_describe_is_denied_but_a_service_task_runs():
    # The migration window: services exist and are running, but the permission
    # set has not been updated so DescribeServices is denied. Stop must still
    # scale, not report "no running environment" and do nothing.
    ecs = FakeEcs(denied=True)
    task = {"taskArn": ARGS[2], "lastStatus": "RUNNING", "startedBy": "ecs-svc/1"}
    with mock.patch.object(b, "get_user_identity", return_value=IDENTITY), mock.patch.object(
        b, "find_running_task", return_value=task
    ):
        b.cmd_stop(ecs, "profile", ENV)
    assert ecs.updates and ecs.updates[0]["desiredCount"] == 0
    assert ecs.stopped == [], "a service's own task is left to the service"


def test_stop_leaves_a_service_owned_task_to_the_service():
    ecs = FakeEcs(services=ACTIVE_RUNNING)
    task = {"taskArn": ARGS[2], "lastStatus": "RUNNING", "startedBy": "ecs-svc/1"}
    with mock.patch.object(b, "get_user_identity", return_value=IDENTITY), mock.patch.object(
        b, "find_running_task", return_value=task
    ):
        b.cmd_stop(ecs, "profile", ENV)
    assert ecs.stopped == [], "scaling to zero is enough for a service's own task"


def test_auth_reports_a_failed_exec_session_distinctly():
    # A session that never connected is not the same as an unfinished login.
    authed = mock.Mock(return_value=False)
    with mock.patch.object(b, "get_user_identity", return_value=IDENTITY), mock.patch.object(
        b, "ensure_environment_running", return_value=ARGS[2]
    ), mock.patch.object(b, "claude_authed", authed), mock.patch.object(
        subprocess, "run", return_value=subprocess.CompletedProcess([], 255)
    ):
        assert b.cmd_auth(mock.Mock(), "profile", ENV) == 1
    assert authed.call_count == 1, "must not re-check auth after a failed session"


def test_stop_stops_the_task_when_there_is_no_service():
    ecs = FakeEcs(services=[])
    task = {"taskArn": ARGS[2], "lastStatus": "RUNNING", "startedBy": "rob"}
    with mock.patch.object(b, "get_user_identity", return_value=IDENTITY), mock.patch.object(
        b, "find_running_task", return_value=task
    ):
        b.cmd_stop(ecs, "profile", ENV)
    assert ecs.stopped and ecs.stopped[0]["task"] == ARGS[2]
    assert ecs.updates == []


def test_auth_skips_login_when_already_authenticated():
    with mock.patch.object(b, "get_user_identity", return_value=IDENTITY), mock.patch.object(
        b, "ensure_environment_running", return_value=ARGS[2]
    ), mock.patch.object(b, "claude_authed", return_value=True), mock.patch.object(
        subprocess, "run"
    ) as run:
        assert b.cmd_auth(mock.Mock(), "profile", ENV) == 0
    assert not run.called, "must not re-run the login flow when already logged in"


def test_auth_aborts_when_login_does_not_complete():
    with mock.patch.object(b, "get_user_identity", return_value=IDENTITY), mock.patch.object(
        b, "ensure_environment_running", return_value=ARGS[2]
    ), mock.patch.object(b, "claude_authed", return_value=False), mock.patch.object(
        subprocess, "run", return_value=subprocess.CompletedProcess([], 0)
    ):
        assert b.cmd_auth(mock.Mock(), "profile", ENV) == 1


def test_wait_for_service_task_ignores_a_standalone_task():
    # find_running_task falls back to a standalone task; returning that here
    # would skip the service placement this function exists to wait for.
    standalone = {"taskArn": "arn:orphan", "startedBy": "rob"}
    owned = {"taskArn": ARGS[2], "startedBy": "ecs-svc/1"}
    with mock.patch.object(
        b, "find_running_task", side_effect=[standalone, standalone, owned]
    ), mock.patch.object(b, "wait_for_task"), mock.patch("time.sleep"):
        assert b.wait_for_service_task(FakeEcs(), "cluster-1", IDENTITY[0]) == ARGS[2]


def test_auth_never_mounts_efs():
    # Credentials are written to ~/.claude on the container's own EFS access
    # point, so there is no local share to mount — and mounting would prompt
    # for a sudo password for no reason.
    with mock.patch.object(b, "get_user_identity", return_value=IDENTITY), mock.patch.object(
        b, "ensure_environment_running", return_value=ARGS[2]
    ), mock.patch.object(b, "claude_authed", return_value=True), mock.patch.object(b, "mount_efs") as mount:
        b.cmd_auth(mock.Mock(), "profile", ENV)
    assert not mount.called


if __name__ == "__main__":
    for name, fn in sorted(vars().copy().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok {name}")
    sys.exit(0)
