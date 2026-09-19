# Immich Backup Manager

A lightweight web dashboard and automation tool for managing external SSD backups and restores of a self-hosted [Immich](https://immich.app/) instance on a Raspberry Pi or Linux server.

## Features
- **UUID-based Mounts:** Mounts target external drives reliably by filesystem UUID, preventing issues when SATA ports or `/dev/sdX` assignments change.
- **Hardware RAID Protection:** Filters out active software RAID members (e.g. `md0`) to prevent accidental mounting or data loss.
- **Automated PostgreSQL Dumps:** Dumps the database and reads the running Immich container version for compatibility matching.
- **Granular Restore:** Web-based restore wizard with three modes (Database only, Files only, or Full Restore) and container state management.
- **Encrypted Remote Sync:** Optional WebDAV upload of database dumps to a Nextcloud instance. Credentials are encrypted on disk.
- **Web UI & SSE:** Real-time log and progress streaming in the browser via Server-Sent Events (SSE).

## Installation

### 1. Prerequisites
Install the required packages on your system:
```bash
sudo apt-get update
sudo apt-get install -y python3-cryptography python3-flask rsync

### 2. Copy File
```bash
sudo mkdir -p /opt/immich-backup-manager/templates
sudo cp app.py /opt/immich-backup-manager/
sudo cp templates/* /opt/immich-backup-manager/templates/
sudo cp immich-backup.sh /usr/local/bin/
sudo chmod +x /usr/local/bin/immich-backup.sh
sudo chown -R $USER:$USER /opt/immich-backup-manager

### 3. sudoer Configuration

Allow the service user to run mount, unmount, and the backup script without entering a password:

```bash
sudo cp sudoers-immich-backup /etc/sudoers.d/immich-backup
sudo chmod 440 /etc/sudoers.d/immich-backup
sudo visudo -c

### 4. Enable SystemD Service

```bash
sudo cp immich-backup-manager.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now immich-backup-manager


### Web Interace

The web interface will be accessible at:

```plaintext
http://<YOUR-SERVER-IP>:8090

(Default login password: immich – make sure to update it on initial setup)

### License
MIT
