# -*- coding: utf-8 -*-
"""
hotkeys_mac.py — macOS版グローバルキーフック。hotkeys.py (Windows) と同じ API。

pynput (Quartz イベントタップ) を使うが、pynput は pywebview の Cocoa
イベントループと同一プロセスで共存できない（ブリッジが死ぬ/クラッシュする）
ため、キー監視は別プロセスのワーカーで行い、stdout パイプで
"D <idx>" / "U <idx>" を親へ通知する。

swallow=True のときは対象キーを他アプリへ渡さない。

必要な macOS 権限:
  - 入力監視 (Input Monitoring) / アクセシビリティ:
    システム設定 > プライバシーとセキュリティ で起動元アプリ
    (ターミナル等) を許可する。権限が無くてもアプリ本体は
    画面ボタンだけで動き続ける。
"""

import json
import os
import queue
import subprocess
import sys
import threading

# macOS のキーコード: テンキー 1〜4
VK_NUMPAD1 = 83
VK_NUMPAD2 = 84
VK_NUMPAD3 = 85
VK_NUMPAD4 = 86

NUMPAD_KEYS = [VK_NUMPAD1, VK_NUMPAD2, VK_NUMPAD3, VK_NUMPAD4]


class HotkeyListener:
    """
    key_map: {キーコード: index}
    on_down(index) / on_up(index) が呼ばれる。
    swallow=True なら対象キーを他アプリへ渡さない。
    """

    def __init__(self, key_map, on_down, on_up, swallow=True, ignore_injected=True):
        self.key_map = dict(key_map)
        self.on_down = on_down
        self.on_up = on_up
        self.swallow = swallow
        self._proc = None

    def _reader(self):
        try:
            for line in self._proc.stdout:
                line = line.strip()
                if line.startswith("D "):
                    try:
                        self.on_down(int(line[2:]))
                    except Exception:
                        pass
                elif line.startswith("U "):
                    try:
                        self.on_up(int(line[2:]))
                    except Exception:
                        pass
        except Exception:
            pass

    def start(self):
        cfg = json.dumps({"map": {str(k): v for k, v in self.key_map.items()},
                          "swallow": self.swallow})
        try:
            self._proc = subprocess.Popen(
                [sys.executable, os.path.abspath(__file__), "--hotkey-worker", cfg],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, bufsize=1)
        except Exception:
            return False

        # ワーカーの READY を待つ（権限が無い場合 "READY untrusted" が来る）
        q = queue.Queue()

        def first_line():
            q.put(self._proc.stdout.readline())

        t = threading.Thread(target=first_line, daemon=True)
        t.start()
        try:
            line = q.get(timeout=5.0)
        except queue.Empty:
            self.stop()
            return False
        if not line.startswith("READY"):
            self.stop()
            return False
        if "untrusted" in line:
            print("[hotkeys] 入力監視の権限がありません。テンキーは無効"
                  "（画面ボタンは使えます）", flush=True)
        threading.Thread(target=self._reader, daemon=True).start()
        return True

    def stop(self):
        if self._proc is not None:
            try:
                self._proc.terminate()
            except Exception:
                pass
            self._proc = None


def numpad_map(count, include_numlock_off=True):
    """先頭 count 個の宛先に テンキー1..4 を割り当てるマップを作る。
    (macOS には NumLock が無いため include_numlock_off は無視される)"""
    return {NUMPAD_KEYS[i]: i for i in range(min(count, 4))}


# ============================================================
# ワーカープロセス本体 (pynput はこの別プロセス内でだけ動かす)
# ============================================================

def _worker(cfg):
    import time

    import Quartz
    from pynput import keyboard

    # 親が死んだら自分も終了する（孤児タップがキーを飲み込み続けないように）
    def parent_watch():
        while True:
            if os.getppid() == 1:
                os._exit(0)
            time.sleep(2)

    threading.Thread(target=parent_watch, daemon=True).start()

    key_map = {int(k): v for k, v in cfg["map"].items()}
    swallow = cfg["swallow"]
    held = set()

    def intercept(event_type, event):
        vk = Quartz.CGEventGetIntegerValueField(
            event, Quartz.kCGKeyboardEventKeycode)
        if vk in key_map:
            idx = key_map[vk]
            if event_type == Quartz.kCGEventKeyDown:
                if idx not in held:                 # キーリピートを無視
                    held.add(idx)
                    print(f"D {idx}", flush=True)
            elif event_type == Quartz.kCGEventKeyUp:
                if idx in held:
                    held.discard(idx)
                    print(f"U {idx}", flush=True)
            if swallow:
                return None
        return event

    trusted = True
    try:
        if hasattr(Quartz, "CGPreflightListenEventAccess"):
            trusted = bool(Quartz.CGPreflightListenEventAccess())
    except Exception:
        pass
    print("READY " + ("ok" if trusted else "untrusted"), flush=True)

    with keyboard.Listener(darwin_intercept=intercept) as listener:
        listener.join()


if __name__ == "__main__" and len(sys.argv) >= 3 and sys.argv[1] == "--hotkey-worker":
    try:
        _worker(json.loads(sys.argv[2]))
    except Exception:
        pass
