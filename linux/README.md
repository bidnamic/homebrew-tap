# bidnamic-os on Linux (Debian/Ubuntu & Arch)

The same launcher that ships via Homebrew on macOS runs on Linux. The
installer is thin — it only installs the launcher and its `boto3` venv. The
prerequisites below are installed by hand (mirroring the Tailscale-app
prerequisite on macOS); the launcher's preflight checks verify them and print
a hint for anything missing.

## Prerequisites

You need: **Python 3** (with `venv`), **curl**, **Tailscale** (running and
signed in), **AWS CLI v2**, **session-manager-plugin**, and
**amazon-efs-utils**. None of the AWS ones are in the base distro repos, so:

### Debian / Ubuntu

```bash
# Base tooling
sudo apt-get update
sudo apt-get install -y python3 python3-venv curl unzip git

# Tailscale (then sign in)
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up

# AWS CLI v2 (official bundled installer — apt ships v1)
curl "https://awscli.amazonaws.com/awscli-exe-linux-$(uname -m).zip" -o awscliv2.zip
unzip -q awscliv2.zip && sudo ./aws/install && rm -rf aws awscliv2.zip

# Session Manager plugin (.deb). Use ubuntu_arm64 instead of ubuntu_64bit on arm64.
curl "https://s3.amazonaws.com/session-manager-downloads/plugin/latest/ubuntu_64bit/session-manager-plugin.deb" \
  -o session-manager-plugin.deb
sudo dpkg -i session-manager-plugin.deb && rm session-manager-plugin.deb

# amazon-efs-utils (built from source — not in apt). Pin to the stunnel-based
# 1.x line: 2.0+ needs a heavy Rust/CMake/Go AWS-LC-FIPS build.
sudo apt-get install -y binutils gettext-base stunnel4
git clone -b v1.36.0 https://github.com/aws/efs-utils
( cd efs-utils && ./build-deb.sh && sudo apt-get install -y ./build/amazon-efs-utils*deb )
rm -rf efs-utils

# efs-utils 1.x reads region only from its config (not the mount option, and
# IMDS isn't reachable off-EC2). bidnamic-os is always eu-west-1:
sudo sed -i 's/^#region = .*/region = eu-west-1/' /etc/amazon/efs/efs-utils.conf
```

### Arch

The three AWS tools live in three different places: `aws-cli-v2` is in the
official repos, `aws-session-manager-plugin` is in the AUR, and
`amazon-efs-utils` is in **neither** — so build it from the PKGBUILD shipped in
this repo under [arch/amazon-efs-utils](arch/amazon-efs-utils).

```bash
# Base + Tailscale + AWS CLI v2 (all official repos)
sudo pacman -S --needed python curl base-devel git tailscale aws-cli-v2
sudo systemctl enable --now tailscaled && sudo tailscale up

# Session Manager plugin (AUR)
paru -S aws-session-manager-plugin          # or: yay -S aws-session-manager-plugin

# amazon-efs-utils — build the bundled PKGBUILD (pulls stunnel + python-botocore)
paru -B linux/arch/amazon-efs-utils         # run from the repo root
#   no AUR helper? cd linux/arch/amazon-efs-utils && makepkg -si
sudo systemctl enable --now amazon-efs-mount-watchdog
```

`makepkg -si` / `paru -B` install the runtime dependencies automatically. The
PKGBUILD pins efs-utils to the stunnel-based 1.x line (no Rust/FIPS build);
bump `pkgver` + `sha256sums` to upgrade within 1.x.

### EFS mount watchdog

For long-lived TLS mounts, the `amazon-efs-utils` watchdog should be running
(it supervises the stunnel and refreshes IAM credentials):

```bash
sudo systemctl enable --now amazon-efs-mount-watchdog
```

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/bidnamic/homebrew-tap/main/linux/installer.sh | bash
```

Installs to `/usr/local/bin/bidnamic-os` with a `boto3` venv under
`/usr/local/share/bidnamic-os`. Pin a specific release with
`BIDNAMIC_OS_TAG=v… ` before the pipe.

## Use

```bash
bidnamic-os            # mount EFS + connect to the cloud agent
bidnamic-os status     # is my environment running?
bidnamic-os stop       # stop my environment
bidnamic-os unmount    # unmount the EFS share
bidnamic-os tutorial   # open the AM tutorial
bidnamic-os version    # print the installed version
```

The first `bidnamic-os` run mounts your EFS access point at `~/bidnamic-os`
(prompts for your `sudo` password) and then connects.

## Upgrade

```bash
bidnamic-os upgrade
```

Re-runs the installer to fetch the latest release (there's no Homebrew on
Linux).

## Uninstall

```bash
bidnamic-os uninstall
```

Unmounts any active share and removes the installed files under
`/usr/local`. Prerequisites (awscli, efs-utils, session-manager-plugin,
Tailscale) are left installed — remove those with your package manager if you
want them gone.

## Differences from macOS

- No `post-install` step: the distro `amazon-efs-utils` package registers its
  own mount helper and watchdog, so there's nothing privileged to set up.
- EFS is mounted with `mount -t efs` (the Linux kernel preserves the
  environment for the mount helper; macOS strips it, which is why the macOS
  path invokes `mount.efs` directly).
- No Spotlight indexing to disable.
