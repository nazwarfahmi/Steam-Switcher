import os
import io
import sys
import re
import glob
import json
import time
import struct
import socket
import base64
import shutil
import zipfile
import hashlib
import winreg
import fnmatch
import tempfile
import threading
import ctypes
import urllib.parse
import urllib.request
import subprocess
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn

if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

PORT = 18420
if getattr(sys, "frozen", False):
    ASSET_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(sys.executable)))
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    ASSET_DIR = os.path.dirname(os.path.abspath(__file__))
    BASE_DIR = ASSET_DIR

APP_REG_PATH = r"Software\SteamSwitcher"
DEFAULT_CLOUD_URL = ""
MAX_SAVE_HISTORY = 5
STARTUP_REG_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
STARTUP_REG_NAME = "SteamSwitcher"

STATE_CACHE = None
COVER_CACHE = {}
COVER_B64_CACHE = {}
UFS_CACHE = {}
SCHEMA_CACHE = {}
CACHE_LOCK = threading.Lock()
ACTIVE_WATCHERS = set()
LAUNCHING_UNTIL = {}
UPLOADING_APPIDS = set()
UPLOADING_UNTIL = {}
LAST_UPLOAD_MSG = {}
LAST_ACHIEVEMENT_UNLOCK = {}
ACHIEVEMENT_POPUP_QUEUE = []
POPUP_LOCK = threading.Lock()
CLOUD_ACH_APPID = "999999999"
PATCHES_META_CACHE = {}
PATCHES_META_TS = 0
PATCHES_FETCHING = False
LAST_UI_ACTIVITY_TS = time.time()
TRAY_ICON = None

def _load_asset_bytes(filename):
    for folder in (ASSET_DIR, BASE_DIR):
        p = os.path.join(folder, filename)
        if os.path.exists(p):
            try:
                with open(p, "rb") as f:
                    return f.read()
            except Exception:
                pass
    return b""

LOGO_PNG_BYTES = _load_asset_bytes("logo.png")
LOGO_ICO_BYTES = _load_asset_bytes("logo.ico")
LOGO_B64_URI = (
    "data:image/png;base64," + base64.b64encode(LOGO_PNG_BYTES).decode("ascii")
    if LOGO_PNG_BYTES else "/logo.png"
)

NON_GAME_APPIDS = {
    "228980",    # Steamworks Common Redistributables
    "2371090",   # Steam Game Notes
    "241100",    # Steam Controller Configs
    "250820",    # SteamVR
    "769",       # Steamworks SDK
    "1070560",   # Steam Linux Runtime
    "1391110",   # Steam Linux Runtime - Soldier
    "1628350",   # Steam Linux Runtime - Sniper
    "999999999", # Internal Cloud Achievement Sync Bundle
}

def _reg_read_json(value_name):
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, APP_REG_PATH, 0, winreg.KEY_READ)
        raw, _ = winreg.QueryValueEx(key, value_name)
        winreg.CloseKey(key)
        if raw:
            return json.loads(raw)
    except Exception:
        pass
    return None

def _reg_write_json(value_name, data):
    try:
        key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, APP_REG_PATH)
        winreg.SetValueEx(key, value_name, 0, winreg.REG_SZ, json.dumps(data))
        winreg.CloseKey(key)
    except Exception:
        pass

def load_cloud_config():
    default = {
        "server_url": DEFAULT_CLOUD_URL,
        "username": "",
        "password": "",
        "device_name": socket.gethostname() or "PC",
        "auto_sync": True,
        "enabled_appids": []
    }
    data = _reg_read_json("CloudConfig")
    if isinstance(data, dict):
        default.update(data)
    if not default.get("server_url"):
        default["server_url"] = DEFAULT_CLOUD_URL
    if not isinstance(default.get("enabled_appids"), list):
        default["enabled_appids"] = []
    default["enabled_appids"] = [str(x) for x in default["enabled_appids"]]
    return default

def save_cloud_config(cfg):
    try:
        cur = load_cloud_config()
        if "server_url" in cfg:
            cur["server_url"] = cfg["server_url"].strip().rstrip("/") or DEFAULT_CLOUD_URL
        if "username" in cfg:
            cur["username"] = cfg["username"].strip().lower()
        if "password" in cfg:
            cur["password"] = cfg["password"].strip()
        if "device_name" in cfg and cfg["device_name"].strip():
            cur["device_name"] = cfg["device_name"].strip()
        if "auto_sync" in cfg:
            cur["auto_sync"] = bool(cfg["auto_sync"])
        if "enabled_appids" in cfg and isinstance(cfg["enabled_appids"], list):
            cur["enabled_appids"] = sorted(list({str(x) for x in cfg["enabled_appids"]}))
        _reg_write_json("CloudConfig", cur)
    except Exception:
        pass

def is_game_cloud_enabled(appid):
    cfg = load_cloud_config()
    return str(appid) in set(cfg.get("enabled_appids", []))

def load_sync_state():
    data = _reg_read_json("SyncState")
    return data if isinstance(data, dict) else {}

def update_sync_state(username, appid, files_sha256, cloud_sha256, mtime, active_filename="latest.zip"):
    try:
        st = load_sync_state()
        key = f"{(username or '').lower()}:{appid}"
        st[key] = {
            "files_sha256": files_sha256 or "",
            "cloud_sha256": cloud_sha256 or "",
            "mtime": int(mtime or 0),
            "active_filename": active_filename or "latest.zip",
            "synced_at": int(time.time())
        }
        _reg_write_json("SyncState", st)
    except Exception:
        pass

def get_last_sync_record(username, appid):
    st = load_sync_state()
    return st.get(f"{(username or '').lower()}:{appid}", {})

def _get_active_ach_profile():
    cfg = load_cloud_config()
    u = (cfg.get("username") or "").strip().lower()
    return u if u else "_local"

def load_personal_achievements_raw():
    data = _reg_read_json("PersonalAchievements")
    if not isinstance(data, dict):
        data = {}
    profiles = data.get("profiles")
    if not isinstance(profiles, dict):
        profiles = {}
        # Migrate legacy top-level appid maps into _local and active profile
        legacy = {k: v for k, v in data.items() if str(k).isdigit() and isinstance(v, dict)}
        if legacy:
            profiles["_local"] = dict(legacy)
            prof = _get_active_ach_profile()
            if prof != "_local":
                profiles[prof] = dict(legacy)
        data = {"profiles": profiles, "reset_at": {}, "locked_at": {}}
        _reg_write_json("PersonalAchievements", data)
    if not isinstance(data.get("reset_at"), dict):
        data["reset_at"] = {}
    if not isinstance(data.get("locked_at"), dict):
        data["locked_at"] = {}
    return data

def save_personal_achievements_raw(data):
    if isinstance(data, dict):
        _reg_write_json("PersonalAchievements", data)

def get_game_personal_achievements(appid):
    raw = load_personal_achievements_raw()
    prof = _get_active_ach_profile()
    prof_map = raw["profiles"].get(prof)
    if prof_map is None and prof != "_local" and "_local" in raw["profiles"]:
        # Copy _local achievements on first login if profile empty
        prof_map = dict(raw["profiles"]["_local"])
        raw["profiles"][prof] = prof_map
        save_personal_achievements_raw(raw)
    if not isinstance(prof_map, dict):
        return {}
    game_ach = prof_map.get(str(appid), {})
    return dict(game_ach) if isinstance(game_ach, dict) else {}

def get_game_reset_timestamp(appid):
    raw = load_personal_achievements_raw()
    prof = _get_active_ach_profile()
    return int((raw.get("reset_at", {}).get(prof, {}) or {}).get(str(appid), 0) or 0)

def get_game_locked_achievements(appid):
    raw = load_personal_achievements_raw()
    prof = _get_active_ach_profile()
    locked_prof = raw.get("locked_at", {}).get(prof, {})
    if not isinstance(locked_prof, dict):
        return {}
    game_locked = locked_prof.get(str(appid), {})
    return dict(game_locked) if isinstance(game_locked, dict) else {}

def set_game_personal_achievements(appid, ach_map, mark_reset=False, locked_key=None, unlocked_key=None):
    raw = load_personal_achievements_raw()
    prof = _get_active_ach_profile()
    prof_map = raw["profiles"].setdefault(prof, {})
    locked_prof = raw.setdefault("locked_at", {}).setdefault(prof, {})
    game_locked = locked_prof.setdefault(str(appid), {})
    if not isinstance(game_locked, dict):
        game_locked = {}
        locked_prof[str(appid)] = game_locked

    now_ts = int(time.time())
    clean_map = {}
    if isinstance(ach_map, dict):
        for k, v in ach_map.items():
            try:
                clean_map[str(k)] = int(v)
            except Exception:
                pass
    prof_map[str(appid)] = clean_map
    if locked_key:
        game_locked[str(locked_key)] = now_ts
    if unlocked_key:
        game_locked.pop(str(unlocked_key), None)
    for k in clean_map:
        game_locked.pop(str(k), None)
    if mark_reset:
        rst_prof = raw["reset_at"].setdefault(prof, {})
        rst_prof[str(appid)] = now_ts
        locked_prof[str(appid)] = {}
    save_personal_achievements_raw(raw)
    return clean_map

def enqueue_achievement_popup(appid, game_name, ach_item, unlocked_count, total_count):
    with POPUP_LOCK:
        ACHIEVEMENT_POPUP_QUEUE.append({
            "id": f"{appid}:{ach_item.get('key', '')}:{int(time.time()*1000)}",
            "appid": str(appid),
            "game_name": game_name or f"App {appid}",
            "key": ach_item.get("key", ""),
            "title": ach_item.get("title", "Achievement Unlocked"),
            "description": ach_item.get("description", ""),
            "icon_url": ach_item.get("icon_url", ""),
            "unlocked_count": int(unlocked_count),
            "total_count": int(total_count),
            "ts": int(time.time())
        })
        if len(ACHIEVEMENT_POPUP_QUEUE) > 25:
            del ACHIEVEMENT_POPUP_QUEUE[:-25]

def _parse_vdf_bin_typed(data, pos=0):
    res = {}
    n = len(data)
    while pos < n:
        t = data[pos]
        pos += 1
        if t == 8:
            break
        end = data.find(b"\x00", pos)
        if end == -1:
            break
        key = data[pos:end].decode("utf-8", errors="replace")
        pos = end + 1
        if t == 0:
            val, pos = _parse_vdf_bin_typed(data, pos)
            res[key] = val
        elif t == 1:
            end2 = data.find(b"\x00", pos)
            if end2 == -1:
                break
            res[key] = ("str", data[pos:end2].decode("utf-8", errors="replace"))
            pos = end2 + 1
        elif t == 2:
            if pos + 4 > n:
                break
            res[key] = ("int", struct.unpack("<I", data[pos:pos+4])[0])
            pos += 4
        elif t == 3:
            if pos + 4 > n:
                break
            res[key] = ("float", struct.unpack("<f", data[pos:pos+4])[0])
            pos += 4
        elif t == 7:
            if pos + 8 > n:
                break
            res[key] = ("uint64", struct.unpack("<Q", data[pos:pos+8])[0])
            pos += 8
        else:
            break
    return res, pos

def _dump_vdf_bin_typed(d):
    out = bytearray()
    for k, v in d.items():
        kb = str(k).encode("utf-8") + b"\x00"
        if isinstance(v, dict):
            out.append(0)
            out.extend(kb)
            out.extend(_dump_vdf_bin_typed(v))
        elif isinstance(v, tuple) and len(v) == 2:
            t_name, val = v
            if t_name == "str":
                out.append(1)
                out.extend(kb)
                out.extend(str(val).encode("utf-8") + b"\x00")
            elif t_name == "int":
                out.append(2)
                out.extend(kb)
                out.extend(struct.pack("<I", int(val) & 0xFFFFFFFF))
            elif t_name == "float":
                out.append(3)
                out.extend(kb)
                out.extend(struct.pack("<f", float(val)))
            elif t_name == "uint64":
                out.append(7)
                out.extend(kb)
                out.extend(struct.pack("<Q", int(val) & 0xFFFFFFFFFFFFFFFF))
    out.append(8)
    return bytes(out)

def _unwrap_vdf_typed(obj):
    if isinstance(obj, tuple) and len(obj) == 2:
        return obj[1]
    if isinstance(obj, dict):
        return {k: _unwrap_vdf_typed(v) for k, v in obj.items()}
    return obj

def parse_game_achievement_schema(steam_path, appid):
    aid_s = str(appid)
    if aid_s in SCHEMA_CACHE:
        return SCHEMA_CACHE[aid_s]

    schema_path = os.path.join(steam_path, "appcache", "stats", f"UserGameStatsSchema_{aid_s}.bin")
    if not os.path.exists(schema_path):
        return []

    achievements = []
    try:
        raw = open(schema_path, "rb").read()
        parsed_typed, _ = _parse_vdf_bin_typed(raw)
        parsed = _unwrap_vdf_typed(parsed_typed)
        app_block = parsed.get(aid_s) or next(iter(parsed.values()), {})
        stats_block = app_block.get("stats", {}) if isinstance(app_block, dict) else {}

        for stat_id, stat_def in stats_block.items():
            if not isinstance(stat_def, dict):
                continue
            stype = str(stat_def.get("type", "")).upper()
            bits_block = stat_def.get("bits", {})
            if stype not in ("ACHIEVEMENTS", "4") and not bits_block:
                continue
            if not isinstance(bits_block, dict):
                continue
            for bit_key, bit_def in bits_block.items():
                if not isinstance(bit_def, dict) or not str(bit_key).isdigit():
                    continue
                bit_num = int(bit_key)
                disp = bit_def.get("display", {}) if isinstance(bit_def.get("display"), dict) else {}
                name_obj = disp.get("name", {})
                desc_obj = disp.get("desc") or disp.get("description") or {}
                title = (
                    name_obj.get("english") or next(iter(name_obj.values()), "")
                    if isinstance(name_obj, dict) else str(name_obj or "")
                )
                desc = (
                    desc_obj.get("english") or next(iter(desc_obj.values()), "")
                    if isinstance(desc_obj, dict) else str(desc_obj or "")
                )
                icon_fn = str(disp.get("icon", "") or "")
                icon_gray_fn = str(disp.get("icon_gray", "") or icon_fn)
                hidden_flag = str(disp.get("hidden", "0")) == "1"
                achievements.append({
                    "key": f"{stat_id}:{bit_num}",
                    "stat_id": str(stat_id),
                    "bit_id": bit_num,
                    "api_name": str(bit_def.get("name", f"ACH_{stat_id}_{bit_num}")),
                    "title": title or str(bit_def.get("name", f"Achievement #{len(achievements)+1}")),
                    "description": desc,
                    "hidden": hidden_flag,
                    "icon_url": f"https://cdn.akamai.steamstatic.com/steamcommunity/public/images/apps/{aid_s}/{icon_fn}" if icon_fn else "",
                    "icon_gray_url": f"https://cdn.akamai.steamstatic.com/steamcommunity/public/images/apps/{aid_s}/{icon_gray_fn}" if icon_gray_fn else ""
                })
        achievements.sort(key=lambda x: (int(x["stat_id"]) if x["stat_id"].isdigit() else 0, x["bit_id"]))
        SCHEMA_CACHE[aid_s] = achievements
    except Exception:
        return []
    return achievements

def read_steam_user_achievements(steam_path, sid3, appid):
    """Read unlocked achievement keys and timestamps from Steam's UserGameStats_<sid3>_<appid>.bin."""
    aid_s = str(appid)
    schema = parse_game_achievement_schema(steam_path, aid_s)
    ach_stat_ids = {item["stat_id"] for item in schema}
    stats_dir = os.path.join(steam_path, "appcache", "stats")
    candidates = []
    if sid3:
        p_main = os.path.join(stats_dir, f"UserGameStats_{sid3}_{aid_s}.bin")
        if os.path.exists(p_main):
            candidates.append(p_main)
    if not candidates:
        candidates = glob.glob(os.path.join(stats_dir, f"UserGameStats_*_{aid_s}.bin"))

    unlocked = {}
    for cp in candidates:
        try:
            raw = open(cp, "rb").read()
            parsed_typed, _ = _parse_vdf_bin_typed(raw)
            parsed = _unwrap_vdf_typed(parsed_typed)
            cache = parsed.get("cache", {})
            if not isinstance(cache, dict):
                continue
            for stat_id, s_val in cache.items():
                if ach_stat_ids and str(stat_id) not in ach_stat_ids:
                    continue
                if not isinstance(s_val, dict):
                    continue
                mask = int(s_val.get("data", 0) or 0)
                times = s_val.get("AchievementTimes", {})
                if not isinstance(times, dict):
                    times = {}
                for b in range(32):
                    if mask & (1 << b):
                        k = f"{stat_id}:{b}"
                        ts = int(times.get(str(b), 0) or int(os.path.getmtime(cp)))
                        unlocked[k] = ts
        except Exception:
            pass
    return unlocked

def apply_personal_achievements_to_steam_bin(steam_path, sid3, appid, personal_map=None):
    """Write personal achievement bitmasks into UserGameStats_*_<appid>.bin so Steam/game only sees user's own achievements."""
    aid_s = str(appid)
    if personal_map is None:
        personal_map = get_game_personal_achievements(aid_s)

    schema = parse_game_achievement_schema(steam_path, aid_s)
    ach_stat_ids = {item["stat_id"] for item in schema}
    if not ach_stat_ids:
        return False

    stats_dir = os.path.join(steam_path, "appcache", "stats")
    os.makedirs(stats_dir, exist_ok=True)
    target_bins = set(glob.glob(os.path.join(stats_dir, f"UserGameStats_*_{aid_s}.bin")))
    if sid3:
        target_bins.add(os.path.join(stats_dir, f"UserGameStats_{sid3}_{aid_s}.bin"))
    if not target_bins:
        return False

    wrote_any = False
    for target_bin in target_bins:
        parsed_typed = {"cache": {"crc": ("int", 0), "PendingChanges": ("int", 0)}}
        if os.path.exists(target_bin):
            try:
                raw = open(target_bin, "rb").read()
                pt, _ = _parse_vdf_bin_typed(raw)
                if isinstance(pt, dict) and "cache" in pt:
                    parsed_typed = pt
            except Exception:
                pass

        cache_block = parsed_typed.setdefault("cache", {})
        for stat_id in ach_stat_ids:
            mask = 0
            times_typed = {}
            for k, ts in personal_map.items():
                parts = str(k).split(":", 1)
                if len(parts) == 2 and parts[0] == str(stat_id) and parts[1].isdigit():
                    b = int(parts[1])
                    if 0 <= b < 32:
                        mask |= (1 << b)
                        times_typed[str(b)] = ("int", int(ts or time.time()))
            stat_entry = cache_block.get(str(stat_id))
            if not isinstance(stat_entry, dict):
                stat_entry = {}
            stat_entry["data"] = ("int", mask)
            if times_typed:
                stat_entry["AchievementTimes"] = times_typed
            else:
                stat_entry.pop("AchievementTimes", None)
            cache_block[str(stat_id)] = stat_entry

        try:
            out_bytes = _dump_vdf_bin_typed(parsed_typed)
            with open(target_bin, "wb") as f:
                f.write(out_bytes)
            wrote_any = True
        except Exception:
            pass
    return wrote_any

def sync_cloud_achievements_bundle(steam_path=None, direction="both"):
    """Automatically sync all games' personal achievements for the logged-in Cloud user via CLOUD_ACH_APPID."""
    cfg = load_cloud_config()
    username = (cfg.get("username") or "").strip().lower()
    if not cfg.get("server_url") or not username or not cfg.get("password"):
        return False, "Cloud belum login"

    raw = load_personal_achievements_raw()
    local_prof = raw["profiles"].setdefault(username, {})
    local_rst = raw["reset_at"].setdefault(username, {})
    local_locked = raw.setdefault("locked_at", {}).setdefault(username, {})
    changed_local = False

    # 1. Pull & merge from Cloud bundle if direction in ('both', 'pull')
    if direction in ("both", "pull"):
        dl_bytes, dl_err = cloud_request(f"/api/saves/{CLOUD_ACH_APPID}/download", timeout=8)
        if not dl_err and isinstance(dl_bytes, bytes):
            try:
                with zipfile.ZipFile(io.BytesIO(dl_bytes), "r") as zf:
                    if "achievements_bundle.json" in zf.namelist():
                        bundle = json.loads(zf.read("achievements_bundle.json").decode("utf-8", errors="ignore"))
                        c_prof = bundle.get("unlocked", {}) if isinstance(bundle, dict) else {}
                        c_rst = bundle.get("reset_at", {}) if isinstance(bundle, dict) else {}
                        c_locked = bundle.get("locked_at", {}) if isinstance(bundle, dict) else {}
                        if isinstance(c_rst, dict):
                            for aid, r_ts in c_rst.items():
                                r_ts_i = int(r_ts or 0)
                                cur_r = int(local_rst.get(str(aid), 0) or 0)
                                if r_ts_i > cur_r:
                                    local_rst[str(aid)] = r_ts_i
                                    cur_map = local_prof.get(str(aid), {})
                                    if isinstance(cur_map, dict):
                                        local_prof[str(aid)] = {k: v for k, v in cur_map.items() if int(v or 0) > r_ts_i}
                                    changed_local = True
                        if isinstance(c_locked, dict):
                            for aid, l_map in c_locked.items():
                                if not isinstance(l_map, dict):
                                    continue
                                dest_l = local_locked.setdefault(str(aid), {})
                                cur_u = local_prof.setdefault(str(aid), {})
                                for k, l_ts in l_map.items():
                                    l_ts_i = int(l_ts or 0)
                                    if l_ts_i > int(dest_l.get(str(k), 0) or 0):
                                        dest_l[str(k)] = l_ts_i
                                        changed_local = True
                                    if str(k) in cur_u and int(cur_u[str(k)] or 0) <= max(l_ts_i, int(dest_l.get(str(k), 0) or 0)):
                                        cur_u.pop(str(k), None)
                                        changed_local = True
                        if isinstance(c_prof, dict):
                            for aid, ach_map in c_prof.items():
                                if not isinstance(ach_map, dict):
                                    continue
                                rst_ts = int(local_rst.get(str(aid), 0) or 0)
                                l_map = local_locked.get(str(aid), {})
                                if not isinstance(l_map, dict):
                                    l_map = {}
                                dest = local_prof.setdefault(str(aid), {})
                                for k, ts in ach_map.items():
                                    ts_i = int(ts or 0)
                                    k_lock_ts = int(l_map.get(str(k), 0) or 0)
                                    if ts_i > rst_ts and ts_i > k_lock_ts and (str(k) not in dest or ts_i < int(dest[str(k)] or 0)):
                                        dest[str(k)] = ts_i
                                        changed_local = True
            except Exception:
                pass

    if changed_local:
        save_personal_achievements_raw(raw)
        if steam_path:
            running_aid = get_running_steam_appid()
            for aid, ach_map in local_prof.items():
                if str(aid) != str(running_aid):
                    apply_personal_achievements_to_steam_bin(steam_path, "", aid, ach_map)

    # 2. Push merged bundle to Cloud if direction in ('both', 'push')
    if direction in ("both", "push"):
        try:
            payload_bytes = json.dumps({
                "user": username,
                "updated_at": int(time.time()),
                "unlocked": local_prof,
                "reset_at": local_rst,
                "locked_at": local_locked
            }, indent=2, ensure_ascii=False).encode("utf-8")
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("achievements_bundle.json", payload_bytes)
            zip_bytes = buf.getvalue()
            sha = hashlib.sha256(payload_bytes).hexdigest()
            up_headers = {
                "Content-Type": "application/zip",
                "X-Save-Mtime": str(int(time.time())),
                "X-Save-Content-Sha": sha,
                "X-Device-Name": cfg.get("device_name", "PC"),
                "X-Game-Name": "Personal Achievements Bundle",
                "X-Account-Name": username
            }
            cloud_request(f"/api/saves/{CLOUD_ACH_APPID}/upload", method="POST", body=zip_bytes, headers=up_headers, timeout=10)
        except Exception:
            pass

    return True, "OK"

def capture_new_achievements_from_steam_bin(steam_path, sid3, appid, session_start_ts=0, game_name="", trigger_popup=True):
    """Detect newly unlocked achievements in UserGameStats_<sid3>_<appid>.bin, trigger popup, and auto-sync to Cloud."""
    aid_s = str(appid)
    steam_unlocked = read_steam_user_achievements(steam_path, sid3, aid_s)
    if not steam_unlocked:
        return []
    personal_map = get_game_personal_achievements(aid_s)
    rst_ts = get_game_reset_timestamp(aid_s)
    locked_map = get_game_locked_achievements(aid_s)
    newly_unlocked_keys = []
    stale_locked_seen = False
    now_ts = int(time.time())
    for k, ts in steam_unlocked.items():
        if k not in personal_map:
            if rst_ts and ts < rst_ts and (not session_start_ts or ts < session_start_ts - 60):
                stale_locked_seen = True
                continue
            lock_ts = int(locked_map.get(str(k), 0) or 0)
            if lock_ts and ts <= lock_ts + 10:
                stale_locked_seen = True
                continue
            personal_map[k] = ts if (session_start_ts and ts >= session_start_ts - 120) else now_ts
            newly_unlocked_keys.append(k)
    if stale_locked_seen and not newly_unlocked_keys:
        apply_personal_achievements_to_steam_bin(steam_path, sid3, aid_s, personal_map)
    if newly_unlocked_keys:
        set_game_personal_achievements(aid_s, personal_map)
        schema = parse_game_achievement_schema(steam_path, aid_s)
        schema_by_key = {item["key"]: item for item in schema}
        titles = []
        for k in newly_unlocked_keys:
            item = schema_by_key.get(k, {"key": k, "title": k, "description": "", "icon_url": ""})
            titles.append(item.get("title", k))
            if trigger_popup:
                enqueue_achievement_popup(aid_s, game_name or f"App {aid_s}", item, len(personal_map), len(schema))
        LAST_ACHIEVEMENT_UNLOCK[aid_s] = {
            "ts": now_ts,
            "count": len(newly_unlocked_keys),
            "titles": titles
        }
        # Auto-sync personal achievements to Cloud immediately in background
        threading.Thread(target=sync_cloud_achievements_bundle, args=(steam_path, "both"), daemon=True).start()
    return newly_unlocked_keys

def get_game_achievements_view(steam_path, appid, account_info):
    aid_s = str(appid)
    sid3 = account_info.get("steamid3", "")
    schema = parse_game_achievement_schema(steam_path, aid_s)
    personal_map = get_game_personal_achievements(aid_s)
    steam_local_map = read_steam_user_achievements(steam_path, sid3, aid_s)

    items = []
    for ach in schema:
        k = ach["key"]
        unlocked_ts = personal_map.get(k, 0)
        is_un = bool(unlocked_ts > 0)
        items.append({
            **ach,
            "unlocked": is_un,
            "unlocked_at": int(unlocked_ts) if is_un else 0,
            "in_steam_local": k in steam_local_map
        })
    return {
        "achievements": items,
        "unlocked_count": sum(1 for x in items if x["unlocked"]),
        "total_count": len(items),
        "steam_local_count": len(steam_local_map)
    }

def get_startup_command():
    exe_candidate = os.path.join(BASE_DIR, "SteamSwitcher.exe")
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --tray'
    if os.path.exists(exe_candidate):
        return f'"{exe_candidate}" --tray'
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    runner = pyw if os.path.exists(pyw) else sys.executable
    return f'"{runner}" "{os.path.abspath(__file__)}" --tray'

def is_windows_startup_enabled():
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_REG_PATH, 0, winreg.KEY_READ)
        val, _ = winreg.QueryValueEx(key, STARTUP_REG_NAME)
        winreg.CloseKey(key)
        return bool(val)
    except Exception:
        return False

def set_windows_startup_tray(enabled):
    try:
        key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, STARTUP_REG_PATH)
        if enabled:
            winreg.SetValueEx(key, STARTUP_REG_NAME, 0, winreg.REG_SZ, get_startup_command())
        else:
            try:
                winreg.DeleteValue(key, STARTUP_REG_NAME)
            except Exception:
                pass
        winreg.CloseKey(key)
        return True
    except Exception:
        return False

def load_window_state():
    startup_reg = is_windows_startup_enabled()
    default = {
        "width": 1280,
        "height": 820,
        "x": None,
        "y": None,
        "theme": "dark",
        "selected_account": "ALL",
        "hide_to_tray_on_exit": True,
        "start_at_startup_tray": startup_reg
    }
    data = _reg_read_json("WindowState")
    if isinstance(data, dict):
        try:
            return {
                "width": max(600, int(data.get("width", 1280))),
                "height": max(420, int(data.get("height", 820))),
                "x": data.get("x"),
                "y": data.get("y"),
                "theme": data.get("theme", "dark"),
                "selected_account": data.get("selected_account", "ALL"),
                "hide_to_tray_on_exit": bool(data.get("hide_to_tray_on_exit", True)),
                "start_at_startup_tray": startup_reg
            }
        except Exception:
            pass
    return default

def save_window_state(state):
    try:
        cur = load_window_state()
        if "width" in state and state["width"]:
            cur["width"] = max(600, int(state["width"]))
        if "height" in state and state["height"]:
            cur["height"] = max(420, int(state["height"]))
        if "x" in state and state["x"] is not None:
            cur["x"] = int(state["x"])
        if "y" in state and state["y"] is not None:
            cur["y"] = int(state["y"])
        if "theme" in state and state["theme"] in ("dark", "light"):
            cur["theme"] = state["theme"]
        if "selected_account" in state and state["selected_account"]:
            cur["selected_account"] = state["selected_account"]
        if "hide_to_tray_on_exit" in state:
            cur["hide_to_tray_on_exit"] = bool(state["hide_to_tray_on_exit"])
        if "start_at_startup_tray" in state:
            enabled = bool(state["start_at_startup_tray"])
            set_windows_startup_tray(enabled)
            cur["start_at_startup_tray"] = enabled
        _reg_write_json("WindowState", cur)
    except Exception:
        pass

def get_steam_registry_info():
    info = {
        "steam_path": r"C:\Program Files (x86)\Steam",
        "steam_exe": r"C:\Program Files (x86)\Steam\steam.exe",
        "auto_login_user": "",
        "active_user_sid3": 0,
        "running_appid": 0
    }
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", 0, winreg.KEY_READ)
        try:
            sp, _ = winreg.QueryValueEx(key, "SteamPath")
            if sp:
                info["steam_path"] = os.path.normpath(sp)
        except Exception:
            pass
        try:
            se, _ = winreg.QueryValueEx(key, "SteamExe")
            if se:
                info["steam_exe"] = os.path.normpath(se)
        except Exception:
            pass
        try:
            alu, _ = winreg.QueryValueEx(key, "AutoLoginUser")
            if alu:
                info["auto_login_user"] = alu
        except Exception:
            pass
        try:
            r_app, _ = winreg.QueryValueEx(key, "RunningAppID")
            info["running_appid"] = int(r_app) if r_app else 0
        except Exception:
            pass
        winreg.CloseKey(key)
    except Exception:
        pass
    try:
        akey = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam\ActiveProcess", 0, winreg.KEY_READ)
        au, _ = winreg.QueryValueEx(akey, "ActiveUser")
        info["active_user_sid3"] = int(au) if au else 0
        winreg.CloseKey(akey)
    except Exception:
        pass
    return info

def get_running_steam_appid():
    reg = get_steam_registry_info()
    r_aid = reg.get("running_appid", 0)
    if r_aid and int(r_aid) > 0:
        return str(r_aid)
    return "0"

def is_app_running_in_steam(appid):
    reg = get_steam_registry_info()
    if str(reg.get("running_appid", 0)) == str(appid):
        return True
    try:
        akey = winreg.OpenKey(winreg.HKEY_CURRENT_USER, rf"Software\Valve\Steam\Apps\{appid}", 0, winreg.KEY_READ)
        try:
            running, _ = winreg.QueryValueEx(akey, "Running")
            if int(running) == 1:
                winreg.CloseKey(akey)
                return True
        except Exception:
            pass
        try:
            launching, _ = winreg.QueryValueEx(akey, "Launching")
            if int(launching) == 1:
                winreg.CloseKey(akey)
                return True
        except Exception:
            pass
        winreg.CloseKey(akey)
    except Exception:
        pass
    return False

def get_uploading_appids():
    now = time.time()
    active = set(UPLOADING_APPIDS)
    for aid, until_ts in list(UPLOADING_UNTIL.items()):
        if until_ts > now:
            active.add(str(aid))
        else:
            UPLOADING_UNTIL.pop(aid, None)
    return sorted(list(active))

def get_running_appids(candidate_appids=None):
    running_set = set()
    reg = get_steam_registry_info()
    r_aid = reg.get("running_appid", 0)
    if r_aid and int(r_aid) > 0:
        running_set.add(str(r_aid))
    cands = candidate_appids
    if cands is None and STATE_CACHE and "games" in STATE_CACHE:
        cands = [str(g["appid"]) for g in STATE_CACHE["games"]]
    if cands:
        now = time.time()
        for aid in cands:
            aid_s = str(aid)
            if is_app_running_in_steam(aid_s):
                running_set.add(aid_s)
                LAUNCHING_UNTIL.pop(aid_s, None)
            elif LAUNCHING_UNTIL.get(aid_s, 0) > now:
                running_set.add(aid_s)
    # Attach background watcher to any running game so auto-upload triggers as soon as it stops
    if running_set and STATE_CACHE and "games" in STATE_CACHE:
        steam_path = reg["steam_path"]
        accounts = parse_loginusers(steam_path)
        for r_id in running_set:
            if r_id not in ACTIVE_WATCHERS:
                g_match = next((g for g in STATE_CACHE["games"] if str(g["appid"]) == r_id), None)
                if g_match:
                    acc_info = accounts.get(g_match.get("owner_account", ""), {})
                    threading.Thread(
                        target=watch_game_and_auto_upload,
                        args=(steam_path, r_id, acc_info, g_match.get("name", f"App {r_id}")),
                        daemon=True
                    ).start()
    return sorted(list(running_set))

def is_steam_running():
    try:
        out = subprocess.check_output(
            ["tasklist", "/FI", "IMAGENAME eq steam.exe", "/NH"],
            creationflags=subprocess.CREATE_NO_WINDOW,
            stderr=subprocess.DEVNULL
        ).decode("utf-8", errors="ignore")
        return "steam.exe" in out.lower()
    except Exception:
        return False

def _parse_vdf_binary_dict(buf, offset, max_end, strings):
    res = {}
    while offset < max_end:
        t = buf[offset]
        offset += 1
        if t == 0x08:
            break
        if offset + 4 > max_end:
            break
        key_idx = struct.unpack("<I", buf[offset:offset+4])[0]
        key = strings[key_idx] if key_idx < len(strings) else str(key_idx)
        offset += 4
        if t == 0x00:
            val, offset = _parse_vdf_binary_dict(buf, offset, max_end, strings)
            res[key] = val
        elif t == 0x01:
            s_end = buf.find(b"\x00", offset, max_end)
            if s_end == -1:
                break
            res[key] = buf[offset:s_end].decode("utf-8", errors="replace")
            offset = s_end + 1
        elif t == 0x02:
            res[key] = struct.unpack("<i", buf[offset:offset+4])[0]
            offset += 4
        elif t == 0x07:
            res[key] = struct.unpack("<Q", buf[offset:offset+8])[0]
            offset += 8
        else:
            break
    return res, offset

def parse_appinfo_vdf(steam_path, extract_ufs_appids=None):
    """Fast binary VDF v29 parser for appinfo.vdf to extract game names, types, and UFS save rules."""
    appinfo_path = os.path.join(steam_path, "appcache", "appinfo.vdf")
    result = {}
    if not os.path.exists(appinfo_path):
        return result
    try:
        with open(appinfo_path, "rb") as f:
            data = f.read()
        if len(data) < 16:
            return result
        magic, _ = struct.unpack("<II", data[:8])
        if magic == 0x07564429:
            str_table_offset = struct.unpack("<q", data[8:16])[0]
            num_strings = struct.unpack("<I", data[str_table_offset:str_table_offset+4])[0]
            pos = str_table_offset + 4
            strings = []
            for _ in range(num_strings):
                end = data.find(b"\x00", pos)
                if end == -1:
                    break
                strings.append(data[pos:end].decode("utf-8", errors="replace"))
                pos = end + 1
            name_idx = strings.index("name") if "name" in strings else -1
            type_idx = strings.index("type") if "type" in strings else -1
            if name_idx == -1:
                return result
            name_pat = b"\x01" + struct.pack("<I", name_idx)
            type_pat = b"\x01" + struct.pack("<I", type_idx) if type_idx != -1 else None

            pos = 16
            while pos < str_table_offset - 8:
                appid, size = struct.unpack("<II", data[pos:pos+8])
                if appid == 0 or size <= 0:
                    break
                entry_end = pos + 8 + size
                vdf_pos = pos + 68
                if entry_end > str_table_offset:
                    break
                aid_str = str(appid)
                n_pos = data.find(name_pat, vdf_pos, entry_end)
                if n_pos != -1:
                    s_end = data.find(b"\x00", n_pos + 5, entry_end)
                    if s_end != -1:
                        gname = data[n_pos+5:s_end].decode("utf-8", errors="replace")
                        gtype = "game"
                        if type_pat:
                            t_pos = data.find(type_pat, vdf_pos, entry_end)
                            if t_pos != -1:
                                t_end = data.find(b"\x00", t_pos + 5, entry_end)
                                if t_end != -1:
                                    gtype = data[t_pos+5:t_end].decode("utf-8", errors="replace").lower()
                        result[aid_str] = {"name": gname, "type": gtype}

                if extract_ufs_appids and aid_str in extract_ufs_appids and aid_str not in UFS_CACHE:
                    try:
                        parsed_dict, _ = _parse_vdf_binary_dict(data, vdf_pos, entry_end, strings)
                        ufs_block = parsed_dict.get("appinfo", {}).get("ufs", {}).get("savefiles", {})
                        if ufs_block:
                            UFS_CACHE[aid_str] = list(ufs_block.values())
                    except Exception:
                        pass
                pos = entry_end
    except Exception:
        pass
    return result

def resolve_root_base_dir(root_name, steam_path, sid3, appid):
    r = str(root_name).lower()
    user_profile = os.environ.get("USERPROFILE", os.path.expanduser("~"))
    if r in ("winmydocuments", "2"):
        return os.path.join(user_profile, "Documents")
    if r in ("winappdatalocal", "3"):
        return os.environ.get("LOCALAPPDATA", os.path.join(user_profile, "AppData", "Local"))
    if r in ("winappdatalocallow", "12"):
        return os.path.join(user_profile, "AppData", "LocalLow")
    if r in ("winappdataroaming", "4"):
        return os.environ.get("APPDATA", os.path.join(user_profile, "AppData", "Roaming"))
    if r in ("winsavedgames", "11"):
        return os.path.join(user_profile, "Saved Games")
    if r in ("0", "default", "steamcloudDocuments"):
        return os.path.join(steam_path, "userdata", str(sid3), str(appid), "remote")
    return None

def get_game_save_targets(steam_path, appid, account_info):
    """Return list of {"dir": resolved_folder, "pattern": file_pattern} for a game."""
    sid64 = account_info.get("steamid64", "")
    sid3 = account_info.get("steamid3", "")
    targets = []

    if appid not in UFS_CACHE:
        parse_appinfo_vdf(steam_path, extract_ufs_appids={str(appid)})

    ufs_rules = UFS_CACHE.get(str(appid), [])
    for rule in ufs_rules:
        platforms = rule.get("platforms", {})
        if platforms and "windows" not in [str(v).lower() for v in platforms.values()]:
            continue
        base_dir = resolve_root_base_dir(rule.get("root", ""), steam_path, sid3, appid)
        if not base_dir:
            continue
        rel_path_raw = rule.get("path", "").replace("/", os.sep).replace("\\", os.sep).strip(os.sep)
        rel_path = rel_path_raw.replace("{64BitSteamID}", sid64).replace("{Steam3AccountID}", sid3)
        full_dir = os.path.normpath(os.path.join(base_dir, rel_path))
        pattern = rule.get("pattern", "*")
        targets.append({"dir": full_dir, "pattern": pattern})

        # If {64BitSteamID} folder for this account doesn't exist yet, check if saves exist under another SteamID64 folder and migrate/include them
        if "{64BitSteamID}" in rel_path_raw and not os.path.isdir(full_dir):
            glob_rel = rel_path_raw.replace("{64BitSteamID}", "*").replace("{Steam3AccountID}", "*")
            for cand in glob.glob(os.path.join(base_dir, glob_rel)):
                if os.path.isdir(cand):
                    try:
                        os.makedirs(full_dir, exist_ok=True)
                        for item in os.listdir(cand):
                            s_item = os.path.join(cand, item)
                            d_item = os.path.join(full_dir, item)
                            if os.path.isfile(s_item) and not os.path.exists(d_item):
                                shutil.copy2(s_item, d_item)
                    except Exception:
                        pass

    # Also check userdata/<sid3>/<appid>/remote
    remote_dir = os.path.join(steam_path, "userdata", str(sid3), str(appid), "remote")
    if os.path.isdir(remote_dir) or not targets:
        targets.append({"dir": remote_dir, "pattern": "*"})

    # De-duplicate targets
    unique = []
    seen = set()
    for t in targets:
        key = (t["dir"].lower(), t["pattern"].lower())
        if key not in seen:
            seen.add(key)
            unique.append(t)
    return unique

def collect_local_save_files(steam_path, appid, account_info):
    """Find all matching local save files for a game directly in Steam's save folder, returning (files_list, latest_mtime, combined_sha256)."""
    targets = get_game_save_targets(steam_path, appid, account_info)
    matched_files = []  # list of (target_idx, rel_path_in_target, abs_path, mtime)
    latest_mtime = 0

    for idx, t in enumerate(targets):
        base_dir = t["dir"]
        pattern = t["pattern"]
        if not os.path.isdir(base_dir):
            continue
        for root, _, files in os.walk(base_dir):
            for fname in files:
                if fname.lower() == "steam_autocloud.vdf":
                    continue
                if fnmatch.fnmatch(fname.lower(), pattern.lower()) or pattern == "*":
                    abs_p = os.path.join(root, fname)
                    rel_p = os.path.relpath(abs_p, base_dir).replace("\\", "/")
                    mtime = int(os.path.getmtime(abs_p))
                    if mtime > latest_mtime:
                        latest_mtime = mtime
                    matched_files.append((idx, rel_p, abs_p, mtime))

    if not matched_files:
        return [], 0, ""

    matched_files.sort(key=lambda x: (x[0], x[1]))
    h = hashlib.sha256()
    for idx, rel_p, abs_p, _ in matched_files:
        h.update(f"{idx}:{rel_p}:".encode("utf-8"))
        try:
            h.update(open(abs_p, "rb").read())
        except Exception:
            pass
    return matched_files, latest_mtime, h.hexdigest()

def pack_save_zip_bytes(steam_path, appid, account_info):
    files, latest_mtime, content_sha = collect_local_save_files(steam_path, appid, account_info)
    if not files:
        return None, 0, ""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        manifest = {"appid": str(appid), "files_sha256": content_sha, "updated_at": latest_mtime}
        zf.writestr("_cloud_manifest.json", json.dumps(manifest))
        pers_ach = get_game_personal_achievements(appid)
        zf.writestr("_personal_achievements.json", json.dumps(pers_ach))
        for idx, rel_p, abs_p, mtime in files:
            arcname = f"t{idx}/{rel_p}"
            zf.write(abs_p, arcname)
    return buf.getvalue(), latest_mtime, content_sha

def unpack_save_zip_bytes(steam_path, appid, account_info, zip_bytes, mtime_to_apply=None):
    targets = get_game_save_targets(steam_path, appid, account_info)
    if not targets:
        return False

    buf = io.BytesIO(zip_bytes)
    with zipfile.ZipFile(buf, "r") as zf:
        if "_personal_achievements.json" in zf.namelist():
            try:
                ach_data = json.loads(zf.read("_personal_achievements.json").decode("utf-8", errors="ignore"))
                if isinstance(ach_data, dict):
                    rst_ts = get_game_reset_timestamp(appid)
                    locked_map = get_game_locked_achievements(appid)
                    filtered_ach = {}
                    for k, v in ach_data.items():
                        ts_i = int(v or 0)
                        if ts_i > rst_ts and ts_i > int(locked_map.get(str(k), 0) or 0):
                            filtered_ach[str(k)] = ts_i
                    set_game_personal_achievements(appid, filtered_ach)
                    apply_personal_achievements_to_steam_bin(steam_path, account_info.get("steamid3", ""), appid, filtered_ach)
            except Exception:
                pass
        for member in zf.namelist():
            if member in ("_cloud_manifest.json", "_personal_achievements.json") or member.endswith("/"):
                continue
            parts = member.split("/", 1)
            if len(parts) == 2 and parts[0].startswith("t") and parts[0][1:].isdigit():
                t_idx = int(parts[0][1:])
                rel_p = parts[1]
            else:
                t_idx = 0
                rel_p = member
            if t_idx >= len(targets):
                t_idx = 0
            dest_base = targets[t_idx]["dir"]
            dest_path = os.path.normpath(os.path.join(dest_base, rel_p))
            if not dest_path.startswith(os.path.normpath(dest_base)):
                continue
            os.makedirs(os.path.dirname(dest_path), exist_ok=True)
            with open(dest_path, "wb") as out_f:
                out_f.write(zf.read(member))
            if mtime_to_apply:
                try:
                    os.utime(dest_path, (mtime_to_apply, mtime_to_apply))
                except Exception:
                    pass
    return True

def cloud_request(path, method="GET", body=None, headers=None, timeout=6, custom_cfg=None):
    cfg = custom_cfg or load_cloud_config()
    base_url = cfg.get("server_url", "").rstrip("/")
    if not base_url:
        return None, "Server URL belum diisi"
    url = f"{base_url}{path}"
    req_headers = {
        "X-Cloud-User": urllib.parse.quote(cfg.get("username", "")),
        "X-Cloud-Pass": urllib.parse.quote(cfg.get("password", "")),
        "User-Agent": "SteamSwitcherCloud/2.0"
    }
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, data=body, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            ctype = resp.headers.get("Content-Type", "")
            raw = resp.read()
            if "application/json" in ctype:
                return json.loads(raw.decode("utf-8", errors="ignore")), None
            return raw, None
    except urllib.error.HTTPError as he:
        try:
            err_json = json.loads(he.read().decode("utf-8", errors="ignore"))
            return None, err_json.get("error") or str(he)
        except Exception:
            return None, str(he)
    except Exception as e:
        return None, str(e)

def is_local_and_cloud_same(has_local, local_mtime, local_sha, cloud_exists, cloud_meta, username, appid):
    """Determine if local save and cloud save are identical (timezone-independent UTC epoch + SHA256 check)."""
    if not has_local or not cloud_exists or not cloud_meta:
        return False
    cloud_mtime = int(cloud_meta.get("updated_at", 0))
    cloud_files_sha = cloud_meta.get("files_sha256", "")
    cloud_zip_sha = cloud_meta.get("sha256", "")
    if cloud_files_sha and local_sha and cloud_files_sha == local_sha:
        return True
    if abs(local_mtime - cloud_mtime) <= 2:
        return True
    last_rec = get_last_sync_record(username, appid)
    if (
        last_rec.get("files_sha256") == local_sha
        and last_rec.get("cloud_sha256") == cloud_zip_sha
        and cloud_zip_sha != ""
    ):
        return True
    return False

def sync_game_save(steam_path, appid, account_info, game_name, direction="auto", check_conflict=False):
    """
    direction: 'auto' (smart compare), 'upload' (force upload local), 'download' (force download cloud)
    If check_conflict=True and local is strictly newer than cloud (when cloud exists), returns ('conflict', info_dict).
    Returns (ok, action_taken, message_or_dict)
    """
    cfg = load_cloud_config()
    username = cfg.get("username", "")
    if not cfg.get("server_url") or not username or not cfg.get("password"):
        return True, "skipped", "Belum login Cloud"

    zip_bytes, local_mtime, local_sha = pack_save_zip_bytes(steam_path, appid, account_info)
    has_local = zip_bytes is not None

    meta_resp, err = cloud_request(f"/api/saves/{appid}/meta", timeout=4)
    if err or not isinstance(meta_resp, dict):
        return False, "error", f"Cloud tidak terjangkau: {err}"

    cloud_exists = bool(meta_resp.get("exists") and meta_resp.get("meta"))
    cloud_meta = meta_resp.get("meta") or {}
    cloud_mtime = int(cloud_meta.get("updated_at", 0))
    cloud_zip_sha = cloud_meta.get("sha256", "")

    # 1. If direction is auto and both exist and are identical -> in_sync immediately!
    if direction == "auto" and is_local_and_cloud_same(has_local, local_mtime, local_sha, cloud_exists, cloud_meta, username, appid):
        return True, "in_sync", f"Save '{game_name}' sudah sama & terbaru."

    # 2. If direction == 'download' OR (direction == 'auto' and Cloud is newer or local doesn't exist) -> auto use Cloud
    if direction == "download" or (
        direction == "auto" and cloud_exists and (not has_local or cloud_mtime > local_mtime + 2)
    ):
        dl_bytes, dl_err = cloud_request(f"/api/saves/{appid}/download", timeout=15)
        if dl_err or not isinstance(dl_bytes, bytes):
            return False, "error", f"Gagal mengunduh save dari Cloud: {dl_err}"
        unpack_save_zip_bytes(steam_path, appid, account_info, dl_bytes, mtime_to_apply=cloud_mtime)
        _, new_local_mtime, new_local_sha = collect_local_save_files(steam_path, appid, account_info)
        update_sync_state(username, appid, new_local_sha, cloud_zip_sha, cloud_mtime, active_filename="latest.zip")
        return True, "downloaded", f"Save terbaru '{game_name}' diunduh dari Cloud ({cloud_meta.get('device', 'Cloud')})"

    # 3. If direction == 'auto' and Local is newer than existing Cloud save -> ask user if check_conflict=True!
    if (
        direction == "auto"
        and check_conflict
        and has_local
        and cloud_exists
        and local_mtime > cloud_mtime + 2
    ):
        return True, "conflict", {
            "appid": str(appid),
            "account": account_info.get("account_name", ""),
            "game_name": game_name,
            "local_mtime": local_mtime,
            "cloud_mtime": cloud_mtime,
            "cloud_device": cloud_meta.get("device", "Cloud")
        }

    # 4. Upload local save to Cloud (when direction == 'upload', or first time upload when cloud doesn't exist, or post-game auto upload)
    if direction == "upload" or (
        direction == "auto" and has_local and (not cloud_exists or local_mtime > cloud_mtime + 2)
    ):
        if not has_local:
            return False, "none", "Belum ada file save lokal untuk di-upload"
        up_headers = {
            "Content-Type": "application/zip",
            "X-Save-Mtime": str(local_mtime),
            "X-Save-Content-Sha": local_sha,
            "X-Device-Name": cfg.get("device_name", "PC"),
            "X-Game-Name": urllib.parse.quote(game_name),
            "X-Account-Name": account_info.get("account_name", "")
        }
        up_resp, up_err = cloud_request(
            f"/api/saves/{appid}/upload",
            method="POST",
            body=zip_bytes,
            headers=up_headers,
            timeout=20
        )
        if up_err:
            return False, "error", f"Gagal upload save ke Cloud: {up_err}"
        new_cloud_sha = (up_resp.get("meta", {}) if isinstance(up_resp, dict) else {}).get("sha256", hashlib.sha256(zip_bytes).hexdigest())
        update_sync_state(username, appid, local_sha, new_cloud_sha, local_mtime, active_filename="latest.zip")
        return True, "uploaded", f"Save '{game_name}' berhasil di-upload ke Cloud!"

    return True, "in_sync", f"Save '{game_name}' sudah versi terbaru."

def get_combined_save_history(steam_path, appid, account_info):
    """Return up to MAX_SAVE_HISTORY (5) save versions from Cloud Server, directly comparing with live Steam save folder."""
    local_files, local_mtime, local_sha = collect_local_save_files(steam_path, appid, account_info)
    has_local = len(local_files) > 0

    entries = []
    cfg = load_cloud_config()
    username = cfg.get("username", "")
    last_rec = get_last_sync_record(username, appid)
    meta_files_sha = ""
    meta_device = "Cloud"

    if cfg.get("server_url") and username and cfg.get("password"):
        m_resp, m_err = cloud_request(f"/api/saves/{appid}/meta", timeout=4)
        if not m_err and isinstance(m_resp, dict) and m_resp.get("exists") and m_resp.get("meta"):
            meta_obj = m_resp["meta"]
            meta_files_sha = meta_obj.get("files_sha256", "")
            meta_device = meta_obj.get("device", "Cloud")

        h_resp, h_err = cloud_request(f"/api/saves/{appid}/history", timeout=4)
        if not h_err and isinstance(h_resp, dict) and isinstance(h_resp.get("history"), list):
            for item in h_resp["history"]:
                mtime = int(item.get("mtime", 0))
                fn = item.get("filename", "")
                is_lat = bool(item.get("is_latest"))
                entries.append({
                    "source": "cloud",
                    "filename": fn,
                    "is_latest": is_lat,
                    "mtime": mtime,
                    "size_kb": round(int(item.get("size_bytes", 0)) / 1024, 1),
                    "label": f"Cloud Terbaru ({item.get('device') or meta_device})" if is_lat else "Riwayat Cloud",
                    "is_used": False
                })
        elif not m_err and isinstance(m_resp, dict) and m_resp.get("exists") and m_resp.get("meta"):
            meta = m_resp["meta"]
            mtime = int(meta.get("updated_at", 0))
            sz = int(meta.get("size_bytes", 0))
            entries.append({
                "source": "cloud",
                "filename": "latest.zip",
                "is_latest": True,
                "mtime": mtime,
                "size_kb": round(sz / 1024, 1),
                "label": f"Cloud Terbaru ({meta.get('device', 'Cloud')})",
                "is_used": False
            })

    entries.sort(key=lambda x: (1 if x["is_latest"] else 0, x["mtime"]), reverse=True)
    entries = entries[:MAX_SAVE_HISTORY]

    # Directly compare live Steam save folder against Cloud history entries to mark which version is currently used
    matched_idx = None
    if has_local and entries:
        active_fn = last_rec.get("active_filename", "")
        # 1. Exact SHA256 match with last restored/synced file in Registry
        if local_sha and last_rec.get("files_sha256") == local_sha and active_fn:
            for i, e in enumerate(entries):
                if e["filename"] == active_fn:
                    matched_idx = i
                    break
        # 2. Exact SHA256 match with Cloud latest.zip meta
        if matched_idx is None and local_sha and meta_files_sha and local_sha == meta_files_sha:
            for i, e in enumerate(entries):
                if e["filename"] == "latest.zip":
                    matched_idx = i
                    break
        # 3. Direct mtime comparison between live Steam save folder and Cloud history versions
        if matched_idx is None:
            for i, e in enumerate(entries):
                if abs(int(e["mtime"]) - int(local_mtime)) <= 2:
                    matched_idx = i
                    break

    if matched_idx is not None:
        entries[matched_idx]["is_used"] = True

    local_info = {
        "exists": has_local,
        "mtime": local_mtime,
        "files_count": len(local_files),
        "matched": matched_idx is not None,
        "matched_index": (matched_idx + 1) if matched_idx is not None else None
    }
    return entries, local_info

def restore_save_from_history(steam_path, appid, account_info, game_name, source, filename, mtime_hint=0):
    """Restore a specific save version from Cloud history directly into Steam's save folder and mark it as active."""
    target_mtime = int(mtime_hint) if mtime_hint else int(time.time())
    parts = os.path.basename(filename).split("_")
    if parts and parts[0].isdigit():
        target_mtime = int(parts[0])

    if filename == "latest.zip":
        dl_bytes, dl_err = cloud_request(f"/api/saves/{appid}/download", timeout=15)
    else:
        dl_bytes, dl_err = cloud_request(f"/api/saves/{appid}/history/{urllib.parse.quote(filename)}", timeout=15)
    if dl_err or not isinstance(dl_bytes, bytes):
        return False, f"Gagal mengunduh riwayat dari Cloud: {dl_err}"

    # Read exact timestamp from _cloud_manifest.json inside zip if available
    try:
        with zipfile.ZipFile(io.BytesIO(dl_bytes), "r") as zf:
            if "_cloud_manifest.json" in zf.namelist():
                man = json.loads(zf.read("_cloud_manifest.json").decode("utf-8", errors="ignore"))
                if man.get("updated_at"):
                    target_mtime = int(man["updated_at"])
    except Exception:
        pass

    ok = unpack_save_zip_bytes(steam_path, appid, account_info, dl_bytes, mtime_to_apply=target_mtime)
    if not ok:
        return False, "Gagal mengekstrak file riwayat save ke folder Steam."

    _, new_local_mtime, new_local_sha = collect_local_save_files(steam_path, appid, account_info)
    cfg = load_cloud_config()
    username = cfg.get("username", "")
    last_rec = get_last_sync_record(username, appid)
    cloud_sha = last_rec.get("cloud_sha256") or hashlib.sha256(dl_bytes).hexdigest()
    update_sync_state(username, appid, new_local_sha, cloud_sha, target_mtime, active_filename=filename)

    build_full_state()
    return True, f"Versi save '{game_name}' berhasil diterapkan ke folder Steam!"

def watch_game_and_auto_upload(steam_path, appid, account_info, game_name):
    """Background watcher: waits for game to run in Steam, tracks personal achievements in real-time, and auto-uploads save on exit."""
    watch_key = str(appid)
    if watch_key in ACTIVE_WATCHERS:
        return
    ACTIVE_WATCHERS.add(watch_key)
    sid3 = account_info.get("steamid3", "")
    session_start_ts = int(time.time())
    try:
        _, initial_mtime, initial_sha = collect_local_save_files(steam_path, appid, account_info)
        # 1. Wait up to 90 seconds for Steam to mark the game as Running
        started = False
        for _ in range(45):
            if is_app_running_in_steam(appid):
                started = True
                break
            time.sleep(2)
        if started:
            # 2. Poll personal achievements every 1.2s in real-time while game is running
            while is_app_running_in_steam(appid):
                new_rt = capture_new_achievements_from_steam_bin(
                    steam_path, sid3, appid, session_start_ts=session_start_ts, game_name=game_name, trigger_popup=True
                )
                if new_rt:
                    build_full_state()
                time.sleep(1.2)
            # 3. Game just closed! Final achievement capture + Cloud Save upload if enabled
            time.sleep(1.2)
            new_ach = capture_new_achievements_from_steam_bin(
                steam_path, sid3, appid, session_start_ts=session_start_ts, game_name=game_name, trigger_popup=True
            )
            if is_game_cloud_enabled(appid):
                UPLOADING_APPIDS.add(watch_key)
                UPLOADING_UNTIL[watch_key] = time.time() + 4.5
                try:
                    time.sleep(1.0)
                    _, new_mtime, new_sha = collect_local_save_files(steam_path, appid, account_info)
                    if (new_sha and new_sha != initial_sha) or new_ach:
                        ok_u, act_u, msg_u = sync_game_save(steam_path, appid, account_info, game_name, direction="upload")
                    else:
                        ok_u, act_u, msg_u = sync_game_save(steam_path, appid, account_info, game_name, direction="auto")
                    if isinstance(msg_u, str):
                        LAST_UPLOAD_MSG[watch_key] = {"ts": int(time.time()), "message": msg_u, "action": act_u}
                finally:
                    UPLOADING_APPIDS.discard(watch_key)
            build_full_state()
        else:
            # Fallback: check if save files or achievements changed
            new_ach = capture_new_achievements_from_steam_bin(
                steam_path, sid3, appid, session_start_ts=session_start_ts, game_name=game_name, trigger_popup=True
            )
            if is_game_cloud_enabled(appid):
                _, new_mtime, new_sha = collect_local_save_files(steam_path, appid, account_info)
                if (new_sha and new_sha != initial_sha) or new_ach:
                    UPLOADING_APPIDS.add(watch_key)
                    UPLOADING_UNTIL[watch_key] = time.time() + 4.0
                    try:
                        ok_u, act_u, msg_u = sync_game_save(steam_path, appid, account_info, game_name, direction="upload")
                        if isinstance(msg_u, str):
                            LAST_UPLOAD_MSG[watch_key] = {"ts": int(time.time()), "message": msg_u, "action": act_u}
                    finally:
                        UPLOADING_APPIDS.discard(watch_key)
                    build_full_state()
    except Exception:
        pass
    finally:
        UPLOADING_APPIDS.discard(watch_key)
        ACTIVE_WATCHERS.discard(watch_key)

def global_game_watcher_loop():
    """Continuous background daemon: auto-syncs Cloud achievements on startup and attaches real-time watcher to any running Steam game."""
    time.sleep(1.5)
    try:
        reg = get_steam_registry_info()
        sp = reg["steam_path"]
        # 1. Sync personal achievements with Cloud on startup
        sync_cloud_achievements_bundle(sp, direction="both")
        # 2. Sanitize local Steam achievement bins for all non-running installed games so shared-account achievements don't leak
        running_now = get_running_steam_appid()
        installed = get_installed_games(sp)
        for aid in installed:
            if str(aid) != str(running_now):
                apply_personal_achievements_to_steam_bin(sp, "", aid)
        build_full_state()
    except Exception:
        pass

    while True:
        try:
            running_aid = get_running_steam_appid()
            if running_aid and running_aid != "0" and running_aid not in NON_GAME_APPIDS and running_aid not in ACTIVE_WATCHERS:
                reg = get_steam_registry_info()
                sp = reg["steam_path"]
                accounts = parse_loginusers(sp)
                cur_acc = reg.get("auto_login_user", "")
                acc_info = accounts.get(cur_acc, {})
                if not acc_info and reg.get("active_user_sid3"):
                    for a_obj in accounts.values():
                        if str(a_obj.get("steamid3")) == str(reg.get("active_user_sid3")):
                            acc_info = a_obj
                            break
                installed = get_installed_games(sp)
                gname = (installed.get(running_aid) or {}).get("name") or resolve_game_name(sp, running_aid)
                threading.Thread(
                    target=watch_game_and_auto_upload,
                    args=(sp, running_aid, acc_info, gname),
                    daemon=True
                ).start()
        except Exception:
            pass
        time.sleep(1.5)

def parse_loginusers(steam_path):
    """Parse loginusers.vdf strictly for currently registered Steam accounts."""
    vdf_path = os.path.join(steam_path, "config", "loginusers.vdf")
    accounts = {}
    if os.path.exists(vdf_path):
        try:
            txt = open(vdf_path, encoding="utf-8", errors="ignore").read()
            for m in re.finditer(r'"(\d{17})"\s*\{([^}]+)\}', txt):
                sid64 = m.group(1)
                body = m.group(2)
                acc = re.search(r'"AccountName"\s+"([^"]+)"', body)
                pers = re.search(r'"PersonaName"\s+"([^"]+)"', body)
                rem = re.search(r'"RememberPassword"\s+"([^"]+)"', body)
                off = re.search(r'"WantsOfflineMode"\s+"([^"]+)"', body)
                ts = re.search(r'"timestamp"\s+"([^"]+)"', body)
                if acc:
                    acc_name = acc.group(1)
                    sid3 = str(int(sid64) - 76561197960265728)
                    accounts[acc_name] = {
                        "account_name": acc_name,
                        "persona_name": pers.group(1) if pers else acc_name,
                        "steamid64": sid64,
                        "steamid3": sid3,
                        "remember_password": (rem.group(1) == "1") if rem else True,
                        "offline_mode": (off.group(1) == "1") if off else False,
                        "timestamp": int(ts.group(1)) if (ts and ts.group(1).isdigit()) else 0
                    }
        except Exception:
            pass
    return accounts

def get_library_folders(steam_path):
    """Read libraryfolders.vdf to find all Steam library paths across drives."""
    folders = [steam_path]
    vdf_path = os.path.join(steam_path, "steamapps", "libraryfolders.vdf")
    if os.path.exists(vdf_path):
        try:
            txt = open(vdf_path, encoding="utf-8", errors="ignore").read()
            for m in re.finditer(r'"path"\s+"([^"]+)"', txt):
                raw_path = m.group(1).replace("\\\\", "\\")
                norm_path = os.path.normpath(raw_path)
                if os.path.exists(norm_path) and norm_path not in folders:
                    folders.append(norm_path)
        except Exception:
            pass
    return folders

def get_installed_games(steam_path):
    """Scan all library folders for appmanifest_*.acf."""
    installed = {}
    folders = get_library_folders(steam_path)
    for folder in folders:
        steamapps = os.path.join(folder, "steamapps")
        if not os.path.isdir(steamapps):
            continue
        drive = os.path.splitdrive(folder)[0] or folder
        for acf in glob.glob(os.path.join(steamapps, "appmanifest_*.acf")):
            try:
                txt = open(acf, encoding="utf-8", errors="ignore").read()
                m_id = re.search(r'"appid"\s+"(\d+)"', txt)
                if not m_id:
                    continue
                appid = m_id.group(1)
                if appid in NON_GAME_APPIDS:
                    continue
                m_name = re.search(r'"name"\s+"([^"]+)"', txt)
                m_idir = re.search(r'"installdir"\s+"([^"]+)"', txt)
                m_size = re.search(r'"SizeOnDisk"\s+"(\d+)"', txt)
                m_lp = re.search(r'"LastPlayed"\s+"(\d+)"', txt)
                m_owner = re.search(r'"LastOwner"\s+"(\d+)"', txt)

                size_bytes = int(m_size.group(1)) if m_size else 0
                install_dir_name = m_idir.group(1) if m_idir else ""
                install_dir_full = os.path.join(steamapps, "common", install_dir_name) if install_dir_name else ""
                installed[appid] = {
                    "appid": appid,
                    "name": m_name.group(1) if m_name else f"App {appid}",
                    "install_dir": install_dir_full,
                    "size_bytes": size_bytes,
                    "size_gb": round(size_bytes / (1024 ** 3), 2) if size_bytes > 0 else 0,
                    "last_played": int(m_lp.group(1)) if m_lp else 0,
                    "last_owner_sid64": m_owner.group(1) if m_owner else "",
                    "drive": drive,
                    "library_path": folder
                }
            except Exception:
                pass
    return installed

def get_userdata_account_games(steam_path, accounts):
    """Scan userdata/<steamid3> to discover which games each account owns/played, excluding hidden games."""
    account_games = {acc_name: set() for acc_name in accounts}
    last_played_per_acc = {acc_name: {} for acc_name in accounts}

    for acc_name, acc in accounts.items():
        sid3 = acc["steamid3"]
        udir = os.path.join(steam_path, "userdata", sid3)
        if not os.path.isdir(udir):
            continue

        hidden_ids = set()
        cs_path = os.path.join(udir, "config", "cloudstorage", "cloud-storage-namespace-1.json")
        if os.path.exists(cs_path):
            try:
                cs_txt = open(cs_path, encoding="utf-8", errors="ignore").read()
                for m in re.finditer(r'\\"id\\":\\"hidden\\",\\"name\\":\\"Hidden\\",\\"added\\":\[([\d,\s]*)\]', cs_txt):
                    for part in m.group(1).split(","):
                        part = part.strip()
                        if part.isdigit():
                            hidden_ids.add(part)
            except Exception:
                pass

        lcache_dir = os.path.join(udir, "config", "librarycache")
        if os.path.isdir(lcache_dir):
            for jf in glob.glob(os.path.join(lcache_dir, "*.json")):
                aid = os.path.splitext(os.path.basename(jf))[0]
                if aid.isdigit() and aid not in NON_GAME_APPIDS and aid not in hidden_ids:
                    account_games[acc_name].add(aid)

        lcfg = os.path.join(udir, "config", "localconfig.vdf")
        if os.path.exists(lcfg):
            try:
                txt = open(lcfg, encoding="utf-8", errors="ignore").read()
                for m in re.finditer(r'"(\d{4,8})"\s*\{\s*"LastPlayed"\s*"(\d+)"', txt):
                    aid, lp = m.group(1), int(m.group(2))
                    if aid not in NON_GAME_APPIDS and aid not in hidden_ids:
                        account_games[acc_name].add(aid)
                        last_played_per_acc[acc_name][aid] = lp
            except Exception:
                pass

    return account_games, last_played_per_acc

def find_local_cover(steam_path, appid):
    """Find cached cover image in Steam's appcache/librarycache/<appid>."""
    libcache = os.path.join(steam_path, "appcache", "librarycache", str(appid))
    if not os.path.isdir(libcache):
        return None
    found_files = {}
    for root, _, files in os.walk(libcache):
        for f in files:
            if f not in found_files:
                found_files[f] = os.path.join(root, f)
    for pref in ["library_600x900.jpg", "library_capsule.jpg", "library_header.jpg", "header.jpg"]:
        if pref in found_files:
            return found_files[pref]
    for f, full_path in found_files.items():
        if f.endswith(".jpg") and "blur" not in f and "hero" not in f:
            return full_path
    return None

def load_patched_games():
    data = _reg_read_json("PatchedGames")
    return data if isinstance(data, dict) else {}

def save_patched_games(data):
    if isinstance(data, dict):
        _reg_write_json("PatchedGames", data)

def fetch_cloud_patches_meta(force=False):
    global PATCHES_META_CACHE, PATCHES_META_TS, PATCHES_FETCHING
    now = time.time()
    if not force and PATCHES_META_TS > 0 and (now - PATCHES_META_TS) < 45:
        return PATCHES_META_CACHE

    def _do_fetch():
        global PATCHES_META_CACHE, PATCHES_META_TS, PATCHES_FETCHING
        try:
            resp, err = cloud_request("/api/patches", timeout=3.5)
            if not err and isinstance(resp, dict) and isinstance(resp.get("patches"), dict):
                PATCHES_META_CACHE = resp["patches"]
                PATCHES_META_TS = time.time()
            elif PATCHES_META_TS == 0:
                PATCHES_META_TS = time.time() - 35
        except Exception:
            pass
        finally:
            PATCHES_FETCHING = False

    if force:
        PATCHES_FETCHING = True
        _do_fetch()
    elif not PATCHES_FETCHING:
        PATCHES_FETCHING = True
        threading.Thread(target=_do_fetch, daemon=True).start()
    return PATCHES_META_CACHE

LOCAL_FILE_HASH_CACHE = {}

def _fast_local_sha256(fp):
    if not fp or not os.path.isfile(fp):
        return ""
    try:
        st = os.stat(fp)
        key = (fp, int(st.st_mtime), int(st.st_size))
        if key in LOCAL_FILE_HASH_CACHE:
            return LOCAL_FILE_HASH_CACHE[key]
        h = hashlib.sha256()
        with open(fp, "rb") as f:
            while True:
                chunk = f.read(1024 * 1024)
                if not chunk:
                    break
                h.update(chunk)
        digest = h.hexdigest()
        LOCAL_FILE_HASH_CACHE[key] = digest
        return digest
    except Exception:
        return ""

def find_local_patch_file(appid, game_name=""):
    """Check if a local repacked mod exists in ../Translate/<game_name>/4_repacked_mod or %LOCALAPPDATA%/SteamSwitcher/patches/<appid>."""
    aid_s = str(appid)
    candidates = []
    # 1. Prioritize sibling Translate/<game_name>/4_repacked_mod folder (author's local build)
    for root_cand in [os.path.dirname(BASE_DIR), r"C:\Users\Nazwa\Pictures\Ngoding"]:
        if game_name:
            repack_dir = os.path.join(root_cand, "Translate", game_name, "4_repacked_mod")
            if os.path.isdir(repack_dir):
                for fn in os.listdir(repack_dir):
                    fp = os.path.join(repack_dir, fn)
                    if os.path.isfile(fp) and fn.lower().endswith(".pak"):
                        candidates.append(fp)

    # 2. Fallback to local downloaded cache
    local_store = os.path.join(os.environ.get("LOCALAPPDATA", tempfile.gettempdir()), "SteamSwitcher", "patches", aid_s)
    if os.path.isdir(local_store):
        for fn in os.listdir(local_store):
            fp = os.path.join(local_store, fn)
            if os.path.isfile(fp) and not fn.endswith(".tmp"):
                candidates.append(fp)
    return candidates[0] if candidates else None

def find_local_original_backup(appid, game_name="", target_filename=""):
    """Check if a local original backup exists in ../Translate/<game_name>/1_original_unpack."""
    for root_cand in [os.path.dirname(BASE_DIR), r"C:\Users\Nazwa\Pictures\Ngoding"]:
        if game_name:
            orig_dir = os.path.join(root_cand, "Translate", game_name, "1_original_unpack")
            if os.path.isdir(orig_dir):
                for fn in os.listdir(orig_dir):
                    if "ORIGINAL_BACKUP" in fn.upper() and fn.lower().endswith(".pak"):
                        return os.path.join(orig_dir, fn)
    return None

def resolve_game_patch_target(install_dir, filename):
    """Locate where filename (e.g. FFRS-Windows_0_P.pak) belongs inside the game's Steam install directory."""
    if not install_dir or not os.path.isdir(install_dir) or not filename:
        return None
    fn_lower = os.path.basename(filename).lower()
    for root, _, files in os.walk(install_dir):
        for f in files:
            if f.lower() == fn_lower:
                return os.path.join(root, f)
    # Fallback: look for Content/Paks directory for Unreal Engine .pak files
    if fn_lower.endswith(".pak"):
        for root, dirs, _ in os.walk(install_dir):
            if os.path.basename(root).lower() == "paks" and "content" in root.lower():
                return os.path.join(root, os.path.basename(filename))
    return os.path.join(install_dir, os.path.basename(filename))

def get_game_patch_info(appid, game_name, inst_info, cloud_patches=None):
    """
    Determine patch_status for a game:
    - 'installed': Patch Bahasa Indonesia is currently installed in the game folder
    - 'available': Patch Bahasa Indonesia is available (on Cloud Server or local Translate folder)
    - 'none': No patch available for this game
    """
    aid_s = str(appid)
    if cloud_patches is None:
        cloud_patches = fetch_cloud_patches_meta(force=False)
    server_info = cloud_patches.get(aid_s)
    local_patch_fp = find_local_patch_file(aid_s, game_name)
    has_patch_source = bool(server_info or local_patch_fp)

    patched_reg = load_patched_games()
    rec = patched_reg.get(aid_s, {})
    install_dir = (inst_info or {}).get("install_dir", "")

    patch_fname = ""
    if server_info and server_info.get("primary_file"):
        patch_fname = server_info["primary_file"]
    elif local_patch_fp:
        patch_fname = os.path.basename(local_patch_fp)
    elif rec.get("filename"):
        patch_fname = rec["filename"]

    target_fp = rec.get("target_path", "")
    if (not target_fp or not os.path.exists(target_fp)) and install_dir and patch_fname:
        target_fp = resolve_game_patch_target(install_dir, patch_fname) or ""

    is_patched_now = False
    if inst_info and install_dir and os.path.isdir(install_dir):
        if rec.get("status") == "installed" and target_fp and os.path.exists(target_fp):
            is_patched_now = True
        elif rec.get("status") != "unpatched" and target_fp and os.path.exists(target_fp):
            # Auto-detect if target file matches patched size or has .orig_backup
            try:
                cur_sz = os.path.getsize(target_fp)
                if os.path.exists(target_fp + ".orig_backup"):
                    is_patched_now = True
                elif local_patch_fp and os.path.exists(local_patch_fp) and cur_sz == os.path.getsize(local_patch_fp):
                    orig_b = find_local_original_backup(aid_s, game_name, patch_fname)
                    if not orig_b or cur_sz != os.path.getsize(orig_b):
                        is_patched_now = True
                elif server_info and server_info.get("total_size_bytes") and cur_sz == int(server_info["total_size_bytes"]):
                    is_patched_now = True
            except Exception:
                pass
            if is_patched_now:
                patched_reg[aid_s] = {
                    "status": "installed",
                    "filename": patch_fname,
                    "target_path": target_fp,
                    "patched_at": int(os.path.getmtime(target_fp))
                }
                save_patched_games(patched_reg)

    if is_patched_now:
        status = "installed"
    elif has_patch_source:
        status = "available"
    else:
        status = "none"

    has_update = False
    if is_patched_now and target_fp and os.path.exists(target_fp):
        installed_sha = _fast_local_sha256(target_fp)
        if installed_sha:
            server_sha = (server_info or {}).get("primary_sha256", "")
            local_sha = _fast_local_sha256(local_patch_fp) if (local_patch_fp and os.path.exists(local_patch_fp)) else ""
            if server_sha and installed_sha != server_sha:
                has_update = True
            elif local_sha and installed_sha != local_sha:
                has_update = True

    return {
        "patch_status": status,
        "patch_has_update": has_update,
        "patch_filename": patch_fname,
        "patch_target": target_fp,
        "patch_in_server": bool(server_info),
        "patch_has_local": bool(local_patch_fp and "4_repacked_mod" in local_patch_fp),
        "patch_local_path": local_patch_fp or ""
    }

def install_game_patch(steam_path, appid, game_name):
    aid_s = str(appid)
    if is_app_running_in_steam(aid_s):
        return False, f"Tutup game '{game_name}' terlebih dahulu sebelum memasang patch Bahasa Indonesia."
    installed = get_installed_games(steam_path)
    inst_info = installed.get(aid_s)
    if not inst_info or not inst_info.get("install_dir") or not os.path.isdir(inst_info["install_dir"]):
        return False, f"Game '{game_name}' belum terinstal di PC ini."

    install_dir = inst_info["install_dir"]
    cloud_patches = fetch_cloud_patches_meta(force=True)
    server_info = cloud_patches.get(aid_s)
    local_patch_fp = find_local_patch_file(aid_s, game_name)

    patch_fname = ""
    tmp_downloaded = None
    source_fp = None

    # 1. Try downloading from Cloud Server first if available
    if server_info:
        patch_fname = server_info.get("primary_file") or "patch.pak"
        cfg = load_cloud_config()
        base_url = cfg.get("server_url", "").rstrip("/")
        if base_url:
            try:
                req = urllib.request.Request(
                    f"{base_url}/api/patches/{aid_s}/download",
                    headers={"User-Agent": "SteamSwitcherCloud/2.0"}
                )
                with urllib.request.urlopen(req, timeout=120) as resp:
                    hdr_fn = urllib.parse.unquote(resp.headers.get("X-Patch-Filename", "")).strip()
                    if hdr_fn:
                        patch_fname = os.path.basename(hdr_fn)
                    tmp_dir = os.path.join(os.environ.get("LOCALAPPDATA", tempfile.gettempdir()), "SteamSwitcher", "patches", aid_s)
                    os.makedirs(tmp_dir, exist_ok=True)
                    tmp_downloaded = os.path.join(tmp_dir, patch_fname)
                    with open(tmp_downloaded + ".tmp", "wb") as out_f:
                        shutil.copyfileobj(resp, out_f, length=1024 * 1024)
                    os.replace(tmp_downloaded + ".tmp", tmp_downloaded)
                    source_fp = tmp_downloaded
            except Exception as e:
                if not local_patch_fp:
                    return False, f"Gagal mengunduh patch dari Server Cloud: {e}"

    # 2. Fallback to local repacked mod if not on server yet
    if not source_fp and local_patch_fp and os.path.exists(local_patch_fp):
        source_fp = local_patch_fp
        patch_fname = os.path.basename(local_patch_fp)

    if not source_fp or not os.path.exists(source_fp):
        return False, f"File patch Bahasa Indonesia untuk '{game_name}' belum tersedia di Server Cloud."

    target_fp = resolve_game_patch_target(install_dir, patch_fname)
    if not target_fp:
        return False, "Gagal menemukan lokasi folder instalasi patch di dalam folder game."

    os.makedirs(os.path.dirname(target_fp), exist_ok=True)
    backup_fp = target_fp + ".orig_backup"

    # Backup original file before replacing if backup doesn't exist yet
    if not os.path.exists(backup_fp):
        ext_orig = find_local_original_backup(aid_s, game_name, patch_fname)
        if ext_orig and os.path.exists(ext_orig):
            try:
                shutil.copy2(ext_orig, backup_fp)
            except Exception:
                pass
        elif os.path.exists(target_fp) and os.path.getsize(target_fp) != os.path.getsize(source_fp):
            try:
                shutil.copy2(target_fp, backup_fp)
            except Exception:
                pass

    try:
        shutil.copy2(source_fp, target_fp)
    except Exception as e:
        return False, f"Gagal memasang file patch ke folder game: {e}"

    patched_reg = load_patched_games()
    patched_reg[aid_s] = {
        "status": "installed",
        "filename": patch_fname,
        "target_path": target_fp,
        "patched_at": int(time.time()),
        "size_bytes": os.path.getsize(target_fp)
    }
    save_patched_games(patched_reg)
    build_full_state()
    return True, f"Patch Bahasa Indonesia berhasil dipasang untuk '{game_name}'!"

def uninstall_game_patch(steam_path, appid, game_name):
    aid_s = str(appid)
    if is_app_running_in_steam(aid_s):
        return False, f"Tutup game '{game_name}' terlebih dahulu sebelum melepas patch."
    installed = get_installed_games(steam_path)
    inst_info = installed.get(aid_s)
    patched_reg = load_patched_games()
    rec = patched_reg.get(aid_s, {})
    target_fp = rec.get("target_path", "")
    if not target_fp and inst_info and inst_info.get("install_dir"):
        p_info = get_game_patch_info(aid_s, game_name, inst_info)
        target_fp = p_info.get("patch_target", "")

    if not target_fp:
        return False, "Lokasi file patch tidak ditemukan."

    backup_fp = target_fp + ".orig_backup"
    if not os.path.exists(backup_fp):
        ext_orig = find_local_original_backup(aid_s, game_name, os.path.basename(target_fp))
        if ext_orig and os.path.exists(ext_orig):
            backup_fp = ext_orig
        elif target_fp.lower().endswith("_p.pak") and os.path.exists(target_fp):
            try:
                os.remove(target_fp)
            except Exception as e:
                return False, f"Gagal menghapus file patch add-on: {e}"
            patched_reg[aid_s] = {
                "status": "unpatched",
                "filename": os.path.basename(target_fp),
                "target_path": target_fp,
                "updated_at": int(time.time())
            }
            save_patched_games(patched_reg)
            build_full_state()
            return True, f"Patch Bahasa Indonesia untuk '{game_name}' berhasil dihapus."
        else:
            return False, "File backup original belum ditemukan, gunakan Verify Integrity di Steam untuk mengembalikan file asli."

    try:
        shutil.copy2(backup_fp, target_fp)
    except Exception as e:
        return False, f"Gagal mengembalikan file original: {e}"

    patched_reg[aid_s] = {
        "status": "unpatched",
        "filename": os.path.basename(target_fp),
        "target_path": target_fp,
        "updated_at": int(time.time())
    }
    save_patched_games(patched_reg)
    build_full_state()
    return True, f"File original '{game_name}' berhasil dikembalikan (Patch dilepas)."

def upload_local_patch_to_cloud(appid, game_name):
    aid_s = str(appid)
    cfg = load_cloud_config()
    base_url = cfg.get("server_url", "").rstrip("/")
    username = cfg.get("username", "")
    password = cfg.get("password", "")
    if not base_url or not username or not password:
        return False, "Silakan Login Cloud terlebih dahulu di kanan atas launcher."

    local_fp = find_local_patch_file(aid_s, game_name)
    if not local_fp or not os.path.exists(local_fp):
        return False, f"File patch lokal untuk '{game_name}' tidak ditemukan."

    fname = os.path.basename(local_fp)
    raw_bytes = open(local_fp, "rb").read()
    headers = {
        "Content-Type": "application/octet-stream",
        "X-Patch-Filename": urllib.parse.quote(fname)
    }
    resp, err = cloud_request(f"/api/patches/{aid_s}/upload", method="POST", body=raw_bytes, headers=headers, timeout=180)
    if err or not isinstance(resp, dict) or not resp.get("ok"):
        return False, f"Gagal upload patch ke Server Cloud: {err or 'Pastikan cloud_server.py di server sudah diperbarui.'}"

    fetch_cloud_patches_meta(force=True)
    build_full_state()
    return True, resp.get("message", f"Patch '{fname}' berhasil di-upload ke Server Cloud!")

def build_full_state():
    global STATE_CACHE
    reg = get_steam_registry_info()
    steam_path = reg["steam_path"]
    accounts = parse_loginusers(steam_path)
    installed = get_installed_games(steam_path)
    account_games, last_played_per_acc = get_userdata_account_games(steam_path, accounts)

    acc_sizes = {acc_name: len(gset) for acc_name, gset in account_games.items()}

    all_appids = set()
    for gset in account_games.values():
        all_appids.update(gset)

    appinfo = parse_appinfo_vdf(steam_path, extract_ufs_appids=all_appids)
    running_ids = set(get_running_appids(all_appids))
    uploading_ids = set(get_uploading_appids())
    cloud_cfg_now = load_cloud_config()
    enabled_cloud_ids = set(cloud_cfg_now.get("enabled_appids", []))
    cloud_patches = fetch_cloud_patches_meta(force=False)

    games_list = []
    for aid in all_appids:
        if aid in NON_GAME_APPIDS:
            continue
        info_meta = appinfo.get(aid, {})
        gtype = info_meta.get("type", "game")
        if gtype not in ("game", ""):
            continue

        inst_info = installed.get(aid)
        has_local_cover = os.path.isdir(os.path.join(steam_path, "appcache", "librarycache", str(aid)))

        name = (inst_info["name"] if inst_info else None) or info_meta.get("name")
        if not name:
            continue

        associated_accounts = [acc_name for acc_name, gset in account_games.items() if aid in gset]
        if not associated_accounts:
            continue

        associated_sorted = sorted(associated_accounts, key=lambda x: acc_sizes.get(x, 9999))
        owner_account = associated_sorted[0]

        lp_ts = inst_info["last_played"] if inst_info else 0
        for acc_name in associated_accounts:
            acc_lp = last_played_per_acc.get(acc_name, {}).get(aid, 0)
            if acc_lp > lp_ts:
                lp_ts = acc_lp

        if aid not in COVER_CACHE:
            cov_path = find_local_cover(steam_path, aid)
            if cov_path and os.path.exists(cov_path):
                try:
                    with open(cov_path, "rb") as cf:
                        raw_bytes = cf.read()
                        COVER_CACHE[aid] = raw_bytes
                        COVER_B64_CACHE[aid] = "data:image/jpeg;base64," + base64.b64encode(raw_bytes).decode("ascii")
                except Exception:
                    pass

        acc_info = accounts.get(owner_account, {})
        save_files, save_mtime, _ = collect_local_save_files(steam_path, aid, acc_info)
        ach_schema = parse_game_achievement_schema(steam_path, aid)
        pers_ach = get_game_personal_achievements(aid)
        valid_ach_keys = {x["key"] for x in ach_schema}
        ach_unlocked_cnt = sum(1 for k, v in pers_ach.items() if v and (not valid_ach_keys or k in valid_ach_keys))
        patch_info = get_game_patch_info(aid, name, inst_info, cloud_patches)

        games_list.append({
            "appid": aid,
            "name": name,
            "installed": inst_info is not None,
            "running": str(aid) in running_ids,
            "uploading": str(aid) in uploading_ids,
            "cloud_enabled": str(aid) in enabled_cloud_ids,
            "size_gb": inst_info["size_gb"] if inst_info else 0,
            "drive": inst_info["drive"] if inst_info else "",
            "last_played": lp_ts,
            "owner_account": owner_account,
            "owner_persona": acc_info.get("persona_name", owner_account),
            "accounts": associated_accounts,
            "has_local_cover": has_local_cover,
            "cover_src": COVER_B64_CACHE.get(aid, f"/api/cover/{aid}"),
            "has_local_save": len(save_files) > 0,
            "local_save_mtime": save_mtime,
            "ach_unlocked": ach_unlocked_cnt,
            "ach_total": len(ach_schema),
            **patch_info
        })

    games_list.sort(key=lambda g: (g["name"].lower(), g["appid"]))

    accounts_list = []
    for acc_name, acc in accounts.items():
        acc_games = [g for g in games_list if g["owner_account"] == acc_name]
        inst_count = sum(1 for g in acc_games if g["installed"])
        accounts_list.append({
            **acc,
            "is_active": (acc_name.lower() == reg["auto_login_user"].lower()),
            "installed_count": inst_count,
            "total_count": len(acc_games)
        })

    accounts_list.sort(key=lambda a: a["account_name"].lower())

    state = {
        "steam_running": is_steam_running(),
        "active_account": reg["auto_login_user"],
        "steam_path": steam_path,
        "logo_src": LOGO_B64_URI,
        "accounts": accounts_list,
        "games": games_list,
        "window_state": load_window_state(),
        "cloud_config": load_cloud_config()
    }
    with CACHE_LOCK:
        STATE_CACHE = state
    return state

def get_cached_state():
    with CACHE_LOCK:
        if STATE_CACHE is not None:
            return STATE_CACHE
    return build_full_state()

def set_steam_account(steam_path, target_account, offline_override=None):
    """Switch Steam's AutoLoginUser in Registry, disable AlwaysShowUserChooser in config.vdf, and update loginusers.vdf."""
    try:
        key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam")
        winreg.SetValueEx(key, "AutoLoginUser", 0, winreg.REG_SZ, target_account)
        winreg.SetValueEx(key, "RememberPassword", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(key, "StartupModeTmpIsValid", 0, winreg.REG_DWORD, 0)
        winreg.CloseKey(key)
    except Exception as e:
        return False, f"Gagal mengubah Registry: {e}"

    cfg_vdf_path = os.path.join(steam_path, "config", "config.vdf")
    if os.path.exists(cfg_vdf_path):
        try:
            ctxt = open(cfg_vdf_path, encoding="utf-8", errors="ignore").read()
            if '"AlwaysShowUserChooser"' in ctxt:
                new_ctxt = re.sub(r'("AlwaysShowUserChooser"\s+)"\d+"', r'\g<1>"0"', ctxt)
                if new_ctxt != ctxt:
                    with open(cfg_vdf_path, "w", encoding="utf-8") as f:
                        f.write(new_ctxt)
        except Exception:
            pass

    vdf_path = os.path.join(steam_path, "config", "loginusers.vdf")
    if os.path.exists(vdf_path):
        try:
            txt = open(vdf_path, encoding="utf-8", errors="ignore").read()
            now_ts = str(int(time.time()))

            def ensure_kv(block_body, k, v):
                if re.search(rf'"{k}"\s+"[^"]*"', block_body):
                    return re.sub(rf'("{k}"\s+)"[^"]*"', rf'\g<1>"{v}"', block_body)
                return block_body.rstrip() + f'\n\t\t"{k}"\t\t"{v}"\n\t'

            def repl_block(match):
                sid64 = match.group(1)
                body = match.group(2)
                m_acc = re.search(r'"AccountName"\s+"([^"]+)"', body)
                is_target = m_acc and (m_acc.group(1).lower() == target_account.lower())
                if is_target:
                    body = ensure_kv(body, "RememberPassword", "1")
                    body = ensure_kv(body, "AllowAutoLogin", "1")
                    body = ensure_kv(body, "MostRecent", "1")
                    body = ensure_kv(body, "AutoLogin", "1")
                    body = ensure_kv(body, "timestamp", now_ts)
                    if offline_override is not None:
                        val = "1" if offline_override else "0"
                        body = ensure_kv(body, "WantsOfflineMode", val)
                    if re.search(r'"WantsOfflineMode"\s+"1"', body) or (offline_override is True):
                        body = ensure_kv(body, "SkipOfflineModeWarning", "1")
                else:
                    body = ensure_kv(body, "MostRecent", "0")
                    body = ensure_kv(body, "AutoLogin", "0")
                return f'"{sid64}"\n\t{{{body}}}'

            new_txt = re.sub(r'"(\d{17})"\s*\{([^}]+)\}', repl_block, txt)
            with open(vdf_path, "w", encoding="utf-8") as f:
                f.write(new_txt)
        except Exception:
            pass

    build_full_state()
    return True, "OK"

def stop_steam_fast():
    """Stop Steam quickly so it can restart under the new account."""
    if not is_steam_running():
        return
    try:
        subprocess.run(
            ["taskkill", "/F", "/IM", "steam.exe", "/T"],
            creationflags=subprocess.CREATE_NO_WINDOW,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5
        )
    except Exception:
        pass
    for _ in range(15):
        if not is_steam_running():
            break
        time.sleep(0.15)

def switch_and_launch(target_account, appid=None, install_mode=False, game_name="", conflict_choice=""):
    reg = get_steam_registry_info()
    steam_path = reg["steam_path"]
    steam_exe = reg["steam_exe"]
    current_acc = reg["auto_login_user"]
    active_sid3 = reg["active_user_sid3"]
    running = is_steam_running()

    accounts = parse_loginusers(steam_path)
    acc_info = accounts.get(target_account, {})
    target_sid3 = int(acc_info.get("steamid3", -1))

    # 1. Pre-Launch Cloud Save Smart Sync (Only if Cloud Save is checked for this game)
    sync_note = ""
    cloud_cfg = load_cloud_config()
    game_cloud_on = bool(appid and is_game_cloud_enabled(appid))
    if appid and not install_mode and game_cloud_on and cloud_cfg.get("server_url") and cloud_cfg.get("username") and cloud_cfg.get("auto_sync", True):
        if conflict_choice == "local":
            ok_s, action_s, msg_s = sync_game_save(steam_path, appid, acc_info, game_name or f"App {appid}", direction="upload")
        elif conflict_choice == "cloud":
            ok_s, action_s, msg_s = sync_game_save(steam_path, appid, acc_info, game_name or f"App {appid}", direction="download")
        else:
            ok_s, action_s, msg_s = sync_game_save(
                steam_path, appid, acc_info, game_name or f"App {appid}", direction="auto", check_conflict=True
            )
            if action_s == "conflict" and isinstance(msg_s, dict):
                return True, {"conflict": True, **msg_s}

        if action_s in ("downloaded", "uploaded") and isinstance(msg_s, str):
            sync_note = f" • {msg_s}"

    # 2. Switch Steam Account if needed
    need_restart = (
        (not running)
        or (current_acc.lower() != target_account.lower())
        or (active_sid3 == 0)
        or (target_sid3 != -1 and active_sid3 != target_sid3)
    )

    if need_restart:
        if running:
            stop_steam_fast()
        ok, msg = set_steam_account(steam_path, target_account)
        if not ok:
            return False, msg

    # 3. Isolate local Steam achievement bin to match user's personal achievements before launching game
    if appid and not install_mode:
        apply_personal_achievements_to_steam_bin(steam_path, acc_info.get("steamid3", ""), appid)

    # 4. Launch Game or Steam
    if appid:
        if install_mode:
            cmd = [steam_exe, f"steam://install/{appid}"]
        else:
            cmd = [steam_exe, "-applaunch", str(appid)]
    else:
        cmd = [steam_exe]

    try:
        subprocess.Popen(
            cmd,
            close_fds=True,
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        )
        if appid and not install_mode:
            LAUNCHING_UNTIL[str(appid)] = time.time() + 18
        # 4. Start background watcher to auto-upload save when user exits the game
        if appid and not install_mode:
            t = threading.Thread(
                target=watch_game_and_auto_upload,
                args=(steam_path, appid, acc_info, game_name or f"App {appid}"),
                daemon=True
            )
            t.start()

        base_msg = f"Akun '{target_account}' siap" + (f" & menjalankan {game_name or appid}!" if appid else " & membuka Steam!")
        return True, base_msg + sync_note
    except Exception as e:
        return False, f"Gagal menjalankan Steam: {e}"


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


class LauncherRequestHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def send_json(self, data, status=200):
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        global LAST_UI_ACTIVITY_TS
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/" or path == "/index.html":
            LAST_UI_ACTIVITY_TS = time.time()
            index_path = os.path.join(ASSET_DIR, "index.html")
            if not os.path.exists(index_path):
                index_path = os.path.join(BASE_DIR, "index.html")
            if os.path.exists(index_path):
                html_str = open(index_path, "r", encoding="utf-8", errors="ignore").read()
                st = get_cached_state()
                ws = st.get("window_state", {})
                theme = ws.get("theme", "dark")
                html_str = html_str.replace('data-theme="dark"', f'data-theme="{theme}"', 1)
                inject_script = f"<script>window.__INITIAL_STATE__ = {json.dumps(st, ensure_ascii=False)};</script>"
                html_str = html_str.replace("<!--__INJECT_STATE__-->", inject_script, 1)
                raw = html_str.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(raw)
            else:
                self.send_error(404, "index.html not found")
            return

        if path == "/logo.png":
            raw = LOGO_PNG_BYTES or _load_asset_bytes("logo.png")
            if raw:
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Cache-Control", "public, max-age=31536000")
                self.end_headers()
                self.wfile.write(raw)
                return
            self.send_error(404)
            return

        if path == "/favicon.ico":
            raw = LOGO_ICO_BYTES or _load_asset_bytes("logo.ico") or LOGO_PNG_BYTES
            if raw:
                self.send_response(200)
                self.send_header("Content-Type", "image/x-icon")
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Cache-Control", "public, max-age=31536000")
                self.end_headers()
                self.wfile.write(raw)
                return
            self.send_error(404)
            return

        if path == "/overlay.html":
            raw = OVERLAY_HTML_PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)
            return

        if path == "/api/achievement_popups":
            with POPUP_LOCK:
                items = list(ACHIEVEMENT_POPUP_QUEUE)
                ACHIEVEMENT_POPUP_QUEUE.clear()
            self.send_json({"ok": True, "popups": items})
            return

        if path == "/api/state":
            LAST_UI_ACTIVITY_TS = time.time()
            state = build_full_state()
            self.send_json(state)
            return

        if path == "/api/running":
            LAST_UI_ACTIVITY_TS = time.time()
            self.send_json({
                "ok": True,
                "running_appids": get_running_appids(),
                "uploading_appids": get_uploading_appids(),
                "last_uploads": LAST_UPLOAD_MSG,
                "last_achievements": LAST_ACHIEVEMENT_UNLOCK,
                "active_account": get_steam_registry_info().get("auto_login_user", "")
            })
            return

        if path.startswith("/api/cover/"):
            appid = path.split("/api/cover/")[-1].split("?")[0]
            raw = COVER_CACHE.get(appid)
            if not raw:
                reg = get_steam_registry_info()
                local_img = find_local_cover(reg["steam_path"], appid)
                if local_img and os.path.exists(local_img):
                    try:
                        raw = open(local_img, "rb").read()
                        COVER_CACHE[appid] = raw
                    except Exception:
                        pass
            if raw:
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Cache-Control", "public, max-age=31536000, immutable")
                self.end_headers()
                self.wfile.write(raw)
                return
            cdn_url = f"https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/{appid}/library_600x900.jpg"
            self.send_response(302)
            self.send_header("Location", cdn_url)
            self.end_headers()
            return

        self.send_error(404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body_raw = self.rfile.read(length).decode("utf-8", errors="ignore") if length > 0 else "{}"
        try:
            payload = json.loads(body_raw)
        except Exception:
            payload = {}

        if self.path == "/api/launch":
            account = payload.get("account", "").strip()
            appid = str(payload.get("appid", "")).strip()
            game_name = payload.get("game_name", "").strip()
            install_mode = bool(payload.get("install_mode", False))
            conflict_choice = payload.get("conflict_choice", "").strip()
            if not account:
                self.send_json({"ok": False, "message": "Akun Steam belum dipilih!"}, 400)
                return
            ok, msg = switch_and_launch(
                account,
                appid if appid else None,
                install_mode=install_mode,
                game_name=game_name,
                conflict_choice=conflict_choice
            )
            if isinstance(msg, dict) and msg.get("conflict"):
                self.send_json({"ok": True, **msg})
                return
            self.send_json({"ok": ok, "message": msg})
            return

        if self.path == "/api/save_history":
            appid = str(payload.get("appid", "")).strip()
            account = payload.get("account", "").strip()
            reg = get_steam_registry_info()
            accounts = parse_loginusers(reg["steam_path"])
            acc_info = accounts.get(account, {})
            hist, local_info = get_combined_save_history(reg["steam_path"], appid, acc_info)
            self.send_json({"ok": True, "history": hist, "local_save": local_info})
            return

        if self.path == "/api/restore_save_history":
            appid = str(payload.get("appid", "")).strip()
            account = payload.get("account", "").strip()
            game_name = payload.get("game_name", "").strip() or f"App {appid}"
            source = payload.get("source", "cloud").strip()
            filename = payload.get("filename", "").strip()
            mtime_hint = int(payload.get("mtime", 0) or 0)
            reg = get_steam_registry_info()
            accounts = parse_loginusers(reg["steam_path"])
            acc_info = accounts.get(account, {})
            ok, msg = restore_save_from_history(reg["steam_path"], appid, acc_info, game_name, source, filename, mtime_hint=mtime_hint)
            self.send_json({"ok": ok, "message": msg})
            return

        if self.path == "/api/achievements":
            appid = str(payload.get("appid", "")).strip()
            account = payload.get("account", "").strip()
            reg = get_steam_registry_info()
            accounts = parse_loginusers(reg["steam_path"])
            acc_info = accounts.get(account, {})
            view = get_game_achievements_view(reg["steam_path"], appid, acc_info)
            self.send_json({"ok": True, **view})
            return

        if self.path == "/api/toggle_achievement":
            appid = str(payload.get("appid", "")).strip()
            account = payload.get("account", "").strip()
            game_name = payload.get("game_name", "").strip() or f"App {appid}"
            action = payload.get("action", "toggle").strip()
            ach_key = str(payload.get("ach_key", "")).strip()
            unlocked = bool(payload.get("unlocked", True))

            reg = get_steam_registry_info()
            steam_path = reg["steam_path"]
            accounts = parse_loginusers(steam_path)
            acc_info = accounts.get(account, {})
            sid3 = acc_info.get("steamid3", "")

            pers_map = get_game_personal_achievements(appid)
            schema = parse_game_achievement_schema(steam_path, appid)
            schema_by_key = {item["key"]: item for item in schema}

            if action == "test_popup":
                sample_item = schema[0] if schema else {
                    "key": "test:0",
                    "title": "Contoh Achievement Terbuka",
                    "description": "Notifikasi otomatis saat misi achievement selesai di dalam game.",
                    "icon_url": ""
                }
                for it in schema:
                    if it["key"] in pers_map:
                        sample_item = it
                        break
                enqueue_achievement_popup(appid, game_name, sample_item, len(pers_map) or 1, len(schema) or 1)
                view = get_game_achievements_view(steam_path, appid, acc_info)
                self.send_json({"ok": True, "message": "Popup notifikasi achievement ditampilkan di pojok kanan bawah layar!", **view})
                return

            if action == "reset_all":
                pers_map = {}
                set_game_personal_achievements(appid, pers_map, mark_reset=True)
                msg_out = f"Seluruh achievement '{game_name}' berhasil di-reset ke 0."
                sync_dir = "push"
            elif action == "sync_cloud":
                cfg = load_cloud_config()
                if not cfg.get("server_url") or not cfg.get("username"):
                    self.send_json({"ok": False, "message": "Silakan Login Cloud terlebih dahulu di kanan atas launcher."}, 400)
                    return
                sync_cloud_achievements_bundle(steam_path, direction="both")
                pers_map = get_game_personal_achievements(appid)
                rst_ts = get_game_reset_timestamp(appid)
                locked_map = get_game_locked_achievements(appid)
                dl_bytes, dl_err = cloud_request(f"/api/saves/{appid}/download", timeout=12)
                cloud_ach = {}
                if not dl_err and isinstance(dl_bytes, bytes):
                    try:
                        with zipfile.ZipFile(io.BytesIO(dl_bytes), "r") as zf:
                            if "_personal_achievements.json" in zf.namelist():
                                loaded_c = json.loads(zf.read("_personal_achievements.json").decode("utf-8", errors="ignore"))
                                if isinstance(loaded_c, dict):
                                    cloud_ach = loaded_c
                    except Exception:
                        pass
                for k, ts in cloud_ach.items():
                    try:
                        ts_i = int(ts)
                        k_lock_ts = int(locked_map.get(str(k), 0) or 0)
                        if ts_i > rst_ts and ts_i > k_lock_ts and (k not in pers_map or ts_i < pers_map[k]):
                            pers_map[str(k)] = ts_i
                    except Exception:
                        pass
                set_game_personal_achievements(appid, pers_map)
                msg_out = f"Sinkronisasi Cloud selesai! ({len(pers_map)} achievement tersinkron untuk '{game_name}')"
                sync_dir = "both"
            else:
                if unlocked:
                    pers_map[ach_key] = int(time.time())
                    set_game_personal_achievements(appid, pers_map, unlocked_key=ach_key)
                    msg_out = "Achievement ditandai terbuka!"
                    ach_item = schema_by_key.get(ach_key, {"key": ach_key, "title": ach_key, "description": "", "icon_url": ""})
                    enqueue_achievement_popup(appid, game_name, ach_item, len(pers_map), len(schema))
                else:
                    pers_map.pop(ach_key, None)
                    set_game_personal_achievements(appid, pers_map, locked_key=ach_key)
                    msg_out = "Achievement dikunci kembali."
                sync_dir = "push"

            apply_personal_achievements_to_steam_bin(steam_path, sid3, appid, pers_map)
            threading.Thread(target=sync_cloud_achievements_bundle, args=(steam_path, sync_dir), daemon=True).start()
            if is_game_cloud_enabled(appid) or action == "sync_cloud":
                threading.Thread(
                    target=sync_game_save,
                    args=(steam_path, appid, acc_info, game_name, "upload"),
                    daemon=True
                ).start()
            build_full_state()
            view = get_game_achievements_view(steam_path, appid, acc_info)
            self.send_json({"ok": True, "message": msg_out, **view})
            return

        if self.path == "/api/switch_account":
            account = payload.get("account", "").strip()
            if not account:
                self.send_json({"ok": False, "message": "Akun Steam belum dipilih!"}, 400)
                return
            ok, msg = switch_and_launch(account, appid=None)
            self.send_json({"ok": ok, "message": msg})
            return

        if self.path == "/api/toggle_offline":
            account = payload.get("account", "").strip()
            offline = bool(payload.get("offline", False))
            reg = get_steam_registry_info()
            ok, msg = set_steam_account(reg["steam_path"], account, offline_override=offline)
            if ok:
                self.send_json({"ok": True, "message": f"Mode {'Offline' if offline else 'Online'} diatur untuk {account}."})
            else:
                self.send_json({"ok": False, "message": msg}, 500)
            return

        if self.path == "/api/window_bounds":
            save_window_state(payload)
            if STATE_CACHE is not None:
                STATE_CACHE["window_state"] = load_window_state()
            self.send_json({"ok": True})
            return

        if self.path == "/api/open_window":
            threading.Thread(target=open_app_window, daemon=True).start()
            self.send_json({"ok": True})
            return

        if self.path == "/api/window_closed":
            save_window_state(payload)
            ws = load_window_state()
            if STATE_CACHE is not None:
                STATE_CACHE["window_state"] = ws
            self.send_json({"ok": True})
            if not ws.get("hide_to_tray_on_exit", True):
                close_ts = time.time()
                def _delayed_shutdown():
                    time.sleep(1.8)
                    # Only exit if no UI reconnected (e.g. reload) and no active game watcher
                    if LAST_UI_ACTIVITY_TS <= close_ts and not ACTIVE_WATCHERS:
                        shutdown_application()
                threading.Thread(target=_delayed_shutdown, daemon=True).start()
            return

        if self.path == "/api/cloud_auth":
            action = payload.get("action", "login")
            if action == "logout":
                save_cloud_config({"username": "", "password": ""})
                build_full_state()
                self.send_json({"ok": True, "message": "Berhasil logout dari Cloud Save."})
                return

            server_url = (payload.get("server_url") or "").strip().rstrip("/")
            username = (payload.get("username") or "").strip().lower()
            password = (payload.get("password") or "").strip()
            device_name = (payload.get("device_name") or "").strip()
            if not server_url or not username or not password:
                self.send_json({"ok": False, "message": "Server URL, Username, dan Password wajib diisi."}, 400)
                return

            endpoint = "/api/register" if action == "register" else "/api/login"
            req_body = json.dumps({"username": username, "password": password}).encode("utf-8")
            temp_cfg = {"server_url": server_url, "username": username, "password": password}
            resp, err = cloud_request(
                endpoint,
                method="POST",
                body=req_body,
                headers={"Content-Type": "application/json"},
                timeout=6,
                custom_cfg=temp_cfg
            )
            if err or not isinstance(resp, dict) or not resp.get("ok"):
                self.send_json({"ok": False, "message": err or "Gagal autentikasi ke Server Cloud."}, 400)
                return

            save_cloud_config({
                "server_url": server_url,
                "username": resp.get("username", username),
                "password": password,
                "device_name": device_name
            })
            def _post_login_ach_sync():
                try:
                    reg_i = get_steam_registry_info()
                    sync_cloud_achievements_bundle(reg_i["steam_path"], direction="both")
                    build_full_state()
                except Exception:
                    pass
            threading.Thread(target=_post_login_ach_sync, daemon=True).start()
            build_full_state()
            self.send_json({"ok": True, "message": resp.get("message", "Berhasil!")})
            return

        if self.path == "/api/toggle_game_cloud":
            appid = str(payload.get("appid", "")).strip()
            enabled = bool(payload.get("enabled", False))
            game_name = payload.get("game_name", "").strip() or f"App {appid}"
            cfg = load_cloud_config()
            cur_set = set(cfg.get("enabled_appids", []))
            if enabled:
                cur_set.add(appid)
            else:
                cur_set.discard(appid)
            save_cloud_config({"enabled_appids": sorted(list(cur_set))})

            uploading_now = False
            if enabled and cfg.get("server_url") and cfg.get("username"):
                UPLOADING_APPIDS.add(appid)
                UPLOADING_UNTIL[appid] = time.time() + 3.5
                uploading_now = True

                def _bg_upload_on_toggle():
                    try:
                        reg = get_steam_registry_info()
                        steam_path = reg["steam_path"]
                        accounts = parse_loginusers(steam_path)
                        g_match = next((g for g in (STATE_CACHE or {}).get("games", []) if str(g["appid"]) == appid), None)
                        owner_acc = g_match["owner_account"] if g_match else reg.get("auto_login_user", "")
                        acc_info = accounts.get(owner_acc, {})
                        ok_u, act_u, msg_u = sync_game_save(steam_path, appid, acc_info, game_name, direction="auto")
                        if isinstance(msg_u, str):
                            LAST_UPLOAD_MSG[appid] = {"ts": int(time.time()), "message": msg_u, "action": act_u}
                    except Exception:
                        pass
                    finally:
                        UPLOADING_APPIDS.discard(appid)
                        build_full_state()

                threading.Thread(target=_bg_upload_on_toggle, daemon=True).start()

            build_full_state()
            status_str = "diaktifkan" if enabled else "dinonaktifkan"
            self.send_json({
                "ok": True,
                "uploading": uploading_now,
                "message": f"Cloud Save {status_str} untuk {game_name}."
            })
            return

        if self.path == "/api/cloud_sync_all":
            st = build_full_state()
            steam_path = st["steam_path"]
            accounts = parse_loginusers(steam_path)
            synced_count = 0
            details = []
            enabled_games = [g for g in st["games"] if g.get("cloud_enabled")]
            if not enabled_games:
                self.send_json({"ok": True, "message": "Belum ada game yang dicentang Cloud Save."})
                return
            for g in enabled_games:
                acc_info = accounts.get(g["owner_account"], {})
                ok_s, act_s, msg_s = sync_game_save(steam_path, g["appid"], acc_info, g["name"], direction="auto")
                if ok_s and act_s in ("uploaded", "downloaded", "in_sync"):
                    synced_count += 1
                    if act_s in ("uploaded", "downloaded"):
                        details.append(msg_s)
            build_full_state()
            summary = f"Sinkronisasi selesai ({synced_count} game Cloud Save dicek)."
            if details:
                summary += " " + " | ".join(details[:2])
            self.send_json({"ok": True, "message": summary})
            return

        if self.path == "/api/game_patch":
            appid = str(payload.get("appid", "")).strip()
            game_name = payload.get("game_name", "").strip() or f"App {appid}"
            action = payload.get("action", "install").strip()
            reg = get_steam_registry_info()
            steam_path = reg["steam_path"]
            if action == "install":
                ok_p, msg_p = install_game_patch(steam_path, appid, game_name)
            elif action == "uninstall":
                ok_p, msg_p = uninstall_game_patch(steam_path, appid, game_name)
            elif action == "upload_server":
                ok_p, msg_p = upload_local_patch_to_cloud(appid, game_name)
            else:
                installed = get_installed_games(steam_path)
                p_info = get_game_patch_info(appid, game_name, installed.get(appid), fetch_cloud_patches_meta(force=True))
                self.send_json({"ok": True, **p_info})
                return
            installed = get_installed_games(steam_path)
            p_info = get_game_patch_info(appid, game_name, installed.get(appid))
            self.send_json({"ok": ok_p, "message": msg_p, **p_info})
            return

        self.send_error(404)


def is_port_in_use(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


WEBVIEW2_HOST_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA", tempfile.gettempdir()),
    "SteamSwitcher",
    "wv2_host"
)
UI_HOST_PROC = None

OVERLAY_HTML_PAGE = """<!DOCTYPE html>
<html lang="id">
<head>
<meta charset="UTF-8" />
<title>HIDE_OVERLAY</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; user-select: none; }
  html, body {
    width: 100%;
    height: 100%;
    background: transparent;
    overflow: hidden;
    font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
  }
  .toast-wrap {
    position: fixed;
    right: 12px;
    bottom: 12px;
    width: 364px;
    padding: 13px 15px;
    border-radius: 12px;
    background: linear-gradient(135deg, rgba(15, 23, 42, 0.97) 0%, rgba(23, 37, 84, 0.97) 100%);
    border: 1px solid rgba(250, 204, 21, 0.55);
    box-shadow: 0 14px 34px rgba(0, 0, 0, 0.75), 0 0 18px rgba(250, 204, 21, 0.18);
    display: flex;
    align-items: center;
    gap: 13px;
    transform: translateY(140%);
    opacity: 0;
    transition: transform 0.38s cubic-bezier(0.22, 1, 0.36, 1), opacity 0.32s ease;
  }
  .toast-wrap.visible {
    transform: translateY(0);
    opacity: 1;
  }
  .ach-icon {
    width: 54px;
    height: 54px;
    border-radius: 9px;
    object-fit: cover;
    flex-shrink: 0;
    border: 1.5px solid #facc15;
    background: #1e293b;
    box-shadow: 0 0 12px rgba(250, 204, 21, 0.35);
  }
  .ach-body {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 3px;
  }
  .ach-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 6px;
  }
  .ach-tag {
    font-size: 10px;
    font-weight: 800;
    letter-spacing: 0.7px;
    text-transform: uppercase;
    color: #facc15;
  }
  .ach-prog {
    font-size: 10px;
    font-weight: 700;
    color: #4ade80;
    background: rgba(34, 197, 94, 0.16);
    border: 1px solid rgba(34, 197, 94, 0.35);
    padding: 1px 6px;
    border-radius: 999px;
  }
  .ach-title {
    font-size: 13.5px;
    font-weight: 700;
    color: #f8fafc;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .ach-desc {
    font-size: 11.5px;
    color: #cbd5e1;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
    line-height: 1.3;
  }
</style>
</head>
<body>
  <div class="toast-wrap" id="toastBox">
    <img class="ach-icon" id="toastIcon" src="" alt="" />
    <div class="ach-body">
      <div class="ach-header">
        <span class="ach-tag">🏆 ACHIEVEMENT TERBUKA</span>
        <span class="ach-prog" id="toastProg">1/1</span>
      </div>
      <div class="ach-title" id="toastTitle">Achievement</div>
      <div class="ach-desc" id="toastDesc"></div>
    </div>
  </div>
<script>
  const FALLBACK_SVG = "data:image/svg+xml;utf8," + encodeURIComponent(
    '<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64" viewBox="0 0 64 64"><rect width="64" height="64" rx="10" fill="#1e293b"/><text x="50%" y="54%" dominant-baseline="middle" text-anchor="middle" font-size="28">🏆</text></svg>'
  );
  const iconEl = document.getElementById('toastIcon');
  iconEl.onerror = function() {
    const cur = iconEl.getAttribute('src') || '';
    if (!iconEl.dataset.cf && cur.includes('cdn.akamai.steamstatic.com')) {
      iconEl.dataset.cf = '1';
      iconEl.src = cur.replace('cdn.akamai.steamstatic.com', 'cdn.cloudflare.steamstatic.com');
      return;
    }
    iconEl.src = FALLBACK_SVG;
  };

  function playUnlockSound() {
    try {
      const Ctx = window.AudioContext || window.webkitAudioContext;
      if (!Ctx) return;
      const ctx = new Ctx();
      const now = ctx.currentTime;
      const notes = [523.25, 659.25, 783.99, 1046.50];
      notes.forEach((freq, idx) => {
        const osc = ctx.createOscillator();
        const gain = ctx.createGain();
        osc.type = 'triangle';
        osc.frequency.setValueAtTime(freq, now + idx * 0.085);
        gain.gain.setValueAtTime(0.001, now + idx * 0.085);
        gain.gain.exponentialRampToValueAtTime(0.18, now + idx * 0.085 + 0.02);
        gain.gain.exponentialRampToValueAtTime(0.0001, now + idx * 0.085 + 0.55);
        osc.connect(gain);
        gain.connect(ctx.destination);
        osc.start(now + idx * 0.085);
        osc.stop(now + idx * 0.085 + 0.6);
      });
    } catch (e) {}
  }

  let queue = [];
  let showing = false;

  async function processQueue() {
    if (showing || queue.length === 0) return;
    showing = true;
    const item = queue.shift();
    document.title = 'SHOW_OVERLAY';
    iconEl.dataset.cf = '';
    iconEl.src = item.icon_url || FALLBACK_SVG;
    document.getElementById('toastTitle').textContent = item.title || 'Achievement Unlocked';
    document.getElementById('toastDesc').textContent = item.description || item.game_name || '';
    document.getElementById('toastProg').textContent = `${item.unlocked_count || 1}/${item.total_count || 1}`;

    await new Promise(r => setTimeout(r, 60));
    playUnlockSound();
    document.getElementById('toastBox').classList.add('visible');

    await new Promise(r => setTimeout(r, 4800));
    document.getElementById('toastBox').classList.remove('visible');
    await new Promise(r => setTimeout(r, 420));

    if (queue.length === 0) {
      document.title = 'HIDE_OVERLAY';
    }
    showing = false;
    if (queue.length > 0) processQueue();
  }

  async function pollPopups() {
    try {
      const res = await fetch('/api/achievement_popups');
      const data = await res.json();
      if (data && Array.isArray(data.popups) && data.popups.length > 0) {
        queue.push(...data.popups);
        processQueue();
      }
    } catch (e) {}
  }
  setInterval(pollPopups, 1100);
</script>
</body>
</html>
"""

def ensure_webview2_host():
    """Ensure the bundled ~1MB native C# WebView2 host is unpacked to WEBVIEW2_HOST_DIR."""
    exe_path = os.path.join(WEBVIEW2_HOST_DIR, "SteamSwitcherUI.exe")
    bundled_dir = os.path.join(ASSET_DIR, "webview2_runtime")
    if not os.path.isdir(bundled_dir):
        bundled_dir = os.path.join(BASE_DIR, "webview2_runtime")

    if os.path.isdir(bundled_dir):
        os.makedirs(WEBVIEW2_HOST_DIR, exist_ok=True)
        for fn in os.listdir(bundled_dir):
            src = os.path.join(bundled_dir, fn)
            dst = os.path.join(WEBVIEW2_HOST_DIR, fn)
            if os.path.isfile(src):
                try:
                    need_copy = (
                        not os.path.exists(dst)
                        or os.path.getsize(src) != os.path.getsize(dst)
                        or int(os.path.getmtime(src)) != int(os.path.getmtime(dst))
                    )
                    if need_copy:
                        shutil.copy2(src, dst)
                except Exception:
                    pass
    if os.path.exists(exe_path):
        return exe_path
    return None


def open_app_window(hidden=False):
    global UI_HOST_PROC
    url = f"http://127.0.0.1:{PORT}"
    ws = load_window_state()
    w, h = ws.get("width", 1280), ws.get("height", 820)
    x, y = ws.get("x"), ws.get("y")

    host_exe = ensure_webview2_host()
    if host_exe and os.path.exists(host_exe):
        args = [
            host_exe,
            f"--app-url={url}",
            f"--win-w={w}",
            f"--win-h={h}",
        ]
        if x is not None and y is not None:
            args.append(f"--win-x={int(x)}")
            args.append(f"--win-y={int(y)}")
        if hidden:
            args.append("--hidden")
        proc = subprocess.Popen(
            args,
            cwd=WEBVIEW2_HOST_DIR,
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        )
        if UI_HOST_PROC is None or UI_HOST_PROC.poll() is not None:
            UI_HOST_PROC = proc
        return
    if not hidden:
        os.startfile(url)


def shutdown_application():
    global TRAY_ICON, UI_HOST_PROC
    try:
        if UI_HOST_PROC is not None and UI_HOST_PROC.poll() is None:
            UI_HOST_PROC.terminate()
    except Exception:
        pass
    try:
        subprocess.run(
            ["taskkill", "/F", "/IM", "SteamSwitcherUI.exe"],
            creationflags=subprocess.CREATE_NO_WINDOW,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2
        )
    except Exception:
        pass
    try:
        if TRAY_ICON is not None:
            TRAY_ICON.stop()
    except Exception:
        pass
    os._exit(0)


def start_system_tray():
    global TRAY_ICON
    try:
        import pystray
        from PIL import Image
        logo_bytes = LOGO_PNG_BYTES or _load_asset_bytes("logo.png")
        if logo_bytes:
            img = Image.open(io.BytesIO(logo_bytes)).convert("RGBA")
        else:
            img = Image.new("RGBA", (64, 64), (59, 130, 246, 255))

        def on_open(icon, item):
            open_app_window()

        def on_sync(icon, item):
            def _bg_sync():
                try:
                    st = build_full_state()
                    steam_path = st["steam_path"]
                    accounts = parse_loginusers(steam_path)
                    for g in st["games"]:
                        if not g.get("cloud_enabled"):
                            continue
                        acc_info = accounts.get(g["owner_account"], {})
                        sync_game_save(steam_path, g["appid"], acc_info, g["name"], direction="auto")
                    build_full_state()
                except Exception:
                    pass
            threading.Thread(target=_bg_sync, daemon=True).start()

        def on_exit(icon, item):
            shutdown_application()

        menu = pystray.Menu(
            pystray.MenuItem("Buka Steam Switcher", on_open, default=True),
            pystray.MenuItem("Sinkronkan Cloud Save", on_sync),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Keluar (Exit)", on_exit)
        )
        TRAY_ICON = pystray.Icon("SteamSwitcher", img, "Steam Switcher & Cloud Save", menu)
        threading.Thread(target=TRAY_ICON.run, daemon=True).start()
    except Exception:
        pass


if __name__ == "__main__":
    no_window = ("--no-window" in sys.argv) or ("--tray" in sys.argv)
    if is_port_in_use(PORT):
        if not no_window:
            try:
                req = urllib.request.Request(f"http://127.0.0.1:{PORT}/api/open_window", data=b"{}", method="POST")
                urllib.request.urlopen(req, timeout=1)
            except Exception:
                open_app_window()
        sys.exit(0)

    server = ThreadedHTTPServer(("127.0.0.1", PORT), LauncherRequestHandler)
    threading.Thread(target=build_full_state, daemon=True).start()
    threading.Thread(target=global_game_watcher_loop, daemon=True).start()
    open_app_window(hidden=no_window)
    start_system_tray()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        shutdown_application()

