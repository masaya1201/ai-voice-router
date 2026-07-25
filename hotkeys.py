# -*- coding: utf-8 -*-
"""
hotkeys.py — グローバルキーフック。
アプリが前面でなくても、指定キーを「押している間だけ録音 → 離すと送信」できる。

低レベルキーボードフック(WH_KEYBOARD_LL)を専用スレッドで動かす。
対象キーは他アプリへ渡さない（数字が入力されるのを防ぐ）。
"""

import ctypes
import threading
from ctypes import wintypes

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

WH_KEYBOARD_LL = 13
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
LLKHF_EXTENDED = 0x01
LLKHF_INJECTED = 0x10

# テンキー 1〜9（NumLock ON のとき）
VK_NUMPAD1 = 0x61
VK_NUMPAD2 = 0x62
VK_NUMPAD3 = 0x63
VK_NUMPAD4 = 0x64
VK_NUMPAD5 = 0x65
VK_NUMPAD6 = 0x66
VK_NUMPAD7 = 0x67
VK_NUMPAD8 = 0x68
VK_NUMPAD9 = 0x69

# NumLock OFF のときのテンキーは、方向キーやHome/End等と同じコードになる。
# 1=End 2=↓ 3=PageDown 4=← 5=Clear 6=→ 7=Home 8=↑ 9=PageUp
VK_END = 0x23
VK_DOWN = 0x28
VK_NEXT = 0x22
VK_LEFT = 0x25
VK_CLEAR = 0x0C
VK_RIGHT = 0x27
VK_HOME = 0x24
VK_UP = 0x26
VK_PRIOR = 0x21

NUMPAD_KEYS = [VK_NUMPAD1, VK_NUMPAD2, VK_NUMPAD3, VK_NUMPAD4, VK_NUMPAD5,
               VK_NUMPAD6, VK_NUMPAD7, VK_NUMPAD8, VK_NUMPAD9]
NUMPAD_ALT = [VK_END, VK_DOWN, VK_NEXT, VK_LEFT, VK_CLEAR,
              VK_RIGHT, VK_HOME, VK_UP, VK_PRIOR]
MAX_HOTKEYS = len(NUMPAD_KEYS)


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG)),
    ]


HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
user32.SetWindowsHookExW.restype = wintypes.HHOOK
user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
user32.CallNextHookEx.restype = ctypes.c_long
user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]


class HotkeyListener:
    """
    key_map: {仮想キーコード: index}
    on_down(index) / on_up(index) が呼ばれる。
    swallow=True なら対象キーを他アプリへ渡さない。
    """

    def __init__(self, key_map, on_down, on_up, swallow=True, ignore_injected=True):
        self.key_map = dict(key_map)
        self.on_down = on_down
        self.on_up = on_up
        self.swallow = swallow
        self.ignore_injected = ignore_injected
        self._held = set()
        self._hook = None
        self._thread = None
        self._thread_id = None
        self._proc = None      # GC防止のため保持する
        self.started = threading.Event()

    def _callback(self, nCode, wParam, lParam):
        if nCode == 0:
            try:
                kb = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                injected = bool(kb.flags & LLKHF_INJECTED)
                if not (self.ignore_injected and injected):
                    vk = kb.vkCode
                    # NumLockオフ時のテンキーは方向キー/End等と同じコード。
                    # 本物の方向キーは「拡張キー」なので、それは横取りしない。
                    if vk in NUMPAD_ALT and (kb.flags & LLKHF_EXTENDED):
                        return user32.CallNextHookEx(None, nCode, wParam, lParam)
                    if vk in self.key_map:
                        idx = self.key_map[vk]
                        if wParam in (WM_KEYDOWN, WM_SYSKEYDOWN):
                            if idx not in self._held:      # 押しっぱなしの連打を無視
                                self._held.add(idx)
                                try:
                                    self.on_down(idx)
                                except Exception:
                                    pass
                            if self.swallow:
                                return 1
                        elif wParam in (WM_KEYUP, WM_SYSKEYUP):
                            if idx in self._held:
                                self._held.discard(idx)
                                try:
                                    self.on_up(idx)
                                except Exception:
                                    pass
                            if self.swallow:
                                return 1
            except Exception:
                pass
        return user32.CallNextHookEx(None, nCode, wParam, lParam)

    def _run(self):
        self._thread_id = kernel32.GetCurrentThreadId()
        self._proc = HOOKPROC(self._callback)
        self._hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._proc, None, 0)
        self.started.set()
        if not self._hook:
            return
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        user32.UnhookWindowsHookEx(self._hook)
        self._hook = None

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self.started.wait(3.0)
        return self._hook is not None

    def stop(self):
        if self._thread_id:
            user32.PostThreadMessageW(self._thread_id, 0x0012, 0, 0)  # WM_QUIT


def numpad_map(count, include_numlock_off=True):
    """先頭 count 個の宛先に テンキー1..9 を順に割り当てるマップを作る。
    画面のボタンの並び順とテンキーの数字が一致する。"""
    m = {}
    for i in range(min(count, MAX_HOTKEYS)):
        m[NUMPAD_KEYS[i]] = i
        if include_numlock_off:
            m[NUMPAD_ALT[i]] = i
    return m
