#!/usr/local/share/bidnamic-os/venv/bin/python
"""CLI for connecting to our Bidnamic OS in the cloud"""

import argparse
import fcntl
import os
import platform
import pty
import select
import shutil
import signal
import struct
import subprocess
import sys
import termios
import time
import tty
import webbrowser
from itertools import batched
from pathlib import Path

try:
    import boto3
    from botocore.exceptions import (
        ClientError,
        NoCredentialsError,
        SSOTokenLoadError,
        TokenRetrievalError,
        UnauthorizedSSOTokenError,
    )
except ImportError:
    boto3 = None

# Rewritten by the Homebrew formula at install time (see
# Formula/bidnamic-os.rb in homebrew-tap). Source-tree invocations report
# "dev". `bidnamic-os version` prints this.
__version__ = "dev"

# Shared configuration
SERVICE_TAG = "bidnamic-os"
REGION = "eu-west-1"
SSO_START_URL = "https://d-936796524a.awsapps.com/start"
# Matches the `creation_token` on aws_efs_file_system.bidnamic_os in the infra
# repo (modules/bidnamic-os/efs.tf). Used to discover the filesystem without
# hardcoding per-environment IDs. Keep in sync with Terraform.
EFS_CREATION_TOKEN = "bidnamic-os"
LOCAL_MOUNT_PATH = Path.home() / "bidnamic-os"

# SHARE_DIR is rewritten by the Homebrew formula at install time to point at
# HOMEBREW_PREFIX/share/bidnamic-os. Source-tree invocations point at the
# legacy /usr/local path which won't exist — that's by design; the
# `tutorial` command is for installed users.
SHARE_DIR = Path("/usr/local/share/bidnamic-os")
TUTORIAL_PATH = SHARE_DIR / "TUTORIAL.html"

# Filesystem artefacts left behind by the legacy zip-based installer. The
# brew install lives at /opt/homebrew/{bin,share}/, so anything under these
# paths is dead weight and shadows the brew binary if /usr/local/bin is
# earlier on PATH. `bidnamic-os post-install` wipes them.
LEGACY_INSTALL_PATHS = [
    Path("/usr/local/bin/bidnamic-os"),
    Path("/usr/local/share/bidnamic-os"),
    Path("/Applications/Bidnamic OS.app"),
]

AWS_CONFIG_PROFILE_TEMPLATE = """\
[profile {profile}]
sso_start_url = {sso_start_url}
sso_region = {region}
sso_account_id = {account_id}
sso_role_name = {sso_role_name}
region = {region}
"""

# Per-environment configuration
ENVIRONMENTS = {
    "beta": {
        "account_id": "715191048898",
        "cluster": "default-KF7fFm2UPZc",
        "subnets": ["subnet-0a0099012d66f4e80", "subnet-08e495740630eaf28"],
        "security_groups": ["sg-0564e16cccc860e3c"],
        "sso_role_name": "BidnamicOSAccess-Beta",
    },
    "live": {
        "account_id": "666005677731",
        "cluster": "default-USrp2B1uSJE",
        "subnets": ["subnet-0b900e463e20f90d5", "subnet-07a8f2844e05df3a4"],
        "security_groups": ["sg-06fb97bae07636229"],
        "sso_role_name": "BidnamicOSAccess-Live",
    },
}

GREEN = "\033[0;32m"
RED = "\033[0;31m"
NC = "\033[0m"


def info(msg):
    print(f"{GREEN}[bidnamic-os]{NC} {msg}")


def error(msg):
    print(f"{RED}[bidnamic-os]{NC} {msg}", file=sys.stderr)


def sudo(*args, check=True):
    """Run a command via sudo, inheriting the TTY so password prompts work.

    sudo caches credentials for ~5 minutes by default, so the first call
    in a session prompts and subsequent ones don't. Pass check=False for
    commands that may legitimately fail (e.g. launchctl unload on a plist
    that isn't loaded).
    """
    return subprocess.run(["sudo", *args], check=check)


def get_env_config(env_name):
    """Get and validate the configuration for a given environment."""
    if env_name not in ENVIRONMENTS:
        error(f"Unknown environment: {env_name}. Choose from: {', '.join(ENVIRONMENTS)}")
        sys.exit(1)

    env = ENVIRONMENTS[env_name]
    missing = []
    if not SSO_START_URL:
        missing.append("SSO_START_URL")
    if not env["account_id"]:
        missing.append(f"ENVIRONMENTS['{env_name}']['account_id']")
    if not env["cluster"]:
        missing.append(f"ENVIRONMENTS['{env_name}']['cluster']")
    if not env["subnets"]:
        missing.append(f"ENVIRONMENTS['{env_name}']['subnets']")
    if not env["security_groups"]:
        missing.append(f"ENVIRONMENTS['{env_name}']['security_groups']")
    if missing:
        error(f"Required config not set: {', '.join(missing)}. Edit this script to configure.")
        sys.exit(1)

    return env


def configure_profile(env_name, env):
    """Write the AWS SSO profile to ~/.aws/config if not present."""
    profile = f"bidnamic-os-{env_name}"
    config_path = Path(os.environ.get("AWS_CONFIG_FILE", Path.home() / ".aws" / "config"))
    if config_path.exists() and f"[profile {profile}]" in config_path.read_text():
        return profile

    info(f"Configuring AWS SSO profile '{profile}'...")
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with open(config_path, "a") as f:
        f.write(
            "\n"
            + AWS_CONFIG_PROFILE_TEMPLATE.format(
                profile=profile,
                sso_start_url=SSO_START_URL,
                region=REGION,
                account_id=env["account_id"],
                sso_role_name=env["sso_role_name"],
            )
        )
    info("Profile configured.")
    return profile


def get_session(profile):
    """Get a boto3 session using the SSO profile, triggering login if needed."""
    session = boto3.Session(profile_name=profile, region_name=REGION)
    try:
        sts = session.client("sts")
        sts.get_caller_identity()
        return session
    except (
        NoCredentialsError,
        TokenRetrievalError,
        SSOTokenLoadError,
        UnauthorizedSSOTokenError,
        ClientError,
    ):
        info("SSO session expired. Opening browser for login...")
        subprocess.run(["aws", "sso", "login", "--profile", profile], check=True)
        session = boto3.Session(profile_name=profile, region_name=REGION)
        return session


def get_user_identity(session):
    """Extract email and sanitized username from the SSO caller identity ARN.

    Returns (email, username) where username matches Terraform's users_map key.
    """
    sts = session.client("sts")
    arn = sts.get_caller_identity()["Arn"]
    # SSO ARN format: arn:aws:sts::ACCOUNT:assumed-role/AWSReservedSSO_.../user@bidnamic.com
    email = arn.split("/")[-1].lower()
    local_part = email.split("@")[0]
    # Sanitize to match Terraform's users_map key normalization
    username = local_part.replace(".", "-").replace("+", "-")
    return email, username


def get_tag(tags, key):
    """Extract a tag value from a list of ECS tag dicts."""
    for tag in tags or []:
        if tag.get("key") == key:
            return tag.get("value")
    return None


def find_running_task(ecs, cluster, email):
    """Find a running bidnamic-os task for the given user. Returns task dict or None."""
    paginator = ecs.get_paginator("list_tasks")
    task_arns = []
    for page in paginator.paginate(cluster=cluster, desiredStatus="RUNNING"):
        task_arns.extend(page["taskArns"])

    if not task_arns:
        return None

    # Describe in batches of 100 (API limit)
    for batch in batched(task_arns, 100):
        response = ecs.describe_tasks(cluster=cluster, tasks=list(batch), include=["TAGS"])
        for task in response["tasks"]:
            tags = task.get("tags", [])
            service = get_tag(tags, "Service")
            user = get_tag(tags, "User")
            if service == SERVICE_TAG and user and user.lower() == email.lower():
                return task

    return None


def start_task(ecs, env, email, username):
    """Start a new bidnamic-os task for the user. Returns task ARN."""
    task_family = f"{SERVICE_TAG}-{username}"
    info("Starting your environment...")

    response = ecs.run_task(
        cluster=env["cluster"],
        taskDefinition=task_family,
        launchType="FARGATE",
        enableExecuteCommand=True,
        networkConfiguration={
            "awsvpcConfiguration": {
                "subnets": env["subnets"],
                "securityGroups": env["security_groups"],
                "assignPublicIp": "DISABLED",
            }
        },
        tags=[
            {"key": "User", "value": email},
            {"key": "Service", "value": SERVICE_TAG},
            {"key": "EcsExec", "value": "true"},
        ],
    )

    tasks = response.get("tasks", [])
    if not tasks:
        failures = response.get("failures", [])
        reasons = [f.get("reason", "unknown") for f in failures]
        error(f"Failed to start environment: {', '.join(reasons)}")
        sys.exit(1)

    return tasks[0]["taskArn"]


def wait_for_task(ecs, cluster, task_arn, max_wait=120):
    """Wait for a task to reach RUNNING status."""
    info("Waiting for environment to be ready...")
    waited = 0
    while waited < max_wait:
        response = ecs.describe_tasks(cluster=cluster, tasks=[task_arn])
        tasks = response.get("tasks", [])
        if not tasks:
            error("Task disappeared while waiting.")
            sys.exit(1)

        status = tasks[0]["lastStatus"]
        if status == "RUNNING":
            # Check if the ECS Exec agent is ready (avoids race condition)
            exec_ready = False
            for container in tasks[0].get("containers", []):
                for agent in container.get("managedAgents", []):
                    if agent.get("name") == "ExecuteCommandAgent" and agent.get("lastStatus") == "RUNNING":
                        exec_ready = True
                        break
            if exec_ready:
                info("Environment is ready.")
                return
            info("Waiting for exec agent...")
        if status in ("STOPPED", "DEACTIVATING"):
            reason = tasks[0].get("stoppedReason", "unknown")
            error(f"Environment failed to start: {reason}")
            sys.exit(1)

        time.sleep(5)
        waited += 5

    error("Timed out waiting for environment to start.")
    sys.exit(1)


# ECS Exec runs over SSM Session Manager, which terminates a session after 20
# minutes of idle time — a hard-coded, non-configurable limit for ECS Exec.
# exec_with_keepalive nudges the terminal size every KEEPALIVE_INTERVAL_SECONDS
# to emit a harmless resize that (we rely on, see the design doc) the SSM idle
# timer counts as activity. 120s = 2 min nudges well inside the 20-min window.
KEEPALIVE_INTERVAL_SECONDS = 120


def _pty_window_size(tty_fd):
    """Return (rows, cols) of the terminal on tty_fd, defaulting to 24x80."""
    try:
        size = os.get_terminal_size(tty_fd)
        return size.lines, size.columns
    except OSError:
        return 24, 80


def _set_pty_window_size(fd, rows, cols):
    """Set the window size of the pty referred to by fd."""
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def exec_with_keepalive(argv):
    """Run argv attached to a pty, keeping the SSM session alive while idle.

    Wraps the child (the interactive `aws ecs execute-command`) in a
    pseudo-terminal so a background timer can periodically resize it. The
    resize fires SIGWINCH in the AWS CLI, which the session-manager-plugin
    forwards to the SSM agent as a `set_size` control message — harmless to the
    remote shell (no bytes injected) but enough to reset the 20-minute idle
    timer, so a session left untouched is not dropped mid-task.

    Returns the child's exit code.

    Falls back to a plain subprocess when stdin is not a tty: there is no
    terminal to make raw or to keep alive (and the interactive session would be
    pointless anyway), so the pty machinery would only get in the way.
    """
    if not sys.stdin.isatty():
        try:
            return subprocess.run(argv).returncode
        except KeyboardInterrupt:
            return 130

    pid, master_fd = pty.fork()
    if pid == 0:
        # Child: become the AWS CLI. execvp only returns on failure.
        try:
            os.execvp(argv[0], argv)
        except OSError:
            os._exit(127)

    stdin_fd = sys.stdin.fileno()
    old_attrs = termios.tcgetattr(stdin_fd)

    rows, cols = _pty_window_size(stdin_fd)
    _set_pty_window_size(master_fd, rows, cols)

    # Forward real user resizes to the child so the remote shell tracks the
    # actual terminal. Changing master_fd's size signals the child, not us, so
    # the periodic keepalive nudge below never re-enters this handler.
    def handle_resize(signum, frame):
        r, c = _pty_window_size(stdin_fd)
        _set_pty_window_size(master_fd, r, c)

    old_winch = signal.signal(signal.SIGWINCH, handle_resize)

    last_keepalive = time.monotonic()
    try:
        tty.setraw(stdin_fd)
        while True:
            now = time.monotonic()
            if now - last_keepalive >= KEEPALIVE_INTERVAL_SECONDS:
                # Shrink one column and restore: two genuine size changes, so
                # the agent definitely sees activity. The brief intermediate
                # size makes full-screen apps repaint once — the accepted cost.
                r, c = _pty_window_size(stdin_fd)
                nudged = c - 1 if c > 1 else c + 1
                _set_pty_window_size(master_fd, r, nudged)
                time.sleep(0.05)
                _set_pty_window_size(master_fd, r, c)
                last_keepalive = now

            # PEP 475 auto-retries select across the SIGWINCH handler, so a
            # user resize mid-wait doesn't surface as an error here.
            readable, _, _ = select.select([stdin_fd, master_fd], [], [], 1)

            if stdin_fd in readable:
                data = os.read(stdin_fd, 1024)
                if not data:
                    break
                try:
                    os.write(master_fd, data)
                except OSError:
                    # Child closed the pty (session ended).
                    break

            if master_fd in readable:
                try:
                    data = os.read(master_fd, 1024)
                except OSError:
                    # Child closed the pty (session ended).
                    break
                if not data:
                    break
                try:
                    os.write(sys.stdout.fileno(), data)
                except OSError:
                    # stdout closed (e.g. piped reader went away).
                    break
    finally:
        termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old_attrs)
        signal.signal(signal.SIGWINCH, old_winch)
        os.close(master_fd)

    _, status = os.waitpid(pid, 0)
    return os.waitstatus_to_exitcode(status)


def connect_to_task(profile, cluster, task_arn, username):
    """Connect to a running task via ECS Exec (shells out to AWS CLI for interactive session)."""
    task_id = task_arn.split("/")[-1]
    container_name = f"{SERVICE_TAG}-{username}"

    info("Connecting...")
    # Wrapped in a pty keepalive (see exec_with_keepalive) so a session left
    # idle past SSM's 20-minute timeout isn't dropped. Ctrl-C is handled by the
    # remote shell — in raw mode the 0x03 byte flows through to it rather than
    # killing the launcher — so there is no KeyboardInterrupt to catch here.
    return exec_with_keepalive(
        [
            "aws",
            "ecs",
            "execute-command",
            "--profile",
            profile,
            "--cluster",
            cluster,
            "--task",
            task_id,
            "--container",
            container_name,
            "--interactive",
            "--command",
            f"gosu {username} /opt/bin/start-bidnamic-os.sh",
        ]
    )


def efs_utils_installed():
    """Return True if the amazon-efs-utils mount helper is on PATH."""
    return shutil.which("mount.efs") is not None


def efs_utils_libexec_bin():
    """Return the amazon-efs-utils private venv bin dir, or None if not found.

    The Homebrew formula installs botocore into a private venv under
    libexec/ but leaves mount.efs's shebang as `#!/usr/bin/env python3`.
    Whichever python3 PATH resolves at run time is what mount.efs imports
    from — and system / pyenv pythons rarely have botocore. We prepend
    this dir to PATH so mount.efs finds the formula's own interpreter.
    """
    mount_efs = shutil.which("mount.efs")
    if not mount_efs:
        return None
    libexec_bin = Path(mount_efs).resolve().parent.parent / "libexec" / "bin"
    if not libexec_bin.is_dir():
        return None
    return libexec_bin


def efs_utils_botocore_ok():
    """Return True if the formula's private Python can import botocore."""
    libexec_bin = efs_utils_libexec_bin()
    if libexec_bin is None:
        return False
    python_bin = libexec_bin / "python3"
    if not python_bin.exists():
        return False
    try:
        result = subprocess.run(
            [str(python_bin), "-c", "import botocore"],
            capture_output=True,
            timeout=10,
        )
    except (subprocess.SubprocessError, OSError):
        return False
    return result.returncode == 0


EFS_UTILS_INSTALL_HINT = (
    "amazon-efs-utils is not installed. Reinstall with:\n    brew reinstall bidnamic-os"
)

# macOS requires the EFS mount helper to be registered here so that
# `mount -t efs` dispatches correctly. amazon-efs-utils does not create this
# symlink automatically on macOS; `bidnamic-os post-install` creates it.
# MACOS_EFS_HELPER_ROOT is the top-level dir we own for cleanup — the
# Contents/Resources subtree underneath only exists because we created it.
MACOS_EFS_HELPER_ROOT = Path("/Library/Filesystems/efs.fs")
MACOS_EFS_HELPER_LINK = MACOS_EFS_HELPER_ROOT / "Contents/Resources/mount_efs"
MACOS_WATCHDOG_PLIST = Path("/Library/LaunchAgents/amazon-efs-mount-watchdog.plist")


def macos_efs_helper_registered():
    """Return True if the /Library EFS mount helper symlink exists."""
    return MACOS_EFS_HELPER_LINK.exists()


def get_macos_version():
    """Return (major, minor) macOS version tuple, or None if unavailable."""
    ver = platform.mac_ver()[0]
    if not ver:
        return None
    parts = ver.split(".")
    try:
        return (int(parts[0]), int(parts[1]) if len(parts) > 1 else 0)
    except ValueError:
        return None


def get_developer_tools_major_version():
    """Return the developer tools major version, or None if not installed.

    Homebrew formula builds (e.g. amazon-efs-utils) require developer tools
    whose major version matches the macOS major (CLT 26.x on macOS 26.x).
    Older tools pass `xcode-select -p` but fail Homebrew mid-install.

    Two valid setups exist on macOS:

    - **Standalone Command Line Tools.** `xcode-select -p` points at
      `/Library/Developer/CommandLineTools` and the CLT pkg is registered in
      pkgutil. This is the expected setup for this installer.
    - **Full Xcode.app.** `xcode-select -p` points inside
      `/Applications/Xcode.app` and the CLT pkg is absent. In that case we
      read the Xcode version via `xcodebuild -version` — the bundled tools
      are always at the same major as Xcode.
    """
    if not shutil.which("xcode-select"):
        return None
    # First: where is the developer directory currently pointing?
    try:
        selected = subprocess.run(
            ["xcode-select", "-p"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if selected.returncode != 0:
        return None
    selected_path = selected.stdout.strip()
    # Standalone CLT: version comes from the installer pkg receipt.
    try:
        pkg_info = subprocess.run(
            ["pkgutil", "--pkg-info=com.apple.pkg.CLTools_Executables"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.SubprocessError, OSError):
        pkg_info = None
    if pkg_info is not None and pkg_info.returncode == 0:
        for line in pkg_info.stdout.splitlines():
            if line.startswith("version:"):
                try:
                    return int(line.split()[1].split(".")[0])
                except (ValueError, IndexError):
                    return None
    # Full Xcode.app: pkgutil won't know about it, so parse xcodebuild output.
    if "Xcode.app" in selected_path and shutil.which("xcodebuild"):
        try:
            xcb = subprocess.run(
                ["xcodebuild", "-version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (subprocess.SubprocessError, OSError):
            return None
        if xcb.returncode != 0:
            return None
        first_line = xcb.stdout.splitlines()[0] if xcb.stdout else ""
        parts = first_line.split()
        if len(parts) >= 2:
            try:
                return int(parts[1].split(".")[0])
            except (ValueError, IndexError):
                return None
    return None


def get_aws_cli_major_version():
    """Return the AWS CLI major version as int, or None if not installed."""
    if not shutil.which("aws"):
        return None
    try:
        result = subprocess.run(
            ["aws", "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    # Output: "aws-cli/2.15.0 Python/3.11.6 ..."
    output = (result.stdout or result.stderr or "").strip()
    if not output.startswith("aws-cli/"):
        return None
    try:
        version_str = output.split()[0].split("/")[1]
        return int(version_str.split(".")[0])
    except (ValueError, IndexError):
        return None


def preflight_checks():
    """Verify local dependencies before any AWS sign-in attempt.

    Runs before SSO so a missing dependency fails fast and does not orphan
    a browser login or ECS task. The launcher is macOS-only — EFS mounting,
    Homebrew, and Session Manager install guidance all assume Darwin.
    """
    if platform.system() != "Darwin":
        error("bidnamic-os only supports macOS.")
        sys.exit(1)

    errors = []

    mac_ver = get_macos_version()
    if mac_ver is None:
        errors.append("Could not determine macOS version.")
    elif mac_ver[0] < 26:
        errors.append(f"macOS Tahoe (26.x) or later required (found {mac_ver[0]}.{mac_ver[1]}).")

    required_dev_major = mac_ver[0] if mac_ver else 26
    dev_major = get_developer_tools_major_version()
    if dev_major is None:
        errors.append("Command Line Tools not found. Install with: xcode-select --install")
    elif dev_major < required_dev_major:
        errors.append(
            f"Command Line Tools {dev_major}.x is too old for macOS "
            f"{required_dev_major}.x. Reinstall with:\n"
            "    sudo rm -rf /Library/Developer/CommandLineTools\n"
            "    sudo xcode-select --install"
        )

    if not efs_utils_installed():
        errors.append(EFS_UTILS_INSTALL_HINT)

    # Catches the "brew install bidnamic-os was run, but post-install
    # wasn't" case. Without this, the user would fail later inside
    # mount_efs after going through SSO setup — annoying and confusing.
    if not macos_efs_helper_registered():
        errors.append(
            "First-time setup hasn't been run. Run:\n    bidnamic-os post-install"
        )

    aws_major = get_aws_cli_major_version()
    if aws_major is None:
        errors.append(
            "AWS CLI v2 is not installed. Install from "
            "https://docs.aws.amazon.com/cli/latest/userguide/getting-started-version.html"
        )
    elif aws_major < 2:
        errors.append(
            f"AWS CLI v2 required (found v{aws_major}). Upgrade: "
            "https://docs.aws.amazon.com/cli/latest/userguide/getting-started-version.html"
        )

    if not shutil.which("session-manager-plugin"):
        errors.append(
            "AWS Session Manager plugin not found. Install with:\n    brew install --cask session-manager-plugin"
        )

    if boto3 is None:
        errors.append("boto3 is not installed. Reinstall with: brew reinstall bidnamic-os")

    if errors:
        for e in errors:
            error(e)
        sys.exit(1)


def _parse_mount_table(mount_output):
    """Parse `mount(8)` output into a set of mount point paths.

    Each line is "<device> on <mountpoint> (<type>, <opts>)". The device
    may itself contain spaces (e.g. "map auto_home"), so we split on the
    first " on "; the options are always a parenthesised suffix, so we
    rsplit on the final " (" to tolerate a mount point containing " (".
    """
    mounts = set()
    for line in mount_output.splitlines():
        if " on " not in line or " (" not in line:
            continue
        mountpoint = line.split(" on ", 1)[1].rsplit(" (", 1)[0]
        mounts.add(mountpoint)
    return mounts


def get_mount_table():
    """Return the set of mount point paths from the kernel mount table.

    Shells out to `mount(8)`, which reports getmntinfo(3) — in-kernel,
    in-memory data. Crucially it never does I/O against the mounted
    filesystems, so a stale or hung NFS mount is still listed instead of
    making the probe itself hang. Returns an empty set if `mount` cannot
    be run or exits non-zero.
    """
    try:
        result = subprocess.run(
            ["/sbin/mount"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.SubprocessError, OSError):
        return set()
    if result.returncode != 0:
        return set()
    return _parse_mount_table(result.stdout)


def is_mounted(path):
    """Return True if `path` is currently a mount point.

    Consults the kernel mount table rather than os.path.ismount(): the
    latter lstat()s `path`, and on a stale NFS mount that lstat hangs (or,
    with our soft mount, fails after timeo) — which os.path.ismount then
    reports as "not a mount point". That is exactly the case we most need
    to detect, so the stale mount can be torn down and remounted rather
    than the launcher blindly trying (and failing) to mkdir over it. The
    mount table lists stale and healthy mounts alike.
    """
    return str(path) in get_mount_table()


def is_mount_healthy(path):
    """Return True if `path` responds to directory I/O.

    A stale NFS mount (server reboot, network change, prior umount that
    only detached the namespace) still appears in the kernel mount table
    (so is_mounted reports it), but I/O against it fails. The launcher
    mounts with `soft` and a bounded
    `timeo` (see mount_efs), so this probe returns an error within
    seconds instead of hanging forever, making it safe as a liveness
    check.

    Uses scandir + a single next() rather than listdir so a busy share
    isn't enumerated end-to-end just to answer "does I/O work" — opening
    the directory and reading one entry is enough to surface ESTALE/EIO.
    """
    try:
        with os.scandir(str(path)) as it:
            next(it, None)
        return True
    except OSError:
        return False


def get_filesystem_id(session):
    """Discover the bidnamic-os EFS filesystem ID by its creation token."""
    efs = session.client("efs")
    response = efs.describe_file_systems(CreationToken=EFS_CREATION_TOKEN)
    filesystems = response.get("FileSystems", [])
    if not filesystems:
        return None
    return filesystems[0]["FileSystemId"]


def get_access_point_id(session, filesystem_id, email):
    """Find the access point for a user by matching the User tag == email."""
    efs = session.client("efs")
    paginator = efs.get_paginator("describe_access_points")
    for page in paginator.paginate(FileSystemId=filesystem_id):
        for ap in page.get("AccessPoints", []):
            for tag in ap.get("Tags", []):
                if tag.get("Key") == "User" and tag.get("Value", "").lower() == email.lower():
                    return ap["AccessPointId"]
    return None


def tailscale_cli():
    """Return the path to the tailscale CLI, or None if not found.

    Prefers `tailscale` on PATH; falls back to the Mac App binary which the
    App Store edition ships without symlinking into /usr/local/bin.
    """
    path = shutil.which("tailscale")
    if path:
        return path
    app_cli = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
    if os.path.exists(app_cli):
        return app_cli
    return None


def check_tailscale_running():
    """Verify the Tailscale Mac app is running and logged in.

    Fatal: exits on failure. `tailscale status` returns non-zero if the
    daemon is not running or the user is not signed in.
    """
    cli = tailscale_cli()
    if cli is None:
        error(
            "[step: tailscale check] tailscale CLI not found. Install the "
            "Tailscale Mac app from https://tailscale.com/download/mac and "
            "sign in."
        )
        sys.exit(1)

    info("Checking Tailscale status...")
    status = subprocess.run([cli, "status"], capture_output=True, text=True)
    if status.returncode != 0:
        error(
            "[step: tailscale check] Tailscale is not running or not signed "
            f"in. Open the Tailscale menu bar app and sign in. "
            f"{(status.stderr or status.stdout).strip()}"
        )
        sys.exit(1)
    info("Tailscale is running.")


def get_mount_target_ip(session, filesystem_id):
    """Return one available mount target IP. Tailscale routes to any AZ,
    so any available target works."""
    efs = session.client("efs")
    response = efs.describe_mount_targets(FileSystemId=filesystem_id)
    for mt in response.get("MountTargets", []):
        if mt.get("LifeCycleState") == "available":
            return mt["IpAddress"]
    return None


def prepare_mount_point(path):
    """Ensure `path` is safe to mount over.

    Mounting over a non-empty directory would hide the user's local files
    until unmount and risks them being confused with EFS contents. Symlinks
    and non-directories are rejected outright. A stray .DS_Store left by
    Finder is removed silently. Anything else is listed and the user is
    prompted to delete it. Fatal: exits the process if the path is unsafe
    or the user declines.
    """
    info(f"Verifying {path} is safe to use as a mount point...")
    # Reject symlinks before .exists() / .is_dir() — those follow links, so
    # a symlinked mount point would let us enumerate and rmtree the target's
    # contents after the prompt below.
    if path.is_symlink():
        error(
            f"[step: mount point check] {path} is a symlink. Move or remove "
            "it before reconnecting; the launcher will not mount over a "
            "symlink."
        )
        sys.exit(1)
    if not path.exists():
        return
    if not path.is_dir():
        error(f"[step: mount point check] {path} exists but is not a directory. Move or remove it before reconnecting.")
        sys.exit(1)
    # macOS sprinkles .DS_Store everywhere Finder peeks; nuke it silently
    # so a stray Finder visit doesn't block the mount.
    ds_store = path / ".DS_Store"
    if ds_store.is_file() and not ds_store.is_symlink():
        ds_store.unlink()
    entries = sorted(path.iterdir())
    if not entries:
        return
    info(f"{path} is not empty. Contents:")
    for entry in entries:
        suffix = "/" if entry.is_dir() and not entry.is_symlink() else ""
        print(f"  - {entry.name}{suffix}")
    response = input(f"Delete these {len(entries)} item(s) and continue? [y/N]: ").strip().lower()
    if response != "y":
        error(
            "[step: mount point check] Aborting. Move or remove the files "
            "manually before reconnecting; the launcher will not mount over "
            "existing local files."
        )
        sys.exit(1)
    for entry in entries:
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry)
        else:
            entry.unlink()
    info(f"Deleted {len(entries)} item(s) from {path}.")


def mount_efs(session, email, profile):
    """Mount the user's EFS access point at LOCAL_MOUNT_PATH.

    Fatal: exits the process if any step fails. The caller can rely on a
    successful return meaning the mount is in place.
    """
    info(f"Checking if {LOCAL_MOUNT_PATH} is already mounted...")
    if is_mounted(LOCAL_MOUNT_PATH):
        if is_mount_healthy(LOCAL_MOUNT_PATH):
            info(f"EFS already mounted at {LOCAL_MOUNT_PATH}.")
            return
        # Stale mount: the kernel still has the mount point but I/O
        # fails. Without this branch the launcher would treat it as
        # healthy and skip remounting, leaving the user with a broken
        # share. Drop it and fall through to the normal mount flow.
        info(f"Existing mount at {LOCAL_MOUNT_PATH} is unresponsive. Unmounting before reconnecting...")
        _unmount_path(LOCAL_MOUNT_PATH)
        if is_mounted(LOCAL_MOUNT_PATH):
            error(
                "[step: stale mount cleanup] Could not unmount stale "
                f"mount at {LOCAL_MOUNT_PATH}. Close any terminals, "
                "editors, or Finder windows under that path and try again."
            )
            sys.exit(1)

    info("Checking amazon-efs-utils is installed...")
    if not efs_utils_installed():
        error(f"[step: efs-utils check] {EFS_UTILS_INSTALL_HINT}")
        sys.exit(1)

    libexec_bin = efs_utils_libexec_bin()
    if libexec_bin is None or not efs_utils_botocore_ok():
        # The formula's private Python should carry botocore; if it's
        # missing the install is broken (and SSO profile mounts would fail
        # with a confusing /var/root/.aws/ error). Reinstalling rebuilds
        # the libexec venv.
        error(
            "[step: efs-utils check] amazon-efs-utils is installed but its "
            "private Python cannot import botocore. Reinstall with:\n"
            "    brew reinstall amazon-efs-utils"
        )
        sys.exit(1)

    if platform.system() == "Darwin" and not macos_efs_helper_registered():
        error(
            "[step: efs helper check] macOS EFS mount helper is not "
            "registered. Run: bidnamic-os post-install"
        )
        sys.exit(1)

    info("Discovering EFS filesystem by creation token...")
    filesystem_id = get_filesystem_id(session)
    if not filesystem_id:
        error(
            "[step: discover filesystem] Could not find the bidnamic-os EFS "
            f"filesystem (creation token: {EFS_CREATION_TOKEN})."
        )
        sys.exit(1)
    info(f"Found filesystem {filesystem_id}.")

    info(f"Looking up EFS access point for {email}...")
    access_point_id = get_access_point_id(session, filesystem_id, email)
    if not access_point_id:
        error(
            f"[step: lookup access point] No EFS access point found for "
            f"{email} on {filesystem_id}. Has this user been deployed to "
            "this environment?"
        )
        sys.exit(1)
    info(f"Found access point {access_point_id}.")

    info("Resolving an available EFS mount target IP...")
    mount_target_ip = get_mount_target_ip(session, filesystem_id)
    if not mount_target_ip:
        error(f"[step: resolve mount target] Could not resolve an available EFS mount target IP for {filesystem_id}.")
        sys.exit(1)
    info(f"Using mount target {mount_target_ip}.")

    check_tailscale_running()

    prepare_mount_point(LOCAL_MOUNT_PATH)

    info(f"Creating mount point directory {LOCAL_MOUNT_PATH}...")
    try:
        LOCAL_MOUNT_PATH.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        error(f"[step: create mount point] Could not create {LOCAL_MOUNT_PATH}: {e}")
        sys.exit(1)

    info(f"Mounting EFS at {LOCAL_MOUNT_PATH} (will prompt for password)...")
    # HOME is set explicitly so botocore expands ~/.aws/config and
    # ~/.aws/sso/cache/ to the invoking user's home, not /var/root.
    #
    # PATH is pinned to the formula's libexec bin so mount.efs's
    # `#!/usr/bin/env python3` shebang resolves to the private interpreter
    # that ships with botocore — not whichever python3 sudo would otherwise
    # pick (pyenv shims, /usr/bin/python3, etc.), which usually lacks
    # botocore and silently falls back to a file-only credential lookup
    # that fails for SSO profiles.
    #
    # `sudo env VAR=val cmd` is used instead of `sudo -E` because macOS sudoers
    # defaults strip HOME even with -E. `env` is exec'd by sudo as root, then
    # sets the vars itself before exec'ing mount.efs, bypassing sudo's env
    # filtering entirely.
    #
    # We invoke mount.efs directly instead of `mount -t efs` because on macOS
    # the kernel's mount dispatch strips the environment, so these vars would
    # never reach the helper.
    #
    # soft/timeo=300 keep Finder responsive when the share is slow or briefly
    # unreachable: soft makes I/O return errors instead of hanging forever, and
    # timeo=300 sets the per-RPC timeout to 30s (timeo is in tenths of a second
    # per the NFS docs).
    mount_options = (
        f"tls,iam,region={REGION},awsprofile={profile},accesspoint={access_point_id},mounttargetip={mount_target_ip}"
        ",soft,timeo=300"
    )
    mount_cmd = [
        "sudo",
        "env",
        f"HOME={Path.home()}",
        f"PATH={libexec_bin}:/usr/bin:/bin:/usr/sbin:/sbin",
        shutil.which("mount.efs"),
        "-o",
        mount_options,
        f"{filesystem_id}:/",
        str(LOCAL_MOUNT_PATH),
    ]
    info(f"Running: {' '.join(mount_cmd)}")
    result = subprocess.run(mount_cmd)
    if result.returncode != 0:
        error(f"[step: mount] sudo mount failed with exit code {result.returncode}. Options: {mount_options}")
        sys.exit(1)

    info(f"EFS mounted at {LOCAL_MOUNT_PATH}.")

    disable_spotlight_indexing(LOCAL_MOUNT_PATH)


def disable_spotlight_indexing(path):
    """Turn off Spotlight indexing on the mounted volume.

    Spotlight crawling EFS is the most likely cause of the Finder hangs we've
    seen on the shared drive (issue #249) — it walks the whole tree and pins
    file handles over a high-latency NFS mount. `mdutil` won't operate on a
    sub-volume mount point ("unknown indexing state"), so we drop a
    `.metadata_never_index` marker at the mount root instead — Spotlight
    honours it and skips the tree entirely. Best-effort: a failure here
    shouldn't abort an otherwise successful mount.
    """
    marker = path / ".metadata_never_index"
    info(f"Disabling Spotlight indexing on {path} via {marker.name}...")
    try:
        marker.touch(exist_ok=True)
    except OSError as e:
        error(f"Could not disable Spotlight indexing on {path}: {e} (continuing)")


def unmount_efs():
    """Unmount the EFS share. Best-effort."""
    if platform.system() != "Darwin":
        return
    if is_mounted(LOCAL_MOUNT_PATH):
        _unmount_path(LOCAL_MOUNT_PATH)


def _unmount_path(path):
    info(f"Unmounting {path}...")
    result = subprocess.run(
        ["sudo", "umount", str(path)],
        stderr=subprocess.DEVNULL,
    )
    if result.returncode != 0:
        error(f"{path} is busy — close any terminals, editors, or Finder windows under that path. Forcing unmount...")
        subprocess.run(["sudo", "umount", "-f", str(path)])


def cmd_connect(session, profile, env):
    email, username = get_user_identity(session)
    cluster = env["cluster"]
    info(f"Hello, {username}.")

    # Mount EFS first so a mount failure bails out before we start an
    # ECS task that would otherwise be left running without a local mount.
    if platform.system() == "Darwin":
        mount_efs(session, email, profile)

    ecs = session.client("ecs")
    task = find_running_task(ecs, cluster, email)

    if task:
        task_arn = task["taskArn"]
        if task["lastStatus"] == "RUNNING":
            info("Found your running environment.")
        else:
            info("Your environment is starting up...")
            wait_for_task(ecs, cluster, task_arn)
    else:
        task_arn = start_task(ecs, env, email, username)
        wait_for_task(ecs, cluster, task_arn)

    return connect_to_task(profile, cluster, task_arn, username)


def cmd_stop(session, profile, env):
    email, username = get_user_identity(session)
    cluster = env["cluster"]
    ecs = session.client("ecs")
    task = find_running_task(ecs, cluster, email)

    if task:
        info("Stopping your environment...")
        ecs.stop_task(
            cluster=cluster,
            task=task["taskArn"],
            reason="User requested stop",
        )
        info("Environment stopped.")
    else:
        info("No running environment found.")


def cmd_unmount(session, profile, env):
    if not is_mounted(LOCAL_MOUNT_PATH):
        info("Nothing to unmount.")
        return
    unmount_efs()


def cmd_status(session, profile, env):
    email, username = get_user_identity(session)
    cluster = env["cluster"]
    ecs = session.client("ecs")
    task = find_running_task(ecs, cluster, email)

    if task:
        info(f"Environment status: {task['lastStatus']}")
    else:
        info("No running environment.")


def cmd_tutorial():
    if not TUTORIAL_PATH.exists():
        error(f"Tutorial not found at {TUTORIAL_PATH}. Reinstall: brew reinstall bidnamic-os")
        return 1
    info(f"Opening tutorial: {TUTORIAL_PATH}")
    if not webbrowser.open(TUTORIAL_PATH.as_uri()):
        error(f"Could not open a browser. Open {TUTORIAL_PATH} manually.")
        return 1


def cmd_version():
    print(__version__)


def _check_post_install_environment():
    """Fail-fast environment checks before doing privileged work.

    These mirror the upfront checks the previous install.sh ran, so users
    on an unsupported macOS / arch / dev-tools / Tailscale config get a
    clear error before being prompted for sudo.
    """
    if platform.system() != "Darwin":
        error("post-install only supports macOS.")
        return False
    if platform.machine() != "arm64":
        error("Apple Silicon (arm64) required — this installer assumes the /opt/homebrew prefix.")
        return False

    mac_ver = get_macos_version()
    if mac_ver is None:
        error("Could not determine macOS version.")
        return False
    if mac_ver[0] < 26:
        error(f"macOS Tahoe (26.x) or later required (found {mac_ver[0]}.{mac_ver[1]}).")
        return False

    dev_major = get_developer_tools_major_version()
    if dev_major is None:
        error("Command Line Tools not found. Install with: xcode-select --install")
        return False
    if dev_major < mac_ver[0]:
        error(
            f"Command Line Tools {dev_major}.x is too old for macOS {mac_ver[0]}.x. "
            "Reinstall with:\n"
            "    sudo rm -rf /Library/Developer/CommandLineTools\n"
            "    sudo xcode-select --install"
        )
        return False

    if not Path("/Applications/Tailscale.app").is_dir():
        error(
            "Tailscale Mac app not found. Install from "
            "https://tailscale.com/download/mac (App Store edition), sign in, "
            "then re-run post-install."
        )
        return False

    return True


def cmd_post_install():
    """Run the one-time setup after `brew install bidnamic-os`.

    Installs the session-manager-plugin cask (formulae can't depend on
    casks), wipes artefacts from the legacy zip-based installer, then
    registers the macOS EFS mount helper symlink and the watchdog
    LaunchAgent — neither of which `brew install` can do unprivileged.
    Idempotent: re-running is safe and re-asserts the desired state.
    """
    if not _check_post_install_environment():
        return 1

    if not shutil.which("session-manager-plugin"):
        info("Installing AWS Session Manager plugin via brew...")
        subprocess.run(["brew", "install", "--cask", "session-manager-plugin"], check=True)

    info("You'll be prompted for your macOS password.")

    info("Cleaning up artefacts from the legacy zip-based installer...")
    for path in LEGACY_INSTALL_PATHS:
        if path.exists() or path.is_symlink():
            info(f"  Removing {path}")
            sudo("rm", "-rf", str(path))

    info("Registering the EFS mount helper with macOS...")
    sudo("mkdir", "-p", str(MACOS_EFS_HELPER_LINK.parent))
    sudo("ln", "-sfn", "/opt/homebrew/bin/mount.efs", str(MACOS_EFS_HELPER_LINK))

    info("Enabling the EFS watchdog LaunchAgent...")
    watchdog_src = Path("/opt/homebrew/opt/amazon-efs-utils/libexec/amazon-efs-mount-watchdog.plist")
    if not watchdog_src.exists():
        error(
            f"LaunchAgent source missing at {watchdog_src}. Reinstall: brew reinstall bidnamic-os"
        )
        return 1
    sudo("cp", str(watchdog_src), str(MACOS_WATCHDOG_PLIST))
    # Plists in /Library/LaunchAgents auto-load at next login, but we load
    # now so the very first `bidnamic-os connect` in this session has a
    # supervised stunnel (restart on crash, IAM credential refresh).
    # Unload-then-load makes re-running post-install safe even if a previous
    # run already loaded the agent.
    sudo("launchctl", "unload", str(MACOS_WATCHDOG_PLIST), check=False)
    sudo("launchctl", "load", str(MACOS_WATCHDOG_PLIST))

    info("Setup complete. Run `bidnamic-os` to connect.")
    if TUTORIAL_PATH.exists():
        subprocess.run(["open", str(TUTORIAL_PATH)], check=False)


def cmd_upgrade():
    """Update Homebrew and upgrade bidnamic-os to the latest release.

    Runs `brew update` (refreshes formula definitions, including this
    tap) then `brew upgrade bidnamic-os`, and finally re-runs
    post-install so any new privileged setup (EFS mount helper, watchdog
    LaunchAgent) is applied. Re-running post-install is idempotent.

    `brew upgrade` rewrites this script on disk, but the running process
    still holds the *old* code in memory — so post-install is reached by
    re-exec'ing the freshly installed `bidnamic-os`, not by calling
    cmd_post_install() directly. That guarantees the new setup logic
    runs. execv replaces this process, so nothing returns past it.
    """
    if platform.system() != "Darwin":
        error("upgrade only supports macOS.")
        return 1

    brew = shutil.which("brew")
    if brew is None:
        error("Homebrew is not installed or not on PATH. See https://brew.sh")
        return 1

    info("Updating Homebrew formula definitions...")
    if subprocess.run([brew, "update"]).returncode != 0:
        error("`brew update` failed. Resolve the error above and retry.")
        return 1

    info("Upgrading bidnamic-os...")
    # `brew upgrade` exits 0 whether it upgraded or the package was
    # already current, so a zero exit tells us nothing about whether the
    # script changed — we re-run post-install regardless; it's idempotent.
    if subprocess.run([brew, "upgrade", "bidnamic-os"]).returncode != 0:
        error("`brew upgrade bidnamic-os` failed. Resolve the error above and retry.")
        return 1

    bidnamic_os = shutil.which("bidnamic-os")
    if bidnamic_os is None:
        error(
            "Could not find the bidnamic-os binary after upgrade. "
            "Run `bidnamic-os post-install` manually."
        )
        return 1

    info("Re-running post-install...")
    os.execv(bidnamic_os, [bidnamic_os, "post-install"])


def cmd_uninstall():
    """Reverse `post-install` and remove the brew package.

    Order matters: unload+remove privileged artefacts first (LaunchAgent,
    EFS helper symlink), unmount any active EFS shares so brew uninstall
    isn't blocked, then exec brew uninstall. The exec replaces this
    process so brew can safely remove the script that's currently
    executing.
    """
    if platform.system() != "Darwin":
        error("uninstall only supports macOS.")
        return 1

    info("Reversing post-install setup. You'll be prompted for your macOS password.")

    if MACOS_WATCHDOG_PLIST.exists():
        # launchctl unload exits non-zero if the agent isn't currently
        # loaded; that's fine — we still want to remove the plist.
        sudo("launchctl", "unload", str(MACOS_WATCHDOG_PLIST), check=False)
        sudo("rm", "-f", str(MACOS_WATCHDOG_PLIST))

    if MACOS_EFS_HELPER_ROOT.exists():
        info(f"Removing {MACOS_EFS_HELPER_ROOT}")
        sudo("rm", "-rf", str(MACOS_EFS_HELPER_ROOT))

    unmount_efs()

    info("Removing brew package...")
    # execvp replaces this process. brew can then remove the script
    # without trying to delete an open executable mid-run.
    os.execvp("brew", ["brew", "uninstall", "bidnamic-os"])


def main():
    parser = argparse.ArgumentParser(
        prog="bidnamic-os",
        description="Manage your Bidnamic OS environment",
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="connect",
        choices=[
            "connect", "stop", "status", "unmount", "tutorial",
            "version", "post-install", "upgrade", "uninstall",
        ],
        help="Command to run (default: connect)",
    )
    parser.add_argument(
        "--env",
        default="live",
        choices=ENVIRONMENTS.keys(),
        help="Target environment (default: live)",
    )
    args = parser.parse_args()

    if os.getuid() == 0:
        error("Do not run this script as root (or via sudo). It will prompt for sudo when needed.")
        sys.exit(1)

    # Local-only commands. These skip preflight + SSO entirely so they work
    # without AWS credentials, network, or a configured profile.
    # post-install, upgrade, and uninstall manage their own sudo calls
    # (upgrade re-runs post-install, which prompts for sudo).
    local_commands = {
        "version": cmd_version,
        "tutorial": cmd_tutorial,
        "post-install": cmd_post_install,
        "upgrade": cmd_upgrade,
        "uninstall": cmd_uninstall,
    }
    if args.command in local_commands:
        sys.exit(local_commands[args.command]() or 0)

    preflight_checks()

    env = get_env_config(args.env)
    profile = configure_profile(args.env, env)
    session = get_session(profile)

    aws_commands = {
        "connect": cmd_connect,
        "stop": cmd_stop,
        "status": cmd_status,
        "unmount": cmd_unmount,
    }
    exit_code = aws_commands[args.command](session, profile, env)
    sys.exit(exit_code or 0)


if __name__ == "__main__":
    main()
