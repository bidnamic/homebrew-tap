#!/usr/bin/env bash
#
# bidnamic-os Linux installer (Debian/Ubuntu and Arch).
#
# Thin by design: this only installs the launcher itself and its boto3
# venv. The heavy prerequisites — awscli v2, amazon-efs-utils,
# session-manager-plugin, tailscale — are NOT installed here; they're
# documented per-distro in linux/README.md and verified by the launcher's
# own preflight checks at run time. Keeping them out of this script avoids
# baking distro-specific build steps (efs-utils from source on Debian, AUR
# makepkg on Arch) into a curl|bash one-liner.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/bidnamic/homebrew-tap/main/linux/installer.sh | bash
#
# Override the release with BIDNAMIC_OS_TAG=v2026.06.08-xxxx before piping.

set -euo pipefail

REPO="bidnamic/homebrew-tap"
PREFIX="/usr/local"
SHARE="${PREFIX}/share/bidnamic-os"
VENV="${SHARE}/venv"
BIN="${PREFIX}/bin/bidnamic-os"

err() { printf '\033[0;31m[installer]\033[0m %s\n' "$*" >&2; }
info() { printf '\033[0;32m[installer]\033[0m %s\n' "$*"; }

need() {
  command -v "$1" >/dev/null 2>&1 || { err "required command not found: $1"; exit 1; }
}

need curl
need tar
need python3

# python3-venv is a separate package on Debian; fail early with a clear hint
# rather than midway through venv creation.
if ! python3 -c 'import venv' >/dev/null 2>&1; then
  err "python3 venv module missing. On Debian/Ubuntu: sudo apt-get install -y python3-venv"
  exit 1
fi

# Resolve the release tag. The release workflow publishes a source tarball
# per tag; we pin to the latest release for a reproducible install.
TAG="${BIDNAMIC_OS_TAG:-}"
if [ -z "${TAG}" ]; then
  TAG="$(curl -fsSL "https://api.github.com/repos/${REPO}/releases/latest" \
    | sed -n 's/.*"tag_name"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' \
    | head -n1)"
fi
[ -n "${TAG}" ] || { err "could not resolve the latest release tag"; exit 1; }
VERSION="${TAG#v}"

TMP="$(mktemp -d)"
trap 'rm -rf "${TMP}"' EXIT

info "Downloading bidnamic-os ${TAG}..."
curl -fsSL "https://github.com/${REPO}/archive/refs/tags/${TAG}.tar.gz" \
  | tar -xz -C "${TMP}" --strip-components=1

info "Installing to ${PREFIX} (sudo required)..."
sudo mkdir -p "${SHARE}"
sudo python3 -m venv "${VENV}"
sudo "${VENV}/bin/pip" install --no-cache-dir --upgrade boto3

# The launcher's shebang (#!/usr/local/share/bidnamic-os/venv/bin/python) and
# SHARE_DIR (/usr/local/share/bidnamic-os) already match this layout — only
# the version needs stamping, exactly as the Homebrew formula does on macOS.
sudo install -m 0755 "${TMP}/launcher/bidnamic_os.py" "${BIN}"
sudo install -m 0644 "${TMP}/launcher/TUTORIAL.html" "${SHARE}/TUTORIAL.html"
sudo sed -i "s|^__version__ = .*|__version__ = \"${VERSION}\"|" "${BIN}"

info "Installed bidnamic-os ${VERSION}."
info "Run 'bidnamic-os' to connect (it will flag any missing prerequisites)."
