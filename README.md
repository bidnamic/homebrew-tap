# bidnamic-os homebrew tap

Public Homebrew tap for the Bidnamic OS launcher.

> **On Linux (Debian/Ubuntu or Arch)?** Homebrew isn't the install path —
> see [linux/README.md](linux/README.md) for prerequisites and a one-line
> installer. The rest of this page is macOS-only.

## Prerequisites

Install these before installing `bidnamic-os`:

- **macOS Tahoe (26.x) or later**, on **Apple Silicon (arm64)**
- **Xcode Command Line Tools** matching your macOS major version:

  ```
  xcode-select --install
  ```

- **Tailscale Mac app** — https://tailscale.com/download/mac
  (App Store edition; sign in before running `bidnamic-os post-install`)
- **Homebrew** — https://brew.sh

## Install

See [launcher/INSTALL.html](launcher/INSTALL.html) for the full step-by-step guide.

## Use

```
bidnamic-os                 # connect to the cloud agent
bidnamic-os tutorial        # open the AM tutorial
bidnamic-os version         # print the installed version
bidnamic-os status          # check whether your environment is running
bidnamic-os stop            # stop your environment
bidnamic-os unmount         # unmount the EFS share
bidnamic-os upgrade         # update to the latest release
```

## Upgrade

```
bidnamic-os upgrade
```

This runs `brew update`, `brew upgrade bidnamic-os`, and re-runs
`post-install` (which will prompt for your macOS password). The equivalent
manual steps still work:

```
brew update
brew upgrade bidnamic-os
bidnamic-os post-install
```

Releases are cut automatically on every change to `launcher/`; new versions
appear within minutes of merging.

## Uninstall

```
bidnamic-os uninstall
```

This unloads the LaunchAgent, removes the EFS mount helper, unmounts any
active shares, and removes the brew package.

## Linux (Debian/Arch)

Linux users don't use Homebrew. The same launcher is installed via a thin
script, with the AWS prerequisites installed by hand per distro. Full
instructions: [linux/README.md](linux/README.md).

```bash
curl -fsSL https://raw.githubusercontent.com/bidnamic/homebrew-tap/main/linux/installer.sh | bash
```

## Reporting issues

File issues in the `bidnamic-os` repo, not here. This repo's contents are
mechanically managed — the formula is auto-updated by CI on every release.
