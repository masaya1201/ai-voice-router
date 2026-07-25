# -*- coding: utf-8 -*-
"""
sender_mac.py — macOS版の送信モジュール。sender.py (Windows) と同じ API を提供する。

Windows版は UI Automation で入力欄そのものを特定して検証するが、
macOS には同等の汎用APIが無いため方針を変える:

  1. 送信先アプリ / ブラウザのタブを AppleScript で特定して前面化する
  2. "実際に前面になったか" をポーリングで確認する（誤爆防止の要）
  3. クリップボード経由で Cmd+V 貼り付け → Enter (key code 36)

ChatGPT / Claude / Gemini の各Webアプリはページ全体で paste を受けて
入力欄に入れるため、タブが前面にさえなっていれば貼り付けは届く。
ブラウザで JavaScript 実行が許可されている場合は、貼る前に入力欄へ
フォーカスを移して（アドレスバー等への誤爆を防いで）確実性を上げる。

必要な macOS 権限:
  - アクセシビリティ: System Events でキー入力を送るため
    (システム設定 > プライバシーとセキュリティ > アクセシビリティ に
     ターミナル/iTerm 等の起動元アプリを追加)
  - オートメーション: 初回にダイアログが出るので「許可」する
  - kind="click"（音声会話の起動など）を使う場合のみ、ブラウザの
    「Apple Events からの JavaScript を許可」を有効にする
    (Chrome: 表示 > 開発 / Safari: 開発 メニュー)
"""

import json
import subprocess
import time

import pyperclip

# Windows版と同じ名前を用意しておく（voice_router.py から参照されるため）
VK_RETURN = 36          # macOS の key code 36 = Return


# ============================================================
# AppleScript 実行ヘルパ
# ============================================================

def _osascript(script, timeout=15):
    """AppleScript を実行して (ok, stdout, stderr) を返す。"""
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


def _is_front(app_name):
    cur = frontmost_app().lower()
    want = (app_name or "").lower()
    return bool(cur) and (cur == want or want in cur or cur in want)


def activate_app(app_name, timeout=3.0):
    """アプリを前面化し、実際に前面になるまで待つ。"""
    _osascript(f'tell application "{_esc(app_name)}" to activate')
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _is_front(app_name):
            return True
        time.sleep(0.1)
    return False


# Windows版と名前を合わせたシム（voice_router.py の stt="app" 経路から呼ばれる）
def focus_window(app_name, timeout=2.0):
    return activate_app(app_name, timeout)


def _key(vk, up=False):
    """キーを1回押す。macOSでは押下と離上を分けられないため up は無視する。"""
    if up:
        return True
    ok, _, _ = _osascript(
        f'tell application "System Events" to key code {int(vk)}')
    return ok


def _keystroke_paste():
    return _osascript(
        'tell application "System Events" to keystroke "v" using {command down}')


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
# ページ内 JavaScript（入力欄フォーカス・ボタンクリック）
# ============================================================
# ブラウザ側で「Apple Events からの JavaScript を許可」が必要。
# 無効なら失敗するだけなので、任意機能としてのみ使う。

def run_page_js(app, js):
    """前面タブで JavaScript を実行して結果の文字列を返す。
    戻り値: (ok, 結果 or エラー文字列)"""
    if app == "Safari":
        script = (f'tell application "Safari" to do JavaScript "{_esc(js)}" '
                  'in current tab of window 1')
    else:
        script = (f'tell application "{_esc(app)}" to tell active tab of window 1 '
                  f'to execute javascript "{_esc(js)}"')
    ok, out, err = _osascript(script)
    return ok, (out if ok else err)


# 入力欄を探してフォーカスする。見つからなければ "NG"。
_JS_FOCUS_COMPOSER = """
(function(){
  var sels = ['div[contenteditable="true"]','textarea','input[type="text"]'];
  for (var i=0;i<sels.length;i++){
    var els = document.querySelectorAll(sels[i]);
    for (var j=0;j<els.length;j++){
      var e = els[j], r = e.getBoundingClientRect();
      if (r.width > 80 && r.height > 10 && e.offsetParent !== null){
        e.focus();
        if (document.activeElement === e) return 'OK';
      }
    }
  }
  return 'NG';
})()
"""

# 入力欄の現在の内容を返す
_JS_READ_COMPOSER = """
(function(){
  var sels = ['div[contenteditable="true"]','textarea','input[type="text"]'];
  for (var i=0;i<sels.length;i++){
    var els = document.querySelectorAll(sels[i]);
    for (var j=0;j<els.length;j++){
      var e = els[j], r = e.getBoundingClientRect();
      if (r.width > 80 && r.height > 10 && e.offsetParent !== null){
        return (e.value !== undefined && e.value !== null) ? e.value : (e.innerText || '');
      }
    }
  }
  return '';
})()
"""

# 名前でボタンを探して押す。NAMES は JSON 配列に差し替える。
_JS_CLICK_BUTTON = """
(function(){
  var names = NAMES;
  var els = document.querySelectorAll('button,[role="button"],a,[aria-label]');
  for (var n=0;n<names.length;n++){
    var want = names[n].toLowerCase();
    for (var i=0;i<els.length;i++){
      var e = els[i];
      var lab = ((e.getAttribute('aria-label')||'') + ' ' +
                 (e.getAttribute('title')||'') + ' ' +
                 (e.textContent||'')).toLowerCase();
      if (lab.indexOf(want) >= 0){
        var r = e.getBoundingClientRect();
        if (r.width > 0 && r.height > 0){ e.click(); return 'OK'; }
      }
    }
  }
  return 'NG';
})()
"""


class WebComposer:
    """ブラウザのページ内入力欄を表す。JS経由でのみ読み書きできる。"""

    def __init__(self, app):
        self.app = app

    def read(self):
        ok, out = run_page_js(self.app, _JS_READ_COMPOSER)
        return out if ok else ""

    def focus(self):
        ok, out = run_page_js(self.app, _JS_FOCUS_COMPOSER)
        return ok and out == "OK"


def focus_composer(composer, timeout=2.0):
    """入力欄にフォーカスを移す（できなければ False。呼び出し側は続行してよい）。"""
    if composer is None:
        return False
    try:
        return composer.focus()
    except Exception:
        return False


def read_control_text(composer):
    if composer is None:
        return ""
    try:
        return composer.read()
    except Exception:
        return ""


def click_named_button(app, names, page_only, label="", timeout=6.0):
    """開いているアプリ/タブ内のボタンを名前で押す（対象特定済みの場合に使う）。
    ブラウザはページ内JS、デスクトップアプリは System Events で押す。"""
    if isinstance(names, str):
        names = [names]
    if not names:
        return SendResult(False, f"✗ {label}: 押すボタンが設定されていません")

    if page_only:
        js = _JS_CLICK_BUTTON.replace("NAMES", json.dumps(names))
        deadline = time.time() + timeout
        ok, last = True, ""
        while time.time() < deadline:
            ok, out = run_page_js(app, js)
            if ok and out == "OK":
                return SendResult(True, "")
            last = out
            if not ok:
                break                      # JS が禁止されている等。待っても変わらない
            time.sleep(0.4)
        if not ok:
            return SendResult(
                False, f"✗ {label}: ページ内のボタンを押せません",
                f"{app} の「Apple Events からの JavaScript を許可」を"
                f"有効にしてください（{last[:60]}）")
        return SendResult(False, f"✗ {label}: ボタンが見つかりません",
                          f"探した名前: {' / '.join(names)}")

    # デスクトップアプリ: System Events でボタンを名前(前方一致)で押す
    for nm in names:
        ok, out, err = _osascript(f'''
tell application "System Events" to tell process "{_esc(app)}"
    repeat with b in (entire contents)
        try
            if (role of b is "AXButton") and ((name of b) contains "{_esc(nm)}") then
                perform action "AXPress" of b
                return "OK"
            end if
        end try
    end repeat
end tell
return "NG"
''', timeout=20)
        if ok and out == "OK":
            return SendResult(True, "")
    return SendResult(False, f"✗ {label}: ボタンが見つかりません",
                      f"探した名前: {' / '.join(names)}")


def wait_for_composer_text(composer, timeout=12.0, poll=0.4):
    """入力欄に文字が入るまで待って、その内容を返す（アプリ側の書き起こし完了待ち）。
    macOS ではブラウザのページ内でのみ読める（デスクトップアプリは "" を返す）。"""
    if composer is None:
        return ""
    baseline = read_control_text(composer).strip()
    deadline = time.time() + timeout
    stable, last = 0, None
    while time.time() < deadline:
        cur = read_control_text(composer).strip()
        if cur and cur != baseline:
            if cur == last:
                stable += 1
                if stable >= 2:          # 増えなくなったら書き起こし完了とみなす
                    return cur
            else:
                stable = 0
            last = cur
        time.sleep(poll)
    return (last or "").strip()


# ============================================================
# 送信本体
# ============================================================

def _is_browser(target):
    return target.get("kind") in ("browser_tab", "edge_tab", "click") \
        and bool(target.get("tab") or target.get("url"))


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


class Prepared:
    """送信先の準備結果（アプリ/タブを特定して前面化するところまで済んだ状態）。
    音声認識と並行して先に済ませておくことで、体感の待ち時間を短くする。
    Windows版の hwnd に当たるものが、macOSではアプリ名になる。"""

    def __init__(self, target, hwnd=None, composer=None,
                 focused_win=False, focus_ok=False, error=None):
        self.target = target
        self.hwnd = hwnd            # macOS ではアプリ名
        self.composer = composer
        self.focused_win = focused_win
        self.focus_ok = focus_ok
        self.error = error          # SendResult（失敗時）


def prepare_target(target):
    """送信先を開いて（できれば入力欄にフォーカスして）おく（文字は入れない）。"""
    label = target.get("label", "?")

    if _is_browser(target):
        app, info = select_browser_tab(target.get("tab"), target.get("url"),
                                       target.get("browser", "chrome"))
        if app is None:
            tabs = "／".join(t[:20] for t in info[:6]) if isinstance(info, list) else ""
            return Prepared(target, error=SendResult(
                False, f"✗ ブラウザに「{target.get('tab')}」のタブがありません",
                f"開いているタブ: {tabs}" if tabs else "対象ブラウザが起動していない可能性"))
        focused_win = activate_app(app)
        time.sleep(0.4)             # タブ切替直後の描画待ち
    else:
        app = _app_name(target)
        if not app_running(app):
            return Prepared(target, error=SendResult(
                False, f"✗ {label} ({app}) が起動していません",
                "アプリを起動してから試してください"))
        focused_win = activate_app(app)
        time.sleep(0.25)

    # ブラウザは可能なら入力欄へフォーカスしておく（アドレスバー等への誤爆防止）。
    # JavaScript が許可されていない場合は失敗するが、ページ全体に貼れば
    # 各AIのWebアプリは入力欄に入れてくれるので続行する。
    composer, focus_ok = None, False
    if _is_browser(target):
        c = WebComposer(app)
        if c.focus():
            composer, focus_ok = c, True

    return Prepared(target, app, composer, focused_win, focus_ok)


def prepare_target_threadsafe(target):
    """別スレッドから準備するとき用。
    macOS の実装は AppleScript を別プロセスで実行するだけなのでスレッド安全。
    （Windows版は UI Automation のスレッド初期化が必要なため別実装になっている）"""
    try:
        return prepare_target(target)
    except Exception:
        import traceback
        traceback.print_exc()
        return Prepared(target)


def send_text(target, text, press_enter=True, verify=True, prepared=None):
    """
    target: dict
      kind='app' (または 'proc') -> app='Claude' (macOSのアプリ名)
      kind='browser_tab'         -> browser='chrome', tab='gemini', url='gemini.google.com'
    """
    label = target.get("label", "?")

    # --- 1. 送信先の準備（済んでいれば再利用して待ち時間を省く） ---
    prep = prepared if prepared is not None else prepare_target(target)
    if prep.error is not None:
        return prep.error
    app = prep.hwnd
    focused_win = prep.focused_win

    # 準備から時間が経っていると別のアプリが前面に来ている場合があるため入れ直す
    if not _is_front(app):
        focused_win = activate_app(app)
        if prep.composer is not None:
            focus_composer(prep.composer)

    # --- 2. 前面化を検証してから貼り付け（誤爆防止） ---
    if verify and not focused_win and not _is_front(app):
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
        _key(VK_RETURN)

    note = "" if focused_win else "（前面化が遅延）"
    if _is_browser(target) and prep.composer is None:
        note += "（入力欄未特定）"
    return SendResult(True, f"✓ {label} へ送信{note}")


def press_button(target):
    """設定で指定されたボタンを押す（ライブ音声モードの起動など）。
    文字を送るのではなく、アプリ内のボタンを1回クリックする種類の宛先。"""
    label = target.get("label", "?")
    names = target.get("button") or []
    if isinstance(names, str):
        names = [names]
    if not names:
        return SendResult(False, f"✗ {label}: 押すボタンが設定されていません")

    page_only = _is_browser(target)
    if page_only:
        app, info = select_browser_tab(target.get("tab"), target.get("url"),
                                       target.get("browser", "chrome"))
        if app is None:
            return SendResult(False, f"✗ {label} のタブが見つかりません")
    else:
        app = _app_name(target)
        if not app_running(app):
            return SendResult(False, f"✗ {label} ({app}) が起動していません")

    activate_app(app)
    time.sleep(0.4)

    r = click_named_button(app, names, page_only, label)
    if not r.ok:
        return r
    return SendResult(True, f"✓ {label} を起動しました")


def clear_composer(target):
    """テスト用: 入力欄を全選択して消す。"""
    prep = prepare_target(target)
    if prep.error is not None:
        return False
    time.sleep(0.2)
    _osascript('tell application "System Events" to keystroke "a" using {command down}')
    time.sleep(0.1)
    _osascript('tell application "System Events" to key code 51')  # delete
    return True
