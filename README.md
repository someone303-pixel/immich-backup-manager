# Immich Backup Manager

A lightweight web dashboard and automation tool for managing external SSD backups and restores of a self-hosted [Immich](https://immich.app/) instance on a Raspberry Pi or Linux server.

---

## Features

- **UUID-based Mounts:** Mounts target external drives reliably by filesystem UUID, preventing issues when SATA ports or `/dev/sdX` assignments change.
- **Hardware Integrity & RAID Protection:** Automatically filters out active software RAID members (e.g. `md0`, `sda`-`sdd`) to prevent accidental mounting or data loss.
- **Automated PostgreSQL Dumps:** Dumps the database via `pg_dump` and records the running Immich container version for compatibility checks.
- **Granular Restore:** Web-based restore wizard with three distinct modes:
  - **Database only:** Restores metadata, albums, and tags without touching media files.
  - **Files only:** Syncs photos and videos back from the SSD without stopping containers.
  - **Full Restore:** Complete restoration of database and media files with container lifecycle management.
- **Encrypted Remote Sync:** Optional WebDAV upload of database dumps to a Nextcloud instance with on-disk encrypted credentials.
- **Web UI & Live Streaming:** Real-time log and command output streaming in the browser via Server-Sent Events (SSE).

---

## Installation

### 1. Prerequisites

Install the required system packages:

```bash
sudo apt-get update
sudo apt-get install -y python3-cryptography python3-flask rsync
```

### 2. Set Up Application Directory

Create the target directory and copy the project files:

```bash
sudo mkdir -p /opt/immich-backup-manager/templates
sudo cp app.py /opt/immich-backup-manager/
sudo cp templates/* /opt/immich-backup-manager/templates/
sudo cp immich-backup.sh /usr/local/bin/
sudo chmod +x /usr/local/bin/immich-backup.sh
sudo chown -R $USER:$USER /opt/immich-backup-manager
```

### 3. Sudoers Configuration

Allow the service user to execute mount, unmount, and the backup script without entering
a password:

```bash
sudo cp sudoers-immich-backup /etc/sudoers.d/immich-backup
sudo chmod 440 /etc/sudoers.d/immich-backup
sudo visudo -c
```

> **Note:** Make sure `/etc/sudoers.d/immich-backup` lists your actual service user (replace `your_username` with your Linux user).

### 4. Enable Systemd Service

Install and start the systemd background service:

```bash
sudo cp immich-backup-manager.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now immich-backup-manager
```

Check the service status:

```bash
sudo systemctl status immich-backup-manager
```

---

## Web Interface

Access the dashboard in your web browser:

```text
http://<YOUR-SERVER-IP>:8090
```

- **Default login password:** `immich`
- *Change the password immediately in the configuration upon initial setup.*

---

## Workflow

1. **Configure Backup SSD:**
   - Plug in your external drive.
   - Open the **Konfiguration** tab in the web UI and click **font awesome Scan / ‑ Angesteckte SSDs suchen**.
   - Select your partition and click *)⁾ Als aktive Backup-SSD speichern** to bind the drive by its UUID.
2. **Run Backups:**
   - Click **| Mount** in the top bar to mount the drive to `/mnt/backup`.
   - Navigate to the **Backup** tab and trigger **▰ Backup starten**.
3. **Safely Eject:**
   - Click **| Auswerfen** to unmount the drive and safely power down the SCSI port before disconnecting.

---

## License

This project is licensed under the MIT License.
