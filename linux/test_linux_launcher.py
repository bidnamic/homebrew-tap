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
    # Linux omits region= (1.x leaks it into the NFS opts; region comes from
    # efs-utils.conf instead).
    linux_opts = b.efs_mount_options("p", "fsap-1", "10.0.0.5", include_region=False)
    assert "region=" not in linux_opts, linux_opts
    assert linux_opts == "tls,iam,awsprofile=p,accesspoint=fsap-1,mounttargetip=10.0.0.5,soft,timeo=300"


def test_linux_mount_command():
    opts = b.efs_mount_options("p", "fsap-1", "10.0.0.5")
    cmd = b.linux_mount_command(
        "/usr/bin/mount.efs", "fs-abc", opts, Path("/home/u/bidnamic-os"), Path("/home/u")
    )
    # mount.efs invoked directly (not via `mount -t efs`), HOME pinned so the
    # helper's botocore reads the user's ~/.aws, no macOS PATH pin. Linux
    # reads fsname/mountpoint as args[1]/args[2], so positionals come before -o.
    assert cmd[:3] == ["sudo", "env", "HOME=/home/u"], cmd
    assert cmd[3] == "/usr/bin/mount.efs"
    assert cmd[4] == "fs-abc:/"
    assert cmd[5] == "/home/u/bidnamic-os"
    assert cmd[6:8] == ["-o", opts]
    assert "mount" not in cmd, cmd  # no `mount -t efs` layer
    assert "PATH=" not in " ".join(cmd)


if __name__ == "__main__":
    test_efs_mount_options()
    test_linux_mount_command()
    print("ok")
