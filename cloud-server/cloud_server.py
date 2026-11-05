import os
import re
import json
import time
import shutil
import hashlib
import threading
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn

PORT = int(os.environ.get("PORT", "8765"))
DATA_DIR = os.environ.get("DATA_DIR", "/data/saves")
PATCHES_DIR = os.path.join(DATA_DIR, "patches")
MAX_HISTORY = int(os.environ.get("MAX_HISTORY", "5"))
USERS_FILE = os.path.join(DATA_DIR, "users.json")
USERS_LOCK = threading.Lock()
PATCH_HASH_CACHE = {}

os.makedirs(os.path.join(DATA_DIR, "users"), exist_ok=True)
os.makedirs(PATCHES_DIR, exist_ok=True)


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


def load_users():
    with USERS_LOCK:
        if os.path.exists(USERS_FILE):
            try:
                with open(USERS_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {}


def save_users(users_data):
    with USERS_LOCK:
        with open(USERS_FILE, "w", encoding="utf-8") as f:
            json.dump(users_data, f, indent=2, ensure_ascii=False)


def sanitize_username(u):
    return re.sub(r"[^a-zA-Z0-9_.-]", "", (u or "").strip().lower())


def get_user_saves_dir(username):
    u = sanitize_username(username)
    p = os.path.join(DATA_DIR, "users", u)
    os.makedirs(p, exist_ok=True)
    return p


def get_all_user_meta(username):
    result = {}
    user_dir = get_user_saves_dir(username)
    if not os.path.isdir(user_dir):
        return result
    for appid in os.listdir(user_dir):
        meta_path = os.path.join(user_dir, appid, "meta.json")
        if os.path.exists(meta_path):
            try:
                with open(meta_path, "r", encoding="utf-8") as f:
                    result[appid] = json.load(f)
            except Exception:
                pass
    return result


def get_app_history_list(username, appid):
    app_dir = os.path.join(get_user_saves_dir(username), appid)
    hist_dir = os.path.join(app_dir, "history")
    items = []
    meta_path = os.path.join(app_dir, "meta.json")
    latest_zip = os.path.join(app_dir, "latest.zip")
    if os.path.exists(latest_zip):
        meta = {}
        if os.path.exists(meta_path):
            try:
                meta = json.load(open(meta_path, "r", encoding="utf-8"))
            except Exception:
                pass
        items.append({
            "filename": "latest.zip",
            "is_latest": True,
            "mtime": int(meta.get("updated_at") or os.path.getmtime(latest_zip)),
            "uploaded_at": int(meta.get("uploaded_at") or os.path.getmtime(latest_zip)),
            "size_bytes": os.path.getsize(latest_zip),
            "device": meta.get("device", "Cloud"),
            "sha256": meta.get("sha256", "")
        })
    if os.path.isdir(hist_dir):
        hfiles = sorted(
            [f for f in os.listdir(hist_dir) if f.endswith(".zip")],
            key=lambda fn: os.path.getmtime(os.path.join(hist_dir, fn)),
            reverse=True
        )
        for fn in hfiles[:max(0, MAX_HISTORY - 1)]:
            fp = os.path.join(hist_dir, fn)
            ts = int(os.path.getmtime(fp))
            parts = fn.split("_")
            if parts and parts[0].isdigit():
                ts = int(parts[0])
            items.append({
                "filename": fn,
                "is_latest": False,
                "mtime": ts,
                "uploaded_at": int(os.path.getmtime(fp)),
                "size_bytes": os.path.getsize(fp),
                "device": "Riwayat Cloud",
                "sha256": parts[-1].replace(".zip", "") if len(parts) > 1 else ""
            })
    return items


def _fast_file_sha256(fp):
    try:
        st = os.stat(fp)
        key = (fp, int(st.st_mtime), int(st.st_size))
        if key in PATCH_HASH_CACHE:
            return PATCH_HASH_CACHE[key]
        h = hashlib.sha256()
        with open(fp, "rb") as f:
            while True:
                chunk = f.read(1024 * 1024)
                if not chunk:
                    break
                h.update(chunk)
        digest = h.hexdigest()
        PATCH_HASH_CACHE[key] = digest
        return digest
    except Exception:
        return ""


def get_all_patches_meta():
    result = {}
    if not os.path.isdir(PATCHES_DIR):
        return result
    for appid in os.listdir(PATCHES_DIR):
        if not appid.isdigit():
            continue
        app_patch_dir = os.path.join(PATCHES_DIR, appid)
        if not os.path.isdir(app_patch_dir):
            continue
        files_meta = []
        latest_mtime = 0
        total_size = 0
        for root, _, files in os.walk(app_patch_dir):
            for fn in files:
                if fn.endswith(".tmp") or fn.startswith("."):
                    continue
                fp = os.path.join(root, fn)
                try:
                    sz = os.path.getsize(fp)
                    mt = int(os.path.getmtime(fp))
                except Exception:
                    continue
                if sz <= 0:
                    continue
                rel_p = os.path.relpath(fp, app_patch_dir).replace("\\", "/")
                sha = _fast_file_sha256(fp)
                if mt > latest_mtime:
                    latest_mtime = mt
                total_size += sz
                files_meta.append({
                    "name": fn,
                    "rel_path": rel_p,
                    "size_bytes": sz,
                    "mtime": mt,
                    "sha256": sha
                })
        if files_meta:
            files_meta.sort(key=lambda x: x["rel_path"])
            result[appid] = {
                "appid": appid,
                "files": files_meta,
                "primary_file": files_meta[0]["name"],
                "primary_sha256": files_meta[0]["sha256"],
                "total_size_bytes": total_size,
                "updated_at": latest_mtime
            }
    return result


class CloudSaveHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {fmt % args}", flush=True)

    def send_json(self, data, status=200):
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def authenticate_user(self):
        username = sanitize_username(urllib.parse.unquote(self.headers.get("X-Cloud-User", "")))
        password = urllib.parse.unquote(self.headers.get("X-Cloud-Pass", ""))
        if not username or not password:
            self.send_json({"ok": False, "error": "Username dan password wajib diisi."}, 401)
            return None
        users = load_users()
        user_obj = users.get(username)
        if not user_obj or user_obj.get("password") != password:
            self.send_json({"ok": False, "error": "Username atau password salah."}, 401)
            return None
        return username

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/")

        if path == "":
            self.send_json({"ok": True, "service": "Steam Cloud Save Server", "status": "online"})
            return

        if path == "/api/health":
            username = self.authenticate_user()
            if not username:
                return
            saves = get_all_user_meta(username)
            self.send_json({"ok": True, "username": username, "saves_count": len(saves)})
            return

        if path == "/api/saves":
            username = self.authenticate_user()
            if not username:
                return
            self.send_json({"ok": True, "saves": get_all_user_meta(username)})
            return

        if path == "/api/patches":
            self.send_json({"ok": True, "patches": get_all_patches_meta()})
            return

        parts = path.split("/")
        # /api/patches/<appid>/download
        if len(parts) == 5 and parts[1] == "api" and parts[2] == "patches" and parts[4] == "download":
            appid = parts[3]
            if not appid.isdigit():
                self.send_json({"ok": False, "error": "Invalid appid"}, 400)
                return
            patches = get_all_patches_meta()
            p_info = patches.get(appid)
            if not p_info or not p_info.get("files"):
                self.send_json({"ok": False, "error": "Patch belum tersedia di server untuk game ini"}, 404)
                return
            first_file = p_info["files"][0]
            fp = os.path.join(PATCHES_DIR, appid, first_file["rel_path"].replace("/", os.sep))
            if not os.path.exists(fp):
                self.send_json({"ok": False, "error": "File patch tidak ditemukan"}, 404)
                return
            sz = os.path.getsize(fp)
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(sz))
            self.send_header("X-Patch-Filename", urllib.parse.quote(first_file["name"]))
            self.send_header("X-Patch-RelPath", urllib.parse.quote(first_file["rel_path"]))
            self.send_header("X-Patch-Sha256", first_file["sha256"])
            self.end_headers()
            with open(fp, "rb") as f:
                shutil.copyfileobj(f, self.wfile, length=1024 * 1024)
            return
        # /api/saves/<appid>/meta
        if len(parts) == 5 and parts[1] == "api" and parts[2] == "saves" and parts[4] == "meta":
            username = self.authenticate_user()
            if not username:
                return
            appid = parts[3]
            meta_path = os.path.join(get_user_saves_dir(username), appid, "meta.json")
            if os.path.exists(meta_path):
                with open(meta_path, "r", encoding="utf-8") as f:
                    self.send_json({"ok": True, "exists": True, "meta": json.load(f)})
            else:
                self.send_json({"ok": True, "exists": False, "meta": None})
            return

        # /api/saves/<appid>/history
        if len(parts) == 5 and parts[1] == "api" and parts[2] == "saves" and parts[4] == "history":
            username = self.authenticate_user()
            if not username:
                return
            appid = parts[3]
            self.send_json({"ok": True, "history": get_app_history_list(username, appid)})
            return

        # /api/saves/<appid>/history/<filename>
        if len(parts) == 6 and parts[1] == "api" and parts[2] == "saves" and parts[4] == "history":
            username = self.authenticate_user()
            if not username:
                return
            appid = parts[3]
            fname = os.path.basename(parts[5])
            app_dir = os.path.join(get_user_saves_dir(username), appid)
            target_zip = os.path.join(app_dir, "latest.zip") if fname == "latest.zip" else os.path.join(app_dir, "history", fname)
            if not os.path.exists(target_zip):
                self.send_json({"ok": False, "error": "File riwayat tidak ditemukan"}, 404)
                return
            raw = open(target_zip, "rb").read()
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return

        # /api/saves/<appid>/download
        if len(parts) == 5 and parts[1] == "api" and parts[2] == "saves" and parts[4] == "download":
            username = self.authenticate_user()
            if not username:
                return
            appid = parts[3]
            zip_path = os.path.join(get_user_saves_dir(username), appid, "latest.zip")
            if not os.path.exists(zip_path):
                self.send_json({"ok": False, "error": "File save tidak ditemukan di Cloud"}, 404)
                return
            raw = open(zip_path, "rb").read()
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return

        self.send_json({"ok": False, "error": "Not found"}, 404)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/")

        if path in ("/api/register", "/api/login"):
            length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(length).decode("utf-8", errors="ignore") if length > 0 else "{}"
            try:
                body = json.loads(raw_body)
            except Exception:
                body = {}
            username = sanitize_username(body.get("username", ""))
            password = (body.get("password") or "").strip()

            if len(username) < 2 or len(password) < 1:
                self.send_json({"ok": False, "error": "Username minimal 2 karakter dan password tidak boleh kosong."}, 400)
                return

            users = load_users()
            if path == "/api/register":
                if username in users:
                    self.send_json({"ok": False, "error": f"Username '{username}' sudah terdaftar. Silakan klik Login."}, 409)
                    return
                users[username] = {
                    "password": password,
                    "created_at": time.strftime("%Y-%m-%d %H:%M:%S")
                }
                save_users(users)
                get_user_saves_dir(username)
                self.send_json({"ok": True, "username": username, "saves_count": 0, "message": f"Akun '{username}' berhasil didaftarkan!"})
                return

            if path == "/api/login":
                if username not in users:
                    self.send_json({"ok": False, "error": f"Username '{username}' belum terdaftar. Silakan klik Register dulu."}, 404)
                    return
                if users[username].get("password") != password:
                    self.send_json({"ok": False, "error": "Password salah!"}, 401)
                    return
                saves = get_all_user_meta(username)
                self.send_json({"ok": True, "username": username, "saves_count": len(saves), "message": f"Login berhasil sebagai '{username}' ({len(saves)} game tersimpan)"})
                return

        parts = path.split("/")
        # /api/patches/<appid>/upload
        if len(parts) == 5 and parts[1] == "api" and parts[2] == "patches" and parts[4] == "upload":
            username = self.authenticate_user()
            if not username:
                return
            appid = parts[3]
            if not appid.isdigit():
                self.send_json({"ok": False, "error": "Invalid appid"}, 400)
                return
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 1024 * 1024 * 1024:
                self.send_json({"ok": False, "error": "Invalid patch payload size"}, 400)
                return
            raw_fname = urllib.parse.unquote(self.headers.get("X-Patch-Filename", "patch.pak")).strip()
            safe_fname = os.path.basename(raw_fname) or "patch.pak"
            app_patch_dir = os.path.join(PATCHES_DIR, appid)
            os.makedirs(app_patch_dir, exist_ok=True)
            # Clear old files in this appid patch folder if different filename
            for existing_fn in os.listdir(app_patch_dir):
                existing_fp = os.path.join(app_patch_dir, existing_fn)
                if os.path.isfile(existing_fp) and existing_fn != safe_fname:
                    try:
                        os.remove(existing_fp)
                    except Exception:
                        pass
            tmp_fp = os.path.join(app_patch_dir, safe_fname + ".tmp")
            final_fp = os.path.join(app_patch_dir, safe_fname)
            remaining = length
            h = hashlib.sha256()
            with open(tmp_fp, "wb") as out_f:
                while remaining > 0:
                    chunk = self.rfile.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    out_f.write(chunk)
                    h.update(chunk)
                    remaining -= len(chunk)
            os.replace(tmp_fp, final_fp)
            sha_hex = h.hexdigest()
            self.send_json({
                "ok": True,
                "appid": appid,
                "filename": safe_fname,
                "size_bytes": os.path.getsize(final_fp),
                "sha256": sha_hex,
                "message": f"Patch '{safe_fname}' untuk AppID {appid} berhasil di-upload ke Server!"
            })
            return

        # /api/saves/<appid>/upload
        if len(parts) == 5 and parts[1] == "api" and parts[2] == "saves" and parts[4] == "upload":
            username = self.authenticate_user()
            if not username:
                return
            appid = parts[3]
            if not appid.isdigit():
                self.send_json({"ok": False, "error": "Invalid appid"}, 400)
                return

            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 500 * 1024 * 1024:
                self.send_json({"ok": False, "error": "Invalid payload size"}, 400)
                return

            zip_bytes = self.rfile.read(length)
            sha256_hash = hashlib.sha256(zip_bytes).hexdigest()

            mtime_hdr = self.headers.get("X-Save-Mtime", str(int(time.time())))
            content_sha_hdr = self.headers.get("X-Save-Content-Sha", "")
            device_hdr = self.headers.get("X-Device-Name", "PC")
            gname_hdr = urllib.parse.unquote(self.headers.get("X-Game-Name", f"App {appid}"))
            account_hdr = self.headers.get("X-Account-Name", "")

            app_dir = os.path.join(get_user_saves_dir(username), appid)
            hist_dir = os.path.join(app_dir, "history")
            os.makedirs(hist_dir, exist_ok=True)

            latest_zip = os.path.join(app_dir, "latest.zip")
            old_meta_path = os.path.join(app_dir, "meta.json")
            if os.path.exists(latest_zip):
                old_raw = open(latest_zip, "rb").read()
                old_sha = hashlib.sha256(old_raw).hexdigest()
                old_mtime = int(os.path.getmtime(latest_zip))
                old_up_time = int(time.time())
                if os.path.exists(old_meta_path):
                    try:
                        om = json.load(open(old_meta_path, "r", encoding="utf-8"))
                        old_mtime = int(om.get("updated_at", old_mtime))
                        old_up_time = int(om.get("uploaded_at", old_up_time))
                    except Exception:
                        pass
                if old_sha != sha256_hash or int(float(mtime_hdr)) != old_mtime:
                    hist_file = os.path.join(hist_dir, f"{old_mtime}_{old_up_time}_{old_sha[:8]}.zip")
                    shutil.copy2(latest_zip, hist_file)
                    hfiles = sorted(
                        [os.path.join(hist_dir, f) for f in os.listdir(hist_dir) if f.endswith(".zip")],
                        key=lambda p: os.path.getmtime(p)
                    )
                    max_archived = max(1, MAX_HISTORY - 1)
                    while len(hfiles) > max_archived:
                        try:
                            os.remove(hfiles.pop(0))
                        except Exception:
                            break

            with open(latest_zip, "wb") as f:
                f.write(zip_bytes)

            meta = {
                "appid": appid,
                "user": username,
                "game_name": gname_hdr,
                "account_name": account_hdr,
                "updated_at": int(float(mtime_hdr)),
                "uploaded_at": int(time.time()),
                "sha256": sha256_hash,
                "files_sha256": content_sha_hdr,
                "size_bytes": len(zip_bytes),
                "device": device_hdr
            }
            with open(os.path.join(app_dir, "meta.json"), "w", encoding="utf-8") as f:
                json.dump(meta, f, indent=2, ensure_ascii=False)

            self.send_json({"ok": True, "meta": meta})
            return

        self.send_json({"ok": False, "error": "Not found"}, 404)


if __name__ == "__main__":
    print(f"Steam Multi-User Cloud Save Server listening on 0.0.0.0:{PORT}", flush=True)
    print(f"Users file (plaintext editable): {USERS_FILE}", flush=True)
    server = ThreadedHTTPServer(("0.0.0.0", PORT), CloudSaveHandler)
    server.serve_forever()
