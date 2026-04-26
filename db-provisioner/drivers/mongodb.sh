#!/usr/bin/env bash

mongo_validate() {
  require_non_empty "APP_DB_NAME" "${APP_DB_NAME:-}"
  require_non_empty "APP_DB_USER" "${APP_DB_USER:-}"
  require_non_empty "APP_DB_PASSWORD" "${APP_DB_PASSWORD:-}"

  if [[ -z "${MONGO_ADMIN_URI:-}" ]]; then
    require_non_empty "DB_HOST" "${DB_HOST:-}"
    require_non_empty "DB_PORT" "${DB_PORT:-}"
    require_non_empty "MONGO_ADMIN_USER" "${MONGO_ADMIN_USER:-}"
    require_non_empty "MONGO_ADMIN_PASSWORD" "${MONGO_ADMIN_PASSWORD:-}"
  fi
}

mongo_admin_uri() {
  if [[ -n "${MONGO_ADMIN_URI:-}" ]]; then
    printf '%s' "${MONGO_ADMIN_URI}"
    return
  fi

  local auth_db="${MONGO_AUTH_DB:-admin}"
  local tls_qs=""
  if bool_true "${DB_TLS_ENABLED:-false}"; then
    tls_qs="&tls=true"
  fi
  printf 'mongodb://%s:%s@%s:%s/%s?authSource=%s%s' \
    "${MONGO_ADMIN_USER}" "${MONGO_ADMIN_PASSWORD}" "${DB_HOST}" "${DB_PORT}" "${auth_db}" "${auth_db}" "${tls_qs}"
}

mongo_provision() {
  if bool_true "${DRY_RUN:-false}"; then
    local admin_uri_dry
    admin_uri_dry="$(mongo_admin_uri)"
    log "Provision MongoDB: db=${APP_DB_NAME}, user=${APP_DB_USER}"
    log "DRY_RUN=true, skip executing mongosh"
    log "Admin URI (redacted): $(printf '%s' "${admin_uri_dry}" | sed -E "s#(mongodb://[^:]+:)[^@]+#\\1****#")"
    return 0
  fi

  require_cmd "mongosh"
  wait_k8s_pods_ready
  local admin_uri
  admin_uri="$(mongo_admin_uri)"
  mongo_precheck "${admin_uri}"

  log "Provision MongoDB: db=${APP_DB_NAME}, user=${APP_DB_USER}"

  APP_DB_URI=""
  APP_DB_URI="$(mongosh "${admin_uri}" --quiet --eval "
const dbName='${APP_DB_NAME}';
const username='${APP_DB_USER}';
const password='${APP_DB_PASSWORD}';
const appDb=db.getSiblingDB(dbName);
const existing=appDb.getUser(username);
if (existing) {
  appDb.updateUser(username, {
    pwd: password,
    roles: [
      { role: 'readWrite', db: dbName },
      { role: 'dbAdmin', db: dbName }
    ]
  });
  print('[mongo] updated existing user: ' + username);
} else {
  appDb.createUser({
    user: username,
    pwd: password,
    roles: [
      { role: 'readWrite', db: dbName },
      { role: 'dbAdmin', db: dbName }
    ]
  });
  print('[mongo] created user: ' + username);
}
try {
  appDb.createCollection('_init_marker');
} catch (e) {}
print('mongodb://' + encodeURIComponent(username) + ':' + encodeURIComponent(password) + '@${DB_HOST:-localhost}:${DB_PORT:-27017}/' + dbName + '?authSource=' + dbName);
" | tail -n 1)"

  require_non_empty "APP_DB_URI(mongo)" "${APP_DB_URI}"
}

mongo_deprovision() {
  if bool_true "${DRY_RUN:-false}"; then
    local admin_uri_dry
    admin_uri_dry="$(mongo_admin_uri)"
    log "Deprovision MongoDB: db=${APP_DB_NAME}, user=${APP_DB_USER}"
    log "DRY_RUN=true, skip executing mongosh"
    log "Admin URI (redacted): $(printf '%s' "${admin_uri_dry}" | sed -E "s#(mongodb://[^:]+:)[^@]+#\\1****#")"
    APP_DB_URI=""
    return 0
  fi

  require_cmd "mongosh"
  wait_k8s_pods_ready
  local admin_uri
  admin_uri="$(mongo_admin_uri)"
  mongo_precheck "${admin_uri}"

  log "Deprovision MongoDB: db=${APP_DB_NAME}, user=${APP_DB_USER}"
  mongosh "${admin_uri}" --quiet --eval "
const dbName='${APP_DB_NAME}';
const username='${APP_DB_USER}';
const dropDb='${DEPROVISION_DROP_DATABASE:-false}' === 'true';
const appDb=db.getSiblingDB(dbName);
const existing=appDb.getUser(username);
if (existing) {
  appDb.dropUser(username);
  print('[mongo] dropped user: ' + username);
} else {
  print('[mongo] user not found, skip: ' + username);
}
if (dropDb) {
  appDb.dropDatabase();
  print('[mongo] dropped database: ' + dbName);
}
"
  APP_DB_URI=""
}

mongo_precheck() {
  local admin_uri="$1"
  local timeout="${DB_PRECHECK_TIMEOUT_SECONDS:-60}"
  local interval="${DB_PRECHECK_INTERVAL_SECONDS:-3}"

  if ! precheck_enabled; then
    log "MongoDB precheck disabled"
    return 0
  fi

  log "MongoDB precheck: waiting for readiness (timeout=${timeout}s, interval=${interval}s)"
  if wait_until "${timeout}" "${interval}" mongosh "${admin_uri}" --quiet --eval "db.adminCommand({ ping: 1 }).ok" >/dev/null 2>&1; then
    log "MongoDB precheck passed"
    return 0
  fi

  die "MongoDB precheck failed: service not ready or unreachable"
}
