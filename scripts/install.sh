#!/usr/bin/env bash

set -euo pipefail

COMMAND=""
MODE=""
START_SERVICE=1
SETCAP="${SETCAP:-0}"
PYTHON_BIN="${PYTHON:-python3}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
CONFIG_TEMPLATE="${REPO_ROOT}/packaging/config/dmx-bridge.toml"
SYSTEM_UNIT="${REPO_ROOT}/packaging/systemd/dmx-lan-bridge.service"
USER_UNIT="${REPO_ROOT}/packaging/systemd/dmx-lan-bridge-user.service"

log() {
  echo "[install] $*"
}

warn() {
  echo "[install][warn] $*" >&2
}

usage() {
  cat <<'EOF'
Usage: scripts/install.sh <install|uninstall> [--system|--user] [options]

Options:
  --no-start           Install unit files but do not start them immediately.
  --setcap             Apply cap_net_bind_service to the dmx-lan-bridge binary.
  --python PATH        Python interpreter to use (default: python3 or $PYTHON env).
  -h, --help           Show this help message.

Environment:
  SETCAP               Default for --setcap (0 or 1).
  PYTHON               Default interpreter when --python is omitted.
EOF
  exit 1
}

require_root() {
  if [[ "${EUID}" -ne 0 ]]; then
    echo "This action requires root privileges." >&2
    exit 1
  fi
}

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    exit 1
  fi
}

maybe_setcap() {
  if [[ "${SETCAP}" -ne 1 ]]; then
    return
  fi
  if [[ "${EUID}" -ne 0 ]]; then
    warn "--setcap requested but not running as root; skipping capability changes."
    return
  fi
  if ! command -v setcap >/dev/null 2>&1; then
    warn "setcap not available; skipping capability changes."
    return
  fi

  local bin_path
  bin_path="$(command -v dmx-lan-bridge || command -v govee-artnet-bridge || true)"
  if [[ -z "${bin_path}" ]]; then
    warn "Could not find dmx-lan-bridge binary to apply capabilities."
    return
  fi

  if setcap 'cap_net_bind_service=+ep' "${bin_path}"; then
    log "Applied cap_net_bind_service to ${bin_path}"
  else
    warn "Failed to apply capabilities to ${bin_path}"
  fi
}

ensure_build_deps() {
  log "Checking Python build dependencies..."

  # Check if setuptools and wheel are available
  if "${PYTHON_BIN}" -c "import setuptools, wheel" 2>/dev/null; then
    log "Build dependencies already available."
    return 0
  fi

  log "Build dependencies missing (setuptools, wheel)."

  # If running as root, try to install via apt
  if [[ "${EUID}" -eq 0 ]]; then
    if command -v apt-get >/dev/null 2>&1; then
      log "Attempting to install build dependencies via apt..."
      if apt-get update -qq && apt-get install -y python3-setuptools python3-wheel 2>/dev/null; then
        log "Build dependencies installed via apt."
        return 0
      else
        warn "Failed to install build dependencies via apt."
      fi
    fi
  fi

  # Final check - maybe they were just installed
  if "${PYTHON_BIN}" -c "import setuptools, wheel" 2>/dev/null; then
    return 0
  fi

  echo "ERROR: Missing Python build dependencies (setuptools, wheel)." >&2
  echo "Please install them with: sudo apt install python3-setuptools python3-wheel" >&2
  echo "Or ensure you have network connectivity for pip to download them." >&2
  exit 1
}

install_system() {
  require_root
  require_command systemctl
  ensure_build_deps

  log "Installing Python package (system)..."
  "${PYTHON_BIN}" -m pip install --break-system-packages --upgrade "${REPO_ROOT}"

  if id -u dmx-bridge >/dev/null 2>&1; then
    log "User dmx-bridge already exists."
  else
    log "Creating system user dmx-bridge..."
    useradd --system --home /var/lib/dmx-bridge --create-home \
      --shell /usr/sbin/nologin --comment "DMX LAN bridge service user" \
      dmx-bridge
  fi

  log "Ensuring configuration directory /etc/dmx-bridge..."
  install -d -m 755 /etc/dmx-bridge
  if [[ ! -f /etc/dmx-bridge/config.toml ]]; then
    install -m 640 -o root -g dmx-bridge "${CONFIG_TEMPLATE}" /etc/dmx-bridge/config.toml
  else
    log "Existing /etc/dmx-bridge/config.toml preserved."
  fi

  log "Ensuring data directory /var/lib/dmx-bridge..."
  install -d -o dmx-bridge -g dmx-bridge -m 750 /var/lib/dmx-bridge

  # Prevent the pre-2.0 unit from running alongside the renamed service.
  systemctl disable --now dmx-bridge.service >/dev/null 2>&1 || true
  rm -f /etc/systemd/system/dmx-bridge.service

  log "Installing systemd unit..."
  install -m 644 "${SYSTEM_UNIT}" /etc/systemd/system/dmx-lan-bridge.service
  systemctl daemon-reload

  if [[ "${START_SERVICE}" -eq 1 ]]; then
    systemctl enable --now dmx-lan-bridge.service
  else
    systemctl enable dmx-lan-bridge.service
    log "Service installed but not started (--no-start)."
  fi

  maybe_setcap
}

install_user() {
  require_command systemctl
  ensure_build_deps

  log "Installing Python package for current user..."
  "${PYTHON_BIN}" -m pip install --user --upgrade "${REPO_ROOT}"

  local config_dir data_dir unit_dir config_path
  config_dir="${XDG_CONFIG_HOME:-${HOME}/.config}/dmx-bridge"
  data_dir="${XDG_DATA_HOME:-${HOME}/.local/share}/dmx-lan-bridge"
  unit_dir="${XDG_CONFIG_HOME:-${HOME}/.config}/systemd/user"
  config_path="${config_dir}/config.toml"

  install -d -m 755 "${config_dir}" "${data_dir}" "${unit_dir}"

  if [[ ! -f "${config_path}" ]]; then
    sed "s|/var/lib/dmx-bridge/bridge.sqlite3|${data_dir}/bridge.sqlite3|g" \
      "${CONFIG_TEMPLATE}" >"${config_path}"
    log "Wrote user config template to ${config_path}"
  else
    log "Existing user config ${config_path} preserved."
  fi

  # The old user unit has a different name and would otherwise remain active.
  systemctl --user disable --now dmx-bridge-user.service >/dev/null 2>&1 || true
  rm -f "${unit_dir}/dmx-bridge-user.service"

  log "Installing user systemd unit..."
  install -m 644 "${USER_UNIT}" "${unit_dir}/dmx-lan-bridge.service"
  systemctl --user daemon-reload

  if [[ "${START_SERVICE}" -eq 1 ]]; then
    systemctl --user enable --now dmx-lan-bridge.service
  else
    systemctl --user enable dmx-lan-bridge.service
    log "User service installed but not started (--no-start)."
  fi

  log "If you want this user service to start at boot, enable linger with:"
  log "  sudo loginctl enable-linger ${USER}"
}

uninstall_system() {
  require_root
  require_command systemctl

  systemctl stop dmx-lan-bridge.service 2>/dev/null || true
  systemctl disable dmx-lan-bridge.service 2>/dev/null || true
  rm -f /etc/systemd/system/dmx-lan-bridge.service
  systemctl daemon-reload

  "${PYTHON_BIN}" -m pip uninstall --break-system-packages -y dmx-lan-bridge || true
  log "System files removed. Configuration and data were left in place."
}

uninstall_user() {
  require_command systemctl

  systemctl --user stop dmx-lan-bridge.service 2>/dev/null || true
  systemctl --user disable dmx-lan-bridge.service 2>/dev/null || true
  rm -f "${XDG_CONFIG_HOME:-${HOME}/.config}/systemd/user/dmx-lan-bridge.service"
  systemctl --user daemon-reload

  "${PYTHON_BIN}" -m pip uninstall -y dmx-lan-bridge || true
  log "User files removed. Configuration and data were left in place."
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    install|uninstall)
      COMMAND="$1"
      ;;
    --system)
      MODE="system"
      ;;
    --user)
      MODE="user"
      ;;
    --no-start)
      START_SERVICE=0
      ;;
    --setcap)
      SETCAP=1
      ;;
    --python)
      shift
      PYTHON_BIN="$1"
      ;;
    -h|--help)
      usage
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage
      ;;
  esac
  shift
done

if [[ -z "${COMMAND}" || -z "${MODE}" ]]; then
  usage
fi

case "${COMMAND}:${MODE}" in
  install:system)
    install_system
    ;;
  install:user)
    install_user
    ;;
  uninstall:system)
    uninstall_system
    ;;
  uninstall:user)
    uninstall_user
    ;;
  *)
    usage
    ;;
esac
