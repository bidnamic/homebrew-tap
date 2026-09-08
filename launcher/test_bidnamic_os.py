#!/usr/bin/env python3
"""Self-check: run `python3 launcher/test_bidnamic_os.py`."""

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


ENV = {"cluster": "cluster-1"}
IDENTITY = ("rob@bidnamic.com", "rob-e")


class FakeEcs:
    """Records update_service calls; doubles as the boto3 session."""

    def __init__(self):
        self.updates = []

    def client(self, _name):
        return self

    def update_service(self, **kwargs):
        self.updates.append(kwargs)


def test_ensure_service_running_scales_a_stopped_environment_up():
    # Beta services are seeded at desired_count 0, so connecting has to scale.
    ecs = FakeEcs()
    with mock.patch.object(b, "find_running_task", return_value=None), mock.patch.object(
        b, "wait_for_service_task", return_value=ARGS[2]
    ):
        assert b.ensure_service_running(ecs, ENV, *IDENTITY) == ARGS[2]
    assert ecs.updates == [
        {"cluster": "cluster-1", "service": "bidnamic-os-rob-e", "desiredCount": 1}
    ]


def test_ensure_service_running_reuses_a_running_task():
    ecs = FakeEcs()
    task = {"taskArn": ARGS[2], "lastStatus": "RUNNING"}
    with mock.patch.object(b, "find_running_task", return_value=task):
        assert b.ensure_service_running(ecs, ENV, *IDENTITY) == ARGS[2]
    assert ecs.updates == [], "must not scale a service that already has a task"


def test_stop_scales_the_service_to_zero():
    # Stopping the task alone is pointless — the service would replace it.
    ecs = FakeEcs()
    with mock.patch.object(b, "get_user_identity", return_value=IDENTITY):
        b.cmd_stop(ecs, "profile", ENV)
    assert ecs.updates == [
        {"cluster": "cluster-1", "service": "bidnamic-os-rob-e", "desiredCount": 0}
    ]


def test_auth_skips_login_when_already_authenticated():
    with mock.patch.object(b, "get_user_identity", return_value=IDENTITY), mock.patch.object(
        b, "ensure_service_running", return_value=ARGS[2]
    ), mock.patch.object(b, "claude_authed", return_value=True), mock.patch.object(
        b, "exec_with_keepalive"
    ) as keepalive:
        assert b.cmd_auth(mock.Mock(), "profile", ENV) == 0
    assert not keepalive.called, "must not re-run the login flow when already logged in"


def test_auth_aborts_when_login_does_not_complete():
    with mock.patch.object(b, "get_user_identity", return_value=IDENTITY), mock.patch.object(
        b, "ensure_service_running", return_value=ARGS[2]
    ), mock.patch.object(b, "claude_authed", return_value=False), mock.patch.object(
        b, "exec_with_keepalive", return_value=0
    ):
        assert b.cmd_auth(mock.Mock(), "profile", ENV) == 1


def test_auth_never_mounts_efs():
    # Credentials are written to ~/.claude on the container's own EFS access
    # point, so there is no local share to mount — and mounting would prompt
    # for a sudo password for no reason.
    with mock.patch.object(b, "get_user_identity", return_value=IDENTITY), mock.patch.object(
        b, "ensure_service_running", return_value=ARGS[2]
    ), mock.patch.object(b, "claude_authed", return_value=True), mock.patch.object(
        b, "mount_efs"
    ) as mount:
        b.cmd_auth(mock.Mock(), "profile", ENV)
    assert not mount.called


def test_find_running_task_ignores_pre_service_orphans():
    # A task from the old run_task path carries identical tags but runs no
    # remote control, so exec'ing into it would silently do the wrong thing.
    tags = [{"key": "Service", "value": "bidnamic-os"}, {"key": "User", "value": "rob@bidnamic.com"}]
    orphan = {"taskArn": "arn:orphan", "startedBy": "rob", "tags": tags}
    owned = {"taskArn": "arn:owned", "startedBy": "ecs-svc/123", "tags": tags}

    class Ecs:
        def __init__(self, tasks):
            self.tasks = tasks

        def get_paginator(self, _):
            outer = self

            class P:
                def paginate(self, **kw):
                    return [{"taskArns": [t["taskArn"] for t in outer.tasks]}]

            return P()

        def describe_tasks(self, **kw):
            return {"tasks": self.tasks}

    assert b.find_running_task(Ecs([orphan]), "c", "rob@bidnamic.com") is None
    found = b.find_running_task(Ecs([orphan, owned]), "c", "rob@bidnamic.com")
    assert found["taskArn"] == "arn:owned"


if __name__ == "__main__":
    for name, fn in sorted(vars().copy().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok {name}")
    sys.exit(0)
