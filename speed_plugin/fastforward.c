#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <winnt.h>
#include <string.h>

#define WM_SS_FASTFORWARD (WM_APP + 0x535)
#define SS_MAGIC_WPARAM   0x5357
#define SS_TIMER_ID       0x535799

typedef void* (*pfn_domain_get)(void);
typedef void* (*pfn_resolve_icall)(const char*);
typedef void  (*pfn_set_timescale)(float);
typedef float (*pfn_get_timescale)(void);

typedef void* (*pfn_mono_image_loaded)(const char*);
typedef void* (*pfn_mono_class_from_name)(void*, const char*, const char*);
typedef void* (*pfn_mono_class_get_method)(void*, const char*, int);
typedef void* (*pfn_mono_runtime_invoke)(void*, void*, void**, void**);
typedef void* (*pfn_mono_object_unbox)(void*);

static pfn_set_timescale       s_set_ts          = NULL;
static pfn_get_timescale       s_get_ts          = NULL;
static pfn_mono_runtime_invoke s_mono_invoke     = NULL;
static pfn_mono_object_unbox   s_mono_unbox      = NULL;
static void*                   s_mono_set_method = NULL;
static void*                   s_mono_get_method = NULL;

static float s_target_speed = 1.0f;
static int   s_timer_active = 0;

static float ff_abs(float x) {
    return x < 0.0f ? -x : x;
}

/* --- Runtime Timer Fallback for Non-Unity x64 Games (100% In-Memory) --- */
typedef BOOL      (WINAPI *pfn_QPC)(LARGE_INTEGER*);
typedef ULONGLONG (WINAPI *pfn_GTC64)(void);
typedef DWORD     (WINAPI *pfn_GTC)(void);
typedef DWORD     (WINAPI *pfn_TGT)(void);

static CRITICAL_SECTION s_tmr_cs;
static int              s_tmr_ready = 0;
static pfn_QPC          s_orig_qpc   = NULL;
static pfn_GTC64        s_orig_gtc64 = NULL;
static pfn_GTC          s_orig_gtc   = NULL;
static pfn_TGT          s_orig_tgt   = NULL;

static LONGLONG  s_qpc_real = 0, s_qpc_fake = 0;
static ULONGLONG s_gtc_real = 0, s_gtc_fake = 0;
static DWORD     s_tgt_real = 0, s_tgt_fake = 0;
static double    s_tmr_scale = 1.0;

static BOOL WINAPI rt_scaled_qpc(LARGE_INTEGER* out) {
    if (!s_orig_qpc) return QueryPerformanceCounter(out);
    LARGE_INTEGER now;
    if (!s_orig_qpc(&now)) return FALSE;
    if (out) {
        EnterCriticalSection(&s_tmr_cs);
        LONGLONG d = now.QuadPart - s_qpc_real;
        if (d < 0) d = 0;
        out->QuadPart = s_qpc_fake + (LONGLONG)((double)d * s_tmr_scale);
        LeaveCriticalSection(&s_tmr_cs);
    }
    return TRUE;
}

static ULONGLONG WINAPI rt_scaled_gtc64(void) {
    ULONGLONG now = s_orig_gtc64 ? s_orig_gtc64() : (ULONGLONG)GetTickCount();
    EnterCriticalSection(&s_tmr_cs);
    ULONGLONG d = (now >= s_gtc_real) ? (now - s_gtc_real) : 0;
    ULONGLONG res = s_gtc_fake + (ULONGLONG)((double)d * s_tmr_scale);
    LeaveCriticalSection(&s_tmr_cs);
    return res;
}

static DWORD WINAPI rt_scaled_gtc(void) {
    if (s_orig_gtc64) return (DWORD)rt_scaled_gtc64();
    DWORD now = s_orig_gtc ? s_orig_gtc() : GetTickCount();
    EnterCriticalSection(&s_tmr_cs);
    DWORD d = now - (DWORD)s_gtc_real;
    if (d >= 0x80000000u) d = 0;
    DWORD res = (DWORD)s_gtc_fake + (DWORD)((double)d * s_tmr_scale);
    LeaveCriticalSection(&s_tmr_cs);
    return res;
}

static DWORD WINAPI rt_scaled_tgt(void) {
    DWORD now = s_orig_tgt ? s_orig_tgt() : GetTickCount();
    EnterCriticalSection(&s_tmr_cs);
    DWORD d = now - s_tgt_real;
    if (d >= 0x80000000u) d = 0;
    DWORD res = s_tgt_fake + (DWORD)((double)d * s_tmr_scale);
    LeaveCriticalSection(&s_tmr_cs);
    return res;
}

static int ensure_generic_runtime_timers(void) {
    if (s_tmr_ready) return 1;
    HMODULE h_exe = GetModuleHandleW(NULL);
    if (!h_exe) return 0;
    BYTE* base = (BYTE*)h_exe;
    PIMAGE_DOS_HEADER dos = (PIMAGE_DOS_HEADER)base;
    if (dos->e_magic != IMAGE_DOS_SIGNATURE) return 0;
    PIMAGE_NT_HEADERS nt = (PIMAGE_NT_HEADERS)(base + dos->e_lfanew);
    if (nt->Signature != IMAGE_NT_SIGNATURE) return 0;
    DWORD imp_rva = nt->OptionalHeader.DataDirectory[IMAGE_DIRECTORY_ENTRY_IMPORT].VirtualAddress;
    if (!imp_rva) return 0;

    InitializeCriticalSection(&s_tmr_cs);
    HMODULE hk32 = GetModuleHandleW(L"kernel32.dll");
    if (hk32) {
        s_orig_qpc   = (pfn_QPC)GetProcAddress(hk32, "QueryPerformanceCounter");
        s_orig_gtc64 = (pfn_GTC64)GetProcAddress(hk32, "GetTickCount64");
        s_orig_gtc   = (pfn_GTC)GetProcAddress(hk32, "GetTickCount");
    }
    HMODULE hwinmm = GetModuleHandleW(L"winmm.dll");
    if (hwinmm) {
        s_orig_tgt = (pfn_TGT)GetProcAddress(hwinmm, "timeGetTime");
    }
    if (s_orig_qpc) {
        LARGE_INTEGER q0;
        if (s_orig_qpc(&q0)) { s_qpc_real = q0.QuadPart; s_qpc_fake = q0.QuadPart; }
    }
    if (s_orig_gtc64) {
        ULONGLONG g0 = s_orig_gtc64();
        s_gtc_real = g0; s_gtc_fake = g0;
    } else if (s_orig_gtc) {
        DWORD g0 = s_orig_gtc();
        s_gtc_real = g0; s_gtc_fake = g0;
    }
    if (s_orig_tgt) {
        DWORD t0 = s_orig_tgt();
        s_tgt_real = t0; s_tgt_fake = t0;
    }

    int patched = 0;
    PIMAGE_IMPORT_DESCRIPTOR desc = (PIMAGE_IMPORT_DESCRIPTOR)(base + imp_rva);
    for (; desc->Name != 0; desc++) {
        if (!desc->OriginalFirstThunk || !desc->FirstThunk) continue;
        PIMAGE_THUNK_DATA orig_t = (PIMAGE_THUNK_DATA)(base + desc->OriginalFirstThunk);
        PIMAGE_THUNK_DATA iat_t  = (PIMAGE_THUNK_DATA)(base + desc->FirstThunk);
        for (; orig_t->u1.AddressOfData != 0; orig_t++, iat_t++) {
            if (IMAGE_SNAP_BY_ORDINAL(orig_t->u1.Ordinal)) continue;
            PIMAGE_IMPORT_BY_NAME ibn = (PIMAGE_IMPORT_BY_NAME)(base + orig_t->u1.AddressOfData);
            const char* fn = (const char*)ibn->Name;
            void* repl = NULL;
            if (strcmp(fn, "QueryPerformanceCounter") == 0) repl = (void*)rt_scaled_qpc;
            else if (strcmp(fn, "GetTickCount64") == 0)     repl = (void*)rt_scaled_gtc64;
            else if (strcmp(fn, "GetTickCount") == 0)       repl = (void*)rt_scaled_gtc;
            else if (strcmp(fn, "timeGetTime") == 0)        repl = (void*)rt_scaled_tgt;
            if (repl) {
                DWORD old_p = 0;
                if (VirtualProtect(&iat_t->u1.Function, sizeof(ULONG_PTR), PAGE_READWRITE, &old_p)) {
                    iat_t->u1.Function = (ULONG_PTR)repl;
                    VirtualProtect(&iat_t->u1.Function, sizeof(ULONG_PTR), old_p, &old_p);
                    patched++;
                }
            }
        }
    }
    if (patched > 0) {
        s_tmr_ready = 1;
        return 1;
    }
    DeleteCriticalSection(&s_tmr_cs);
    return 0;
}

static void set_generic_runtime_scale(double new_scale) {
    if (!ensure_generic_runtime_timers()) return;
    EnterCriticalSection(&s_tmr_cs);
    if (s_orig_qpc) {
        LARGE_INTEGER nq;
        if (s_orig_qpc(&nq)) {
            LONGLONG dq = nq.QuadPart - s_qpc_real;
            if (dq < 0) dq = 0;
            s_qpc_fake += (LONGLONG)((double)dq * s_tmr_scale);
            s_qpc_real = nq.QuadPart;
        }
    }
    if (s_orig_gtc64) {
        ULONGLONG ng = s_orig_gtc64();
        ULONGLONG dg = (ng >= s_gtc_real) ? (ng - s_gtc_real) : 0;
        s_gtc_fake += (ULONGLONG)((double)dg * s_tmr_scale);
        s_gtc_real = ng;
    } else if (s_orig_gtc) {
        DWORD ng32 = s_orig_gtc();
        DWORD dg32 = ng32 - (DWORD)s_gtc_real;
        if (dg32 >= 0x80000000u) dg32 = 0;
        s_gtc_fake = ((DWORD)s_gtc_fake + (DWORD)((double)dg32 * s_tmr_scale));
        s_gtc_real = ng32;
    }
    if (s_orig_tgt) {
        DWORD nt = s_orig_tgt();
        DWORD dt = nt - s_tgt_real;
        if (dt >= 0x80000000u) dt = 0;
        s_tgt_fake += (DWORD)((double)dt * s_tmr_scale);
        s_tgt_real = nt;
    }
    s_tmr_scale = new_scale;
    LeaveCriticalSection(&s_tmr_cs);
}

static int ensure_engine_timescale_api(void) {
    if (s_set_ts || (s_mono_invoke && s_mono_set_method)) {
        return 1;
    }

    /* 1. Unity IL2CPP (GameAssembly.dll) */
    HMODULE h_il2cpp = GetModuleHandleW(L"GameAssembly.dll");
    if (h_il2cpp) {
        pfn_domain_get dom_get = (pfn_domain_get)GetProcAddress(h_il2cpp, "il2cpp_domain_get");
        pfn_resolve_icall res_icall = (pfn_resolve_icall)GetProcAddress(h_il2cpp, "il2cpp_resolve_icall");
        if (dom_get && res_icall && dom_get() != NULL) {
            pfn_set_timescale fn_s = (pfn_set_timescale)res_icall("UnityEngine.Time::set_timeScale(System.Single)");
            if (!fn_s) fn_s = (pfn_set_timescale)res_icall("UnityEngine.Time::set_timeScale");
            pfn_get_timescale fn_g = (pfn_get_timescale)res_icall("UnityEngine.Time::get_timeScale()");
            if (!fn_g) fn_g = (pfn_get_timescale)res_icall("UnityEngine.Time::get_timeScale");
            if (fn_s) {
                s_set_ts = fn_s;
                s_get_ts = fn_g;
                return 1;
            }
        }
    }

    /* 2. Unity Mono (mono-2.0-bdwgc.dll / mono.dll) */
    HMODULE h_mono = GetModuleHandleW(L"mono-2.0-bdwgc.dll");
    if (!h_mono) h_mono = GetModuleHandleW(L"mono.dll");
    if (h_mono) {
        pfn_domain_get m_dom = (pfn_domain_get)GetProcAddress(h_mono, "mono_get_root_domain");
        pfn_mono_image_loaded img_loaded = (pfn_mono_image_loaded)GetProcAddress(h_mono, "mono_image_loaded");
        pfn_mono_class_from_name cls_from_name = (pfn_mono_class_from_name)GetProcAddress(h_mono, "mono_class_from_name");
        pfn_mono_class_get_method get_method = (pfn_mono_class_get_method)GetProcAddress(h_mono, "mono_class_get_method_from_name");
        pfn_mono_runtime_invoke rt_invoke = (pfn_mono_runtime_invoke)GetProcAddress(h_mono, "mono_runtime_invoke");
        pfn_mono_object_unbox obj_unbox = (pfn_mono_object_unbox)GetProcAddress(h_mono, "mono_object_unbox");
        if (m_dom && m_dom() != NULL && img_loaded && cls_from_name && get_method && rt_invoke) {
            void* img = img_loaded("UnityEngine.CoreModule");
            if (!img) img = img_loaded("UnityEngine");
            if (img) {
                void* klass = cls_from_name(img, "UnityEngine", "Time");
                if (klass) {
                    void* m_set = get_method(klass, "set_timeScale", 1);
                    void* m_get = get_method(klass, "get_timeScale", 0);
                    if (m_set) {
                        s_mono_invoke = rt_invoke;
                        s_mono_unbox = obj_unbox;
                        s_mono_set_method = m_set;
                        s_mono_get_method = m_get;
                        return 1;
                    }
                }
            }
        }
    }
    return 0;
}

static float read_current_timescale(void) {
    if (s_get_ts) {
        return s_get_ts();
    }
    if (s_mono_invoke && s_mono_get_method && s_mono_unbox) {
        void* exc = NULL;
        void* res = s_mono_invoke(s_mono_get_method, NULL, NULL, &exc);
        if (res && !exc) {
            float* fp = (float*)s_mono_unbox(res);
            if (fp) return *fp;
        }
    }
    return 1.0f;
}

static void write_timescale(float val) {
    if (s_set_ts) {
        s_set_ts(val);
        return;
    }
    if (s_mono_invoke && s_mono_set_method) {
        void* args[1];
        void* exc = NULL;
        args[0] = &val;
        s_mono_invoke(s_mono_set_method, NULL, args, &exc);
    }
}

static void apply_current_speed_now(void) {
    if (ensure_engine_timescale_api()) {
        float cur = read_current_timescale();
        if (cur <= 0.005f) return; /* Respect pause */
        if (ff_abs(cur - s_target_speed) > 0.01f) {
            write_timescale(s_target_speed);
        }
        return;
    }
    if (GetModuleHandleW(L"UnityPlayer.dll") == NULL) {
        set_generic_runtime_scale((double)s_target_speed);
    }
}

static VOID CALLBACK maintain_speed_timer_proc(HWND hwnd, UINT uMsg, UINT_PTR idEvent, DWORD dwTime) {
    (void)uMsg;
    (void)idEvent;
    (void)dwTime;
    if (ff_abs(s_target_speed - 1.0f) <= 0.01f) {
        if (s_timer_active) {
            KillTimer(hwnd, SS_TIMER_ID);
            s_timer_active = 0;
        }
        return;
    }
    if (ensure_engine_timescale_api()) {
        float cur = read_current_timescale();
        if (cur > 0.005f && ff_abs(cur - s_target_speed) > 0.01f) {
            write_timescale(s_target_speed);
        }
    }
}

__declspec(dllexport) LRESULT CALLBACK FastForwardHookProc(int nCode, WPARAM wParam, LPARAM lParam) {
    if (nCode >= 0 && lParam != 0) {
        CWPSTRUCT* cwp = (CWPSTRUCT*)lParam;
        if (cwp->message == WM_SS_FASTFORWARD && cwp->wParam == SS_MAGIC_WPARAM) {
            long milli = (long)cwp->lParam;
            if (milli >= 100 && milli <= 20000) {
                s_target_speed = (float)milli / 1000.0f;
                apply_current_speed_now();
                if (ff_abs(s_target_speed - 1.0f) > 0.01f) {
                    SetTimer(cwp->hwnd, SS_TIMER_ID, 120, maintain_speed_timer_proc);
                    s_timer_active = 1;
                } else if (s_timer_active) {
                    KillTimer(cwp->hwnd, SS_TIMER_ID);
                    s_timer_active = 0;
                }
            }
        }
    }
    return CallNextHookEx(NULL, nCode, wParam, lParam);
}

BOOL WINAPI DllMain(HINSTANCE hinstDLL, DWORD fdwReason, LPVOID lpvReserved) {
    (void)lpvReserved;
    if (fdwReason == DLL_PROCESS_ATTACH) {
        DisableThreadLibraryCalls(hinstDLL);
    }
    return TRUE;
}
