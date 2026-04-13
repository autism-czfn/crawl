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
is_running() {
    if [[ -f "$PID_FILE" ]]; then
        local pid
        pid=$(cat "$PID_FILE")
        if kill -0 "$pid" 2>/dev/null; then
            return 0   # running
        fi
    fi
    return 1   # not running
}

stop_existing() {
    if is_running; then
        local pid
        pid=$(cat "$PID_FILE")
        warn "Stopping existing crawler process (PID $pid) ..."
        kill "$pid"
        # Wait up to 10 s for clean exit
        local i=0
        while kill -0 "$pid" 2>/dev/null && [[ $i -lt 10 ]]; do
            sleep 1; i=$((i + 1))
        done
        kill -0 "$pid" 2>/dev/null && kill -9 "$pid" || true
        rm -f "$PID_FILE"
        success "Stopped."
    fi
}

# ── Option 1 — Start / Restart (crawler + Django admin) ───────────────────────
start_restart_all() {
    echo
    check_env
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
        success "Django admin started  (PID $admin_pid)  →  http://localhost:${ADMIN_PORT}/admin/"
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
    if [[ -f "$ADMIN_PID_FILE" ]]; then
        local pid
        pid=$(cat "$ADMIN_PID_FILE")
        if kill -0 "$pid" 2>/dev/null; then
            return 0
        fi
    fi
    return 1
}

stop_existing_admin() {
    if is_admin_running; then
        local pid
        pid=$(cat "$ADMIN_PID_FILE")
        warn "Stopping existing Django admin process (PID $pid) ..."
        kill "$pid"
        local i=0
        while kill -0 "$pid" 2>/dev/null && [[ $i -lt 10 ]]; do
            sleep 1; i=$((i + 1))
        done
        kill -0 "$pid" 2>/dev/null && kill -9 "$pid" || true
        rm -f "$ADMIN_PID_FILE"
        success "Stopped."
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

# ── Option 2 — Status ──────────────────────────────────────────────────────────
check_status() {
    echo
    info "=== Service status ==="
    echo
    if is_running; then
        local pid
        pid=$(cat "$PID_FILE")
        success "Crawler is RUNNING  (PID $pid)"
        echo
        # Memory / CPU via ps
        ps -p "$pid" -o pid,pcpu,pmem,etime,cmd --no-headers 2>/dev/null \
            | awk '{printf "  PID: %s  CPU: %s%%  MEM: %s%%  Uptime: %s\n", $1,$2,$3,$4}' || true
    else
        warn "Crawler is NOT running."
    fi

    echo
    info "=== Last 30 log lines ($LOG_FILE) ==="
    echo
    if [[ -f "$LOG_FILE" ]]; then
        tail -30 "$LOG_FILE"
    else
        warn "No log file found yet."
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
    echo -e "  ${RED}0)${RESET} Exit"
    echo
    echo -n "  Select an option [0-7]: "
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
        0)
            echo
            info "Goodbye."
            echo
            exit 0
            ;;
        *)
            error "Invalid option '${choice}'. Please enter 0–7."
            ;;
    esac
done
