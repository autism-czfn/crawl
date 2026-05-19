#!/usr/bin/env bash
# setup.sh — Autism Crawler service management (no Docker)

set -euo pipefail

# ── Colours ────────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
RESET='\033[0m'

# ── Helpers ────────────────────────────────────────────────────────────────────
info()    { echo -e "${CYAN}[INFO]${RESET}  $*"; }
success() { echo -e "${GREEN}[OK]${RESET}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${RESET}  $*"; }
error()   { echo -e "${RED}[ERROR]${RESET} $*"; }

# ── Paths ──────────────────────────────────────────────────────────────────────
cd "$(dirname "$0")"
PROJECT_DIR="$(pwd)"
PID_FILE="$PROJECT_DIR/.crawler.pid"
ADMIN_PID_FILE="$PROJECT_DIR/.admin.pid"
LOG_FILE="$PROJECT_DIR/crawler.log"
ADMIN_LOG_FILE="$PROJECT_DIR/admin.log"
ADMIN_PORT="${ADMIN_PORT:-8001}"
PYTHON="${VIRTUAL_ENV:+$VIRTUAL_ENV/bin/python}"
PYTHON="${PYTHON:-$(command -v python)}"

# ── Checks ─────────────────────────────────────────────────────────────────────
check_python() {
    if ! command -v python3 &>/dev/null; then
        error "python3 is not installed or not in PATH."
        exit 1
    fi
}

check_env() {
    if [[ ! -f .env ]]; then
        warn ".env file not found."
        if [[ -f .env.example ]]; then
            warn "Copying .env.example → .env  (edit it with your credentials before starting)"
            cp .env.example .env
        else
            error "No .env or .env.example found. Please create a .env file first."
            exit 1
        fi
    fi
}

# ── Dependencies ──────────────────────────────────────────────────────────────
install_deps() {
    info "Installing / updating dependencies from requirements.txt ..."
    pip install --quiet -r requirements.txt
    success "Dependencies installed."
}

# ── Public IP → .env ──────────────────────────────────────────────────────────
update_csrf_origins() {
    local public_ip
    public_ip=$(curl -sf --max-time 5 https://ifconfig.me \
             || curl -sf --max-time 5 https://api.ipify.org \
             || curl -sf --max-time 5 https://icanhazip.com \
             || true)

    if [[ -z "$public_ip" ]]; then
        warn "Could not detect public IP — DJANGO_CSRF_TRUSTED_ORIGINS left unchanged."
        return
    fi

    local origins="https://${public_ip},http://127.0.0.1,http://localhost"

    if grep -q "^DJANGO_CSRF_TRUSTED_ORIGINS=" .env; then
        sed -i "s|^DJANGO_CSRF_TRUSTED_ORIGINS=.*|DJANGO_CSRF_TRUSTED_ORIGINS=${origins}|" .env
    else
        echo "DJANGO_CSRF_TRUSTED_ORIGINS=${origins}" >> .env
    fi

    success "DJANGO_CSRF_TRUSTED_ORIGINS set to: ${origins}"
}

# ── Migrations ─────────────────────────────────────────────────────────────────
run_migrations() {
    info "Running database migrations ..."
    if alembic upgrade head; then
        success "Migrations applied."
    else
        warn "Migrations failed — check your DATABASE_URL in .env"
    fi
}

# ── PID helpers ────────────────────────────────────────────────────────────────
# Pattern used to identify the crawler process regardless of who started it.
# Must NOT start with "-" or pgrep will treat it as a flag.
CRAWLER_PGREP_PATTERN="src[.]main"
ADMIN_PGREP_PATTERN="admin_site/manage[.]py.*runserver"

# Find the crawler PID by looking for a Python process launched with "-m src.main".
# Matching " -m src.main" (with space, without a colon suffix) avoids false
# positives from uvicorn apps whose module path also contains "src.main:app".
_find_crawler_pid() {
    ps -eo pid,cmd --no-headers 2>/dev/null \
        | awk '$2 ~ /python/ && / -m src[.]main( |$)/ {print $1}' \
        | head -1
}

# Find the Django admin PID — always return the PARENT (reloader) process.
# runserver spawns two processes: a file-watcher parent and an HTTP-server child.
# Killing the parent takes down both; killing only the child causes the parent
# to immediately respawn it.  sort -n gives lowest PID = the parent.
_find_admin_pid() {
    ps -eo pid,cmd --no-headers 2>/dev/null \
        | awk '$2 ~ /python/ && /admin_site\/manage[.]py/ && /runserver/ {print $1}' \
        | sort -n \
        | head -1
}

is_running() {
    # 1. PID file exists — check the process is alive AND is actually the crawler.
    #    (The file may contain a stale PID from a shell wrapper, not a python process.)
    if [[ -f "$PID_FILE" ]]; then
        local pid
        pid=$(cat "$PID_FILE")
        if kill -0 "$pid" 2>/dev/null; then
            # Validate: the stored PID must be a python -m src.main process.
            if ps -p "$pid" -o cmd --no-headers 2>/dev/null \
               | awk '$1 ~ /python/ && / -m src[.]main( |$)/ {found=1} END {exit !found}'; then
                return 0   # running, PID file is valid
            fi
        fi
        # Dead or wrong process — discard the stale file and fall through.
        rm -f "$PID_FILE"
    fi

    # 2. Fallback: find by process scan (handles processes started by other
    #    parents, e.g. a Claude agent).  Adopt the PID so the rest of the
    #    script can manage it normally.
    local found_pid
    found_pid=$(_find_crawler_pid)
    if [[ -n "$found_pid" ]]; then
        echo "$found_pid" > "$PID_FILE"   # adopt externally-started process
        return 0
    fi

    return 1   # not running
}

stop_existing() {
    # Refresh / adopt PID before stopping.
    is_running || true

    if [[ -f "$PID_FILE" ]]; then
        local pid
        pid=$(cat "$PID_FILE")
        warn "Stopping existing crawler process (PID $pid) ..."
        kill "$pid" 2>/dev/null || true
        # Wait up to 10 s for clean exit.
        local i=0
        while kill -0 "$pid" 2>/dev/null && [[ $i -lt 10 ]]; do
            sleep 1; i=$((i + 1))
        done
        kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null || true
        rm -f "$PID_FILE"
        success "Stopped."
    fi

    # Kill any additional stragglers that share the same command pattern
    # (e.g. child worker processes spawned by the main process).
    local stragglers
    stragglers=$(pgrep -f "$CRAWLER_PGREP_PATTERN" 2>/dev/null || true)
    if [[ -n "$stragglers" ]]; then
        warn "Killing remaining crawler processes: $(echo "$stragglers" | tr '\n' ' ')"
        echo "$stragglers" | xargs kill 2>/dev/null || true
        sleep 2
        stragglers=$(pgrep -f "$CRAWLER_PGREP_PATTERN" 2>/dev/null || true)
        [[ -n "$stragglers" ]] && echo "$stragglers" | xargs kill -9 2>/dev/null || true
    fi
}

# ── Option 1 — Start / Restart (crawler + Django admin) ───────────────────────
start_restart_all() {
    echo
    check_env
    update_csrf_origins
    install_deps
    run_migrations

    # ── Crawler ──
    stop_existing

    info "Starting crawler in the background ..."
    info "Logs → $LOG_FILE"

    nohup "$PYTHON" -m src.main >> "$LOG_FILE" 2>&1 &
    local pid=$!
    echo "$pid" > "$PID_FILE"

    sleep 2
    if kill -0 "$pid" 2>/dev/null; then
        success "Crawler started  (PID $pid)"
    else
        error "Crawler exited immediately. Check logs:"
        tail -20 "$LOG_FILE"
    fi

    echo

    # ── Django admin ──
    stop_existing_admin

    info "Running Django admin migrations ..."
    "$PYTHON" admin_site/manage.py migrate --run-syncdb 2>&1 || \
        warn "Django migrations had warnings — check $ADMIN_LOG_FILE"

    info "Starting Django admin on port ${ADMIN_PORT} ..."
    info "Logs → $ADMIN_LOG_FILE"

    nohup "$PYTHON" admin_site/manage.py runserver "0.0.0.0:${ADMIN_PORT}" \
        >> "$ADMIN_LOG_FILE" 2>&1 &
    local admin_pid=$!
    echo "$admin_pid" > "$ADMIN_PID_FILE"

    sleep 2
    if kill -0 "$admin_pid" 2>/dev/null; then
        local public_ip
        public_ip=$(grep "^DJANGO_CSRF_TRUSTED_ORIGINS=" .env | grep -oP 'https://\K[^,]+' | head -1)
        success "Django admin started  (PID $admin_pid)  →  https://${public_ip}/admin/"
    else
        error "Django admin exited immediately. Check logs:"
        tail -20 "$ADMIN_LOG_FILE"
    fi
    echo
}

# ── Option 3 — Run migrations only ────────────────────────────────────────────
only_migrate() {
    echo
    check_env
    run_migrations
    info "Running Django admin migrations ..."
    if "$PYTHON" admin_site/manage.py migrate --run-syncdb 2>&1; then
        success "Django migrations applied."
    else
        warn "Django migrations failed."
    fi
    echo
}

# ── Django admin PID helpers ───────────────────────────────────────────────────
is_admin_running() {
    # 1. PID file exists — validate it's alive AND is actually the Django admin.
    if [[ -f "$ADMIN_PID_FILE" ]]; then
        local pid
        pid=$(cat "$ADMIN_PID_FILE")
        if kill -0 "$pid" 2>/dev/null; then
            if ps -p "$pid" -o cmd --no-headers 2>/dev/null \
               | awk '$1 ~ /python/ && /admin_site\/manage[.]py/ && /runserver/ {found=1} END {exit !found}'; then
                return 0
            fi
        fi
        rm -f "$ADMIN_PID_FILE"
    fi

    # 2. Process-scan fallback — adopt externally-started admin process.
    local found_pid
    found_pid=$(_find_admin_pid)
    if [[ -n "$found_pid" ]]; then
        echo "$found_pid" > "$ADMIN_PID_FILE"   # adopt externally-started process
        return 0
    fi

    return 1
}

stop_existing_admin() {
    # Refresh / adopt PID before stopping.
    is_admin_running || true

    if [[ -f "$ADMIN_PID_FILE" ]]; then
        local pid
        pid=$(cat "$ADMIN_PID_FILE")
        warn "Stopping existing Django admin process (PID $pid) ..."
        kill "$pid" 2>/dev/null || true
        local i=0
        while kill -0 "$pid" 2>/dev/null && [[ $i -lt 10 ]]; do
            sleep 1; i=$((i + 1))
        done
        kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null || true
        rm -f "$ADMIN_PID_FILE"
        success "Stopped."
    fi

    # Kill any stragglers.
    local stragglers
    stragglers=$(pgrep -f "$ADMIN_PGREP_PATTERN" 2>/dev/null || true)
    if [[ -n "$stragglers" ]]; then
        warn "Killing remaining Django admin processes: $(echo "$stragglers" | tr '\n' ' ')"
        echo "$stragglers" | xargs kill 2>/dev/null || true
        sleep 2
        stragglers=$(pgrep -f "$ADMIN_PGREP_PATTERN" 2>/dev/null || true)
        [[ -n "$stragglers" ]] && echo "$stragglers" | xargs kill -9 2>/dev/null || true
    fi
}

# ── Option 6 — Stop all services ──────────────────────────────────────────────
stop_all() {
    echo
    info "Stopping all services ..."
    echo

    if is_running; then
        stop_existing
    else
        warn "Crawler is not running — nothing to stop."
    fi

    echo

    if is_admin_running; then
        stop_existing_admin
    else
        warn "Django admin is not running — nothing to stop."
    fi

    echo
    success "All services stopped."
    echo
}

# ── Option 4 — Show admin URLs ─────────────────────────────────────────────────
show_urls() {
    echo
    info "=== Admin URLs ==="
    echo
    if is_admin_running; then
        local pid
        pid=$(cat "$ADMIN_PID_FILE")
        success "Django Admin is RUNNING  (PID $pid)"
    else
        warn "Django Admin is NOT running  (use option 1 to start it)"
    fi
    echo
    local internal_ip
    internal_ip=$(ip route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src") print $(i+1); exit}')
    if [[ -z "$internal_ip" ]]; then
        internal_ip=$(hostname -I 2>/dev/null | awk '{print $1}')
    fi
    if [[ -z "$internal_ip" ]]; then
        internal_ip="localhost"
        warn "Could not detect internal IP — falling back to localhost"
    fi
    echo -e "  ${BOLD}Django Admin:${RESET}  http://${internal_ip}:${ADMIN_PORT}/admin/"
    echo
    echo -e "  ${CYAN}[INFO]${RESET}  First time? Use option 5 to create a superuser."
    echo
}

# ── Option 5 — Create Django superuser ────────────────────────────────────────
create_superuser() {
    echo
    info "Creating Django admin superuser ..."
    echo
    "$PYTHON" admin_site/manage.py createsuperuser
    echo
}

# ── Option 7 — DB Stats ────────────────────────────────────────────────────────
show_db_stats() {
    echo
    info "=== Database & Crawl Stats ==="
    echo

    if [[ ! -f .env ]]; then
        error ".env not found — cannot determine DATABASE_URL."
        echo; return
    fi

    # Use the project's own Python/psycopg2 — no psql needed
    "$PYTHON" - << 'PYEOF'
import os, sys, json, pathlib

# ── Load .env ──
for line in pathlib.Path(".env").read_text().splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

db_url = os.environ.get("DATABASE_URL", "")
if not db_url:
    print("  \033[0;31m[ERROR]\033[0m  DATABASE_URL not set in .env")
    sys.exit(1)

# Strip SQLAlchemy driver prefix (e.g. postgresql+asyncpg:// → postgresql://)
import re
db_url = re.sub(r"^(postgresql)\+\w+://", r"\1://", db_url)

BOLD  = "\033[1m"
GREEN = "\033[0;32m"
WARN  = "\033[1;33m"
ERR   = "\033[0;31m"
RESET = "\033[0m"

# ── DB size & crawled items ──
db_size = item_count = "N/A"
try:
    import psycopg2
    conn = psycopg2.connect(db_url)
    cur  = conn.cursor()
    cur.execute("SELECT pg_size_pretty(pg_database_size(current_database()));")
    db_size = cur.fetchone()[0].strip()
    cur.execute("SELECT COUNT(*) FROM crawled_items;")
    item_count = f"{cur.fetchone()[0]:,}"
    cur.close()
    conn.close()
except Exception as e:
    print(f"  {WARN}[WARN]{RESET}   DB query failed: {e}")

# ── Active sources from surfaces.json ──
source_count = "N/A"
try:
    surfaces = json.loads(pathlib.Path("config/surfaces.json").read_text())
    source_count = sum(1 for s in surfaces if s.get("enabled", True))
except Exception as e:
    print(f"  {WARN}[WARN]{RESET}   Could not read surfaces.json: {e}")

print(f"  {BOLD}DB Size:       {RESET}{GREEN}{db_size}{RESET}")
print(f"  {BOLD}Crawled Items: {RESET}{GREEN}{item_count}{RESET}")
print(f"  {BOLD}Active Sources:{RESET}{GREEN}{source_count}{RESET}  (config/surfaces.json)")
print()
PYEOF
}

# ── Option 8 — Install & initialize PostgreSQL ────────────────────────────────
step() { echo -e "\n${BOLD}==▶ $*${RESET}"; }

_pg_parse_env_db_url() {
    # Populate PG_DB_* from DATABASE_URL_SYNC in .env if present.
    [[ -f "$PROJECT_DIR/.env" ]] || return 0
    local url
    url=$(grep -E '^DATABASE_URL_SYNC=' "$PROJECT_DIR/.env" | tail -1 | cut -d'=' -f2- | tr -d '"' | tr -d "'")
    [[ -z "$url" ]] && return 0

    local rest="${url#postgresql://}"
    rest="${rest#postgres://}"

    local creds="${rest%%@*}"
    local hostpart="${rest#*@}"

    local parsed_user="${creds%%:*}"
    local parsed_pass="${creds#*:}"

    # Ignore the .env.example placeholders — keep the built-in defaults instead.
    if [[ -n "$parsed_user" && "$parsed_user" != "user" ]]; then
        PG_DB_USER="$parsed_user"
    fi
    if [[ -n "$parsed_pass" && "$parsed_pass" != "pass" ]]; then
        PG_DB_PASS="$parsed_pass"
    fi

    local hostport="${hostpart%%/*}"
    local parsed_db="${hostpart#*/}"
    parsed_db="${parsed_db%%\?*}"
    [[ -n "$parsed_db" ]] && PG_DB_NAME="$parsed_db"

    local parsed_host="${hostport%%:*}"
    [[ -n "$parsed_host" ]] && PG_DB_HOST="$parsed_host"
    if [[ "$hostport" == *:* ]]; then
        PG_DB_PORT="${hostport#*:}"
    fi
}

_pg_prompt_credentials() {
    echo
    info "Enter DB credentials (these will be written to .env and applied to the DB role)."

    local default_user="${PG_DB_USER:-dbuser}"
    local input_user
    read -rp "  DB user [${default_user}]: " input_user
    PG_DB_USER="${input_user:-$default_user}"

    local default_db="${PG_DB_NAME:-autism_crawler}"
    local input_db
    read -rp "  DB name [${default_db}]: " input_db
    PG_DB_NAME="${input_db:-$default_db}"

    if [[ -n "${PG_DB_PASS}" && "${PG_DB_PASS}" != "pass" ]]; then
        local input_pass
        read -rsp "  Password for '${PG_DB_USER}' [press Enter to keep existing]: " input_pass
        echo
        [[ -n "$input_pass" ]] && PG_DB_PASS="$input_pass"
    else
        read -rsp "  Password for '${PG_DB_USER}': " PG_DB_PASS
        echo
        if [[ -z "$PG_DB_PASS" ]]; then
            error "Password cannot be empty."
            return 1
        fi
    fi
}

_pg_write_env() {
    local env_file="$PROJECT_DIR/.env"
    local async_url="postgresql+asyncpg://${PG_DB_USER}:${PG_DB_PASS}@${PG_DB_HOST}:${PG_DB_PORT}/${PG_DB_NAME}"
    local sync_url="postgresql://${PG_DB_USER}:${PG_DB_PASS}@${PG_DB_HOST}:${PG_DB_PORT}/${PG_DB_NAME}"

    if [[ ! -f "$env_file" ]]; then
        if [[ -f "$PROJECT_DIR/.env.example" ]]; then
            info "Creating .env from .env.example"
            cp "$PROJECT_DIR/.env.example" "$env_file"
        else
            info "Creating new .env"
            : > "$env_file"
        fi
    fi

    python3 - "$env_file" "$async_url" "$sync_url" <<'PYEOF'
import sys, pathlib
path = pathlib.Path(sys.argv[1])
async_url, sync_url = sys.argv[2], sys.argv[3]
lines = path.read_text().splitlines() if path.exists() else []
want = {"DATABASE_URL": async_url, "DATABASE_URL_SYNC": sync_url}
seen = set()
out = []
for line in lines:
    key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else None
    if key in want:
        out.append(f"{key}={want[key]}")
        seen.add(key)
    else:
        out.append(line)
for key, val in want.items():
    if key not in seen:
        out.append(f"{key}={val}")
path.write_text("\n".join(out) + "\n")
PYEOF

    chmod 600 "$env_file" 2>/dev/null || true
    success ".env updated with DATABASE_URL / DATABASE_URL_SYNC"
}

_pg_detect_os() {
    if [[ -f /etc/os-release ]]; then
        . /etc/os-release
        PG_OS_ID="${ID:-unknown}"
        PG_OS_LIKE="${ID_LIKE:-}"
    else
        PG_OS_ID="unknown"
        PG_OS_LIKE=""
    fi
}

_pg_is_installed() {
    # Package-level check (most reliable across distros)
    case "$PG_OS_ID" in
        rhel|centos|rocky|almalinux|fedora)
            rpm -q postgresql-server &>/dev/null && return 0
            ;;
        ubuntu|debian)
            dpkg -s postgresql &>/dev/null && return 0
            ;;
    esac
    # Fallbacks: systemd unit or server binary on disk
    [[ -f /usr/lib/systemd/system/postgresql.service ]] && return 0
    [[ -f /etc/systemd/system/postgresql.service ]] && return 0
    systemctl list-unit-files postgresql.service 2>/dev/null | grep -q '^postgresql\.service' && return 0
    [[ -x /usr/bin/postgres ]] && return 0
    return 1
}

_pg_rhel_enable_pg16_module() {
    # Fedora removed DNF modules; it ships PostgreSQL directly (usually already ≥16).
    if [[ "$PG_OS_ID" == "fedora" ]]; then
        return 0
    fi
    info "Enabling postgresql:16 module stream ..."
    $PG_SUDO dnf -qy module reset postgresql >/dev/null 2>&1 || true
    if ! $PG_SUDO dnf -qy module enable postgresql:16; then
        error "Failed to enable postgresql:16 module stream."
        return 1
    fi
    return 0
}

_pg_debian_add_pgdg_repo() {
    info "Configuring PGDG apt repository for PostgreSQL 16 ..."
    $PG_SUDO apt-get update || return 1
    $PG_SUDO DEBIAN_FRONTEND=noninteractive apt-get install -y \
        curl ca-certificates gnupg lsb-release || return 1

    local keyring="/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc"
    $PG_SUDO install -d /usr/share/postgresql-common/pgdg
    if [[ ! -f "$keyring" ]]; then
        curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
            | $PG_SUDO tee "$keyring" >/dev/null || return 1
    fi

    local codename
    codename=$(lsb_release -cs 2>/dev/null)
    if [[ -z "$codename" ]]; then
        error "Could not determine distro codename (lsb_release -cs)."
        return 1
    fi

    local line="deb [signed-by=${keyring}] https://apt.postgresql.org/pub/repos/apt ${codename}-pgdg main"
    echo "$line" | $PG_SUDO tee /etc/apt/sources.list.d/pgdg.list >/dev/null
    $PG_SUDO apt-get update || return 1
    return 0
}

_pg_pkg_install() {
    if _pg_is_installed; then
        info "PostgreSQL already installed — skipping package install."
        return 0
    fi
    case "$PG_OS_ID" in
        rhel|centos|rocky|almalinux|fedora)
            _pg_rhel_enable_pg16_module || return 1
            $PG_SUDO dnf install -y postgresql-server postgresql-contrib
            ;;
        ubuntu|debian)
            _pg_debian_add_pgdg_repo || return 1
            $PG_SUDO DEBIAN_FRONTEND=noninteractive apt-get install -y postgresql-16 postgresql-contrib-16
            ;;
        *)
            if [[ "$PG_OS_LIKE" == *rhel* || "$PG_OS_LIKE" == *fedora* ]]; then
                _pg_rhel_enable_pg16_module || return 1
                $PG_SUDO dnf install -y postgresql-server postgresql-contrib
            elif [[ "$PG_OS_LIKE" == *debian* ]]; then
                _pg_debian_add_pgdg_repo || return 1
                $PG_SUDO DEBIAN_FRONTEND=noninteractive apt-get install -y postgresql-16 postgresql-contrib-16
            else
                error "Unsupported OS: $PG_OS_ID ($PG_OS_LIKE). Install PostgreSQL 16 manually."
                return 1
            fi
            ;;
    esac
}

_pg_init_cluster() {
    case "$PG_OS_ID" in
        rhel|centos|rocky|almalinux|fedora)
            if [[ ! -f /var/lib/pgsql/data/PG_VERSION ]]; then
                info "Initializing PostgreSQL data directory ..."
                $PG_SUDO /usr/bin/postgresql-setup --initdb
            else
                info "PostgreSQL data directory already initialized — skipping initdb."
            fi
            ;;
        *)
            info "Cluster initialization handled by package manager on this distro."
            ;;
    esac
}

_pg_start_service() {
    $PG_SUDO systemctl enable --now postgresql
    sleep 1
    if systemctl is-active --quiet postgresql; then
        success "PostgreSQL service is active."
    else
        error "PostgreSQL failed to start. Check: systemctl status postgresql"
        return 1
    fi
}

_pg_psql_super() { $PG_SUDO -u postgres psql -v ON_ERROR_STOP=1 "$@"; }

_pg_create_role_and_db() {
    info "Ensuring password_encryption=scram-sha-256 (required for pg_hba.conf auth) ..."
    _pg_psql_super -c "ALTER SYSTEM SET password_encryption = 'scram-sha-256';" >/dev/null
    _pg_psql_super -c "SELECT pg_reload_conf();" >/dev/null

    info "Creating role '${PG_DB_USER}' (if missing) and database '${PG_DB_NAME}' ..."
    local esc_pass="${PG_DB_PASS//\'/\'\'}"

    _pg_psql_super <<SQL
SET password_encryption = 'scram-sha-256';
DO \$\$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '${PG_DB_USER}') THEN
        CREATE ROLE "${PG_DB_USER}" WITH LOGIN PASSWORD '${esc_pass}';
    ELSE
        ALTER ROLE "${PG_DB_USER}" WITH LOGIN PASSWORD '${esc_pass}';
    END IF;
END
\$\$;
SQL

    if _pg_psql_super -tAc "SELECT 1 FROM pg_database WHERE datname='${PG_DB_NAME}'" | grep -q 1; then
        info "Database '${PG_DB_NAME}' already exists."
    else
        _pg_psql_super -c "CREATE DATABASE \"${PG_DB_NAME}\" OWNER \"${PG_DB_USER}\";"
    fi

    _pg_psql_super -c "GRANT ALL PRIVILEGES ON DATABASE \"${PG_DB_NAME}\" TO \"${PG_DB_USER}\";"
    success "Role and database ready."
}

_pg_configure_hba() {
    case "$PG_OS_ID" in
        rhel|centos|rocky|almalinux|fedora) ;;
        *) return 0 ;;
    esac

    local hba="/var/lib/pgsql/data/pg_hba.conf"
    [[ -f "$hba" ]] || return 0

    if $PG_SUDO grep -qE '^[[:space:]]*host[[:space:]]+all[[:space:]]+all[[:space:]]+127\.0\.0\.1/32[[:space:]]+(md5|scram-sha-256)' "$hba"; then
        info "pg_hba.conf already allows password auth from localhost."
        return 0
    fi

    info "Enabling password auth for localhost in pg_hba.conf ..."
    $PG_SUDO cp "$hba" "${hba}.bak.$(date +%s)"
    $PG_SUDO sed -ri \
        -e 's|^(host[[:space:]]+all[[:space:]]+all[[:space:]]+127\.0\.0\.1/32[[:space:]]+).*|\1scram-sha-256|' \
        -e 's|^(host[[:space:]]+all[[:space:]]+all[[:space:]]+::1/128[[:space:]]+).*|\1scram-sha-256|' \
        "$hba"

    # Post-sed verification: confirm scram rule is actually present.
    if ! $PG_SUDO grep -qE '^[[:space:]]*host[[:space:]]+all[[:space:]]+all[[:space:]]+127\.0\.0\.1/32[[:space:]]+scram-sha-256' "$hba"; then
        error "pg_hba.conf update failed — 127.0.0.1/32 line is still not scram-sha-256."
        error "Edit $hba manually: change 'ident' to 'scram-sha-256' on the host all all 127.0.0.1/32 and ::1/128 lines, then: sudo systemctl reload postgresql"
        return 1
    fi

    $PG_SUDO systemctl reload postgresql
    success "pg_hba.conf updated and postgresql reloaded."
}

_pg_major_version() {
    local v=""
    if command -v postgres &>/dev/null; then
        v=$(postgres --version 2>/dev/null | awk '{print $NF}')
    elif command -v pg_config &>/dev/null; then
        v=$(pg_config --version 2>/dev/null | awk '{print $NF}')
    fi
    echo "${v%%.*}"
}

_pg_vector_installed() {
    # Extension files live in the server's sharedir. Check the common layouts.
    local major="$1"
    [[ -f "/usr/share/pgsql/extension/vector.control" ]] && return 0
    [[ -f "/usr/pgsql-${major}/share/extension/vector.control" ]] && return 0
    [[ -f "/usr/share/postgresql/${major}/extension/vector.control" ]] && return 0
    return 1
}

_pg_install_pgvector_from_source() {
    local major="$1"
    local src_dir="${PGVECTOR_SRC_DIR:-/tmp/pgvector}"
    local version="${PGVECTOR_VERSION:-v0.7.4}"

    info "Installing build dependencies ..."
    case "$PG_OS_ID" in
        rhel|centos|rocky|almalinux|fedora)
            # postgresql-server-devel provides /usr/lib64/pgsql/pgxs/... (required by pgvector's Makefile).
            # postgresql-devel provides pg_config + client headers.
            $PG_SUDO dnf install -y git make gcc redhat-rpm-config postgresql-devel postgresql-server-devel || {
                error "Failed to install build dependencies (git, make, gcc, postgresql-devel, postgresql-server-devel)."
                return 1
            }
            ;;
        ubuntu|debian)
            $PG_SUDO apt-get update && \
            $PG_SUDO apt-get install -y git make gcc "postgresql-server-dev-${major}" || {
                error "Failed to install build dependencies."
                return 1
            }
            ;;
        *)
            error "Don't know how to install build deps on '${PG_OS_ID}'. Install git/make/gcc/postgresql-devel manually."
            return 1
            ;;
    esac

    if ! command -v pg_config &>/dev/null; then
        error "pg_config still not on PATH after installing postgresql-devel — aborting source build."
        return 1
    fi

    info "Fetching pgvector ${version} into ${src_dir} ..."
    if [[ -d "$src_dir/.git" ]]; then
        ( cd "$src_dir" && git fetch --tags --depth 1 origin "$version" && git checkout "$version" ) || {
            error "Failed to update existing pgvector checkout at ${src_dir}."
            return 1
        }
    else
        rm -rf "$src_dir"
        git clone --depth 1 --branch "$version" https://github.com/pgvector/pgvector.git "$src_dir" || {
            error "git clone failed."
            return 1
        }
    fi

    info "Building pgvector ..."
    ( cd "$src_dir" && make ) || { error "pgvector make failed."; return 1; }

    info "Installing pgvector (sudo make install) ..."
    ( cd "$src_dir" && $PG_SUDO make install ) || { error "pgvector make install failed."; return 1; }

    if _pg_vector_installed "$major"; then
        success "pgvector built and installed from source."
        return 0
    fi
    warn "Build completed but vector.control not found in expected paths."
    return 1
}

_pg_install_pgvector() {
    local major
    major=$(_pg_major_version)
    if [[ -z "$major" ]]; then
        warn "Could not detect PostgreSQL major version — skipping pgvector install."
        return 0
    fi

    if _pg_vector_installed "$major"; then
        info "pgvector extension files already present for PostgreSQL ${major}."
        return 0
    fi

    info "Attempting package install of pgvector for PostgreSQL ${major} ..."
    case "$PG_OS_ID" in
        rhel|centos|rocky|almalinux|fedora)
            if $PG_SUDO dnf install -y "pgvector_${major}" 2>/dev/null; then
                success "pgvector_${major} installed."
                return 0
            elif $PG_SUDO dnf install -y pgvector 2>/dev/null; then
                success "pgvector installed."
                return 0
            fi
            ;;
        ubuntu|debian)
            if $PG_SUDO apt-get install -y "postgresql-${major}-pgvector" 2>/dev/null; then
                success "postgresql-${major}-pgvector installed."
                return 0
            fi
            ;;
    esac

    warn "pgvector package not available from configured repos."
    case "$PG_OS_ID" in
        rhel|centos|rocky|almalinux|fedora)
            info "On AlmaLinux/RHEL 9, native pgvector ships ONLY in the modular 'postgresql:16' stream."
            info "If you are on an older major (e.g. base-repo postgresql-13), pgxs.mk is not packaged"
            info "and source builds will fail. Recommended fix: migrate to the postgresql:16 module:"
            info "    sudo systemctl stop postgresql"
            info "    sudo mv /var/lib/pgsql/data /var/lib/pgsql/data.bak.\$(date +%s)"
            info "    sudo dnf remove -y postgresql-server postgresql-contrib postgresql"
            info "    sudo dnf module reset postgresql -y && sudo dnf module enable postgresql:16 -y"
            info "    sudo dnf install -y postgresql-server postgresql-contrib pgvector"
            info "    sudo /usr/bin/postgresql-setup --initdb"
            info "    sudo systemctl enable --now postgresql"
            info "Then re-run option 8."
            ;;
    esac
    echo
    local answer="${PGVECTOR_BUILD_FROM_SOURCE:-}"
    if [[ -z "$answer" ]]; then
        read -rp "  Build pgvector from source (git + make)? [y/N] " answer
    fi
    if [[ "$answer" =~ ^[Yy]$ || "$answer" == "1" ]]; then
        _pg_install_pgvector_from_source "$major" || return 1
    else
        info "Skipping pgvector install. To build later, re-run option 8 and answer 'y',"
        info "or set PGVECTOR_BUILD_FROM_SOURCE=1 before running setup.sh."
        return 1
    fi
}

_pg_create_extension_vector() {
    info "Enabling 'vector' extension in '${PG_DB_NAME}' ..."
    if _pg_psql_super -d "$PG_DB_NAME" -c "CREATE EXTENSION IF NOT EXISTS vector;" >/dev/null 2>&1; then
        success "vector extension enabled."
    else
        warn "Failed to enable 'vector' extension — pgvector package may be missing."
    fi
}

_pg_verify_connection() {
    info "Verifying connection as '${PG_DB_USER}' ..."
    local err
    err=$(PGPASSWORD="$PG_DB_PASS" psql -h "$PG_DB_HOST" -p "$PG_DB_PORT" -U "$PG_DB_USER" -d "$PG_DB_NAME" -c '\q' 2>&1)
    if [[ $? -eq 0 ]]; then
        success "Connection OK — ${PG_DB_USER}@${PG_DB_HOST}:${PG_DB_PORT}/${PG_DB_NAME}"
        return 0
    fi
    error "Could not connect as '${PG_DB_USER}':"
    echo "  $err"
    if [[ "$err" == *"Ident authentication"* ]]; then
        error "pg_hba.conf still uses 'ident' for localhost. Check:"
        error "  sudo grep -E '^host.*(127\\.0\\.0\\.1|::1)' /var/lib/pgsql/data/pg_hba.conf"
        error "Both lines must end in 'scram-sha-256', not 'ident'."
    elif [[ "$err" == *"password authentication"* ]]; then
        error ".env password and postgres role password are out of sync."
        error "Re-run option 8 and type the password you want to use — it will ALTER ROLE and rewrite .env in one go."
    fi
    return 1
}

install_postgres() {
    echo

    # Defaults (overridden by .env if present)
    PG_DB_NAME="autism_crawler"
    PG_DB_USER="dbuser"
    PG_DB_PASS=""
    PG_DB_HOST="localhost"
    PG_DB_PORT="5432"

    PG_SUDO=""
    if [[ $EUID -ne 0 ]]; then
        if command -v sudo &>/dev/null; then
            PG_SUDO="sudo"
        else
            error "This option needs root privileges (or sudo) to install PostgreSQL."
            echo; return
        fi
    fi

    step "Detecting OS"
    _pg_detect_os
    info "OS: ${PG_OS_ID} (like: ${PG_OS_LIKE:-n/a})"

    step "Loading existing DB settings from .env (if present)"
    _pg_parse_env_db_url
    info "  DB_NAME=${PG_DB_NAME}"
    info "  DB_USER=${PG_DB_USER}"
    info "  DB_HOST=${PG_DB_HOST}"
    info "  DB_PORT=${PG_DB_PORT}"

    _pg_prompt_credentials || { echo; return; }

    if _pg_is_installed; then
        info "PostgreSQL already installed — skipping install, init, and service start."
        local existing_major
        existing_major=$(_pg_major_version)
        if [[ -n "$existing_major" && "$existing_major" != "16" ]]; then
            error "Existing PostgreSQL is major version ${existing_major}, but this project requires 16."
            error "Remove the existing cluster first, then re-run this option."
            case "$PG_OS_ID" in
                rhel|centos|rocky|almalinux|fedora)
                    error "  sudo systemctl stop postgresql"
                    error "  sudo dnf remove -y postgresql-server postgresql-contrib postgresql"
                    error "  sudo rm -rf /var/lib/pgsql/data"
                    ;;
                ubuntu|debian)
                    error "  sudo systemctl stop postgresql"
                    error "  sudo apt-get purge -y 'postgresql-*'"
                    error "  sudo rm -rf /var/lib/postgresql /etc/postgresql"
                    ;;
            esac
            echo; return
        fi
        if ! systemctl is-active --quiet postgresql; then
            warn "postgresql service is not active. Start it first: 'sudo systemctl start postgresql'"
            echo; return
        fi
    else
        step "Installing PostgreSQL packages"
        _pg_pkg_install || { error "Package install failed."; echo; return; }

        step "Initializing cluster"
        _pg_init_cluster

        step "Starting PostgreSQL service"
        _pg_start_service || { echo; return; }
    fi

    step "Configuring host-based auth"
    _pg_configure_hba

    step "Creating role and database"
    _pg_create_role_and_db

    step "Installing pgvector extension"
    _pg_install_pgvector || warn "pgvector install failed — migrations that CREATE EXTENSION vector will fail until this is resolved."

    step "Enabling vector extension in database"
    _pg_create_extension_vector

    step "Writing credentials to .env"
    _pg_write_env

    step "Verifying connection"
    _pg_verify_connection

    echo
    success "PostgreSQL is ready and .env is in sync."
    info "Next: use option 3 to apply Alembic + Django migrations."
    echo
}

# ── Option 2 — Status ──────────────────────────────────────────────────────────
check_status() {
    echo
    info "=== Service status ==="
    echo

    # ── Crawler ──
    # is_running() adopts an externally-started process if found via pgrep.
    if is_running; then
        local pid
        pid=$(cat "$PID_FILE")
        local cmd
        cmd=$(ps -p "$pid" -o cmd --no-headers 2>/dev/null | head -1 || echo "n/a")
        success "Crawler     UP   │ PID $pid │ $cmd"
    else
        warn    "Crawler     DOWN"
    fi

    # ── Django admin ──
    if is_admin_running; then
        local apid
        apid=$(cat "$ADMIN_PID_FILE")
        local acmd
        acmd=$(ps -p "$apid" -o cmd --no-headers 2>/dev/null | head -1 || echo "n/a")
        success "Django admin UP   │ PID $apid │ $acmd"
    else
        warn    "Django admin DOWN"
    fi

    echo
}

# ── Menu ───────────────────────────────────────────────────────────────────────
print_menu() {
    echo
    echo -e "${BOLD}╔══════════════════════════════════════╗${RESET}"
    echo -e "${BOLD}║     Autism Crawler — Setup Menu      ║${RESET}"
    echo -e "${BOLD}╚══════════════════════════════════════╝${RESET}"
    echo
    echo -e "  ${GREEN}1)${RESET} Start / Restart all  (crawler + Django admin)"
    echo -e "  ${CYAN}2)${RESET} Check service status"
    echo -e "  ${YELLOW}3)${RESET} Run database migrations"
    echo -e "  ${CYAN}4)${RESET} Show admin URLs"
    echo -e "  ${YELLOW}5)${RESET} Create Django superuser"
    echo -e "  ${RED}6)${RESET} Stop all services     (crawler + Django admin)"
    echo -e "  ${CYAN}7)${RESET} Show DB stats         (size / crawled items / sources)"
    echo -e "  ${GREEN}8)${RESET} Install & initialize PostgreSQL"
    echo -e "  ${RED}0)${RESET} Exit"
    echo
    echo -n "  Select an option [0-8]: "
}

# ── Entry point ────────────────────────────────────────────────────────────────
check_python

while true; do
    print_menu
    read -r choice

    case "$choice" in
        1) start_restart_all ;;
        2) check_status      ;;
        3) only_migrate      ;;
        4) show_urls         ;;
        5) create_superuser  ;;
        6) stop_all          ;;
        7) show_db_stats     ;;
        8) install_postgres  ;;
        0)
            echo
            info "Goodbye."
            echo
            exit 0
            ;;
        *)
            error "Invalid option '${choice}'. Please enter 0–8."
            ;;
    esac
done
