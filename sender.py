# -*- coding: utf-8 -*-
"""
sender.py — テキストを各アプリの入力欄へ確実に届けるモジュール。

方針(旧実装の失敗を踏まえた):
  1. 送信先ウィンドウを特定する（Edgeは「タブ自体を選択」してから）
  2. 前面化を"実際に前面になるまで"確認する
  3. 入力欄(コンポーザ)をUI Automationで特定し SetFocus する
     - Claudeの入力欄は EditControl ではなく Name='プロンプト' の GroupControl
  4. 貼り付け後、入力欄に本当に文字が入ったかを読み取って検証する
  5. 検証できてから Enter を押す（入っていなければ再試行）
"""

import ctypes
import time
from ctypes import wintypes

import pyperclip
import uiautomation as auto

# ============================================================
# Win32
# ============================================================
user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.BringWindowToTop.argtypes = [wintypes.HWND]
user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
user32.GetForegroundWindow.restype = wintypes.HWND
user32.IsIconic.argtypes = [wintypes.HWND]
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

SW_RESTORE = 9
KEYEVENTF_KEYUP = 0x0002
VK_CONTROL = 0x11
VK_MENU = 0x12
VK_RETURN = 0x0D
VK_V = 0x56
VK_A = 0x41
VK_DELETE = 0x2E


def proc_name(pid):
    h = kernel32.OpenProcess(0x1000, False, pid)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(4096)
        size = wintypes.DWORD(4096)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value.split("\\")[-1].lower()
        return ""
    finally:
        kernel32.CloseHandle(h)


def win_title(hwnd):
    n = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def win_pid(hwnd):
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def find_window_by_proc(proc):
    """プロセス名に一致する可視ウィンドウを返す（面積が最大のもの＝メイン窓）。"""
    found = []

    def cb(hwnd, lparam):
        if user32.IsWindowVisible(hwnd) and win_title(hwnd):
            if proc_name(win_pid(hwnd)) == proc:
                r = wintypes.RECT()
                user32.GetWindowRect(hwnd, ctypes.byref(r))
                area = (r.right - r.left) * (r.bottom - r.top)
                found.append((area, int(hwnd)))
        return True

    user32.EnumWindows(EnumWindowsProc(cb), 0)
    if not found:
        return None
    found.sort(reverse=True)
    return found[0][1]


def focus_window(hwnd, timeout=2.0):
    """前面化し、実際に前面になるまで待つ。"""
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    fg = user32.GetForegroundWindow()
    tid_fg = user32.GetWindowThreadProcessId(fg, None)
    tid_tg = user32.GetWindowThreadProcessId(hwnd, None)
    cur = kernel32.GetCurrentThreadId()
    user32.keybd_event(VK_MENU, 0, 0, 0)
    user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
    user32.AttachThreadInput(cur, tid_tg, True)
    user32.AttachThreadInput(cur, tid_fg, True)
    user32.BringWindowToTop(hwnd)
    user32.SetForegroundWindow(hwnd)
    user32.AttachThreadInput(cur, tid_tg, False)
    user32.AttachThreadInput(cur, tid_fg, False)

    deadline = time.time() + timeout
    while time.time() < deadline:
        if user32.GetForegroundWindow() == hwnd:
            return True
        time.sleep(0.03)
    return False


def _key(vk, up=False):
    user32.keybd_event(vk, 0, KEYEVENTF_KEYUP if up else 0, 0)


def _combo(mod, vk):
    _key(mod)
    _key(vk)
    _key(vk, True)
    _key(mod, True)


# ============================================================
# UI Automation ヘルパ
# ============================================================
# 入力欄と思われるコントロール名（日本語UI/英語UI両対応）
COMPOSER_NAMES = (
    "プロンプト", "prompt",
    "メッセージ", "message",
    "チャットする", "chat with",
    "ask anything", "何でも聞いてください", "質問する", "相談",
    "reply to claude",
)

# 入力欄ではない部品（ここに入力すると事故になる）。名前に含まれたら除外する。
EXCLUDE_NAMES = (
    # ブラウザUI
    "アドレス", "address and search", "search bar", "検索バー",
    "お気に入り", "bookmark", "タブ", "tab ",
    "ズーム", "zoom", "翻訳", "translate",
    # 会話中のメッセージ操作ボタン（"メッセージ"を含むため誤検出しやすい）
    "コピー", "copy", "アクション", "action", "編集", "edit ",
    "フォーク", "fork", "再試行", "retry", "削除", "delete",
    "送信", "send", "添付", "attach", "音声", "voice", "マイク", "mic",
)

# 入力欄になり得るコントロール種別（ボタン等を名前一致で拾わないため）
COMPOSER_TYPES = ("EditControl", "ComboBoxControl", "GroupControl", "DocumentControl")


def uia_window(hwnd):
    """hwnd から UIA コントロールを得る。"""
    return auto.ControlFromHandle(hwnd)


def _pattern(ctrl, pid_):
    try:
        return ctrl.GetPattern(pid_)
    except Exception:
        return None


def read_control_text(ctrl):
    """コントロールの現在テキストを読む（ValuePattern → TextPattern の順）。"""
    vp = _pattern(ctrl, auto.PatternId.ValuePattern)
    if vp is not None:
        try:
            return vp.Value or ""
        except Exception:
            pass
    tp = _pattern(ctrl, auto.PatternId.TextPattern)
    if tp is not None:
        try:
            return tp.DocumentRange.GetText(-1) or ""
        except Exception:
            pass
    return ""


def _same_control(a, b):
    """2つのUIAコントロールが同一かを矩形と種別で判定する。"""
    try:
        ra, rb = a.BoundingRectangle, b.BoundingRectangle
        return (a.ControlTypeName == b.ControlTypeName
                and ra.left == rb.left and ra.top == rb.top
                and ra.right == rb.right and ra.bottom == rb.bottom)
    except Exception:
        return False


def focus_composer(composer, timeout=2.0):
    """入力欄にフォーカスを移し、"本当に入った"ことを確認するまで待つ。
    Web(Edge)はSetFocus直後にフォーカスが定まらず、待たずに貼ると入らない。"""
    deadline = time.time() + timeout
    tried = 0
    while time.time() < deadline:
        if tried == 0 or time.time() > deadline - timeout / 2:
            try:
                composer.SetFocus()
            except Exception:
                pass
            tried += 1
        time.sleep(0.12)
        try:
            if _same_control(auto.GetFocusedControl(), composer):
                return True
        except Exception:
            pass
    return False


def find_page_documents(win_ctrl):
    """ブラウザのページ本体候補(DocumentControl)を面積の大きい順に返す。
    同サイズの空DocumentControlが混ざるため、1つに決め打ちせず候補列として返す。"""
    docs = []

    def scan(ctrl, depth=0):
        if depth > 16:
            return
        try:
            children = ctrl.GetChildren()
        except Exception:
            return
        for c in children:
            try:
                if c.ControlTypeName == "DocumentControl":
                    r = c.BoundingRectangle
                    area = (r.right - r.left) * (r.bottom - r.top)
                    if area > 10000:
                        docs.append((area, c))
                scan(c, depth + 1)
            except Exception:
                pass

    scan(win_ctrl)
    docs.sort(key=lambda x: x[0], reverse=True)
    return [c for _a, c in docs]


def _is_text_input(ctrl):
    """テキストを保持できる部品か（ボタン等を入力欄と誤認しないための確認）。"""
    return (_pattern(ctrl, auto.PatternId.ValuePattern) is not None
            or _pattern(ctrl, auto.PatternId.TextPattern) is not None)


def _excluded(name):
    n = (name or "").strip().lower()
    return bool(n) and any(k in n for k in EXCLUDE_NAMES)


def _scan_for_composer(scope, wr, allow_bottom=True):
    """1つの探索範囲(scope)を1回走査して入力欄候補を返す。"""
    win_h = wr.bottom - wr.top
    bottom_zone = wr.bottom - win_h * 0.35
    by_name, edits, bottom_focusable = [], [], []

    def scan(ctrl, depth=0):
        if depth > 30:
            return
        try:
            children = ctrl.GetChildren()
        except Exception:
            return
        for c in children:
            try:
                raw = c.Name or ""
                nm = raw.strip().lower()
                ct = c.ControlTypeName
                r = c.BoundingRectangle
                wide = (r.right - r.left) > 120
                if _excluded(raw):
                    continue          # ブラウザUI・メッセージ操作ボタン等は無視
                if (nm and any(k in nm for k in COMPOSER_NAMES)
                        and c.IsKeyboardFocusable
                        and ct in COMPOSER_TYPES
                        and _is_text_input(c)):
                    by_name.append(c)
                elif ct in ("EditControl", "ComboBoxControl") and c.IsKeyboardFocusable and wide:
                    edits.append(c)
                elif (allow_bottom and c.IsKeyboardFocusable and wide
                      and r.top >= bottom_zone and r.bottom <= wr.bottom + 10
                      and ct in ("GroupControl", "DocumentControl", "PaneControl")):
                    bottom_focusable.append(c)
                scan(c, depth + 1)
            except Exception:
                pass

    scan(scope)
    for group in (by_name, edits, bottom_focusable):
        if group:
            return group[0]
    return None


def find_composer(win_ctrl, timeout=6.0, page_only=False):
    """入力欄コントロールを探す。見つからなければ None。
    page_only=True (ブラウザ) はページ本体の中だけを探す。
    同サイズの"空"DocumentControlが混在するため、候補を順に試す。"""
    deadline = time.time() + timeout
    wr = win_ctrl.BoundingRectangle

    while time.time() < deadline:
        if page_only:
            for sc in find_page_documents(win_ctrl):
                # ページ内は下部限定にしない（新規チャットでは中央に入力欄がある）
                c = _scan_for_composer(sc, wr, allow_bottom=False)
                if c is not None:
                    return c
        else:
            c = _scan_for_composer(win_ctrl, wr, allow_bottom=True)
            if c is not None:
                return c
        time.sleep(0.3)
    return None


# ============================================================
# Edge: タブを名前で選択する
# ============================================================
# 対応ブラウザ（いずれもChromium系なので同じ方法でタブを扱える）
BROWSER_PROCS = {
    "edge": ("msedge.exe",),
    "chrome": ("chrome.exe",),
    "brave": ("brave.exe",),
    "vivaldi": ("vivaldi.exe",),
    "any": ("msedge.exe", "chrome.exe", "brave.exe", "vivaldi.exe"),
}


def _browser_windows(browser="edge"):
    """指定ブラウザの可視ウィンドウを返す（プロセス名で判定するので言語設定に依存しない）。"""
    procs = BROWSER_PROCS.get((browser or "edge").lower(), BROWSER_PROCS["edge"])
    root = auto.GetRootControl()
    out = []
    for w in root.GetChildren():
        try:
            if (w.ControlTypeName == "WindowControl"
                    and w.ClassName == "Chrome_WidgetWin_1"):
                hwnd = w.NativeWindowHandle
                if hwnd and proc_name(win_pid(hwnd)) in procs:
                    out.append(w)
        except Exception:
            pass
    return out


def _edge_tabs(win):
    tabs = []

    def scan(ctrl, depth=0):
        if depth > 18:
            return
        try:
            ch = ctrl.GetChildren()
        except Exception:
            return
        for c in ch:
            try:
                if c.ControlTypeName == "TabItemControl":
                    tabs.append(c)
                else:
                    scan(c, depth + 1)
            except Exception:
                pass

    scan(win)
    return tabs


def _select_tab(tab):
    sp = _pattern(tab, auto.PatternId.SelectionItemPattern)
    if sp is not None:
        try:
            sp.Select()
            time.sleep(0.3)
            return True
        except Exception:
            pass
    ip = _pattern(tab, auto.PatternId.InvokePattern)
    if ip is not None:
        try:
            ip.Invoke()
            time.sleep(0.3)
            return True
        except Exception:
            pass
    return False


def _tab_selected(tab):
    sp = _pattern(tab, auto.PatternId.SelectionItemPattern)
    try:
        return bool(sp.IsSelected) if sp is not None else False
    except Exception:
        return False


def _browser_current_url(win):
    """アドレスバーから現在のURLを読む。"""
    for depth in (12, 20):
        try:
            bar = win.EditControl(searchDepth=depth, foundIndex=1)
            if bar.Exists(1):
                nm = (bar.Name or "")
                if "アドレス" in nm or "address" in nm.lower():
                    return read_control_text(bar)
        except Exception:
            pass
    # 名前で総当たり
    found = []

    def scan(ctrl, d=0):
        if d > 14 or found:
            return
        try:
            ch = ctrl.GetChildren()
        except Exception:
            return
        for c in ch:
            if found:
                return
            try:
                nm = (c.Name or "")
                if c.ControlTypeName == "EditControl" and ("アドレス" in nm or "address" in nm.lower()):
                    found.append(c)
                    return
                scan(c, d + 1)
            except Exception:
                pass

    scan(win)
    return read_control_text(found[0]) if found else ""


def edge_select_tab(match, url_match=None, browser="edge"):
    """Edgeの全ウィンドウ・全タブから目的のタブを選択する。
    1) タブ名に match を含むもの
    2) 見つからなければ URL に url_match を含むタブを総当たりで探す
       （ChatGPT等はタブ名が会話タイトルに変わり、名前では見つからないため）
    戻り値: (hwnd, タブ名) / 見つからなければ (None, タブ名一覧)。"""
    match = (match or "").lower()
    all_tabs = []

    # --- 1) タブ名で探す ---
    for w in _browser_windows(browser):
        try:
            hwnd = w.NativeWindowHandle
            for t in _edge_tabs(w):
                nm = t.Name or ""
                all_tabs.append(nm)
                if match and match in nm.lower():
                    _select_tab(t)
                    return hwnd, nm
        except Exception:
            pass

    # --- 2) URLで探す（タブを順に選択して確認） ---
    if url_match:
        url_match = url_match.lower()
        for w in _browser_windows(browser):
            try:
                hwnd = w.NativeWindowHandle
                tabs = _edge_tabs(w)
                original = None
                for t in tabs:
                    if _tab_selected(t):
                        original = t
                        break
                # まず現在のタブを確認（切替なしで済むことが多い）
                order = ([original] if original is not None else []) + \
                        [t for t in tabs if t is not original]
                for t in order:
                    try:
                        if not _tab_selected(t):
                            if not _select_tab(t):
                                continue
                            time.sleep(0.25)
                        url = (_browser_current_url(w) or "").lower()
                        if url_match in url:
                            return hwnd, (t.Name or "")
                    except Exception:
                        pass
                # 見つからなければ元のタブへ戻す
                if original is not None and not _tab_selected(original):
                    _select_tab(original)
            except Exception:
                pass

    return None, all_tabs


# ============================================================
# 送信本体
# ============================================================
def _is_browser(target):
    """ブラウザのタブが対象か（旧名 edge_tab も受け付ける）。"""
    return target.get("kind") in ("browser_tab", "edge_tab") or (
        target.get("kind") == "click" and bool(target.get("url") or target.get("tab")))


def find_button(win_ctrl, names, page_only=False, timeout=6.0):
    """名前が一致するボタンを探す（完全一致 → 部分一致の順）。"""
    wanted = [n.strip().lower() for n in names if n]
    deadline = time.time() + timeout
    while time.time() < deadline:
        exact, partial = [], []

        def walk(x, d=0):
            if d > 28:
                return
            try:
                ch = x.GetChildren()
            except Exception:
                return
            for y in ch:
                try:
                    if y.ControlTypeName in ("ButtonControl", "MenuItemControl", "ListItemControl"):
                        nm = (y.Name or "").strip().lower()
                        if nm:
                            if nm in wanted:
                                exact.append(y)
                            elif any(w in nm for w in wanted):
                                partial.append(y)
                    walk(y, d + 1)
                except Exception:
                    pass

        scopes = find_page_documents(win_ctrl) if page_only else []
        for sc in (scopes[:2] or [win_ctrl]):
            walk(sc)
        for group in (exact, partial):
            for b in group:
                try:
                    if b.IsEnabled:
                        return b
                except Exception:
                    return b
        time.sleep(0.3)
    return None


def click_named_button(hwnd, names, page_only, label="", timeout=6.0):
    """開いているウィンドウ内のボタンを名前で押す（対象特定済みの場合に使う）。"""
    if isinstance(names, str):
        names = [names]
    btn = find_button(uia_window(hwnd), names, page_only=page_only, timeout=timeout)
    if btn is None:
        return SendResult(False, f"✗ {label}: ボタンが見つかりません",
                          f"探した名前: {' / '.join(names)}")
    ip = _pattern(btn, auto.PatternId.InvokePattern)
    if ip is not None:
        try:
            ip.Invoke()
            return SendResult(True, "")
        except Exception:
            pass
    # Invoke に対応しない部品（Claudeの音声入力など）は既定動作かクリックで押す
    la = _pattern(btn, auto.PatternId.LegacyIAccessiblePattern)
    if la is not None:
        try:
            la.DoDefaultAction()
            return SendResult(True, "")
        except Exception:
            pass
    try:
        btn.Click(simulateMove=False)
        return SendResult(True, "")
    except Exception as e:
        return SendResult(False, f"✗ {label}: ボタンを押せませんでした", str(e)[:60])


def wait_for_composer_text(composer, timeout=12.0, poll=0.2):
    """入力欄に文字が入るまで待って、その内容を返す（アプリ側の書き起こし完了待ち）。"""
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


def press_button(target):
    """設定で指定されたボタンを押す（ライブ音声モードの起動など）。
    文字を送るのではなく、アプリ内のボタンを1回クリックする種類の宛先。"""
    label = target.get("label", "?")
    names = target.get("button") or []
    if isinstance(names, str):
        names = [names]
    if not names:
        return SendResult(False, f"✗ {label}: 押すボタンが設定されていません")

    if _is_browser(target):
        hwnd, info = edge_select_tab(target.get("tab", ""), target.get("url"),
                                     target.get("browser", "edge"))
        if hwnd is None:
            return SendResult(False, f"✗ {label} のタブが見つかりません")
    else:
        hwnd = find_window_by_proc(target["proc"])
        if hwnd is None:
            return SendResult(False, f"✗ {label} のウィンドウが見つかりません")

    focus_window(hwnd)
    time.sleep(0.4)

    btn = find_button(uia_window(hwnd), names, page_only=_is_browser(target))
    if btn is None:
        return SendResult(False, f"✗ {label}: ボタンが見つかりません",
                          f"探した名前: {' / '.join(names)}")

    pressed = False
    ip = _pattern(btn, auto.PatternId.InvokePattern)
    if ip is not None:
        try:
            ip.Invoke()
            pressed = True
        except Exception:
            pass
    if not pressed:
        try:
            btn.Click(simulateMove=False)
            pressed = True
        except Exception:
            pass
    if not pressed:
        return SendResult(False, f"✗ {label}: ボタンを押せませんでした")
    return SendResult(True, f"✓ {label} を起動しました")


class SendResult:
    def __init__(self, ok, msg, detail=""):
        self.ok = ok
        self.msg = msg
        self.detail = detail

    def __repr__(self):
        return f"SendResult(ok={self.ok}, msg={self.msg!r}, detail={self.detail!r})"


class Prepared:
    """送信先の準備結果（ウィンドウ特定・前面化・入力欄フォーカスまで済んだ状態）。
    音声認識と並行して先に済ませておくことで、体感の待ち時間を短くする。"""

    def __init__(self, target, hwnd=None, composer=None,
                 focused_win=False, focus_ok=False, error=None):
        self.target = target
        self.hwnd = hwnd
        self.composer = composer
        self.focused_win = focused_win
        self.focus_ok = focus_ok
        self.error = error          # SendResult（失敗時）


def prepare_target(target):
    """送信先を開いて入力欄にフォーカスするところまでを行う（文字は入れない）。"""
    label = target.get("label", "?")

    if _is_browser(target):
        hwnd, info = edge_select_tab(target["tab"], target.get("url"),
                                     target.get("browser", "edge"))
        if hwnd is None:
            tabs = "／".join(t[:20] for t in info[:6]) if isinstance(info, list) else ""
            return Prepared(target, error=SendResult(
                False, f"✗ Edgeに「{target['tab']}」のタブがありません", f"開いているタブ: {tabs}"))
    else:
        hwnd = find_window_by_proc(target["proc"])
        if hwnd is None:
            return Prepared(target, error=SendResult(
                False, f"✗ {label} のウィンドウが見つかりません"))

    focused_win = focus_window(hwnd)
    time.sleep(0.15)
    if _is_browser(target):
        time.sleep(0.35)

    composer, focus_ok = None, False
    try:
        composer = find_composer(uia_window(hwnd), page_only=_is_browser(target))
        if composer is not None:
            focus_ok = focus_composer(composer)
    except Exception:
        composer = None

    if _is_browser(target) and composer is None:
        return Prepared(target, hwnd, error=SendResult(
            False, f"✗ {label} のページ内に入力欄が見つかりません",
            "ページが読み込み中か、ログイン画面の可能性"))

    return Prepared(target, hwnd, composer, focused_win, focus_ok)


def send_text(target, text, press_enter=True, verify=True, prepared=None):
    """
    target: dict
      kind='proc'     -> proc='claude.exe'
      kind='edge_tab' -> tab='chatgpt'
    """
    label = target.get("label", "?")

    # --- 1〜3. 送信先の準備（済んでいれば再利用して待ち時間を省く） ---
    prep = prepared if prepared is not None else prepare_target(target)
    if prep.error is not None:
        return prep.error
    hwnd = prep.hwnd
    composer = prep.composer
    focused_win = prep.focused_win
    focus_ok = prep.focus_ok

    # 準備から時間が経っていると別ウィンドウが前面に来ている場合があるため入れ直す
    if user32.GetForegroundWindow() != hwnd:
        focused_win = focus_window(hwnd)
        if composer is not None:
            focus_ok = focus_composer(composer)

    # --- 4. 貼り付け（検証つきで最大3回） ---
    pyperclip.copy(text)
    time.sleep(0.08)

    needle = text.strip()[:12]

    # 入力欄のテキストを読めるか（読めないなら検証不能→従来方式で貼るしかない）
    # GetPattern は負荷時に一時的に失敗することがあるため必ず再試行する。
    readable = False
    if composer is not None:
        for _try in range(4):
            if (_pattern(composer, auto.PatternId.ValuePattern) is not None
                    or _pattern(composer, auto.PatternId.TextPattern) is not None):
                readable = True
                break
            if read_control_text(composer) != "":
                readable = True
                break
            time.sleep(0.2)

    def _landed():
        """貼り付き判定。プレースホルダ等を誤検出しないよう本文一致のみを成功とする。"""
        deadline = time.time() + 2.5
        while time.time() < deadline:
            if needle and needle in read_control_text(composer):
                return True
            time.sleep(0.1)
        return False

    do_verify = bool(verify and composer is not None and readable and needle)
    landed = False

    if not do_verify:
        _combo(VK_CONTROL, VK_V)
        time.sleep(0.35)
        landed = True
    else:
        for i in range(3):
            # 既に入っているなら貼り直さない（二重貼付の防止）
            if needle in read_control_text(composer):
                landed = True
                break
            _combo(VK_CONTROL, VK_V)
            if _landed():
                landed = True
                break
            focus_composer(composer, timeout=1.2)

    if not landed:
        return SendResult(False, f"✗ {label} の入力欄に文字を入れられませんでした",
                          f"入力欄={'特定OK' if composer is not None else '特定できず'} / "
                          f"フォーカス={focus_ok} / 前面化={focused_win}")

    # --- 5. 送信 ---
    if press_enter:
        time.sleep(0.12)
        _key(VK_RETURN)
        _key(VK_RETURN, True)

    note = ""
    if not focused_win:
        note += "（前面化が遅延）"
    if composer is None:
        note += "（入力欄未特定・検証なし）"
    elif not do_verify:
        note += "（検証不可）"
    return SendResult(True, f"✓ {label} へ送信{note}")


def clear_composer(target):
    """テスト用: 入力欄を全選択して消す。"""
    if _is_browser(target):
        hwnd, _ = edge_select_tab(target["tab"], target.get("url"),
                                  target.get("browser", "edge"))
    else:
        hwnd = find_window_by_proc(target["proc"])
    if hwnd is None:
        return False
    focus_window(hwnd)
    time.sleep(0.15)
    try:
        c = find_composer(uia_window(hwnd), page_only=_is_browser(target))
        if c is not None:
            c.SetFocus()
            time.sleep(0.1)
    except Exception:
        pass
    _combo(VK_CONTROL, VK_A)
    time.sleep(0.08)
    _key(VK_DELETE)
    _key(VK_DELETE, True)
    return True
