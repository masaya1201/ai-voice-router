# -*- coding: utf-8 -*-
"""
Voice Router
ボタンを押しながら話し、離すと、その宛先アプリの入力欄へ
音声認識テキストを自動入力し Enter で送信する常駐ツール。

UI     : pywebview (HTML/CSS/JS)
音声認識: faster-whisper (オフライン / CPU)
送信    : sender.py (UI Automationで入力欄を特定→フォーカス→貼付検証→Enter)
"""

import os
import sys

os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")       # Anaconda OpenMP重複回避
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

# pythonw(コンソール無し)起動では stdout/stderr が None になり、ライブラリの
# 警告出力で読込スレッドがクラッシュする。ログファイルへ退避して回避。
# 実行フォルダ（.exe化した場合も設定/ログを実行ファイルの隣に置く）
if getattr(sys, "frozen", False):
    APP_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))

_LOG_PATH = os.path.join(APP_DIR, "voice_router.log")
try:
    if sys.stdout is None or sys.stderr is None:
        _logf = open(_LOG_PATH, "a", encoding="utf-8")
        if sys.stdout is None:
            sys.stdout = _logf
        if sys.stderr is None:
            sys.stderr = _logf
except Exception:
    pass

import threading
import time

import numpy as np
import sounddevice as sd
import webview
from faster_whisper import WhisperModel

IS_MAC = sys.platform == "darwin"
if IS_MAC:
    import hotkeys_mac as hotkeys
    import sender_mac as sender
else:
    import hotkeys
    import sender

# ============================================================
# 設定
# ============================================================
MODEL_SIZE = "small"   # base(速い) / small(推奨) / medium(高精度・重い)
LANGUAGE = None        # None=自動判定(日/英) / "ja"=日本語固定(最速) / "en"=英語固定
SAMPLE_RATE = 16000

# テンキー 1〜4 を各AIに割り当てる（押している間だけ録音／離すと送信）
HOTKEYS_ENABLED = True
# True にすると対象キーを他アプリへ渡さない（テンキーで数字が打てなくなる）。
# 数字入力も使いたい場合は False にする。
HOTKEY_SWALLOW = True

# ============================================================
# 送信先の設定
#   ここに1行足すだけでボタンが増える。
#   kind="proc"     : デスクトップアプリ。proc= プロセス名(小文字)
#   kind="edge_tab" : Edgeのタブ。tab= タブ名に含まれる文字(小文字)
# 例) Perplexityを足す:
#   {"key":"pplx","label":"Perplexity","color":"#20808d","kind":"edge_tab","tab":"perplexity"},
# ============================================================
if IS_MAC:
    # macOS: kind="app" は app= にアプリ名（/Applications の .app 名）
    _DEFAULT_TARGETS = [
        {"key": "claude", "label": "Claude", "color": "#d97757",
         "kind": "app", "app": "Claude"},
        {"key": "chatgpt_web", "label": "ChatGPT Web", "color": "#3a8fd6",
         "kind": "browser_tab", "browser": "chrome",
         "tab": "chatgpt", "url": "chatgpt.com"},
        {"key": "gemini", "label": "Gemini", "color": "#8e6fd8",
         "kind": "browser_tab", "browser": "chrome",
         "tab": "gemini", "url": "gemini.google.com"},
        {"key": "claude_web", "label": "Claude Web", "color": "#b8622f",
         "kind": "browser_tab", "browser": "chrome",
         "tab": "claude", "url": "claude.ai"},
    ]
else:
    _DEFAULT_TARGETS = [
        {"key": "chatgpt", "label": "ChatGPT", "color": "#10a37f",
         "kind": "proc", "proc": "chatgpt.exe"},
        {"key": "claude", "label": "Claude", "color": "#d97757",
         "kind": "proc", "proc": "claude.exe"},
        # タブ名は会話タイトルに変わるため URL でも判定する
        {"key": "chatgpt_web", "label": "ChatGPT Web", "color": "#3a8fd6",
         "kind": "browser_tab", "browser": "edge",
         "tab": "chatgpt", "url": "chatgpt.com"},
        {"key": "gemini", "label": "Gemini", "color": "#8e6fd8",
         "kind": "browser_tab", "browser": "edge",
         "tab": "gemini", "url": "gemini.google.com"},
    ]

DEFAULT_CONFIG = {
    "model_size": MODEL_SIZE,
    "language": LANGUAGE,
    "hotkeys_enabled": HOTKEYS_ENABLED,
    "hotkey_swallow": HOTKEY_SWALLOW,
    "targets": _DEFAULT_TARGETS,
}

CONFIG_PATH = os.path.join(APP_DIR, "voice_router_config.json")


def load_config():
    """設定ファイルを読む。無ければ既定値で作る（利用者がコードを触らずに宛先を追加できる）。"""
    import json
    cfg = dict(DEFAULT_CONFIG)
    try:
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, encoding="utf-8") as f:
                user = json.load(f)
            cfg.update({k: v for k, v in user.items() if v is not None or k == "language"})
        else:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(DEFAULT_CONFIG, f, ensure_ascii=False, indent=2)
    except Exception:
        import traceback
        traceback.print_exc()
    return cfg


CONFIG = load_config()
MODEL_SIZE = CONFIG.get("model_size", MODEL_SIZE)
LANGUAGE = CONFIG.get("language", LANGUAGE)
HOTKEYS_ENABLED = CONFIG.get("hotkeys_enabled", HOTKEYS_ENABLED)
HOTKEY_SWALLOW = CONFIG.get("hotkey_swallow", HOTKEY_SWALLOW)
TARGET_LIST = CONFIG.get("targets", DEFAULT_CONFIG["targets"])
TARGETS = {t["key"]: t for t in TARGET_LIST}


# ============================================================
# JS から呼ばれる API
# ============================================================
class Api:
    def __init__(self):
        self._model = None
        self.load_error = None
        self.recording = False
        self.frames = []
        self.stream = None
        # 録音ストリームの開閉を直列化する。GUIボタンとホットキーが別スレッドから
        # 同時に叩くと、close中のストリームへオーディオスレッドがコールバックして
        # SIGSEGVで落ちる（macOSのCoreAudioで顕在化）。
        self._rec_lock = threading.Lock()
        threading.Thread(target=self._load, daemon=True).start()

    def _load(self):
        try:
            self._model = WhisperModel(MODEL_SIZE, device="cpu", compute_type="int8")
        except Exception as e:
            import traceback
            traceback.print_exc()
            self.load_error = f"{type(e).__name__}: {e}"

    def ready(self):
        return self._model is not None

    def error(self):
        return self.load_error

    def targets(self):
        """UIのボタンを設定から自動生成するための一覧。"""
        return [{"key": t["key"], "label": t["label"], "color": t["color"]}
                for t in TARGET_LIST]

    # ---------- 録音 ----------
    # 注意(macOS): 録音のたびにストリームを開閉したり start/stop を繰り返すと、
    # CoreAudio が数サイクルでハング（Pa_StopStream が無限ブロック）または
    # SIGSEGV する。ストリームは初回に一度だけ開いて起動しっぱなしにし、
    # 録音の on/off は recording フラグだけで制御する。
    # 録音していない間の音声はその場で捨てられる（保存も送信もされない）。
    def _ensure_stream(self):
        """常時起動の入力ストリームを用意する。_rec_lock を保持して呼ぶこと。"""
        if self.stream is not None:
            return None
        try:
            stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1,
                                    dtype="float32", callback=self._cb)
            stream.start()
        except Exception as e:
            return str(e)
        self.stream = stream
        return None

    def start(self, key):
        if self._model is None:
            return False
        with self._rec_lock:
            if self.recording:
                return False
            err = self._ensure_stream()
            if err:
                return {"error": err}
            self.frames = []
            self.recording = True
            return True

    def _cb(self, indata, frames, t, status):
        # オーディオスレッドから呼ばれる。例外をC側へ漏らさない。
        try:
            if self.recording:
                self.frames.append(indata.copy())
        except Exception:
            pass

    def shutdown_stream(self):
        """終了時にストリームを止める。ハングし得るため呼び出し側でタイムアウトを設ける。"""
        with self._rec_lock:
            self.recording = False
            stream, self.stream = self.stream, None
        if stream is not None:
            try:
                stream.abort()
                stream.close()
            except Exception:
                pass

    # ---------- 認識 + 送信 ----------
    def stop(self, key):
        with self._rec_lock:
            if not self.recording:
                return {"ok": False, "msg": "（録音していません）"}
            self.recording = False
            frames, self.frames = self.frames, []

        if not frames:
            return {"ok": False, "msg": "（無音）"}
        audio = np.concatenate(frames, axis=0).flatten()
        if len(audio) < SAMPLE_RATE * 0.3:
            return {"ok": False, "msg": "（短すぎ）"}

        try:
            segs, _info = self._model.transcribe(audio, language=LANGUAGE, beam_size=5)
            text = "".join(s.text for s in segs).strip()
        except Exception as e:
            return {"ok": False, "msg": f"認識失敗: {e}"}
        if not text:
            return {"ok": False, "msg": "（認識なし）"}

        target = TARGETS[key]
        try:
            res = sender.send_text(target, text, press_enter=True, verify=True)
        except Exception as e:
            import traceback
            traceback.print_exc()
            return {"ok": False, "text": text, "msg": f"送信エラー: {e}"}

        return {"ok": res.ok, "text": text, "name": target["label"],
                "msg": res.msg, "detail": res.detail}

    def close(self):
        for w in webview.windows:
            w.destroy()


# ============================================================
# UI (HTML / CSS / JS)
# ============================================================
HTML = r"""
<!doctype html><html><head><meta charset="utf-8">
<style>
  * { box-sizing: border-box; -webkit-user-select: none; user-select: none; }
  html,body { margin:0; height:100%; font-family:"Yu Gothic UI","Segoe UI","Hiragino Sans","Hiragino Kaku Gothic ProN",sans-serif; }
  body {
    background: radial-gradient(120% 120% at 50% 0%, #f4f1ec 0%, #e7e1d8 100%);
    color:#3a3a3a; overflow:hidden;
  }
  .titlebar { height:26px; display:flex; align-items:center; justify-content:flex-end;
    -webkit-app-region:drag; padding:0 8px; gap:10px; }
  .titlebar .x { -webkit-app-region:no-drag; cursor:pointer; color:#8a8a8a;
    font-size:15px; line-height:1; padding:3px 5px; border-radius:6px; }
  .titlebar .x:hover { background:#00000012; color:#444; }

  .bubble-wrap { display:flex; justify-content:center; min-height:34px; padding:0 10px; }
  .bubble { position:relative; background:#fff; color:#333;
    border:1px solid #e3ddd3; border-radius:14px; padding:7px 12px; font-size:12px;
    line-height:1.35; box-shadow:0 3px 10px #0000000f; text-align:center;
    white-space:nowrap; overflow:hidden; text-overflow:ellipsis; max-width:260px; }
  .bubble::after { content:""; position:absolute; left:50%; bottom:-7px; transform:translateX(-50%);
    border-left:7px solid transparent; border-right:7px solid transparent; border-top:7px solid #fff; }

  .mic-area { display:flex; align-items:center; justify-content:center; height:96px; position:relative; }
  .mic { width:70px; height:70px; border-radius:50%; position:relative; z-index:2;
    background:radial-gradient(circle at 50% 35%, #eaf3ff, #cfe4ff);
    display:flex; align-items:center; justify-content:center;
    box-shadow:0 4px 14px #4a90e230, inset 0 0 0 1px #ffffffcc; transition:.2s; }
  .mic svg { width:30px; height:30px; fill:#5b6b7a; }
  .waves { position:absolute; inset:0; display:flex; align-items:center; justify-content:center; gap:44px; }
  .wave { width:26px; height:26px; border:3px solid #7db4ff; border-radius:50%; opacity:0; }
  body.rec .mic { box-shadow:0 0 0 6px #4a90e222, 0 4px 18px #4a90e255; }
  body.rec .wave { animation:pulse 1.1s infinite ease-out; }
  body.rec .wave.w2 { animation-delay:.35s; }
  @keyframes pulse { 0%{opacity:.7; transform:scale(.6);} 100%{opacity:0; transform:scale(1.5);} }

  .btns { display:grid; grid-template-columns:repeat(2,1fr); gap:9px;
    justify-content:center; padding:6px 14px 2px; }
  .btns.cols3 { grid-template-columns:repeat(3,1fr); }
  .btn { height:56px; border:none; border-radius:14px; color:#fff; cursor:pointer;
    display:flex; align-items:center; justify-content:center; padding:0 6px;
    font-size:12px; font-weight:700; line-height:1.2; text-align:center;
    box-shadow:0 4px 10px #00000022; transition:transform .08s, filter .1s; }
  .btn:active, .btn.holding { transform:translateY(1px) scale(.97); filter:brightness(1.08); }
  .btn.holding { box-shadow:0 0 0 3px #ffffff88, 0 0 16px #ffffffaa; }
  .btn[disabled]{ filter:grayscale(.5) brightness(.9); cursor:default; }

  .status { text-align:center; font-size:11px; color:#6b6b6b; padding:8px 12px 4px;
    min-height:26px; line-height:1.4; }
  .status b { color:#3a3a3a; }
  .detail { text-align:center; font-size:10px; color:#9a9a9a; padding:0 12px 10px;
    white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
</style></head>
<body>
  <div class="titlebar"><span class="x" onclick="requestClose()">✕</span></div>

  <div class="bubble-wrap"><div class="bubble" id="bubble">押しながら話してください</div></div>

  <div class="mic-area">
    <div class="waves"><span class="wave w1"></span><span class="wave w2"></span></div>
    <div class="mic">
      <svg viewBox="0 0 24 24"><path d="M12 15a3 3 0 0 0 3-3V6a3 3 0 0 0-6 0v6a3 3 0 0 0 3 3zm5-3a5 5 0 0 1-10 0H5a7 7 0 0 0 6 6.92V22h2v-3.08A7 7 0 0 0 19 12h-2z"/></svg>
    </div>
  </div>

  <div class="btns" id="btns"></div>

  <div class="status" id="status">モデル読込中…</div>
  <div class="detail" id="detail"></div>

<script>
  // JS→Python の pywebview ブリッジは macOS で稀に沈黙する（呼び出しや
  // 応答が失われ、復旧しない）。そのため操作は __vr_actions キューに積み、
  // Python 側が evaluate_js（こちらは安定）で吸い上げる方式にしている。
  // 状態（宛先一覧・準備完了・結果）も Python から push される。
  const bubble = document.getElementById('bubble');
  const status = document.getElementById('status');
  const detail = document.getElementById('detail');
  let active = null;
  let ready = false;
  window.__vr_actions = [];

  function setBtns(en){ document.querySelectorAll('.btn').forEach(b=>b.disabled=!en); }

  function buildButtons(list){
    const wrap = document.getElementById('btns');
    wrap.innerHTML = '';
    if(list.length === 3 || list.length > 4) wrap.classList.add('cols3');
    list.forEach(t=>{
      const b = document.createElement('button');
      b.className = 'btn';
      b.dataset.key = t.key;
      b.dataset.label = t.label;
      b.textContent = t.label;
      b.style.background = t.color;
      b.disabled = true;
      b.addEventListener('pointerdown', e=>{ b.setPointerCapture(e.pointerId); press(b); });
      b.addEventListener('pointerup',   e=>{ release(b); });
      b.addEventListener('pointercancel', e=>{ release(b); });
      wrap.appendChild(b);
    });
  }

  // --- Python から push される状態 ---
  window.pushTargets = function(list){
    if(document.querySelectorAll('.btn').length) return true;
    buildButtons(list);
    list.forEach((t,i)=>{
      if(i<4){
        const b=document.querySelector('.btn[data-key="'+t.key+'"]');
        if(b) b.textContent = t.label + '  [' + (i+1) + ']';
      }
    });
    return true;
  };
  window.pushReady = function(){
    if(ready) return;
    ready = true; setBtns(true);
    status.textContent = '準備OK — ボタンを押しながら話す';
  };
  window.pushError = function(err){ status.textContent = '読込失敗: '+err; };

  // --- 録音開始/結果の通知（ボタン・テンキー共通、Python から呼ばれる） ---
  window.hotkeyStart = function(key, label){
    const b = document.querySelector('.btn[data-key="'+key+'"]');
    if(b) b.classList.add('holding');
    document.body.classList.add('rec');
    bubble.textContent = '…';
    detail.textContent = '';
    status.innerHTML = 'Recording for <b>'+label+'</b>… (話してください)';
  };
  window.hotkeyResult = function(res){
    document.querySelectorAll('.btn').forEach(b=>b.classList.remove('holding'));
    document.body.classList.remove('rec');
    if(res && res.text) bubble.textContent = '「'+res.text+'」';
    status.textContent = (res && res.msg) ? res.msg
                        : (res && res.ok ? '送信しました' : '送信できませんでした');
    detail.textContent = (res && res.detail) ? res.detail : '';
  };

  function press(btn){
    if(!ready || active) return;
    active = btn.dataset.key;
    btn.classList.add('holding');
    document.body.classList.add('rec');
    bubble.textContent = '…';
    detail.textContent = '';
    status.innerHTML = 'Recording for <b>'+btn.dataset.label+'</b>… (話してください)';
    __vr_actions.push(["down", active]);
  }

  function release(btn){
    if(active !== btn.dataset.key) return;
    active = null;
    btn.classList.remove('holding');
    document.body.classList.remove('rec');
    status.textContent = '認識中…';
    __vr_actions.push(["up", btn.dataset.key]);
  }

  function requestClose(){ __vr_actions.push(["close", ""]); }
</script>
</body></html>
"""


def make_dispatcher(api, get_window):
    """画面ボタン・テンキー両方からの操作を1本のワーカーで直列処理する。
    重い処理（認識・送信）をイベント発生元のスレッドでやると
    キーボードフックやUIが固まるため、必ずキュー経由にする。"""
    import json
    import queue

    q = queue.Queue()

    def push_js(code):
        win = get_window()
        if win:
            try:
                win.evaluate_js(code)
            except Exception:
                pass

    def worker():
        while True:
            action, key = q.get()
            try:
                if action == "close":
                    api.close()
                    continue
                t = TARGETS.get(key)
                if t is None:
                    continue
                if action == "down":
                    api.start(key)
                    push_js(f"window.hotkeyStart && hotkeyStart({json.dumps(key)},"
                            f"{json.dumps(t['label'])})")
                elif action == "up":
                    res = api.stop(key)
                    push_js(f"window.hotkeyResult && hotkeyResult({json.dumps(res)})")
            except Exception:
                import traceback
                traceback.print_exc()

    threading.Thread(target=worker, daemon=True).start()
    return q


def setup_hotkeys(q):
    """テンキーを各宛先に割り当てる。操作はディスパッチャのキューへ流すだけ。"""
    listener = hotkeys.HotkeyListener(
        hotkeys.numpad_map(len(TARGET_LIST)),
        on_down=lambda i: q.put(("down", TARGET_LIST[i]["key"])),
        on_up=lambda i: q.put(("up", TARGET_LIST[i]["key"])),
        swallow=HOTKEY_SWALLOW,
    )
    return listener if listener.start() else None


def ui_pump(api, window, q):
    """JSブリッジに依存しないUI連携（webview.start() 後に別スレッドで動く）。
    pywebview の JS→Python ブリッジは macOS で稀に沈黙して復旧しないため、
    Python側から evaluate_js（安定している）で状態を押し込み、
    画面ボタンの操作は DOM 上のキュー __vr_actions を吸い上げる。"""
    import json

    targets_json = json.dumps(
        [{"key": t["key"], "label": t["label"], "color": t["color"]}
         for t in TARGET_LIST])
    sent_targets = False
    sent_ready = False
    sent_error = None
    while True:
        time.sleep(0.08)
        try:
            if not sent_targets:
                ok = window.evaluate_js(
                    f"window.pushTargets ? pushTargets({targets_json}) : false")
                if not ok:
                    continue
                sent_targets = True
            if not sent_ready:
                if api.ready():
                    window.evaluate_js("window.pushReady && pushReady()")
                    sent_ready = True
                else:
                    err = api.error()
                    if err and err != sent_error:
                        sent_error = err
                        window.evaluate_js(
                            f"window.pushError && pushError({json.dumps(err)})")
            acts = window.evaluate_js(
                "JSON.stringify((window.__vr_actions||[]).splice(0))")
            if acts:
                for a in json.loads(acts):
                    if isinstance(a, list) and len(a) == 2:
                        q.put((a[0], a[1]))
        except Exception:
            return          # ウィンドウが閉じられた等


if __name__ == "__main__":
    api = Api()
    _win_ref = {}
    _q = make_dispatcher(api, lambda: _win_ref.get("w"))

    if HOTKEYS_ENABLED:
        setup_hotkeys(_q)

    _window = webview.create_window(
        "Voice Router", html=HTML,
        width=300, height=300 + 66 * ((len(TARGET_LIST) + 1) // 2),
        frameless=True, easy_drag=True,
        on_top=True, background_color="#EAE6E1",
    )
    _win_ref["w"] = _window
    webview.start(ui_pump, (api, _window, _q))

    # 終了処理: CoreAudio の stop/close はハングし得るため、別スレッドで
    # 試みて2秒で見切り、確実にプロセスを終える（コールバック中の
    # use-after-free クラッシュも、teardown を踏まないことで避ける）。
    _t = threading.Thread(target=api.shutdown_stream, daemon=True)
    _t.start()
    _t.join(2.0)
    os._exit(0)
