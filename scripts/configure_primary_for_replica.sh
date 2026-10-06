#!/usr/bin/env bash
# configure_primary_for_replica.sh
#
# Run this on the PRIMARY machine BEFORE setting up the replica.
#
# UPDATED 2026-10-03: this primary moved from 192.168.86.44 to this machine
# (192.168.86.20, PGDG packages — postgresql16-server 16.15, pgvector_16
# 0.8.7, service name "postgresql-16", PGDATA under /var/lib/pgsql/16/data)
# as part of a logical pg_dump/pg_restore migration (DATABASE_URL pointed
# at "crawler" now, not "autism_crawler"). 192.168.86.20 previously used to
# BE a replica itself (hence the old OLD_REPLICA_IP/NEW_REPLICA_IP variables
# this script used to have, from when 192.168.86.48 replaced it as replica)
# — that history no longer applies now that this machine is the primary;
# removed to avoid confusion.
#
# What it does:
#   1. Ensures wal_level=replica, max_wal_senders≥5, wal_keep_size in postgresql.conf
#   2. Creates a dedicated replication role (replicator) if missing
#   3. Adds the replica IP (192.168.86.48) to pg_hba.conf
#   4. Reloads PostgreSQL (no restart needed unless wal_level changed)
#
# Usage:
#   sudo bash configure_primary_for_replica.sh

set -euo pipefail

REPLICA_IP="192.168.86.48"
REPL_USER="replicator"
PG_SERVICE="postgresql-16"   # PGDG package service name on this primary — NOT the
                             # generic "postgresql" unit used by module-stream installs

# ── Colours ─────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'
info()    { echo -e "${CYAN}[INFO]${RESET}  $*"; }
success() { echo -e "${GREEN}[OK]${RESET}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${RESET}  $*"; }
error()   { echo -e "${RED}[ERROR]${RESET} $*"; exit 1; }

[[ $EUID -ne 0 ]] && error "Run as root: sudo bash $0"

# ── Locate postgresql.conf / pg_hba.conf ────────────────────────────────────
PGDATA=$(sudo -u postgres psql -tAc "SHOW data_directory;" 2>/dev/null) \
    || error "Cannot query PostgreSQL — is it running? Check: systemctl status $PG_SERVICE"
PG_CONF="$PGDATA/postgresql.conf"
HBA_CONF="$PGDATA/pg_hba.conf"
[[ -f "$PG_CONF" ]] || error "postgresql.conf not found at $PG_CONF"
[[ -f "$HBA_CONF" ]] || error "pg_hba.conf not found at $HBA_CONF"

info "PGDATA: $PGDATA"

# Config backups go OUTSIDE PGDATA, never inside it. A .bak file created by
# this root-run script inside PGDATA ends up root-owned; depending on umask
# it may not be readable by the postgres OS user — and pg_basebackup (which
# streams the ENTIRE PGDATA tree as postgres) then fails outright on that one
# unreadable file, wiping out whatever the replica had already received.
# BUG FOUND 2026-10-03 (live, during .48's basebackup from this exact script's
# earlier run): confirmed by "could not open file ./postgresql.conf.bak.<ts>:
# Permission denied" aborting pg_basebackup on the primary side.
BACKUP_DIR="/root/pg_conf_backups"
mkdir -p "$BACKUP_DIR"

# ── 1. postgresql.conf — replication settings ────────────────────────────────
echo
info "Checking replication settings in postgresql.conf ..."

_ensure_param() {
    local key="$1" val="$2"
    if grep -qE "^[[:space:]]*${key}[[:space:]]*=" "$PG_CONF"; then
        sed -i -E "s|^[[:space:]]*${key}[[:space:]]*=.*|${key} = ${val}|" "$PG_CONF"
    else
        echo "${key} = ${val}" >> "$PG_CONF"
    fi
    info "  $key = $val"
}

# Back up before touching
cp "$PG_CONF" "${BACKUP_DIR}/postgresql.conf.bak.$(date +%s)"

NEED_RESTART=false

current_wal_level=$(sudo -u postgres psql -tAc "SHOW wal_level;" 2>/dev/null | xargs)
if [[ "$current_wal_level" != "replica" && "$current_wal_level" != "logical" ]]; then
    warn "wal_level is '$current_wal_level' — changing to 'replica' (requires PostgreSQL restart)"
    _ensure_param "wal_level" "replica"
    NEED_RESTART=true
else
    success "wal_level already set to '$current_wal_level' — no restart needed for this setting."
fi

_ensure_param "max_wal_senders"  "10"
_ensure_param "wal_keep_size"    "512"   # MB; keeps at least 512 MB of WAL for replication lag
_ensure_param "hot_standby"      "on"    # allows read queries on standby

# ── 2. Replication role ───────────────────────────────────────────────────────
echo
info "Ensuring replication role '${REPL_USER}' exists ..."

REPL_PASS=""
if sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='${REPL_USER}';" | grep -q 1; then
    warn "Role '${REPL_USER}' already exists."
    read -rsp "  Enter NEW password for '${REPL_USER}' (press Enter to keep existing): " REPL_PASS
    echo
    if [[ -n "$REPL_PASS" ]]; then
        sudo -u postgres psql -c "ALTER ROLE ${REPL_USER} WITH REPLICATION LOGIN PASSWORD '${REPL_PASS}';"
        success "Password updated for ${REPL_USER}."
    else
        info "Keeping existing password."
        info "You will need the existing password when running setup_new_replica.sh."
    fi
else
    while [[ -z "$REPL_PASS" ]]; do
        read -rsp "  Set password for new replication role '${REPL_USER}': " REPL_PASS
        echo
    done
    sudo -u postgres psql -c "CREATE ROLE ${REPL_USER} WITH REPLICATION LOGIN PASSWORD '${REPL_PASS}';"
    success "Role '${REPL_USER}' created."
fi

# ── 3. pg_hba.conf — add replica ────────────────────────────────────────────
echo
info "Updating pg_hba.conf ..."
cp "$HBA_CONF" "${BACKUP_DIR}/pg_hba.conf.bak.$(date +%s)"

NEW_HBA_LINE="host    replication     ${REPL_USER}     ${REPLICA_IP}/32          scram-sha-256"

if grep -qF "${REPLICA_IP}" "$HBA_CONF"; then
    info "pg_hba.conf already has an entry for ${REPLICA_IP} — skipping add."
else
    echo "$NEW_HBA_LINE" >> "$HBA_CONF"
    success "Added replication entry for ${REPLICA_IP}."
fi

# ── 4. Also allow primary's listen_addresses to include non-localhost ────────
current_listen=$(sudo -u postgres psql -tAc "SHOW listen_addresses;" 2>/dev/null | xargs)
if [[ "$current_listen" == "localhost" || "$current_listen" == "127.0.0.1" ]]; then
    warn "listen_addresses is '$current_listen' — changing to '*' so replicas can connect."
    warn "This will require a PostgreSQL restart."
    _ensure_param "listen_addresses" "'*'"
    NEED_RESTART=true
else
    info "listen_addresses = '$current_listen' — replicas should be able to connect."
fi

# ── 5. Reload / restart ───────────────────────────────────────────────────────
echo
if [[ "$NEED_RESTART" == "true" ]]; then
    warn "A PostgreSQL RESTART is required for wal_level / listen_addresses changes."
    read -rp "  Restart PostgreSQL now? [Y/n] " yn
    yn="${yn:-Y}"
    if [[ "$yn" =~ ^[Yy]$ ]]; then
        systemctl restart "$PG_SERVICE"
        sleep 2
        systemctl is-active --quiet "$PG_SERVICE" && success "PostgreSQL restarted." \
            || error "PostgreSQL failed to start. Check: journalctl -u $PG_SERVICE -n 50"
    else
        warn "Skipping restart. Apply manually: sudo systemctl restart $PG_SERVICE"
        warn "The replica cannot connect until you restart."
    fi
else
    systemctl reload "$PG_SERVICE"
    sleep 1
    success "PostgreSQL reloaded (pg_hba.conf changes applied)."
fi

# ── 6. Summary ───────────────────────────────────────────────────────────────
PRIMARY_IP=$(ip route get "${REPLICA_IP}" 2>/dev/null | awk '/src/{for(i=1;i<=NF;i++) if($i=="src") print $(i+1); exit}')
[[ -z "$PRIMARY_IP" ]] && PRIMARY_IP=$(hostname -I | awk '{print $1}')

echo
echo -e "${BOLD}╔════════════════════════════════════════════════════════╗${RESET}"
echo -e "${BOLD}║  Primary configured. Next: run on 192.168.86.48         ║${RESET}"
echo -e "${BOLD}╚════════════════════════════════════════════════════════╝${RESET}"
echo
echo -e "  ${BOLD}Primary IP seen from replica:${RESET}  ${PRIMARY_IP}"
echo -e "  ${BOLD}Replication user:${RESET}              ${REPL_USER}"
echo -e "  ${BOLD}Replica IP:${RESET}                    ${REPLICA_IP}"
echo
echo -e "  Copy ${BOLD}setup_new_replica.sh${RESET} to 192.168.86.48 and run:"
echo -e "    ${CYAN}scp scripts/setup_new_replica.sh root@${REPLICA_IP}:~/${RESET}"
echo -e "    ${CYAN}ssh root@${REPLICA_IP} 'sudo bash ~/setup_new_replica.sh'${RESET}"
echo
echo -e "  When prompted, supply:"
echo -e "    Primary host : ${PRIMARY_IP}"
echo -e "    Repl user    : ${REPL_USER}"
echo -e "    Repl password: <the password you just set>"
echo
echo -e "  ${YELLOW}Note:${RESET} setup_new_replica.sh will REPLACE .48's current"
echo -e "  PostgreSQL 16.13 (module-stream) + pgvector 0.6.2 install with the"
echo -e "  same PGDG packages this primary runs (16.15 + pgvector_16 0.8.7),"
echo -e "  to avoid any pgvector on-disk-format mismatch during WAL replay."
echo
