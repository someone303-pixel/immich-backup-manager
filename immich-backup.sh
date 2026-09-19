#!/bin/bash
# Immich Backup Script v2
# Sichert Datenbank, Library-Daten, Konfiguration und Versions-Metadaten

set -euo pipefail

TIMESTAMP=$(date +"%Y-%m-%d_%H-%M")
BACKUP_ROOT="/mnt/backup/immich"
DB_BACKUP_DIR="$BACKUP_ROOT/database"
CONFIG_BACKUP_DIR="$BACKUP_ROOT/config"
LOG_FILE="/var/log/immich-backup.log"
IMMICH_DIR="/mnt/data/immich"
LIBRARY_BASE="$IMMICH_DIR/library"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}

# Prüfen ob Backup-SSD gemountet ist
if ! mountpoint -q /mnt/backup; then
    log "FEHLER: /mnt/backup ist nicht gemountet. Abbruch."
    exit 1
fi

mkdir -p "$DB_BACKUP_DIR" "$CONFIG_BACKUP_DIR"

# --- Immich-Version aus laufendem Container auslesen ---
log "Lese Immich-Version..."
IMMICH_VERSION="unknown"
if docker inspect immich_server &>/dev/null; then
    V=$(docker inspect immich_server --format='{{index .Config.Labels "org.opencontainers.image.version"}}' 2>/dev/null || true)
    if [ -n "$V" ] && [ "$V" != "<no value>" ]; then
        IMMICH_VERSION="$V"
    else
        # Fallback: image tag
        IMMICH_VERSION=$(docker inspect immich_server --format='{{.Config.Image}}' 2>/dev/null | sed 's/.*://' || echo "unknown")
    fi
fi
log "Immich-Version: $IMMICH_VERSION"

# --- Konfigurationsdateien sichern ---
log "Sichere Konfigurationsdateien..."
if [ -f "$IMMICH_DIR/docker-compose.yml" ]; then
    cp "$IMMICH_DIR/docker-compose.yml" "$CONFIG_BACKUP_DIR/docker-compose.yml"
fi
if [ -f "$IMMICH_DIR/.env" ]; then
    cp "$IMMICH_DIR/.env" "$CONFIG_BACKUP_DIR/.env"
fi

# --- Metadaten schreiben ---
META_FILE="$BACKUP_ROOT/meta.json"
cat > "$META_FILE" <<EOF
{
  "timestamp": "$(date -Iseconds)",
  "immich_version": "$IMMICH_VERSION",
  "backup_host": "$(hostname)",
  "backup_script_version": "2"
}
EOF
log "Metadaten geschrieben (Version: $IMMICH_VERSION)"

# --- Datenbank-Dump ---
log "Starte PostgreSQL-Dump..."
DUMP_FILE="$DB_BACKUP_DIR/immich_db_${TIMESTAMP}_v${IMMICH_VERSION}.dump"
docker exec immich_postgres pg_dump \
    -U postgres \
    -d immich \
    --format=custom \
    > "$DUMP_FILE"

# Alte DB-Dumps aufräumen (nur die letzten 7 behalten)
ls -tp "$DB_BACKUP_DIR"/*.dump 2>/dev/null | tail -n +8 | xargs -r rm --
log "Datenbank-Dump abgeschlossen: $(basename $DUMP_FILE)"

# --- Rsync der Library ---
log "Starte rsync für upload/..."
rsync -aHAX --numeric-ids --delete --info=progress2 \
    "$LIBRARY_BASE/upload/" \
    "$BACKUP_ROOT/upload/"

log "Starte rsync für library/..."
rsync -aHAX --numeric-ids --delete --info=progress2 \
    "$LIBRARY_BASE/library/" \
    "$BACKUP_ROOT/library/"

log "Starte rsync für profile/..."
rsync -aHAX --numeric-ids --delete --info=progress2 \
    "$LIBRARY_BASE/profile/" \
    "$BACKUP_ROOT/profile/"

log "Backup abgeschlossen. Version: $IMMICH_VERSION | Dump: $(basename $DUMP_FILE)"
