#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_DIR="${SCRIPT_DIR}/config"

# shellcheck disable=SC1091
source "${CONFIG_DIR}/common.env"

PG_CONFIG="${PG_CONFIG:-${CONFIG_DIR}/postgresql.external.env}"
REDIS_CONFIG="${REDIS_CONFIG:-${CONFIG_DIR}/redis.external.env}"
MONGO_CONFIG="${MONGO_CONFIG:-${CONFIG_DIR}/mongodb.external.env}"

log() {
  printf '[db-provision-template] %s\n' "$*"
}

die() {
  printf '[db-provision-template][error] %s\n' "$*" >&2
  exit 1
}

bool_true() {
  case "${1:-false}" in
    1|true|TRUE|yes|YES|on|ON) return 0 ;;
    *) return 1 ;;
  esac
}

require_file() {
  [[ -f "$1" ]] || die "Missing file: $1"
}

replace_env_value() {
  local file="$1" key="$2" value="$3"
  if grep -q "^${key}=" "$file"; then
    sed -i "s#^${key}=.*#${key}=${value}#g" "$file"
  else
    printf '%s=%s\n' "$key" "$value" >> "$file"
  fi
}

run_postgresql() {
  require_file "${PG_CONFIG}"
  "${DBCTL_BIN}" --config "${PG_CONFIG}" --target external --action provision

  # shellcheck disable=SC1090
  source "${PG_CONFIG}"
  export PGPASSWORD="${APP_DB_PASSWORD}"
  psql -h "${DB_HOST}" -p "${DB_PORT}" -U "${APP_DB_USER}" -d "${APP_DB_NAME}" -c "select current_user, current_database();" >/dev/null
  replace_env_value "${OUT_ENV}" "DATABASE_URL" "postgresql://${APP_DB_USER}:${APP_DB_PASSWORD}@${DB_HOST}:${DB_PORT}/${APP_DB_NAME}"
}

run_mongodb() {
  require_file "${MONGO_CONFIG}"
  "${DBCTL_BIN}" --config "${MONGO_CONFIG}" --target external --action provision

  # shellcheck disable=SC1090
  source "${MONGO_CONFIG}"
  if command -v mongosh >/dev/null 2>&1; then
    mongosh "mongodb://${APP_DB_USER}:${APP_DB_PASSWORD}@${DB_HOST}:${DB_PORT}/${APP_DB_NAME}?authSource=${APP_DB_NAME}" --quiet --eval "db.adminCommand({ ping: 1 }).ok" >/dev/null
  fi
  replace_env_value "${OUT_ENV}" "MONGODB_URI" "mongodb://${APP_DB_USER}:${APP_DB_PASSWORD}@${DB_HOST}:${DB_PORT}/${APP_DB_NAME}?authSource=${APP_DB_NAME}"
}

run_redis() {
  require_file "${REDIS_CONFIG}"
  # shellcheck disable=SC1090
  source "${REDIS_CONFIG}"
  python3 - <<PY
import socket
host='${REDIS_HOST}'
port=int('${REDIS_PORT}')
pwd='${REDIS_PASSWORD}'
def resp(*parts):
    s='*%d\\r\\n'%len(parts)
    for p in parts:
        p=str(p)
        s += '$%d\\r\\n%s\\r\\n'%(len(p.encode()),p)
    return s.encode()
with socket.create_connection((host,port),timeout=5) as c:
    c.sendall(resp('AUTH',pwd)); c.recv(256)
    c.sendall(resp('PING')); ans=c.recv(256).decode(errors='ignore')
    if '+PONG' not in ans:
        raise SystemExit('Redis ping failed: ' + ans)
PY
  replace_env_value "${OUT_ENV}" "REDIS_HOST" "${REDIS_HOST}"
  replace_env_value "${OUT_ENV}" "REDIS_PORT" "${REDIS_PORT}"
  replace_env_value "${OUT_ENV}" "REDIS_DB" "${REDIS_DB}"
  replace_env_value "${OUT_ENV}" "REDIS_PASSWORD" "${REDIS_PASSWORD}"
}

main() {
  [[ -x "${DBCTL_BIN}" ]] || die "DBCTL_BIN not executable: ${DBCTL_BIN}"
  bool_true "${STRICT_FILE_CHECK:-true}" && require_file "${BASE_ENV_TEMPLATE}"

  cp "${BASE_ENV_TEMPLATE}" "${OUT_ENV}"
  log "Base env copied: ${OUT_ENV}"

  bool_true "${ENABLE_POSTGRESQL:-false}" && run_postgresql
  bool_true "${ENABLE_MONGODB:-false}" && run_mongodb
  bool_true "${ENABLE_REDIS:-false}" && run_redis

  log "Provision complete. env ready: ${OUT_ENV}"
}

main "$@"
