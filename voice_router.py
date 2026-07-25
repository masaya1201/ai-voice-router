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
DEFAULT_CONFIG = {
    "model_size": MODEL_SIZE,
    "language": LANGUAGE,
    "hotkeys_enabled": HOTKEYS_ENABLED,
    "hotkey_swallow": HOTKEY_SWALLOW,
    "targets": [
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
        # kind="click" は録音せず、アプリ内のボタンを押すだけの宛先。
        # ChatGPTのライブ音声会話を起動する（押した瞬間に会話が始まる）。
        {"key": "gpt_voice", "label": "🎙 音声会話 開始", "color": "#0d8f6f",
         "kind": "click", "browser": "edge",
         "tab": "chatgpt", "url": "chatgpt.com",
         "button": ["音声を開始する", "Start voice mode", "音声モードを開始"]},
        {"key": "gpt_voice_end", "label": "■ 音声会話 終了", "color": "#8a8f98",
         "kind": "click", "browser": "edge",
         "tab": "chatgpt", "url": "chatgpt.com",
         "button": ["音声を終了する", "End voice mode", "音声モードを終了"]},
    ],
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
        self.model = None
        self.load_error = None
        self.recording = False
        self.frames = []
        self.stream = None
        threading.Thread(target=self._load, daemon=True).start()

    def _load(self):
        try:
            self.model = WhisperModel(MODEL_SIZE, device="cpu", compute_type="int8")
        except Exception as e:
            import traceback
            traceback.print_exc()
            self.load_error = f"{type(e).__name__}: {e}"

    def ready(self):
        return self.model is not None

    def error(self):
        return self.load_error

    def targets(self):
        """UIのボタンを設定から自動生成するための一覧。
        mode='action' は押しただけで実行（録音しない）、'talk' は押しながら話す。"""
        return [{"key": t["key"], "label": t["label"], "color": t["color"],
                 "mode": "action" if t.get("kind") == "click" else "talk"}
                for t in TARGET_LIST]

    def trigger(self, key):
        """録音せずにアプリ内のボタンを押す（ライブ音声会話の起動など）。"""
        target = TARGETS.get(key)
        if target is None:
            return {"ok": False, "msg": "不明な宛先です"}
        try:
            res = sender.press_button(target)
        except Exception as e:
            import traceback
            traceback.print_exc()
            return {"ok": False, "msg": f"起動エラー: {e}"}
        return {"ok": res.ok, "msg": res.msg, "detail": res.detail}

    # ---------- 録音 ----------
    def start(self, key):
        if self.model is None or self.recording:
            return False
        self.recording = True
        self.frames = []
        try:
            self.stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1,
                                         dtype="float32", callback=self._cb)
            self.stream.start()
            return True
        except Exception as e:
            self.recording = False
            return {"error": str(e)}

    def _cb(self, indata, frames, t, status):
        if self.recording:
            self.frames.append(indata.copy())

    # ---------- 認識 + 送信 ----------
    def stop(self, key):
        if not self.recording:
            return {"ok": False, "msg": "（録音していません）"}
        self.recording = False
        try:
            if self.stream:
                self.stream.stop()
                self.stream.close()
                self.stream = None
        except Exception:
            pass

        if not self.frames:
            return {"ok": False, "msg": "（無音）"}
        audio = np.concatenate(self.frames, axis=0).flatten()
        if len(audio) < SAMPLE_RATE * 0.3:
            return {"ok": False, "msg": "（短すぎ）"}

        try:
            segs, _info = self.model.transcribe(audio, language=LANGUAGE, beam_size=5)
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
  html,body { margin:0; height:100%; font-family:"Yu Gothic UI","Segoe UI",sans-serif; }
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
  <div class="titlebar"><span class="x" onclick="pywebview.api.close()">✕</span></div>

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
  const bubble = document.getElementById('bubble');
  const status = document.getElementById('status');
  const detail = document.getElementById('detail');
  let active = null;
  let ready = false;

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
      b.dataset.mode = t.mode || 'talk';
      b.textContent = t.label;
      b.style.background = t.color;
      b.disabled = true;
      if(b.dataset.mode === 'action'){
        // 押すだけで実行（録音しない）
        b.addEventListener('click', ()=>{ runAction(b); });
      }else{
        b.addEventListener('pointerdown', e=>{ b.setPointerCapture(e.pointerId); press(b); });
        b.addEventListener('pointerup',   e=>{ release(b); });
        b.addEventListener('pointercancel', e=>{ release(b); });
      }
      wrap.appendChild(b);
    });
  }

  function waitReady(){
    if(!window.pywebview || !pywebview.api){ return setTimeout(waitReady,120); }
    pywebview.api.ready().then(r=>{
      if(r){ ready=true; setBtns(true); status.textContent='準備OK — ボタンを押しながら話す'; }
      else pywebview.api.error().then(err=>{
        if(err){ status.textContent='読込失敗: '+err; }
        else setTimeout(waitReady,300);
      });
    });
  }

  // 画面が固まった原因が分からなくならないよう、JSのエラーは必ず表示する
  window.onerror = function(msg, src, line){
    status.textContent = 'JSエラー: ' + msg + ' (line ' + line + ')';
  };

  // pywebview は api の器を先に作り、メソッドを後から追加する。
  // 器ができた時点で呼ぶと "not a function" で固まるため、メソッドが揃うまで待つ。
  let initTries = 0;
  let initDone = false;
  function init(){
    if(initDone) return;
    if(!window.pywebview || !pywebview.api || typeof pywebview.api.targets !== 'function'){
      if(++initTries > 300){ status.textContent = 'APIに接続できません'; return; }
      return setTimeout(init, 100);
    }
    initDone = true;
    pywebview.api.targets().catch(e=>{
      status.textContent = 'targets()失敗: ' + e;
      return null;
    }).then(list=>{
      if(!list){ return; }
      buildButtons(list);
      list.forEach((t,i)=>{
        if(i<4){
          const b=document.querySelector('.btn[data-key="'+t.key+'"]');
          if(b) b.textContent = t.label + '  [' + (i+1) + ']';
        }
      });
      waitReady();
    });
  }
  init();
  window.addEventListener('pywebviewready', init);

  // --- テンキー(グローバルホットキー)からの通知 ---
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

  function runAction(btn){
    if(!ready || active) return;
    status.innerHTML = '<b>'+btn.dataset.label+'</b> を起動中…';
    detail.textContent = '';
    bubble.textContent = '…';
    pywebview.api.trigger(btn.dataset.key).then(res=>{
      status.textContent = (res && res.msg) ? res.msg : '起動できませんでした';
      detail.textContent = (res && res.detail) ? res.detail : '';
      bubble.textContent = (res && res.ok) ? '音声会話を開始しました' : '押しながら話してください';
    });
  }

  function press(btn){
    if(!ready || active) return;
    active = btn.dataset.key;
    btn.classList.add('holding');
    document.body.classList.add('rec');
    bubble.textContent = '…';
    detail.textContent = '';
    status.innerHTML = 'Recording for <b>'+btn.dataset.label+'</b>… (話してください)';
    pywebview.api.start(active);
  }

  function release(btn){
    if(active !== btn.dataset.key) return;
    active = null;
    btn.classList.remove('holding');
    document.body.classList.remove('rec');
    status.textContent = '認識中…';
    pywebview.api.stop(btn.dataset.key).then(res=>{
      if(res && res.text) bubble.textContent = '「'+res.text+'」';
      status.textContent = (res && res.msg) ? res.msg : (res && res.ok ? '送信しました' : '送信できませんでした');
      detail.textContent = (res && res.detail) ? res.detail : '';
    });
  }

</script>
</body></html>
"""


def setup_hotkeys(api, get_window):
    """テンキーを各宛先に割り当てる。
    フックのコールバック内で重い処理をするとキーボード全体が固まるため、
    受け取った操作は必ず専用ワーカーへ渡して即座に返す。"""
    import json
    import queue

    q = queue.Queue()

    def worker():
        while True:
            action, idx = q.get()
            t = TARGET_LIST[idx]
            try:
                win = get_window()
                if t.get("kind") == "click":
                    # 押すだけの宛先は、キーを離したときに1回だけ実行する
                    if action == "up":
                        res = api.trigger(t["key"])
                        if win:
                            win.evaluate_js(
                                f"window.hotkeyResult && hotkeyResult({json.dumps(res)})")
                    continue
                if action == "down":
                    api.start(t["key"])
                    if win:
                        win.evaluate_js(
                            f"window.hotkeyStart && hotkeyStart({json.dumps(t['key'])},"
                            f"{json.dumps(t['label'])})")
                else:
                    res = api.stop(t["key"])
                    if win:
                        win.evaluate_js(
                            f"window.hotkeyResult && hotkeyResult({json.dumps(res)})")
            except Exception:
                import traceback
                traceback.print_exc()

    threading.Thread(target=worker, daemon=True).start()

    listener = hotkeys.HotkeyListener(
        hotkeys.numpad_map(len(TARGET_LIST)),
        on_down=lambda i: q.put(("down", i)),
        on_up=lambda i: q.put(("up", i)),
        swallow=HOTKEY_SWALLOW,
    )
    return listener if listener.start() else None


if __name__ == "__main__":
    api = Api()

    if HOTKEYS_ENABLED:
        _win_ref = {}
        setup_hotkeys(api, lambda: _win_ref.get("w"))

    _window = webview.create_window(
        "Voice Router", html=HTML, js_api=api,
        width=300 if len(TARGET_LIST) <= 4 else 340,
        height=245 + 66 * ((len(TARGET_LIST) + (3 if len(TARGET_LIST) > 4 else 2) - 1)
                           // (3 if len(TARGET_LIST) > 4 else 2)),
        frameless=True, easy_drag=True,
        on_top=True, background_color="#EAE6E1",
    )
    if HOTKEYS_ENABLED:
        _win_ref["w"] = _window
    webview.start()
