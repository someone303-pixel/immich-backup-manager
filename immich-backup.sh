#!/bin/bash
# Immich Backup Script v3
# Sichert Datenbank, Library-Daten, Konfiguration und Versions-Metadaten

set -euo pipefail

TIMESTAMP=$(date +"%Y-%m-%d_%H-%M")
BACKUP_ROOT="/mnt/backup/immich"
DB_BACKUP_DIR="$BACKUP_ROOT/database"
CONFIG_BACKUP_DIR="$BACKUP_ROOT/config"
LOG_FILE="/var/log/immich-backup.log"
# --- Dynamische Pfad-Ermittlung ---
# 1. Parameter prüfen, falls durch app.py/CLI übergeben ($1 = Compose-Dir, $2 = Library-Base)
IMMICH_DIR="${1:-}"
LIBRARY_BASE="${2:-}"

# 2. Falls nicht übergeben: Arbeitsverzeichnis direkt aus laufendem Container auslesen
if [ -z "$IMMICH_DIR" ] && docker inspect immich_server &>/dev/null; then
    DETECTED_DIR=$(docker inspect immich_server --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' 2>/dev/null || true)
    if [ -n "$DETECTED_DIR" ] && [ "$DETECTED_DIR" != "<no value>" ] && [ -d "$DETECTED_DIR" ]; then
        IMMICH_DIR="$DETECTED_DIR"
    fi
fi

# 3. Fallbacks setzen, falls Container aus ist und kein Parameter übergeben wurde
IMMICH_DIR="${IMMICH_DIR:-/mnt/data/immich}"
if [ -z "$LIBRARY_BASE" ]; then
    LIBRARY_BASE="$IMMICH_DIR/library"
fi

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

# --- Konfigurationsdateien sichern (Auto-Inspect + Fallbacks) ---
log "Sichere Konfigurationsdateien..."
COMPOSE_FOUND=""

# Stufe 1: Live-Inspektion des laufenden Containers
if docker inspect immich_server &>/dev/null; then
    LABEL_FILES=$(docker inspect immich_server --format='{{index .Config.Labels "com.docker.compose.project.config_files"}}' 2>/dev/null || true)
    if [ -n "$LABEL_FILES" ] && [ "$LABEL_FILES" != "<no value>" ]; then
        # Ersten Pfad nehmen, falls mehrere kommagetrennt sind
        FIRST_CFG=$(echo "$LABEL_FILES" | cut -d',' -f1 | tr -d ' ')
        if [ -f "$FIRST_CFG" ]; then
            COMPOSE_BASENAME=$(basename "$FIRST_CFG")
            cp "$FIRST_CFG" "$CONFIG_BACKUP_DIR/$COMPOSE_BASENAME"
            COMPOSE_FOUND="$COMPOSE_BASENAME"
            log "Compose-Datei via Container-Label gefunden und gesichert: $COMPOSE_BASENAME"
        fi
    fi
fi

# Stufe 2: Standard-Namenssuche im Immich-Verzeichnis
if [ -z "$COMPOSE_FOUND" ]; then
    for f in compose.yaml compose.yml docker-compose.yaml docker-compose.yml; do
        if [ -f "$IMMICH_DIR/$f" ]; then
            cp "$IMMICH_DIR/$f" "$CONFIG_BACKUP_DIR/$f"
            COMPOSE_FOUND="$f"
            log "Compose-Datei nach Standardname gesichert: $f"
            break
        fi
    done
fi

# Stufe 3: Fallback auf *compose*.y*ml im Verzeichnis
if [ -z "$COMPOSE_FOUND" ]; then
    MATCH=$(find "$IMMICH_DIR" -maxdepth 1 -type f \( -name "*compose*.yml" -o -name "*compose*.yaml" \) 2>/dev/null | head -n 1 || true)
    if [ -n "$MATCH" ] && [ -f "$MATCH" ]; then
        COMPOSE_BASENAME=$(basename "$MATCH")
        cp "$MATCH" "$CONFIG_BACKUP_DIR/$COMPOSE_BASENAME"
        COMPOSE_FOUND="$COMPOSE_BASENAME"
        log "Compose-Datei via Muster-Fallback gesichert: $COMPOSE_BASENAME"
    fi
fi

# Kompatibilitäts-Fallback: immer eine docker-compose.yml im Backup bereitstellen
if [ -n "$COMPOSE_FOUND" ] && [ "$COMPOSE_FOUND" != "docker-compose.yml" ]; then
    cp "$CONFIG_BACKUP_DIR/$COMPOSE_FOUND" "$CONFIG_BACKUP_DIR/docker-compose.yml"
fi

if [ -z "$COMPOSE_FOUND" ]; then
    log "WARNUNG: Keine Compose-Datei gefunden."
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
  "compose_file": "$COMPOSE_FOUND",
  "backup_host": "$(hostname)",
  "backup_script_version": "3"
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

# --- Rsync der Library mit Erhalt aller Rechte und IDs ---
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