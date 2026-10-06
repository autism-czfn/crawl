#!/usr/bin/env bash
# setup_new_replica.sh
#
# Run on the AlmaLinux replica machine (192.168.86.48).
#
# UPDATED 2026-10-03: the primary moved from 192.168.86.44 to 192.168.86.20,
# a PGDG-package install (postgresql16-server 16.15, pgvector_16 0.8.7) —
# different from the AlmaLinux "postgresql:16" module-stream install this
# machine (.48) currently has (postgresql-server 16.13, pgvector 0.6.2,
# module_el9 build). That version gap (especially pgvector 0.6.2 → 0.8.7) is
# too large to trust for WAL-level physical replication of pgvector's HNSW
# index pages — a mismatch there could silently corrupt reads rather than
# fail loudly. So this script now ALSO removes the old module-stream
# PostgreSQL/pgvector and installs the exact same PGDG packages as the
# primary, instead of just re-pointing PRIMARY_HOST. This is on top of the
# base backup already being a full wipe-and-reclone (not incremental), so
# there is no separate data-loss step introduced by also switching packages.
#
# What it does:
#   1. Removes the old module-stream PostgreSQL + pgvector (version mismatch)
#   2. Installs PostgreSQL 16 + pgvector from the PGDG repo (same stack as the primary)
#   3. Stops PostgreSQL and wipes the empty data directory
#   4. Pulls a base backup from the primary via pg_basebackup
#   5. Writes postgresql.conf standby settings + creates standby.signal
#   6. Starts PostgreSQL in hot-standby (read-only) mode
#   7. Verifies replication lag is near zero
#
# Pre-requisites (done on primary first):
#   • configure_primary_for_replica.sh has been run on the primary
#   • Port 5432 is reachable: primary → this machine (pg_hba), this machine → primary (firewall)
#
# Usage:
#   sudo bash setup_new_replica.sh

set -euo pipefail

# ── PostgreSQL version (must match primary exactly) ───────────────────────────
PG_MAJOR="16"
PG_VERSION="16.15"          # exact version running on primary (192.168.86.20)
PG_SERVICE="postgresql-16"  # PGDG service unit name (NOT the generic "postgresql"
                            # unit the old module-stream install used)
PGDG_REPO_RPM="https://download.postgresql.org/pub/repos/yum/reporpms/EL-9-x86_64/pgdg-redhat-repo-latest.noarch.rpm"

# ── Configuration (edit if your environment differs) ──────────────────────────
PRIMARY_HOST=""          # filled interactively if blank
PRIMARY_PORT="5432"
REPL_USER="replicator"
REPL_PASS=""             # filled interactively if blank
REPLICA_IP="192.168.86.48"

# ── Colours ─────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'
info()    { echo -e "${CYAN}[INFO]${RESET}  $*"; }
success() { echo -e "${GREEN}[OK]${RESET}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${RESET}  $*"; }
error()   { echo -e "${RED}[ERROR]${RESET} $*"; exit 1; }
step()    { echo -e "\n${BOLD}==▶ $*${RESET}"; }

[[ $EUID -ne 0 ]] && error "Run as root: sudo bash $0"

# ── Gather inputs ─────────────────────────────────────────────────────────────
echo
echo -e "${BOLD}╔══════════════════════════════════════════════════════════╗${RESET}"
echo -e "${BOLD}║   Autism Crawler — New Read Replica Setup (AlmaLinux)    ║${RESET}"
echo -e "${BOLD}╚══════════════════════════════════════════════════════════╝${RESET}"
echo

if [[ -z "$PRIMARY_HOST" ]]; then
    read -rp "  Primary host IP (e.g. 192.168.86.10): " PRIMARY_HOST
    [[ -z "$PRIMARY_HOST" ]] && error "Primary host cannot be empty."
fi

if [[ -z "$REPL_PASS" ]]; then
    read -rsp "  Password for replication user '${REPL_USER}': " REPL_PASS
    echo
    [[ -z "$REPL_PASS" ]] && error "Replication password cannot be empty."
fi

# ── Step 1: OS detection ──────────────────────────────────────────────────────
step "Detecting OS"
if [[ -f /etc/os-release ]]; then
    . /etc/os-release
    OS_ID="${ID:-unknown}"
    OS_LIKE="${ID_LIKE:-}"
else
    OS_ID="unknown"; OS_LIKE=""
fi
info "OS: ${OS_ID} (like: ${OS_LIKE:-n/a})"

case "$OS_ID" in
    almalinux|rocky|rhel|centos|fedora) : ;;
    *)
        if [[ "$OS_LIKE" == *rhel* || "$OS_LIKE" == *fedora* ]]; then
            OS_ID="almalinux"
        else
            error "This script targets AlmaLinux / RHEL-family. Detected: $OS_ID"
        fi
        ;;
esac

# ── Step 2: Remove mismatched module-stream PostgreSQL, install PGDG 16.15 ───
step "Checking existing PostgreSQL installation"

PGDG_INSTALLED_VER=$(rpm -q --qf '%{VERSION}' postgresql16-server 2>/dev/null || echo "")
if [[ "$PGDG_INSTALLED_VER" == "${PG_VERSION}"* ]]; then
    success "PGDG postgresql16-server ${PGDG_INSTALLED_VER} already installed (matches primary) — skipping install."
else
    if rpm -q postgresql-server &>/dev/null; then
        OLD_VER=$(rpm -q --qf '%{VERSION}' postgresql-server 2>/dev/null || echo "unknown")
        warn "Found module-stream postgresql-server ${OLD_VER} (primary runs PGDG ${PG_VERSION}) — removing it."
        systemctl stop postgresql 2>/dev/null || true
        systemctl disable postgresql 2>/dev/null || true
        dnf remove -y postgresql-server postgresql-contrib postgresql postgresql-private-libs pgvector 2>/dev/null || true
        rm -rf /var/lib/pgsql/data
        dnf -qy module reset postgresql >/dev/null 2>&1 || true
        success "Module-stream PostgreSQL removed."
    fi

    step "Installing PostgreSQL ${PG_VERSION} from the PGDG repo"
    if ! rpm -q pgdg-redhat-repo &>/dev/null; then
        info "Installing PGDG repo RPM ..."
        dnf install -y "$PGDG_REPO_RPM" || error "Failed to install PGDG repo RPM ($PGDG_REPO_RPM)."
    fi
    # The AppStream "postgresql" module can shadow PGDG's postgresql16-* packages
    # if left enabled — disable it so dnf resolves to the PGDG packages.
    dnf -qy module disable postgresql >/dev/null 2>&1 || true

    info "Installing postgresql16-server postgresql16-contrib postgresql16-libs ..."
    dnf install -y postgresql16-server postgresql16-contrib postgresql16-libs \
        || error "dnf install of PGDG postgresql16 packages failed."
    success "PostgreSQL $(/usr/pgsql-${PG_MAJOR}/bin/postgres --version | awk '{print $NF}') installed from PGDG."

    # Lock package version to prevent accidental upgrade diverging from primary
    if dnf install -y dnf-plugin-versionlock 2>/dev/null; then
        dnf versionlock add postgresql16-server postgresql16 postgresql16-contrib postgresql16-libs 2>/dev/null || true
        info "postgresql16 packages version-locked to prevent unintended upgrades."
    fi
fi

# ── Step 3: Install pgvector ─────────────────────────────────────────────────
step "Installing pgvector"

_vector_present() {
    [[ -f "/usr/share/pgsql/extension/vector.control" ]] && return 0
    [[ -f "/usr/pgsql-${PG_MAJOR}/share/extension/vector.control" ]] && return 0
    return 1
}

if _vector_present; then
    success "pgvector already present."
else
    info "Trying package install of pgvector_${PG_MAJOR} (PGDG — matches primary's 0.8.7) ..."
    if ! dnf install -y "pgvector_${PG_MAJOR}" 2>/dev/null; then
        warn "pgvector_${PG_MAJOR} package not found — building v0.8.7 from source to match the primary exactly ..."
        dnf install -y git make gcc redhat-rpm-config postgresql16-devel \
            || error "Could not install build dependencies for pgvector."
        PGVECTOR_SRC="/tmp/pgvector"
        PGVECTOR_TAG="v0.8.7"   # must match the primary's installed pgvector_16 version
        rm -rf "$PGVECTOR_SRC"
        git clone --depth 1 --branch "$PGVECTOR_TAG" \
            https://github.com/pgvector/pgvector.git "$PGVECTOR_SRC" \
            || error "git clone of pgvector failed."
        ( cd "$PGVECTOR_SRC" && PATH="/usr/pgsql-${PG_MAJOR}/bin:$PATH" make && PATH="/usr/pgsql-${PG_MAJOR}/bin:$PATH" make install ) \
            || error "pgvector build/install failed."
        _vector_present || error "pgvector built but vector.control not found."
    fi
    success "pgvector installed."
fi

# ── Step 4: Determine PGDATA ─────────────────────────────────────────────────
step "Locating data directory"

# PGDG package path (matches the primary at /var/lib/pgsql/16/data) — NOT the
# module-stream default of /var/lib/pgsql/data.
PGDATA="/var/lib/pgsql/${PG_MAJOR}/data"
info "PGDATA: $PGDATA"

# ── Step 5: Stop PostgreSQL + clear data directory ───────────────────────────
step "Preparing data directory for base backup"

if systemctl is-active --quiet "$PG_SERVICE" 2>/dev/null; then
    warn "Stopping PostgreSQL ..."
    systemctl stop "$PG_SERVICE"
fi

# Detect whether a previous pg_basebackup completed successfully.
# global/pg_control is the last file written by pg_basebackup; its presence
# means the data directory is intact and the backup step can be safely skipped.
SKIP_BASEBACKUP=false
if [[ -f "$PGDATA/global/pg_control" ]]; then
    echo
    warn "PGDATA already contains a completed base backup ($PGDATA/global/pg_control exists)."
    warn "Skipping pg_basebackup saves time when only a later step failed on a previous run."
    read -rp "  Re-use existing data and skip pg_basebackup? [Y/n] " yn
    yn="${yn:-Y}"
    if [[ "$yn" =~ ^[Yy]$ ]]; then
        SKIP_BASEBACKUP=true
        success "Skipping pg_basebackup — re-using existing data directory."
    else
        info "Re-running pg_basebackup as requested."
    fi
fi

if [[ "$SKIP_BASEBACKUP" == "false" ]]; then
    if [[ -d "$PGDATA" && "$(ls -A "$PGDATA" 2>/dev/null)" ]]; then
        warn "PGDATA $PGDATA is not empty."
        read -rp "  Wipe it and replace with primary's data? This is irreversible. [y/N] " yn
        [[ "$yn" =~ ^[Yy]$ ]] || error "Aborted. Clear $PGDATA manually and re-run."
        rm -rf "${PGDATA:?}"/*
        success "Data directory cleared."
    else
        mkdir -p "$PGDATA"
        chown postgres:postgres "$PGDATA"
        chmod 700 "$PGDATA"
        success "Data directory ready (was empty)."
    fi

    # ── Step 6: pg_basebackup from primary ───────────────────────────────────
    step "Running pg_basebackup from primary ${PRIMARY_HOST}"

    info "This may take several minutes depending on database size ..."
    info "If prompted for a password, enter the replication user '${REPL_USER}' password again."
    sudo -u postgres env PGPASSWORD="$REPL_PASS" pg_basebackup \
        --host="$PRIMARY_HOST" \
        --port="$PRIMARY_PORT" \
        --username="$REPL_USER" \
        --pgdata="$PGDATA" \
        --wal-method=stream \
        --checkpoint=fast \
        --progress \
        --verbose \
        || error "pg_basebackup failed. Check:
  • Primary pg_hba.conf allows ${REPLICA_IP}/32 for replication
  • configure_primary_for_replica.sh was run on the primary
  • Firewall: primary port ${PRIMARY_PORT} is reachable from this machine
    (test: nc -zv ${PRIMARY_HOST} ${PRIMARY_PORT})"

    chown -R postgres:postgres "$PGDATA"
    chmod 700 "$PGDATA"
    success "Base backup complete."
fi

# ── Step 7: Configure standby ────────────────────────────────────────────────
step "Configuring standby mode"

PG_CONF="$PGDATA/postgresql.conf"
REPL_CONNINFO="host=${PRIMARY_HOST} port=${PRIMARY_PORT} user=${REPL_USER} password=${REPL_PASS} sslmode=prefer"

# Write / overwrite standby-specific settings at the bottom of postgresql.conf
# (appended block so they override anything pg_basebackup copied from primary)
cat >> "$PG_CONF" <<EOF

# ── Standby settings added by setup_new_replica.sh ──────────────────────────
hot_standby          = on           # allow read-only queries on this standby
hot_standby_feedback = on           # prevent primary from vacuuming rows needed here
primary_conninfo     = '${REPL_CONNINFO}'
recovery_target_timeline = 'latest' # follow primary timeline switches
EOF

# pg_hba.conf: allow local app connections (same credentials as primary)
HBA="$PGDATA/pg_hba.conf"
if ! grep -qE '^host[[:space:]]+all[[:space:]]+all[[:space:]]+127\.0\.0\.1/32[[:space:]]+scram-sha-256' "$HBA"; then
    info "Patching pg_hba.conf for local password auth ..."
    sed -i -E \
        -e 's|^(host[[:space:]]+all[[:space:]]+all[[:space:]]+127\.0\.0\.1/32[[:space:]]+).*|\1scram-sha-256|' \
        -e 's|^(host[[:space:]]+all[[:space:]]+all[[:space:]]+::1/128[[:space:]]+).*|\1scram-sha-256|' \
        "$HBA"
fi

# Allow connections from the local network (so your app server can reach this replica)
LOCAL_NET=$(ip route show dev "$(ip route get "${PRIMARY_HOST}" | awk '/dev/{for(i=1;i<=NF;i++) if($i=="dev") print $(i+1); exit}')" 2>/dev/null | awk 'NR==1{print $1}' || echo "")
if [[ -n "$LOCAL_NET" ]] && ! grep -qF "$LOCAL_NET" "$HBA"; then
    echo "host    all             all             ${LOCAL_NET}         scram-sha-256" >> "$HBA"
    info "Added pg_hba.conf entry for local network ${LOCAL_NET}."
fi

# Create standby.signal (PG 12+: replaces recovery.conf)
touch "$PGDATA/standby.signal"
chown postgres:postgres "$PGDATA/standby.signal"
success "standby.signal created."

# ── Step 8: Firewall ──────────────────────────────────────────────────────────
step "Configuring firewall"
if command -v firewall-cmd &>/dev/null; then
    firewall-cmd --permanent --add-port=5432/tcp --zone=public >/dev/null 2>&1 && \
    firewall-cmd --reload >/dev/null 2>&1 && \
    success "firewalld: port 5432 opened." || \
    warn "firewall-cmd failed — open port 5432 manually if needed."
else
    warn "firewall-cmd not found. Ensure port 5432 is reachable if you use iptables."
fi

# ── Step 9: Start standby ────────────────────────────────────────────────────
step "Starting PostgreSQL standby"
systemctl enable --now "$PG_SERVICE"
sleep 3

if systemctl is-active --quiet "$PG_SERVICE"; then
    success "PostgreSQL standby is running."
else
    error "PostgreSQL failed to start. Check:
  journalctl -u $PG_SERVICE -n 50
  tail -50 ${PGDATA}/log/postgresql-*.log"
fi

# ── Step 10: Verify replication ───────────────────────────────────────────────
step "Verifying standby status"
sleep 2

IS_STANDBY=$(sudo -u postgres psql -tAc "SELECT pg_is_in_recovery();" 2>/dev/null | xargs)
if [[ "$IS_STANDBY" == "t" ]]; then
    success "This node is in recovery mode (read-only standby) ✓"
else
    warn "pg_is_in_recovery() returned '${IS_STANDBY}' — standby.signal may not have been picked up."
    warn "Check logs: journalctl -u $PG_SERVICE -n 50"
fi

LAG=$(sudo -u postgres psql -tAc "
    SELECT CASE
        WHEN pg_last_wal_receive_lsn() = pg_last_wal_replay_lsn() THEN '0 bytes (caught up)'
        ELSE pg_size_pretty(pg_wal_lsn_diff(pg_last_wal_receive_lsn(), pg_last_wal_replay_lsn()))
    END;" 2>/dev/null | xargs || echo "unknown")

info "Replication lag: ${LAG}"

# ── Summary ───────────────────────────────────────────────────────────────────
echo
echo -e "${BOLD}╔══════════════════════════════════════════════════════════╗${RESET}"
echo -e "${BOLD}║              Replica setup complete                      ║${RESET}"
echo -e "${BOLD}╚══════════════════════════════════════════════════════════╝${RESET}"
echo
echo -e "  ${BOLD}Replica IP:${RESET}      ${REPLICA_IP}"
echo -e "  ${BOLD}Primary:${RESET}         ${PRIMARY_HOST}:${PRIMARY_PORT}"
echo -e "  ${BOLD}Mode:${RESET}            hot standby (read-only)"
echo -e "  ${BOLD}Replication lag:${RESET} ${LAG}"
echo
echo -e "  ${BOLD}Verify on primary:${RESET}"
echo -e "    ${CYAN}sudo -u postgres psql -c \"SELECT client_addr, state, sent_lsn, replay_lsn, sync_state FROM pg_stat_replication;\"${RESET}"
echo
echo -e "  ${BOLD}Update your app's .env to point reads at this replica:${RESET}"
echo -e "    ${CYAN}DATABASE_URL_REPLICA=postgresql://dbuser:<pass>@${REPLICA_IP}:5432/crawler${RESET}"
echo -e "    (database is named 'crawler' now, not 'autism_crawler' — renamed during"
echo -e "     the migration to the new primary at ${PRIMARY_HOST})"
echo
echo -e "  ${BOLD}Once this replica is verified caught up:${RESET}"
echo -e "    The OLD primary (192.168.86.44) is the one to decommission now, not"
echo -e "    192.168.86.20 — .20 is the new primary this replica is now following."
echo -e "    On 192.168.86.44, once nothing else still points at it:"
echo -e "    ${CYAN}sudo systemctl stop postgresql && sudo systemctl disable postgresql${RESET}"
echo -e "    (check 192.168.86.44's actual service name first — it may differ from"
echo -e "     the generic 'postgresql' unit, same as this script had to account for here)"
echo

