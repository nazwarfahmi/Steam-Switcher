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
import queue
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
LOCAL_GAME_PROCS = {}
SCHEMA_FETCHING = set()
COVER_FETCHING = set()
PROC_SNAPSHOT_CACHE = {"ts": 0.0, "names": set()}
LOCAL_DATA_ROOT = os.path.join(os.environ.get("LOCALAPPDATA", tempfile.gettempdir()), "SteamSwitcher")

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
SPEED_MOD_DLL_BYTES = _load_asset_bytes(os.path.join("speed_plugin", "ss_fastforward64.dll"))
SPEED_CMD_QUEUE = queue.Queue()
ACTIVE_FF_HOOKS = {}
FF_HOOKS_LOCK = threading.Lock()
CAPTURE_PAD_CANCEL = threading.Event()

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

def _sanitize_speed_target(val, default=2.0):
    try:
        f = round(float(val), 2)
        if 0.2 <= f <= 20.0:
            return f
    except Exception:
        pass
    return default

def _sanitize_kb_bind(raw):
    if not isinstance(raw, dict):
        return None
    vks_raw = raw.get("vks")
    label = str(raw.get("label") or "").strip()
    if not isinstance(vks_raw, list) or not vks_raw or not label:
        return None
    vks = []
    for x in vks_raw[:4]:
        try:
            iv = int(x)
            if 1 <= iv <= 254:
                vks.append(iv)
        except Exception:
            pass
    return {"vks": vks, "label": label[:40]} if vks else None

def _sanitize_pad_bind(raw):
    if not isinstance(raw, dict):
        return None
    src = str(raw.get("source") or "xinput").strip().lower()
    if src not in ("xinput", "joy"):
        src = "xinput"
    try:
        mask = int(raw.get("mask") or 0)
    except Exception:
        mask = 0
    label = str(raw.get("label") or "").strip()
    if mask <= 0 or not label:
        return None
    return {"source": src, "mask": mask, "label": label[:40]}

def load_local_games():
    data = _reg_read_json("LocalGames")
    if not isinstance(data, dict):
        return {}
    clean = {}
    for k, v in data.items():
        aid = str(k).strip()
        if aid and isinstance(v, dict):
            clean[aid] = {
                "appid": aid,
                "name": str(v.get("name") or f"App {aid}"),
                "exe_path": os.path.normpath(v["exe_path"]) if v.get("exe_path") else "",
                "install_dir": os.path.normpath(v["install_dir"]) if v.get("install_dir") else "",
                "save_dir": os.path.normpath(v["save_dir"]) if v.get("save_dir") else "",
                "emu_type": str(v.get("emu_type") or ""),
                "speed_enabled": bool(v.get("speed_enabled", False)),
                "speed_target": _sanitize_speed_target(v.get("speed_target", 2.0), 2.0),
                "speed_kb_bind": _sanitize_kb_bind(v.get("speed_kb_bind")),
                "speed_pad_bind": _sanitize_pad_bind(v.get("speed_pad_bind")),
                "added_at": int(v.get("added_at") or 0),
                "last_played": int(v.get("last_played") or 0),
            }
    return clean

def save_local_games(data):
    if isinstance(data, dict):
        _reg_write_json("LocalGames", data)

WM_SS_APPLY_SPEED = 0x8000 + 0x535
SS_WPARAM_MAGIC = 0x5357

def _is_strictly_local_game(appid):
    aid_s = str(appid).strip()
    local_games = load_local_games()
    if aid_s not in local_games:
        return False
    reg = get_steam_registry_info()
    inst = get_installed_games(reg["steam_path"]).get(aid_s)
    if inst and not inst.get("is_local"):
        return False
    return True

def ensure_runtime_fastforward_dll():
    """Ensure helper DLL exists in %LOCALAPPDATA%\\SteamSwitcher (NEVER inside any game folder)."""
    dll_bytes = SPEED_MOD_DLL_BYTES or _load_asset_bytes(os.path.join("speed_plugin", "ss_fastforward64.dll"))
    if not dll_bytes:
        return ""
    os.makedirs(LOCAL_DATA_ROOT, exist_ok=True)
    dst_fp = os.path.join(LOCAL_DATA_ROOT, "ss_fastforward64.dll")
    try:
        need_write = (not os.path.isfile(dst_fp)) or (os.path.getsize(dst_fp) != len(dll_bytes))
        if not need_write:
            with open(dst_fp, "rb") as f:
                if f.read() != dll_bytes:
                    need_write = True
        if need_write:
            with open(dst_fp, "wb") as f:
                f.write(dll_bytes)
    except PermissionError:
        # Already mapped in an active session
        pass
    except Exception:
        pass
    return dst_fp if os.path.isfile(dst_fp) else ""

def get_local_game_running_pids(appid):
    """Return list of exact running PIDs belonging to this Local Game's executable (path-verified, excluding crash handlers)."""
    aid_s = str(appid).strip()
    lg = load_local_games().get(aid_s)
    if not lg:
        return []
    exe_path = os.path.normpath(lg.get("exe_path", "")).lower() if lg.get("exe_path") else ""
    install_dir = os.path.normpath(lg.get("install_dir", "")).lower() if lg.get("install_dir") else ""
    target_exe_name = os.path.basename(exe_path) if exe_path else ""

    pids = []
    try:
        k32 = ctypes.windll.kernel32
        k32.CreateToolhelp32Snapshot.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
        k32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
        k32.Process32FirstW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_PROCESSENTRY32W)]
        k32.Process32FirstW.restype = ctypes.c_int
        k32.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_PROCESSENTRY32W)]
        k32.Process32NextW.restype = ctypes.c_int
        k32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        k32.OpenProcess.restype = ctypes.c_void_p
        k32.QueryFullProcessImageNameW.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_uint32)]
        k32.QueryFullProcessImageNameW.restype = ctypes.c_int
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        k32.CloseHandle.restype = ctypes.c_int

        h_snap = k32.CreateToolhelp32Snapshot(0x00000002, 0)
        invalid_h = ctypes.c_void_p(-1).value
        if h_snap and h_snap != invalid_h:
            try:
                pe = _PROCESSENTRY32W()
                pe.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
                if k32.Process32FirstW(h_snap, ctypes.byref(pe)):
                    while True:
                        fn_low = (pe.szExeFile or "").lower()
                        pid_val = int(pe.th32ProcessID)
                        if fn_low and fn_low.endswith(".exe") and not fn_low.startswith(_IGNORED_EXE_PREFIXES):
                            cand_match = (target_exe_name and fn_low == target_exe_name) or (not target_exe_name and bool(install_dir))
                            if cand_match or install_dir:
                                hp = k32.OpenProcess(0x1000, 0, pid_val)  # PROCESS_QUERY_LIMITED_INFORMATION
                                if hp:
                                    try:
                                        buf_len = ctypes.c_uint32(520)
                                        pbuf = ctypes.create_unicode_buffer(520)
                                        if k32.QueryFullProcessImageNameW(hp, 0, pbuf, ctypes.byref(buf_len)):
                                            full_img = os.path.normpath(pbuf.value).lower()
                                            if (exe_path and full_img == exe_path) or (install_dir and full_img.startswith(install_dir + os.sep)):
                                                pids.append(pid_val)
                                    finally:
                                        k32.CloseHandle(hp)
                                elif target_exe_name and fn_low == target_exe_name:
                                    pids.append(pid_val)
                        if not k32.Process32NextW(h_snap, ctypes.byref(pe)):
                            break
            finally:
                k32.CloseHandle(h_snap)
    except Exception:
        pass
    return pids

def _find_game_window_and_tid(pids):
    """Find the visible main game window (hwnd, thread_id, pid) for the given running PIDs."""
    if not pids:
        return None, 0, 0
    pid_set = set(int(p) for p in pids)
    best = [None, 0, 0, 0]  # [hwnd, tid, pid, priority]
    try:
        u32 = ctypes.windll.user32
        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_ssize_t)
        u32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        u32.GetWindowThreadProcessId.restype = ctypes.c_uint32
        u32.IsWindowVisible.argtypes = [ctypes.c_void_p]
        u32.IsWindowVisible.restype = ctypes.c_int
        u32.GetClassNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
        u32.GetClassNameW.restype = ctypes.c_int
        u32.GetWindow.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        u32.GetWindow.restype = ctypes.c_void_p

        def _cb(hwnd, _lp):
            wpid = ctypes.c_uint32(0)
            tid = u32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
            if wpid.value in pid_set and tid and u32.IsWindowVisible(hwnd):
                cbuf = ctypes.create_unicode_buffer(128)
                u32.GetClassNameW(hwnd, cbuf, 127)
                cls_name = cbuf.value or ""
                if cls_name == "UnityWndClass":
                    best[:] = [hwnd, int(tid), int(wpid.value), 2]
                    return 0
                if best[3] < 1 and not u32.GetWindow(hwnd, 4):  # GW_OWNER == 4
                    best[:] = [hwnd, int(tid), int(wpid.value), 1]
            return 1

        cb_fn = WNDENUMPROC(_cb)
        u32.EnumWindows(cb_fn, 0)
    except Exception:
        pass
    return best[0], best[1], best[2]

class _XINPUT_GAMEPAD(ctypes.Structure):
    _fields_ = [
        ("wButtons", ctypes.c_uint16),
        ("bLeftTrigger", ctypes.c_uint8),
        ("bRightTrigger", ctypes.c_uint8),
        ("sThumbLX", ctypes.c_int16),
        ("sThumbLY", ctypes.c_int16),
        ("sThumbRX", ctypes.c_int16),
        ("sThumbRY", ctypes.c_int16),
    ]

class _XINPUT_STATE(ctypes.Structure):
    _fields_ = [
        ("dwPacketNumber", ctypes.c_uint32),
        ("Gamepad", _XINPUT_GAMEPAD),
    ]

class _JOYINFOEX(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_uint32), ("dwFlags", ctypes.c_uint32),
        ("dwXpos", ctypes.c_uint32), ("dwYpos", ctypes.c_uint32), ("dwZpos", ctypes.c_uint32),
        ("dwRpos", ctypes.c_uint32), ("dwUpos", ctypes.c_uint32), ("dwVpos", ctypes.c_uint32),
        ("dwButtons", ctypes.c_uint32), ("dwButtonNumber", ctypes.c_uint32),
        ("dwPOV", ctypes.c_uint32), ("dwReserved1", ctypes.c_uint32), ("dwReserved2", ctypes.c_uint32),
    ]

_XINPUT_DLL = None
_XINPUT_INIT_DONE = False
_WINMM_DLL = None

_XINPUT_BTN_NAMES = [
    (0x0040, "L3"),
    (0x0080, "R3"),
    (0x0100, "L1 / LB"),
    (0x0200, "R1 / RB"),
    (0x10000, "L2 / LT"),
    (0x20000, "R2 / RT"),
    (0x0020, "Select / Back"),
    (0x0010, "Start"),
    (0x0400, "Guide"),
    (0x1000, "A / ✕"),
    (0x2000, "B / ○"),
    (0x4000, "X / □"),
    (0x8000, "Y / △"),
    (0x0001, "D-Up"),
    (0x0002, "D-Down"),
    (0x0004, "D-Left"),
    (0x0008, "D-Right"),
]

def _get_xinput_func():
    global _XINPUT_DLL, _XINPUT_INIT_DONE
    if not _XINPUT_INIT_DONE:
        _XINPUT_INIT_DONE = True
        for dname in ("xinput1_4.dll", "xinput1_3.dll", "xinput9_1_0.dll"):
            try:
                dll = ctypes.WinDLL(dname)
                # Try ordinal 100 (XInputGetStateEx) first to also support Guide button, else XInputGetState
                try:
                    fn = dll[100]
                except Exception:
                    fn = dll.XInputGetState
                fn.argtypes = [ctypes.c_uint32, ctypes.POINTER(_XINPUT_STATE)]
                fn.restype = ctypes.c_uint32
                _XINPUT_DLL = fn
                break
            except Exception:
                pass
    return _XINPUT_DLL

def _read_gamepad_state():
    """Return ('xinput'|'joy', combined_button_mask) across connected controllers."""
    xi_fn = _get_xinput_func()
    if xi_fn is not None:
        any_xi_connected = False
        xi_mask = 0
        st = _XINPUT_STATE()
        for idx in range(4):
            try:
                res = xi_fn(idx, ctypes.byref(st))
                if res == 0:
                    any_xi_connected = True
                    m = int(st.Gamepad.wButtons)
                    if st.Gamepad.bLeftTrigger > 165:
                        m |= 0x10000
                    if st.Gamepad.bRightTrigger > 165:
                        m |= 0x20000
                    xi_mask |= m
            except Exception:
                pass
        if any_xi_connected:
            return "xinput", xi_mask

    global _WINMM_DLL
    if _WINMM_DLL is None:
        try:
            _WINMM_DLL = ctypes.WinDLL("winmm.dll")
            _WINMM_DLL.joyGetNumDevs.restype = ctypes.c_uint32
            _WINMM_DLL.joyGetPosEx.argtypes = [ctypes.c_uint32, ctypes.POINTER(_JOYINFOEX)]
            _WINMM_DLL.joyGetPosEx.restype = ctypes.c_uint32
        except Exception:
            _WINMM_DLL = False
    if _WINMM_DLL:
        try:
            ndevs = min(int(_WINMM_DLL.joyGetNumDevs()), 4)
            joy_mask = 0
            ji = _JOYINFOEX()
            ji.dwSize = ctypes.sizeof(_JOYINFOEX)
            ji.dwFlags = 0x0080  # JOY_RETURNBUTTONS
            for j in range(ndevs):
                if _WINMM_DLL.joyGetPosEx(j, ctypes.byref(ji)) == 0:
                    joy_mask |= int(ji.dwButtons)
            return "joy", joy_mask
        except Exception:
            pass
    return "xinput", 0

def _format_pad_mask_label(source, mask):
    parts = []
    if source == "xinput":
        for bit, name in _XINPUT_BTN_NAMES:
            if mask & bit:
                parts.append(name)
    else:
        for b in range(32):
            if mask & (1 << b):
                btn_no = b + 1
                if btn_no == 11:
                    parts.append("Btn 11 (L3)")
                elif btn_no == 12:
                    parts.append("Btn 12 (R3)")
                else:
                    parts.append(f"Stick Btn {btn_no}")
    return " + ".join(parts) if parts else f"Btn 0x{mask:X}"

def capture_gamepad_binding(timeout_sec=6.5):
    """Wait for user to press a controller button (e.g. L3, R3, or L1+R3 combo) and return {'source', 'mask', 'label'}."""
    CAPTURE_PAD_CANCEL.clear()
    t_end = time.time() + float(timeout_sec)

    # 1. Wait briefly for any currently held button to be released first
    while time.time() < t_end and not CAPTURE_PAD_CANCEL.is_set():
        _, init_m = _read_gamepad_state()
        if init_m == 0:
            break
        time.sleep(0.03)

    # 2. Wait for button press; accumulate peak mask for up to 350ms to allow 2-button combos (or finish immediately on release)
    peak_mask = 0
    detected_source = "xinput"
    press_start = 0.0

    while time.time() < t_end and not CAPTURE_PAD_CANCEL.is_set():
        src, cur_m = _read_gamepad_state()
        if cur_m != 0:
            if peak_mask == 0:
                press_start = time.time()
                detected_source = src
            peak_mask |= cur_m
            if (time.time() - press_start) >= 0.35:
                break
        elif peak_mask != 0:
            # User tapped and released cleanly (e.g. single tap L3 or R3)
            break
        time.sleep(0.025)

    if CAPTURE_PAD_CANCEL.is_set():
        return None, "Perekaman tombol stick dibatalkan."
    if peak_mask == 0:
        return None, "Tidak ada tombol stick yang terdeteksi. Pastikan stick terhubung lalu tekan tombolnya (misal L3 atau R3)."
    label = _format_pad_mask_label(detected_source, peak_mask)
    return {"source": detected_source, "mask": int(peak_mask), "label": label}, ""

def get_local_game_speed_state(appid, is_running=None):
    aid_s = str(appid).strip()
    lg = load_local_games().get(aid_s)
    if not lg:
        return {
            "speed_enabled": False,
            "speed_target": 2.0,
            "speed_current": 1.0,
            "speed_active": False,
            "speed_kb_bind": None,
            "speed_pad_bind": None,
            "speed_engine": ""
        }
    enabled = bool(lg.get("speed_enabled", False))
    target_spd = _sanitize_speed_target(lg.get("speed_target", 2.0), 2.0)
    kb_bind = _sanitize_kb_bind(lg.get("speed_kb_bind"))
    pad_bind = _sanitize_pad_bind(lg.get("speed_pad_bind"))
    if not enabled:
        return {
            "speed_enabled": False,
            "speed_target": target_spd,
            "speed_current": 1.0,
            "speed_active": False,
            "speed_kb_bind": kb_bind,
            "speed_pad_bind": pad_bind,
            "speed_engine": ""
        }
    if is_running is None:
        is_running = is_local_game_running(aid_s)
    cur_speed = 1.0
    if is_running:
        with FF_HOOKS_LOCK:
            rec = ACTIVE_FF_HOOKS.get(aid_s)
            if rec:
                cur_speed = round(float(rec.get("speed", 1.0)), 2)
    is_ff_on = bool(is_running and abs(cur_speed - 1.0) > 0.02)
    return {
        "speed_enabled": True,
        "speed_target": target_spd,
        "speed_current": cur_speed,
        "speed_active": is_ff_on,
        "speed_kb_bind": kb_bind,
        "speed_pad_bind": pad_bind,
        "speed_engine": "Runtime In-Memory"
    }

def enqueue_speed_popup(appid, game_name, speed_val, bind_hint=""):
    is_on = abs(speed_val - 1.0) > 0.02
    spd_txt = f"{speed_val:g}x"
    title_txt = f"Fast-Forward ON ({spd_txt})" if is_on else "Fast-Forward OFF (1x Normal)"
    desc_txt = f"{game_name}" + (f" • Toggle: {bind_hint}" if bind_hint else "")
    with POPUP_LOCK:
        ACHIEVEMENT_POPUP_QUEUE[:] = [p for p in ACHIEVEMENT_POPUP_QUEUE if p.get("kind") != "speed"]
        ACHIEVEMENT_POPUP_QUEUE.append({
            "id": f"speed:{appid}:{int(time.time()*1000)}",
            "kind": "speed",
            "appid": str(appid),
            "game_name": game_name,
            "title": title_txt,
            "description": desc_txt,
            "prog_label": spd_txt if is_on else "OFF",
            "ts": int(time.time())
        })

def detach_local_speed_hook(appid, restore_normal=True):
    """Queue clean detachment of runtime speed hook on the persistent hook owner thread."""
    aid_s = str(appid).strip()
    evt = threading.Event()
    box = {}
    SPEED_CMD_QUEUE.put(("detach", aid_s, 1.0 if restore_normal else 0.0, evt, box))
    evt.wait(timeout=1.5)

def detach_all_speed_hooks():
    with FF_HOOKS_LOCK:
        aids = list(ACTIVE_FF_HOOKS.keys())
    for aid_s in aids:
        detach_local_speed_hook(aid_s, restore_normal=True)

def set_local_game_speed(appid, multiplier, show_overlay=False, bind_hint=""):
    aid_s = str(appid).strip()
    if not _is_strictly_local_game(aid_s):
        return False, "Fitur fast-forward hanya tersedia untuk Game Lokal (Offline Single-Player)."
    lg = load_local_games().get(aid_s)
    if not lg or not lg.get("speed_enabled"):
        return False, "Aktifkan fitur Fast-Forward pada game lokal ini terlebih dahulu."

    spd = _sanitize_speed_target(multiplier, 1.0)

    evt = threading.Event()
    box = {}
    SPEED_CMD_QUEUE.put(("set", aid_s, spd, evt, box))
    evt.wait(timeout=2.5)

    if not box.get("ok"):
        return False, box.get("error") or "Gagal menghubungkan fast-forward ke jendela game yang sedang berjalan."

    gtitle = lg.get("name") or f"App {aid_s}"
    if show_overlay:
        enqueue_speed_popup(aid_s, gtitle, spd, bind_hint=bind_hint)

    st = get_local_game_speed_state(aid_s, is_running=True)
    st["speed_current"] = spd
    st["speed_active"] = abs(spd - 1.0) > 0.02
    spd_disp = f"{spd:g}x"
    return True, {
        "message": f"⚡ Fast-Forward '{gtitle}' NYALA ({spd_disp})!" if st["speed_active"] else f"⚡ Fast-Forward '{gtitle}' MATI (1x Normal).",
        **st
    }

def toggle_local_game_speed_now(appid, show_overlay=False, bind_hint=""):
    aid_s = str(appid).strip()
    lg = load_local_games().get(aid_s)
    if not lg or not lg.get("speed_enabled"):
        return False, "Fitur Fast-Forward belum diaktifkan untuk game ini."
    st = get_local_game_speed_state(aid_s, is_running=True)
    next_spd = 1.0 if st.get("speed_active") else _sanitize_speed_target(lg.get("speed_target", 2.0), 2.0)
    if abs(next_spd - 1.0) <= 0.02 and not st.get("speed_active"):
        return False, "Masukkan angka kecepatan selain 1x terlebih dahulu (misal 2 atau 3)."
    return set_local_game_speed(aid_s, next_spd, show_overlay=show_overlay, bind_hint=bind_hint)

def get_active_local_speeds_summary():
    out = {}
    local_games = load_local_games()
    for aid_s, lg in local_games.items():
        if not lg.get("speed_enabled"):
            continue
        if is_local_game_running(aid_s):
            st = get_local_game_speed_state(aid_s, is_running=True)
            out[aid_s] = {
                "speed": st["speed_current"],
                "active": st["speed_active"],
                "target": st["speed_target"],
                "engine": st["speed_engine"]
            }
    return out

def local_speed_hotkey_loop():
    """Persistent owner thread for Win32 runtime fast-forward hooks + in-game Alt+1..4 shortcuts (zero game files touched)."""
    k32 = ctypes.windll.kernel32
    u32 = ctypes.windll.user32

    k32.LoadLibraryW.argtypes = [ctypes.c_wchar_p]
    k32.LoadLibraryW.restype = ctypes.c_void_p
    k32.GetProcAddress.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    k32.GetProcAddress.restype = ctypes.c_void_p

    u32.SetWindowsHookExW.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32]
    u32.SetWindowsHookExW.restype = ctypes.c_void_p
    u32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
    u32.UnhookWindowsHookEx.restype = ctypes.c_int
    u32.SendMessageTimeoutW.argtypes = [
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_size_t, ctypes.c_ssize_t,
        ctypes.c_uint32, ctypes.c_uint32, ctypes.POINTER(ctypes.c_size_t)
    ]
    u32.SendMessageTimeoutW.restype = ctypes.c_ssize_t
    u32.IsWindow.argtypes = [ctypes.c_void_p]
    u32.IsWindow.restype = ctypes.c_int
    u32.GetAsyncKeyState.argtypes = [ctypes.c_int]
    u32.GetAsyncKeyState.restype = ctypes.c_short
    u32.GetForegroundWindow.argtypes = []
    u32.GetForegroundWindow.restype = ctypes.c_void_p
    u32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    u32.GetWindowThreadProcessId.restype = ctypes.c_uint32

    h_ff_dll = None
    fn_ff_proc = None

    def _ensure_hook_loaded():
        nonlocal h_ff_dll, fn_ff_proc
        if h_ff_dll and fn_ff_proc:
            return True
        dll_fp = ensure_runtime_fastforward_dll()
        if not dll_fp:
            return False
        h_ff_dll = k32.LoadLibraryW(dll_fp)
        if not h_ff_dll:
            return False
        fn_ff_proc = k32.GetProcAddress(h_ff_dll, b"FastForwardHookProc")
        return bool(fn_ff_proc)

    def _apply_on_owner_thread(aid_s, spd):
        pids = get_local_game_running_pids(aid_s)
        if not pids:
            return False, "Game sedang tidak berjalan."
        hwnd, tid, target_pid = _find_game_window_and_tid(pids)
        if not hwnd or not tid:
            return False, "Jendela utama game belum siap, tunggu beberapa detik lalu coba lagi."

        if not _ensure_hook_loaded():
            return False, "Modul runtime fast-forward tidak dapat dimuat."

        with FF_HOOKS_LOCK:
            existing = ACTIVE_FF_HOOKS.get(aid_s)

        # Clean up stale hook if window/thread changed (e.g. game restarted)
        if existing and (existing.get("tid") != tid or not u32.IsWindow(existing.get("hwnd"))):
            if existing.get("hhook"):
                u32.UnhookWindowsHookEx(existing["hhook"])
            with FF_HOOKS_LOCK:
                ACTIVE_FF_HOOKS.pop(aid_s, None)
            existing = None

        if abs(spd - 1.0) <= 0.02:
            if existing and existing.get("hhook"):
                lres = ctypes.c_size_t(0)
                u32.SendMessageTimeoutW(hwnd, WM_SS_APPLY_SPEED, SS_WPARAM_MAGIC, 1000, 0x0002, 500, ctypes.byref(lres))
                u32.UnhookWindowsHookEx(existing["hhook"])
            with FF_HOOKS_LOCK:
                ACTIVE_FF_HOOKS.pop(aid_s, None)
            return True, ""

        hhook = existing["hhook"] if existing else None
        if not hhook:
            # WH_CALLWNDPROC == 4, scoped strictly to this single game window's thread ID
            hhook = u32.SetWindowsHookExW(4, fn_ff_proc, h_ff_dll, int(tid))
            if not hhook:
                return False, "Gagal menempelkan pengatur kecepatan ke jendela game (coba jalankan aplikasi sebagai Administrator jika game berjalan elevated)."

        milli = max(100, min(20000, int(round(spd * 1000.0))))
        lres = ctypes.c_size_t(0)
        u32.SendMessageTimeoutW(hwnd, WM_SS_APPLY_SPEED, SS_WPARAM_MAGIC, milli, 0x0002, 600, ctypes.byref(lres))
        with FF_HOOKS_LOCK:
            ACTIVE_FF_HOOKS[aid_s] = {
                "pid": target_pid,
                "tid": tid,
                "hwnd": hwnd,
                "hhook": hhook,
                "speed": spd
            }
        return True, ""

    def _detach_on_owner_thread(aid_s, restore_normal):
        with FF_HOOKS_LOCK:
            rec = ACTIVE_FF_HOOKS.pop(aid_s, None)
        if rec and rec.get("hhook"):
            if restore_normal and rec.get("hwnd") and u32.IsWindow(rec["hwnd"]):
                lres = ctypes.c_size_t(0)
                u32.SendMessageTimeoutW(rec["hwnd"], WM_SS_APPLY_SPEED, SS_WPARAM_MAGIC, 1000, 0x0002, 400, ctypes.byref(lres))
            u32.UnhookWindowsHookEx(rec["hhook"])

    kb_armed = {}   # appid -> bool (True when ready to trigger on next press)
    pad_armed = {}  # appid -> bool
    last_cleanup_ts = 0.0
    modifier_vks = {16, 17, 18, 160, 161, 162, 163, 164, 165}

    msg_buf = (ctypes.c_ubyte * 64)()
    u32.PeekMessageW.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32]
    u32.PeekMessageW.restype = ctypes.c_int

    def _toggle_for_aid(aid_s, lg_obj, hint_label):
        with FF_HOOKS_LOCK:
            cur_rec = ACTIVE_FF_HOOKS.get(aid_s)
            cur_spd = float(cur_rec.get("speed", 1.0)) if cur_rec else 1.0
        target_spd = _sanitize_speed_target(lg_obj.get("speed_target", 2.0), 2.0)
        next_spd = 1.0 if abs(cur_spd - 1.0) > 0.02 else target_spd
        if abs(next_spd - 1.0) <= 0.02 and abs(cur_spd - 1.0) <= 0.02:
            return
        ok_t, _ = _apply_on_owner_thread(aid_s, next_spd)
        if ok_t:
            gtitle = lg_obj.get("name") or f"App {aid_s}"
            enqueue_speed_popup(aid_s, gtitle, next_spd, bind_hint=hint_label)

    while True:
        try:
            while u32.PeekMessageW(msg_buf, None, 0, 0, 1):
                pass
            # 1. Process any immediate UI commands on this owner thread
            while True:
                try:
                    cmd, c_aid, c_spd, c_evt, c_box = SPEED_CMD_QUEUE.get_nowait()
                except queue.Empty:
                    break
                try:
                    if cmd == "set":
                        ok_c, err_c = _apply_on_owner_thread(c_aid, c_spd)
                        c_box["ok"] = ok_c
                        c_box["error"] = err_c
                    elif cmd == "detach":
                        _detach_on_owner_thread(c_aid, c_spd > 0.5)
                        c_box["ok"] = True
                except Exception as ex:
                    c_box["ok"] = False
                    c_box["error"] = str(ex)
                finally:
                    c_evt.set()

            # 2. Periodically clean up hooks whose game window has closed
            now = time.time()
            if (now - last_cleanup_ts) > 1.5:
                last_cleanup_ts = now
                with FF_HOOKS_LOCK:
                    stale_aids = [k for k, v in ACTIVE_FF_HOOKS.items() if not u32.IsWindow(v.get("hwnd"))]
                for s_aid in stale_aids:
                    _detach_on_owner_thread(s_aid, False)

            # 3. Check custom Keyboard & Stick/Gamepad toggle bindings ONLY when opted-in Local Game is focused
            local_games = load_local_games()
            eligible = [
                (aid, lg) for aid, lg in local_games.items()
                if lg.get("speed_enabled") and (lg.get("speed_kb_bind") or lg.get("speed_pad_bind"))
            ]
            if not eligible:
                time.sleep(0.06)
                continue

            fg_hwnd = u32.GetForegroundWindow()
            fg_pid_val = 0
            if fg_hwnd:
                fg_pid = ctypes.c_uint32(0)
                u32.GetWindowThreadProcessId(fg_hwnd, ctypes.byref(fg_pid))
                fg_pid_val = int(fg_pid.value)

            if not fg_pid_val:
                time.sleep(0.05)
                continue

            focused_pair = None
            for aid, lg in eligible:
                pids = get_local_game_running_pids(aid)
                if fg_pid_val in pids:
                    focused_pair = (aid, lg)
                    break

            if not focused_pair:
                time.sleep(0.05)
                continue

            f_aid, f_lg = focused_pair

            # 3a. Custom Keyboard Toggle check
            kb_bind = f_lg.get("speed_kb_bind")
            if kb_bind and kb_bind.get("vks"):
                vks = kb_bind["vks"]
                all_pressed = all((u32.GetAsyncKeyState(int(vk)) & 0x8000) != 0 for vk in vks)
                # Avoid accidental trigger on Alt+F4 if user bound F4 without Alt
                if all_pressed and not any(int(vk) in modifier_vks for vk in vks):
                    if (u32.GetAsyncKeyState(0x12) & 0x8000) != 0:
                        all_pressed = False
                if all_pressed:
                    if kb_armed.get(f_aid, True):
                        kb_armed[f_aid] = False
                        _toggle_for_aid(f_aid, f_lg, kb_bind.get("label", "Keyboard"))
                else:
                    any_pressed = any((u32.GetAsyncKeyState(int(vk)) & 0x8000) != 0 for vk in vks)
                    if not any_pressed:
                        kb_armed[f_aid] = True

            # 3b. Custom Stick / Controller Toggle check (XInput + DirectInput)
            pad_bind = f_lg.get("speed_pad_bind")
            if pad_bind and pad_bind.get("mask"):
                req_mask = int(pad_bind["mask"])
                cur_src, cur_pad_m = _read_gamepad_state()
                if cur_src == pad_bind.get("source", "xinput") and (cur_pad_m & req_mask) == req_mask:
                    if pad_armed.get(f_aid, True):
                        pad_armed[f_aid] = False
                        _toggle_for_aid(f_aid, f_lg, pad_bind.get("label", "Stick"))
                elif (cur_pad_m & req_mask) == 0:
                    pad_armed[f_aid] = True

            time.sleep(0.035)
        except Exception:
            time.sleep(0.1)

def _get_cached_schema_json_path(appid):
    return os.path.join(LOCAL_DATA_ROOT, "schemas", f"{appid}.json")

def fetch_remote_achievement_schema(appid):
    """Fetch official Steam achievement schema (including internal API names, titles, descriptions, icons, and bit keys) without owning the game."""
    aid_s = str(appid).strip()
    if not aid_s.isdigit():
        return []
    url = f"https://api.steampowered.com/IPlayerService/GetGameAchievements/v1/?appid={aid_s}&language=english"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 SteamSwitcher/2.0"})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="ignore"))
        raw_list = (payload.get("response") or {}).get("achievements") or []
        achievements = []
        for idx, item in enumerate(raw_list):
            if not isinstance(item, dict):
                continue
            internal_key = item.get("internal_key")
            if isinstance(internal_key, int) and internal_key >= 0:
                stat_id = str(internal_key >> 8)
                bit_num = int(internal_key & 0xFF)
            else:
                stat_id = str((idx // 32) + 1)
                bit_num = idx % 32
            api_name = str(item.get("internal_name") or f"ACH_{stat_id}_{bit_num}")
            title = str(item.get("localized_name") or api_name)
            desc = str(item.get("localized_desc") or "")
            icon_fn = str(item.get("icon") or "")
            icon_gray_fn = str(item.get("icon_gray") or icon_fn)
            hidden_flag = bool(item.get("hidden", False))
            achievements.append({
                "key": f"{stat_id}:{bit_num}",
                "stat_id": stat_id,
                "bit_id": bit_num,
                "api_name": api_name,
                "title": title,
                "description": desc,
                "hidden": hidden_flag,
                "icon_url": f"https://cdn.akamai.steamstatic.com/steamcommunity/public/images/apps/{aid_s}/{icon_fn}" if icon_fn else "",
                "icon_gray_url": f"https://cdn.akamai.steamstatic.com/steamcommunity/public/images/apps/{aid_s}/{icon_gray_fn}" if icon_gray_fn else ""
            })
        achievements.sort(key=lambda x: (int(x["stat_id"]) if x["stat_id"].isdigit() else 0, x["bit_id"]))
        cache_fp = _get_cached_schema_json_path(aid_s)
        os.makedirs(os.path.dirname(cache_fp), exist_ok=True)
        with open(cache_fp, "w", encoding="utf-8") as f:
            json.dump({"fetched_at": int(time.time()), "achievements": achievements}, f, ensure_ascii=False)
        if achievements:
            SCHEMA_CACHE[aid_s] = achievements
        return achievements
    except Exception:
        return []

def parse_game_achievement_schema(steam_path, appid, allow_remote=False):
    aid_s = str(appid)
    if aid_s in SCHEMA_CACHE and SCHEMA_CACHE[aid_s]:
        return SCHEMA_CACHE[aid_s]

    schema_path = os.path.join(steam_path, "appcache", "stats", f"UserGameStatsSchema_{aid_s}.bin") if steam_path else ""
    if schema_path and os.path.exists(schema_path):
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
            if achievements:
                SCHEMA_CACHE[aid_s] = achievements
                return achievements
        except Exception:
            pass

    # Fallback 1: Local cached JSON schema from Steam Web API
    cache_fp = _get_cached_schema_json_path(aid_s)
    if os.path.exists(cache_fp):
        try:
            cached_obj = json.loads(open(cache_fp, "r", encoding="utf-8", errors="ignore").read())
            c_list = cached_obj.get("achievements", []) if isinstance(cached_obj, dict) else []
            if isinstance(c_list, list) and c_list:
                SCHEMA_CACHE[aid_s] = c_list
                return c_list
            # Avoid repeated fetches if confirmed 0 achievements within last 12h
            if isinstance(cached_obj, dict) and (time.time() - int(cached_obj.get("fetched_at", 0))) < 43200:
                return []
        except Exception:
            pass

    # Fallback 2: Fetch from public Steam Web API on-demand
    if allow_remote:
        return fetch_remote_achievement_schema(aid_s)

    return []

def get_emulator_achievement_candidates(appid, install_dir=""):
    """Return list of (emu_kind, file_path) for known Steam emulators (RUNE, CODEX, Goldberg, GSE, FLT, OnlineFix, Tenoke, SKIDROW/STEAMRIP)."""
    aid_s = str(appid).strip()
    sys_drive = os.environ.get("SystemDrive", "C:")
    pub_docs = os.path.join(os.environ.get("PUBLIC", os.path.join(sys_drive, "Users", "Public")), "Documents")
    user_docs = os.path.join(os.environ.get("USERPROFILE", os.path.expanduser("~")), "Documents")
    appdata = os.environ.get("APPDATA", os.path.expanduser(r"~\AppData\Roaming"))
    localapp = os.environ.get("LOCALAPPDATA", os.path.expanduser(r"~\AppData\Local"))

    if not install_dir:
        lg = load_local_games().get(aid_s)
        if lg and lg.get("install_dir"):
            install_dir = lg["install_dir"]

    raw_cands = [
        ("rune_ini", os.path.join(pub_docs, "Steam", "RUNE", aid_s, "achievements.ini")),
        ("rune_ini", os.path.join(sys_drive, "\\Users\\Public\\Documents\\Steam\\RUNE", aid_s, "achievements.ini")),
        ("rune_ini", os.path.join(appdata, "Steam", "RUNE", aid_s, "achievements.ini")),
        ("codex_ini", os.path.join(pub_docs, "Steam", "CODEX", aid_s, "achievements.ini")),
        ("codex_ini", os.path.join(sys_drive, "\\Users\\Public\\Documents\\Steam\\CODEX", aid_s, "achievements.ini")),
        ("codex_ini", os.path.join(appdata, "Steam", "CODEX", aid_s, "achievements.ini")),
        ("goldberg_json", os.path.join(appdata, "Goldberg SteamEmu Saves", aid_s, "achievements.json")),
        ("goldberg_json", os.path.join(appdata, "GSE Saves", aid_s, "achievements.json")),
        ("flt_ini", os.path.join(appdata, "FLT", aid_s, "stats", "achievements.ini")),
        ("onlinefix_ini", os.path.join(pub_docs, "OnlineFix", aid_s, "Stats", "Achievements.ini")),
        ("skidrow_ini", os.path.join(localapp, "SKIDROW", aid_s, "Storage", "achievements.ini")),
        ("skidrow_at_ini", os.path.join(user_docs, "STEAMRIP", aid_s, "SteamEmu", "UserStats", "achiev.ini")),
        ("skidrow_at_ini", os.path.join(user_docs, "SKIDROW", aid_s, "SteamEmu", "UserStats", "achiev.ini")),
        ("sse_ini", os.path.join(appdata, "SmartSteamEmu", aid_s, "achievements.ini")),
    ]
    if os.path.isdir(user_docs):
        for sr_fp in glob.glob(os.path.join(user_docs, "*", aid_s, "SteamEmu", "UserStats", "achiev.ini")):
            raw_cands.append(("skidrow_at_ini", sr_fp))

    if install_dir and os.path.isdir(install_dir):
        raw_cands.extend([
            ("tenoke_ini", os.path.join(install_dir, "SteamData", "user_stats.ini")),
            ("goldberg_json", os.path.join(install_dir, "steam_settings", "settings", "achievements.json")),
            ("codex_ini", os.path.join(install_dir, "Profile", "Stats", "Achievements.ini")),
            ("codex_ini", os.path.join(install_dir, "Profile", "VALVE", "Stats", "Achievements.ini")),
        ])
        try:
            for r_dir, d_list, f_list in os.walk(install_dir):
                rel_d = os.path.relpath(r_dir, install_dir)
                if rel_d != "." and rel_d.count(os.sep) >= 4:
                    d_list[:] = []
                    continue
                if "user_stats.ini" in [x.lower() for x in f_list] and os.path.basename(r_dir).lower() == "steamdata":
                    raw_cands.append(("tenoke_ini", os.path.join(r_dir, "user_stats.ini")))
        except Exception:
            pass

    seen = set()
    unique = []
    for kind, fp in raw_cands:
        norm = os.path.normpath(fp)
        low = norm.lower()
        if low not in seen:
            seen.add(low)
            unique.append((kind, norm))
    return unique

def _parse_emulator_achievement_file(kind, fp):
    """Parse an emulator achievements.ini / achievements.json file into {api_name: unlock_timestamp}."""
    unlocked_by_api = {}
    if not os.path.isfile(fp):
        return unlocked_by_api
    try:
        file_mtime = int(os.path.getmtime(fp)) or int(time.time())
    except Exception:
        file_mtime = int(time.time())

    try:
        raw_txt = open(fp, "r", encoding="utf-8", errors="ignore").read()
    except Exception:
        return unlocked_by_api

    if kind == "goldberg_json" or fp.lower().endswith(".json"):
        try:
            obj = json.loads(raw_txt)
            if isinstance(obj, dict):
                for api_name, info in obj.items():
                    if isinstance(info, dict):
                        if info.get("earned") in (True, 1, "1", "true") or info.get("achieved") in (True, 1, "1", "true") or info.get("unlocked") in (True, 1, "1", "true"):
                            ts = int(info.get("earned_time") or info.get("unlock_time") or info.get("UnlockTime") or info.get("time") or file_mtime)
                            unlocked_by_api[str(api_name).strip()] = ts if ts > 0 else file_mtime
                    elif info in (True, 1, "1", "true"):
                        unlocked_by_api[str(api_name).strip()] = file_mtime
            elif isinstance(obj, list):
                for entry in obj:
                    if isinstance(entry, dict):
                        aname = str(entry.get("name") or entry.get("id") or "").strip()
                        if aname and (entry.get("achieved") in (True, 1, "1", "true") or entry.get("earned") in (True, 1, "1", "true")):
                            ts = int(entry.get("UnlockTime") or entry.get("earned_time") or file_mtime)
                            unlocked_by_api[aname] = ts if ts > 0 else file_mtime
        except Exception:
            pass
        return unlocked_by_api

    # Parse INI / plain-list formats (RUNE, CODEX, Tenoke, OnlineFix, FLT, ALI213, SKIDROW)
    sections = {}
    cur_sec = ""
    plain_lines = []
    for raw_line in raw_txt.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(";") or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            cur_sec = line[1:-1].strip()
            sections.setdefault(cur_sec, {})
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            sections.setdefault(cur_sec, {})[k.strip()] = v.strip()
        elif not cur_sec:
            plain_lines.append(line)

    # 1. Plain list (e.g. FLT stats/achievements.ini with one API_NAME per line)
    if not sections and plain_lines:
        for pl in plain_lines:
            unlocked_by_api[pl] = file_mtime
        return unlocked_by_api

    sec_ci = {s.lower(): s for s in sections}

    # 2. [SteamAchievements] index block (RUNE / CODEX)
    sa_sec_key = sec_ci.get("steamachievements")
    if sa_sec_key:
        for k, v in sections[sa_sec_key].items():
            if k.lower() == "count":
                continue
            cand_name = v.strip().strip('"')
            if not cand_name:
                continue
            own_sec_key = sec_ci.get(cand_name.lower())
            if own_sec_key is None:
                unlocked_by_api[cand_name] = file_mtime
            else:
                own_sec = sections[own_sec_key]
                own_ci = {x.lower(): y for x, y in own_sec.items()}
                ach_val = str(own_ci.get("achieved", own_ci.get("state", own_ci.get("unlocked", "0")))).strip().lower()
                if ach_val in ("1", "true", "yes"):
                    ts_raw = str(own_ci.get("unlocktime", own_ci.get("time", "0"))).strip()
                    ts_i = int(ts_raw) if ts_raw.isdigit() and int(ts_raw) > 0 else file_mtime
                    unlocked_by_api[own_sec_key] = ts_i

    # 3. Individual [Achievement_API_Name] sections & [ACHIEVEMENTS] block (Tenoke / OnlineFix / SKIDROW / CODEX)
    for sec_name, kv in sections.items():
        if not sec_name:
            continue
        s_low = sec_name.lower()
        if s_low in ("steamachievements", "steamstats", "stats", "leaderboards", "settings", "userstats"):
            continue
        if s_low in ("achievements", "achiev"):
            for ak, av in kv.items():
                clean_k = ak.strip().strip('"')
                av_str = av.strip()
                av_low = av_str.lower()
                # SKIDROW SteamEmu format: API_NAME=1@API_NAME@...@<unlock_timestamp>
                if "@" in av_str:
                    at_parts = av_str.split("@")
                    if at_parts and at_parts[0].strip() in ("1", "true"):
                        last_ts = at_parts[-1].strip()
                        ts_i = int(last_ts) if last_ts.isdigit() and int(last_ts) > 0 else file_mtime
                        unlocked_by_api[clean_k] = ts_i
                    continue
                if av_low in ("1", "true", "yes") or "unlocked = true" in av_low or "unlocked=true" in av_low or "achieved = 1" in av_low:
                    m_ts = re.search(r"time\s*=\s*(\d+)", av_low)
                    ts_i = int(m_ts.group(1)) if (m_ts and int(m_ts.group(1)) > 0) else file_mtime
                    unlocked_by_api[clean_k] = ts_i
        else:
            kv_ci = {x.lower(): y for x, y in kv.items()}
            ach_flag = str(kv_ci.get("achieved") or kv_ci.get("unlocked") or kv_ci.get("state") or "").strip().lower()
            if ach_flag in ("1", "true", "yes"):
                ts_raw = str(kv_ci.get("unlocktime") or kv_ci.get("time") or kv_ci.get("timestamp") or "0").strip()
                ts_i = int(ts_raw) if ts_raw.isdigit() and int(ts_raw) > 0 else file_mtime
                unlocked_by_api[sec_name] = ts_i

    return unlocked_by_api

def read_emulator_achievements(steam_path, appid, install_dir=""):
    """Read unlocked achievement keys and timestamps from RUNE/CODEX/Goldberg/GSE/Tenoke/OnlineFix/FLT emulator files."""
    aid_s = str(appid)
    cands = get_emulator_achievement_candidates(aid_s, install_dir=install_dir)
    existing = [(kind, fp) for kind, fp in cands if os.path.isfile(fp)]
    if not existing:
        return {}

    raw_api_unlocked = {}
    for kind, fp in existing:
        parsed_map = _parse_emulator_achievement_file(kind, fp)
        for aname, ts in parsed_map.items():
            if aname not in raw_api_unlocked or ts < raw_api_unlocked[aname]:
                raw_api_unlocked[aname] = ts

    if not raw_api_unlocked:
        return {}

    schema = parse_game_achievement_schema(steam_path, aid_s, allow_remote=True)
    api_to_key = {}
    for item in schema:
        aname = str(item.get("api_name", "")).strip()
        if aname:
            api_to_key[aname] = item["key"]
            api_to_key[aname.lower()] = item["key"]

    unlocked = {}
    for aname, ts in raw_api_unlocked.items():
        matched_key = api_to_key.get(aname) or api_to_key.get(aname.lower())
        if matched_key:
            unlocked[matched_key] = int(ts)
    return unlocked

def apply_personal_achievements_to_emu_files(appid, personal_map, schema):
    """Sync personal achievements & pre-seed full schema into RUNE/CODEX achievements.ini (so GetAchievement returns True!) or Goldberg JSON."""
    aid_s = str(appid)
    if not schema:
        return False
    all_api_names = []
    seen_api = set()
    key_to_api = {}
    for item in schema:
        an = str(item.get("api_name") or "").strip()
        if an:
            key_to_api[item["key"]] = an
            if an not in seen_api:
                seen_api.add(an)
                all_api_names.append(an)

    unlocked_api = {}
    for k, ts in (personal_map or {}).items():
        if ts and int(ts) > 0 and k in key_to_api:
            unlocked_api[key_to_api[k]] = int(ts)

    # Ensure primary RUNE / CODEX Public Documents folder exists for registered RUNE/CODEX Local Games even before first launch
    lg = load_local_games().get(aid_s)
    if lg:
        emu_t = (lg.get("emu_type") or "").strip().upper()
        sys_drive = os.environ.get("SystemDrive", "C:")
        pub_docs = os.path.join(os.environ.get("PUBLIC", os.path.join(sys_drive, "Users", "Public")), "Documents")
        if emu_t == "RUNE":
            os.makedirs(os.path.join(pub_docs, "Steam", "RUNE", aid_s), exist_ok=True)
        elif emu_t == "CODEX":
            os.makedirs(os.path.join(pub_docs, "Steam", "CODEX", aid_s), exist_ok=True)

    wrote = False
    for kind, fp in get_emulator_achievement_candidates(aid_s):
        parent_dir = os.path.dirname(fp)
        if not os.path.isfile(fp):
            if kind not in ("rune_ini", "codex_ini", "goldberg_json") or not os.path.isdir(parent_dir):
                continue
        try:
            if kind == "goldberg_json":
                cur_obj = {}
                if os.path.isfile(fp):
                    try:
                        loaded = json.loads(open(fp, "r", encoding="utf-8", errors="ignore").read())
                        if isinstance(loaded, dict):
                            cur_obj = loaded
                    except Exception:
                        pass
                new_obj = {}
                for aname in all_api_names:
                    if aname in unlocked_api:
                        new_obj[aname] = {"earned": True, "earned_time": int(unlocked_api[aname])}
                    else:
                        prev_v = cur_obj.get(aname)
                        if isinstance(prev_v, dict):
                            vc = dict(prev_v)
                            vc["earned"] = False
                            vc["earned_time"] = 0
                            new_obj[aname] = vc
                        else:
                            new_obj[aname] = {"earned": False, "earned_time": 0}
                with open(fp, "w", encoding="utf-8") as f:
                    json.dump(new_obj, f, indent=2)
                wrote = True
            elif kind in ("rune_ini", "codex_ini"):
                preserved_progress = {}
                if os.path.isfile(fp):
                    try:
                        cur_txt = open(fp, "r", encoding="utf-8", errors="ignore").read()
                        cur_sec = ""
                        sec_map = {}
                        for raw_l in cur_txt.splitlines():
                            l = raw_l.strip()
                            if l.startswith("[") and l.endswith("]"):
                                cur_sec = l[1:-1].strip()
                                sec_map.setdefault(cur_sec, {})
                            elif "=" in l and cur_sec:
                                kk, vv = l.split("=", 1)
                                sec_map.setdefault(cur_sec, {})[kk.strip()] = vv.strip()
                        for s_name, kv in sec_map.items():
                            if s_name.lower() in ("steamachievements", ""):
                                continue
                            preserved_progress[s_name] = kv
                    except Exception:
                        pass

                # IMPORTANT: RUNE/CODEX GetAchievement(apiName) returns FALSE unless apiName is listed in [SteamAchievements]!
                # Register ALL schema achievements in [SteamAchievements] with Achieved=1 (unlocked) or Achieved=0 (locked).
                ordered_names = list(all_api_names)
                for extra_u in unlocked_api:
                    if extra_u not in seen_api:
                        ordered_names.append(extra_u)

                lines = ["[SteamAchievements]"]
                for idx, aname in enumerate(ordered_names):
                    lines.append(f"{idx:05d}={aname}")
                lines.append(f"Count={len(ordered_names)}")
                lines.append("")

                for aname in ordered_names:
                    prev_kv = preserved_progress.get(aname, {})
                    prev_ci = {a.lower(): b for a, b in prev_kv.items()}
                    cur_p = str(prev_ci.get("curprogress", "0"))
                    max_p = str(prev_ci.get("maxprogress", "0"))
                    lines.append(f"[{aname}]")
                    if aname in unlocked_api:
                        lines.append("Achieved=1")
                        lines.append(f"CurProgress={cur_p}")
                        lines.append(f"MaxProgress={max_p}")
                        lines.append(f"UnlockTime={int(unlocked_api[aname])}")
                    else:
                        lines.append("Achieved=0")
                        lines.append(f"CurProgress={cur_p}")
                        lines.append(f"MaxProgress={max_p}")
                        lines.append("UnlockTime=0")
                    lines.append("")

                with open(fp, "w", encoding="utf-8", newline="\r\n") as f:
                    f.write("\n".join(lines))
                wrote = True
        except Exception:
            pass
    return wrote

def read_steam_user_achievements(steam_path, sid3, appid):
    """Read unlocked achievement keys and timestamps from Steam's UserGameStats_<sid3>_<appid>.bin AND any local Steam Emulator files."""
    aid_s = str(appid)
    schema = parse_game_achievement_schema(steam_path, aid_s)
    ach_stat_ids = {item["stat_id"] for item in schema}
    stats_dir = os.path.join(steam_path, "appcache", "stats") if steam_path else ""
    candidates = []
    if stats_dir and sid3:
        p_main = os.path.join(stats_dir, f"UserGameStats_{sid3}_{aid_s}.bin")
        if os.path.exists(p_main):
            candidates.append(p_main)
    if stats_dir and not candidates:
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

    emu_unlocked = read_emulator_achievements(steam_path, aid_s)
    for k, ts in emu_unlocked.items():
        if k not in unlocked or (ts > 0 and ts < unlocked[k]):
            unlocked[k] = ts

    return unlocked

def load_protected_accounts(steam_path=None):
    """Load list of protected Steam usernames/IDs whose achievement .bin files must NEVER be overwritten."""
    data = _reg_read_json("ProtectedAccounts")
    if isinstance(data, list):
        return sorted(list({str(x).strip().lower() for x in data if str(x).strip()}))
    # Auto-seed on first run with any currently Online Steam accounts (personal accounts)
    seeded = set()
    sp = steam_path or get_steam_registry_info().get("steam_path", "")
    if sp:
        for acc in parse_loginusers(sp).values():
            if not acc.get("offline_mode") and acc.get("account_name"):
                seeded.add(acc["account_name"].strip().lower())
    seeded_list = sorted(list(seeded))
    _reg_write_json("ProtectedAccounts", seeded_list)
    return seeded_list

def save_protected_accounts(acc_list):
    if isinstance(acc_list, list):
        clean = sorted(list({str(x).strip().lower() for x in acc_list if str(x).strip()}))
        _reg_write_json("ProtectedAccounts", clean)
        return clean
    return load_protected_accounts()

def is_account_protected_from_overwrite(acc_info, protected_set=None):
    """Return True if an account is listed in ProtectedAccounts OR is currently in Online mode."""
    if not isinstance(acc_info, dict):
        return True
    if protected_set is None:
        protected_set = set(load_protected_accounts())
    acc_name = str(acc_info.get("account_name") or "").strip().lower()
    pers_name = str(acc_info.get("persona_name") or "").strip().lower()
    sid64 = str(acc_info.get("steamid64") or "").strip().lower()
    if (acc_name and acc_name in protected_set) or (pers_name and pers_name in protected_set) or (sid64 and sid64 in protected_set):
        return True
    if not acc_info.get("offline_mode", False):
        return True
    return False

def _get_offline_and_online_sid3_sets(steam_path):
    isolatable_offline_sids = set()
    protected_sids = set()
    prot_names = set(load_protected_accounts(steam_path))
    if steam_path:
        for acc in parse_loginusers(steam_path).values():
            s3 = str(acc.get("steamid3") or "").strip()
            if not s3:
                continue
            if is_account_protected_from_overwrite(acc, prot_names):
                protected_sids.add(s3)
            else:
                isolatable_offline_sids.add(s3)
    return isolatable_offline_sids, protected_sids

def apply_personal_achievements_to_steam_bin(steam_path, sid3, appid, personal_map=None):
    """Write personal achievement bitmasks ONLY into Offline/Shared Steam account bins and idle Steam Emulator files.
    NEVER overwrites UserGameStats_<sid3>_<appid>.bin of Online (personal) Steam accounts."""
    aid_s = str(appid)
    if personal_map is None:
        personal_map = get_game_personal_achievements(aid_s)

    schema = parse_game_achievement_schema(steam_path, aid_s)
    wrote_emu = False
    if schema and aid_s in load_local_games() and not is_local_game_running(aid_s):
        wrote_emu = apply_personal_achievements_to_emu_files(aid_s, personal_map, schema)

    ach_stat_ids = {item["stat_id"] for item in schema}
    if not ach_stat_ids or not steam_path:
        return wrote_emu

    offline_sids, online_sids = _get_offline_and_online_sid3_sets(steam_path)
    sid3_s = str(sid3 or "").strip()

    # SAFETY GUARD: Never overwrite Steam stats .bin for Online (Personal) accounts!
    if sid3_s:
        if sid3_s in online_sids or sid3_s not in offline_sids:
            return wrote_emu
        target_sids = {sid3_s}
    else:
        target_sids = set(offline_sids)

    if not target_sids:
        return wrote_emu

    stats_dir = os.path.join(steam_path, "appcache", "stats")
    os.makedirs(stats_dir, exist_ok=True)
    target_bins = set()
    for s3 in target_sids:
        bp = os.path.join(stats_dir, f"UserGameStats_{s3}_{aid_s}.bin")
        if sid3_s or os.path.exists(bp):
            target_bins.add(bp)
    if not target_bins:
        return wrote_emu

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
        # Reset crc to 0 on offline-isolated bins so if account ever goes Online, Steam re-syncs from server instead of trusting modified cache
        cache_block["crc"] = ("int", 0)
        cache_block["PendingChanges"] = ("int", 0)
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
    return wrote_any or wrote_emu

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
    schema = parse_game_achievement_schema(steam_path, aid_s, allow_remote=True)
    capture_new_achievements_from_steam_bin(steam_path, sid3, aid_s, session_start_ts=0, trigger_popup=False)
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

class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_uint32),
        ("cntUsage", ctypes.c_uint32),
        ("th32ProcessID", ctypes.c_uint32),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", ctypes.c_uint32),
        ("cntThreads", ctypes.c_uint32),
        ("th32ParentProcessID", ctypes.c_uint32),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", ctypes.c_uint32),
        ("szExeFile", ctypes.c_wchar * 260),
    ]

_IGNORED_EXE_PREFIXES = (
    "unins", "unitycrashhandler", "crashpad", "ue4prereq", "ueprereq",
    "vc_redist", "vcredist", "dxsetup", "dotnet", "crashreport"
)

def get_running_process_names():
    """Ultra-fast (~0.2ms) Win32 Toolhelp32 process snapshot with 0.8s cache and zero subprocess overhead."""
    now = time.time()
    if (now - PROC_SNAPSHOT_CACHE["ts"]) < 0.8 and PROC_SNAPSHOT_CACHE["names"]:
        return PROC_SNAPSHOT_CACHE["names"]
    names = set()
    try:
        k32 = ctypes.windll.kernel32
        k32.CreateToolhelp32Snapshot.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
        k32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
        k32.Process32FirstW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_PROCESSENTRY32W)]
        k32.Process32FirstW.restype = ctypes.c_int
        k32.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_PROCESSENTRY32W)]
        k32.Process32NextW.restype = ctypes.c_int
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        k32.CloseHandle.restype = ctypes.c_int

        h_snap = k32.CreateToolhelp32Snapshot(0x00000002, 0)
        invalid_handle = ctypes.c_void_p(-1).value
        if h_snap and h_snap != invalid_handle:
            try:
                pe = _PROCESSENTRY32W()
                pe.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
                if k32.Process32FirstW(h_snap, ctypes.byref(pe)):
                    while True:
                        if pe.szExeFile:
                            names.add(pe.szExeFile.lower())
                        if not k32.Process32NextW(h_snap, ctypes.byref(pe)):
                            break
            finally:
                k32.CloseHandle(h_snap)
    except Exception:
        pass
    PROC_SNAPSHOT_CACHE["ts"] = now
    PROC_SNAPSHOT_CACHE["names"] = names
    return names

def is_local_game_running(appid):
    aid_s = str(appid)
    proc = LOCAL_GAME_PROCS.get(aid_s)
    if proc is not None:
        if proc.poll() is None:
            return True
        LOCAL_GAME_PROCS.pop(aid_s, None)

    lg = load_local_games().get(aid_s)
    if not lg:
        return False

    running_names = get_running_process_names()
    if not running_names:
        return False

    exe_path = lg.get("exe_path", "")
    if exe_path:
        exe_base = os.path.basename(exe_path).lower()
        if exe_base and exe_base in running_names:
            return True

    install_dir = lg.get("install_dir", "")
    if install_dir and os.path.isdir(install_dir):
        try:
            for root, dirs, files in os.walk(install_dir):
                rel_depth = os.path.relpath(root, install_dir).count(os.sep)
                if rel_depth > 2:
                    dirs[:] = []
                    continue
                for fn in files:
                    fn_low = fn.lower()
                    if fn_low.endswith(".exe") and not fn_low.startswith(_IGNORED_EXE_PREFIXES):
                        if fn_low in running_names:
                            return True
        except Exception:
            pass
    return False

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
    if is_local_game_running(appid):
        return True
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

def get_emulator_remote_save_dirs(appid, install_dir=""):
    aid_s = str(appid).strip()
    sys_drive = os.environ.get("SystemDrive", "C:")
    pub_docs = os.path.join(os.environ.get("PUBLIC", os.path.join(sys_drive, "Users", "Public")), "Documents")
    appdata = os.environ.get("APPDATA", os.path.expanduser(r"~\AppData\Roaming"))
    cands = [
        os.path.join(pub_docs, "Steam", "RUNE", aid_s, "remote"),
        os.path.join(sys_drive, "\\Users\\Public\\Documents\\Steam\\RUNE", aid_s, "remote"),
        os.path.join(appdata, "Steam", "RUNE", aid_s, "remote"),
        os.path.join(pub_docs, "Steam", "CODEX", aid_s, "remote"),
        os.path.join(appdata, "Steam", "CODEX", aid_s, "remote"),
        os.path.join(appdata, "Goldberg SteamEmu Saves", aid_s, "remote"),
        os.path.join(appdata, "GSE Saves", aid_s, "remote"),
        os.path.join(appdata, "FLT", aid_s, "remote"),
        os.path.join(pub_docs, "OnlineFix", aid_s, "Saves"),
    ]
    if install_dir and os.path.isdir(install_dir):
        cands.append(os.path.join(install_dir, "SteamData", "remote"))
    out = []
    seen = set()
    for p in cands:
        np = os.path.normpath(p)
        if np.lower() not in seen:
            seen.add(np.lower())
            out.append(np)
    return out

def get_game_save_targets(steam_path, appid, account_info):
    """Return list of {"dir": resolved_folder, "pattern": file_pattern} for a game (Steam or Local Non-Steam)."""
    aid_s = str(appid)
    sid64 = (account_info or {}).get("steamid64", "")
    sid3 = (account_info or {}).get("steamid3", "")
    targets = []

    local_game = load_local_games().get(aid_s)
    if local_game and local_game.get("save_dir"):
        targets.append({"dir": os.path.normpath(local_game["save_dir"]), "pattern": "*"})

    if steam_path and aid_s not in UFS_CACHE:
        parse_appinfo_vdf(steam_path, extract_ufs_appids={aid_s})

    ufs_rules = UFS_CACHE.get(aid_s, [])
    for rule in ufs_rules:
        platforms = rule.get("platforms", {})
        if platforms and "windows" not in [str(v).lower() for v in platforms.values()]:
            continue
        base_dir = resolve_root_base_dir(rule.get("root", ""), steam_path, sid3, aid_s)
        if not base_dir:
            continue
        rel_path_raw = rule.get("path", "").replace("/", os.sep).replace("\\", os.sep).strip(os.sep)
        if ("{64BitSteamID}" in rel_path_raw and not sid64) or ("{Steam3AccountID}" in rel_path_raw and not sid3):
            glob_rel = rel_path_raw.replace("{64BitSteamID}", "*").replace("{Steam3AccountID}", "*")
            for cand in glob.glob(os.path.join(base_dir, glob_rel)):
                if os.path.isdir(cand):
                    targets.append({"dir": os.path.normpath(cand), "pattern": rule.get("pattern", "*")})
            continue
        rel_path = rel_path_raw.replace("{64BitSteamID}", sid64).replace("{Steam3AccountID}", sid3)
        full_dir = os.path.normpath(os.path.join(base_dir, rel_path))
        pattern = rule.get("pattern", "*")
        targets.append({"dir": full_dir, "pattern": pattern})

        # If {64BitSteamID} folder for this account doesn't exist yet, check if saves exist under another SteamID64 folder and migrate/include them
        if "{64BitSteamID}" in rel_path_raw and sid64 and not os.path.isdir(full_dir):
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

    # Check emulator remote save directories (RUNE, CODEX, Goldberg, GSE, Tenoke, FLT)
    emu_install_dir = local_game.get("install_dir", "") if local_game else ""
    for emu_rdir in get_emulator_remote_save_dirs(aid_s, install_dir=emu_install_dir):
        if os.path.isdir(emu_rdir):
            try:
                has_any_file = any(files for _, _, files in os.walk(emu_rdir))
            except Exception:
                has_any_file = False
            if has_any_file or not targets:
                targets.append({"dir": emu_rdir, "pattern": "*"})

    # Also check userdata/<sid3>/<appid>/remote when associated with a Steam account
    if steam_path and sid3:
        remote_dir = os.path.join(steam_path, "userdata", str(sid3), aid_s, "remote")
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

_IGNORED_SAVE_DIRS = {"gpucache", "code cache", "shadercache", "crashdumps", "crashes", "logs", "webcache", "d3dshader_cache"}
_IGNORED_SAVE_EXTS = (".log", ".dmp", ".tmp")

def collect_local_save_files(steam_path, appid, account_info):
    """Find all matching local save files for a game directly in Steam's or Local Game's save folder, returning (files_list, latest_mtime, combined_sha256)."""
    targets = get_game_save_targets(steam_path, appid, account_info)
    matched_files = []  # list of (target_idx, rel_path_in_target, abs_path, mtime)
    latest_mtime = 0

    for idx, t in enumerate(targets):
        base_dir = t["dir"]
        pattern = t["pattern"]
        if not os.path.isdir(base_dir):
            continue
        for root, dirs, files in os.walk(base_dir):
            dirs[:] = [d for d in dirs if d.lower() not in _IGNORED_SAVE_DIRS]
            for fname in files:
                fn_low = fname.lower()
                if fn_low == "steam_autocloud.vdf" or fn_low.endswith(_IGNORED_SAVE_EXTS):
                    continue
                if fnmatch.fnmatch(fn_low, pattern.lower()) or pattern == "*":
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
            if not sid3:
                detach_local_speed_hook(appid, restore_normal=False)
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

def resolve_game_name(steam_path, appid):
    aid_s = str(appid)
    lg = load_local_games().get(aid_s)
    if lg and lg.get("name"):
        return lg["name"]
    appinfo = parse_appinfo_vdf(steam_path)
    if aid_s in appinfo and appinfo[aid_s].get("name"):
        return appinfo[aid_s]["name"]
    return f"App {aid_s}"

def global_game_watcher_loop():
    """Continuous background daemon: auto-syncs Cloud achievements on startup and attaches real-time watcher to any running Steam or Local game."""
    time.sleep(1.5)
    try:
        reg = get_steam_registry_info()
        sp = reg["steam_path"]
        # 1. Sync personal achievements with Cloud on startup
        sync_cloud_achievements_bundle(sp, direction="both")
        # 2. Import any pre-existing unlocked emulator achievements & pre-seed RUNE/CODEX schemas for Local Games
        for l_aid, lg in load_local_games().items():
            l_schema = parse_game_achievement_schema(sp, l_aid, allow_remote=True)
            capture_new_achievements_from_steam_bin(sp, "", l_aid, session_start_ts=0, game_name=lg.get("name", ""), trigger_popup=False)
            if l_schema and not is_local_game_running(l_aid):
                apply_personal_achievements_to_emu_files(l_aid, get_game_personal_achievements(l_aid), l_schema)
        # 3. Import existing Steam achievements from Online (Personal) accounts so they are never lost
        accounts = parse_loginusers(sp)
        installed = get_installed_games(sp)
        for acc in accounts.values():
            if not acc.get("offline_mode") and acc.get("steamid3"):
                for aid in installed:
                    capture_new_achievements_from_steam_bin(sp, acc["steamid3"], aid, session_start_ts=0, trigger_popup=False)
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

            # Also watch any Local Game (Non-Steam) even if user launched its .exe directly from Windows Explorer
            local_games = load_local_games()
            if local_games:
                for l_aid, lg in local_games.items():
                    if l_aid not in ACTIVE_WATCHERS and is_local_game_running(l_aid):
                        reg = get_steam_registry_info()
                        sp = reg["steam_path"]
                        threading.Thread(
                            target=watch_game_and_auto_upload,
                            args=(sp, l_aid, {"account_name": "LOCAL", "steamid3": "", "steamid64": ""}, lg.get("name") or f"App {l_aid}"),
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

LOCAL_DIR_SIZE_CACHE = {}

def _get_cached_dir_size(folder_path):
    if not folder_path or not os.path.isdir(folder_path):
        return 0
    norm = os.path.normpath(folder_path)
    if norm in LOCAL_DIR_SIZE_CACHE:
        return LOCAL_DIR_SIZE_CACHE[norm]
    total = 0
    try:
        for root, _, files in os.walk(norm):
            for fn in files:
                try:
                    total += os.path.getsize(os.path.join(root, fn))
                except Exception:
                    pass
    except Exception:
        pass
    LOCAL_DIR_SIZE_CACHE[norm] = total
    return total

def get_installed_games(steam_path):
    """Scan all library folders for appmanifest_*.acf, plus registered Local Games."""
    installed = {}
    folders = get_library_folders(steam_path) if steam_path else []
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
                    "library_path": folder,
                    "is_local": False
                }
            except Exception:
                pass

    for l_aid, lg in load_local_games().items():
        if l_aid in installed:
            continue
        idir = lg.get("install_dir", "")
        exe_p = lg.get("exe_path", "")
        exists_now = bool((exe_p and os.path.isfile(exe_p)) or (idir and os.path.isdir(idir)))
        if not exists_now:
            continue
        sz = _get_cached_dir_size(idir)
        drive = os.path.splitdrive(idir or exe_p)[0] or "Lokal"
        installed[l_aid] = {
            "appid": l_aid,
            "name": lg.get("name") or f"App {l_aid}",
            "install_dir": idir,
            "exe_path": exe_p,
            "size_bytes": sz,
            "size_gb": round(sz / (1024 ** 3), 2) if sz > 0 else 0,
            "last_played": int(lg.get("last_played") or 0),
            "last_owner_sid64": "",
            "drive": drive,
            "library_path": idir,
            "is_local": True
        }
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

def _get_cached_cover_path(appid):
    return os.path.join(LOCAL_DATA_ROOT, "covers", f"{appid}.jpg")

def find_local_cover(steam_path, appid):
    """Find cached cover image in Steam's appcache/librarycache/<appid> or SteamSwitcher local cache."""
    aid_s = str(appid)
    if steam_path:
        libcache = os.path.join(steam_path, "appcache", "librarycache", aid_s)
        if os.path.isdir(libcache):
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
    custom_cov = _get_cached_cover_path(aid_s)
    if os.path.isfile(custom_cov):
        return custom_cov
    return None

def _resolve_hashed_steam_cover_urls(appid):
    """Query Steam IStoreBrowseService/GetItems/v1 for hashed cover URLs used by newer (2024+) Steam games."""
    aid_s = str(appid).strip()
    if not aid_s.isdigit():
        return []
    urls = []
    try:
        inp = json.dumps({
            "ids": [{"appid": int(aid_s)}],
            "context": {"language": "english", "country_code": "US"},
            "data_request": {"include_assets": True}
        }, separators=(",", ":"))
        api_url = f"https://api.steampowered.com/IStoreBrowseService/GetItems/v1/?input_json={urllib.parse.quote(inp)}"
        req = urllib.request.Request(api_url, headers={"User-Agent": "Mozilla/5.0 SteamSwitcher/2.0"})
        with urllib.request.urlopen(req, timeout=7) as resp:
            obj = json.loads(resp.read().decode("utf-8", errors="ignore"))
        items = (obj.get("response") or {}).get("store_items") or []
        if items and isinstance(items[0], dict):
            assets = items[0].get("assets") or {}
            fmt = str(assets.get("asset_url_format") or f"steam/apps/{aid_s}/${{FILENAME}}")
            for key in ("library_capsule_2x", "library_capsule", "header_2x", "header", "main_capsule"):
                rel = assets.get(key)
                if rel:
                    rel_path = fmt.replace("${FILENAME}", str(rel)).lstrip("/")
                    urls.append(f"https://shared.akamai.steamstatic.com/store_item_assets/{rel_path}")
    except Exception:
        pass
    if not urls:
        try:
            ad_url = f"https://store.steampowered.com/api/appdetails?appids={aid_s}&l=english"
            req2 = urllib.request.Request(ad_url, headers={"User-Agent": "Mozilla/5.0 SteamSwitcher/2.0"})
            with urllib.request.urlopen(req2, timeout=7) as resp2:
                obj2 = json.loads(resp2.read().decode("utf-8", errors="ignore"))
            data_blk = (obj2.get(aid_s) or {}).get("data") or {}
            for k in ("header_image", "capsule_imagev5", "capsule_image"):
                if data_blk.get(k):
                    urls.append(str(data_blk[k]))
        except Exception:
            pass
    return urls

def download_and_cache_steam_cover(appid):
    """Download official vertical Steam cover (supporting both legacy flat URLs and new SHA1-hashed URLs) and cache locally."""
    aid_s = str(appid).strip()
    if not aid_s.isdigit():
        return b""
    cov_dst = _get_cached_cover_path(aid_s)
    os.makedirs(os.path.dirname(cov_dst), exist_ok=True)

    candidate_urls = [
        f"https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/{aid_s}/library_600x900.jpg",
    ]
    for idx, url in enumerate(candidate_urls):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 SteamSwitcher/2.0"})
            with urllib.request.urlopen(req, timeout=7) as r:
                raw = r.read()
                if raw and len(raw) > 1024:
                    with open(cov_dst, "wb") as cf:
                        cf.write(raw)
                    COVER_CACHE[aid_s] = raw
                    COVER_B64_CACHE[aid_s] = "data:image/jpeg;base64," + base64.b64encode(raw).decode("ascii")
                    return raw
        except Exception:
            pass

    # Newer Steam games store assets under hashed paths; query IStoreBrowseService/GetItems
    for url in _resolve_hashed_steam_cover_urls(aid_s):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 SteamSwitcher/2.0"})
            with urllib.request.urlopen(req, timeout=8) as r:
                raw = r.read()
                if raw and len(raw) > 1024:
                    with open(cov_dst, "wb") as cf:
                        cf.write(raw)
                    COVER_CACHE[aid_s] = raw
                    COVER_B64_CACHE[aid_s] = "data:image/jpeg;base64," + base64.b64encode(raw).decode("ascii")
                    return raw
        except Exception:
            pass

    # Legacy header fallback
    try:
        h_url = f"https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/{aid_s}/header.jpg"
        req = urllib.request.Request(h_url, headers={"User-Agent": "Mozilla/5.0 SteamSwitcher/2.0"})
        with urllib.request.urlopen(req, timeout=7) as r:
            raw = r.read()
            if raw and len(raw) > 1024:
                with open(cov_dst, "wb") as cf:
                    cf.write(raw)
                COVER_CACHE[aid_s] = raw
                COVER_B64_CACHE[aid_s] = "data:image/jpeg;base64," + base64.b64encode(raw).decode("ascii")
                return raw
    except Exception:
        pass
    return b""

def ensure_local_game_assets_async(steam_path, appid):
    """Background fetch & cache of official Steam cover art and achievement schema for Local Games."""
    aid_s = str(appid).strip()
    if not aid_s.isdigit():
        return

    need_schema = (aid_s not in SCHEMA_CACHE) and (not os.path.exists(_get_cached_schema_json_path(aid_s)))
    need_cover = (aid_s not in COVER_CACHE) and (find_local_cover(steam_path, aid_s) is None)
    if not need_schema and not need_cover:
        return
    if aid_s in SCHEMA_FETCHING:
        return
    SCHEMA_FETCHING.add(aid_s)

    def _worker():
        changed = False
        try:
            if need_schema:
                achs = fetch_remote_achievement_schema(aid_s)
                if achs is not None:
                    changed = True
            if need_cover:
                raw_cov = download_and_cache_steam_cover(aid_s)
                if raw_cov:
                    changed = True
            if changed:
                build_full_state()
        finally:
            SCHEMA_FETCHING.discard(aid_s)

    threading.Thread(target=_worker, daemon=True).start()

class _OPENFILENAMEW(ctypes.Structure):
    _fields_ = [
        ("lStructSize", ctypes.c_uint32),
        ("hwndOwner", ctypes.c_void_p),
        ("hInstance", ctypes.c_void_p),
        ("lpstrFilter", ctypes.c_void_p),
        ("lpstrCustomFilter", ctypes.c_void_p),
        ("nMaxCustFilter", ctypes.c_uint32),
        ("nFilterIndex", ctypes.c_uint32),
        ("lpstrFile", ctypes.c_void_p),
        ("nMaxFile", ctypes.c_uint32),
        ("lpstrFileTitle", ctypes.c_void_p),
        ("nMaxFileTitle", ctypes.c_uint32),
        ("lpstrInitialDir", ctypes.c_wchar_p),
        ("lpstrTitle", ctypes.c_wchar_p),
        ("Flags", ctypes.c_uint32),
        ("nFileOffset", ctypes.c_uint16),
        ("nFileExtension", ctypes.c_uint16),
        ("lpstrDefExt", ctypes.c_wchar_p),
        ("lCustData", ctypes.c_void_p),
        ("lpfnHook", ctypes.c_void_p),
        ("lpTemplateName", ctypes.c_wchar_p),
        ("pvReserved", ctypes.c_void_p),
        ("dwReserved", ctypes.c_uint32),
        ("FlagsEx", ctypes.c_uint32),
    ]

class _BROWSEINFOW(ctypes.Structure):
    _fields_ = [
        ("hwndOwner", ctypes.c_void_p),
        ("pidlRoot", ctypes.c_void_p),
        ("pszDisplayName", ctypes.c_void_p),
        ("lpszTitle", ctypes.c_wchar_p),
        ("ulFlags", ctypes.c_uint32),
        ("lpfn", ctypes.c_void_p),
        ("lParam", ctypes.c_void_p),
        ("iImage", ctypes.c_int),
    ]

def pick_exe_file_dialog(initial_dir=""):
    """Open native Win32 OpenFileDialog for selecting a game .exe file."""
    try:
        u32 = ctypes.windll.user32
        u32.GetForegroundWindow.restype = ctypes.c_void_p
        comdlg = ctypes.windll.comdlg32
        comdlg.GetOpenFileNameW.argtypes = [ctypes.POINTER(_OPENFILENAMEW)]
        comdlg.GetOpenFileNameW.restype = ctypes.c_int

        file_buf = ctypes.create_unicode_buffer(1024)
        flt_str = "Game Executable (*.exe)\0*.exe\0Semua File (*.*)\0*.*\0\0"
        flt_buf = ctypes.create_unicode_buffer(flt_str, len(flt_str))

        ofn = _OPENFILENAMEW()
        ofn.lStructSize = ctypes.sizeof(_OPENFILENAMEW)
        ofn.hwndOwner = u32.GetForegroundWindow()
        ofn.lpstrFilter = ctypes.addressof(flt_buf)
        ofn.nFilterIndex = 1
        ofn.lpstrFile = ctypes.addressof(file_buf)
        ofn.nMaxFile = 1024
        if initial_dir and os.path.isdir(initial_dir):
            ofn.lpstrInitialDir = os.path.normpath(initial_dir)
        ofn.lpstrTitle = "Pilih File .exe Game Lokal"
        # OFN_EXPLORER (0x80000) | OFN_FILEMUSTEXIST (0x1000) | OFN_PATHMUSTEXIST (0x800) | OFN_NOCHANGEDIR (0x8)
        ofn.Flags = 0x00081808
        ofn.lpstrDefExt = "exe"
        if comdlg.GetOpenFileNameW(ctypes.byref(ofn)):
            return os.path.normpath(file_buf.value)
    except Exception:
        pass
    return ""

def pick_folder_dialog(title="Pilih Folder"):
    """Open native Win32 BrowseForFolder dialog."""
    try:
        ole32 = ctypes.windll.ole32
        ole32.CoInitialize(None)
        u32 = ctypes.windll.user32
        u32.GetForegroundWindow.restype = ctypes.c_void_p
        sh32 = ctypes.windll.shell32
        sh32.SHBrowseForFolderW.argtypes = [ctypes.POINTER(_BROWSEINFOW)]
        sh32.SHBrowseForFolderW.restype = ctypes.c_void_p
        sh32.SHGetPathFromIDListW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        sh32.SHGetPathFromIDListW.restype = ctypes.c_int

        disp_buf = ctypes.create_unicode_buffer(260)
        path_buf = ctypes.create_unicode_buffer(1024)
        bi = _BROWSEINFOW()
        bi.hwndOwner = u32.GetForegroundWindow()
        bi.pszDisplayName = ctypes.addressof(disp_buf)
        bi.lpszTitle = title
        # BIF_RETURNONLYFSDIRS (0x1) | BIF_NEWDIALOGSTYLE (0x40) | BIF_EDITBOX (0x10)
        bi.ulFlags = 0x00000051
        pidl = sh32.SHBrowseForFolderW(ctypes.byref(bi))
        if pidl:
            try:
                if sh32.SHGetPathFromIDListW(pidl, ctypes.addressof(path_buf)):
                    return os.path.normpath(path_buf.value)
            finally:
                ole32.CoTaskMemFree(ctypes.c_void_p(pidl))
    except Exception:
        pass
    return ""

def _extract_appid_from_input(text):
    s = str(text or "").strip()
    if not s:
        return ""
    if s.isdigit():
        return s
    m = re.search(r"store\.steampowered\.com/app/(\d+)", s, re.IGNORECASE)
    if not m:
        m = re.search(r"steamcommunity\.com/(?:app|stats)/(\d+)", s, re.IGNORECASE)
    if not m:
        m = re.search(r"\b(\d{4,9})\b", s)
    return m.group(1) if m else ""

def _find_best_exe_in_folder(folder_path):
    if not folder_path or not os.path.isdir(folder_path):
        return ""
    folder_norm = os.path.normpath(folder_path)
    folder_stem = re.sub(r"[^a-z0-9]", "", os.path.basename(folder_norm).lower())
    candidates = []
    for root, dirs, files in os.walk(folder_norm):
        rel = os.path.relpath(root, folder_norm)
        depth = 0 if rel == "." else (rel.count(os.sep) + 1)
        if depth > 3:
            dirs[:] = []
            continue
        for fn in files:
            fn_low = fn.lower()
            if not fn_low.endswith(".exe") or fn_low.startswith(_IGNORED_EXE_PREFIXES):
                continue
            fp = os.path.join(root, fn)
            try:
                sz = os.path.getsize(fp)
            except Exception:
                sz = 0
            stem_clean = re.sub(r"[^a-z0-9]", "", os.path.splitext(fn_low)[0])
            name_bonus = 1 if (folder_stem and (stem_clean == folder_stem or stem_clean in folder_stem or folder_stem in stem_clean)) else 0
            shipping_bonus = 1 if "win64-shipping" in fn_low else 0
            # Prefer matching name / Unreal Win64-Shipping / shallower depth / larger size
            score = (shipping_bonus * 4) + (name_bonus * 3) - (depth * 0.5) + min(sz / (50 * 1024 * 1024), 2.5)
            candidates.append((score, sz, fp))
    if not candidates:
        return ""
    candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return candidates[0][2]

def _resolve_game_install_root_from_exe(exe_path):
    exe_dir = os.path.dirname(os.path.normpath(exe_path))
    parts = [p.lower() for p in exe_dir.split(os.sep)]
    # Handle Unreal Engine <GameRoot>/<Project>/Binaries/Win64/<Game>-Win64-Shipping.exe
    if len(parts) >= 3 and parts[-1] in ("win64", "win32", "wingdk") and parts[-2] == "binaries":
        cand_parent = os.path.dirname(os.path.dirname(exe_dir))
        cand_root = os.path.dirname(cand_parent)
        if os.path.isfile(os.path.join(cand_root, os.path.basename(cand_parent) + ".exe")):
            return cand_root
        return cand_parent
    return exe_dir

def _detect_appid_and_emulator_in_dir(install_dir, exe_path=""):
    """Scan game directory for Steam Emulator config files (RUNE, CODEX, Goldberg, Tenoke, FLT, OnlineFix, etc.) to extract AppId."""
    search_roots = []
    if exe_path:
        search_roots.append(os.path.dirname(os.path.normpath(exe_path)))
    if install_dir:
        norm_i = os.path.normpath(install_dir)
        if norm_i not in search_roots:
            search_roots.append(norm_i)
        parent_i = os.path.dirname(norm_i)
        if parent_i and parent_i != norm_i and os.path.basename(norm_i).lower() in ("bin", "x64", "win64", "game"):
            search_roots.append(parent_i)

    appid_found = ""
    emu_found = ""

    for sroot in search_roots:
        if not os.path.isdir(sroot):
            continue
        for root, dirs, files in os.walk(sroot):
            rel = os.path.relpath(root, sroot)
            depth = 0 if rel == "." else (rel.count(os.sep) + 1)
            if depth > 3:
                dirs[:] = []
                continue
            files_low = {f.lower(): f for f in files}

            if "steam_api64.rne" in files_low or "steam_api.rne" in files_low:
                emu_found = emu_found or "RUNE"
            elif "steam_api64.cdx" in files_low or "steam_api.cdx" in files_low:
                emu_found = emu_found or "CODEX"

            if "steam_emu.ini" in files_low:
                fp = os.path.join(root, files_low["steam_emu.ini"])
                try:
                    txt = open(fp, "r", encoding="utf-8", errors="ignore").read()
                    if "RUNE" in txt and not emu_found:
                        emu_found = "RUNE"
                    elif not emu_found:
                        emu_found = "CODEX"
                    m = re.search(r"^\s*AppId\s*=\s*(\d+)", txt, re.IGNORECASE | re.MULTILINE)
                    if m and int(m.group(1)) > 0:
                        return m.group(1), (emu_found or "RUNE")
                except Exception:
                    pass

            if "steam_appid.txt" in files_low:
                fp = os.path.join(root, files_low["steam_appid.txt"])
                try:
                    txt = open(fp, "r", encoding="utf-8", errors="ignore").read().strip()
                    m = re.search(r"(\d+)", txt)
                    if m and int(m.group(1)) > 0:
                        is_gb = ("steam_settings" in root.lower()) or os.path.isdir(os.path.join(root, "steam_settings"))
                        return m.group(1), (emu_found or ("Goldberg" if is_gb else "SteamEmu"))
                except Exception:
                    pass

            for ini_name, label, rx in (
                ("tenoke.ini", "Tenoke", r"^\s*id\s*=\s*(\d+)"),
                ("flt.ini", "FLT", r"^\s*appid\s*=\s*(\d+)"),
                ("onlinefix.ini", "OnlineFix", r"^\s*(?:Real)?AppId\s*=\s*(\d+)"),
                ("coldclientloader.ini", "Goldberg", r"^\s*AppId\s*=\s*(\d+)"),
                ("ali213.ini", "ALI213", r"^\s*AppID\s*=\s*(\d+)"),
                ("valve.ini", "ALI213", r"^\s*AppID\s*=\s*(\d+)"),
                ("skidrow.ini", "SKIDROW", r"^\s*AppId\s*=\s*(\d+)"),
                ("steam_api.ini", "SKIDROW", r"^\s*AppId\s*=\s*(\d+)"),
                ("smartsteamemu.ini", "SmartSteamEmu", r"^\s*AppId\s*=\s*(\d+)"),
            ):
                if ini_name in files_low:
                    fp = os.path.join(root, files_low[ini_name])
                    try:
                        txt = open(fp, "r", encoding="utf-8", errors="ignore").read()
                        m = re.search(rx, txt, re.IGNORECASE | re.MULTILINE)
                        if m and int(m.group(1)) > 0:
                            return m.group(1), label
                    except Exception:
                        pass

    return appid_found, emu_found

def _fetch_steam_store_game_name(appid):
    aid_s = str(appid).strip()
    if not aid_s.isdigit():
        return ""
    try:
        url = f"https://store.steampowered.com/api/appdetails?appids={aid_s}&l=english"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 SteamSwitcher/2.0"})
        with urllib.request.urlopen(req, timeout=6) as resp:
            obj = json.loads(resp.read().decode("utf-8", errors="ignore"))
        data_blk = (obj.get(aid_s) or {}).get("data") or {}
        if isinstance(data_blk, dict) and data_blk.get("name"):
            return str(data_blk["name"]).strip()
    except Exception:
        pass
    return ""

def _search_steam_store_by_name(query_name):
    q = re.sub(r"[-_.]*(steamrip|repack|fitgirl|dodi|elamigos|rune|codex|tenoke|goldberg|v\d[\d.]*|com)\b.*", "", str(query_name or ""), flags=re.IGNORECASE)
    q = re.sub(r"[_\-.]+", " ", q).strip()
    if not q:
        return "", ""
    try:
        url = f"https://store.steampowered.com/api/storesearch/?term={urllib.parse.quote(q)}&l=english&cc=US"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 SteamSwitcher/2.0"})
        with urllib.request.urlopen(req, timeout=6) as resp:
            obj = json.loads(resp.read().decode("utf-8", errors="ignore"))
        items = obj.get("items") or []
        if items and isinstance(items[0], dict):
            return str(items[0].get("id") or ""), str(items[0].get("name") or "")
    except Exception:
        pass
    return "", ""

def auto_detect_local_save_dir(steam_path, appid, game_name, exe_path="", install_dir=""):
    """Smart scan of UFS rules, AppData (Local/LocalLow/Roaming), Saved Games, Documents, and Emulator remote folders."""
    aid_s = str(appid).strip()
    if steam_path and aid_s:
        if aid_s not in UFS_CACHE:
            parse_appinfo_vdf(steam_path, extract_ufs_appids={aid_s})
        for rule in UFS_CACHE.get(aid_s, []):
            base_dir = resolve_root_base_dir(rule.get("root", ""), steam_path, "", aid_s)
            if base_dir:
                rel = rule.get("path", "").replace("/", os.sep).replace("\\", os.sep).strip(os.sep)
                if "{" not in rel:
                    cand = os.path.normpath(os.path.join(base_dir, rel))
                    if os.path.isdir(cand):
                        return cand

    norm_keys = set()
    for raw_n in (game_name, os.path.splitext(os.path.basename(exe_path or ""))[0], os.path.basename(install_dir or "")):
        cleaned = re.sub(r"[^a-z0-9]", "", str(raw_n or "").lower())
        if len(cleaned) >= 3:
            norm_keys.add(cleaned)

    user_prof = os.environ.get("USERPROFILE", os.path.expanduser("~"))
    save_roots = [
        os.environ.get("LOCALAPPDATA", os.path.join(user_prof, "AppData", "Local")),
        os.path.join(user_prof, "AppData", "LocalLow"),
        os.environ.get("APPDATA", os.path.join(user_prof, "AppData", "Roaming")),
        os.path.join(user_prof, "Saved Games"),
        os.path.join(user_prof, "Documents", "My Games"),
        os.path.join(user_prof, "Documents"),
    ]

    # 1. Exact folder name match in top-level save roots (e.g. %LOCALAPPDATA%\StoneShard)
    for sroot in save_roots:
        if not os.path.isdir(sroot):
            continue
        try:
            for entry in os.listdir(sroot):
                full_p = os.path.join(sroot, entry)
                if not os.path.isdir(full_p):
                    continue
                e_norm = re.sub(r"[^a-z0-9]", "", entry.lower())
                if e_norm in norm_keys:
                    return os.path.normpath(full_p)
        except Exception:
            pass

    # 2. One-level-deep Publisher\<GameName> in LocalLow / Roaming / Local
    for sroot in save_roots[:4]:
        if not os.path.isdir(sroot):
            continue
        try:
            for pub in os.listdir(sroot):
                pub_p = os.path.join(sroot, pub)
                if not os.path.isdir(pub_p) or pub.lower() in ("microsoft", "packages", "temp", "google", "mozilla", "nvidia", "amd", "steam"):
                    continue
                for sub in os.listdir(pub_p):
                    sub_p = os.path.join(pub_p, sub)
                    if os.path.isdir(sub_p) and re.sub(r"[^a-z0-9]", "", sub.lower()) in norm_keys:
                        return os.path.normpath(sub_p)
        except Exception:
            pass

    # 3. Emulator remote folder if it exists
    if aid_s:
        for emu_r in get_emulator_remote_save_dirs(aid_s, install_dir=install_dir):
            if os.path.isdir(emu_r):
                return emu_r

    return ""

def detect_local_game_metadata(steam_path, path_input="", appid_or_url=""):
    """Inspect a local .exe or folder + optional Steam AppID/URL and return auto-detected metadata."""
    raw_p = os.path.normpath(path_input.strip().strip('"')) if path_input and path_input.strip() else ""
    exe_path = ""
    install_dir = ""

    if raw_p:
        if os.path.isfile(raw_p):
            exe_path = raw_p
            install_dir = _resolve_game_install_root_from_exe(exe_path)
        elif os.path.isdir(raw_p):
            install_dir = raw_p
            exe_path = _find_best_exe_in_folder(install_dir)
            if exe_path:
                install_dir = _resolve_game_install_root_from_exe(exe_path) if install_dir != raw_p else raw_p

    manual_appid = _extract_appid_from_input(appid_or_url)
    det_appid, emu_type = _detect_appid_and_emulator_in_dir(install_dir, exe_path)
    appid = manual_appid or det_appid

    fallback_title = ""
    if install_dir:
        fallback_title = os.path.basename(install_dir)
    elif exe_path:
        fallback_title = os.path.splitext(os.path.basename(exe_path))[0]

    game_name = ""
    if not appid and fallback_title:
        s_aid, s_name = _search_steam_store_by_name(fallback_title)
        if s_aid:
            appid = s_aid
            game_name = s_name

    if appid and not game_name:
        appinfo = parse_appinfo_vdf(steam_path) if steam_path else {}
        if appid in appinfo and appinfo[appid].get("name"):
            game_name = appinfo[appid]["name"]
        else:
            game_name = _fetch_steam_store_game_name(appid)

    if not game_name:
        game_name = fallback_title or (f"App {appid}" if appid else "")

    save_dir = auto_detect_local_save_dir(steam_path, appid, game_name, exe_path=exe_path, install_dir=install_dir)
    ach_count = 0
    if appid and appid.isdigit():
        schema = parse_game_achievement_schema(steam_path, appid, allow_remote=True)
        ach_count = len(schema)
        ensure_local_game_assets_async(steam_path, appid)

    return {
        "appid": appid,
        "name": game_name,
        "exe_path": exe_path,
        "install_dir": install_dir,
        "save_dir": save_dir,
        "emu_type": emu_type or (" Emulator Lokal" if det_appid else "Lokal"),
        "ach_total": ach_count,
    }

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
    local_games = load_local_games()
    account_games, last_played_per_acc = get_userdata_account_games(steam_path, accounts)

    acc_sizes = {acc_name: len(gset) for acc_name, gset in account_games.items()}

    all_appids = set(local_games.keys())
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
        inst_info = installed.get(aid)
        lg_entry = local_games.get(aid)
        is_local_only = bool(lg_entry and not (inst_info and not inst_info.get("is_local")))

        info_meta = appinfo.get(aid, {})
        if not is_local_only:
            gtype = info_meta.get("type", "game")
            if gtype not in ("game", ""):
                continue

        cov_path = find_local_cover(steam_path, aid)
        has_local_cover = bool(cov_path and os.path.exists(cov_path))

        if is_local_only:
            name = lg_entry.get("name") or (inst_info["name"] if inst_info else None) or info_meta.get("name") or f"App {aid}"
            owner_account = "LOCAL"
            owner_persona = "Game Lokal"
            associated_accounts = ["LOCAL"]
            lp_ts = int(lg_entry.get("last_played") or 0)
            acc_info = {"account_name": "LOCAL", "persona_name": "Game Lokal", "steamid3": "", "steamid64": ""}
            ensure_local_game_assets_async(steam_path, aid)
        else:
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
            acc_info = accounts.get(owner_account, {})
            owner_persona = acc_info.get("persona_name", owner_account)

        if aid not in COVER_CACHE and cov_path and os.path.exists(cov_path):
            try:
                with open(cov_path, "rb") as cf:
                    raw_bytes = cf.read()
                    COVER_CACHE[aid] = raw_bytes
                    COVER_B64_CACHE[aid] = "data:image/jpeg;base64," + base64.b64encode(raw_bytes).decode("ascii")
            except Exception:
                pass

        save_files, save_mtime, _ = collect_local_save_files(steam_path, aid, acc_info)
        ach_schema = parse_game_achievement_schema(steam_path, aid)
        pers_ach = get_game_personal_achievements(aid)
        valid_ach_keys = {x["key"] for x in ach_schema}
        ach_unlocked_cnt = sum(1 for k, v in pers_ach.items() if v and (not valid_ach_keys or k in valid_ach_keys))
        patch_info = get_game_patch_info(aid, name, inst_info, cloud_patches)

        is_run_now = str(aid) in running_ids
        speed_info = (
            get_local_game_speed_state(aid, is_running=is_run_now)
            if is_local_only else {
                "speed_enabled": False,
                "speed_target": 2.0,
                "speed_current": 1.0,
                "speed_active": False,
                "speed_kb_bind": None,
                "speed_pad_bind": None,
                "speed_engine": ""
            }
        )

        games_list.append({
            "appid": aid,
            "name": name,
            "is_local": is_local_only,
            "exe_path": lg_entry.get("exe_path", "") if lg_entry else "",
            "install_dir": (lg_entry.get("install_dir") if is_local_only else (inst_info["install_dir"] if inst_info else "")) or "",
            "save_dir": lg_entry.get("save_dir", "") if lg_entry else "",
            "emu_type": lg_entry.get("emu_type", "") if lg_entry else "",
            "installed": inst_info is not None,
            "running": is_run_now,
            "uploading": str(aid) in uploading_ids,
            "cloud_enabled": str(aid) in enabled_cloud_ids,
            "size_gb": inst_info["size_gb"] if inst_info else 0,
            "drive": inst_info["drive"] if inst_info else "",
            "last_played": lp_ts,
            "owner_account": owner_account,
            "owner_persona": owner_persona,
            "accounts": associated_accounts,
            "has_local_cover": has_local_cover,
            "cover_src": COVER_B64_CACHE.get(aid, f"/api/cover/{aid}"),
            "has_local_save": len(save_files) > 0,
            "local_save_mtime": save_mtime,
            "ach_unlocked": ach_unlocked_cnt,
            "ach_total": len(ach_schema),
            **speed_info,
            **patch_info
        })

    games_list.sort(key=lambda g: (g["name"].lower(), g["appid"]))

    prot_list = load_protected_accounts(steam_path)
    prot_set = set(prot_list)
    accounts_list = []
    for acc_name, acc in accounts.items():
        acc_games = [g for g in games_list if g["owner_account"] == acc_name]
        inst_count = sum(1 for g in acc_games if g["installed"])
        is_prot = (
            acc_name.strip().lower() in prot_set
            or str(acc.get("persona_name") or "").strip().lower() in prot_set
            or str(acc.get("steamid64") or "").strip().lower() in prot_set
        )
        accounts_list.append({
            **acc,
            "is_protected": is_prot,
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
        "protected_accounts": prot_list,
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

def launch_local_game(appid, game_name="", conflict_choice=""):
    """Launch a registered Local (Non-Steam) game directly, with Cloud Save pre-sync and real-time Emulator Achievement tracking."""
    aid_s = str(appid).strip()
    local_games = load_local_games()
    lg = local_games.get(aid_s)
    if not lg:
        return False, "Data game lokal tidak ditemukan!"

    exe_path = lg.get("exe_path", "")
    install_dir = lg.get("install_dir", "")
    if (not exe_path or not os.path.isfile(exe_path)) and install_dir and os.path.isdir(install_dir):
        exe_path = _find_best_exe_in_folder(install_dir)
        if exe_path:
            lg["exe_path"] = exe_path
            local_games[aid_s] = lg
            save_local_games(local_games)

    if not exe_path or not os.path.isfile(exe_path):
        return False, "File .exe game lokal tidak ditemukan! Silakan periksa pengaturannya."

    reg = get_steam_registry_info()
    steam_path = reg["steam_path"]
    acc_info = {"account_name": "LOCAL", "persona_name": "Game Lokal", "steamid3": "", "steamid64": ""}
    gtitle = game_name or lg.get("name") or f"App {aid_s}"

    # 1. Pre-Launch Cloud Save Smart Sync (if enabled)
    sync_note = ""
    cloud_cfg = load_cloud_config()
    if is_game_cloud_enabled(aid_s) and cloud_cfg.get("server_url") and cloud_cfg.get("username") and cloud_cfg.get("auto_sync", True):
        if conflict_choice == "local":
            ok_s, action_s, msg_s = sync_game_save(steam_path, aid_s, acc_info, gtitle, direction="upload")
        elif conflict_choice == "cloud":
            ok_s, action_s, msg_s = sync_game_save(steam_path, aid_s, acc_info, gtitle, direction="download")
        else:
            ok_s, action_s, msg_s = sync_game_save(steam_path, aid_s, acc_info, gtitle, direction="auto", check_conflict=True)
            if action_s == "conflict" and isinstance(msg_s, dict):
                msg_s["account"] = "LOCAL"
                return True, {"conflict": True, **msg_s}
        if action_s in ("downloaded", "uploaded") and isinstance(msg_s, str):
            sync_note = f" • {msg_s}"

    # 2. Ensure schema is cached & sync personal achievements into emulator ini/json before launching
    parse_game_achievement_schema(steam_path, aid_s, allow_remote=True)
    capture_new_achievements_from_steam_bin(steam_path, "", aid_s, session_start_ts=0, game_name=gtitle, trigger_popup=False)
    apply_personal_achievements_to_steam_bin(steam_path, "", aid_s)

    # 3. Launch .exe directly without restarting/switching Steam (zero game folder files touched)
    try:
        work_dir = os.path.dirname(exe_path) or install_dir or None
        proc = subprocess.Popen(
            [exe_path],
            cwd=work_dir,
            close_fds=True,
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        )
        LOCAL_GAME_PROCS[aid_s] = proc
        LAUNCHING_UNTIL[aid_s] = time.time() + 12
        lg["last_played"] = int(time.time())
        local_games[aid_s] = lg
        save_local_games(local_games)

        threading.Thread(
            target=watch_game_and_auto_upload,
            args=(steam_path, aid_s, acc_info, gtitle),
            daemon=True
        ).start()
        return True, f"Menjalankan {gtitle} (Game Lokal)!{sync_note}"
    except Exception as e:
        return False, f"Gagal menjalankan file game lokal: {e}"

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

    # 3. For unprotected Offline/Sharing accounts, isolate local Steam achievement bin; for Protected or Online accounts, import safely without overwriting!
    if appid and not install_mode:
        if not is_account_protected_from_overwrite(acc_info):
            apply_personal_achievements_to_steam_bin(steam_path, acc_info.get("steamid3", ""), appid)
        else:
            capture_new_achievements_from_steam_bin(steam_path, acc_info.get("steamid3", ""), appid, session_start_ts=0, game_name=game_name, trigger_popup=False)

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
                "active_speeds": get_active_local_speeds_summary(),
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
                        COVER_B64_CACHE[appid] = "data:image/jpeg;base64," + base64.b64encode(raw).decode("ascii")
                    except Exception:
                        pass
            if not raw:
                raw = download_and_cache_steam_cover(appid)
            if raw:
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Cache-Control", "public, max-age=31536000, immutable")
                self.end_headers()
                self.wfile.write(raw)
                return
            svg_placeholder = (
                '<svg xmlns="http://www.w3.org/2000/svg" width="300" height="450" viewBox="0 0 300 450">'
                '<rect width="300" height="450" fill="#1e1e24"/>'
                '<text x="50%" y="50%" dominant-baseline="middle" text-anchor="middle" font-size="48">🎮</text>'
                '</svg>'
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml; charset=utf-8")
            self.send_header("Content-Length", str(len(svg_placeholder)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(svg_placeholder)
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

            local_games = load_local_games()
            if appid and (account == "LOCAL" or appid in local_games):
                reg_chk = get_steam_registry_info()
                inst_chk = get_installed_games(reg_chk["steam_path"]).get(appid)
                if account == "LOCAL" or not inst_chk or inst_chk.get("is_local"):
                    ok, msg = launch_local_game(appid, game_name=game_name, conflict_choice=conflict_choice)
                    if isinstance(msg, dict) and msg.get("conflict"):
                        self.send_json({"ok": True, **msg})
                        return
                    self.send_json({"ok": ok, "message": msg})
                    return

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
            schema = parse_game_achievement_schema(steam_path, appid, allow_remote=True)
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

        if self.path == "/api/pick_local_file":
            init_dir = str(payload.get("initial_dir") or "").strip()
            picked = pick_exe_file_dialog(init_dir)
            self.send_json({"ok": True, "path": picked})
            return

        if self.path == "/api/pick_local_folder":
            title = str(payload.get("title") or "Pilih Folder").strip()
            picked = pick_folder_dialog(title)
            self.send_json({"ok": True, "path": picked})
            return

        if self.path == "/api/detect_local_game":
            path_in = str(payload.get("path") or "").strip()
            appid_or_url = str(payload.get("appid_or_url") or "").strip()
            reg = get_steam_registry_info()
            meta = detect_local_game_metadata(reg["steam_path"], path_in, appid_or_url)
            self.send_json({"ok": True, **meta})
            return

        if self.path == "/api/save_local_game":
            raw_path = str(payload.get("exe_path") or "").strip().strip('"')
            appid_in = str(payload.get("appid_or_url") or payload.get("appid") or "").strip()
            custom_name = str(payload.get("name") or "").strip()
            custom_save_dir = str(payload.get("save_dir") or "").strip().strip('"')
            old_appid = str(payload.get("old_appid") or "").strip()

            if not raw_path:
                self.send_json({"ok": False, "message": "Lokasi file .exe atau folder game wajib diisi!"}, 400)
                return

            reg = get_steam_registry_info()
            steam_path = reg["steam_path"]
            meta = detect_local_game_metadata(steam_path, raw_path, appid_in)

            exe_path = meta["exe_path"]
            install_dir = meta["install_dir"]
            if not exe_path or not os.path.isfile(exe_path):
                self.send_json({"ok": False, "message": "File .exe game tidak ditemukan pada lokasi tersebut!"}, 400)
                return

            appid = _extract_appid_from_input(appid_in) or meta["appid"]
            if not appid or not appid.isdigit():
                self.send_json({
                    "ok": False,
                    "message": "Steam AppID belum terdeteksi otomatis. Silakan tempel Link Steam Store atau masukkan angka AppID game tersebut."
                }, 400)
                return

            final_name = custom_name or meta["name"] or f"App {appid}"
            final_save_dir = os.path.normpath(custom_save_dir) if custom_save_dir else meta["save_dir"]

            local_games = load_local_games()
            prev_entry = local_games.get(appid, {})
            if old_appid and old_appid != appid and old_appid in local_games:
                local_games.pop(old_appid, None)

            want_speed = bool(payload["speed_enabled"]) if "speed_enabled" in payload else bool(prev_entry.get("speed_enabled", False))
            local_games[appid] = {
                "appid": appid,
                "name": final_name,
                "exe_path": os.path.normpath(exe_path),
                "install_dir": os.path.normpath(install_dir),
                "save_dir": final_save_dir,
                "emu_type": meta["emu_type"],
                "speed_enabled": want_speed,
                "speed_target": _sanitize_speed_target(prev_entry.get("speed_target", 2.0), 2.0),
                "speed_kb_bind": _sanitize_kb_bind(prev_entry.get("speed_kb_bind")),
                "speed_pad_bind": _sanitize_pad_bind(prev_entry.get("speed_pad_bind")),
                "added_at": int(prev_entry.get("added_at") or time.time()),
                "last_played": int(prev_entry.get("last_played") or 0),
            }
            save_local_games(local_games)
            if not want_speed and prev_entry.get("speed_enabled"):
                detach_local_speed_hook(appid, restore_normal=True)

            # Fetch achievement schema, import existing emulator achievements, and pre-seed RUNE/CODEX schema immediately
            schema = parse_game_achievement_schema(steam_path, appid, allow_remote=True)
            capture_new_achievements_from_steam_bin(steam_path, "", appid, session_start_ts=0, game_name=final_name, trigger_popup=False)
            if schema and not is_local_game_running(appid):
                apply_personal_achievements_to_emu_files(appid, get_game_personal_achievements(appid), schema)
            ensure_local_game_assets_async(steam_path, appid)
            build_full_state()

            self.send_json({
                "ok": True,
                "appid": appid,
                "ach_total": len(schema),
                "message": f"Game lokal '{final_name}' (AppID {appid} • {len(schema)} Achievement) berhasil disimpan!"
            })
            return

        if self.path == "/api/remove_local_game":
            appid = str(payload.get("appid") or "").strip()
            local_games = load_local_games()
            if appid in local_games and local_games[appid].get("speed_enabled"):
                detach_local_speed_hook(appid, restore_normal=True)
            removed = local_games.pop(appid, None)
            save_local_games(local_games)
            build_full_state()
            gtitle = (removed or {}).get("name") or f"App {appid}"
            self.send_json({"ok": True, "message": f"'{gtitle}' dihapus dari daftar Game Lokal (file game di PC tetap aman)."})
            return

        if self.path == "/api/local_speed":
            appid = str(payload.get("appid") or "").strip()
            action = str(payload.get("action") or "set_speed").strip()
            if not _is_strictly_local_game(appid):
                self.send_json({
                    "ok": False,
                    "message": "Fitur fast-forward dikunci khusus untuk Game Lokal (Offline Single-Player)."
                }, 403)
                return

            local_games = load_local_games()
            lg = local_games.get(appid)
            if not lg:
                self.send_json({"ok": False, "message": "Game lokal tidak ditemukan."}, 404)
                return

            if action == "toggle_enable":
                enabled = bool(payload.get("enabled", False))
                if not enabled:
                    detach_local_speed_hook(appid, restore_normal=True)
                lg["speed_enabled"] = enabled
                local_games[appid] = lg
                save_local_games(local_games)
                is_run = is_local_game_running(appid)
                st_info = get_local_game_speed_state(appid, is_running=is_run)
                msg_s = (
                    f"⚡ Fast-Forward diaktifkan untuk '{lg.get('name', appid)}' (tanpa menyentuh file game)!"
                    if enabled else f"Fitur Fast-Forward dinonaktifkan untuk '{lg.get('name', appid)}'."
                )
                build_full_state()
                self.send_json({"ok": True, "message": msg_s, **st_info})
                return

            if action == "set_target":
                target_spd = _sanitize_speed_target(payload.get("speed_target", 2.0), 2.0)
                lg["speed_target"] = target_spd
                local_games[appid] = lg
                save_local_games(local_games)
                is_run = is_local_game_running(appid)
                st_cur = get_local_game_speed_state(appid, is_running=is_run)
                # If fast-forward is currently ON while user edits manual number, apply live immediately
                if is_run and st_cur.get("speed_active"):
                    ok_sp, res_sp = set_local_game_speed(appid, target_spd, show_overlay=False)
                    build_full_state()
                    if isinstance(res_sp, dict):
                        self.send_json({"ok": ok_sp, **res_sp}, 200 if ok_sp else 400)
                    else:
                        self.send_json({"ok": ok_sp, "message": str(res_sp)}, 200 if ok_sp else 400)
                    return
                build_full_state()
                st_info = get_local_game_speed_state(appid, is_running=is_run)
                self.send_json({
                    "ok": True,
                    "message": f"Target kecepatan disimpan: {target_spd:g}x",
                    **st_info
                })
                return

            if action == "toggle_active":
                ok_t, res_t = toggle_local_game_speed_now(appid, show_overlay=False)
                build_full_state()
                if isinstance(res_t, dict):
                    self.send_json({"ok": ok_t, **res_t}, 200 if ok_t else 400)
                else:
                    self.send_json({"ok": ok_t, "message": str(res_t)}, 200 if ok_t else 400)
                return

            if action == "set_kb_bind":
                kb_b = _sanitize_kb_bind(payload.get("kb_bind"))
                lg["speed_kb_bind"] = kb_b
                local_games[appid] = lg
                save_local_games(local_games)
                build_full_state()
                st_info = get_local_game_speed_state(appid)
                msg_kb = f"⌨ Toggle Keyboard diatur ke: {kb_b['label']}" if kb_b else "⌨ Toggle Keyboard dikosongkan."
                self.send_json({"ok": True, "message": msg_kb, **st_info})
                return

            if action == "set_pad_bind":
                pad_b = _sanitize_pad_bind(payload.get("pad_bind"))
                lg["speed_pad_bind"] = pad_b
                local_games[appid] = lg
                save_local_games(local_games)
                build_full_state()
                st_info = get_local_game_speed_state(appid)
                msg_pd = f"🎮 Toggle Stick diatur ke: {pad_b['label']}" if pad_b else "🎮 Toggle Stick dikosongkan."
                self.send_json({"ok": True, "message": msg_pd, **st_info})
                return

            if action == "capture_pad":
                captured, cap_err = capture_gamepad_binding(timeout_sec=6.5)
                if not captured:
                    self.send_json({"ok": False, "message": cap_err}, 200)
                    return
                lg = load_local_games().get(appid, lg)
                lg["speed_pad_bind"] = captured
                local_games = load_local_games()
                local_games[appid] = lg
                save_local_games(local_games)
                build_full_state()
                st_info = get_local_game_speed_state(appid)
                self.send_json({
                    "ok": True,
                    "message": f"🎮 Toggle Stick berhasil diatur ke: {captured['label']}!",
                    **st_info
                })
                return

            if action == "cancel_capture_pad":
                CAPTURE_PAD_CANCEL.set()
                self.send_json({"ok": True})
                return

            speed_val = payload.get("speed", 1.0)
            ok_sp, res_sp = set_local_game_speed(appid, speed_val, show_overlay=False)
            build_full_state()
            if isinstance(res_sp, dict):
                self.send_json({"ok": ok_sp, **res_sp}, 200 if ok_sp else 400)
            else:
                self.send_json({"ok": ok_sp, "message": str(res_sp)}, 200 if ok_sp else 400)
            return

        if self.path == "/api/open_local_folder":
            appid = str(payload.get("appid") or "").strip()
            target_kind = str(payload.get("kind") or "game").strip()
            lg = load_local_games().get(appid, {})
            folder = lg.get("save_dir", "") if target_kind == "save" else (lg.get("install_dir") or os.path.dirname(lg.get("exe_path", "")))
            if folder and os.path.isdir(folder):
                try:
                    os.startfile(folder)
                    self.send_json({"ok": True})
                    return
                except Exception as e:
                    self.send_json({"ok": False, "message": f"Gagal membuka folder: {e}"}, 500)
                    return
            self.send_json({"ok": False, "message": "Folder tidak ditemukan di komputer ini."}, 404)
            return

        if self.path == "/api/protected_accounts":
            reg = get_steam_registry_info()
            cur_set = set(load_protected_accounts(reg["steam_path"]))
            action = str(payload.get("action") or "set").strip()
            acc_val = str(payload.get("account") or "").strip().lower()

            if action == "add" and acc_val:
                cur_set.add(acc_val)
                saved = save_protected_accounts(list(cur_set))
                msg_p = f"Akun '{acc_val}' ditambahkan ke daftar proteksi achievement!"
            elif action == "remove" and acc_val:
                cur_set.discard(acc_val)
                saved = save_protected_accounts(list(cur_set))
                msg_p = f"Akun '{acc_val}' dihapus dari daftar proteksi manual."
            elif action == "toggle" and acc_val:
                enabled = bool(payload.get("protected", True))
                if enabled:
                    cur_set.add(acc_val)
                    msg_p = f"Proteksi achievement diaktifkan untuk '{acc_val}'!"
                else:
                    cur_set.discard(acc_val)
                    msg_p = f"Proteksi manual dilepas untuk '{acc_val}'."
                saved = save_protected_accounts(list(cur_set))
            elif isinstance(payload.get("accounts"), list):
                saved = save_protected_accounts(payload["accounts"])
                msg_p = "Daftar akun terproteksi berhasil disimpan!"
            else:
                saved = sorted(list(cur_set))
                msg_p = "OK"

            build_full_state()
            self.send_json({"ok": True, "protected_accounts": saved, "message": msg_p})
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
        <span class="ach-tag" id="toastTag">🏆 ACHIEVEMENT TERBUKA</span>
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
  const SPEED_SVG = "data:image/svg+xml;utf8," + encodeURIComponent(
    '<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64" viewBox="0 0 64 64"><rect width="64" height="64" rx="10" fill="#172554"/><text x="50%" y="54%" dominant-baseline="middle" text-anchor="middle" font-size="30">⚡</text></svg>'
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

  function playUnlockSound(isSpeed) {
    try {
      const Ctx = window.AudioContext || window.webkitAudioContext;
      if (!Ctx) return;
      const ctx = new Ctx();
      const now = ctx.currentTime;
      const notes = isSpeed ? [587.33, 880.0] : [523.25, 659.25, 783.99, 1046.50];
      notes.forEach((freq, idx) => {
        const osc = ctx.createOscillator();
        const gain = ctx.createGain();
        osc.type = isSpeed ? 'sine' : 'triangle';
        const step = isSpeed ? 0.055 : 0.085;
        osc.frequency.setValueAtTime(freq, now + idx * step);
        gain.gain.setValueAtTime(0.001, now + idx * step);
        gain.gain.exponentialRampToValueAtTime(isSpeed ? 0.11 : 0.18, now + idx * step + 0.015);
        gain.gain.exponentialRampToValueAtTime(0.0001, now + idx * step + (isSpeed ? 0.22 : 0.55));
        osc.connect(gain);
        gain.connect(ctx.destination);
        osc.start(now + idx * step);
        osc.stop(now + idx * step + (isSpeed ? 0.25 : 0.6));
      });
    } catch (e) {}
  }

  let queue = [];
  let showing = false;
  let currentKind = '';
  let extendSpeedUntil = 0;

  function renderPopupItem(item) {
    const isSpeed = item.kind === 'speed';
    currentKind = item.kind || 'ach';
    iconEl.dataset.cf = '';
    iconEl.src = isSpeed ? SPEED_SVG : (item.icon_url || FALLBACK_SVG);
    iconEl.style.borderColor = isSpeed ? '#38bdf8' : '#facc15';
    document.getElementById('toastBox').style.borderColor = isSpeed ? 'rgba(56, 189, 248, 0.6)' : 'rgba(250, 204, 21, 0.55)';
    document.getElementById('toastTag').textContent = isSpeed ? '⚡ KECEPATAN GAME LOKAL' : '🏆 ACHIEVEMENT TERBUKA';
    document.getElementById('toastTag').style.color = isSpeed ? '#38bdf8' : '#facc15';
    document.getElementById('toastTitle').textContent = item.title || 'Achievement Unlocked';
    document.getElementById('toastDesc').textContent = item.description || item.game_name || '';
    document.getElementById('toastProg').textContent = isSpeed ? (item.prog_label || '1x') : `${item.unlocked_count || 1}/${item.total_count || 1}`;
  }

  async function processQueue() {
    if (showing || queue.length === 0) return;
    showing = true;
    const item = queue.shift();
    const isSpeed = item.kind === 'speed';
    document.title = 'SHOW_OVERLAY';
    renderPopupItem(item);

    await new Promise(r => setTimeout(r, 40));
    playUnlockSound(isSpeed);
    document.getElementById('toastBox').classList.add('visible');

    if (isSpeed) {
      extendSpeedUntil = Date.now() + 1700;
      while (Date.now() < extendSpeedUntil) {
        await new Promise(r => setTimeout(r, 120));
      }
    } else {
      await new Promise(r => setTimeout(r, 4800));
    }

    document.getElementById('toastBox').classList.remove('visible');
    await new Promise(r => setTimeout(r, 360));

    if (queue.length === 0) {
      document.title = 'HIDE_OVERLAY';
    }
    showing = false;
    currentKind = '';
    if (queue.length > 0) processQueue();
  }

  async function pollPopups() {
    try {
      const res = await fetch('/api/achievement_popups');
      const data = await res.json();
      if (data && Array.isArray(data.popups) && data.popups.length > 0) {
        for (const p of data.popups) {
          if (p.kind === 'speed' && showing && currentKind === 'speed') {
            renderPopupItem(p);
            playUnlockSound(true);
            extendSpeedUntil = Date.now() + 1700;
          } else {
            if (p.kind === 'speed') {
              queue = queue.filter(x => x.kind !== 'speed');
            }
            queue.push(p);
          }
        }
        processQueue();
      }
    } catch (e) {}
  }
  setInterval(pollPopups, 450);
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
        detach_all_speed_hooks()
    except Exception:
        pass
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
    threading.Thread(target=local_speed_hotkey_loop, daemon=True).start()
    open_app_window(hidden=no_window)
    start_system_tray()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        shutdown_application()

