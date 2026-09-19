#!/usr/bin/env python3
"""
Immich Backup Manager v4
Flask web interface for managing SSD backups of Immich data.
Includes: UUID-based SSD management, secure password storage,
          cron scheduling, Nextcloud DB upload, statistics, and full restore.
"""

import os
import subprocess
import threading
import queue
import json
import time
import urllib.request
import urllib.parse
import base64
from functools import wraps
from pathlib import Path
from flask import Flask, render_template, request, jsonify, Response, session, redirect, url_for
from werkzeug.security import generate_password_hash, check_password_hash
from cryptography.fernet import Fernet

BASE_DIR = "/opt/immich-backup-manager"
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
KEY_FILE = os.path.join(BASE_DIR, ".secret.key")

# --- Persistent Key for Nextcloud Password ---
def get_cipher():
    if not os.path.exists(KEY_FILE):
        key = Fernet.generate_key()
        with open(KEY_FILE, "wb") as f:
            f.write(key)
        os.chmod(KEY_FILE, 0o600)
    else:
        with open(KEY_FILE, "rb") as f:
            key = f.read().strip()
    return Fernet(key)

cipher = get_cipher()

def encrypt_str(plain_text: str) -> str:
    if not plain_text:
        return ""
    return cipher.encrypt(plain_text.encode("utf-8")).decode("utf-8")

def decrypt_str(cipher_text: str) -> str:
    if not cipher_text:
        return ""
    try:
        return cipher.decrypt(cipher_text.encode("utf-8")).decode("utf-8")
    except Exception:
        return ""

app = Flask(__name__)
# Statischer Secret-Key für Sitzungen (Session-Drop bei Neustarts verhindern)
SECRET_KEY_FILE = os.path.join(BASE_DIR, ".flask_secret")
if not os.path.exists(SECRET_KEY_FILE):
    with open(SECRET_KEY_FILE, "wb") as f:
        f.write(os.urandom(32))
    os.chmod(SECRET_KEY_FILE, 0o600)
with open(SECRET_KEY_FILE, "rb") as f:
    app.secret_key = f.read()

# --- Persistent Config ---
DEFAULT_CONFIG = {
    "password_hash": generate_password_hash("immich"),
    "backup_mount": "/mnt/backup",
    "backup_script": "/usr/local/bin/immich-backup.sh",
    "backup_uuid": "",
    "backup_label": "",
    "backup_last_device": "",
    "log_file": "/var/log/immich-backup.log",
    "library_base": "/mnt/data/immich/library",
    "immich_compose_dir": "/mnt/data/immich",
    "nextcloud": {
        "enabled": False,
        "url": "https://plastic-images.de/nxtcloud",
        "username": "",
        "password_enc": "",
        "remote_path": "/Documents/Backup",
    },
    "cron": {
        "daily_enabled": False,
        "daily_hour": 3,
        "daily_minute": 0,
        "weekly_enabled": False,
        "weekly_day": 0,
        "weekly_hour": 2,
        "weekly_minute": 0,
    }
}

def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                data = json.load(f)
            cfg.update(data)
            cfg["nextcloud"] = {**DEFAULT_CONFIG["nextcloud"], **data.get("nextcloud", {})}
            cfg["cron"] = {**DEFAULT_CONFIG["cron"], **data.get("cron", {})}

            # Migration: Klartextpasswort für Login -> Hash
            if "password" in data:
                cfg["password_hash"] = generate_password_hash(data["password"])
                del cfg["password"]
                save_config(cfg)

            # Migration: Klartextpasswort für Nextcloud -> Verschlüsselt
            if "password" in cfg["nextcloud"] and cfg["nextcloud"]["password"]:
                cfg["nextcloud"]["password_enc"] = encrypt_str(cfg["nextcloud"]["password"])
                del cfg["nextcloud"]["password"]
                save_config(cfg)
        except Exception:
            pass
    return cfg

def save_config(cfg):
    os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)

config = load_config()

# Global state
backup_running = False
restore_running = False
backup_output_queue = queue.Queue()
restore_output_queue = queue.Queue()

# --- Auth Decorator ---
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated

@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        pw = request.form.get("password", "")
        if check_password_hash(config.get("password_hash", ""), pw):
            session["logged_in"] = True
            return redirect(url_for("index"))
        error = "Falsches Passwort"
    return render_template("login.html", error=error)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# --- Helpers ---
def run_cmd(cmd, timeout=30):
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "", "Timeout"
    except Exception as e:
        return -1, "", str(e)

def find_sata_host():
    rc, out, _ = run_cmd(["ls", "/sys/class/scsi_host/"])
    if rc == 0:
        for host in out.split():
            rc2, out2, _ = run_cmd(["cat", f"/sys/class/scsi_host/{host}/proc_name"])
            if rc2 == 0 and "ahci" in out2.lower():
                return host
    return "host0"

def get_raid_members():
    raid_members = set()
    # 1. Aus /proc/mdstat auslesen
    try:
        with open("/proc/mdstat", "r") as f:
            for line in f:
                if "[" in line and "]" in line:
                    parts = line.split()
                    for p in parts:
                        dev_name = p.split("[")[0].strip()
                        if dev_name.startswith("sd"):
                            # Root-Disk abfangen (sda1 -> sda)
                            parent = "".join([c for c in dev_name if not c.isdigit()])
                            raid_members.add(dev_name)
                            raid_members.add(parent)
    except Exception:
        pass

    # 2. Aus /sys/block/*/slaves auslesen
    rc, out, _ = run_cmd(["lsblk", "-J", "-d"])
    if rc == 0:
        try:
            data = json.loads(out)
            for dev in data.get("blockdevices", []):
                if dev.get("type") == "raid":
                    r_name = dev.get("name", "")
                    rc2, slaves, _ = run_cmd(["sh", "-c", f"ls -1 /sys/block/{r_name}/slaves/ 2>/dev/null || true"])
                    if rc2 == 0:
                        for s in slaves.split():
                            s_clean = s.strip()
                            raid_members.add(s_clean)
                            raid_members.add("".join([c for c in s_clean if not c.isdigit()]))
        except Exception:
            pass
    return raid_members

def scan_available_partitions():
    raid_members = get_raid_members()
    rc, out, _ = run_cmd(["lsblk", "-J", "-o", "NAME,SIZE,TYPE,FSTYPE,UUID,LABEL,MOUNTPOINT"])
    if rc != 0:
        return []

    results = []
    try:
        data = json.loads(out)
        for dev in data.get("blockdevices", []):
            parent_name = dev.get("name", "")
            if parent_name in raid_members or not parent_name.startswith("sd"):
                continue

            children = dev.get("children", [dev])
            for part in children:
                p_name = part.get("name", "")
                if p_name in raid_members:
                    continue
                uuid = part.get("uuid")
                fstype = part.get("fstype")
                label = part.get("label") or ""
                size = part.get("size")
                mountpoint = part.get("mountpoint") or ""

                # Nur Partitionen/Devices mit Dateisystem aufnehmen
                results.append({
                    "device": f"/dev/{p_name}",
                    "name": p_name,
                    "parent": parent_name,
                    "uuid": uuid if uuid else "KEINE_UUID",
                    "fstype": fstype if fstype else "unformatiert",
                    "size": size,
                    "label": label,
                    "mountpoint": mountpoint,
                    "is_current": (uuid and uuid == config.get("backup_uuid"))
                })
    except Exception:
        pass
    return results

def get_ssd_info():
    info = {
        "present": False, "mounted": False, "label": config.get("backup_label") or "Backup-SSD",
        "uuid": config.get("backup_uuid") or None, "device": config.get("backup_last_device") or None,
        "size": None, "used": None, "avail": None, "use_pct": None, "fstype": None
    }
    mount_point = config["backup_mount"]
    rc, _, _ = run_cmd(["mountpoint", "-q", mount_point])
    info["mounted"] = (rc == 0)

    # Aktives Device ermitteln via findmnt
    rc_mnt, out_mnt, _ = run_cmd(["findmnt", "-no", "SOURCE", mount_point])
    if rc_mnt == 0 and out_mnt:
        info["device"] = out_mnt
        info["present"] = True

    # Wenn nicht gemountet, nach UUID suchen
    target_uuid = config.get("backup_uuid")
    if target_uuid:
        rc_id, out_id, _ = run_cmd(["blkid", "-U", target_uuid])
        if rc_id == 0 and out_id:
            info["present"] = True
            info["device"] = out_id

    if info["mounted"]:
        rc_df, out_df, _ = run_cmd(["df", "-h", mount_point])
        if rc_df == 0:
            lines = out_df.strip().split("\n")
            if len(lines) >= 2:
                parts = lines[1].split()
                if len(parts) >= 6:
                    info["size"] = parts[1]
                    info["used"] = parts[2]
                    info["avail"] = parts[3]
                    info["use_pct"] = parts[4]

    return info

def get_running_immich_version():
    rc, out, _ = run_cmd(["docker", "inspect", "immich_server",
                           "--format", "{{index .Config.Labels \"org.opencontainers.image.version\"}}"])
    if rc == 0 and out and out != "<no value>":
        return out.strip()
    rc2, out2, _ = run_cmd(["docker", "inspect", "immich_server",
                             "--format", "{{.Config.Image}}"])
    if rc2 == 0 and out2:
        tag = out2.split(":")[-1]
        return tag if tag else "unknown"
    return "unknown"

def get_backup_meta():
    meta_file = Path(config["backup_mount"]) / "immich" / "meta.json"
    if meta_file.exists():
        try:
            return json.loads(meta_file.read_text())
        except Exception:
            pass
    return {}

def list_db_dumps():
    db_dir = Path(config["backup_mount"]) / "immich" / "database"
    if not db_dir.exists():
        return []
    dumps = sorted(db_dir.glob("*.dump"), key=lambda p: p.stat().st_mtime, reverse=True)
    result = []
    for d in dumps:
        stat = d.stat()
        name = d.name
        version = "unknown"
        parts = name.replace(".dump", "").split("_v")
        if len(parts) > 1:
            version = parts[-1]
        result.append({
            "filename": name,
            "path": str(d),
            "version": version,
            "size_mb": round(stat.st_size / 1024 / 1024, 1),
            "mtime": time.strftime("%Y-%m-%d %H:%M", time.localtime(stat.st_mtime)),
        })
    return result

def check_config_backup():
    cfg_dir = Path(config["backup_mount"]) / "immich" / "config"
    return {
        "compose": (cfg_dir / "docker-compose.yml").exists(),
        "env": (cfg_dir / ".env").exists(),
    }

def dir_stats(path):
    try:
        p = Path(path)
        if not p.exists():
            return 0, "0"
        count = sum(1 for _ in p.rglob("*") if _.is_file())
        rc, out, _ = run_cmd(["du", "-sh", path], timeout=120)
        size = out.split()[0] if rc == 0 and out else "?"
        return count, size
    except Exception:
        return 0, "?"

# --- Cron Management ---
CRON_TAG_DAILY  = "# immich-backup-daily"
CRON_TAG_WEEKLY = "# immich-backup-weekly"

def read_crontab():
    rc, out, _ = run_cmd(["sudo", "crontab", "-u", "root", "-l"])
    return out if rc == 0 else ""

def write_crontab(content):
    proc = subprocess.Popen(["sudo", "crontab", "-u", "root", "-"],
                            stdin=subprocess.PIPE, text=True)
    proc.communicate(content)

def apply_cron(cfg_cron, script):
    lines = [l for l in read_crontab().splitlines()
             if CRON_TAG_DAILY not in l and CRON_TAG_WEEKLY not in l and l.strip()]
    if cfg_cron["daily_enabled"]:
        h, m = cfg_cron["daily_hour"], cfg_cron["daily_minute"]
        lines.append(f"{m} {h} * * * {script} {CRON_TAG_DAILY}")
    if cfg_cron["weekly_enabled"]:
        h, m, d = cfg_cron["weekly_hour"], cfg_cron["weekly_minute"], cfg_cron["weekly_day"]
        lines.append(f"{m} {h} * * {d} {script} {CRON_TAG_WEEKLY}")
    write_crontab("\n".join(lines) + "\n")

def get_cron_status():
    tab = read_crontab()
    return {
        "daily_active":  CRON_TAG_DAILY  in tab,
        "weekly_active": CRON_TAG_WEEKLY in tab,
    }

# --- Nextcloud Upload ---
def nextcloud_upload(local_path, remote_filename):
    nc = config["nextcloud"]
    decrypted_pw = decrypt_str(nc.get("password_enc", ""))
    if not nc.get("enabled") or not nc.get("username") or not decrypted_pw:
        return False, "Nextcloud ist nicht vollständig konfiguriert."
    base_url = nc["url"].rstrip("/")
    username = nc["username"]
    remote_dir = nc["remote_path"].strip("/")
    webdav_base = f"{base_url}/remote.php/dav/files/{urllib.parse.quote(username)}"
    dest_url = f"{webdav_base}/{remote_dir}/{urllib.parse.quote(remote_filename)}"
    creds = base64.b64encode(f"{username}:{decrypted_pw}".encode()).decode()
    headers = {"Authorization": f"Basic {creds}"}
    try:
        dir_url = f"{webdav_base}/{remote_dir}/"
        mkcol_req = urllib.request.Request(dir_url, method="MKCOL", headers=headers)
        try:
            urllib.request.urlopen(mkcol_req, timeout=15)
        except urllib.error.HTTPError as e:
            if e.code not in (301, 405, 409):
                return False, f"MKCOL Ordnererstellung fehlgeschlagen: {e.code}"
        with open(local_path, "rb") as f:
            data = f.read()
        put_req = urllib.request.Request(dest_url, data=data, method="PUT", headers={
            **headers,
            "Content-Type": "application/octet-stream",
            "Content-Length": str(len(data)),
        })
        urllib.request.urlopen(put_req, timeout=120)
        return True, f"Hochgeladen: {remote_filename}"
    except urllib.error.HTTPError as e:
        return False, f"HTTP-Fehler {e.code}: {e.reason}"
    except Exception as e:
        return False, str(e)

# --- Routes ---
@app.route("/")
@login_required
def index():
    return render_template("index.html")

@app.route("/api/status")
@login_required
def api_status():
    return jsonify({
        "ssd": get_ssd_info(),
        "backup_running": backup_running,
        "restore_running": restore_running,
        "cron": get_cron_status(),
    })

# SSD / SATA
@app.route("/api/scan-sata-devices", methods=["GET"])
@login_required
def api_scan_sata_devices():
    devices = scan_available_partitions()
    return jsonify({"ok": True, "devices": devices})

@app.route("/api/sata-config", methods=["POST"])
@login_required
def api_sata_config_set():
    data = request.json
    uuid = data.get("uuid", "").strip()
    label = data.get("label", "").strip()
    device = data.get("device", "").strip()

    if not uuid or uuid == "KEINE_UUID":
        return jsonify({"ok": False, "msg": "Keine gültige UUID vorhanden (Partition formatiert?)"})

    # Absicherung: Sicherstellen, dass UUID kein RAID-Array ist
    raid_members = get_raid_members()
    for rm in raid_members:
        rc, out, _ = run_cmd(["blkid", "-s", "UUID", "-o", "value", f"/dev/{rm}"])
        if rc == 0 and out.strip() == uuid:
            return jsonify({"ok": False, "msg": "ABBRUCH: Ausgewählte Partition gehört zum internen RAID-Array!"})

    config["backup_uuid"] = uuid
    config["backup_label"] = label or "Backup-SSD"
    config["backup_last_device"] = device
    save_config(config)
    return jsonify({"ok": True, "msg": f"Partition gespeichert (UUID: {uuid[:8]}...)"})

@app.route("/api/mount", methods=["POST"])
@login_required
def api_mount():
    mount_point = config["backup_mount"]
    rc, _, _ = run_cmd(["mountpoint", "-q", mount_point])
    if rc == 0:
        return jsonify({"ok": False, "msg": "SSD ist bereits gemountet."})

    uuid = config.get("backup_uuid")
    if not uuid:
        return jsonify({"ok": False, "msg": "Keine Backup-SSD konfiguriert. Bitte erst im Tab 'Konfiguration' auswählen."})

    # Prüfen, ob Device mit dieser UUID am Bus existiert
    rc_find, dev_path, _ = run_cmd(["blkid", "-U", uuid])
    if rc_find != 0 or not dev_path:
        return jsonify({"ok": False, "msg": f"SSD mit UUID {uuid} wurde nicht gefunden. Bitte anstecken & 'Scan' drücken."})

    run_cmd(["mkdir", "-p", mount_point])
    rc_mnt, _, err_mnt = run_cmd(["sudo", "mount", f"UUID={uuid}", mount_point])
    if rc_mnt != 0:
        return jsonify({"ok": False, "msg": f"Mount fehlgeschlagen: {err_mnt}"})

    config["backup_last_device"] = dev_path
    save_config(config)
    return jsonify({"ok": True, "msg": f"SSD gemountet ({dev_path} -> {mount_point})"})

@app.route("/api/unmount", methods=["POST"])
@login_required
def api_unmount():
    if backup_running or restore_running:
        return jsonify({"ok": False, "msg": "Backup oder Restore läuft noch."})
    mount_point = config["backup_mount"]
    rc, _, _ = run_cmd(["mountpoint", "-q", mount_point])
    if rc != 0:
        return jsonify({"ok": True, "msg": "SSD war nicht gemountet."})

    # Gemountetes Device ermitteln
    rc_f, src_dev, _ = run_cmd(["findmnt", "-no", "SOURCE", mount_point])
    device_name = ""
    if rc_f == 0 and src_dev.startswith("/dev/"):
        device_name = src_dev.replace("/dev/", "")

    rc_u, _, err_u = run_cmd(["sudo", "umount", mount_point])
    if rc_u != 0:
        return jsonify({"ok": False, "msg": f"Unmount fehlgeschlagen: {err_u}"})

    # SCSI Delete triggern
    if device_name:
        parent = "".join([c for c in device_name if not c.isdigit()])
        delete_path = f"/sys/block/{parent}/device/delete"
        if os.path.exists(delete_path):
            run_cmd(["sudo", "sh", "-c", f"echo 1 > {delete_path}"])

    return jsonify({"ok": True, "msg": "SSD sicher ausgehängt und Bus freigegeben."})

@app.route("/api/rescan", methods=["POST"])
@login_required
def api_rescan():
    host = find_sata_host()
    scan_path = f"/sys/class/scsi_host/{host}/scan"
    rc, _, err = run_cmd(["sudo", "sh", "-c", f"echo '- - -' > {scan_path}"])
    if rc != 0:
        return jsonify({"ok": False, "msg": f"Rescan fehlgeschlagen: {err}"})
    time.sleep(2)
    return jsonify({"ok": True, "msg": "SATA-Bus erfolgreich gescannt."})

# Backup Execute
@app.route("/api/backup/start", methods=["POST"])
@login_required
def api_backup_start():
    global backup_running
    if backup_running:
        return jsonify({"ok": False, "msg": "Backup läuft bereits."})
    if restore_running:
        return jsonify({"ok": False, "msg": "Restore läuft noch."})
    rc, _, _ = run_cmd(["mountpoint", "-q", config["backup_mount"]])
    if rc != 0:
        return jsonify({"ok": False, "msg": "SSD ist nicht gemountet."})

    backup_running = True

    def run_backup():
        global backup_running
        try:
            process = subprocess.Popen(
                ["sudo", config["backup_script"]],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1
            )
            for line in process.stdout:
                backup_output_queue.put(line.rstrip())
            process.wait()
            if process.returncode == 0:
                if config["nextcloud"]["enabled"]:
                    backup_output_queue.put("[NC] Starte Nextcloud-Upload...")
                    dumps = list_db_dumps()
                    if dumps:
                        ok, msg = nextcloud_upload(dumps[0]["path"], dumps[0]["filename"])
                        backup_output_queue.put(f"[NC] {'✓' if ok else '✗'} {msg}")
                    else:
                        backup_output_queue.put("[NC] ✗ Kein DB-Dump gefunden.")
                backup_output_queue.put("__DONE__")
            else:
                backup_output_queue.put(f"__ERROR__ Code {process.returncode}")
        except Exception as e:
            backup_output_queue.put(f"__ERROR__ {e}")
        finally:
            backup_running = False

    threading.Thread(target=run_backup, daemon=True).start()
    return jsonify({"ok": True, "msg": "Backup gestartet."})

@app.route("/api/backup/stream")
@login_required
def api_backup_stream():
    def generate():
        yield "data: connected\n\n"
        while True:
            try:
                line = backup_output_queue.get(timeout=30)
                yield f"data: {json.dumps(line)}\n\n"
                if line.startswith("__DONE__") or line.startswith("__ERROR__"):
                    break
            except queue.Empty:
                if not backup_running:
                    break
                yield 'data: "__PING__"\n\n'
    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

# Restore Execute
@app.route("/api/restore/info")
@login_required
def api_restore_info():
    dumps = list_db_dumps()
    meta  = get_backup_meta()
    cfg_backup = check_config_backup()
    running_version = get_running_immich_version()

    version_warnings = [
        d["filename"] for d in dumps
        if d["version"] != "unknown" and running_version != "unknown" and d["version"] != running_version
    ]

    return jsonify({
        "ok": True,
        "dumps": dumps,
        "meta": meta,
        "config_backup": cfg_backup,
        "running_version": running_version,
        "version_warnings": version_warnings,
        "ssd_mounted": get_ssd_info()["mounted"],
    })

@app.route("/api/restore/start", methods=["POST"])
@login_required
def api_restore_start():
    global restore_running
    if restore_running:
        return jsonify({"ok": False, "msg": "Restore läuft bereits."})
    if backup_running:
        return jsonify({"ok": False, "msg": "Backup läuft noch."})

    data = request.json
    mode = data.get("mode")
    dump_file = data.get("dump_file")

    if mode not in ("db_only", "files_only", "full"):
        return jsonify({"ok": False, "msg": "Ungültiger Modus."})

    rc, _, _ = run_cmd(["mountpoint", "-q", config["backup_mount"]])
    if rc != 0:
        return jsonify({"ok": False, "msg": "SSD ist nicht gemountet."})

    if mode in ("db_only", "full") and not dump_file:
        return jsonify({"ok": False, "msg": "Kein Dump ausgewählt."})

    dump_path = str(Path(config["backup_mount"]) / "immich" / "database" / dump_file) if dump_file else None

    if dump_path and not Path(dump_path).exists():
        return jsonify({"ok": False, "msg": f"Dump-Datei nicht gefunden: {dump_file}"})

    restore_running = True

    def run_restore():
        global restore_running
        q = restore_output_queue

        def log(msg, level="info"):
            prefix = {"info": "  ", "ok": "✓ ", "err": "✗ ", "warn": "⚠ ", "head": "► "}
            q.put(f"[{prefix.get(level,'  ')}{msg}]" if level == "head" else f"{prefix.get(level,'  ')}{msg}")

        try:
            compose_dir = config["immich_compose_dir"]

            if mode in ("db_only", "full"):
                log("Stoppe Immich-Container...", "head")
                rc, _, err = run_cmd(["docker", "compose", "-f", f"{compose_dir}/docker-compose.yml", "down"], timeout=60)
                if rc != 0:
                    log(f"docker compose down fehlgeschlagen: {err}", "err")
                    q.put("__ERROR__ Container konnte nicht gestoppt werden.")
                    return
                log("Container gestoppt.", "ok")

            if mode in ("files_only", "full"):
                log("Stelle Dateien wieder her...", "head")
                mount = config["backup_mount"]
                lib   = config["library_base"]

                for src_name, dst_name in [("upload", "upload"), ("library", "library"), ("profile", "profile")]:
                    src = f"{mount}/immich/{src_name}/"
                    dst = f"{lib}/{dst_name}/"
                    if not Path(src).exists():
                        log(f"{src_name}/ nicht auf SSD – übersprungen.", "warn")
                        continue
                    log(f"rsync {src_name}/...", "info")
                    proc = subprocess.Popen(
                        ["rsync", "-a", "--delete", "--info=progress2", src, dst],
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
                    )
                    for line in proc.stdout:
                        q.put(line.rstrip())
                    proc.wait()
                    if proc.returncode != 0:
                        log(f"rsync {src_name}/ fehlgeschlagen.", "err")
                        q.put("__ERROR__ rsync fehlgeschlagen.")
                        return
                    log(f"{src_name}/ wiederhergestellt.", "ok")

            if mode in ("db_only", "full"):
                log("Starte Datenbank-Container...", "head")
                rc, _, err = run_cmd(["docker", "compose", "-f", f"{compose_dir}/docker-compose.yml", "up", "-d", "database"], timeout=60)
                if rc != 0:
                    log(f"Datenbank-Start fehlgeschlagen: {err}", "err")
                    q.put("__ERROR__ Datenbank konnte nicht gestartet werden.")
                    return

                log("Warte auf Datenbankbereitschaft...", "info")
                time.sleep(8)

                log(f"Spiele Dump ein: {os.path.basename(dump_path)}", "head")
                cmds = [
                    ["docker", "exec", "immich_postgres", "psql", "-U", "postgres", "-c", "DROP DATABASE IF EXISTS immich;"],
                    ["docker", "exec", "immich_postgres", "psql", "-U", "postgres", "-c", "CREATE DATABASE immich;"],
                ]
                for cmd in cmds:
                    rc, out, err = run_cmd(cmd, timeout=30)
                    if rc != 0:
                        log(f"DB-Reset fehlgeschlagen: {err}", "err")
                        q.put("__ERROR__ DB konnte nicht zurückgesetzt werden.")
                        return

                with open(dump_path, "rb") as dump_in:
                    proc = subprocess.Popen(
                        ["docker", "exec", "-i", "immich_postgres", "pg_restore", "-U", "postgres", "-d", "immich", "--no-owner", "--role=postgres"],
                        stdin=dump_in, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=1
                    )
                    for line in proc.stdout:
                        q.put(line.decode("utf-8", errors="replace").rstrip())
                    proc.wait()
                log("Datenbank-Restore abgeschlossen.", "ok")

                log("Starte alle Immich-Container...", "head")
                rc, _, err = run_cmd(["docker", "compose", "-f", f"{compose_dir}/docker-compose.yml", "up", "-d"], timeout=60)
                if rc != 0:
                    log(f"docker compose up fehlgeschlagen: {err}", "err")
                    q.put("__ERROR__ Container konnten nicht gestartet werden.")
                    return
                log("Alle Container erfolgreich gestartet.", "ok")

            q.put("__DONE__")
        except Exception as e:
            q.put(f"__ERROR__ {e}")
        finally:
            restore_running = False

    threading.Thread(target=run_restore, daemon=True).start()
    return jsonify({"ok": True, "msg": "Restore gestartet."})

@app.route("/api/restore/stream")
@login_required
def api_restore_stream():
    def generate():
        yield "data: connected\n\n"
        while True:
            try:
                line = restore_output_queue.get(timeout=30)
                yield f"data: {json.dumps(line)}\n\n"
                if line.startswith("__DONE__") or line.startswith("__ERROR__"):
                    break
            except queue.Empty:
                if not restore_running:
                    break
                yield 'data: "__PING__"\n\n'
    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

# Logs & Stats
@app.route("/api/log")
@login_required
def api_log():
    n = request.args.get("lines", 60, type=int)
    rc, out, _ = run_cmd(["tail", f"-{n}", config["log_file"]])
    if rc == 0:
        return jsonify({"ok": True, "lines": out.split("\n")})
    return jsonify({"ok": False, "lines": ["Logdatei nicht gefunden."]})

@app.route("/api/stats")
@login_required
def api_stats():
    lib   = config["library_base"]
    mount = config["backup_mount"]
    sections = [
        ("upload",   f"{lib}/upload",    f"{mount}/immich/upload"),
        ("library",  f"{lib}/library",   f"{mount}/immich/library"),
        ("profile",  f"{lib}/profile",   f"{mount}/immich/profile"),
        ("database", f"{lib}/../postgres", f"{mount}/immich/database"),
    ]
    results = []
    for name, src, dst in sections:
        sc, ss = dir_stats(src)
        dc, ds = dir_stats(dst)
        results.append({"name": name,
                        "source_files": sc, "source_size": ss,
                        "backup_files": dc, "backup_size": ds})
    dumps = list_db_dumps()[:5]
    return jsonify({"ok": True, "sections": results, "db_dumps": dumps})

# Cron
@app.route("/api/cron", methods=["GET"])
@login_required
def api_cron_get():
    return jsonify({"ok": True, "cron": config["cron"]})

@app.route("/api/cron", methods=["POST"])
@login_required
def api_cron_set():
    data = request.json
    config["cron"].update({
        "daily_enabled":  bool(data.get("daily_enabled", False)),
        "daily_hour":     int(data.get("daily_hour", 3)),
        "daily_minute":   int(data.get("daily_minute", 0)),
        "weekly_enabled": bool(data.get("weekly_enabled", False)),
        "weekly_day":     int(data.get("weekly_day", 0)),
        "weekly_hour":    int(data.get("weekly_hour", 2)),
        "weekly_minute":  int(data.get("weekly_minute", 0)),
    })
    save_config(config)
    try:
        apply_cron(config["cron"], config["backup_script"])
        return jsonify({"ok": True, "msg": "Zeitplan gespeichert."})
    except Exception as e:
        return jsonify({"ok": False, "msg": f"Crontab-Fehler: {e}"})

# Nextcloud
@app.route("/api/nextcloud", methods=["GET"])
@login_required
def api_nextcloud_get():
    nc = dict(config["nextcloud"])
    nc["password"] = "••••••••" if nc.get("password_enc") else ""
    return jsonify({"ok": True, "nextcloud": nc})

@app.route("/api/nextcloud", methods=["POST"])
@login_required
def api_nextcloud_set():
    data = request.json
    nc = config["nextcloud"]
    nc["enabled"]     = bool(data.get("enabled", False))
    nc["url"]         = data.get("url", nc["url"]).rstrip("/")
    nc["username"]    = data.get("username", nc["username"])
    nc["remote_path"] = data.get("remote_path", nc["remote_path"])
    new_pw = data.get("password")
    if new_pw and new_pw != "••••••••":
        nc["password_enc"] = encrypt_str(new_pw)
    save_config(config)
    return jsonify({"ok": True, "msg": "Nextcloud-Einstellungen gespeichert."})

@app.route("/api/nextcloud/test", methods=["POST"])
@login_required
def api_nextcloud_test():
    nc = config["nextcloud"]
    decrypted_pw = decrypt_str(nc.get("password_enc", ""))
    if not nc.get("username") or not decrypted_pw:
        return jsonify({"ok": False, "msg": "Zugangsdaten fehlen."})
    base_url = nc["url"].rstrip("/")
    webdav_url = f"{base_url}/remote.php/dav/files/{urllib.parse.quote(nc['username'])}/"
    creds = base64.b64encode(f"{nc['username']}:{decrypted_pw}".encode()).decode()
    try:
        req = urllib.request.Request(webdav_url, method="PROPFIND",
                                     headers={"Authorization": f"Basic {creds}", "Depth": "0"})
        urllib.request.urlopen(req, timeout=10)
        return jsonify({"ok": True, "msg": "Nextcloud-Verbindung erfolgreich!"})
    except urllib.error.HTTPError as e:
        return jsonify({"ok": False, "msg": f"HTTP {e.code}: {e.reason}"})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})

@app.route("/api/nextcloud/upload-now", methods=["POST"])
@login_required
def api_nextcloud_upload_now():
    dumps = list_db_dumps()
    if not dumps:
        return jsonify({"ok": False, "msg": "Kein DB-Dump vorhanden."})
    ok, msg = nextcloud_upload(dumps[0]["path"], dumps[0]["filename"])
    return jsonify({"ok": ok, "msg": msg})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8090, debug=False)