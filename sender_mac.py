# -*- coding: utf-8 -*-
"""
sender_mac.py — macOS版の送信モジュール。sender.py (Windows) と同じ API を提供する。

Windows版は UI Automation で入力欄そのものを特定して検証するが、
macOS では同等の入力欄検証を汎用的に行えないため、方針を変える:

  1. 送信先アプリ / ブラウザのタブを AppleScript で特定して前面化する
  2. "実際に前面になったか" をポーリングで確認する（誤爆防止の要）
  3. クリップボード経由で Cmd+V 貼り付け → Enter (key code 36)

ChatGPT / Claude / Gemini の各Webアプリはページ全体で paste を受けて
入力欄に入れるため、タブが前面にさえなっていれば貼り付けは届く。

必要な macOS 権限:
  - アクセシビリティ: System Events でキー入力を送るため
    (システム設定 > プライバシーとセキュリティ > アクセシビリティ に
     ターミナル/iTerm 等の起動元アプリを追加)
  - オートメーション: 初回にダイアログが出るので「許可」する
"""

import subprocess
import time

import pyperclip

# ============================================================
# AppleScript 実行ヘルパ
# ============================================================

def _osascript(script, timeout=15):
    """AppleScript を実行して (ok, stdout) を返す。"""
    try:
        r = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=timeout,
        )
        return r.returncode == 0, (r.stdout or "").strip(), (r.stderr or "").strip()
    except subprocess.TimeoutExpired:
        return False, "", "AppleScript timeout"


def _esc(s):
    """AppleScript 文字列リテラル用エスケープ。"""
    return (s or "").replace("\\", "\\\\").replace('"', '\\"')


def frontmost_app():
    ok, out, _ = _osascript(
        'tell application "System Events" to get name of '
        'first application process whose frontmost is true')
    return out if ok else ""


def app_running(app_name):
    ok, out, _ = _osascript(f'application "{_esc(app_name)}" is running')
    return ok and out == "true"


def activate_app(app_name, timeout=3.0):
    """アプリを前面化し、実際に前面になるまで待つ。"""
    _osascript(f'tell application "{_esc(app_name)}" to activate')
    deadline = time.time() + timeout
    want = app_name.lower()
    while time.time() < deadline:
        cur = frontmost_app().lower()
        if cur and (cur == want or want in cur or cur in want):
            return True
        time.sleep(0.1)
    return False


def _keystroke_paste():
    return _osascript(
        'tell application "System Events" to keystroke "v" using {command down}')


def _press_return():
    return _osascript('tell application "System Events" to key code 36')


# ============================================================
# ブラウザのタブ選択
# ============================================================
# browser 設定値 → macOS アプリ名
BROWSER_APPS = {
    "chrome": ("Google Chrome",),
    "edge": ("Microsoft Edge",),
    "brave": ("Brave Browser",),
    "vivaldi": ("Vivaldi",),
    "safari": ("Safari",),
    "any": ("Google Chrome", "Microsoft Edge", "Brave Browser",
            "Vivaldi", "Safari"),
}

# Chromium系: タブを URL / タイトルで探して選択し、そのウィンドウを前面へ
_CHROMIUM_SELECT = '''
tell application "{app}"
    set wCount to count of windows
    repeat with wi from 1 to wCount
        set w to window wi
        set tCount to count of tabs of w
        repeat with ti from 1 to tCount
            set t to tab ti of w
            set matched to false
            if "{url}" is not "" and (URL of t contains "{url}") then set matched to true
            if (not matched) and "{tab}" is not "" and (title of t contains "{tab}") then set matched to true
            if matched then
                set active tab index of w to ti
                set index of w to 1
                activate
                return "OK|" & (title of t)
            end if
        end repeat
    end repeat
end tell
return "NG"
'''

# Safari は tab の選択方法とプロパティ名が少し違う
_SAFARI_SELECT = '''
tell application "Safari"
    set wCount to count of windows
    repeat with wi from 1 to wCount
        set w to window wi
        set tCount to count of tabs of w
        repeat with ti from 1 to tCount
            set t to tab ti of w
            set matched to false
            if "{url}" is not "" and (URL of t contains "{url}") then set matched to true
            if (not matched) and "{tab}" is not "" and (name of t contains "{tab}") then set matched to true
            if matched then
                set current tab of w to t
                set index of w to 1
                activate
                return "OK|" & (name of t)
            end if
        end repeat
    end repeat
end tell
return "NG"
'''

_LIST_TABS = '''
tell application "{app}"
    set out to ""
    repeat with w in windows
        repeat with t in tabs of w
            set out to out & ({title_prop} of t) & "／"
        end repeat
    end repeat
    return out
end tell
'''


def select_browser_tab(tab_match, url_match, browser="chrome"):
    """全ウィンドウ・全タブから目的のタブを選択して前面化する。
    戻り値: (アプリ名, タブ名) / 見つからなければ (None, 開いているタブ名の一覧)。"""
    apps = BROWSER_APPS.get((browser or "chrome").lower(), BROWSER_APPS["chrome"])
    all_tabs = []
    for app in apps:
        if not app_running(app):
            continue
        tpl = _SAFARI_SELECT if app == "Safari" else _CHROMIUM_SELECT
        script = tpl.format(app=_esc(app), url=_esc(url_match or ""),
                            tab=_esc(tab_match or ""))
        ok, out, err = _osascript(script)
        if ok and out.startswith("OK|"):
            return app, out[3:]
        # 見つからなかった場合、診断用にタブ一覧を集める
        title_prop = "name" if app == "Safari" else "title"
        ok2, out2, _ = _osascript(
            _LIST_TABS.format(app=_esc(app), title_prop=title_prop))
        if ok2 and out2:
            all_tabs.extend(x for x in out2.split("／") if x)
    return None, all_tabs


# ============================================================
# 送信本体
# ============================================================

def _is_browser(target):
    return target.get("kind") in ("browser_tab", "edge_tab")


def _app_name(target):
    """kind=app/proc の macOS アプリ名を得る。
    Windows の設定 (proc="claude.exe") でもそれらしく解決する。"""
    if target.get("app"):
        return target["app"]
    proc = target.get("proc", "")
    if proc.lower().endswith(".exe"):
        proc = proc[:-4]
    return proc.capitalize() if proc.islower() else proc


class SendResult:
    def __init__(self, ok, msg, detail=""):
        self.ok = ok
        self.msg = msg
        self.detail = detail

    def __repr__(self):
        return f"SendResult(ok={self.ok}, msg={self.msg!r}, detail={self.detail!r})"


def send_text(target, text, press_enter=True, verify=True):
    """
    target: dict
      kind='app' (または 'proc') -> app='Claude' (macOSのアプリ名)
      kind='browser_tab'         -> browser='chrome', tab='gemini', url='gemini.google.com'
    """
    label = target.get("label", "?")

    # --- 1. 送信先を特定して前面化 ---
    if _is_browser(target):
        app, info = select_browser_tab(target.get("tab"), target.get("url"),
                                       target.get("browser", "chrome"))
        if app is None:
            tabs = "／".join(t[:20] for t in info[:6]) if isinstance(info, list) else ""
            return SendResult(False,
                              f"✗ ブラウザに「{target.get('tab')}」のタブがありません",
                              f"開いているタブ: {tabs}" if tabs
                              else "対象ブラウザが起動していない可能性")
        front_ok = activate_app(app)
        # タブ切替直後の描画待ち
        time.sleep(0.4)
    else:
        app = _app_name(target)
        if not app_running(app):
            return SendResult(False, f"✗ {label} ({app}) が起動していません",
                              "アプリを起動してから試してください")
        front_ok = activate_app(app)
        time.sleep(0.25)

    # --- 2. 前面化を検証してから貼り付け（誤爆防止） ---
    if verify and not front_ok:
        return SendResult(False, f"✗ {label} を前面にできませんでした",
                          "アクセシビリティ権限を確認してください")

    pyperclip.copy(text)
    time.sleep(0.1)

    ok, _, err = _keystroke_paste()
    if not ok:
        return SendResult(False, f"✗ {label} へ貼り付けできませんでした",
                          f"System Events エラー: {err[:80]} — "
                          "システム設定>プライバシーとセキュリティ>アクセシビリティ で"
                          "起動元アプリを許可してください")

    # --- 3. 送信 ---
    if press_enter:
        time.sleep(0.35)
        _press_return()

    note = "" if front_ok else "（前面化が遅延）"
    return SendResult(True, f"✓ {label} へ送信{note}")


def clear_composer(target):
    """テスト用: 入力欄を全選択して消す。"""
    if _is_browser(target):
        app, _ = select_browser_tab(target.get("tab"), target.get("url"),
                                    target.get("browser", "chrome"))
        if app is None:
            return False
        activate_app(app)
    else:
        app = _app_name(target)
        if not app_running(app):
            return False
        activate_app(app)
    time.sleep(0.2)
    _osascript('tell application "System Events" to keystroke "a" using {command down}')
    time.sleep(0.1)
    _osascript('tell application "System Events" to key code 51')  # delete
    return True
