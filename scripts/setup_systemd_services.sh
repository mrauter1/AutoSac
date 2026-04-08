#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  sudo bash scripts/setup_systemd_services.sh [options]

Options:
  --service-user USER   Linux user that should run the services
  --repo-dir PATH       AutoSac repository root
  --venv-dir PATH       Virtual environment directory
  --env-file PATH       Environment file to load into systemd
  -h, --help            Show this help text

Defaults:
  service user: the non-root user that invoked sudo
  repo dir:     repository root inferred from this script location
  venv dir:     <repo-dir>/.venv
  env file:     <repo-dir>/.env
EOF
}

die() {
  printf '%s\n' "$*" >&2
  exit 1
}

resolve_dir() {
  local path="$1"
  [[ -d "$path" ]] || die "Directory does not exist: $path"
  (
    cd "$path"
    pwd
  )
}

resolve_file() {
  local path="$1"
  [[ -f "$path" ]] || die "File does not exist: $path"
  local dir
  dir="$(dirname "$path")"
  local base
  base="$(basename "$path")"
  dir="$(resolve_dir "$dir")"
  printf '%s/%s\n' "$dir" "$base"
}

SCRIPT_DIR="$(
  cd "$(dirname "${BASH_SOURCE[0]}")"
  pwd
)"
DEFAULT_REPO_DIR="$(
  cd "${SCRIPT_DIR}/.."
  pwd
)"

SERVICE_USER="${AUTOSAC_SERVICE_USER:-${SUDO_USER:-}}"
REPO_DIR="${AUTOSAC_REPO_DIR:-$DEFAULT_REPO_DIR}"
VENV_DIR="${AUTOSAC_VENV_DIR:-${REPO_DIR}/.venv}"
ENV_FILE="${AUTOSAC_ENV_FILE:-${REPO_DIR}/.env}"

while (($#)); do
  case "$1" in
    --service-user)
      shift
      (($#)) || die "--service-user requires a value"
      SERVICE_USER="$1"
      ;;
    --repo-dir)
      shift
      (($#)) || die "--repo-dir requires a value"
      REPO_DIR="$1"
      ;;
    --venv-dir)
      shift
      (($#)) || die "--venv-dir requires a value"
      VENV_DIR="$1"
      ;;
    --env-file)
      shift
      (($#)) || die "--env-file requires a value"
      ENV_FILE="$1"
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "Unknown argument: $1"
      ;;
  esac
  shift
done

[[ "$EUID" -eq 0 ]] || die "Run this script as root, for example: sudo bash scripts/setup_systemd_services.sh"
command -v systemctl >/dev/null 2>&1 || die "systemctl is required"

[[ -n "$SERVICE_USER" ]] || die "Could not determine a service user. Pass --service-user explicitly."
[[ "$SERVICE_USER" != "root" ]] || die "Refusing to install AutoSac services as root. Pass --service-user explicitly."
id "$SERVICE_USER" >/dev/null 2>&1 || die "Linux user does not exist: $SERVICE_USER"

REPO_DIR="$(resolve_dir "$REPO_DIR")"
VENV_DIR="$(resolve_dir "$VENV_DIR")"
ENV_FILE="$(resolve_file "$ENV_FILE")"

PYTHON_BIN="${VENV_DIR}/bin/python"
[[ -x "$PYTHON_BIN" ]] || die "Python executable not found in virtualenv: $PYTHON_BIN"
[[ -f "${REPO_DIR}/scripts/run_web.py" ]] || die "Missing script: ${REPO_DIR}/scripts/run_web.py"
[[ -f "${REPO_DIR}/scripts/run_worker.py" ]] || die "Missing script: ${REPO_DIR}/scripts/run_worker.py"

SERVICE_GROUP="$(id -gn "$SERVICE_USER")"
SERVICE_HOME="$(getent passwd "$SERVICE_USER" | cut -d: -f6)"
[[ -n "$SERVICE_HOME" ]] || die "Could not determine HOME for user: $SERVICE_USER"
[[ -d "$SERVICE_HOME" ]] || die "Service user HOME does not exist: $SERVICE_HOME"

CODEX_BIN_LINE="$(grep -E '^CODEX_BIN=' "$ENV_FILE" | tail -n 1 || true)"
if [[ "$CODEX_BIN_LINE" == "CODEX_BIN=codex" ]]; then
  if ! env -i PATH="/usr/local/bin:/usr/bin:/bin" HOME="$SERVICE_HOME" su -s /bin/bash "$SERVICE_USER" -c 'command -v codex >/dev/null 2>&1'; then
    printf '%s\n' "Warning: CODEX_BIN is set to 'codex', but that binary was not found for ${SERVICE_USER} in the systemd PATH." >&2
    printf '%s\n' "Set CODEX_BIN in ${ENV_FILE} to the absolute path from 'command -v codex' if the service fails to start." >&2
  fi
fi

WEB_UNIT_PATH="/etc/systemd/system/autosac-web.service"
WORKER_UNIT_PATH="/etc/systemd/system/autosac-worker.service"

write_unit() {
  local unit_path="$1"
  local description="$2"
  local exec_script="$3"
  local syslog_id="$4"

  cat >"$unit_path" <<EOF
[Unit]
Description=${description}
After=network-online.target postgresql.service
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
Group=${SERVICE_GROUP}
WorkingDirectory=${REPO_DIR}
Environment=HOME=${SERVICE_HOME}
Environment=PATH=${SERVICE_HOME}/.local/bin:/usr/local/bin:/usr/bin:/bin
Environment=PYTHONUNBUFFERED=1
EnvironmentFile=${ENV_FILE}
ExecStart=${PYTHON_BIN} ${exec_script}
Restart=always
RestartSec=5
SyslogIdentifier=${syslog_id}

[Install]
WantedBy=multi-user.target
EOF
}

write_unit "$WEB_UNIT_PATH" "AutoSac web service" "scripts/run_web.py" "autosac-web"
write_unit "$WORKER_UNIT_PATH" "AutoSac worker service" "scripts/run_worker.py" "autosac-worker"

chmod 0644 "$WEB_UNIT_PATH" "$WORKER_UNIT_PATH"

systemctl daemon-reload
systemctl enable --now autosac-web autosac-worker
systemctl restart autosac-web autosac-worker

printf '%s\n' "Installed and started autosac-web.service and autosac-worker.service"
printf '%s\n' "Check status with: systemctl status autosac-web autosac-worker --no-pager"
