# -*- coding: utf-8 -*-
"""
NavyHUD v2 - ゲーム用HUDオーバーレイ (Windows / PySide6)

- キーストローク / CPS / FPS / Ping を画面上に重ねて表示 (FPSは 1% Low / 0.1% Low / 平均も表示可)
- FPS は PresentMon(ETW) でゲームの実フレーム出力を数えて算出 (要・管理者権限)
- 設定は変更した瞬間に自動保存 (%APPDATA%\\NavyHUD\\config.json)
- Ctrl+左ドラッグでHUD移動 (画面中央に固定のX線/Y線つき)
"""
import sys
import os
import json
import math
import time
import csv
import re
import colorsys
import ctypes
import threading
import subprocess
import socket
import struct
import unicodedata
import uuid
from ctypes import wintypes
from collections import deque

from PySide6.QtCore import Qt, QTimer, QPoint, QPointF, QRectF, Signal, QObject, QSize
from PySide6.QtGui import (QColor, QPainter, QFont, QPen, QIcon, QGuiApplication,
                           QAction, QPixmap)
from PySide6.QtWidgets import (
    QApplication, QWidget, QMainWindow, QStackedWidget, QScrollArea, QVBoxLayout,
    QHBoxLayout, QGridLayout, QLabel, QPushButton, QSlider, QFrame, QDialog,
    QColorDialog, QComboBox, QSystemTrayIcon, QMenu, QSpinBox, QDoubleSpinBox, QTabWidget,
    QLineEdit, QButtonGroup, QMessageBox, QInputDialog, QTabBar, QAbstractButton)

APP_NAME = "NavyHUD"
APP_VERSION = "2.9"
MIC_IND_AUTO = -99999   # マイク表示の位置が未設定 = 画面右上に出す
MIC_IND_HOLD = 0.6     # 切り替え直後、フェードが始まるまで全表示で見せる秒数
POLL_MS = 5            # キー/クリック検出の更新間隔 (0.005秒)
SNAP_PX = 10           # ガイド線に吸着する距離(px) 線に乗せたいときだけ効く小さめの値
SIDEBAR_W = 224        # 設定画面: サイドバーの幅
PAGE_MARGIN = 36       # 設定画面: ページ左右の余白
CARD_W = 236           # 設定画面: カード1枚の幅
CARD_GAP = 18          # 設定画面: カード同士の間隔
CARD_COLS = 3          # 設定画面: 表示設定のカードを1段に並べる数 (超えたら次の段へ)
PRESET_CARD_W = 228    # プリセット画面: カード1枚の幅 (3枚並べてスクロールバーも収まる幅)
VK_CONTROL = 0x11
PM_EXE_NAME = "NavyHUD_PresentMon.exe"
CREATE_NO_WINDOW = 0x08000000


def resource(name):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


# ======================================================================
#  Win32 helpers
# ======================================================================
user32 = ctypes.WinDLL("user32", use_last_error=True)
winmm = ctypes.WinDLL("winmm")

user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.MapVirtualKeyW.argtypes = [ctypes.c_uint, ctypes.c_uint]
user32.MapVirtualKeyW.restype = ctypes.c_uint
user32.GetKeyNameTextW.argtypes = [ctypes.c_long, wintypes.LPWSTR, ctypes.c_int]
user32.GetKeyNameTextW.restype = ctypes.c_int
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.MessageBeep.argtypes = [ctypes.c_uint]
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.restype = ctypes.c_int
user32.IsIconic.argtypes = [wintypes.HWND]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.BringWindowToTop.argtypes = [wintypes.HWND]
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
_k32 = ctypes.WinDLL("kernel32")
_k32.GetCurrentThreadId.restype = wintypes.DWORD
_k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_k32.OpenProcess.restype = wintypes.HANDLE
_k32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                            ctypes.POINTER(wintypes.DWORD)]
_k32.QueryFullProcessImageNameW.restype = wintypes.BOOL
_k32.CloseHandle.argtypes = [wintypes.HANDLE]
user32.FindWindowExW.argtypes = [wintypes.HWND, wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR]
user32.FindWindowExW.restype = wintypes.HWND

try:
    _get_style = user32.GetWindowLongPtrW
    _set_style = user32.SetWindowLongPtrW
except AttributeError:  # 32bit Python
    _get_style = user32.GetWindowLongW
    _set_style = user32.SetWindowLongW
_get_style.argtypes = [wintypes.HWND, ctypes.c_int]
_get_style.restype = ctypes.c_ssize_t
_set_style.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
_set_style.restype = ctypes.c_ssize_t

GWL_EXSTYLE = -20
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_LAYERED = 0x00080000
WS_EX_NOACTIVATE = 0x08000000


def key_down(vk):
    return bool(user32.GetAsyncKeyState(vk) & 0x8000)


def foreground_pid():
    try:
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return 0
        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return int(pid.value)
    except Exception:
        return 0


# 詳細設定中でも切り替えを許す窓 (タスクトレイの「終了」などを使えるように)
FOCUS_OK_CLASSES = {"Shell_TrayWnd", "Shell_SecondaryTrayWnd", "NotifyIconOverflowWindow",
                    "TopLevelWindowForOverflowXamlIsland", "#32768"}


def pid_exe_name(pid):
    """プロセスID → exe名 (例: Minecraft.Windows.exe)。取れなければ空文字"""
    h = _k32.OpenProcess(0x1000, False, int(pid))          # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        n = wintypes.DWORD(1024)
        if _k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
            return os.path.basename(buf.value)
    finally:
        _k32.CloseHandle(h)
    return ""


def foreground_exe_name():
    """最前面のウィンドウのアプリの exe 名。ストアアプリ(UWP)は中身のアプリを探す"""
    try:
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return ""
        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        name = pid_exe_name(pid.value)
        if name.lower() == "applicationframehost.exe":
            core = user32.FindWindowExW(hwnd, None, "Windows.UI.Core.CoreWindow", None)
            if core:
                p2 = wintypes.DWORD(0)
                user32.GetWindowThreadProcessId(core, ctypes.byref(p2))
                inner = pid_exe_name(p2.value)
                if inner:
                    return inner
        return name
    except Exception:
        return ""


def foreground_class():
    try:
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return 0, ""
        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        buf = ctypes.create_unicode_buffer(128)
        user32.GetClassNameW(hwnd, buf, 128)
        return int(pid.value), buf.value
    except Exception:
        return 0, ""


def bring_to_front(hwnd):
    """他のアプリが最前面でも、指定の窓を最前面に戻す"""
    try:
        h = int(hwnd)
        if user32.IsIconic(h):
            user32.ShowWindow(h, 9)                     # SW_RESTORE (最小化されていたら元に戻す)
        fg = user32.GetForegroundWindow()
        t_fg = user32.GetWindowThreadProcessId(fg, None) if fg else 0
        t_me = _k32.GetCurrentThreadId()
        attached = bool(t_fg and t_fg != t_me and user32.AttachThreadInput(t_me, t_fg, True))
        try:
            user32.BringWindowToTop(h)
            user32.SetForegroundWindow(h)
        finally:
            if attached:
                user32.AttachThreadInput(t_me, t_fg, False)
    except Exception:
        pass


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def set_click_through(hwnd, enable):
    """enable=True: マウス操作がHUDを素通りしてゲームに届く"""
    try:
        h = int(hwnd)
        style = _get_style(h, GWL_EXSTYLE)
        style |= WS_EX_LAYERED | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW
        if enable:
            style |= WS_EX_TRANSPARENT
        else:
            style &= ~WS_EX_TRANSPARENT
        _set_style(h, GWL_EXSTYLE, style)
    except Exception:
        pass


def vk_name(vk):
    special = {
        0x01: "マウス左", 0x02: "マウス右", 0x04: "マウス中",
        0x05: "マウスX1", 0x06: "マウスX2",
        0x20: "Space", 0x10: "Shift", 0x11: "Ctrl", 0x12: "Alt",
        0x09: "Tab", 0x0D: "Enter", 0x08: "BackSpace", 0x14: "CapsLock",
        0x1B: "Esc", 0x25: "←", 0x26: "↑", 0x27: "→", 0x28: "↓",
    }
    if vk in special:
        return special[vk]
    if 0x30 <= vk <= 0x39 or 0x41 <= vk <= 0x5A:
        return chr(vk)
    if 0x70 <= vk <= 0x87:
        return "F%d" % (vk - 0x6F)
    sc = user32.MapVirtualKeyW(vk, 0)
    buf = ctypes.create_unicode_buffer(64)
    if sc and user32.GetKeyNameTextW(sc << 16, buf, 64):
        return buf.value
    return "VK%d" % vk


# ======================================================================
#  マイクのミュート (Windows Core Audio / 追加ライブラリ不要)
#   既定の録音デバイス(マイク)の「ミュート」を切り替える。COMを ctypes だけで直接呼ぶ。
# ======================================================================
class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16),
                ("Data3", ctypes.c_uint16), ("Data4", ctypes.c_ubyte * 8)]


def _guid(text):
    return _GUID.from_buffer_copy(uuid.UUID(text).bytes_le)


_CLSID_MMDeviceEnumerator = _guid("BCDE0395-E52F-467C-8E3D-C4579291692E")
_IID_IMMDeviceEnumerator = _guid("A95664D2-9614-4F35-A746-DE8DB63617E6")
_IID_IAudioEndpointVolume = _guid("5CDF2C82-841E-4546-9722-0CF74078229A")
_CLSCTX_ALL = 23
_E_CAPTURE, _E_CONSOLE = 1, 0           # EDataFlow.eCapture / ERole.eConsole (= 既定の録音デバイス)
_E_NOTFOUND = 0x80070490

_ole32 = ctypes.WinDLL("ole32")
_ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
_ole32.CoInitializeEx.restype = ctypes.c_long
_ole32.CoUninitialize.argtypes = []
_ole32.CoUninitialize.restype = None
_ole32.CoCreateInstance.argtypes = [ctypes.POINTER(_GUID), ctypes.c_void_p, wintypes.DWORD,
                                    ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p)]
_ole32.CoCreateInstance.restype = ctypes.c_long


def _vfn(obj, index, *argtypes):
    """COMオブジェクト(obj)の仮想関数表から index 番目の関数を取り出す (戻り値は HRESULT)"""
    vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.c_void_p))[0]
    addr = ctypes.cast(vtbl, ctypes.POINTER(ctypes.c_void_p))[index]
    return ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)(addr)


def _com_release(obj):
    try:
        if obj and obj.value:
            _vfn(obj, 2)(obj)
    except Exception:
        pass


def mic_mute_op(toggle, ole32=None):
    """既定のマイクのミュート状態を返す (toggle=True なら反転してから)。
    戻り値 (ミュート中か, エラー文字列)。エラーがなければ後者は空文字"""
    ole = ole32 or _ole32
    enum, dev, vol = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
    try:
        hr = ole.CoCreateInstance(ctypes.byref(_CLSID_MMDeviceEnumerator), None, _CLSCTX_ALL,
                                  ctypes.byref(_IID_IMMDeviceEnumerator), ctypes.byref(enum))
        if hr < 0 or not enum.value:
            return None, "CoCreateInstance 0x%08X" % (hr & 0xFFFFFFFF)
        hr = _vfn(enum, 4, ctypes.c_uint, ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p))(
            enum, _E_CAPTURE, _E_CONSOLE, ctypes.byref(dev))                     # GetDefaultAudioEndpoint
        if hr < 0 or not dev.value:
            return None, "nomic" if (hr & 0xFFFFFFFF) == _E_NOTFOUND else "GetDefaultAudioEndpoint 0x%08X" % (hr & 0xFFFFFFFF)
        hr = _vfn(dev, 3, ctypes.POINTER(_GUID), wintypes.DWORD, ctypes.c_void_p,
                  ctypes.POINTER(ctypes.c_void_p))(
            dev, ctypes.byref(_IID_IAudioEndpointVolume), _CLSCTX_ALL, None, ctypes.byref(vol))   # Activate
        if hr < 0 or not vol.value:
            return None, "Activate 0x%08X" % (hr & 0xFFFFFFFF)
        cur = wintypes.BOOL(0)
        hr = _vfn(vol, 15, ctypes.POINTER(wintypes.BOOL))(vol, ctypes.byref(cur))                  # GetMute
        if hr < 0:
            return None, "GetMute 0x%08X" % (hr & 0xFFFFFFFF)
        muted = bool(cur.value)
        if toggle:
            muted = not muted
            hr = _vfn(vol, 14, wintypes.BOOL, ctypes.c_void_p)(vol, 1 if muted else 0, None)       # SetMute
            if hr < 0:
                return None, "SetMute 0x%08X" % (hr & 0xFFFFFFFF)
        return muted, ""
    except Exception as e:
        return None, "%s" % (e,)
    finally:
        _com_release(vol)
        _com_release(dev)
        _com_release(enum)


class MicMute:
    """マイクのミュート切り替え。COMは待たされることがあるので別スレッドで行う。"""

    def __init__(self):
        self.muted = None       # None = 不明 / True = ミュート中 / False = ミュートしていない
        self.error = ""
        self._busy = False

    def status_text(self):
        if self.error == "nomic":
            return T("マイクが見つかりません")
        if self.error:
            return T("取得できません (%s)") % self.error
        if self.muted is None:
            return T("確認中…")
        return T("ミュート中") if self.muted else T("ミュートしていません")

    def toggle(self):
        self._start(True)

    def refresh(self):
        self._start(False)

    def _start(self, toggle):
        if self._busy:
            return
        self._busy = True
        threading.Thread(target=self._run, args=(toggle,), name="NavyHUD-mic", daemon=True).start()

    def _run(self, toggle):
        inited = False
        try:
            hr = _ole32.CoInitializeEx(None, 0)         # COINIT_MULTITHREADED (このスレッド専用)
            inited = hr in (0, 1)                       # S_OK / S_FALSE
            self.muted, self.error = mic_mute_op(toggle)
        except Exception as e:
            self.muted, self.error = None, "%s" % (e,)
        finally:
            if inited:
                try:
                    _ole32.CoUninitialize()
                except Exception:
                    pass
            self._busy = False



# ======================================================================
#  FPS 測定 (PresentMon / ETW)
#   ゲームが画面にフレームを出した回数(Present)を直接数える。
#   マイクラ(Java版/統合版)のF3表示と同じ「1秒間のフレーム数」。
# ======================================================================
PM_API = "https://api.github.com/repos/GameTechDev/PresentMon/releases/latest"
PM_ASSET_RE = re.compile(r"^PresentMon-[\d.]+-x64\.exe$")     # .msi や x86 は対象外
WINDOW_SECONDS = 60.0     # 保持する直近のフレーム時間
STALE_SECONDS = 3.0       # これ以上フレームが届かなければ「止まった」とみなす
IGNORED_APPS = {"dwm", "explorer", "<unknown>", ""}
GAME_HINTS = {"javaw", "java", "minecraft.windows", "minecraft", "minecraftlauncher"}


def _app_key(name):
    name = name.strip().lower()
    return name[:-4] if name.endswith(".exe") else name


def find_presentmon(extra_dir=None):
    here = os.path.dirname(sys.executable if getattr(sys, "frozen", False)
                           else os.path.abspath(__file__))
    folders = [here, os.path.join(here, "tools")]
    if extra_dir:
        folders += [extra_dir, os.path.join(extra_dir, "tools")]
    folders.append(getattr(sys, "_MEIPASS", here))
    for folder in folders:
        try:
            names = sorted((n for n in os.listdir(folder)
                            if n.lower().endswith(".exe") and "presentmon" in n.lower()), reverse=True)
        except OSError:
            continue
        if names:
            return os.path.join(folder, names[0])
    return None


def kill_stale_instances():
    """すでに動いている古い NavyHUD.exe を終了する。
    管理者として動いているので、通常のコマンドプロンプトからの taskkill では消せない。
    (自分自身と、onefile版の親プロセスは除く。二重に動くとFPS測定が取り合いになる)"""
    try:
        subprocess.run(["taskkill", "/F", "/FI", "IMAGENAME eq NavyHUD.exe",
                        "/FI", "PID ne %d" % os.getpid(), "/FI", "PID ne %d" % os.getppid()],
                       capture_output=True, creationflags=CREATE_NO_WINDOW, timeout=8)
        subprocess.run(["taskkill", "/F", "/IM", PM_EXE_NAME],
                       capture_output=True, creationflags=CREATE_NO_WINDOW, timeout=8)
    except Exception:
        pass
    time.sleep(0.4)


_single_mutex = None


def acquire_single_instance():
    """True: 自分だけ / False: すでに別のNavyHUDが動いている"""
    global _single_mutex
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        k32.CreateMutexW.restype = wintypes.HANDLE
        _single_mutex = k32.CreateMutexW(None, False, "Local\\NavyHUD_SingleInstance")
        return ctypes.get_last_error() != 183           # ERROR_ALREADY_EXISTS
    except Exception:
        return True


def kill_presentmon():
    for cmd in (["taskkill", "/F", "/IM", PM_EXE_NAME], ["logman", "stop", "PresentMon", "-ets"]):
        try:
            subprocess.run(cmd, capture_output=True, creationflags=CREATE_NO_WINDOW, timeout=8)
        except Exception:
            pass


def download_presentmon(dest, log):
    """同梱されていない場合、公式GitHubの「最新版」PresentMon(x64コンソール版)を取得する"""
    import urllib.request
    try:
        req = urllib.request.Request(PM_API, headers={"User-Agent": "NavyHUD",
                                                      "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            release = json.load(r)
        asset = next((a for a in release.get("assets", []) if PM_ASSET_RE.match(a.get("name", ""))), None)
        if asset is None:
            log("download: x64 asset not found")
            return False
        url = asset["browser_download_url"]
        if not url.startswith("https://github.com/GameTechDev/PresentMon/"):
            log("download: unexpected url " + url)
            return False
        log("download: " + url)
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "NavyHUD"}),
                                    timeout=60) as r:
            data = r.read()
        if len(data) > 100000 and data[:2] == b"MZ":
            tmp = dest + ".part"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, dest)
            return True
        log("download: invalid file (%d bytes)" % len(data))
    except Exception as ex:
        log("download failed: %r" % (ex,))
    return False


# ======================================================================
#  Ping 測定 (ICMP / TCP接続時間 / 統合版(RakNet)サーバー応答)
# ======================================================================
_PING_MS_RE = re.compile(rb"[=<]\s*(\d+)\s*ms", re.IGNORECASE)    # 言語が違っても「=12ms」「<1ms」の形は同じ
_PING_HOST_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._\-]*|[0-9A-Fa-f:.]*:[0-9A-Fa-f:.]*")
RAKNET_MAGIC = bytes.fromhex("00ffff00fefefefefdfdfdfd12345678")
BEDROCK_PORTS = (19132, 19133)          # 統合版の標準ポート (UDP)


_PING_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://")
_PING_V6_RE = re.compile(r"\[([0-9A-Fa-f:.]+)\](?::(\d+))?")
_PING_HP_RE = re.compile(r"(\S+?)\s*[:\s]\s*(\d+)")
_PING_NAME_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._\-]*")
_PING_V6LIT_RE = re.compile(r"[0-9A-Fa-f:.]*:[0-9A-Fa-f:.]*")


def parse_ping_target(text):
    """測定先の文字 → (host, port or None)。読み取れなければ None。
    全角文字・前後の空白・http:// などの頭・/以降のパス・日本語ドメイン・[IPv6]:ポート・「ホスト 19132」も受け付ける。
    ping.exe にそのまま渡すので、'-' で始まる文字列(オプション扱い)や記号は通さない。"""
    t = unicodedata.normalize("NFKC", text or "").strip()
    t = _PING_SCHEME_RE.sub("", t)
    t = re.split(r"[/?#]", t, maxsplit=1)[0].rsplit("@", 1)[-1].strip()
    if not t or len(t) > 253:
        return None
    host, ps = t, None
    m = _PING_V6_RE.fullmatch(t)
    if m:
        host, ps = m.group(1), m.group(2)
    elif t.count(":") <= 1:
        m = _PING_HP_RE.fullmatch(t)
        if m:
            host, ps = m.group(1), m.group(2)
    port = None
    if ps is not None:
        if not (0 < int(ps) < 65536):
            return None
        port = int(ps)
    host = host.rstrip(".")
    if ":" in host:
        return (host, port) if (host.count(":") >= 2 and _PING_V6LIT_RE.fullmatch(host)) else None
    try:
        host = host.encode("idna").decode("ascii")      # 日本語などのドメイン名
    except Exception:
        return None
    return (host, port) if (_PING_NAME_RE.fullmatch(host) and not host.isdigit()) else None


def parse_ping_output(raw):
    """ping.exe の出力(bytes)から応答時間(ms)を取り出す。応答なしは None。"""
    m = _PING_MS_RE.search(raw or b"")
    return max(1, int(m.group(1))) if m else None


def icmp_ping(host, timeout_ms=1000):
    """(ms or None, エラー文 or '')。エラー文は ping.exe 自体を実行できなかったときだけ入る。"""
    try:
        r = subprocess.run(["ping", "-n", "1", "-w", str(int(timeout_ms)), host],
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                           creationflags=CREATE_NO_WINDOW, timeout=timeout_ms / 1000.0 + 2.5)
    except subprocess.TimeoutExpired:
        return None, ""
    except FileNotFoundError:
        return None, "ping.exe が見つかりません"
    except Exception as e:
        return None, "ping.exe を実行できません: %s" % e
    return parse_ping_output(r.stdout), ""


def tcp_ping(host, port, timeout=1.0):
    """名前解決を除いた、TCP接続にかかった時間(ms)"""
    try:
        fam, typ, proto, _c, sa = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)[0]
        sk = socket.socket(fam, typ, proto)
        sk.settimeout(timeout)
        try:
            t0 = time.perf_counter()
            sk.connect(sa)
            dt = time.perf_counter() - t0
        finally:
            sk.close()
        return max(1, int(round(dt * 1000)))
    except Exception:
        return None


def raknet_ping(host, port, timeout=1.0):
    """統合版(Bedrock)サーバーにUDPで「Unconnected Ping」を送り、返事が来るまでの時間(ms)"""
    try:
        fam, typ, proto, _c, sa = socket.getaddrinfo(host, port, type=socket.SOCK_DGRAM)[0]
        sk = socket.socket(fam, typ, proto)
        sk.settimeout(timeout)
        try:
            pkt = (b"\x01" + struct.pack(">q", int(time.time() * 1000) & 0x7FFFFFFFFFFFFFFF)
                   + RAKNET_MAGIC + struct.pack(">q", 2))
            t0 = time.perf_counter()
            sk.sendto(pkt, sa)
            while time.perf_counter() - t0 < timeout:
                data, _addr = sk.recvfrom(4096)
                if data[:1] in (b"\x1c", b"\x1d"):      # Unconnected Pong
                    return max(1, int(round((time.perf_counter() - t0) * 1000)))
        finally:
            sk.close()
    except Exception:
        pass
    return None


def measure_ping(host, port):
    """(ms or None, 使った方法, 試したが駄目だった方法の一覧)
    ポートなし: ICMP → 駄目なら TCP 443 → TCP 80。ポートあり: 統合版の標準ポートなら RakNet → TCP、それ以外は TCP → RakNet。"""
    tried = []
    if port:
        order = ("raknet", "tcp") if port in BEDROCK_PORTS else ("tcp", "raknet")
        for m in order:
            if m == "raknet":
                ms, name = raknet_ping(host, port), "RakNet(UDP)"
            else:
                ms, name = tcp_ping(host, port), "TCP"
            if ms is not None:
                return ms, name, tried
            tried.append(name)
        return None, "", tried
    ms, err = icmp_ping(host)
    if ms is not None:
        return ms, "ICMP", tried
    tried.append("ICMP(%s)" % err if err else "ICMP")
    for pt in (443, 80):
        ms = tcp_ping(host, pt)
        if ms is not None:
            return ms, "TCP %d" % pt, tried
        tried.append("TCP %d" % pt)
    return None, "", tried


class PingMonitor:
    """別スレッドで一定間隔ごとに応答時間を測る。active が False の間は何も送らない。"""

    def __init__(self, get_settings):
        self.get_settings = get_settings        # () -> (host文字列, 間隔ms, 設定画面のポート(0=なし))
        self.value = None                       # 直近の ms (測れなければ None)
        self.status = ("測定待ち…", ())          # 設定画面の「状態」用 (日本語の元文, 引数)
        self.active = False
        self._stop = threading.Event()
        self._th = threading.Thread(target=self._run, name="NavyHUD-ping", daemon=True)
        self._th.start()

    def status_text(self):
        key, args = self.status
        return T(key) % args if args else T(key)

    def shutdown(self):
        self._stop.set()

    def _run(self):
        last = None
        while not self._stop.is_set():
            wait = 1.0
            try:
                raw, interval, cport = self.get_settings()
                if (raw, cport) != last:        # 測定先を変えたら古い値は捨てる
                    last = (raw, cport)
                    self.value = None
                    self.status = ("測定待ち…", ())
                if not self.active:
                    self.value = None
                    self._stop.wait(0.25)
                    continue
                t0 = time.perf_counter()
                target = parse_ping_target(raw)
                if target is None:
                    self.value = None
                    shown = unicodedata.normalize("NFKC", raw or "").strip()
                    if shown.isdigit():
                        self.status = ("アドレス欄が数字だけです。サーバーのアドレスを入れ、ポート番号は下の「ポート」欄に入れてください", ())
                    elif shown:
                        self.status = ("測定先「%s」を読み取れません (例: 1.1.1.1 / example.com / example.com:19132)",
                                       (shown[:40],))
                    else:
                        self.status = ("測定先が空です (例: 1.1.1.1 / example.com / example.com:19132)", ())
                else:
                    host, port = target
                    if port is None and cport:          # 「ポート」欄の指定 (アドレス欄に :ポート があればそちら優先)
                        port = int(cport)
                    ms, how, tried = measure_ping(host, port)
                    label = "%s:%d" % (host, port) if port else host
                    self.value = ms
                    if ms is not None:
                        self.status = ("%s  →  %d ms  (%s)", (label, ms, how))
                    else:
                        self.status = ("%s  →  応答なし  (試した方法: %s)", (label, " / ".join(tried)))
                wait = max(0.05, interval / 1000.0 - (time.perf_counter() - t0))
            except Exception as e:
                self.value = None
                self.status = ("測定エラー: %s", (e,))
            self._stop.wait(wait)


class PresentMonFps:
    """FPS測定。PC_Monitor と同じ仕組み:
    PresentMon(最新版)を `--output_stdout --stop_existing_session` で起動 (だめなら `-` 形式)、
    各行の msBetweenPresents / FrameTime を足し合わせた「ストリーム上の時計」で直近1秒の
    フレーム数を数える。パイプのバッファで届くのが遅れても数字がぶれない。"""

    def __init__(self, data_dir=None):
        self.lock = threading.Lock()
        self.frames = {}     # pid -> deque((ストリーム時刻[秒], フレーム時間ms))
        self.clock = {}      # pid -> 積算した時刻[秒]
        self.arrival = {}    # pid -> 最後の行を受け取った実時刻
        self.names = {}      # pid -> アプリ名(小文字, .exeなし)
        self.status = "starting"   # starting / ok / need_admin / noexe / downloading / error
        self.detail = ""
        self.rows = 0
        self.extra = (None, None, None)     # (1% Low, 0.1% Low, 平均) のFPS。測れていなければ None
        self._extra_t = 0.0
        self.proc = None
        self.data_dir = data_dir
        self.log_path = os.path.join(data_dir, "fps_debug.log") if data_dir else None
        self._log_n = 0
        if self.log_path:
            try:
                open(self.log_path, "w", encoding="utf-8").close()
            except Exception:
                self.log_path = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    # ---- 診断ログ (%APPDATA%\\NavyHUD\\fps_debug.log) ----
    def _log(self, msg):
        if not self.log_path or self._log_n > 400:
            return
        self._log_n += 1
        try:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write("%s  %s\n" % (time.strftime("%H:%M:%S"), msg))
        except Exception:
            pass

    # ---- 状態表示 ----
    def status_text(self):
        base = {
            "starting": "測定エンジンを起動中…",
            "ok": "測定中",
            "need_admin": "管理者権限が必要です (NavyHUD.exe を管理者で実行)",
            "noexe": "PresentMon が見つかりません (build.bat で同梱されます)",
            "downloading": "PresentMon をダウンロード中…",
            "error": "測定エンジンの起動に失敗。再試行中…",
        }.get(self.status, self.status)
        return T(base) + ((" / " + self.detail) if self.detail else "")

    def shutdown(self):
        self._stop.set()
        try:
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()
        except Exception:
            pass
        kill_presentmon()

    # ---- 子プロセス管理 ----
    def _run(self):
        exe = find_presentmon(self.data_dir)
        if not exe and self.data_dir:
            self.status = "downloading"
            dest = os.path.join(self.data_dir, PM_EXE_NAME)
            if download_presentmon(dest, self._log):
                exe = dest
        if not exe:
            self.status = "noexe"
            self._log("PresentMon exe not found")
            return
        self._log("exe: " + exe)
        if not is_admin():
            self.status = "need_admin"
            self._log("not admin")
            return
        kill_presentmon()
        while not self._stop.is_set():
            started = False
            # v2系は "--"、v1系は "-" 形式。どちらかで起動できた方を使う (PC_Monitorと同じ)
            for flag in ("--", "-"):
                if self._stop.is_set():
                    return
                if self._start(exe, flag):
                    started = True
                    self._read(self.proc)          # 終了するまで読み続ける
                    break
            self.status = "starting" if started else "error"
            self._stop.wait(1.0 if started else 4.0)

    def _start(self, exe, flag):
        args = [exe, flag + "output_stdout", flag + "stop_existing_session"]
        self._log("spawn: " + " ".join(args))
        try:
            proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    stdin=subprocess.DEVNULL, text=True, encoding="utf-8",
                                    errors="replace", creationflags=CREATE_NO_WINDOW)
        except OSError as ex:
            self._log("spawn failed: %r" % (ex,))
            self.detail = T("起動エラー: %s") % (ex,)
            return False
        time.sleep(0.6)
        if proc.poll() is None:
            self.proc = proc
            self.detail = ""
            threading.Thread(target=self._drain, args=(proc.stderr,), daemon=True).start()
            return True
        texts = []
        for stream in (proc.stderr, proc.stdout):       # エラーはどちらに出ることもあるので両方読む
            try:
                texts.append(stream.read() or "")
            except Exception:
                texts.append("")
        err = "\n".join(t for t in texts if t.strip())
        code = proc.returncode
        self._log("exited early (flag=%soutput_stdout exit=%r): %s" % (flag, code, err.strip()[:300]))
        low = err.lower()
        if "access" in low or "privilege" in low or "administrator" in low or "performance log" in low:
            self.status = "need_admin"
        if flag == "--":        # 本命の形式の理由を表示 (次に試す "-" 形式の失敗では上書きしない)
            first = next((ln.strip() for ln in err.splitlines() if ln.strip()), "")
            self.detail = T("PresentMonが終了しました (終了コード %s) %s") % (code, first[:90])
        return False

    def _drain(self, stream):
        try:
            for line in stream:
                t = line.strip()
                if t:
                    self._log("stderr: " + t[:200])
        except Exception:
            pass

    def _read(self, proc):
        try:
            rows = csv.reader(proc.stdout)
            header = None
            for row in rows:                    # 見出し行まで読み飛ばす
                low = [h.strip().lstrip("﻿").lower() for h in row]
                if "application" in low or "processname" in low:
                    header = low
                    break
                if self._log_n < 20:
                    self._log("stdout: " + ",".join(row)[:200])
            if header is None:
                return

            def col(*names):
                return next((header.index(n) for n in names if n in header), None)
            ms_col = col("msbetweenpresents", "frametime")
            app_col = col("application", "processname")
            pid_col = col("processid")
            self._log("header: " + ",".join(header)[:300])
            if ms_col is None or app_col is None or pid_col is None:
                self.status = "error"
                self.detail = T("PresentMon の出力形式が未対応です")
                return
            self.status = "ok"
            self.detail = ""
            last_prune = 0.0
            for row in rows:
                if self._stop.is_set():
                    break
                try:
                    ms, pid = float(row[ms_col]), int(row[pid_col])
                except (ValueError, IndexError):
                    continue
                if ms <= 0:
                    continue
                now = time.monotonic()
                with self.lock:
                    self.names[pid] = _app_key(row[app_col])
                    t = self.clock.get(pid, 0.0) + ms / 1000.0
                    self.clock[pid] = t
                    self.arrival[pid] = now
                    d = self.frames.setdefault(pid, deque(maxlen=30000))
                    d.append((t, ms))
                    while d and t - d[0][0] > WINDOW_SECONDS:
                        d.popleft()
                    if now - last_prune > 5.0:      # 動いていないプロセスを掃除
                        last_prune = now
                        for k in [k for k, a in self.arrival.items() if now - a > WINDOW_SECONDS]:
                            for table in (self.frames, self.clock, self.arrival, self.names):
                                table.pop(k, None)
                self.rows += 1
                if self.rows <= 3:
                    self._log("row: " + ",".join(row)[:200])
        except Exception as ex:
            self._log("read error: %r" % (ex,))
        finally:
            try:
                if proc.poll() is None:
                    proc.terminate()
                code = proc.wait(timeout=3)
            except Exception:
                code = None
            self._log("process ended (exit=%r, rows=%d)" % (code, self.rows))

    # ---- FPS 算出 ----
    @staticmethod
    def _fps(frames):
        """直近1秒の (フレーム間隔の数 ÷ 経過時間)。時刻はPresentMonのフレーム時間の積算"""
        if not frames:
            return None
        end = frames[-1][0]
        window = [t for t, _ in frames if end - t <= 1.0]
        span = end - window[0]
        if len(window) >= 2 and span > 0:
            return (len(window) - 1) / span
        return None

    @staticmethod
    def _low_stats(frames):
        """直近のフレーム時間から (1% Low, 0.1% Low, 平均) のFPSを求める。
        Low = 「遅い方から1% (0.1%) のフレーム」の平均フレーム時間をFPSに直した値。
        フレーム数が足りないうちは None (1%は100枚、0.1%は1000枚以上たまってから)。"""
        ms = [m for _, m in frames]
        n = len(ms)
        if n < 2:
            return (None, None, None)
        total = sum(ms)
        avg = int(round(1000.0 * n / total)) if total > 0 else None
        ms.sort(reverse=True)

        def low(pct):
            if n < int(math.ceil(100.0 / pct)):
                return None
            k = max(1, int(round(n * pct / 100.0)))
            worst = sum(ms[:k]) / k
            return int(round(1000.0 / worst)) if worst > 0 else None
        return (low(1.0), low(0.1), avg)

    def measure(self, now, fg_pid, target, want_extra=False):
        """(FPS or None, 説明テキスト)。測れていないときは 0 ではなく None (= 「--」表示)。
        want_extra=True のときは 1% Low / 0.1% Low / 平均も計算して self.extra に入れる"""
        fps, text, frames = self._measure(fg_pid, target)
        if want_extra and fps is not None and frames:
            t = time.monotonic()
            if t - self._extra_t >= 0.25:           # 重い計算なので 0.25秒に1回まで
                self._extra_t = t
                self.extra = self._low_stats(frames)
        else:
            self.extra = (None, None, None)
            self._extra_t = 0.0
        return fps, text

    def _measure(self, fg_pid, target):
        """(FPS or None, 説明テキスト, 直近のフレーム一覧 or None)"""
        if self.status != "ok":
            return None, self.status_text(), None
        own = os.getpid()
        mono = time.monotonic()
        with self.lock:
            fresh = [p for p, a in self.arrival.items()
                     if mono - a <= STALE_SECONDS and self.names.get(p) not in IGNORED_APPS
                     and p != own]
            chosen = None
            if target:
                t = _app_key(target)
                same = [p for p in fresh if self.names.get(p) == t]
                if same:
                    chosen = max(same, key=lambda p: self.arrival[p])
                else:
                    return None, T("「%s」の描画を待機中 (受信 %d 行)") % (target, self.rows), None
            elif fg_pid in fresh:
                chosen = fg_pid
            else:
                games = [p for p in fresh if self.names.get(p) in GAME_HINTS]
                pool = games or fresh
                if pool:
                    chosen = max(pool, key=lambda p: sum(1 for t, _ in self.frames[p]
                                                         if self.clock[p] - t < 1.0))
            if chosen is None:
                return None, T("最前面のゲームの描画を待機中 (受信 %d 行)") % self.rows, None
            frames = list(self.frames.get(chosen, ()))
            name = self.names.get(chosen, "?")
        fps = self._fps(frames)
        if fps is None:
            return None, T("%s (PID %d) 計測待ち") % (name, chosen), None
        return int(round(fps)), "%s (PID %d)" % (name, chosen), frames


# ======================================================================
#  設定 (変更と同時に自動保存)
# ======================================================================
DEFAULT_KEYS = {"forward": 0x57, "back": 0x53, "left": 0x41, "right": 0x44,
                "jump": 0x20, "dash": 0x10, "sprint": 0x09, "lmb": 0x01, "rmb": 0x02}
KEY_LABELS = {"forward": "前進", "back": "後進", "left": "左移動", "right": "右移動",
              "jump": "ジャンプ", "dash": "ダッシュ", "sprint": "スプリント",
              "lmb": "左クリック", "rmb": "右クリック"}
MOVE_KEYS = ["forward", "back", "left", "right", "jump"]
EXTRA_KEYS = ["dash", "sprint"]         # 初期割り当て: ダッシュ=Shift / スプリント=Tab (押されたかどうかだけ検知)
CLICK_KEYS = ["lmb", "rmb"]

COMMON_DEFAULT = dict(   # 初期設定 = 「すべて初期化」の結果 = 同じ値 (基本の見た目だけ)
    enabled=False, opacity=100, scale=100,
    text_color="#ffffff", text_opacity=100, text_size=100, text_w=100, text_h=100,
    text_y=0, text_spacing=0,
    bg_on=True, bg_color="#000000", bg_opacity=50, bg_w=100, bg_h=100, corner=6,
    border_on=False, border_color="#ffffff", border_width=1, border_opacity=60,
    border_blur=False, border_blur_size=8,
)
HUD_ORDER = ["cps", "fps", "keystrokes", "ping"]
HUD_DEFS = {
    "cps": ("CPS", "左クリックと右クリックの毎秒クリック数", dict(x=80, y=300)),
    "fps": ("FPS", "ゲームの実フレームレート (PresentMon)",
            dict(fps_label=False, fps_interval=1000, fps_target="",
                 show_low1=False, show_low01=False, show_avg=False, x=80, y=240)),
    "keystrokes": ("キーストローク", "WASD / スペース / マウスボタン",
                   dict(show_mouse=True, show_dash=False, show_sprint=False,
                        pressed_color="#7a7a7a", pressed_opacity=85, x=80, y=380)),
    "ping": ("Ping", "サーバー(ホスト)までの応答時間 (ms)",
             dict(ping_format=1, ping_interval=1000, ping_host="1.1.1.1", ping_port=0, x=80, y=200)),
}
GROUP_KEYS = {
    "basic": ["opacity", "scale", "x", "y"],
    "text": ["text_color", "text_opacity", "text_size", "text_w", "text_h", "text_y", "text_spacing"],
    "bg": ["bg_on", "bg_color", "bg_opacity", "bg_w", "bg_h", "corner"],
    "border": ["border_on", "border_color", "border_width", "border_opacity",
               "border_blur", "border_blur_size"],
    "keystrokes": ["show_mouse", "show_dash", "show_sprint", "pressed_color", "pressed_opacity"],
    "fps": ["fps_label", "fps_interval", "fps_target", "show_low1", "show_low01", "show_avg"],
    "ping": ["ping_format", "ping_interval", "ping_host", "ping_port"],
}


# Pingのテンプレート (統合版サーバー): (ボタン名, アドレス, ポート)
PING_TEMPLATES = [("Hive", "geo.hivebedrock.network", 19132),
                  ("Zeqa", "zeqa.net", 19132),
                  ("CubeCraft", "play.cubecraft.net", 19132)]


def default_hud(hid):
    return {**COMMON_DEFAULT, **HUD_DEFS[hid][2]}


class Config(QObject):
    hud_changed = Signal(str)
    keys_changed = Signal()

    def __init__(self):
        super().__init__()
        folder = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), APP_NAME)
        os.makedirs(folder, exist_ok=True)
        self.folder = folder
        self.path = os.path.join(folder, "config.json")
        self.data = self._defaults()
        self._load()
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(120)
        self._timer.timeout.connect(self.save)

    @staticmethod
    def _defaults():
        return {"app": {"show_on_start": True, "language": "ja", "ui_color": "", "lock_detail": True,
                        "game_only": True, "game_names": "Minecraft.Windows.exe, javaw.exe",
                        "mic_shortcut": False,
                        "mic_ind_x": MIC_IND_AUTO, "mic_ind_y": MIC_IND_AUTO,   # AUTO = 画面右上 (初期位置)
                        "mic_fade": False, "mic_fade_sec": 2.0,
                        "mic_ind_scale": 100, "mic_border": True,
                        "mic_border_fade": False, "mic_border_fade_sec": 1.5,   # 縁取りだけのフェード           # 表示の大きさ(%) / 赤・緑の枠
                        "mic_sound": True, "mic_sound_vol": 50, "mic_sound_style": 0},            # 切り替え音 / 音量(%)
                "keys": dict(DEFAULT_KEYS),
                "huds": {h: default_hud(h) for h in HUD_ORDER},
                "presets": []}

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                saved = json.load(f)
            self.data["app"].update({k: v for k, v in saved.get("app", {}).items()
                                     if k in self.data["app"]})
            self.data["keys"].update({k: int(v) for k, v in saved.get("keys", {}).items()
                                      if k in DEFAULT_KEYS})
            for h in HUD_ORDER:
                dst = self.data["huds"][h]
                for k, v in saved.get("huds", {}).get(h, {}).items():
                    if k in dst and type(v) == type(dst[k]):
                        dst[k] = v
                    elif k in dst and isinstance(dst[k], (int, float)) and isinstance(v, (int, float)):
                        dst[k] = type(dst[k])(v)
            for pr in saved.get("presets", []):
                cp = clean_preset(pr)
                if cp:
                    self.data["presets"].append(cp)
        except Exception:
            pass

    def save(self):
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except Exception:
            pass

    def hud(self, hid):
        return self.data["huds"][hid]

    @property
    def keys(self):
        return self.data["keys"]

    @property
    def app(self):
        return self.data["app"]

    def set(self, hid, key, value):
        self.data["huds"][hid][key] = value
        self.hud_changed.emit(hid)
        self._timer.start()

    def set_many(self, hid, values):
        """複数の値を1回の通知でまとめて変更する"""
        self.data["huds"][hid].update(values)
        self.hud_changed.emit(hid)
        self._timer.start()

    def set_app(self, key, value):
        self.data["app"][key] = value
        self._timer.start()

    def set_key(self, name, vk):
        self.data["keys"][name] = int(vk)
        self.keys_changed.emit()
        self._timer.start()

    def reset_keys(self, names):
        for n in names:
            self.data["keys"][n] = DEFAULT_KEYS[n]
        self.keys_changed.emit()
        self._timer.start()

    def reset_hud(self, hid, keys):
        d = default_hud(hid)
        for k in keys:
            if k in d:
                self.data["huds"][hid][k] = d[k]
        self.hud_changed.emit(hid)
        self._timer.start()

    # ---- プリセット ----
    def all_presets(self):
        return [dict(p, builtin=False) for p in self.data["presets"]]

    def get_preset(self, pid):
        for p in self.all_presets():
            if p["id"] == pid:
                return p
        return None

    def add_preset(self, name, huds):
        pid = "u%d" % int(time.time() * 1000)
        self.data["presets"].append({"id": pid, "name": name[:40], "huds": huds})
        self._timer.start()
        return pid

    def _user_preset(self, pid):
        for p in self.data["presets"]:
            if p["id"] == pid:
                return p
        return None

    def rename_preset(self, pid, name):
        p = self._user_preset(pid)
        if p:
            p["name"] = name[:40]
            self._timer.start()

    def overwrite_preset(self, pid, huds):
        p = self._user_preset(pid)
        if p:
            p["huds"] = huds
            self._timer.start()

    def delete_preset(self, pid):
        self.data["presets"] = [p for p in self.data["presets"] if p["id"] != pid]
        self._timer.start()

    def apply_preset(self, preset):
        for h in HUD_ORDER:
            vals = {k: v for k, v in preset["huds"].get(h, {}).items() if k in appearance_keys(h)}
            if vals:
                self.set_many(h, vals)

    def reset_all(self):
        presets = self.data["presets"]          # 保存したプリセットは消さない
        self.data = self._defaults()
        self.data["presets"] = presets
        for h in HUD_ORDER:
            self.hud_changed.emit(h)
        self.keys_changed.emit()
        self._timer.start()


APPEARANCE_EXCLUDE = {"enabled", "x", "y", "fps_interval", "fps_target", "ping_interval", "ping_host", "ping_port"}


def appearance_keys(hid):
    """プリセットに含める「見た目」の設定 (位置・ON/OFF・測定設定は含めない)"""
    return [k for k in default_hud(hid) if k not in APPEARANCE_EXCLUDE]


def capture_appearance(cfg):
    return {h: {k: cfg.hud(h)[k] for k in appearance_keys(h)} for h in HUD_ORDER}


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def clean_preset(p):
    try:
        out = {}
        for h in HUD_ORDER:
            src = p.get("huds", {}).get(h, {})
            d = default_hud(h)
            a = {}
            for k in appearance_keys(h):
                if k not in src:
                    continue
                v = src[k]
                if type(v) == type(d[k]):
                    a[k] = v
                elif _num(d[k]) and _num(v):
                    a[k] = type(d[k])(v)
            out[h] = a
        return {"id": str(p["id"]), "name": str(p["name"])[:40], "huds": out}
    except Exception:
        return None


def qcolor(hex_str, opacity_percent):
    c = QColor(hex_str)
    c.setAlphaF(max(0.0, min(1.0, opacity_percent / 100.0)))
    return c



# ======================================================================
#  言語 / アプリの色
# ======================================================================
LANG = "ja"
UI_COLOR = ""      # "" = 初期のネイビー。色を選ぶと設定画面全体の色合いがその色に変わる

EN = {
    # --- サイドバー / 共通 ---
    "▣   表示設定": "▣   HUDs", "◈   プリセット": "◈   Presets", "⚙   システム": "⚙   System",
    "Ctrl + ドラッグ\nでHUDを移動\n\n×で閉じてもHUDは\nタスクトレイで動作中":
        "Ctrl + drag\nto move a HUD\n\nClosing the window keeps\nthe HUDs running in the tray",
    "確認": "Confirm", "設定を開く": "Open settings", "終了": "Quit",
    # --- 表示設定 ---
    "表示設定": "HUDs",
    "カードをクリックで詳細設定  /  ボタン: 青=表示・暗い色=非表示  /  Ctrl+ドラッグでHUDを移動":
        "Click a card for details  /  Button: blue = shown, dark = hidden  /  Ctrl+drag moves a HUD",
    "クリックで詳細設定 ›": "Click for details ›",
    "左クリックと右クリックの毎秒クリック数": "Left / right clicks per second",
    "ゲームの実フレームレート (PresentMon)": "Real game frame rate (PresentMon)",
    "WASD / スペース / マウスボタン": "WASD / Space / mouse buttons",
    "キーストローク": "Keystrokes",
    "‹  戻る": "‹  Back", "プレビュー表示中": "Previewing", "表示": "Show",
    "変更は自動で保存されます。プレビューはこのウィンドウの横に表示されます。":
        "Changes are saved automatically. The preview appears next to this window.",
    "このタブを初期値に戻す": "Reset this tab",
    "色を選択": "Choose a color",
    "全体": "General", "全体の不透明度": "Overall opacity", "HUD全体の透け具合": "How see-through the whole HUD is",
    "大きさ": "Size", "HUD全体の拡大・縮小": "Scale of the whole HUD",
    "位置 (画面左上からのpx)": "Position (px from top-left of screen)",
    "X 位置": "X position", "Y 位置": "Y position", "Ctrl+ドラッグでも移動できます": "You can also Ctrl+drag",
    "文字": "Text", "文字の色": "Text color", "文字の不透明度": "Text opacity",
    "文字の大きさ": "Text size", "文字全体の大きさ (幅・高さ同時)": "Overall text size (width and height together)",
    "文字の幅の比": "Character width ratio", "1文字ごとの横幅。大きいほど横に広い字になります":
        "Width of each character. Larger = wider letters",
    "文字の高さの比": "Character height ratio", "1文字ごとの縦の長さ。大きいほど縦に長い字になります":
        "Height of each character. Larger = taller letters",
    "文字の上下位置": "Text vertical offset", "枠の中で文字を上(−)/下(+)へ動かします":
        "Move the text up (−) / down (+) inside the box",
    "字間": "Letter spacing", "文字と文字の間隔 (0%が標準。\"LMB\"のような複数文字の表示に効きます)":
        "Space between letters (0% = normal; affects multi-letter labels like \"LMB\")",
    "背景": "Background", "背景を表示": "Show background", "背景の色": "Background color",
    "背景の不透明度": "Background opacity", "形": "Shape",
    "横の比": "Width ratio", "枠・背景の横幅の比率 (文字の大きさは変わりません)":
        "Width ratio of the box/background (text size is unchanged)",
    "縦の比": "Height ratio", "枠・背景の縦幅の比率": "Height ratio of the box/background",
    "角の丸み": "Corner radius",
    "枠": "Border", "枠を表示": "Show border", "背景とは独立して設定できます": "Set independently of the background",
    "枠の色": "Border color", "枠の太さ": "Border width", "枠の不透明度": "Border opacity",
    "ぼかし (外側へ向かって薄くなる光)": "Blur (glow fading outward)", "枠をぼかす": "Blur the border",
    "ぼかしの広がり": "Blur spread",
    "FPS測定": "FPS measurement",
    "Ping測定": "Ping measurement",
    "サーバー(ホスト)までの応答時間 (ms)": "Response time to a server / host (ms)",
    "数字のみ   [32]": "Number only   [32]", "単位付き   [32ms]": "With unit   [32ms]",
    "ラベル付き   [PING:32ms]": "With label   [PING:32ms]",
    "1.0秒": "1.0 s", "2.0秒": "2.0 s", "5.0秒": "5.0 s",
    "測定先のアドレス": "Target address", "ポート (任意)": "Port (optional)", "自動": "Auto",
    "サーバーのアドレス (IPアドレスかホスト名)。ポート番号だけでは測れません。「play.example.net:19132」のように続けて書いてもOKです。":
        "The server address (IP or host name). A port number alone can't be measured. "
        "You can also write it as \"play.example.net:19132\".",
    "「自動」ならpingで測ります。統合版サーバーは 19132 を入れると、サーバー自身の応答時間を測れます。":
        "\"Auto\" measures with ping. For Bedrock servers enter 19132 to measure the server's own response time.",
    "例: play.example.net  /  1.1.1.1": "e.g. play.example.net  /  1.1.1.1",
    "アドレス欄が数字だけです。サーバーのアドレスを入れ、ポート番号は下の「ポート」欄に入れてください":
        "The address box has only digits. Enter the server address, and put the port in the Port box below",
    "測定待ち…": "Waiting for a reply…",
    "測定先「%s」を読み取れません (例: 1.1.1.1 / example.com / example.com:19132)":
        "Can't read the target \"%s\" (e.g. 1.1.1.1 / example.com / example.com:19132)",
    "測定先が空です (例: 1.1.1.1 / example.com / example.com:19132)":
        "The target is empty (e.g. 1.1.1.1 / example.com / example.com:19132)",
    "%s  →  %d ms  (%s)": "%s  →  %d ms  (%s)",
    "%s  →  応答なし  (試した方法: %s)": "%s  →  no reply  (tried: %s)",
    "測定エラー: %s": "Measurement error: %s",
    "右/左クリックを表示": "Show left/right click", "キーストロークの下に表示します": "Shown below the keys",
    "押したときの色": "Pressed color", "押したときの不透明度": "Pressed opacity",
    "キー割り当て": "Key bindings",
    "ゲームでキー設定を変えている場合に、実際のキーに合わせます。":
        "Match the keys you actually use in the game. ",
    "表示(W/A/S/D)は変わらず、検出するキーだけが変わります。":
        "The display (W/A/S/D) stays the same; only the detected key changes.",
    "クリック割り当て": "Click bindings",
    "ゲームでマウスボタンの設定を変えている場合に合わせます。": "Match the mouse buttons you use in the game. ",
    "表示は 左クリック-右クリック の順です。": "Shown as left-right.",
    "表記": "Format", "数字のみ   [140]": "Number only   [140]", "ラベル付き   [FPS:140]": "With label   [FPS:140]",
    "更新間隔": "Update interval", "1.0秒 (マイクラのF3と同じ)": "1.0 s (same as Minecraft F3)",
    "0.5秒": "0.5 s", "0.25秒": "0.25 s", "0.1秒": "0.1 s",
    "マイクラは1秒ごとに数字を更新します。同じ数字に揃えたい場合は 1.0秒 のままに。":
        "Minecraft updates its number once a second. Keep 1.0 s to match it.",
    "測定": "Measurement", "状態": "Status", "空欄 = 最前面のゲーム (自動)": "Empty = foreground game (auto)",
    "測定するプロセス名": "Process to measure", "通常は空欄でOK。複数起動している場合だけ javaw.exe などを指定。":
        "Normally leave empty. Specify e.g. javaw.exe only if several games run.",
    "前進": "Forward", "後進": "Back", "左移動": "Left", "右移動": "Right", "ジャンプ": "Jump",
    "左クリック": "Left click", "右クリック": "Right click", "通常": "Default",
    "マウス左": "Mouse L", "マウス右": "Mouse R", "マウス中": "Mouse M", "マウスX1": "Mouse X1", "マウスX2": "Mouse X2",
    "キー割り当て ": "Key binding",
    "「%s」に割り当てる\nキー(またはマウスボタン)を押してください": "Press the key (or mouse button)\nto assign to \"%s\"",
    "Esc でキャンセル": "Esc to cancel",
    # --- v2.9: ダッシュ/スプリント・FPSの追加表示・便利ショートカット ---
    "ダッシュ": "Dash", "スプリント": "Sprint",
    "ダッシュを表示": "Show Dash",
    "スペースの下に表示します。割り当てたキーが押されているかどうかを表示します":
        "Shown below the space bar. It shows whether the assigned key is pressed",
    "スプリントを表示": "Show Sprint",
    "ダッシュと同じ段に並びます。割り当てたキーが押されているかどうかを表示します":
        "Shown on the same row as Dash. It shows whether the assigned key is pressed",
    "ゲームでキー設定を変えている場合に、実際のキーに合わせます。表示(W/A/S/D)は変わらず、検出するキーだけが変わります。ダッシュは初期でShift、スプリントは初期でTabです。":
        "Match the keys you actually use in the game. The display (W/A/S/D) stays the same; only the detected key changes. "
        "Dash is Shift and Sprint is Tab by default.",
    "追加の表示": "Extra readouts",
    "1% Low を表示": "Show 1% Low",
    "FPSの下に表示します。直近60秒で遅かった方から1%のフレームの平均をFPSにした値です":
        "Shown below the FPS. The average of the slowest 1% of frames in the last 60 s, as FPS",
    "0.1% Low を表示": "Show 0.1% Low",
    "直近60秒で遅かった方から0.1%のフレームの平均。1000フレームほどたまるまでは「--」です":
        "The average of the slowest 0.1% of frames in the last 60 s. Shows \"--\" until about 1000 frames are collected",
    "平均FPSを表示": "Show average FPS", "直近60秒の平均です": "Average over the last 60 s",
    "便利ショートカット": "Handy shortcuts", "マイクのミュート切り替え など": "Mic mute toggle, etc.",
    "マイクミュートのショートカット (Ctrl+Shift+M)": "Mic mute shortcut (Ctrl+Shift+M)",
    "ONにすると、どのアプリが前面でも Ctrl+Shift+M を押すたびにマイクのミュート/解除を切り替えます。対象はWindowsの「既定の録音デバイス」です。":
        "When ON, every Ctrl+Shift+M press toggles mic mute, whichever app is in front. "
        "It targets Windows' default recording device.",
    "ミュート表示 (画面右上の🎙●)": "Mute indicator (🎙● at top right)",
    "切り替え時にだんだん消す (フェードアウト)": "Fade out after toggling",
    "ミュート=赤●、解除=緑●が画面右上に出ます (Ctrl+ドラッグで移動)。ONにすると、切り替えた後にだんだん消えます。OFFなら出しっぱなしです。":
        "Muted = red ●, unmuted = green ● at the top right (Ctrl+drag to move). When ON, it fades away after each toggle; when OFF it stays.",
    "フェードアウトの秒数": "Fade-out duration", " 秒": " s",
    "表示してから消えるまでにかける時間です (最初の約0.6秒は消え始めません)。":
        "How long the fade takes (it holds fully visible for the first ~0.6 s).",
    "赤・緑の枠と縁取り": "Red / green frame and glow",
    "ミュート=赤、解除=緑の枠と、外側のぼやけた縁取りを付けます。": "Adds a red (muted) / green (unmuted) frame with a soft glow.",
    "便利ショートカットキー": "Handy shortcut keys", "マイクミュート": "Mic mute",
    "Ctrl+Shift+M / 状態 / 表示の詳細設定": "Ctrl+Shift+M / status / indicator details",
    "ミュート表示 ― 見た目": "Mute indicator - Look", "大きさ・赤/緑の枠・位置": "Size, red/green frame, position",
    "ミュート表示 ― フェード": "Mute indicator - Fade", "全体 / 縁取りだけ消す設定": "Fade the whole thing / only the frame",
    "ミュート表示 ― 音": "Mute indicator - Sound", "切り替え音の種類・音量": "Sound type and volume",
    "大きさ・枠": "Size and frame", "位置 (画面左上からのpx)": "Position (px from top-left)",
    "X 位置": "X position", "Y 位置": "Y position", "初期位置": "Default position",
    "Ctrl+ドラッグでも動かせます (見えているときだけ)。": "Ctrl+drag also works (only while it is visible).",
    "初期の画面右上に戻します。": "Moves it back to the top right.",
    "全体のフェードアウト": "Fade out everything", "縁取りだけのフェードアウト": "Fade out only the frame",
    "切り替え時にだんだん消す": "Fade out after toggling",
    "ONにすると、切り替えた後に表示全体がだんだん消えます。OFFなら出しっぱなしです。":
        "When ON, the whole indicator fades away after each toggle. When OFF it stays.",
    "消えるまでにかける時間です (最初の約0.6秒は消え始めません)。": "How long the fade takes (holds for the first ~0.6 s).",
    "音の種類": "Sound type", "チャイム": "Chime", "ポップ": "Pop", "ピコ": "Beep", "ソフト": "Soft", "レトロ": "Retro", "クリック": "Click",
    "チャイム=やわらかい2音 / ポップ=ぽよんと1回 / ピコ=短い2連 / ソフト=低めでやさしい2音 / レトロ=ゲーム機風の3音 / クリック=ごく短いカチッ。": "Chime = soft two notes / Pop = one bloop / Beep = two short beeps / Soft = low gentle notes / Retro = 3-note game-console style / Click = a tiny click.",
    "試し聞きにも反映されます。": "Also applies to the test sounds.", "試し聞き": "Test sounds",
    "ミュートの音を聞く": "Play mute sound", "解除の音を聞く": "Play unmute sound",
    "縁取りだけフェードアウト": "Fade out only the frame",
    "ONにすると、切り替え後に枠と縁取りだけがだんだん消えます (🎙●は残ります)。":
        "When ON, only the frame and glow fade after each toggle (🎙● stays).",
    "縁取りのフェード秒数": "Frame fade duration",
    "枠が消えるまでにかける時間です (最初の約0.6秒は消え始めません)。":
        "How long the frame takes to fade (holds fully for the first ~0.6 s).",
    "表示の大きさ": "Size", "100%が標準です。": "100% is the default size.",
    "切り替え音": "Toggle sound", "ミュートで下がる音、解除で上がる音を鳴らします。":
        "Plays a falling tone on mute and a rising tone on unmute.",
    "音量": "Volume", "変えたあと、ミュートを切り替えると反映されます。": "Applies the next time you toggle mute.",
    "表示位置": "Position", "表示位置を右上に戻す": "Reset position to top right",
    "Ctrl+ドラッグで動かした位置を、初期の画面右上に戻します。":
        "Moves the indicator back to its default spot at the top right.",
    "マイクの状態": "Mic status",
    "この画面を開いている間、Windows側の状態に合わせて更新されます。":
        "Updated from Windows' state while this page is open.",
    "ミュート中": "Muted", "ミュートしていません": "Not muted", "確認中…": "Checking…",
    "マイクが見つかりません": "No microphone found", "取得できません (%s)": "Unavailable (%s)",
    # --- プリセット ---
    "プリセット": "Presets",
    "見た目(色・文字・背景・枠など)を保存して切り替え  /  カードを選ぶとプレビュー":
        "Save and switch looks (color, text, background, border…)  /  Select a card to preview",
    "＋  今の見た目を保存": "＋  Save current look", "保存したプリセット": "Saved preset",
    "適用": "Apply", "プレビュー解除": "Cancel preview", "名前変更": "Rename",
    "今の見た目で上書き": "Overwrite with current look", "削除": "Delete",
    "カードを選ぶと、画面の横にプレビューが出ます": "Select a card to preview it on your HUDs",
    "まだプリセットがありません。HUDを好みの見た目にして「＋ 今の見た目を保存」を押してください":
        "No presets yet. Style your HUDs, then press \"＋ Save current look\".",
    "選択中:  ": "Selected:  ", "プリセットの名前:": "Preset name:", "プリセットとして保存": "Save as preset",
    "「%s」を保存しました": "Saved \"%s\"",
    "「%s」を適用しました (位置・ON/OFFは変わりません)": "Applied \"%s\" (position and ON/OFF unchanged)",
    "「%s」を、今のHUDの見た目で上書きします。よろしいですか?": "Overwrite \"%s\" with the current HUD look?",
    "プリセット「%s」を削除します。よろしいですか?": "Delete preset \"%s\"?",
    "NavyHUDが最前面でなくなったため、プレビューを解除しました":
        "NavyHUD is no longer in front, so the preview was cancelled",
    # --- システム ---
    "システム": "System", "項目を選ぶと詳細が開きます": "Select an item to open its settings",
    "起動": "Startup", "起動時の動作": "What happens when NavyHUD starts",
    "言語設定": "Language", "設定画面の表示言語": "Display language of this window",
    "アプリの色": "App color", "設定画面全体の色合い": "Color tone of this window",
    "FPS測定エンジン": "FPS engine", "PresentMonの状態と診断ログ": "PresentMon status and diagnostic log",
    "データ": "Data", "設定フォルダと初期化": "Settings folder and reset",
    "起動時にこの設定画面を開く": "Open this window on startup",
    "OFFにするとタスクトレイに入ったまま起動します": "When OFF, NavyHUD starts minimized to the tray",
    "詳細設定": "Detail settings",
    "詳細設定中は他のウィンドウに切り替えさせない": "Don't let other windows take focus while in detail settings",
    "詳細設定(プレビュー表示)を開いている間は、他のウィンドウを選んでも音が鳴って設定画面に戻ります。「‹ 戻る」で解除されます。":
        "While detail settings (preview) are open, selecting another window plays a sound and returns to this window. Press \"‹ Back\" to release.",
    "表示するタイミング": "When to show",
    "ゲームが最前面のときだけHUDを出す など": "Show HUDs only while the game is in front, etc.",
    "ゲームが最前面のときだけHUDを表示": "Show HUDs only while the game is in front",
    "ONにすると、下の「対象のゲーム」が画面の一番手前にあるときだけHUDを出します。NavyHUDの設定画面を開いている間は常に表示されます。":
        "When ON, HUDs appear only while one of the games below is the front window. They are always shown while NavyHUD's own window is in front.",
    "対象のゲーム (プロセス名)": "Games (process names)",
    "カンマ(,)で区切って複数指定できます。統合版は Minecraft.Windows.exe、Java版は javaw.exe です。":
        "Separate several with commas. Bedrock is Minecraft.Windows.exe, Java is javaw.exe.",
    "対象のゲームに追加": "Add to games",
    "直前に最前面だったアプリ": "Last app that was in front",
    "ゲームを前面にしてからNavyHUDに戻ると、そのゲームの名前がここに出ます。":
        "Bring the game to the front, then come back to NavyHUD: its name appears here.",
    "HUDが出ないとき": "If the HUD doesn't appear",
    "上の名前がゲームなら、このボタンで対象に加えられます。": "If the name above is the game, add it with this button.",
    "サーバーのテンプレート": "Server templates", "統合版サーバー": "Bedrock servers",
    "押すと下の「測定先のアドレス」と「ポート」に自動で入ります。ほかのサーバーは下に直接入力してください。":
        "Press one to fill in the address and port below. Type other servers directly below.",
    "言語": "Language", "設定画面で使う言語を選びます (HUDの表示は変わりません)":
        "Choose the language of this window (HUD display is unaffected)",
    "色合い": "Tone", "アプリの色を選ぶ": "Pick a color",
    "設定画面全体の色合いを変えます (HUDの見た目には影響しません)":
        "Changes the color tone of this window (HUDs are unaffected)",
    "好きな色を選ぶ…": "Custom color…", "初期の色(ネイビー)に戻す": "Back to default (navy)",
    "FPS測定エンジン (PresentMon)": "FPS engine (PresentMon)",
    "診断ログを開く": "Open diagnostic log", "診断ログ": "Diagnostic log",
    "FPSが測れないとき、原因がここに記録されます": "If FPS can't be measured, the cause is logged here",
    "管理者として実行中": "Running as administrator", "管理者ではありません": "Not running as administrator",
    "権限": "Privileges", "FPS測定には管理者権限が必要です": "FPS measurement needs administrator rights",
    "設定フォルダを開く": "Open settings folder", "設定ファイルの場所": "Settings file location",
    "すべて初期化": "Reset everything", "すべての設定を初期値に戻す": "Reset all settings",
    "HUDの見た目・位置・キー割り当てが初期化されます (保存したプリセットは残ります)":
        "HUD look, position and key bindings are reset (saved presets are kept)",
    "すべての設定を初期値に戻します。よろしいですか?": "Reset all settings to their defaults?",
    # --- FPS状態 ---
    "測定エンジンを起動中…": "Starting the measurement engine…", "測定中": "Measuring",
    "管理者権限が必要です (NavyHUD.exe を管理者で実行)": "Administrator rights required (run NavyHUD.exe as administrator)",
    "PresentMon が見つかりません (build.bat で同梱されます)": "PresentMon not found (bundled by build.bat)",
    "PresentMon をダウンロード中…": "Downloading PresentMon…",
    "測定エンジンの起動に失敗。再試行中…": "Failed to start the engine. Retrying…",
    "PresentMon の出力形式が未対応です": "Unsupported PresentMon output format",
    "PresentMonが終了しました (終了コード %s) %s": "PresentMon exited (exit code %s) %s", "起動エラー: %s": "Launch error: %s",
    "「%s」の描画を待機中 (受信 %d 行)": "Waiting for \"%s\" to render (%d rows received)",
    "最前面のゲームの描画を待機中 (受信 %d 行)": "Waiting for the foreground game to render (%d rows received)",
    "%s (PID %d) 計測待ち": "%s (PID %d) waiting for frames",
}


def T(s):
    """現在の言語に翻訳 (辞書に無い文字列はそのまま)"""
    return EN.get(s, s) if LANG == "en" else s


def set_lang(code):
    global LANG
    LANG = "en" if code == "en" else "ja"


def set_ui_color(hex_str):
    global UI_COLOR
    UI_COLOR = hex_str if (hex_str and QColor(hex_str).isValid()) else ""


_HEX_RE = re.compile(r"#[0-9a-fA-F]{6}\b")


def _theme_one(hex_str):
    """青系の色だけを、選んだ色の色相に付け替える (赤の削除ボタン・白・黒・灰色はそのまま)"""
    if not UI_COLOR:
        return hex_str
    c = QColor(hex_str)
    h, l, s = colorsys.rgb_to_hls(c.redF(), c.greenF(), c.blueF())
    if s < 0.05 or not (0.5 <= h <= 0.72):
        return hex_str
    t = QColor(UI_COLOR)
    th, _tl, ts = colorsys.rgb_to_hls(t.redF(), t.greenF(), t.blueF())
    scale = 0.0 if ts < 0.12 else min(1.0, ts / 0.45)        # 灰色を選んだらモノトーンに
    r, g, b = colorsys.hls_to_rgb(th, l, s * scale)
    return "#%02x%02x%02x" % (int(round(r * 255)), int(round(g * 255)), int(round(b * 255)))


def theme_hex(hex_str):
    return _theme_one(hex_str)


def themed(qss):
    return _HEX_RE.sub(lambda m: _theme_one(m.group(0)), qss) if UI_COLOR else qss


def retranslate(root):
    """root 以下の文字を現在の言語にする。元の日本語は ja_src に覚えておき、いつでも切り替えられる。
    辞書に無い文字(ユーザーが付けた名前や数字)と、プログラムが随時書き換える欄(dyn)は触らない。"""
    def fix(w, get, put):
        src = w.property("ja_src")
        if src is None:
            cur = get()
            if cur not in EN:
                return
            w.setProperty("ja_src", cur)
            src = cur
        put(T(src))
    for w in [root] + root.findChildren(QWidget):
        if w.property("dyn"):
            continue
        if isinstance(w, QTabBar):
            for i in range(w.count()):
                src = w.tabData(i)
                if src is None:
                    if w.tabText(i) not in EN:
                        continue
                    src = w.tabText(i)
                    w.setTabData(i, src)
                w.setTabText(i, T(src))
        elif isinstance(w, QComboBox):
            for i in range(w.count()):
                src = w.itemData(i, Qt.UserRole + 1)
                if src is None:
                    if w.itemText(i) not in EN:
                        continue
                    src = w.itemText(i)
                    w.setItemData(i, src, Qt.UserRole + 1)
                w.setItemText(i, T(src))
        elif isinstance(w, QLineEdit):
            fix(w, w.placeholderText, w.setPlaceholderText)
        elif isinstance(w, (QLabel, QAbstractButton)):
            fix(w, w.text, w.setText)


# ======================================================================
#  HUD ウィンドウ
# ======================================================================
class State:
    def __init__(self):
        self.down = {k: False for k in DEFAULT_KEYS}
        self.cps = (0, 0)
        self.fps = None     # None = 測定できていない
        self.fps_extra = (None, None, None)     # (1% Low, 0.1% Low, 平均) None = 測定できていない
        self.ping = None    # None = 測定できていない (ms)


class GuideOverlay(QWidget):
    """Ctrl+ドラッグ中だけ出るX線(横線)/Y線(縦線)。
    線はモニターの中央(十字)に固定。HUDの中心が線に近づくと吸着する。"""

    def __init__(self):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint |
                         Qt.Tool | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.cx = 0.0           # Y線(縦線)の画面X座標
        self.cy = 0.0           # X線(横線)の画面Y座標
        self.lock_x = False     # HUDの中心がY線(縦線)に吸着中
        self.lock_y = False     # HUDの中心がX線(横線)に吸着中
        self._screen = None

    def set_screen(self, screen):
        """線を引くモニターを切り替える (そのモニターの中央に線を固定)"""
        if screen is None:      # モニターの境目などで取れないときは今のまま
            if self._screen is not None:
                return
            screen = QGuiApplication.primaryScreen()
        if screen is self._screen:
            return
        self._screen = screen
        g = screen.geometry()
        self.setGeometry(g)
        self.cx = g.x() + g.width() / 2.0
        self.cy = g.y() + g.height() / 2.0
        self.update()

    def begin(self, screen):
        self._screen = None
        self.set_screen(screen)
        self.lock_x = self.lock_y = False
        self.show()
        set_click_through(self.winId(), True)
        self.update()

    def set_lock(self, lx, ly):
        if (lx, ly) != (self.lock_x, self.lock_y):
            self.lock_x, self.lock_y = lx, ly
            self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        lx = int(round(self.cx - self.x()))
        ly = int(round(self.cy - self.y()))
        for vertical, locked in ((True, self.lock_x), (False, self.lock_y)):
            if locked:
                pen = QPen(QColor(90, 175, 255, 255), 2, Qt.SolidLine)
            else:
                pen = QPen(QColor(120, 170, 255, 110), 1, Qt.DashLine)
            p.setPen(pen)
            if vertical:
                p.drawLine(lx, 0, lx, self.height())
            else:
                p.drawLine(0, ly, self.width(), ly)


class HudWindow(QWidget):
    HID = ""

    def __init__(self, cfg, state, guide):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint |
                         Qt.Tool | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.cfg = cfg
        self.state = state
        self.guide = guide
        self.override = None        # プリセットのプレビュー用: 設定を借りずにこの値で描く
        self.preview = False
        self.follow_saved = False   # True: プレビュー中でも本来の位置(保存位置)に表示する
        self._drag = False
        self._sig = None
        self._m = 2
        self._start_cursor = QPoint()
        self._start_pos = QPoint()
        self.apply_config()

    # ---- 設定 ----
    def c(self):
        return self.override if self.override is not None else self.cfg.hud(self.HID)

    def margin(self, c):
        """枠・ぼかしがはみ出す分の余白 (HUD本体の座標系)"""
        m = 2
        if c["border_on"]:
            m += int(math.ceil(c["border_width"] / 2.0))
            if c["border_blur"]:
                m += int(c["border_blur_size"])
        return m

    def apply_config(self):
        c = self.c()
        w, h = self.base_size(c)
        self._m = self.margin(c)
        s = c["scale"] / 100.0
        self.setFixedSize(max(1, int(round((w + 2 * self._m) * s))),
                          max(1, int(round((h + 2 * self._m) * s))))
        self._sig = None
        if not self._drag and (not self.preview or self.follow_saved):
            self.move_to_saved()
        self.update()

    def move_to_saved(self):
        c = self.c()
        off = int(round(self._m * c["scale"] / 100.0))
        geo = QGuiApplication.primaryScreen().virtualGeometry()
        x = min(max(int(c["x"]), geo.left() - 20), geo.right() - 40)
        y = min(max(int(c["y"]), geo.top()), geo.bottom() - 40)
        self.move(x - off, y - off)

    # ---- 状態 ----
    def refresh_if_needed(self):
        sig = self.signature()
        if sig != self._sig:
            self._sig = sig
            self.update()

    def set_interactive(self, ctrl_held):
        """Ctrl押下中だけマウスを受け取る (プレビュー中は常に素通し)"""
        if self._drag:
            return
        interactive = ctrl_held and not self.preview
        set_click_through(self.winId(), not interactive)
        self.setCursor(Qt.SizeAllCursor if interactive else Qt.ArrowCursor)

    # ---- ドラッグ移動 ----
    def mousePressEvent(self, e):
        if e.button() != Qt.LeftButton or self.preview:
            return
        self._drag = True
        self._start_cursor = e.globalPosition().toPoint()
        self._start_pos = self.pos()
        self.guide.begin(QGuiApplication.screenAt(self._start_cursor))
        self.raise_()

    def mouseMoveEvent(self, e):
        if not self._drag:
            return
        cur = e.globalPosition().toPoint()
        self.guide.set_screen(QGuiApplication.screenAt(cur))
        d = cur - self._start_cursor
        nx = self._start_pos.x() + d.x()       # カーソルに追従した位置
        ny = self._start_pos.y() + d.y()
        w2, h2 = self.width() / 2.0, self.height() / 2.0
        # HUDの中心が固定線の近くに来たら、中心を線に合わせる (離れればカーソルに戻る)
        lock_x = abs(nx + w2 - self.guide.cx) < SNAP_PX
        lock_y = abs(ny + h2 - self.guide.cy) < SNAP_PX
        if lock_x:
            nx = int(round(self.guide.cx - w2))
        if lock_y:
            ny = int(round(self.guide.cy - h2))
        self.move(nx, ny)
        self.guide.set_lock(lock_x, lock_y)

    def mouseReleaseEvent(self, e):
        if e.button() != Qt.LeftButton or not self._drag:
            return
        self._drag = False
        self.guide.hide()
        off = int(round(self._m * self.c()["scale"] / 100.0))
        # 先に両方の座標を確定させる。cfg.set(x)の直後にHUDが保存済み位置へ
        # 戻されるため、self.y()を後から読むと移動前のYになってしまう。
        nx, ny = self.x() + off, self.y() + off
        self.cfg.set_many(self.HID, {"x": nx, "y": ny})   # 1回で確定 (途中で別位置へ動かない)
        self.set_interactive(key_down(VK_CONTROL))

    # ---- 描画 ----
    def paintEvent(self, _):
        c = self.c()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.TextAntialiasing, True)
        p.setOpacity(c["opacity"] / 100.0)
        s = c["scale"] / 100.0
        p.scale(s, s)
        p.translate(self._m, self._m)
        self.draw(p, c)

    def box(self, p, c, rect, label, pressed=False, px=16):
        r = QRectF(rect)
        radius = float(c["corner"])
        bw = float(c["border_width"])
        p.setBrush(Qt.NoBrush)

        # 1) 外側へ向かってぼやける枠 (グロー)
        if c["border_on"]:
            base = qcolor(c["border_color"], c["border_opacity"])
            if c["border_blur"] and c["border_blur_size"] > 0:
                n = int(c["border_blur_size"])
                for i in range(n, 0, -1):
                    t = i / float(n + 1)
                    col = QColor(base)
                    col.setAlphaF(base.alphaF() * ((1.0 - t) ** 2) * 0.65)
                    p.setPen(QPen(col, 1.3))
                    off = bw / 2.0 + i - 0.5
                    p.drawRoundedRect(r.adjusted(-off, -off, off, off), radius + off, radius + off)

        # 2) 背景 (押下中は押下色)
        fill = None
        if pressed:
            fill = qcolor(c.get("pressed_color", "#7a7a7a"), c.get("pressed_opacity", 85))
        elif c["bg_on"]:
            fill = qcolor(c["bg_color"], c["bg_opacity"])
        if fill is not None:
            p.setPen(Qt.NoPen)
            p.setBrush(fill)
            p.drawRoundedRect(r, radius, radius)
            p.setBrush(Qt.NoBrush)

        # 3) 枠本体
        if c["border_on"]:
            p.setPen(QPen(qcolor(c["border_color"], c["border_opacity"]), bw))
            p.drawRoundedRect(r, radius, radius)

        # 4) 文字
        f = QFont("Segoe UI")
        f.setPixelSize(max(6, int(round(px * c["text_size"] / 100.0))))
        f.setBold(True)
        sp = c.get("text_spacing", 0)
        if sp:
            f.setLetterSpacing(QFont.PercentageSpacing, max(10, 100 + sp))   # 字間 (100%が標準)
        p.setFont(f)
        p.setPen(qcolor(c["text_color"], c["text_opacity"]))
        sw = max(0.1, c.get("text_w", 100) / 100.0)     # 1文字ごとの横の比
        sh = max(0.1, c.get("text_h", 100) / 100.0)     # 1文字ごとの縦の比
        p.save()
        p.translate(r.center() + QPointF(0, c.get("text_y", 0)))   # 文字の上下位置
        p.scale(sw, sh)
        p.drawText(QRectF(-r.width() / 2.0 / sw, -r.height() / 2.0 / sh,
                          r.width() / sw, r.height() / sh), Qt.AlignCenter, label)
        p.restore()

    # サブクラスで実装
    def base_size(self, c):
        return 80, 36

    def draw(self, p, c):
        pass

    def signature(self):
        return None


class KeystrokeHud(HudWindow):
    HID = "keystrokes"
    K, G, SPACE_H, MOUSE_H = 44, 4, 24, 32

    def dims(self, c):
        rx, ry = c["bg_w"] / 100.0, c["bg_h"] / 100.0
        return self.K * rx, self.K * ry, self.SPACE_H * ry, self.MOUSE_H * ry

    def base_size(self, c):
        kw, kh, sh, mh = self.dims(c)
        w = 3 * kw + 2 * self.G
        h = 2 * kh + 2 * self.G + sh
        if c["show_dash"] or c["show_sprint"]:
            h += self.G + mh
        if c["show_mouse"]:
            h += self.G + mh
        return w, h

    def draw(self, p, c):
        G = self.G
        kw, kh, sh, mh = self.dims(c)
        d = self.state.down
        W = 3 * kw + 2 * G
        self.box(p, c, QRectF(kw + G, 0, kw, kh), "W", d["forward"])
        self.box(p, c, QRectF(0, kh + G, kw, kh), "A", d["left"])
        self.box(p, c, QRectF(kw + G, kh + G, kw, kh), "S", d["back"])
        self.box(p, c, QRectF(2 * (kw + G), kh + G, kw, kh), "D", d["right"])
        sy = 2 * (kh + G)
        self.box(p, c, QRectF(0, sy, W, sh), "---", d["jump"], px=12)
        y = sy + sh + G
        dash_on, sprint_on = c["show_dash"], c["show_sprint"]
        if dash_on or sprint_on:        # ダッシュ / スプリント (どちらか片方だけなら横いっぱい)
            if dash_on and sprint_on:
                half = (W - G) / 2.0
                self.box(p, c, QRectF(0, y, half, mh), "DASH", d["dash"], px=12)
                self.box(p, c, QRectF(half + G, y, half, mh), "SPRINT", d["sprint"], px=12)
            elif dash_on:
                self.box(p, c, QRectF(0, y, W, mh), "DASH", d["dash"], px=12)
            else:
                self.box(p, c, QRectF(0, y, W, mh), "SPRINT", d["sprint"], px=12)
            y += mh + G
        if c["show_mouse"]:
            my = y
            half = (W - G) / 2.0
            self.box(p, c, QRectF(0, my, half, mh), "LMB", d["lmb"], px=12)
            self.box(p, c, QRectF(half + G, my, half, mh), "RMB", d["rmb"], px=12)

    def signature(self):
        d = self.state.down
        return tuple(d[k] for k in DEFAULT_KEYS)


class CpsHud(HudWindow):
    HID = "cps"

    def base_size(self, c):
        return 84 * c["bg_w"] / 100.0, 36 * c["bg_h"] / 100.0

    def draw(self, p, c):
        w, h = self.base_size(c)
        l, r = self.state.cps
        self.box(p, c, QRectF(0, 0, w, h), "%d-%d" % (l, r))

    def signature(self):
        return self.state.cps


class FpsHud(HudWindow):
    HID = "fps"
    G, EXTRA_W, EXTRA_H = 4, 112, 28

    def extras(self, c):
        """表示する追加項目 [(ラベル, state.fps_extra の番号)]"""
        out = []
        if c["show_low1"]:
            out.append(("1% LOW", 0))
        if c["show_low01"]:
            out.append(("0.1% LOW", 1))
        if c["show_avg"]:
            out.append(("AVG", 2))
        return out

    def base_size(self, c):
        rx, ry = c["bg_w"] / 100.0, c["bg_h"] / 100.0
        w = (108 if c["fps_label"] else 72) * rx
        h = 36 * ry
        ex = self.extras(c)
        if ex:      # 追加項目があるときは、FPSの枠も同じ幅にそろえる
            w = max(w, self.EXTRA_W * rx)
            h += len(ex) * (self.G + self.EXTRA_H * ry)
        return w, h

    def draw(self, p, c):
        w, h = self.base_size(c)
        ry = c["bg_h"] / 100.0
        v = self.state.fps
        num = "--" if v is None else "%d" % v
        self.box(p, c, QRectF(0, 0, w, 36 * ry), ("FPS:" + num) if c["fps_label"] else num)
        y = 36 * ry + self.G
        vals = self.state.fps_extra
        for label, i in self.extras(c):
            val = vals[i]
            self.box(p, c, QRectF(0, y, w, self.EXTRA_H * ry),
                     "%s:%s" % (label, "--" if val is None else "%d" % val), px=12)
            y += self.EXTRA_H * ry + self.G

    def signature(self):
        return (self.state.fps, self.state.fps_extra)


class PingHud(HudWindow):
    HID = "ping"

    def base_size(self, c):
        fmt = c["ping_format"]
        return (66 if fmt == 0 else 84 if fmt == 1 else 126) * c["bg_w"] / 100.0, 36 * c["bg_h"] / 100.0

    def draw(self, p, c):
        w, h = self.base_size(c)
        v = self.state.ping
        num = "--" if v is None else "%d" % v
        fmt = c["ping_format"]
        if fmt == 0:
            label = num
        elif fmt == 1:
            label = num if v is None else num + "ms"
        else:
            label = "PING:" + (num if v is None else num + "ms")
        self.box(p, c, QRectF(0, 0, w, h), label)

    def signature(self):
        return self.state.ping


# ======================================================================
#  コントローラ (入力監視 / FPS / 表示制御)
# ======================================================================
_mic_wavs = {}
MIC_SOUND_STYLES = ["チャイム", "ポップ", "ピコ", "ソフト", "レトロ", "クリック"]


def _mic_segments(rising, style):
    """(開始Hz, 終了Hz, 秒, 減衰, 倍音量, 直前の無音秒) のリスト。ミュート=下がる / 解除=上がる"""
    if style == 3:      # ソフト: 低めのやさしい2音
        a, b = (329.63, 493.88) if rising else (493.88, 329.63)
        return [(a, a, 0.18, 7.0, 0.1, 0.0), (b, b, 0.28, 6.0, 0.1, 0.0)]
    if style == 4:      # レトロ: 角ばった3音 (ファミコン風)
        n = (440.0, 554.37, 659.25)
        n = n if rising else n[::-1]
        return [(f, f, 0.07, 5.0, -1.0, 0.0) for f in n]
    if style == 5:      # クリック: ごく短いカチッ
        return [(1500.0, 1100.0, 0.035, 40.0, 0.0, 0.0)] if rising else [(1000.0, 700.0, 0.035, 40.0, 0.0, 0.0)]
    if style == 1:      # ポップ: ぽよん、と1回
        return [(380.0, 900.0, 0.11, 14.0, 0.15, 0.0)] if rising else [(800.0, 330.0, 0.11, 14.0, 0.15, 0.0)]
    if style == 2:      # ピコ: 短い2連のピコッ
        a, b = (540.0, 760.0) if rising else (760.0, 540.0)
        return [(a, a, 0.07, 9.0, 0.45, 0.0), (b, b, 0.09, 9.0, 0.45, 0.03)]
    a, b = (523.25, 783.99) if rising else (783.99, 523.25)     # チャイム: ド→ソ / ソ→ド (やわらかい2音)
    return [(a, a, 0.14, 12.0, 0.3, 0.0), (b, b, 0.22, 9.0, 0.3, 0.0)]


def _mic_wav(rising, vol, style=0):
    """切り替え音のWAV(メモリ上)。rising=True: 上がる音(解除) / False: 下がる音(ミュート)"""
    key = (bool(rising), int(vol), int(style))
    if key not in _mic_wavs:
        import wave, io
        rate = 22050
        amp = 32767 * 0.55 * max(0, min(100, vol)) / 100.0
        frames = bytearray()
        for f0, f1, dur, decay, harm, gap in _mic_segments(bool(rising), int(style)):
            frames += b"\x00\x00" * int(rate * gap)
            n = int(rate * dur)
            ph = 0.0
            for i in range(n):
                t = i / float(n)
                ph += 2 * math.pi * (f0 + (f1 - f0) * t) / rate
                env = min(1.0, i / 90.0) * math.exp(-decay * t * dur) * min(1.0, (1 - t) * 8)
                if harm < 0:        # 角ばった音 (矩形波に近い)
                    v, norm = (1.0 if math.sin(ph) >= 0 else -1.0) * 0.7, 1.0
                else:
                    v, norm = math.sin(ph) + harm * math.sin(2 * ph), 1 + harm
                frames += struct.pack("<h", int(amp * env * v / norm))
        bio = io.BytesIO()
        with wave.open(bio, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(bytes(frames))
        _mic_wavs[key] = bio.getvalue()
    return _mic_wavs[key]


def play_mic_sound(muted, vol, style=0):
    """SND_MEMORY は SND_ASYNC と併用できない(例外になる)ため、専用スレッドで同期再生する"""
    def run():
        try:
            import winsound
            winsound.PlaySound(_mic_wav(not muted, vol, style), winsound.SND_MEMORY)
        except Exception:
            try:
                import winsound
                winsound.Beep(500 if muted else 1000, 150)      # 失敗時の予備 (単音)
            except Exception:
                pass
    threading.Thread(target=run, name="NavyHUD-micsnd", daemon=True).start()


class MicIndicator(QWidget):
    """マイクのミュート状態表示: 🎙+●(ミュート=赤 / 解除=緑)。既定は画面右上。
    Ctrl+左ドラッグで移動。切り替え時のフェードアウトは設定(mic_fade / mic_fade_sec)に従う。"""
    W, H = 64, 36
    PAD = 8                 # 枠のぼかしがはみ出す余白

    def __init__(self, cfg):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint |
                         Qt.Tool | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.cfg = cfg
        self.muted = None
        self.alpha = 1.0
        self._drag = False
        self._start_cursor = QPoint()
        self._start_pos = QPoint()
        self.scale = 0
        self.border_sig = True
        self.border_alpha = 1.0
        self.apply_scale()

    def apply_scale(self):
        sc = max(30, min(500, int(self.cfg.app.get("mic_ind_scale", 100))))
        if sc == self.scale:
            return
        self.scale = sc
        k = sc / 100.0
        self.setFixedSize(int(round((self.W + 2 * self.PAD) * k)), int(round((self.H + 2 * self.PAD) * k)))
        self.move_to_saved()
        self.update()

    def move_to_saved(self):
        a = self.cfg.app
        x, y = int(a.get("mic_ind_x", MIC_IND_AUTO)), int(a.get("mic_ind_y", MIC_IND_AUTO))
        geo = QGuiApplication.primaryScreen().geometry()
        if x == MIC_IND_AUTO or y == MIC_IND_AUTO:      # 初期位置: 画面右上
            x, y = geo.right() - self.width() - 8, geo.top() + 8
        vg = QGuiApplication.primaryScreen().virtualGeometry()
        x = min(max(x, vg.left() - 20), vg.right() - 40)
        y = min(max(y, vg.top()), vg.bottom() - 40)
        self.move(x, y)

    def set_state(self, muted):
        if muted != self.muted:
            self.muted = muted
            self.update()

    def set_border_alpha(self, a):
        if abs(a - self.border_alpha) > 0.004:
            self.border_alpha = a
            self.update()

    def set_alpha(self, a):
        """全体の透明度。ウィンドウ自体の透明度は触らず、描画側で掛ける (Ctrlでの属性変更で全表示に戻らないように)"""
        a = max(0.0, min(1.0, a))
        if abs(a - self.alpha) > 0.004:
            self.alpha = a
            self.update()

    def set_interactive(self, ctrl_held):
        if self._drag:
            return
        set_click_through(self.winId(), not ctrl_held)
        self.setCursor(Qt.SizeAllCursor if ctrl_held else Qt.ArrowCursor)

    def mousePressEvent(self, e):
        if e.button() != Qt.LeftButton:
            return
        self._drag = True
        self._start_cursor = e.globalPosition().toPoint()
        self._start_pos = self.pos()

    def mouseMoveEvent(self, e):
        if self._drag:
            self.move(self._start_pos + (e.globalPosition().toPoint() - self._start_cursor))

    def mouseReleaseEvent(self, e):
        if e.button() != Qt.LeftButton or not self._drag:
            return
        self._drag = False
        self.cfg.set_app("mic_ind_x", self.x())
        self.cfg.set_app("mic_ind_y", self.y())
        self.set_interactive(key_down(VK_CONTROL))

    def paintEvent(self, _):
        if self.muted is None:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.TextAntialiasing, True)
        p.setOpacity(self.alpha)
        k = self.scale / 100.0
        p.scale(k, k)
        p.translate(self.PAD, self.PAD)
        col = QColor(235, 50, 50) if self.muted else QColor(60, 200, 90)    # 赤=ミュート / 緑=解除
        r = QRectF(0, 0, self.W, self.H)
        bon = self.cfg.app.get("mic_border", True) and self.border_alpha > 0.004
        if bon:
            for i in range(6, 0, -1):                   # 外側へぼやける縁取り
                g = QColor(col)
                g.setAlphaF(0.5 * (1.0 - i / 7.0) ** 2 * self.border_alpha)
                p.setPen(QPen(g, 1.3))
                p.setBrush(Qt.NoBrush)
                o = 1.5 + i - 0.5
                p.drawRoundedRect(r.adjusted(-o, -o, o, o), 8 + o, 8 + o)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 140))                # 背景 (どの画面でも見えるように薄い黒)
        p.drawRoundedRect(r, 8, 8)
        if bon:
            p.setBrush(Qt.NoBrush)
            bc = QColor(col)
            bc.setAlphaF(self.border_alpha)
            p.setPen(QPen(bc, 3))                      # 枠本体
            p.drawRoundedRect(r.adjusted(1.5, 1.5, -1.5, -1.5), 7, 7)
        f = QFont("Segoe UI Emoji")
        f.setPixelSize(20)
        p.setFont(f)
        p.setPen(QColor(255, 255, 255))
        p.drawText(QRectF(6, 0, 30, self.H), Qt.AlignCenter, "\U0001F399")   # 🎙
        p.setPen(Qt.NoPen)
        p.setBrush(col)
        p.drawEllipse(QRectF(40, self.H / 2 - 9, 18, 18))


class Controller(QObject):
    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg
        self.state = State()
        self.pm = PresentMonFps(cfg.folder)
        self.mic = MicMute()
        self.mic_ind = MicIndicator(cfg)
        self._mic_poll = 0.0
        self._mic_seen = None       # マイク表示: 前回見たミュート状態
        self._mic_changed = 0.0     # マイク表示: 最後に状態が変わった時刻 (フェード開始の基準)
        self._mic_ind_ctrl = None
        self._mic_prev = False      # Ctrl+Shift+M を前回の更新で押していたか (押した瞬間だけ反応させる)
        self.fps_text = ""
        self.guide = GuideOverlay()
        self.huds = {
            "cps": CpsHud(cfg, self.state, self.guide),
            "fps": FpsHud(cfg, self.state, self.guide),
            "keystrokes": KeystrokeHud(cfg, self.state, self.guide),
            "ping": PingHud(cfg, self.state, self.guide),
        }
        self.ping = PingMonitor(lambda: (cfg.hud("ping")["ping_host"], cfg.hud("ping")["ping_interval"],
                                         cfg.hud("ping")["ping_port"]))
        self.settings = None
        self.preview_id = None
        self.preset_prev = None     # プリセットのプレビュー中は {hid: 設定} が入る
        self._ctrl = False
        self._lclicks = deque()
        self._rclicks = deque()
        self._last_fg = 0
        self._away_since = None     # プリセットのプレビュー中に NavyHUD が最前面でなくなった時刻
        self._fps_pub = 0.0
        self._fg_check = 0.0
        self._last_beep = 0.0
        self._game_fg = True        # ゲーム(または NavyHUD 自身)が最前面か
        self.last_fg_name = ""      # 直前に最前面だった(NavyHUD以外の)アプリのexe名
        cfg.hud_changed.connect(self.on_hud_changed)
        self.apply_visibility()

        self.focus_timer = QTimer(self)             # 詳細設定中は他の窓に切り替えさせない見張り
        self.focus_timer.setInterval(40)
        self.focus_timer.timeout.connect(self.guard_focus)
        self.focus_timer.start()

        self.timer = QTimer(self)
        self.timer.setTimerType(Qt.PreciseTimer)
        self.timer.setInterval(POLL_MS)
        self.timer.timeout.connect(self.tick)
        self.timer.start()

    def update_mic_indicator(self, now):
        """マイク表示の 表示/非表示・色・フェードを更新する"""
        a = self.cfg.app
        ind = self.mic_ind
        muted = self.mic.muted
        known = bool(a.get("mic_shortcut", False)) and muted is not None
        if not known:
            self._mic_seen = None
        elif muted != self._mic_seen:                   # 切り替わった瞬間 → フェードをやり直す + 音
            if self._mic_seen is not None and a.get("mic_sound", True):     # 音は表示の有無に関係なく鳴らす
                play_mic_sound(muted, int(a.get("mic_sound_vol", 50)), int(a.get("mic_sound_style", 0)))
            self._mic_seen = muted
            self._mic_changed = now
        if not known:                                   # ショートカットOFF(またはマイク状態不明)なら非表示
            if ind.isVisible():
                ind.hide()
            return
        # ゲームが最前面かどうかに関係なく、常に表示する (フェードの設定がONなら、その設定で消える)
        ind.apply_scale()
        ind.set_state(muted)
        if ind.border_sig != bool(a.get("mic_border", True)):
            ind.border_sig = bool(a.get("mic_border", True))
            ind.update()
        if a.get("mic_fade", False):
            sec = max(0.1, float(a.get("mic_fade_sec", 2.0)))
            t = now - self._mic_changed - MIC_IND_HOLD
            alpha = 1.0 if t <= 0 else max(0.0, 1.0 - t / sec)
        else:
            alpha = 1.0
        if a.get("mic_border_fade", False):             # 縁取りだけのフェード (本体は消えない)
            bsec = max(0.1, float(a.get("mic_border_fade_sec", 1.5)))
            bt = now - self._mic_changed - MIC_IND_HOLD
            balpha = 1.0 if bt <= 0 else max(0.0, 1.0 - bt / bsec)
        else:
            balpha = 1.0
        if alpha <= 0.004:                              # 完全に消えたら窓ごと隠す (Ctrlを押しても出ない)
            if ind.isVisible():
                ind.hide()
            self._mic_ind_ctrl = None
            return
        ind.set_alpha(alpha)
        ind.set_border_alpha(balpha)
        if not ind.isVisible():
            ind.show()
            self._mic_ind_ctrl = None
        grab = bool(self._ctrl and alpha > 0.05)        # 見えているときだけCtrl+ドラッグで動かせる
        if self._mic_ind_ctrl != grab:
            self._mic_ind_ctrl = grab
            ind.set_interactive(grab)

    def shutdown(self):
        self.pm.shutdown()
        self.ping.shutdown()

    # ---- 表示制御 ----
    def wanted(self, hid):
        """そのHUDを今表示すべきか (詳細設定のプレビュー中は、そのHUDだけ)"""
        if self.preset_prev is not None:
            return True
        if self.preview_id:
            return hid == self.preview_id
        return bool(self.cfg.hud(hid)["enabled"]) and self._game_fg

    def game_names(self):
        return {_app_key(x) for x in re.split(r"[,、;\s]+", self.cfg.app.get("game_names", "")) if x.strip()}

    def update_game_focus(self, pid):
        """「ゲームが最前面のときだけHUDを表示」の判定。NavyHUD自身が最前面(設定画面など)のときは常に表示"""
        own = (pid == os.getpid())
        name = "" if own else foreground_exe_name()
        if name:
            self.last_fg_name = name
        if own or not self.cfg.app.get("game_only", True):
            self._game_fg = True
        else:
            self._game_fg = bool(pid) and _app_key(name) in self.game_names()

    def sync_visibility(self):
        """表示状態が食い違っていたら直す (どの経路で変わっても常に正しい状態へ)"""
        for hid, hud in self.huds.items():
            want = self.wanted(hid)
            if want and not hud.isVisible():
                hud.show()
                hud.set_interactive(self._ctrl)
            elif not want and hud.isVisible():
                hud.hide()

    def apply_visibility(self):
        for hid, hud in self.huds.items():
            want = self.wanted(hid)
            if want:
                if not hud.isVisible():
                    hud.show()
                hud.set_interactive(self._ctrl)
            elif hud.isVisible():
                hud.hide()

    def on_hud_changed(self, hid):
        self.huds[hid].apply_config()
        self.apply_visibility()
        if hid == self.preview_id:
            self.reposition_preview()

    def set_preview(self, hid):
        self.preview_id = hid
        for h, hud in self.huds.items():
            hud.preview = (h == hid)
            if hid is None:
                hud.move_to_saved()
        self.apply_visibility()
        self.reposition_preview()

    def set_preset_preview(self, merged):
        """merged: {hid: 設定dict} でその見た目を全てのHUDに仮表示。None で解除。
        位置は動かさず、各HUDが本来いる場所(保存位置)にプリセットの見た目で出す。"""
        self.preset_prev = merged
        self._away_since = None
        for h, hud in self.huds.items():
            if merged is not None:
                d = dict(merged[h])
                real = self.cfg.hud(h)
                d["x"], d["y"] = real["x"], real["y"]       # 位置だけは本物の設定のまま
                hud.override = d
                hud.preview = True
                hud.follow_saved = True
            else:
                hud.override = None
                hud.follow_saved = False
                hud.preview = (h == self.preview_id)
            hud.apply_config()
        self.apply_visibility()

    def reposition_preview(self):
        if self.preset_prev is not None:        # プリセットのプレビューは位置を動かさない
            return
        if not self.preview_id or self.settings is None:
            return
        hud = self.huds[self.preview_id]
        g = self.settings.frameGeometry()
        scr = self.settings.screen().availableGeometry()
        w, h, m = hud.width(), hud.height(), 16
        if g.right() + m + w <= scr.right():
            x, y = g.right() + m, g.top()
        elif g.left() - m - w >= scr.left():
            x, y = g.left() - m - w, g.top()
        else:
            x, y = g.left(), min(g.bottom() + m, scr.bottom() - h)
        hud.move(x, y)

    def check_preset_preview(self, now, fg_pid):
        """プリセットを選んだだけ(未適用)で、NavyHUD の画面が最前面でなくなったらプレビューを解除する"""
        if self.preset_prev is None:
            self._away_since = None
            return
        if fg_pid == os.getpid():
            self._away_since = None
            return
        if self._away_since is None:
            self._away_since = now
        elif now - self._away_since > 0.5 and self.settings is not None:   # 一瞬の切り替えは無視
            self.settings.cancel_preset_preview(T("NavyHUDが最前面でなくなったため、プレビューを解除しました"))

    # ---- 詳細設定中のフォーカス固定 ----
    def detail_locked(self):
        st = self.settings
        return bool(st is not None and st.detail is not None and st.isVisible()
                    and self.cfg.app.get("lock_detail", True))

    def guard_focus(self):
        """詳細設定を開いている間、他の窓が最前面になったら音を鳴らして設定画面に戻す"""
        if not self.detail_locked():
            return
        pid, cls = foreground_class()
        if pid == 0 or pid == os.getpid() or cls in FOCUS_OK_CLASSES:     # pid 0 = UAC画面などの特殊な状態
            return
        now = time.monotonic()
        if now - self._last_beep > 0.6:
            self._last_beep = now
            user32.MessageBeep(0)                   # Windowsの標準の警告音 (モーダルでブロックされた時と同じ)
        target = QApplication.activeModalWidget() or self.settings
        bring_to_front(target.winId())

    # ---- ポーリング ----
    def tick(self):
        now = time.perf_counter()
        st = self.state
        for name, vk in self.cfg.keys.items():
            down = key_down(vk)
            if down and not st.down[name]:
                if name == "lmb":
                    self._lclicks.append(now)
                elif name == "rmb":
                    self._rclicks.append(now)
            st.down[name] = down
        cutoff = now - 1.0
        while self._lclicks and self._lclicks[0] < cutoff:
            self._lclicks.popleft()
        while self._rclicks and self._rclicks[0] < cutoff:
            self._rclicks.popleft()
        st.cps = (len(self._lclicks), len(self._rclicks))

        # 最前面プロセス (自分以外) を記憶
        if now - self._fg_check > 0.1:
            self._fg_check = now
            pid = foreground_pid()
            if pid and pid != os.getpid():
                self._last_fg = pid
            self.update_game_focus(pid)
            self.check_preset_preview(now, pid)
            self.sync_visibility()
        # 便利ショートカット: Ctrl+Shift+M でマイクのミュート切り替え (押した瞬間に1回だけ)
        combo = (key_down(0x4D) and key_down(VK_CONTROL) and key_down(0x10)
                 and not key_down(0x12))            # M が押されていないときは1回の確認で終わる
        if combo and not self._mic_prev and self.cfg.app.get("mic_shortcut", False):
            self.mic.toggle()
        if self.cfg.app.get("mic_shortcut", False) and now - self._mic_poll >= 3.0:
            self._mic_poll = now                    # 起動直後と、他の手段でミュートが変わった場合に表示を合わせる
            self.mic.refresh()
        self._mic_prev = combo                      # OFFのときも押下状態は追う (ONにした瞬間の誤作動を防ぐ)

        # FPS: 設定した間隔で更新 (既定 1.0秒 = マイクラのF3と同じ)
        c = self.cfg.hud("fps")
        if now - self._fps_pub >= c["fps_interval"] / 1000.0:
            self._fps_pub = now
            shown = self.huds["fps"].c()        # プリセットのプレビュー中は、その見た目の設定
            want_extra = bool(shown["show_low1"] or shown["show_low01"] or shown["show_avg"]
                              or (self.settings is not None and self.settings.isVisible()))
            st.fps, self.fps_text = self.pm.measure(now, self._last_fg, c["fps_target"], want_extra)
            st.fps_extra = self.pm.extra

        # Ping: 表示中(または設定画面が開いている間)だけ測る
        self.ping.active = bool(self.cfg.hud("ping")["enabled"]) or (self.settings is not None and self.settings.isVisible())
        st.ping = self.ping.value

        ctrl = key_down(VK_CONTROL)
        if ctrl != self._ctrl:
            self._ctrl = ctrl
            for hud in self.huds.values():
                if hud.isVisible():
                    hud.set_interactive(ctrl)

        for hud in self.huds.values():
            if hud.isVisible():
                hud.refresh_if_needed()
        self.update_mic_indicator(now)


# ======================================================================
#  設定画面
# ======================================================================
QSS = """
* { font-family: "Yu Gothic UI", "Segoe UI", sans-serif; font-size: 13px; color: #d6e4ff; }
QMainWindow, QDialog, QMessageBox { background: #070d1c; }
#root, #page, #host, QStackedWidget, QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; }
QScrollArea { border: none; background: transparent; }
QLabel { background: transparent; }
#sidebar { background: #050a16; border-right: 1px solid #14224a; }
#logo { font-size: 18px; font-weight: bold; color: #ffffff; letter-spacing: 1px; }
#logosub { font-size: 10px; color: #5f78b0; letter-spacing: 2px; }
#navtip { color: #6a80ad; font-size: 11px; }
QPushButton#nav { background: transparent; border: none; border-radius: 10px; text-align: left;
                  padding: 11px 16px; font-size: 14px; color: #9db3e0; }
QPushButton#nav:hover { background: #0e1a3a; color: #ffffff; }
QPushButton#nav:checked { background: #14295c; color: #ffffff; border-left: 3px solid #4aa3ff; }
#title { font-size: 26px; font-weight: bold; color: #ffffff; }
#subtitle { color: #7d92bf; font-size: 12px; }
#panel { background: #0d1833; border: 1px solid #1d3163; border-radius: 14px; }
#paneltitle { font-size: 12px; font-weight: bold; color: #6fa8ff; letter-spacing: 1px; }
#rowlabel { font-size: 13px; color: #e8f0ff; }
#rowdesc { font-size: 11px; color: #7083ad; }
#sep { background: #17274f; max-height: 1px; min-height: 1px; border: none; }
#card { background: #0d1833; border: 1px solid #1d3163; border-radius: 16px; }
#card:hover { background: #12214a; border: 1px solid #4a74c9; }
#card[sel="true"] { background: #14295c; border: 1px solid #4aa3ff; }
#thumb { background: #070d1c; border: 1px solid #14224a; border-radius: 10px; }
#cardtitle { font-size: 16px; font-weight: bold; color: #ffffff; }
#cardsub { font-size: 11px; color: #7083ad; }
#badge { background: #14295c; border-radius: 9px; padding: 3px 10px; color: #8fc1ff; font-size: 11px; }
#status { color: #9db3e0; }
QPushButton { background: #14244b; border: 1px solid #28427f; border-radius: 9px;
              padding: 7px 16px; color: #e6efff; }
QPushButton:hover { background: #1d3670; border: 1px solid #4a74c9; }
QPushButton:pressed { background: #2a4a8c; }
QPushButton:disabled { color: #4d5f88; background: #0e1730; border: 1px solid #1a2850; }
QPushButton#pill, QPushButton#pillsm { background: #0f1b3d; border: 1px solid #263f7a; color: #6f86b8;
                   font-weight: bold; padding: 0; }
QPushButton#pill:hover, QPushButton#pillsm:hover { background: #15264f; border: 1px solid #3a5a9f; }
QPushButton#pill:checked, QPushButton#pillsm:checked { background: #2f6bd8; border: 1px solid #5b95f0;
                   color: #ffffff; }
QPushButton#pill:checked:hover, QPushButton#pillsm:checked:hover { background: #3a78e8; border: 1px solid #7aaaf8; }
QPushButton#pill { border-radius: 14px; font-size: 12px; }
QPushButton#pillsm { border-radius: 12px; font-size: 11px; }
QPushButton#bind { text-align: left; padding: 9px 16px; }
QPushButton#tmpl { padding: 7px 14px; min-width: 84px; }
QPushButton#tmpl:checked { background: #2f6bd8; border: 1px solid #5b95f0; color: #ffffff; font-weight: bold; }
QPushButton#danger { background: #3a1620; border: 1px solid #8a2f45; }
QPushButton#danger:hover { background: #55202f; }
QSlider::groove:horizontal { height: 6px; background: #1a2b57; border-radius: 3px; }
QSlider::sub-page:horizontal { background: #3b82f6; border-radius: 3px; }
QSlider::handle:horizontal { background: #cfe3ff; width: 16px; margin: -5px 0; border-radius: 8px; }
QSlider::handle:horizontal:hover { background: #ffffff; }
QSpinBox, QDoubleSpinBox, QLineEdit, QComboBox { background: #0a1329; border: 1px solid #28427f; border-radius: 8px;
                                 padding: 5px 8px; selection-background-color: #2a4a8c; }
QSpinBox:focus, QDoubleSpinBox:focus, QLineEdit:focus, QComboBox:focus { border: 1px solid #4aa3ff; }
QSpinBox::up-button, QSpinBox::down-button, QDoubleSpinBox::up-button, QDoubleSpinBox::down-button { width: 0; border: none; }
QComboBox::drop-down { border: none; width: 22px; }
QComboBox QAbstractItemView { background: #0d1833; border: 1px solid #28427f;
                              selection-background-color: #2a4a8c; outline: none; }
QTabWidget::pane { border: none; }
QTabBar { background: transparent; }
QTabBar::tab { background: transparent; color: #8aa0cf; padding: 9px 20px; margin-right: 4px;
               border-bottom: 2px solid transparent; font-size: 13px; }
QTabBar::tab:hover { color: #ffffff; }
QTabBar::tab:selected { color: #ffffff; border-bottom: 2px solid #4aa3ff; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar::handle:vertical { background: #25417f; border-radius: 4px; min-height: 36px; }
QScrollBar::handle:vertical:hover { background: #3a5fb3; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
QToolTip { background: #0d1833; color: #d6e4ff; border: 1px solid #28427f; padding: 4px; }
"""


class NoWheelSlider(QSlider):
    def wheelEvent(self, e):
        e.ignore()


class NoWheelSpin(QSpinBox):
    def wheelEvent(self, e):
        e.ignore()


class NoWheelDoubleSpin(QDoubleSpinBox):
    def wheelEvent(self, e):
        e.ignore()


class NoWheelCombo(QComboBox):
    def wheelEvent(self, e):
        e.ignore()


class Pill(QPushButton):
    """ON(明るい青) / OFF(暗い紺) のボタン"""

    def __init__(self, small=False):
        super().__init__()
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        if small:
            self.setObjectName("pillsm")
            self.setFixedSize(56, 24)
        else:
            self.setObjectName("pill")
            self.setFixedSize(84, 28)
        self.toggled.connect(self._sync)
        self._sync(False)

    def _sync(self, on):
        self.setText("ON" if on else "OFF")

    def set_on(self, on):
        self.blockSignals(True)
        self.setChecked(bool(on))
        self.blockSignals(False)
        self._sync(bool(on))


class Panel(QFrame):
    """設定項目をまとめる囲み"""

    def __init__(self, title=None):
        super().__init__()
        self.setObjectName("panel")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(20, 14, 20, 8)
        self.lay.setSpacing(0)
        self._n = 0
        if title:
            t = QLabel(title)
            t.setObjectName("paneltitle")
            self.lay.addWidget(t)
            self.lay.addSpacing(4)

    def add_row(self, label, control, desc=""):
        wrap = QWidget()                    # 区切り線 + 行 をひとまとめ (隠すとき一緒に消える)
        wl = QVBoxLayout(wrap)
        wl.setContentsMargins(0, 0, 0, 0)
        wl.setSpacing(0)
        if self._n:
            sep = QFrame()
            sep.setObjectName("sep")
            sep.setFixedHeight(1)
            wl.addWidget(sep)
        row = QWidget()
        wl.addWidget(row)
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 11, 0, 11)
        left = QVBoxLayout()
        left.setSpacing(2)
        l1 = QLabel(label)
        l1.setObjectName("rowlabel")
        left.addWidget(l1)
        if desc:
            l2 = QLabel(desc)
            l2.setObjectName("rowdesc")
            l2.setWordWrap(True)
            left.addWidget(l2)
        h.addLayout(left, 1)
        h.addSpacing(16)
        h.addWidget(control, 0, Qt.AlignVCenter)
        self.lay.addWidget(wrap)
        self._n += 1
        return wrap


class HudCard(QFrame):
    clicked = Signal()

    def __init__(self, hid, hud, cfg):
        super().__init__()
        self.hid, self.hud, self.cfg = hid, hud, cfg
        self.setObjectName("card")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setFixedSize(CARD_W, 218)
        self.setCursor(Qt.PointingHandCursor)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 14, 14, 14)
        lay.setSpacing(6)
        self.thumb = QLabel()
        self.thumb.setObjectName("thumb")
        self.thumb.setFixedHeight(88)
        self.thumb.setAlignment(Qt.AlignCenter)
        lay.addWidget(self.thumb)
        t = QLabel(HUD_DEFS[hid][0])
        t.setObjectName("cardtitle")
        s = QLabel(HUD_DEFS[hid][1])
        s.setObjectName("cardsub")
        s.setWordWrap(True)
        lay.addWidget(t)
        lay.addWidget(s)
        lay.addStretch()
        row = QHBoxLayout()
        self.hint = QLabel("クリックで詳細設定 ›")
        self.hint.setObjectName("cardsub")
        self.toggle = Pill()
        row.addWidget(self.hint, 1)
        row.addWidget(self.toggle)
        lay.addLayout(row)
        self.toggle.set_on(cfg.hud(hid)["enabled"])
        self.refresh_thumb()

    def refresh_thumb(self):
        pix = QPixmap(self.hud.size())
        pix.fill(Qt.transparent)
        self.hud.render(pix)
        maxw, maxh = self.thumb.width() - 16 if self.thumb.width() > 40 else 190, 76
        scale = min(maxw / max(1, pix.width()), maxh / max(1, pix.height()), 1.3)
        if scale != 1.0:
            pix = pix.scaled(max(1, int(pix.width() * scale)), max(1, int(pix.height() * scale)),
                             Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self.thumb.setPixmap(pix)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton and self.rect().contains(e.position().toPoint()):
            self.clicked.emit()


class _ThumbCfg:
    """サムネイル描画用の設定入れ物 (本物の設定には触れない)"""

    def __init__(self):
        self.d = {h: default_hud(h) for h in HUD_ORDER}

    def hud(self, hid):
        return self.d[hid]


class ThumbRenderer:
    """プリセットの見た目で CPS / FPS / キーストローク / Ping を1枚の絵にする"""

    def __init__(self, st):
        self.cfg = _ThumbCfg()
        # 本物の状態 (実測FPS・実際のCPS・実際のキー入力) をそのまま使う。固定のダミー値は使わない
        self.huds = {"cps": CpsHud(self.cfg, st, None),
                     "fps": FpsHud(self.cfg, st, None),
                     "keystrokes": KeystrokeHud(self.cfg, st, None),
                     "ping": PingHud(self.cfg, st, None)}

    def _grab(self, hid, appearance):
        d = default_hud(hid)
        d.update(appearance.get(hid, {}))
        self.cfg.d[hid] = d
        hud = self.huds[hid]
        hud.apply_config()
        pix = QPixmap(hud.size())
        pix.fill(Qt.transparent)
        hud.render(pix)
        return pix

    def render(self, appearance, max_w, max_h):
        cps, fps, ks, png = (self._grab(h, appearance) for h in ("cps", "fps", "keystrokes", "ping"))
        gap = 8
        w = ks.width() + gap + max(cps.width(), fps.width(), png.width())
        h = max(ks.height(), cps.height() + gap + fps.height() + gap + png.height())
        scale = min(max_w / float(w), max_h / float(h), 1.0)
        out = QPixmap(max(1, int(w * scale)), max(1, int(h * scale)))
        out.fill(Qt.transparent)
        pt = QPainter(out)
        pt.setRenderHint(QPainter.SmoothPixmapTransform, True)
        pt.scale(scale, scale)
        pt.drawPixmap(0, 0, ks)
        x = ks.width() + gap
        pt.drawPixmap(x, 0, cps)
        pt.drawPixmap(x, cps.height() + gap, fps)
        pt.drawPixmap(x, cps.height() + gap + fps.height() + gap, png)
        pt.end()
        return out


class PresetCard(QFrame):
    clicked = Signal(str)

    def __init__(self, preset, thumb_pix):
        super().__init__()
        self.pid = preset["id"]
        self.setObjectName("card")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setFixedSize(PRESET_CARD_W, 176)
        self.setCursor(Qt.PointingHandCursor)
        self.setProperty("sel", False)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(6)
        th = QLabel()
        th.setObjectName("thumb")
        th.setFixedHeight(104)
        th.setAlignment(Qt.AlignCenter)
        th.setPixmap(thumb_pix)
        self.th = th
        self.huds = preset["huds"]
        lay.addWidget(th)
        t = QLabel(preset["name"])
        t.setObjectName("cardtitle")
        sub = QLabel("保存したプリセット")
        sub.setObjectName("cardsub")
        lay.addWidget(t)
        lay.addWidget(sub)

    def set_selected(self, on):
        self.setProperty("sel", bool(on))
        self.style().unpolish(self)
        self.style().polish(self)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton and self.rect().contains(e.position().toPoint()):
            self.clicked.emit(self.pid)


class SysRow(QFrame):
    """システム画面の項目 (クリックで詳細が開く)"""
    clicked = Signal()

    def __init__(self, title, desc):
        super().__init__()
        self.setObjectName("card")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setFixedHeight(68)
        self.setCursor(Qt.PointingHandCursor)
        h = QHBoxLayout(self)
        h.setContentsMargins(20, 10, 20, 10)
        v = QVBoxLayout()
        v.setSpacing(2)
        t = QLabel(title)
        t.setObjectName("cardtitle")
        d = QLabel(desc)
        d.setObjectName("cardsub")
        v.addWidget(t)
        v.addWidget(d)
        h.addLayout(v, 1)
        arrow = QLabel("›")
        arrow.setObjectName("title")
        h.addWidget(arrow)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton and self.rect().contains(e.position().toPoint()):
            self.clicked.emit()


class CaptureDialog(QDialog):
    """次に押された物理キー/マウスボタンを検出する"""
    SCAN = [v for v in range(1, 255) if not (0xA0 <= v <= 0xA5)]

    def __init__(self, parent, label):
        super().__init__(parent)
        self.setWindowTitle(T("キー割り当て"))
        self.setModal(True)
        self.setFixedSize(420, 170)
        self.result_vk = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 24, 24, 18)
        t = QLabel(T("「%s」に割り当てる\nキー(またはマウスボタン)を押してください") % label)
        t.setAlignment(Qt.AlignCenter)
        t.setStyleSheet("font-size: 15px; color: #ffffff;")
        h = QLabel(T("Esc でキャンセル"))
        h.setObjectName("rowdesc")
        h.setAlignment(Qt.AlignCenter)
        lay.addWidget(t, 1)
        lay.addWidget(h)
        self._ignore = {v for v in self.SCAN if key_down(v)}
        self._timer = QTimer(self)
        self._timer.setInterval(10)
        self._timer.timeout.connect(self._poll)
        self._timer.start()

    def _poll(self):
        for vk in self.SCAN:
            if not key_down(vk):
                self._ignore.discard(vk)
                continue
            if vk in self._ignore:
                continue
            if vk == 0x1B:
                self.reject()
                return
            self.result_vk = vk
            self.accept()
            return


class DetailPage(QWidget):
    back = Signal()
    reload = Signal()

    def __init__(self, cfg, ctrl, hid, tab=0):
        super().__init__()
        self.setObjectName("page")
        self.cfg, self.ctrl, self.hid = cfg, ctrl, hid
        self.c = cfg.hud(hid)
        self.dep = {"bg_on": [], "border_on": [], "border_blur": []}
        self.bind_buttons = {}
        self.pos_spins = {}
        self.fps_status_label = None
        self.ping_status_label = None
        self._tmpl_btns = []

        root = QVBoxLayout(self)
        root.setContentsMargins(36, 28, 36, 20)
        head = QHBoxLayout()
        b = QPushButton("‹  戻る")
        b.clicked.connect(self.back.emit)
        head.addWidget(b)
        head.addSpacing(14)
        t = QLabel(HUD_DEFS[hid][0])
        t.setObjectName("title")
        head.addWidget(t)
        head.addSpacing(12)
        badge = QLabel("プレビュー表示中")
        badge.setObjectName("badge")
        head.addWidget(badge)
        head.addStretch()
        root.addLayout(head)
        sub = QLabel("変更は自動で保存されます。プレビューはこのウィンドウの横に表示されます。")
        sub.setObjectName("subtitle")
        root.addWidget(sub)
        root.addSpacing(6)

        self.tabs = QTabWidget()
        root.addWidget(self.tabs, 1)
        self._tab_basic()
        self._tab_text()
        self._tab_bg()
        self._tab_border()
        self._tab_specific()
        self.tabs.setCurrentIndex(tab)
        self._update_enabled()
        retranslate(self)

        cfg.keys_changed.connect(self.refresh_binds)
        cfg.hud_changed.connect(self.on_cfg)
        self._timer = QTimer(self)
        self._timer.setInterval(400)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    # ---- タブ生成 ----
    def _new_tab(self, title):
        sc = QScrollArea()
        sc.setWidgetResizable(True)
        host = QWidget()
        host.setObjectName("host")
        lay = QVBoxLayout(host)
        lay.setContentsMargins(0, 14, 14, 14)
        lay.setSpacing(14)
        sc.setWidget(host)
        self.tabs.addTab(sc, title)
        return lay

    def _reset_button(self, lay, group, extra=None):
        btn = QPushButton("このタブを初期値に戻す")
        btn.clicked.connect(lambda: self._do_reset(group, extra))
        row = QHBoxLayout()
        row.addStretch()
        row.addWidget(btn)
        lay.addLayout(row)
        lay.addStretch()

    def _do_reset(self, group, extra):
        self.cfg.reset_hud(self.hid, GROUP_KEYS[group])
        if extra:
            extra()
        self.reload.emit()

    # ---- 行の部品 ----
    def _slider_ctl(self, key, lo, hi, suffix):
        box = QWidget()
        h = QHBoxLayout(box)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(10)
        s = NoWheelSlider(Qt.Horizontal)
        s.setRange(lo, hi)
        s.setValue(int(self.c[key]))
        s.setFixedWidth(230)
        sp = NoWheelSpin()
        sp.setRange(lo, hi)
        sp.setValue(int(self.c[key]))
        sp.setSuffix(suffix)
        sp.setFixedWidth(84)
        sp.setAlignment(Qt.AlignRight)

        def from_slider(v):
            sp.blockSignals(True)
            sp.setValue(v)
            sp.blockSignals(False)
            self.cfg.set(self.hid, key, v)

        def from_spin(v):
            s.blockSignals(True)
            s.setValue(v)
            s.blockSignals(False)
            self.cfg.set(self.hid, key, v)
        s.valueChanged.connect(from_slider)
        sp.valueChanged.connect(from_spin)
        h.addWidget(s)
        h.addWidget(sp)
        return box

    def r_slider(self, panel, key, label, lo, hi, suffix="%", desc="", dep=None):
        row = panel.add_row(label, self._slider_ctl(key, lo, hi, suffix), desc)
        if dep:
            self.dep[dep].append(row)
        return row

    def r_color(self, panel, key, label, desc="", dep=None):
        b = QPushButton()
        b.setFixedSize(130, 30)

        def paint():
            col = QColor(self.c[key])
            fg = "#000000" if col.lightness() > 140 else "#ffffff"
            b.setText(self.c[key].upper())
            b.setStyleSheet("QPushButton { background: %s; color: %s; border: 1px solid #6f8fd8;"
                            " border-radius: 8px; font-weight: bold; padding: 0; }"
                            .replace("#6f8fd8", theme_hex("#6f8fd8")) % (self.c[key], fg))

        def pick():
            col = QColorDialog.getColor(QColor(self.c[key]), self.window(), T("色を選択"))
            if col.isValid():
                self.cfg.set(self.hid, key, col.name())
                paint()
        paint()
        b.clicked.connect(pick)
        row = panel.add_row(label, b, desc)
        if dep:
            self.dep[dep].append(row)
        return row

    def r_toggle(self, panel, key, label, desc="", dep=None):
        t = Pill(small=True)
        t.set_on(self.c[key])

        def changed(on):
            self.cfg.set(self.hid, key, on)
            self._update_enabled()
        t.toggled.connect(changed)
        row = panel.add_row(label, t, desc)
        if dep:
            self.dep[dep].append(row)
        return row

    def r_combo(self, panel, key, label, items, desc=""):
        cb = NoWheelCombo()
        cb.setFixedWidth(230)
        cur = 0
        for i, (text, val) in enumerate(items):
            cb.addItem(text, val)
            if val == self.c[key]:
                cur = i
        cb.setCurrentIndex(cur)
        cb.currentIndexChanged.connect(lambda i: self.cfg.set(self.hid, key, cb.itemData(i)))
        return panel.add_row(label, cb, desc)

    def _update_enabled(self):
        """ON/OFFに連動する設定は、OFFのとき非表示・ONのとき表示"""
        bg = bool(self.c["bg_on"])
        border = bool(self.c["border_on"])
        blur = border and bool(self.c["border_blur"])
        for w in self.dep["bg_on"]:
            w.setVisible(bg)
        for w in self.dep["border_on"]:
            w.setVisible(border)
        for w in self.dep["border_blur"]:
            w.setVisible(blur)

    # ---- 各タブ ----
    def _tab_basic(self):
        lay = self._new_tab("全体")
        p = Panel("表示")
        self.r_slider(p, "opacity", "全体の不透明度", 0, 100, "%", "HUD全体の透け具合")
        self.r_slider(p, "scale", "大きさ", 30, 400, "%", "HUD全体の拡大・縮小")
        lay.addWidget(p)
        p2 = Panel("位置 (画面左上からのpx)")
        for key, label in (("x", "X 位置"), ("y", "Y 位置")):
            sp = NoWheelSpin()
            sp.setRange(-5000, 20000)
            sp.setValue(int(self.c[key]))
            sp.setSuffix(" px")
            sp.setFixedWidth(110)
            sp.setAlignment(Qt.AlignRight)
            sp.valueChanged.connect(lambda v, k=key: self.cfg.set(self.hid, k, v))
            self.pos_spins[key] = sp
            p2.add_row(label, sp, "Ctrl+ドラッグでも移動できます" if key == "x" else "")
        lay.addWidget(p2)
        self._reset_button(lay, "basic")

    def _tab_text(self):
        lay = self._new_tab("文字")
        p = Panel("文字")
        self.r_color(p, "text_color", "文字の色")
        self.r_slider(p, "text_opacity", "文字の不透明度", 0, 100)
        self.r_slider(p, "text_size", "文字の大きさ", 50, 250, "%", "文字全体の大きさ (幅・高さ同時)")
        self.r_slider(p, "text_w", "文字の幅の比", 30, 300, "%", "1文字ごとの横幅。大きいほど横に広い字になります")
        self.r_slider(p, "text_h", "文字の高さの比", 30, 300, "%", "1文字ごとの縦の長さ。大きいほど縦に長い字になります")
        self.r_slider(p, "text_y", "文字の上下位置", -30, 30, " px", "枠の中で文字を上(−)/下(+)へ動かします")
        self.r_slider(p, "text_spacing", "字間", -50, 200, "%", "文字と文字の間隔 (0%が標準。\"LMB\"のような複数文字の表示に効きます)")
        lay.addWidget(p)
        self._reset_button(lay, "text")

    def _tab_bg(self):
        lay = self._new_tab("背景")
        p = Panel("背景")
        self.r_toggle(p, "bg_on", "背景を表示")
        self.r_color(p, "bg_color", "背景の色", dep="bg_on")
        self.r_slider(p, "bg_opacity", "背景の不透明度", 0, 100, dep="bg_on")
        lay.addWidget(p)
        p2 = Panel("形")
        self.r_slider(p2, "bg_w", "横の比", 20, 400, "%", "枠・背景の横幅の比率 (文字の大きさは変わりません)")
        self.r_slider(p2, "bg_h", "縦の比", 20, 400, "%", "枠・背景の縦幅の比率")
        self.r_slider(p2, "corner", "角の丸み", 0, 40, " px")
        lay.addWidget(p2)
        self._reset_button(lay, "bg")

    def _tab_border(self):
        lay = self._new_tab("枠")
        p = Panel("枠")
        self.r_toggle(p, "border_on", "枠を表示", "背景とは独立して設定できます")
        self.r_color(p, "border_color", "枠の色", dep="border_on")
        self.r_slider(p, "border_width", "枠の太さ", 1, 12, " px", dep="border_on")
        self.r_slider(p, "border_opacity", "枠の不透明度", 0, 100, dep="border_on")
        lay.addWidget(p)
        p2 = Panel("ぼかし (外側へ向かって薄くなる光)")
        self.dep["border_on"].append(p2)    # 枠がOFFならぼかしの欄ごと隠す
        self.r_toggle(p2, "border_blur", "枠をぼかす")
        self.r_slider(p2, "border_blur_size", "ぼかしの広がり", 1, 40, " px", dep="border_blur")
        lay.addWidget(p2)
        self._reset_button(lay, "border")

    def _tab_specific(self):
        titles = {"keystrokes": "キーストローク", "cps": "CPS", "fps": "FPS測定", "ping": "Ping測定"}
        lay = self._new_tab(titles[self.hid])
        if self.hid == "keystrokes":
            p = Panel("表示")
            self.r_toggle(p, "show_dash", "ダッシュを表示",
                          "スペースの下に表示します。割り当てたキーが押されているかどうかを表示します")
            self.r_toggle(p, "show_sprint", "スプリントを表示",
                          "ダッシュと同じ段に並びます。割り当てたキーが押されているかどうかを表示します")
            self.r_toggle(p, "show_mouse", "右/左クリックを表示", "キーストロークの下に表示します")
            self.r_color(p, "pressed_color", "押したときの色")
            self.r_slider(p, "pressed_opacity", "押したときの不透明度", 0, 100)
            lay.addWidget(p)
            self._bind_panel(lay, "キー割り当て", MOVE_KEYS + EXTRA_KEYS + CLICK_KEYS,
                             "ゲームでキー設定を変えている場合に、実際のキーに合わせます。"
                             "表示(W/A/S/D)は変わらず、検出するキーだけが変わります。"
                             "ダッシュは初期でShift、スプリントは初期でTabです。")
            self._reset_button(lay, "keystrokes",
                               lambda: self.cfg.reset_keys(MOVE_KEYS + EXTRA_KEYS + CLICK_KEYS))
        elif self.hid == "cps":
            self._bind_panel(lay, "クリック割り当て", CLICK_KEYS,
                             "ゲームでマウスボタンの設定を変えている場合に合わせます。"
                             "表示は 左クリック-右クリック の順です。")
            lay.addStretch()
        elif self.hid == "ping":
            p = Panel("表示")
            self.r_combo(p, "ping_format", "表記",
                         [("数字のみ   [32]", 0), ("単位付き   [32ms]", 1), ("ラベル付き   [PING:32ms]", 2)])
            self.r_combo(p, "ping_interval", "更新間隔",
                         [("0.5秒", 500), ("1.0秒", 1000), ("2.0秒", 2000), ("5.0秒", 5000)])
            lay.addWidget(p)
            pt = Panel("サーバーのテンプレート")
            box = QWidget()
            bl = QHBoxLayout(box)
            bl.setContentsMargins(0, 0, 0, 0)
            bl.setSpacing(8)
            self._tmpl_btns = []
            for name, host, port in PING_TEMPLATES:
                tb = QPushButton(name)
                tb.setObjectName("tmpl")
                tb.setCheckable(True)
                tb.setCursor(Qt.PointingHandCursor)
                tb.clicked.connect(lambda _=False, h=host, pt_=port: self._apply_ping_template(h, pt_))
                bl.addWidget(tb)
                self._tmpl_btns.append(tb)
            pt.add_row("統合版サーバー", box,
                       "押すと下の「測定先のアドレス」と「ポート」に自動で入ります。ほかのサーバーは下に直接入力してください。")
            lay.addWidget(pt)
            p2 = Panel("測定")
            self.ping_status_label = QLabel("")
            self.ping_status_label.setObjectName("status")
            self.ping_status_label.setProperty("dyn", True)
            self.ping_status_label.setMinimumWidth(260)
            self.ping_status_label.setWordWrap(True)
            p2.add_row("状態", self.ping_status_label)
            le = QLineEdit(self.c["ping_host"])
            le.setPlaceholderText("例: play.example.net  /  1.1.1.1")
            le.setFixedWidth(260)
            le.textChanged.connect(lambda t: (self.cfg.set(self.hid, "ping_host", t.strip()),
                                               self._sync_ping_templates()))
            self._ping_le = le
            p2.add_row("測定先のアドレス", le,
                       "サーバーのアドレス (IPアドレスかホスト名)。ポート番号だけでは測れません。"
                       "「play.example.net:19132」のように続けて書いてもOKです。")
            sp = NoWheelSpin()
            sp.setRange(0, 65535)
            sp.setSpecialValueText(T("自動"))
            sp.setValue(int(self.c["ping_port"]))
            sp.setFixedWidth(110)
            sp.setAlignment(Qt.AlignRight)
            sp.valueChanged.connect(lambda v: (self.cfg.set(self.hid, "ping_port", int(v)),
                                               self._sync_ping_templates()))
            self._ping_sp = sp
            p2.add_row("ポート (任意)", sp,
                       "「自動」ならpingで測ります。統合版サーバーは 19132 を入れると、サーバー自身の応答時間を測れます。")
            lay.addWidget(p2)
            self._sync_ping_templates()
            self._reset_button(lay, "ping")
        else:
            p = Panel("表示")
            self.r_combo(p, "fps_label", "表記",
                         [("数字のみ   [140]", False), ("ラベル付き   [FPS:140]", True)])
            self.r_combo(p, "fps_interval", "更新間隔",
                         [("1.0秒 (マイクラのF3と同じ)", 1000), ("0.5秒", 500),
                          ("0.25秒", 250), ("0.1秒", 100)],
                         "マイクラは1秒ごとに数字を更新します。同じ数字に揃えたい場合は 1.0秒 のままに。")
            lay.addWidget(p)
            px = Panel("追加の表示")
            self.r_toggle(px, "show_low1", "1% Low を表示",
                          "FPSの下に表示します。直近60秒で遅かった方から1%のフレームの平均をFPSにした値です")
            self.r_toggle(px, "show_low01", "0.1% Low を表示",
                          "直近60秒で遅かった方から0.1%のフレームの平均。1000フレームほどたまるまでは「--」です")
            self.r_toggle(px, "show_avg", "平均FPSを表示", "直近60秒の平均です")
            lay.addWidget(px)
            p2 = Panel("測定")
            self.fps_status_label = QLabel("")
            self.fps_status_label.setObjectName("status")
            self.fps_status_label.setProperty("dyn", True)
            self.fps_status_label.setMinimumWidth(260)
            self.fps_status_label.setWordWrap(True)
            p2.add_row("状態", self.fps_status_label)
            le = QLineEdit(self.c["fps_target"])
            le.setPlaceholderText("空欄 = 最前面のゲーム (自動)")
            le.setFixedWidth(260)
            le.textChanged.connect(lambda t: self.cfg.set(self.hid, "fps_target", t.strip()))
            p2.add_row("測定するプロセス名", le,
                       "通常は空欄でOK。複数起動している場合だけ javaw.exe などを指定。")
            lay.addWidget(p2)
            self._reset_button(lay, "fps")

    def _apply_ping_template(self, host, port):
        self.cfg.set_many(self.hid, {"ping_host": host, "ping_port": port})
        for w, apply in ((self._ping_le, lambda w: w.setText(host)), (self._ping_sp, lambda w: w.setValue(port))):
            w.blockSignals(True)
            apply(w)
            w.blockSignals(False)
        self._sync_ping_templates()

    def _sync_ping_templates(self):
        """今の測定先がテンプレートと同じなら、そのボタンを青く光らせる"""
        t = parse_ping_target(self.c["ping_host"])
        host, port = (t[0].lower(), t[1] or int(self.c["ping_port"])) if t else ("", 0)
        for (_n, h, p_), b in zip(PING_TEMPLATES, self._tmpl_btns):
            b.setChecked(h == host and p_ == port)

    def _bind_panel(self, lay, title, names, desc):
        p = Panel(title)
        d = QLabel(desc)
        d.setObjectName("rowdesc")
        d.setWordWrap(True)
        d.setContentsMargins(0, 2, 0, 8)
        p.lay.addWidget(d)
        for n in names:
            b = QPushButton(self.bind_text(n))
            b.setObjectName("bind")
            b.setProperty("dyn", True)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _=False, name=n: self.capture(name))
            self.bind_buttons[n] = b
            p.lay.addWidget(b)
            p.lay.addSpacing(8)
        lay.addWidget(p)

    # ---- キー割り当て ----
    def bind_text(self, name):
        vk = self.cfg.keys[name]
        shown = T("通常") if (name in CLICK_KEYS and vk == DEFAULT_KEYS[name]) else T(vk_name(vk))
        return "%s : %s" % (T(KEY_LABELS[name]), shown)

    def capture(self, name):
        dlg = CaptureDialog(self.window(), T(KEY_LABELS[name]))
        if dlg.exec() == QDialog.Accepted and dlg.result_vk:
            self.cfg.set_key(name, dlg.result_vk)

    def refresh_binds(self):
        try:
            for n, b in self.bind_buttons.items():
                b.setText(self.bind_text(n))
        except RuntimeError:
            pass

    def on_cfg(self, hid):
        if hid != self.hid:
            return
        try:
            for k, sp in self.pos_spins.items():
                if sp.value() != int(self.c[k]):
                    sp.blockSignals(True)
                    sp.setValue(int(self.c[k]))
                    sp.blockSignals(False)
        except RuntimeError:
            pass

    def _tick(self):
        if self.ping_status_label is not None:
            text = self.ctrl.ping.status_text()
            self.ping_status_label.setText(text)
        if self.fps_status_label is not None:
            text = self.ctrl.fps_text or self.ctrl.pm.status_text()
            v = self.ctrl.state.fps
            if v is not None:
                text = "%s  →  %d FPS" % (text, v)
            self.fps_status_label.setText(text)


class SettingsWindow(QMainWindow):
    def __init__(self, cfg, ctrl, icon):
        super().__init__()
        self.cfg, self.ctrl = cfg, ctrl
        self.cards = {}
        self.detail = None
        self.hooks = []         # 言語・色が変わったときに呼ぶ関数 (トレイメニューの更新など)
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(icon)
        # ウィンドウの大きさは固定 (カードがぴったり収まる幅)。最大化も不可。
        n = CARD_COLS
        self.setWindowFlag(Qt.WindowMaximizeButtonHint, False)
        self.setFixedSize(SIDEBAR_W + 2 * PAGE_MARGIN + n * CARD_W + (n - 1) * CARD_GAP, 640)
        self.setStyleSheet(themed(QSS))

        root = QWidget()
        root.setObjectName("root")
        h = QHBoxLayout(root)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)
        self.setCentralWidget(root)

        # ---- サイドバー ----
        side = QFrame()
        side.setObjectName("sidebar")
        side.setFixedWidth(SIDEBAR_W)
        sv = QVBoxLayout(side)
        sv.setContentsMargins(16, 22, 16, 18)
        sv.setSpacing(6)
        logo_row = QHBoxLayout()
        pm = QLabel()
        pm.setPixmap(icon.pixmap(QSize(40, 40)))
        logo_row.addWidget(pm)
        lt = QVBoxLayout()
        lt.setSpacing(0)
        a = QLabel(APP_NAME)
        a.setObjectName("logo")
        b = QLabel("GAME HUD  v" + APP_VERSION)
        b.setObjectName("logosub")
        lt.addWidget(a)
        lt.addWidget(b)
        logo_row.addLayout(lt)
        logo_row.addStretch()
        sv.addLayout(logo_row)
        sv.addSpacing(20)
        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        self.nav_btns = {}
        for key, text in (("hud", "▣   表示設定"), ("presets", "◈   プリセット"),
                          ("system", "⚙   システム")):
            btn = QPushButton(text)
            btn.setObjectName("nav")
            btn.setCheckable(True)
            btn.setCursor(Qt.PointingHandCursor)
            btn.clicked.connect(lambda _=False, k=key: self.show_page(k))
            self.nav_group.addButton(btn)
            self.nav_btns[key] = btn
            sv.addWidget(btn)
        sv.addStretch()
        tip = QLabel("Ctrl + ドラッグ\nでHUDを移動\n\n×で閉じてもHUDは\nタスクトレイで動作中")
        tip.setObjectName("navtip")
        sv.addWidget(tip)
        h.addWidget(side)

        # ---- ページ ----
        self.stack = QStackedWidget()
        h.addWidget(self.stack, 1)
        self.thumbs = ThumbRenderer(self.ctrl.state)
        self.sel_preset = None
        self.preset_cards = {}
        self.sys_shortcut_idx = -1
        self._mic_polled = 0.0
        self.list_page = self._build_list()
        self.preset_page = self._build_presets()
        self.system_page = self._build_system()
        self.stack.addWidget(self.list_page)
        self.stack.addWidget(self.preset_page)
        self.stack.addWidget(self.system_page)
        self.nav_btns["hud"].setChecked(True)

        cfg.hud_changed.connect(self._on_hud_changed)
        self._thumb_timer = QTimer(self)
        self._thumb_timer.setInterval(300)
        self._thumb_timer.timeout.connect(self._refresh_thumbs)
        self._thumb_timer.start()
        self._status_timer = QTimer(self)
        self._status_timer.setInterval(500)
        self._status_timer.timeout.connect(self._refresh_status)
        self._status_timer.start()
        retranslate(self)

    # ---- 言語 / 色 ----
    def set_language(self, code):
        set_lang(code)
        self.cfg.set_app("language", LANG)
        retranslate(self)
        for fn in self.hooks:
            fn()
        self._update_preset_bar()
        for c, b in self.lang_btns.items():
            b.setChecked(c == LANG)

    def set_ui_color(self, hex_str):
        set_ui_color(hex_str)
        self.cfg.set_app("ui_color", UI_COLOR)
        self.apply_theme()

    def apply_theme(self):
        self.setStyleSheet(themed(QSS))
        self._paint_titlebar()
        for fn in self.hooks:
            fn()

    # ---- 一覧ページ ----
    def _header(self, title, sub):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(2)
        t = QLabel(title)
        t.setObjectName("title")
        s = QLabel(sub)
        s.setObjectName("subtitle")
        v.addWidget(t)
        v.addWidget(s)
        return w

    def _build_list(self):
        page = QWidget()
        page.setObjectName("page")
        v = QVBoxLayout(page)
        v.setContentsMargins(PAGE_MARGIN, 28, PAGE_MARGIN, 20)
        v.addWidget(self._header(
            "表示設定",
            "カードをクリックで詳細設定  /  ボタン: 青=表示・暗い色=非表示  /  Ctrl+ドラッグでHUDを移動"))
        v.addSpacing(14)
        grid = QGridLayout()                # カードは左から順に並べ、CARD_COLS枚で次の段へ
        grid.setHorizontalSpacing(CARD_GAP)
        grid.setVerticalSpacing(CARD_GAP)
        grid.setContentsMargins(0, 0, 0, 0)
        for i, hid in enumerate(HUD_ORDER):
            card = HudCard(hid, self.ctrl.huds[hid], self.cfg)
            card.toggle.toggled.connect(lambda on, h=hid: self.cfg.set(h, "enabled", on))
            card.clicked.connect(lambda h=hid: self.open_detail(h))
            self.cards[hid] = card
            grid.addWidget(card, i // CARD_COLS, i % CARD_COLS, Qt.AlignLeft | Qt.AlignTop)
        grid.setColumnStretch(CARD_COLS, 1)
        grid.setRowStretch((len(HUD_ORDER) - 1) // CARD_COLS + 1, 1)
        v.addLayout(grid)
        v.addStretch(1)
        return page

    def _on_hud_changed(self, hid):
        card = self.cards.get(hid)
        if card:
            card.toggle.set_on(self.cfg.hud(hid)["enabled"])
            card.refresh_thumb()

    def _refresh_thumbs(self):
        if not self.isVisible():
            return
        cur = self.stack.currentWidget()
        if cur is self.list_page:
            for card in self.cards.values():
                card.refresh_thumb()
        elif cur is self.preset_page:       # プリセットのカードも実測値で更新し続ける
            for card in self.preset_cards.values():
                card.th.setPixmap(self.thumbs.render(card.huds, PRESET_CARD_W - 40, 96))

    # ---- プリセットページ ----
    def _build_presets(self):
        page = QWidget()
        page.setObjectName("page")
        v = QVBoxLayout(page)
        v.setContentsMargins(PAGE_MARGIN, 28, PAGE_MARGIN, 20)
        head = QHBoxLayout()
        head.addWidget(self._header(
            "プリセット",
            "見た目(色・文字・背景・枠など)を保存して切り替え  /  カードを選ぶとプレビュー"), 1)
        b = QPushButton("＋  今の見た目を保存")
        b.clicked.connect(self._preset_save_new)
        head.addWidget(b, 0, Qt.AlignTop)
        v.addLayout(head)
        v.addSpacing(14)
        sc = QScrollArea()
        sc.setWidgetResizable(True)
        sc.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        host = QWidget()
        host.setObjectName("host")
        self.preset_grid = QGridLayout(host)
        self.preset_grid.setSpacing(14)
        self.preset_grid.setContentsMargins(0, 0, 0, 0)
        self.preset_grid.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        sc.setWidget(host)
        v.addWidget(sc, 1)

        bar = QFrame()
        bar.setObjectName("panel")
        bh = QHBoxLayout(bar)
        bh.setContentsMargins(16, 10, 16, 10)
        self.preset_info = QLabel("")
        self.preset_info.setObjectName("status")
        self.preset_info.setProperty("dyn", True)
        bh.addWidget(self.preset_info, 1)
        self.pb_apply = QPushButton("適用")
        self.pb_clear = QPushButton("プレビュー解除")
        self.pb_rename = QPushButton("名前変更")
        self.pb_over = QPushButton("今の見た目で上書き")
        self.pb_del = QPushButton("削除")
        self.pb_del.setObjectName("danger")
        self.pb_apply.clicked.connect(self._preset_apply)
        self.pb_clear.clicked.connect(lambda: self.cancel_preset_preview())
        self.pb_rename.clicked.connect(self._preset_rename)
        self.pb_over.clicked.connect(self._preset_overwrite)
        self.pb_del.clicked.connect(self._preset_delete)
        for w in (self.pb_apply, self.pb_clear, self.pb_rename, self.pb_over, self.pb_del):
            bh.addWidget(w)
        v.addSpacing(10)
        v.addWidget(bar)
        self._update_preset_bar()
        return page

    def _rebuild_presets(self):
        for c in self.preset_cards.values():
            self.preset_grid.removeWidget(c)
            c.deleteLater()
        self.preset_cards = {}
        cols = 3
        for i, pr in enumerate(self.cfg.all_presets()):
            pix = self.thumbs.render(pr["huds"], PRESET_CARD_W - 40, 96)
            card = PresetCard(pr, pix)
            card.clicked.connect(self._select_preset)
            self.preset_cards[pr["id"]] = card
            self.preset_grid.addWidget(card, i // cols, i % cols)
        retranslate(self.preset_page)
        self._update_preset_bar()

    def _select_preset(self, pid):
        pr = self.cfg.get_preset(pid)
        if pr is None:
            return
        self.sel_preset = pid
        for k, c in self.preset_cards.items():
            c.set_selected(k == pid)
        merged = {}
        for h in HUD_ORDER:
            d = default_hud(h)
            d.update(pr["huds"].get(h, {}))
            merged[h] = d
        self.ctrl.set_preset_preview(merged)
        self._update_preset_bar()

    def _update_preset_bar(self):
        pr = self.cfg.get_preset(self.sel_preset) if self.sel_preset else None
        if pr is None:
            self.preset_info.setText(T("カードを選ぶと、画面の横にプレビューが出ます") if self.preset_cards
                                     else T("まだプリセットがありません。HUDを好みの見た目にして「＋ 今の見た目を保存」を押してください"))
        else:
            self.preset_info.setText(T("選択中:  ") + pr["name"])
        user = pr is not None and not pr.get("builtin")
        self.pb_apply.setEnabled(pr is not None)
        self.pb_clear.setEnabled(pr is not None)
        for w in (self.pb_rename, self.pb_over, self.pb_del):
            w.setEnabled(user)

    def cancel_preset_preview(self, message=None):
        """選択を外してプレビューをやめる (HUDは本来の見た目に戻る。適用はされない)"""
        self.ctrl.set_preset_preview(None)
        self.sel_preset = None
        for c in self.preset_cards.values():
            c.set_selected(False)
        self._update_preset_bar()
        if message:
            self.preset_info.setText(message)

    def _ask_name(self, title, default=""):
        name, ok = QInputDialog.getText(self, T(title), T("プリセットの名前:"), text=default)
        name = name.strip()
        return name if ok and name else None

    def _preset_save_new(self):
        name = self._ask_name("プリセットとして保存")
        if not name:
            return
        pid = self.cfg.add_preset(name, capture_appearance(self.cfg))
        self._rebuild_presets()
        self._select_preset(pid)
        self.preset_info.setText(T("「%s」を保存しました") % name)

    def _preset_apply(self):
        pr = self.cfg.get_preset(self.sel_preset) if self.sel_preset else None
        if pr is None:
            return
        self.ctrl.set_preset_preview(None)       # 仮表示をやめて、本物のHUDに反映
        self.cfg.apply_preset(pr)
        self.preset_info.setText(T("「%s」を適用しました (位置・ON/OFFは変わりません)") % pr["name"])

    def _preset_rename(self):
        pr = self.cfg.get_preset(self.sel_preset) if self.sel_preset else None
        if pr is None or pr.get("builtin"):
            return
        name = self._ask_name("名前変更", pr["name"])
        if name:
            self.cfg.rename_preset(pr["id"], name)
            keep = pr["id"]
            self._rebuild_presets()
            self._select_preset(keep)

    def _preset_overwrite(self):
        pr = self.cfg.get_preset(self.sel_preset) if self.sel_preset else None
        if pr is None or pr.get("builtin"):
            return
        if QMessageBox.question(self, T("確認"), T("「%s」を、今のHUDの見た目で上書きします。よろしいですか?")
                                % pr["name"]) == QMessageBox.Yes:
            self.cfg.overwrite_preset(pr["id"], capture_appearance(self.cfg))
            keep = pr["id"]
            self._rebuild_presets()
            self._select_preset(keep)

    def _preset_delete(self):
        pr = self.cfg.get_preset(self.sel_preset) if self.sel_preset else None
        if pr is None or pr.get("builtin"):
            return
        if QMessageBox.question(self, T("確認"), T("プリセット「%s」を削除します。よろしいですか?")
                                % pr["name"]) == QMessageBox.Yes:
            self.cfg.delete_preset(pr["id"])
            self.ctrl.set_preset_preview(None)
            self.sel_preset = None
            self._rebuild_presets()

    # ---- システムページ (項目の一覧 → 押すと詳細) ----
    def _sys_sub(self, title, build, parent=None):
        """詳細ページを1枚作る。build(lay) で中身を入れる"""
        page = QWidget()
        page.setObjectName("page")
        v = QVBoxLayout(page)
        v.setContentsMargins(36, 28, 36, 20)
        head = QHBoxLayout()
        b = QPushButton("‹  戻る")
        b.clicked.connect(lambda: self.sys_stack.setCurrentIndex(self._sys_idx[parent] if parent else 0))
        head.addWidget(b)
        head.addSpacing(14)
        t = QLabel(title)
        t.setObjectName("title")
        head.addWidget(t)
        head.addStretch()
        v.addLayout(head)
        v.addSpacing(14)
        sc = QScrollArea()
        sc.setWidgetResizable(True)
        host = QWidget()
        host.setObjectName("host")
        lay = QVBoxLayout(host)
        lay.setContentsMargins(0, 0, 12, 0)
        lay.setSpacing(14)
        sc.setWidget(host)
        v.addWidget(sc, 1)
        build(lay)
        lay.addStretch()
        self.sys_stack.addWidget(page)
        return self.sys_stack.count() - 1

    def _sys_row(self, lay, title, desc, key):
        """詳細ページへ進む行 (ページ番号は押した時に self._sys_idx[key] から引く)"""
        row = SysRow(title, desc)
        row.clicked.connect(lambda: self.sys_stack.setCurrentIndex(self._sys_idx[key]))
        lay.addWidget(row)

    def _build_system(self):
        """システム > 起動 / 表示するタイミング / 便利ショートカットキー > マイクミュート > (見た目・フェード・音) / ..."""
        self.sys_stack = QStackedWidget()
        self._sys_idx = {}
        lst = QWidget()
        lst.setObjectName("page")
        v = QVBoxLayout(lst)
        v.setContentsMargins(36, 28, 36, 20)
        v.addWidget(self._header("システム", "項目を選ぶと詳細が開きます"))
        v.addSpacing(10)
        sc = QScrollArea()                      # 項目が増えても画面を圧迫しないようスクロールできる
        sc.setWidgetResizable(True)
        host = QWidget()
        host.setObjectName("host")
        hl = QVBoxLayout(host)
        hl.setContentsMargins(0, 0, 12, 0)
        hl.setSpacing(6)
        sc.setWidget(host)
        v.addWidget(sc, 1)
        self.sys_stack.addWidget(lst)
        pages = [   # (キー, 題名, 説明, 作る関数, 親キー=None)
            ("startup", "起動", "起動時の動作", self._sys_startup, None),
            ("visibility", "表示するタイミング", "ゲームが最前面のときだけHUDを出す など", self._sys_visibility, None),
            ("shortcuts", "便利ショートカットキー", "マイクミュート など", self._sys_shortcut_list, None),
            ("mic", "マイクミュート", "Ctrl+Shift+M / 状態 / 表示の詳細設定", self._sys_shortcuts, "shortcuts"),
            ("mic_look", "ミュート表示 ― 見た目", "大きさ・赤/緑の枠・位置", self._sys_mic_look, "mic"),
            ("mic_fade", "ミュート表示 ― フェード", "全体 / 縁取りだけ消す設定", self._sys_mic_fade, "mic"),
            ("mic_sound", "ミュート表示 ― 音", "切り替え音の種類・音量", self._sys_mic_sound, "mic"),
            ("language", "言語設定", "設定画面の表示言語", self._sys_language, None),
            ("color", "アプリの色", "設定画面全体の色合い", self._sys_color, None),
            ("engine", "FPS測定エンジン", "PresentMonの状態と診断ログ", self._sys_engine, None),
            ("data", "データ", "設定フォルダと初期化", self._sys_data, None),
        ]
        for key, title, desc, build, parent in pages:
            self._sys_idx[key] = self._sys_sub(title, build, parent)
            if parent is None:
                self._sys_row(hl, title, desc, key)
        self.sys_shortcut_idx = self._sys_idx["mic"]        # このページを開いている間だけマイク状態を読み直す
        hl.addStretch()
        return self.sys_stack

    def _sys_shortcut_list(self, lay):
        self._sys_row(lay, "マイクミュート", "Ctrl+Shift+M / 状態 / 表示の詳細設定", "mic")

    def _sys_startup(self, lay):
        p = Panel("起動")
        t = Pill(small=True)
        t.set_on(self.cfg.app["show_on_start"])
        t.toggled.connect(lambda on: self.cfg.set_app("show_on_start", on))
        p.add_row("起動時にこの設定画面を開く", t, "OFFにするとタスクトレイに入ったまま起動します")
        lay.addWidget(p)
        p2 = Panel("詳細設定")
        lk = Pill(small=True)
        lk.set_on(self.cfg.app.get("lock_detail", True))
        lk.toggled.connect(lambda on: self.cfg.set_app("lock_detail", on))
        p2.add_row("詳細設定中は他のウィンドウに切り替えさせない", lk,
                   "詳細設定(プレビュー表示)を開いている間は、他のウィンドウを選んでも音が鳴って設定画面に戻ります。"
                   "「‹ 戻る」で解除されます。")
        lay.addWidget(p2)

    def _sys_visibility(self, lay):
        p = Panel("表示するタイミング")
        t = Pill(small=True)
        t.set_on(self.cfg.app.get("game_only", True))
        t.toggled.connect(lambda on: self.cfg.set_app("game_only", on))
        p.add_row("ゲームが最前面のときだけHUDを表示", t,
                  "ONにすると、下の「対象のゲーム」が画面の一番手前にあるときだけHUDを出します。"
                  "NavyHUDの設定画面を開いている間は常に表示されます。")
        le = QLineEdit(self.cfg.app.get("game_names", ""))
        le.setFixedWidth(300)
        le.textChanged.connect(lambda txt: self.cfg.set_app("game_names", txt.strip()))
        self.sys_game_edit = le
        p.add_row("対象のゲーム (プロセス名)", le,
                  "カンマ(,)で区切って複数指定できます。統合版は Minecraft.Windows.exe、Java版は javaw.exe です。")
        lay.addWidget(p)
        p2 = Panel("対象のゲームに追加")
        self.sys_lastfg = QLabel("")
        self.sys_lastfg.setObjectName("status")
        self.sys_lastfg.setProperty("dyn", True)
        self.sys_lastfg.setMinimumWidth(220)
        p2.add_row("直前に最前面だったアプリ", self.sys_lastfg,
                   "ゲームを前面にしてからNavyHUDに戻ると、そのゲームの名前がここに出ます。")
        b = QPushButton("対象のゲームに追加")
        b.clicked.connect(self._add_last_game)
        p2.add_row("HUDが出ないとき", b, "上の名前がゲームなら、このボタンで対象に加えられます。")
        lay.addWidget(p2)

    def _sys_shortcuts(self, lay):
        p = Panel("マイクミュート")
        t = Pill(small=True)
        t.set_on(self.cfg.app.get("mic_shortcut", False))
        t.toggled.connect(self._set_mic_shortcut)
        self.mic_pill = t
        p.add_row("マイクミュートのショートカット (Ctrl+Shift+M)", t,
                  "ONにすると、どのアプリが前面でも Ctrl+Shift+M を押すたびにマイクのミュート/解除を切り替えます。"
                  "対象はWindowsの「既定の録音デバイス」です。")
        self.mic_status = QLabel("")
        self.mic_status.setObjectName("status")
        self.mic_status.setProperty("dyn", True)
        self.mic_status.setMinimumWidth(220)
        p.add_row("マイクの状態", self.mic_status, "この画面を開いている間、Windows側の状態に合わせて更新されます。")
        lay.addWidget(p)
        t2 = QLabel(T("詳細設定"))
        t2.setObjectName("paneltitle")
        lay.addWidget(t2)
        self._sys_row(lay, "ミュート表示 ― 見た目", "大きさ・赤/緑の枠・位置", "mic_look")
        self._sys_row(lay, "ミュート表示 ― フェード", "全体 / 縁取りだけ消す設定", "mic_fade")
        self._sys_row(lay, "ミュート表示 ― 音", "切り替え音の種類・音量", "mic_sound")

    def _num_spin(self, lo, hi, val, suffix, slot, dbl=False):
        sp = NoWheelDoubleSpin() if dbl else NoWheelSpin()
        sp.setRange(lo, hi)
        if dbl:
            sp.setSingleStep(0.5)
            sp.setDecimals(1)
        sp.setSuffix(T(suffix))
        sp.setFixedWidth(110)
        sp.setAlignment(Qt.AlignRight)
        sp.setValue(val)
        sp.valueChanged.connect(slot)
        return sp

    def _sys_mic_look(self, lay):
        p = Panel("大きさ・枠")
        self.mic_scale_spin = self._num_spin(30, 500, int(self.cfg.app.get("mic_ind_scale", 100)), " %",
                                             lambda v: self.cfg.set_app("mic_ind_scale", int(v)))
        p.add_row("表示の大きさ", self.mic_scale_spin, "100%が標準です。")
        bp = Pill(small=True)
        bp.set_on(self.cfg.app.get("mic_border", True))
        bp.toggled.connect(lambda on: self.cfg.set_app("mic_border", bool(on)))
        self.mic_border_pill = bp
        p.add_row("赤・緑の枠と縁取り", bp, "ミュート=赤、解除=緑の枠と、外側のぼやけた縁取りを付けます。")
        lay.addWidget(p)
        p2 = Panel("位置 (画面左上からのpx)")
        ind = self.ctrl.mic_ind
        self.mic_x_spin = self._num_spin(-5000, 20000, ind.x(), " px", lambda v: self._set_mic_pos())
        self.mic_y_spin = self._num_spin(-5000, 20000, ind.y(), " px", lambda v: self._set_mic_pos())
        p2.add_row("X 位置", self.mic_x_spin, "Ctrl+ドラッグでも動かせます (見えているときだけ)。")
        p2.add_row("Y 位置", self.mic_y_spin)
        b = QPushButton("表示位置を右上に戻す")
        b.clicked.connect(self._reset_mic_pos)
        p2.add_row("初期位置", b, "初期の画面右上に戻します。")
        lay.addWidget(p2)

    def _sys_mic_fade(self, lay):
        p = Panel("全体のフェードアウト")
        f = Pill(small=True)
        f.set_on(self.cfg.app.get("mic_fade", False))
        f.toggled.connect(self._set_mic_fade)
        self.mic_fade_pill = f
        p.add_row("切り替え時にだんだん消す", f,
                  "ONにすると、切り替えた後に表示全体がだんだん消えます。OFFなら出しっぱなしです。")
        self.mic_fade_spin = self._num_spin(0.1, 30.0, float(self.cfg.app.get("mic_fade_sec", 2.0)), " 秒",
                                            lambda v: self.cfg.set_app("mic_fade_sec", float(v)), dbl=True)
        self.mic_fade_spin.setEnabled(self.cfg.app.get("mic_fade", False))
        p.add_row("フェードアウトの秒数", self.mic_fade_spin, "消えるまでにかける時間です (最初の約0.6秒は消え始めません)。")
        lay.addWidget(p)
        p2 = Panel("縁取りだけのフェードアウト")
        bf = Pill(small=True)
        bf.set_on(self.cfg.app.get("mic_border_fade", False))
        bf.toggled.connect(self._set_mic_border_fade)
        self.mic_bfade_pill = bf
        p2.add_row("縁取りだけフェードアウト", bf, "ONにすると、切り替え後に枠と縁取りだけがだんだん消えます (🎙●は残ります)。")
        self.mic_bfade_spin = self._num_spin(0.1, 30.0, float(self.cfg.app.get("mic_border_fade_sec", 1.5)), " 秒",
                                             lambda v: self.cfg.set_app("mic_border_fade_sec", float(v)), dbl=True)
        self.mic_bfade_spin.setEnabled(self.cfg.app.get("mic_border_fade", False))
        p2.add_row("縁取りのフェード秒数", self.mic_bfade_spin, "枠が消えるまでにかける時間です (最初の約0.6秒は消え始めません)。")
        lay.addWidget(p2)

    def _sys_mic_sound(self, lay):
        p = Panel("切り替え音")
        sd = Pill(small=True)
        sd.set_on(self.cfg.app.get("mic_sound", True))
        sd.toggled.connect(self._set_mic_sound)
        self.mic_sound_pill = sd
        p.add_row("切り替え音", sd, "ミュートで下がる音、解除で上がる音を鳴らします。")
        cb = NoWheelCombo()
        for name in MIC_SOUND_STYLES:
            cb.addItem(T(name))
        cb.setCurrentIndex(max(0, min(len(MIC_SOUND_STYLES) - 1, int(self.cfg.app.get("mic_sound_style", 0)))))
        cb.setFixedWidth(130)
        cb.currentIndexChanged.connect(lambda i: self.cfg.set_app("mic_sound_style", int(i)))
        self.mic_style_combo = cb
        p.add_row("音の種類", cb, "チャイム=やわらかい2音 / ポップ=ぽよんと1回 / ピコ=短い2連 / ソフト=低めでやさしい2音 / レトロ=ゲーム機風の3音 / クリック=ごく短いカチッ。")
        self.mic_vol_spin = self._num_spin(0, 100, int(self.cfg.app.get("mic_sound_vol", 50)), " %",
                                           lambda v: self.cfg.set_app("mic_sound_vol", int(v)))
        p.add_row("音量", self.mic_vol_spin, "試し聞きにも反映されます。")
        lay.addWidget(p)
        p2 = Panel("試し聞き")
        for label, muted in (("ミュートの音を聞く", True), ("解除の音を聞く", False)):
            b = QPushButton(label)
            b.clicked.connect(lambda _=False, m=muted: play_mic_sound(
                m, int(self.cfg.app.get("mic_sound_vol", 50)), int(self.cfg.app.get("mic_sound_style", 0))))
            p2.add_row(label, b)
        lay.addWidget(p2)

    def _set_mic_pos(self):
        self.cfg.set_app("mic_ind_x", int(self.mic_x_spin.value()))
        self.cfg.set_app("mic_ind_y", int(self.mic_y_spin.value()))
        self.ctrl.mic_ind.move_to_saved()

    def _set_mic_shortcut(self, on):
        self.cfg.set_app("mic_shortcut", bool(on))
        if on:
            self.ctrl.mic.refresh()

    def _set_mic_border_fade(self, on):
        self.cfg.set_app("mic_border_fade", bool(on))
        self.mic_bfade_spin.setEnabled(bool(on))

    def _set_mic_sound(self, on):
        self.cfg.set_app("mic_sound", bool(on))

    def _set_mic_fade(self, on):
        self.cfg.set_app("mic_fade", bool(on))
        self.mic_fade_spin.setEnabled(bool(on))

    def _reset_mic_pos(self):
        self.cfg.set_app("mic_ind_x", MIC_IND_AUTO)
        self.cfg.set_app("mic_ind_y", MIC_IND_AUTO)
        self.ctrl.mic_ind.move_to_saved()
        self._sync_mic_pos_spins(force=True)

    def _sync_mic_pos_spins(self, force=False):
        """Ctrl+ドラッグで動かした位置を、X/Yの入力欄に反映する"""
        ind = self.ctrl.mic_ind
        for sp, v in ((self.mic_x_spin, ind.x()), (self.mic_y_spin, ind.y())):
            if sp.value() != v and (force or not sp.hasFocus()):
                sp.blockSignals(True)
                sp.setValue(v)
                sp.blockSignals(False)

    def _add_last_game(self):
        name = self.ctrl.last_fg_name
        if not name:
            return
        names = self.ctrl.game_names()
        if _app_key(name) in names:
            return
        cur = self.cfg.app.get("game_names", "").strip().rstrip(",")
        self.sys_game_edit.setText((cur + ", " if cur else "") + name)     # 入力欄の変更で自動保存される

    def _sys_language(self, lay):
        p = Panel("言語設定")
        box = QWidget()
        h = QHBoxLayout(box)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(8)
        grp = QButtonGroup(self)
        grp.setExclusive(True)
        self.lang_btns = {}
        for code, name in (("ja", "日本語"), ("en", "English")):
            b = QPushButton(name)
            b.setObjectName("pill")
            b.setCheckable(True)
            b.setFixedSize(100, 28)
            b.setCursor(Qt.PointingHandCursor)
            b.setChecked(code == LANG)
            b.clicked.connect(lambda _=False, c=code: self.set_language(c))
            grp.addButton(b)
            h.addWidget(b)
            self.lang_btns[code] = b
        p.add_row("言語", box, "設定画面で使う言語を選びます (HUDの表示は変わりません)")
        lay.addWidget(p)

    COLOR_CHOICES = [("#3b82f6", "ブルー"), ("#8b5cf6", "パープル"), ("#ec4899", "ピンク"),
                     ("#22c55e", "グリーン"), ("#f59e0b", "オレンジ"), ("#9ca3af", "グレー")]

    def _sys_color(self, lay):
        p = Panel("色合い")
        box = QWidget()
        h = QHBoxLayout(box)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(8)
        for hexv, _name in self.COLOR_CHOICES:
            b = QPushButton()
            b.setFixedSize(30, 30)
            b.setCursor(Qt.PointingHandCursor)
            b.setStyleSheet("QPushButton { background: %s; border: 2px solid #ffffff; border-radius: 15px; padding: 0; }"
                            "QPushButton:hover { border: 2px solid #4aa3ff; }" % hexv)
            b.clicked.connect(lambda _=False, c=hexv: self.set_ui_color(c))
            h.addWidget(b)
        p.add_row("アプリの色を選ぶ", box, "設定画面全体の色合いを変えます (HUDの見た目には影響しません)")
        lay.addWidget(p)
        p2 = Panel()
        b1 = QPushButton("好きな色を選ぶ…")

        def pick():
            col = QColorDialog.getColor(QColor(UI_COLOR or "#3b82f6"), self, T("色を選択"))
            if col.isValid():
                self.set_ui_color(col.name())
        b1.clicked.connect(pick)
        p2.add_row("好きな色を選ぶ…", b1)
        b2 = QPushButton("初期の色(ネイビー)に戻す")
        b2.clicked.connect(lambda: self.set_ui_color(""))
        p2.add_row("初期の色(ネイビー)に戻す", b2)
        lay.addWidget(p2)

    def _sys_engine(self, lay):
        p2 = Panel("FPS測定エンジン (PresentMon)")
        self.sys_engine = QLabel("")
        self.sys_engine.setObjectName("status")
        self.sys_engine.setProperty("dyn", True)
        self.sys_engine.setWordWrap(True)
        self.sys_engine.setMinimumWidth(300)
        p2.add_row("状態", self.sys_engine)
        if self.ctrl.pm.log_path:
            bl = QPushButton("診断ログを開く")
            bl.clicked.connect(lambda: os.startfile(self.ctrl.pm.log_path))
            p2.add_row("診断ログ", bl, "FPSが測れないとき、原因がここに記録されます")
        adm = QLabel("管理者として実行中" if is_admin() else "管理者ではありません")
        adm.setObjectName("status")
        p2.add_row("権限", adm, "FPS測定には管理者権限が必要です")
        lay.addWidget(p2)

    def _sys_data(self, lay):
        p3 = Panel("データ")
        b1 = QPushButton("設定フォルダを開く")
        b1.clicked.connect(lambda: os.startfile(self.cfg.folder))
        p3.add_row("設定ファイルの場所", b1, self.cfg.path)
        b2 = QPushButton("すべて初期化")
        b2.setObjectName("danger")
        b2.clicked.connect(self._reset_all)
        p3.add_row("すべての設定を初期値に戻す", b2,
                   "HUDの見た目・位置・キー割り当てが初期化されます (保存したプリセットは残ります)")
        lay.addWidget(p3)

    def _reset_all(self):
        if QMessageBox.question(self, T("確認"), T("すべての設定を初期値に戻します。よろしいですか?")) \
                == QMessageBox.Yes:
            self.close_detail(restore_list=True)
            self.cfg.reset_all()
            for hid, card in self.cards.items():
                card.toggle.set_on(self.cfg.hud(hid)["enabled"])
            self.ctrl.apply_visibility()
            self.mic_pill.set_on(self.cfg.app.get("mic_shortcut", False))
            self.mic_fade_pill.set_on(self.cfg.app.get("mic_fade", False))
            self.mic_fade_spin.setEnabled(self.cfg.app.get("mic_fade", False))
            self.mic_fade_spin.setValue(float(self.cfg.app.get("mic_fade_sec", 2.0)))
            self.mic_border_pill.set_on(self.cfg.app.get("mic_border", True))
            self.mic_bfade_pill.set_on(self.cfg.app.get("mic_border_fade", False))
            self.mic_bfade_spin.setEnabled(self.cfg.app.get("mic_border_fade", False))
            self.mic_bfade_spin.setValue(float(self.cfg.app.get("mic_border_fade_sec", 1.5)))
            self.mic_scale_spin.setValue(int(self.cfg.app.get("mic_ind_scale", 100)))
            self.mic_sound_pill.set_on(self.cfg.app.get("mic_sound", True))
            self.mic_vol_spin.setValue(int(self.cfg.app.get("mic_sound_vol", 50)))
            self.mic_style_combo.setCurrentIndex(int(self.cfg.app.get("mic_sound_style", 0)))
            self.ctrl.mic_ind.move_to_saved()
            self._sync_mic_pos_spins(force=True)

    def _refresh_status(self):
        if self.isVisible():
            text = self.ctrl.fps_text or self.ctrl.pm.status_text()
            self.sys_engine.setText(text)
            self.sys_lastfg.setText(self.ctrl.last_fg_name or "-")
            self._sync_mic_pos_spins()
            if (self.stack.currentWidget() is self.system_page
                    and self.sys_stack.currentIndex() == self.sys_shortcut_idx):
                now = time.monotonic()
                if now - self._mic_polled >= 1.5:       # 開いている間だけ、Windows側のミュート状態を読み直す
                    self._mic_polled = now
                    self.ctrl.mic.refresh()
                self.mic_status.setText(self.ctrl.mic.status_text())

    # ---- ページ切り替え ----
    def show_page(self, key):
        self.close_detail(restore_list=False)
        self.ctrl.set_preview(None)
        self.ctrl.set_preset_preview(None)
        self.sel_preset = None
        page = {"hud": self.list_page, "presets": self.preset_page}.get(key, self.system_page)
        if key == "presets":
            self._rebuild_presets()
        if key == "system":
            self.sys_stack.setCurrentIndex(0)        # いつも項目の一覧から始める
        self.stack.setCurrentWidget(page)
        self.nav_btns[key].setChecked(True)

    def open_detail(self, hid, tab=0):
        self.close_detail(restore_list=False)
        self.detail = DetailPage(self.cfg, self.ctrl, hid, tab)
        self.detail.back.connect(lambda: self.show_page("hud"))
        self.detail.reload.connect(self._reload_detail)
        self.stack.addWidget(self.detail)
        self.stack.setCurrentWidget(self.detail)
        self.nav_btns["hud"].setChecked(True)
        self.ctrl.set_preview(hid)

    def _reload_detail(self):
        if self.detail is None:
            return
        hid, tab = self.detail.hid, self.detail.tabs.currentIndex()
        self.open_detail(hid, tab)

    def close_detail(self, restore_list=True):
        if self.detail is not None:
            self.stack.removeWidget(self.detail)
            self.detail.deleteLater()
            self.detail = None
        if restore_list:
            self.stack.setCurrentWidget(self.list_page)
            self.ctrl.set_preview(None)

    def showEvent(self, e):
        super().showEvent(e)
        self._paint_titlebar()

    def _paint_titlebar(self):
        try:  # タイトルバーもアプリの色に (Windows 10/11)
            dwm = ctypes.WinDLL("dwmapi")
            hwnd = int(self.winId())
            one = ctypes.c_int(1)
            dwm.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(one), 4)
            c = QColor(theme_hex("#070d1c"))
            col = ctypes.c_int(c.red() | (c.green() << 8) | (c.blue() << 16))     # BGR
            dwm.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(col), 4)
        except Exception:
            pass

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.ctrl.reposition_preview()

    def moveEvent(self, e):
        super().moveEvent(e)
        self.ctrl.reposition_preview()

    def closeEvent(self, e):
        # ×で閉じてもHUDは動かし続ける (タスクトレイに常駐)
        e.ignore()
        self.show_page("hud")
        self.hide()


# ======================================================================
def main():
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("NavyHUD.App")
    except Exception:
        pass
    if not getattr(sys, "frozen", False) and not is_admin():
        # FPS測定(ETW)は管理者権限が必要。python から直接起動した場合は自動で昇格し直す
        try:
            args = " ".join('"%s"' % a for a in [os.path.abspath(sys.argv[0])] + sys.argv[1:])
            if ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, args, None, 1) > 32:
                sys.exit(0)
        except SystemExit:
            raise
        except Exception:
            pass
    kill_stale_instances()
    if not acquire_single_instance():
        user32.MessageBoxW(None, "NavyHUD は既に起動しています。\nタスクトレイ(画面右下)のアイコンから設定を開く/終了してください。",
                           "NavyHUD", 0x40)
        sys.exit(0)
    winmm.timeBeginPeriod(1)  # 5ms周期を安定させる

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName(APP_NAME)
    icon = QIcon(resource("icon.ico"))
    app.setWindowIcon(icon)

    cfg = Config()
    set_lang(cfg.app.get("language", "ja"))
    set_ui_color(cfg.app.get("ui_color", ""))
    ctrl = Controller(cfg)
    app.aboutToQuit.connect(ctrl.shutdown)
    settings = SettingsWindow(cfg, ctrl, icon)
    ctrl.settings = settings

    tray = QSystemTrayIcon(icon, app)
    tray.setToolTip(APP_NAME)
    menu = QMenu()
    MENU_QSS = ("QMenu { background: #0d1833; color: #d6e4ff; border: 1px solid #28427f; padding: 4px; }"
                "QMenu::item { padding: 7px 26px; border-radius: 6px; }"
                "QMenu::item:selected { background: #2a4a8c; }"
                "QMenu::separator { height: 1px; background: #17274f; margin: 4px 8px; }")
    act_open = QAction(T("設定を開く"), menu)
    act_quit = QAction(T("終了"), menu)

    def refresh_menu():             # 言語・色が変わったらトレイメニューも合わせる
        menu.setStyleSheet(themed(MENU_QSS))
        act_open.setText(T("設定を開く"))
        act_quit.setText(T("終了"))
    refresh_menu()
    settings.hooks.append(refresh_menu)

    def show_settings():
        settings.show()
        settings.raise_()
        settings.activateWindow()

    def quit_app():
        cfg.save()
        ctrl.shutdown()
        tray.hide()
        app.quit()

    act_open.triggered.connect(show_settings)
    act_quit.triggered.connect(quit_app)
    menu.addAction(act_open)
    menu.addSeparator()
    menu.addAction(act_quit)
    tray.setContextMenu(menu)
    tray.activated.connect(lambda r: show_settings() if r in (
        QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick) else None)
    tray.show()

    if cfg.app["show_on_start"]:
        settings.show()
    code = app.exec()
    winmm.timeEndPeriod(1)
    sys.exit(code)


if __name__ == "__main__":
    main()
