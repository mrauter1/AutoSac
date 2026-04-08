#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  bash scripts/setup_postgres_local.sh [--database-url URL] [--check-only]

Behavior:
  - Validates that DATABASE_URL targets a localhost PostgreSQL instance
  - Installs PostgreSQL locally when missing (Debian/Ubuntu via apt-get, macOS via Homebrew)
  - Starts the local PostgreSQL service
  - Creates or updates the database role/password from DATABASE_URL
  - Creates the target database if needed
  - Verifies the configured DATABASE_URL can connect

Notes:
  - Linux setup may require sudo privileges
  - --check-only prints a plan and exits without installing or changing state
EOF
}

die() {
  printf '%s\n' "$*" >&2
  exit 1
}

have_cmd() {
  command -v "$1" >/dev/null 2>&1
}

DATABASE_URL="${DATABASE_URL:-}"
CHECK_ONLY=0

while (($#)); do
  case "$1" in
    --database-url)
      shift
      (($#)) || die "--database-url requires a value"
      DATABASE_URL="$1"
      ;;
    --check-only)
      CHECK_ONLY=1
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

[[ -n "$DATABASE_URL" ]] || die "DATABASE_URL is required"

eval "$(
  python3 - "$DATABASE_URL" <<'PY'
import shlex
import sys
from urllib.parse import quote, unquote, urlparse

database_url = sys.argv[1].strip()
parsed = urlparse(database_url)
scheme = parsed.scheme.lower()
host = (parsed.hostname or "").lower()
if not scheme.startswith("postgresql"):
    raise SystemExit("DATABASE_URL must use a PostgreSQL scheme")
if host not in {"", "localhost", "127.0.0.1", "::1"}:
    raise SystemExit("setup_postgres_local.sh only supports localhost PostgreSQL URLs")

user = unquote(parsed.username or "")
password = unquote(parsed.password or "")
database = parsed.path.lstrip("/")
port = parsed.port or 5432
safe_host = host or "localhost"

if not user:
    raise SystemExit("DATABASE_URL must include a database user")
if not password:
    raise SystemExit("DATABASE_URL must include a database password")
if not database:
    raise SystemExit("DATABASE_URL must include a database name")

values = {
    "DB_HOST": safe_host,
    "DB_PORT": str(port),
    "DB_USER": user,
    "DB_PASSWORD": password,
    "DB_NAME": database,
    "PSQL_DATABASE_URL": f"postgresql://{quote(user)}:{quote(password)}@{safe_host}:{port}/{quote(database)}",
}
for key, value in values.items():
    print(f"{key}={shlex.quote(value)}")
PY
)"

PLATFORM="$(uname -s)"
DISTRO_ID=""
DISTRO_LIKE=""
PACKAGE_MANAGER="unsupported"
SERVICE_MANAGER="unsupported"

if [[ "$PLATFORM" == "Linux" ]]; then
  if [[ -f /etc/os-release ]]; then
    # shellcheck disable=SC1091
    source /etc/os-release
    DISTRO_ID="${ID:-}"
    DISTRO_LIKE="${ID_LIKE:-}"
  fi
  if have_cmd apt-get; then
    PACKAGE_MANAGER="apt-get"
  elif have_cmd dnf; then
    PACKAGE_MANAGER="dnf"
  elif have_cmd yum; then
    PACKAGE_MANAGER="yum"
  fi

  if have_cmd pg_ctlcluster; then
    SERVICE_MANAGER="pg_ctlcluster"
  elif have_cmd systemctl; then
    SERVICE_MANAGER="systemctl"
  elif have_cmd service; then
    SERVICE_MANAGER="service"
  fi
elif [[ "$PLATFORM" == "Darwin" ]]; then
  PACKAGE_MANAGER="brew"
  if have_cmd brew; then
    SERVICE_MANAGER="brew-services"
  fi
fi

print_check_only_summary() {
  python3 - <<PY
import json

summary = {
    "script": "setup_postgres_local.sh",
    "status": "ok" if "${PACKAGE_MANAGER}" != "unsupported" else "unsupported",
    "mode": "check_only",
    "platform": "${PLATFORM}",
    "database_host": "${DB_HOST}",
    "database_port": int("${DB_PORT}"),
    "database_name": "${DB_NAME}",
    "database_user": "${DB_USER}",
    "package_manager": "${PACKAGE_MANAGER}",
    "service_manager": "${SERVICE_MANAGER}",
    "psql_installed": ${have_psql_py},
    "pg_isready_installed": ${have_pg_isready_py},
}
print(json.dumps(summary))
PY
}

have_psql=false
have_pg_isready=false
if have_cmd psql; then
  have_psql=true
fi
if have_cmd pg_isready; then
  have_pg_isready=true
fi
have_psql_py="False"
have_pg_isready_py="False"
if [[ "$have_psql" == "true" ]]; then
  have_psql_py="True"
fi
if [[ "$have_pg_isready" == "true" ]]; then
  have_pg_isready_py="True"
fi

if [[ "$CHECK_ONLY" -eq 1 ]]; then
  print_check_only_summary
  [[ "$PACKAGE_MANAGER" != "unsupported" ]] || exit 1
  exit 0
fi

ensure_supported_platform() {
  [[ "$PACKAGE_MANAGER" != "unsupported" ]] || die "Unsupported platform for automatic PostgreSQL installation"
}

install_postgres_if_missing() {
  if have_cmd psql && have_cmd pg_isready; then
    return
  fi

  ensure_supported_platform
  if [[ "$PACKAGE_MANAGER" == "apt-get" ]]; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update
    apt-get install -y postgresql postgresql-contrib
  elif [[ "$PACKAGE_MANAGER" == "dnf" ]]; then
    dnf install -y postgresql-server postgresql-contrib
  elif [[ "$PACKAGE_MANAGER" == "yum" ]]; then
    yum install -y postgresql-server postgresql-contrib
  elif [[ "$PACKAGE_MANAGER" == "brew" ]]; then
    if ! brew list --versions postgresql@16 >/dev/null 2>&1 && ! brew list --versions postgresql >/dev/null 2>&1; then
      brew install postgresql@16 || brew install postgresql
    fi
  fi
}

postgres_ready() {
  have_cmd pg_isready || return 1
  pg_isready -h "$DB_HOST" -p "$DB_PORT" >/dev/null 2>&1
}

start_postgres_service() {
  postgres_ready && return

  if [[ "$SERVICE_MANAGER" == "pg_ctlcluster" ]] && have_cmd pg_lsclusters; then
    while read -r version cluster _rest; do
      [[ -n "$version" && -n "$cluster" ]] || continue
      pg_ctlcluster "$version" "$cluster" start || true
    done < <(pg_lsclusters --no-header || true)
  elif [[ "$SERVICE_MANAGER" == "systemctl" ]]; then
    systemctl start postgresql || true
  elif [[ "$SERVICE_MANAGER" == "service" ]]; then
    service postgresql start || true
  elif [[ "$SERVICE_MANAGER" == "brew-services" ]]; then
    if brew list --versions postgresql@16 >/dev/null 2>&1; then
      brew services start postgresql@16 || true
    else
      brew services start postgresql || true
    fi
    if ! postgres_ready && have_cmd initdb && have_cmd pg_ctl; then
      : "${PGDATA:=$HOME/.local/share/postgresql/data}"
      mkdir -p "$(dirname "$PGDATA")"
      if [[ ! -f "$PGDATA/PG_VERSION" ]]; then
        initdb -D "$PGDATA"
      fi
      pg_ctl -D "$PGDATA" -l "$PGDATA/server.log" start || true
    fi
  fi

  for _ in $(seq 1 20); do
    postgres_ready && return
    sleep 1
  done
  die "PostgreSQL service did not become ready on ${DB_HOST}:${DB_PORT}"
}

run_superuser_psql() {
  if [[ "$PLATFORM" == "Darwin" ]]; then
    psql -v ON_ERROR_STOP=1 postgres "$@"
    return
  fi
  if [[ "$(id -un)" == "postgres" ]]; then
    psql -v ON_ERROR_STOP=1 postgres "$@"
    return
  fi
  if have_cmd sudo; then
    sudo -u postgres psql -v ON_ERROR_STOP=1 postgres "$@"
    return
  fi
  die "Need sudo or direct postgres user access to provision the local PostgreSQL role and database"
}

provision_role_and_database() {
  run_superuser_psql --set=db_user="$DB_USER" --set=db_password="$DB_PASSWORD" <<'SQL'
SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', :'db_user', :'db_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'db_user') \gexec
SELECT format('ALTER ROLE %I LOGIN PASSWORD %L', :'db_user', :'db_password') \gexec
SQL

  run_superuser_psql --set=db_name="$DB_NAME" --set=db_user="$DB_USER" <<'SQL'
SELECT format('CREATE DATABASE %I OWNER %I', :'db_name', :'db_user')
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = :'db_name') \gexec
SELECT format('ALTER DATABASE %I OWNER TO %I', :'db_name', :'db_user')
WHERE EXISTS (SELECT 1 FROM pg_database WHERE datname = :'db_name') \gexec
SQL

  PGPASSWORD="$DB_PASSWORD" psql "$PSQL_DATABASE_URL" -v ON_ERROR_STOP=1 -c "SELECT 1" >/dev/null
}

install_postgres_if_missing
start_postgres_service
provision_role_and_database

python3 - <<PY
import json

print(json.dumps({
    "script": "setup_postgres_local.sh",
    "status": "ok",
    "database_host": "${DB_HOST}",
    "database_port": int("${DB_PORT}"),
    "database_name": "${DB_NAME}",
    "database_user": "${DB_USER}",
}))
PY
