#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
apm.py - Universal Autonomous Supervisor, Watchdog & Anti-Rollback Engine
========================================================================
• ZERO DEPENDENCIES: 100% Python Standard Library (No requirements.txt, No Docker needed).
• PRIVACY FIRST: Public web dashboard is a clean health-check only (NO public rollback/download).
• DYNAMIC TOKEN:
    - In Cloud/Docker: Reads from TELEGRAM_BOT_TOKEN environment variable.
    - In Local/Termux: Prompts user interactively if not already saved, and saves to ~/.bot_token.
    - NO hardcoded default token.
• TELEGRAM CONTROL: Full rollback and file download controlled via Telegram / Terminal:
    - rollback list       -> See last 5 commits and backups
    - rollback 1 / 2      -> Instant rollback to version #1 or #2
    - rollback <sha>      -> Rollback to specific Gist commit
    - rollback dl         -> Sends setupbot.py directly to your Telegram chat
• CRASH-PROOF & AUTO-ROLLBACK: Automatically rolls back if 3 runtime crashes occur in 90s.
• TERMUX READY: 100% compatible with Termux and all Linux cloud servers.
"""

import os
import sys
import time
import glob
import json
import signal
import hashlib
import tempfile
import threading
import subprocess
import py_compile
import urllib.request
import urllib.error
import urllib.parse
from urllib.parse import urlparse
from http.server import HTTPServer, BaseHTTPRequestHandler

# =========================================================================
# CONFIGURATION
# =========================================================================
GIST_USER = "AnataVortex7"
GIST_ID = "24a131290c378c54478ac203c8c040f5"
GIST_RAW_URL = f"https://gist.githubusercontent.com/{GIST_USER}/{GIST_ID}/raw/setupbot.py"
GIST_COMMITS_API = f"https://api.github.com/gists/{GIST_ID}/commits"

DEFAULT_ADMIN_ID = "1193564058"
UPDATE_CHECK_INTERVAL = 60  # seconds between auto-checking Gist for updates
MAX_LOCAL_BACKUPS = 5       # Keep last 5 working versions in storage

# Detect storage paths (Cloud container vs Termux vs Local)
IS_CLOUD_VOLUME = os.path.isdir("/data") and not os.path.exists("/data/data")
if IS_CLOUD_VOLUME:
    SETUPBOT_PATH = "/data/setupbot.py"
    BACKUP_DIR = "/data/setupbot_backups"
else:
    SETUPBOT_PATH = os.path.abspath("./setupbot.py")
    BACKUP_DIR = os.path.abspath("./setupbot_backups")

os.makedirs(BACKUP_DIR, exist_ok=True)

STATE = {
    "status": "Starting",
    "bot_pid": None,
    "restart_count": 0,
    "last_sync": "Never",
    "last_error": "None",
    "start_time": time.time(),
    "current_hash": "",
    "current_commit": "Latest",
    "logs": [],
    "version_start_time": time.time(),
    "version_crash_count": 0
}

SUPERVISOR_LOCK = threading.Lock()
ACTIVE_PROCESS = None

def log(msg):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with SUPERVISOR_LOCK:
        STATE["logs"].append(line)
        if len(STATE["logs"]) > 60:
            STATE["logs"].pop(0)

def get_file_hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()

# =========================================================================
# 1. DYNAMIC BOT TOKEN RESOLUTION (CLOUD ENV vs LOCAL INTERACTIVE)
# =========================================================================
def get_bot_token():
    # 1. Cloud / Docker: Check environment variable
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if token:
        return token

    # 2. Check saved file on disk
    saved_candidates = [
        os.path.expanduser("~/.bot_token"),
        os.path.expanduser("~/agy_bot/.env"),
        "/data/agy_bot/.env",
        "/data/.bot_token",
        "./.bot_token"
    ]
    for p in saved_candidates:
        if os.path.exists(p):
            try:
                with open(p, "r") as f:
                    content = f.read().strip()
                    if "TELEGRAM_BOT_TOKEN=" in content:
                        for line in content.splitlines():
                            if "TELEGRAM_BOT_TOKEN=" in line:
                                token = line.split("=", 1)[1].strip().strip('"').strip("'")
                    elif content and not content.startswith("#"):
                        token = content
            except Exception:
                pass
        if token:
            return token

    # 3. Local Server / Termux: Ask user interactively if running in terminal (TTY)
    if sys.stdin and sys.stdin.isatty():
        print("\n" + "=" * 55)
        print("🔑 TELEGRAM BOT TOKEN REQUIRED")
        print("=" * 55)
        try:
            token = input("👉 कृपया तुमचा Telegram Bot Token टाका: ").strip()
        except Exception:
            token = ""
        print("=" * 55 + "\n")

        if token:
            try:
                save_path = os.path.expanduser("~/.bot_token")
                os.makedirs(os.path.dirname(save_path), exist_ok=True)
                with open(save_path, "w") as f:
                    f.write(token)
                print(f"✅ Token सुरक्षित सेव्ह केला: {save_path}\n")
            except Exception:
                pass
            return token

    # 4. Cloud / Non-interactive without token:
    log("⚠️ [WARNING] TELEGRAM_BOT_TOKEN is not set in Environment Variables!")
    log("ℹ️ Cloud/Docker Settings -> Secrets/Variables मध्ये TELEGRAM_BOT_TOKEN सेट करा.")
    return ""

def get_admin_id():
    return os.environ.get("TELEGRAM_ALLOWED_USER_ID", "").strip() or DEFAULT_ADMIN_ID

def get_bot_env():
    token = get_bot_token()
    env = os.environ.copy()
    if token:
        env["TELEGRAM_BOT_TOKEN"] = token
    env["TELEGRAM_ALLOWED_USER_ID"] = get_admin_id()
    env["PYTHONUNBUFFERED"] = "1"
    env["IS_DOCKER"] = "1"
    return env

# =========================================================================
# 2. LOCAL BACKUP ROTATION (LAST 5 VERSIONS)
# =========================================================================
def save_local_backup(code_bytes: bytes, tag: str = "auto") -> str:
    try:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        h = get_file_hash(code_bytes)[:8]
        filename = f"setupbot_{ts}_{h}_{tag}.py"
        filepath = os.path.join(BACKUP_DIR, filename)

        with open(filepath, "wb") as f:
            f.write(code_bytes)

        existing = sorted(glob.glob(os.path.join(BACKUP_DIR, "setupbot_*.py")), key=os.path.getmtime)
        while len(existing) > MAX_LOCAL_BACKUPS:
            oldest = existing.pop(0)
            try:
                os.remove(oldest)
            except Exception:
                pass

        return filepath
    except Exception as e:
        log(f"Backup save error: {e}")
        return ""

def get_local_backups():
    if not os.path.isdir(BACKUP_DIR):
        return []
    files = sorted(glob.glob(os.path.join(BACKUP_DIR, "setupbot_*.py")), key=os.path.getmtime, reverse=True)
    backups = []
    for f in files[:MAX_LOCAL_BACKUPS]:
        basename = os.path.basename(f)
        mtime = os.path.getmtime(f)
        date_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mtime))
        backups.append({
            "filename": basename,
            "filepath": f,
            "date": date_str,
            "size": f"{os.path.getsize(f) / 1024:.1f} KB"
        })
    return backups

# =========================================================================
# 3. GIST COMMITS API (LAST 5 COMMITS)
# =========================================================================
def get_gist_commits():
    try:
        req = urllib.request.Request(GIST_COMMITS_API, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            commits = []
            for item in data[:5]:
                sha = item.get("version", "")
                committed_at = item.get("committed_at", "").replace("T", " ").replace("Z", "")
                raw_url = f"https://gist.githubusercontent.com/{GIST_USER}/{GIST_ID}/raw/{sha}/setupbot.py"
                commits.append({
                    "sha": sha,
                    "short_sha": sha[:8],
                    "date": committed_at,
                    "raw_url": raw_url
                })
            return commits
    except Exception:
        return []

# =========================================================================
# 4. SAFE VALIDATION, DEPLOYMENT & ROLLBACK
# =========================================================================
def validate_python_syntax(code_bytes: bytes) -> tuple:
    with tempfile.NamedTemporaryFile(suffix=".py", delete=False) as tmp:
        tmp.write(code_bytes)
        tmp_name = tmp.name

    try:
        py_compile.compile(tmp_name, doraise=True)
        return True, "Syntax OK"
    except py_compile.PyCompileError as pe:
        return False, str(pe)
    except Exception as e:
        return False, str(e)
    finally:
        if os.path.exists(tmp_name):
            try:
                os.unlink(tmp_name)
            except Exception:
                pass

def deploy_code_safely(code_bytes: bytes, source_label: str = "Update") -> tuple:
    global ACTIVE_PROCESS
    valid, err = validate_python_syntax(code_bytes)
    if not valid:
        log(f"❌ SYNTAX CHECK FAILED for {source_label}: {err}")
        log("🛡️ Bad code rejected! Keeping current working version running.")
        STATE["last_error"] = f"Syntax error in {source_label}: {err}"
        return False, f"Syntax Error: {err}"

    if os.path.exists(SETUPBOT_PATH):
        try:
            with open(SETUPBOT_PATH, "rb") as cur_f:
                save_local_backup(cur_f.read(), tag="prev")
        except Exception:
            pass

    os.makedirs(os.path.dirname(os.path.abspath(SETUPBOT_PATH)), exist_ok=True)
    with open(SETUPBOT_PATH, "wb") as f:
        f.write(code_bytes)
    os.chmod(SETUPBOT_PATH, 0o755)

    STATE["current_hash"] = get_file_hash(code_bytes)
    STATE["last_sync"] = time.strftime("%H:%M:%S")
    STATE["version_start_time"] = time.time()
    STATE["version_crash_count"] = 0
    save_local_backup(code_bytes, tag="verified")

    log(f"✅ {source_label} successfully deployed!")
    restart_bot_process()
    return True, "Success"

def restart_bot_process():
    global ACTIVE_PROCESS
    if ACTIVE_PROCESS and ACTIVE_PROCESS.poll() is None:
        log("🔄 Terminating old bot process...")
        try:
            ACTIVE_PROCESS.terminate()
            ACTIVE_PROCESS.wait(timeout=4)
        except Exception:
            try:
                ACTIVE_PROCESS.kill()
            except Exception:
                pass
    ACTIVE_PROCESS = None
    subprocess.run("pkill -f 'telegram_bot.py' 2>/dev/null || true", shell=True)

def perform_rollback(target_type: str, target: str) -> tuple:
    log(f"⏪ Rollback: Type={target_type}, Target={target}")
    code_bytes = None

    if target_type == "commit":
        url = f"https://gist.githubusercontent.com/{GIST_USER}/{GIST_ID}/raw/{target}/setupbot.py"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                code_bytes = resp.read()
            STATE["current_commit"] = target[:8]
        except Exception as e:
            return False, f"Failed to fetch commit {target[:8]}: {e}"

    elif target_type == "file":
        safe_name = os.path.basename(target)
        file_path = os.path.join(BACKUP_DIR, safe_name)
        if os.path.exists(file_path):
            try:
                with open(file_path, "rb") as f:
                    code_bytes = f.read()
                STATE["current_commit"] = safe_name[:16]
            except Exception as e:
                return False, f"Failed to read backup {safe_name}: {e}"
        else:
            return False, f"Backup file {safe_name} not found"
    else:
        return False, "Invalid rollback target type"

    if code_bytes:
        ok, msg = deploy_code_safely(code_bytes, source_label=f"Rollback ({target[:8]})")
        return ok, msg
    return False, "No code data found"

def auto_rollback_to_latest_working():
    log("🚨 AUTO-ROLLBACK: Rapid crash detected. Reverting to previous working backup...")
    backups = get_local_backups()
    if len(backups) >= 2:
        target_file = backups[1]["filename"]
        ok, msg = perform_rollback("file", target_file)
        if ok:
            log(f"✅ Reverted to local backup: {target_file}")
            return True

    commits = get_gist_commits()
    if len(commits) >= 2:
        target_sha = commits[1]["sha"]
        ok, msg = perform_rollback("commit", target_sha)
        if ok:
            log(f"✅ Reverted to Gist commit: {target_sha[:8]}")
            return True

    log("⚠️ No previous version found for auto-rollback.")
    return False

def fetch_and_check_latest_gist():
    try:
        req = urllib.request.Request(GIST_RAW_URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            new_code = resp.read()
    except Exception:
        return False

    if not new_code or len(new_code) < 500:
        return False

    new_hash = get_file_hash(new_code)
    if new_hash == STATE["current_hash"]:
        STATE["last_sync"] = time.strftime("%H:%M:%S")
        return False

    log("⚡ New commit on Gist detected! Validating & applying...")
    ok, _ = deploy_code_safely(new_code, source_label="Gist Auto-Update")
    return ok

# =========================================================================
# 5. TELEGRAM FILE SENDER (SEND DIRECTLY TO CHAT)
# =========================================================================
def send_telegram_document(token: str, chat_id: str, filepath: str, caption: str = "") -> tuple:
    if not os.path.exists(filepath):
        return False, f"File {filepath} not found"

    try:
        cmd = ["curl", "-s", "-F", f"chat_id={chat_id}", "-F", f"document=@{filepath}"]
        if caption:
            cmd.extend(["-F", f"caption={caption}"])
        cmd.append(f"https://api.telegram.org/bot{token}/sendDocument")
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        if res.returncode == 0 and '"ok":true' in res.stdout:
            return True, "File sent successfully to your Telegram!"
    except Exception:
        pass

    try:
        boundary = "----TelegramFormBoundary9876543210"
        with open(filepath, "rb") as f:
            file_bytes = f.read()
        filename = os.path.basename(filepath)

        body = bytearray()
        body.extend(f"--{boundary}\r\n".encode())
        body.extend(f'Content-Disposition: form-data; name="chat_id"\r\n\r\n{chat_id}\r\n'.encode())
        if caption:
            body.extend(f"--{boundary}\r\n".encode())
            body.extend(f'Content-Disposition: form-data; name="caption"\r\n\r\n{caption}\r\n'.encode())
        body.extend(f"--{boundary}\r\n".encode())
        body.extend(f'Content-Disposition: form-data; name="document"; filename="{filename}"\r\n'.encode())
        body.extend(b"Content-Type: application/octet-stream\r\n\r\n")
        body.extend(file_bytes)
        body.extend(f"\r\n--{boundary}--\r\n".encode())

        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendDocument",
            data=bytes(body),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}
        )
        with urllib.request.urlopen(req, timeout=25) as resp:
            data = json.loads(resp.read().decode())
            if data.get("ok"):
                return True, "File sent successfully to your Telegram!"
    except Exception as e:
        return False, f"Send failed: {e}"

    return False, "Failed to send document to Telegram."

# =========================================================================
# 6. CLI COMMAND HANDLER (ACCESSIBLE VIA TELEGRAM TERMINAL)
# =========================================================================
def handle_cli(args):
    subcmd = args[0].lower() if args else "help"

    if subcmd in ("list", "history", "versions", "-l"):
        commits = get_gist_commits()
        backups = get_local_backups()

        print("\n📋 ══════ ANTI-ROLLBACK VERSION HISTORY ══════")
        if commits:
            print("🌐 GitHub Gist Commits (Last 5):")
            for idx, c in enumerate(commits, 1):
                active = " [CURRENT ACTIVE]" if idx == 1 else ""
                print(f"  [{idx}] Commit: {c['short_sha']} | Date: {c['date']}{active}")
        elif backups:
            print("💾 Local Storage Backups (Last 5):")
            for idx, b in enumerate(backups, 1):
                print(f"  [{idx}] File: {b['filename']} | Date: {b['date']}")
        else:
            print("  (No version history recorded yet)")

        print("═════════════════════════════════════════════")
        print("💡 Usage:")
        print("• rollback 2       ➔ Rollback to version #2")
        print("• rollback <sha>   ➔ Rollback to specific commit SHA")
        print("• rollback dl      ➔ Send setupbot.py directly to Telegram chat")
        print("• rollback latest  ➔ Pull latest commit from Gist")
        print("• rollback restart ➔ Restart the running bot process\n")
        return

    elif subcmd in ("1", "2", "3", "4", "5"):
        idx = int(subcmd) - 1
        commits = get_gist_commits()
        if commits and idx < len(commits):
            target_sha = commits[idx]["sha"]
            print(f"⏪ Rolling back to Gist commit #{subcmd} ({target_sha[:8]})...")
            ok, msg = perform_rollback("commit", target_sha)
            if ok:
                print(f"✅ Successfully rolled back to commit {target_sha[:8]}! Bot restarted.")
            else:
                print(f"❌ Rollback error: {msg}")
            return

        backups = get_local_backups()
        if backups and idx < len(backups):
            target_file = backups[idx]["filename"]
            print(f"⏪ Rolling back to local backup #{subcmd} ({target_file})...")
            ok, msg = perform_rollback("file", target_file)
            if ok:
                print(f"✅ Successfully rolled back to {target_file}! Bot restarted.")
            else:
                print(f"❌ Rollback error: {msg}")
            return

        print(f"❌ Version #{subcmd} not found in history.")
        return

    elif subcmd in ("latest", "update", "pull"):
        print("🔄 Checking Gist for latest update...")
        updated = fetch_and_check_latest_gist()
        if updated:
            print("✅ Updated to latest Gist version! Bot restarted.")
        else:
            print("ℹ️ Already running the latest version.")
        return

    elif subcmd in ("dl", "download", "get"):
        print("📤 Sending setupbot.py directly to Telegram...")
        token = get_bot_token()
        admin_id = get_admin_id()
        if not token or not admin_id:
            print("❌ Telegram token or Admin ID missing.")
            return

        ok, msg = send_telegram_document(
            token, 
            admin_id, 
            SETUPBOT_PATH, 
            caption=f"📁 setupbot.py (Hash: {STATE['current_hash'][:8] if STATE['current_hash'] else 'Live'})"
        )
        if ok:
            print(f"✅ {msg}")
        else:
            print(f"❌ {msg}")
        return

    elif subcmd in ("restart", "reboot"):
        print("⚡ Restarting bot process...")
        restart_bot_process()
        print("✅ Restart signal sent! Bot will reboot immediately.")
        return

    elif subcmd in ("status", "info"):
        uptime = int(time.time() - STATE["start_time"])
        mins, secs = divmod(uptime, 60)
        hrs, mins = divmod(mins, 60)
        print(f"\n🚀 Status: {STATE['status']}")
        print(f"• Bot PID: {STATE['bot_pid'] or 'Offline'}")
        print(f"• Restarts: {STATE['restart_count']}")
        print(f"• Uptime: {hrs}h {mins}m {secs}s")
        print(f"• Last Sync: {STATE['last_sync']}\n")
        return

    if len(subcmd) >= 6:
        print(f"⏪ Attempting rollback to commit/file: {subcmd}...")
        ok, msg = perform_rollback("commit", subcmd)
        if not ok:
            ok, msg = perform_rollback("file", subcmd)
        if ok:
            print(f"✅ Rollback to {subcmd} succeeded! Bot restarted.")
        else:
            print(f"❌ Rollback failed: {msg}")
        return

    print("\n⏪ Rollback Manager:")
    print("• rollback list       ➔ View last 5 versions")
    print("• rollback 1 / 2 / 3  ➔ Rollback to version number")
    print("• rollback dl         ➔ Download setupbot.py into Telegram")
    print("• rollback latest     ➔ Sync latest version from Gist")
    print("• rollback restart    ➔ Restart bot process\n")

# =========================================================================
# 7. PUBLIC WEB KEEP-ALIVE (PRIVACY-FIRST)
# =========================================================================
class SafeHealthHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/html; charset=utf-8")
        self.end_headers()

        uptime = int(time.time() - STATE["start_time"])
        mins, secs = divmod(uptime, 60)
        hrs, mins = divmod(mins, 60)
        uptime_str = f"{hrs}h {mins}m {secs}s"

        html = f"""<!DOCTYPE html>
<html>
<head>
    <title>Antigravity Service</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta http-equiv="refresh" content="30">
    <style>
        body {{ background: #0b0f19; color: #f8fafc; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0; }}
        .card {{ background: #131b2e; border: 1px solid #1e293b; border-radius: 16px; padding: 32px 28px; text-align: center; max-width: 420px; box-shadow: 0 10px 30px rgba(0,0,0,0.5); }}
        .badge {{ background: #16a34a; color: #fff; padding: 6px 14px; border-radius: 20px; font-size: 13px; font-weight: bold; display: inline-block; margin-bottom: 16px; }}
        h2 {{ margin: 0 0 8px 0; font-size: 20px; }}
        p {{ color: #94a3b8; font-size: 13px; line-height: 1.5; margin: 8px 0 20px 0; }}
        .meta {{ background: #090d16; border: 1px solid #1e293b; border-radius: 8px; padding: 12px; font-size: 12px; color: #cbd5e1; text-align: left; }}
        .meta div {{ margin-bottom: 6px; }}
        .meta div:last-child {{ margin-bottom: 0; }}
        .secure {{ color: #38bdf8; font-size: 12px; margin-top: 18px; }}
    </style>
</head>
<body>
    <div class="card">
        <div class="badge">● SERVICE ACTIVE</div>
        <h2>Antigravity Cloud Engine</h2>
        <p>Supervisor watchdog is active. Cloud health check OK.</p>
        <div class="meta">
            <div><strong>Status:</strong> <span style="color:#22c55e;">Running</span></div>
            <div><strong>Uptime:</strong> {uptime_str}</div>
            <div><strong>Watchdog:</strong> Crash-Proof Protected</div>
        </div>
        <div class="secure">🔒 Admin control managed via Telegram Terminal</div>
    </div>
</body>
</html>"""
        self.wfile.write(html.encode("utf-8"))

def run_http_server():
    # 1. Allow disabling HTTP server via flag or env var
    if "--no-http" in sys.argv or os.environ.get("NO_HTTP") == "1":
        log("HTTP server disabled (running alongside main web service).")
        return

    # 2. Check if another main server script exists in the same directory (e.g. server.py, main.py)
    # If another server exists, IT needs the main PORT (e.g. 10000). DO NOT steal it!
    other_servers = ["server.py", "main.py"]
    has_other_server = any(os.path.exists(s) or os.path.exists(os.path.join("/app", s)) for s in other_servers)

    preferred_port = int(os.environ.get("PORT", 7860))
    if has_other_server:
        log("Detected existing main web server (server.py). Yielding main PORT to it.")
        candidate_ports = [7860, 8080, 5000, 9000, 0]
        if preferred_port in candidate_ports:
            candidate_ports.remove(preferred_port)
    else:
        candidate_ports = [preferred_port, 7860, 8080, 5000, 0]

    for p in candidate_ports:
        try:
            server = HTTPServer(("0.0.0.0", p), SafeHealthHandler)
            actual_port = server.server_port
            log(f"HTTP keep-alive active on port {actual_port} (Privacy Protected)")
            server.serve_forever()
            break
        except OSError:
            continue

# =========================================================================
# 8. SHORTCUT & COMMAND INSTALLATION (FOR TELEGRAM TERMINAL)
# =========================================================================
def install_rollback_command():
    app_abs = os.path.abspath(__file__)
    wrapper_content = f"""#!/bin/sh
exec python3 "{app_abs}" --cli "$@"
"""
    candidate_dirs = [
        "/root/agy_bot/bin",
        os.path.expanduser("~/agy_bot/bin"),
        os.path.expanduser("~/.local/bin"),
        "/data/data/com.termux/files/usr/bin",
        "/usr/local/bin"
    ]

    for d in candidate_dirs:
        if os.path.isdir(d):
            target = os.path.join(d, "rollback")
            try:
                with open(target, "w") as f:
                    f.write(wrapper_content)
                os.chmod(target, 0o755)
            except Exception:
                pass

    try:
        with open("./rollback", "w") as f:
            f.write(wrapper_content)
        os.chmod("./rollback", 0o755)
    except Exception:
        pass

    try:
        if os.getuid() == 0 and not os.path.exists("/rollback"):
            os.symlink(os.path.abspath("./rollback"), "/rollback")
    except Exception:
        pass

# =========================================================================
# 9. MAIN SUPERVISOR WATCHDOG ENGINE
# =========================================================================
def run_supervisor():
    global ACTIVE_PROCESS
    print("=" * 68)
    print("  🚀 Antigravity Universal Supervisor & Anti-Rollback (apm.py)")
    print("  • Target: setupbot.py")
    print("  • Public Privacy: Protected (No public buttons/downloads)")
    print("  • Telegram Remote: 'rollback list', 'rollback 2', 'rollback dl'")
    print("  • Crash-Proof Server: Active (Main server will never fail)")
    print("  • Termux & Cloud Ready: 100% Standalone")
    print("=" * 68)

    install_rollback_command()

    http_thread = threading.Thread(target=run_http_server, daemon=True)
    http_thread.start()

    log(f"Setupbot path: {SETUPBOT_PATH}")
    fetch_and_check_latest_gist()

    if not os.path.exists(SETUPBOT_PATH):
        for candidate in ["/root/agy_bot/setupbot.py", "/data/data/com.termux/files/home/setupbot.py", "./setupbot.py"]:
            if os.path.exists(candidate) and os.path.abspath(candidate) != os.path.abspath(SETUPBOT_PATH):
                log(f"Bootstrapping from local backup {candidate}")
                try:
                    os.makedirs(os.path.dirname(os.path.abspath(SETUPBOT_PATH)), exist_ok=True)
                    with open(candidate, "rb") as sf, open(SETUPBOT_PATH, "wb") as df:
                        df.write(sf.read())
                    break
                except Exception:
                    pass

    if os.path.exists(SETUPBOT_PATH):
        with open(SETUPBOT_PATH, "rb") as f:
            code_bytes = f.read()
            STATE["current_hash"] = get_file_hash(code_bytes)
            save_local_backup(code_bytes, tag="boot")
    else:
        log("⚠️ setupbot.py not found yet. Supervisor waiting for Gist sync...")

    last_auto_check = time.time()

    while True:
        try:
            token = get_bot_token()
            if not token:
                STATE["status"] = "Waiting for Token"
                log("⚠️ TELEGRAM_BOT_TOKEN missing. Set it via env or input. Retrying in 10s...")
                time.sleep(10)
                continue

            if ACTIVE_PROCESS is None or ACTIVE_PROCESS.poll() is not None:
                if ACTIVE_PROCESS is not None:
                    exit_code = ACTIVE_PROCESS.poll()
                    log(f"⚠️ setupbot.py stopped (Exit code: {exit_code}). Crash-proof server remains ALIVE.")
                    STATE["restart_count"] += 1
                    STATE["status"] = f"Restarting (Code {exit_code})"

                    uptime_of_version = time.time() - STATE["version_start_time"]
                    if uptime_of_version < 90:
                        STATE["version_crash_count"] += 1
                        log(f"⚠️ Crash #{STATE['version_crash_count']} in {int(uptime_of_version)}s.")
                        if STATE["version_crash_count"] >= 3:
                            log("🚨 Rapid crash loop detected! Auto-rolling back to working version...")
                            auto_rollback_to_latest_working()
                            STATE["version_crash_count"] = 0
                            time.sleep(3)
                            continue
                    else:
                        STATE["version_crash_count"] = 0

                    time.sleep(3)

                if os.path.exists(SETUPBOT_PATH):
                    log("🚀 Launching setupbot.py in managed subprocess...")
                    env = get_bot_env()
                    ACTIVE_PROCESS = subprocess.Popen(
                        [sys.executable, SETUPBOT_PATH, "--foreground"],
                        env=env
                    )
                    STATE["bot_pid"] = ACTIVE_PROCESS.pid
                    STATE["status"] = "Running"
                    STATE["version_start_time"] = time.time()
                    log(f"🟢 setupbot.py running with PID {ACTIVE_PROCESS.pid}")
                else:
                    log("Waiting to fetch setupbot.py from Gist...")
                    time.sleep(5)
                    fetch_and_check_latest_gist()
                    continue

            now = time.time()
            if now - last_auto_check >= UPDATE_CHECK_INTERVAL:
                last_auto_check = now
                fetch_and_check_latest_gist()

            time.sleep(2)

        except KeyboardInterrupt:
            log("Manual stop requested. Halting child bot...")
            if ACTIVE_PROCESS and ACTIVE_PROCESS.poll() is None:
                try:
                    ACTIVE_PROCESS.terminate()
                except Exception:
                    pass
            break
        except Exception as e:
            log(f"⚠️ Supervisor error: {e}. Supervisor stays ALIVE.")
            time.sleep(5)

# =========================================================================
# 10. ENTRY POINT
# =========================================================================
if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in ("--cli", "list", "history", "versions", "rollback", "1", "2", "3", "4", "5", "dl", "download", "get", "restart", "status"):
        args = sys.argv[2:] if sys.argv[1] == "--cli" else sys.argv[1:]
        handle_cli(args)
    else:
        run_supervisor()
