# bidnamic-os homebrew tap

Public Homebrew tap for the Bidnamic OS launcher.

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

```
brew tap bidnamic/homebrew-tap
brew install bidnamic-os
bidnamic-os post-install
```

`post-install` is a one-time privileged step that registers the EFS mount
helper and the watchdog LaunchAgent. It will prompt for your macOS password.

## Use

```
bidnamic-os                 # connect to the cloud agent
bidnamic-os tutorial        # open the AM tutorial
bidnamic-os version         # print the installed version
bidnamic-os status          # check whether your environment is running
bidnamic-os stop            # stop your environment
bidnamic-os unmount         # unmount the EFS share
```

## Upgrade

```
brew update
brew upgrade bidnamic-os
```

Releases are cut automatically on every change to `launcher/`; new versions
appear within minutes of merging.

## Uninstall

```
bidnamic-os uninstall
```

This unloads the LaunchAgent, removes the EFS mount helper, unmounts any
active shares, and removes the brew package.

## Reporting issues

File issues in the `bidnamic-os` repo, not here. This repo's contents are
mechanically managed — the formula is auto-updated by CI on every release.
