#!/usr/bin/env python3
"""Self-check for the Linux EFS mount command construction.

A wrong mount option string or argv silently produces a broken/no mount,
so pin the two pure builders. Run: python3 linux/test_linux_launcher.py
(from the repo root) or just `python3 test_linux_launcher.py` from here.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "launcher"))
import bidnamic_os as b  # noqa: E402


def test_efs_mount_options():
    opts = b.efs_mount_options("bidnamic-os-live", "fsap-123", "10.0.0.5")
    assert opts == (
        "tls,iam,region=eu-west-1,awsprofile=bidnamic-os-live,"
        "accesspoint=fsap-123,mounttargetip=10.0.0.5,soft,timeo=300"
    ), opts


def test_linux_mount_command():
    opts = b.efs_mount_options("p", "fsap-1", "10.0.0.5")
    cmd = b.linux_mount_command("fs-abc", opts, Path("/home/u/bidnamic-os"), Path("/home/u"))
    assert cmd[:7] == ["sudo", "env", "HOME=/home/u", "mount", "-t", "efs", "-o"], cmd
    assert cmd[7] == opts
    assert cmd[8] == "fs-abc:/"
    assert cmd[9] == "/home/u/bidnamic-os"
    # macOS-only bits must NOT leak into the Linux command.
    joined = " ".join(cmd)
    assert "PATH=" not in joined, joined
    assert "mount.efs" not in joined, joined


if __name__ == "__main__":
    test_efs_mount_options()
    test_linux_mount_command()
    print("ok")
