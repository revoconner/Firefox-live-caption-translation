"""Notification area icon for the background process, built on the Win32 API through ctypes so no third party package is needed.

The icon shows the backend state as its tooltip and offers a menu with the state, the log folder and Quit. It runs its own message loop on a daemon thread; other threads change the tooltip through set_status, which posts a message to that thread.
"""
from __future__ import annotations

import ctypes as C
import logging
import os
import threading
from ctypes import wintypes as W
from pathlib import Path
from typing import Callable

log = logging.getLogger("captions.tray")

user32 = C.WinDLL("user32", use_last_error=True)
shell32 = C.WinDLL("shell32", use_last_error=True)
kernel32 = C.WinDLL("kernel32", use_last_error=True)

WM_NULL = 0x0000
WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_LBUTTONUP = 0x0202
WM_RBUTTONUP = 0x0205
WM_APP = 0x8000
WM_TRAY = WM_APP + 1  # the icon's callback message
WM_REFRESH = WM_APP + 2  # posted by set_status from any thread
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP = 1, 2, 4
MF_STRING, MF_GRAYED, MF_SEPARATOR = 0x0, 0x1, 0x800
TPM_RIGHTBUTTON, TPM_NONOTIFY, TPM_RETURNCMD = 0x2, 0x80, 0x100
IMAGE_ICON, LR_LOADFROMFILE = 1, 0x10
IDI_APPLICATION = 32512
SM_CXSMICON = 49
ID_STATUS, ID_LOGS, ID_QUIT = 1, 2, 3

LRESULT = C.c_ssize_t
WNDPROC = C.WINFUNCTYPE(LRESULT, W.HWND, W.UINT, W.WPARAM, W.LPARAM)


class GUID(C.Structure):
    _fields_ = [("data1", W.DWORD), ("data2", W.WORD), ("data3", W.WORD), ("data4", W.BYTE * 8)]


class NOTIFYICONDATAW(C.Structure):
    _fields_ = [("cbSize", W.DWORD), ("hWnd", W.HWND), ("uID", W.UINT), ("uFlags", W.UINT), ("uCallbackMessage", W.UINT), ("hIcon", W.HICON), ("szTip", W.WCHAR * 128), ("dwState", W.DWORD), ("dwStateMask", W.DWORD), ("szInfo", W.WCHAR * 256), ("uVersion", W.UINT), ("szInfoTitle", W.WCHAR * 64), ("dwInfoFlags", W.DWORD), ("guidItem", GUID), ("hBalloonIcon", W.HICON)]


class WNDCLASSW(C.Structure):
    _fields_ = [("style", W.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", C.c_int), ("cbWndExtra", C.c_int), ("hInstance", W.HINSTANCE), ("hIcon", W.HICON), ("hCursor", W.HANDLE), ("hbrBackground", W.HBRUSH), ("lpszMenuName", W.LPCWSTR), ("lpszClassName", W.LPCWSTR)]


user32.DefWindowProcW.restype = LRESULT
user32.DefWindowProcW.argtypes = [W.HWND, W.UINT, W.WPARAM, W.LPARAM]
user32.RegisterClassW.restype = W.ATOM
user32.RegisterClassW.argtypes = [C.POINTER(WNDCLASSW)]
user32.CreateWindowExW.restype = W.HWND
user32.CreateWindowExW.argtypes = [W.DWORD, W.LPCWSTR, W.LPCWSTR, W.DWORD, C.c_int, C.c_int, C.c_int, C.c_int, W.HWND, W.HMENU, W.HINSTANCE, W.LPVOID]
user32.DestroyWindow.argtypes = [W.HWND]
user32.PostMessageW.argtypes = [W.HWND, W.UINT, W.WPARAM, W.LPARAM]
user32.PostQuitMessage.argtypes = [C.c_int]
user32.GetMessageW.argtypes = [C.POINTER(W.MSG), W.HWND, W.UINT, W.UINT]
user32.TranslateMessage.argtypes = [C.POINTER(W.MSG)]
user32.DispatchMessageW.restype = LRESULT
user32.DispatchMessageW.argtypes = [C.POINTER(W.MSG)]
user32.RegisterWindowMessageW.restype = W.UINT
user32.RegisterWindowMessageW.argtypes = [W.LPCWSTR]
user32.CreatePopupMenu.restype = W.HMENU
user32.AppendMenuW.argtypes = [W.HMENU, W.UINT, C.c_size_t, W.LPCWSTR]
user32.DestroyMenu.argtypes = [W.HMENU]
user32.TrackPopupMenu.restype = W.BOOL
user32.TrackPopupMenu.argtypes = [W.HMENU, W.UINT, C.c_int, C.c_int, C.c_int, W.HWND, W.LPVOID]
user32.GetCursorPos.argtypes = [C.POINTER(W.POINT)]
user32.SetForegroundWindow.argtypes = [W.HWND]
user32.GetSystemMetrics.argtypes = [C.c_int]
user32.LoadImageW.restype = W.HANDLE
user32.LoadImageW.argtypes = [W.HINSTANCE, W.LPCWSTR, W.UINT, C.c_int, C.c_int, W.UINT]
user32.LoadIconW.restype = W.HICON
user32.LoadIconW.argtypes = [W.HINSTANCE, W.LPVOID]
shell32.Shell_NotifyIconW.restype = W.BOOL
shell32.Shell_NotifyIconW.argtypes = [W.DWORD, C.POINTER(NOTIFYICONDATAW)]
kernel32.GetModuleHandleW.restype = W.HMODULE
kernel32.GetModuleHandleW.argtypes = [W.LPCWSTR]


class TrayIcon:
    def __init__(self, title: str, icon_path: Path | None, on_quit: Callable[[], None], log_dir: Path | None = None) -> None:
        self.title = title
        self.icon_path = icon_path
        self.on_quit = on_quit
        self.log_dir = log_dir
        self.status = "starting"
        self.hwnd: int | None = None
        self.hicon = None
        self._proc = WNDPROC(self._wndproc)  # keeps the callback alive for the window lifetime
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._taskbar_created = 0

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="tray", daemon=True)
        self._thread.start()
        self._ready.wait(5)

    def stop(self) -> None:
        if self.hwnd:
            user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)
        if self._thread:
            self._thread.join(2)

    def set_status(self, text: str) -> None:
        self.status = text
        if self.hwnd:
            user32.PostMessageW(self.hwnd, WM_REFRESH, 0, 0)

    def _data(self, flags: int) -> NOTIFYICONDATAW:
        d = NOTIFYICONDATAW()
        d.cbSize = C.sizeof(d)
        d.hWnd = self.hwnd
        d.uID = 1
        d.uFlags = flags
        d.uCallbackMessage = WM_TRAY
        d.hIcon = self.hicon
        d.szTip = f"{self.title}: {self.status}"[:127]
        return d

    def _add(self) -> None:
        if not shell32.Shell_NotifyIconW(NIM_ADD, C.byref(self._data(NIF_MESSAGE | NIF_ICON | NIF_TIP))):
            log.warning("tray icon not added (error %d)", C.get_last_error())

    def _load_icon(self):
        if self.icon_path and Path(self.icon_path).is_file():
            size = user32.GetSystemMetrics(SM_CXSMICON)
            h = user32.LoadImageW(None, str(self.icon_path), IMAGE_ICON, size, size, LR_LOADFROMFILE)
            if h:
                return h
            log.warning("icon %s not loaded (error %d)", self.icon_path, C.get_last_error())
        return user32.LoadIconW(None, IDI_APPLICATION)

    def _run(self) -> None:
        try:
            hinst = kernel32.GetModuleHandleW(None)
            wc = WNDCLASSW()
            wc.lpfnWndProc = self._proc
            wc.hInstance = hinst
            wc.lpszClassName = "LiveCaptionTranslateTray"
            if not user32.RegisterClassW(C.byref(wc)):
                log.warning("tray window class not registered (error %d)", C.get_last_error())
                return
            self.hicon = self._load_icon()
            self._taskbar_created = user32.RegisterWindowMessageW("TaskbarCreated")
            self.hwnd = user32.CreateWindowExW(0, wc.lpszClassName, self.title, 0, 0, 0, 0, 0, None, None, hinst, None)
            if not self.hwnd:
                log.warning("tray window not created (error %d)", C.get_last_error())
                return
            self._add()
        finally:
            self._ready.set()
        msg = W.MSG()
        while user32.GetMessageW(C.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(C.byref(msg))
            user32.DispatchMessageW(C.byref(msg))
        self.hwnd = None

    def _menu(self, hwnd: int) -> None:
        menu = user32.CreatePopupMenu()
        user32.AppendMenuW(menu, MF_STRING | MF_GRAYED, ID_STATUS, self.status[:80] or "starting")
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        if self.log_dir:
            user32.AppendMenuW(menu, MF_STRING, ID_LOGS, "Open log folder")
        user32.AppendMenuW(menu, MF_STRING, ID_QUIT, "Quit")
        pt = W.POINT()
        user32.GetCursorPos(C.byref(pt))
        user32.SetForegroundWindow(hwnd)  # without this the menu stays open when the user clicks elsewhere
        cmd = user32.TrackPopupMenu(menu, TPM_RIGHTBUTTON | TPM_NONOTIFY | TPM_RETURNCMD, pt.x, pt.y, 0, hwnd, None)
        user32.PostMessageW(hwnd, WM_NULL, 0, 0)  # recommended after TrackPopupMenu on a notification icon
        user32.DestroyMenu(menu)
        if cmd == ID_QUIT:
            log.info("quit requested from the tray")
            self.on_quit()
        elif cmd == ID_LOGS and self.log_dir:
            try:
                os.startfile(str(self.log_dir))
            except OSError as e:
                log.warning("cannot open %s: %s", self.log_dir, e)

    def _wndproc(self, hwnd, msg, wparam, lparam):
        if msg == WM_TRAY:
            if lparam in (WM_LBUTTONUP, WM_RBUTTONUP):
                self._menu(hwnd)
            return 0
        if msg == WM_REFRESH:
            shell32.Shell_NotifyIconW(NIM_MODIFY, C.byref(self._data(NIF_TIP)))
            return 0
        if msg == self._taskbar_created and msg:
            self._add()  # explorer restarted and lost every icon
            return 0
        if msg == WM_CLOSE:
            user32.DestroyWindow(hwnd)
            return 0
        if msg == WM_DESTROY:
            shell32.Shell_NotifyIconW(NIM_DELETE, C.byref(self._data(0)))
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)
