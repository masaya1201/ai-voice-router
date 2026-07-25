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

# .app に固めた場合、テンキー監視の子プロセスもこの実行ファイルとして起動される
# （PyInstaller はスクリプトのパスを渡しても無視して常に本体を実行するため）。
# GUIを立ち上げる前にここで拾って、キー監視だけを行って終わる。
if "--hotkey-worker" in sys.argv:
    import hotkeys_mac
    hotkeys_mac.run_worker_from_argv()
    sys.exit(0)

os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")       # Anaconda OpenMP重複回避
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

# pythonw(コンソール無し)起動では stdout/stderr が None になり、ライブラリの
# 警告出力で読込スレッドがクラッシュする。ログファイルへ退避して回避。
# 実行フォルダ（.exe化した場合も設定/ログを実行ファイルの隣に置く）
if getattr(sys, "frozen", False):
    if sys.platform == "darwin":
        # macOS の .app は中身が読み取り専用扱い（署名の対象）なので、
        # 設定とログはユーザーのアプリケーションサポートに置く。
        APP_DIR = os.path.expanduser(
            "~/Library/Application Support/VoiceRouter")
        os.makedirs(APP_DIR, exist_ok=True)
    else:
        APP_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))

# アイコンなど同梱リソースの場所（PyInstaller は _MEIPASS に展開する）
RES_DIR = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))

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
from punctuate import punctuate
import vocab

# ============================================================
# 設定
# ============================================================
# 音声認識エンジン
#   "vosk"    : 話している最中に認識するため、離した瞬間に送信される（推奨）
#   "whisper" : 精度は少し高いが、離してから数秒待つ（CPUのみの場合）
ENGINE = "vosk"

# 認識結果に句点(。)を補う。Voskは句読点を出さないため。
PUNCTUATE = True

MODEL_SIZE = "small"   # base(速いが日本語精度が落ちる) / small(推奨) / medium(高精度・重い)
# 言語を固定すると判定処理が省けて大幅に速い。None=自動判定(遅い)
LANGUAGE = "ja"
SAMPLE_RATE = 16000
# 使用するマイク。null=Windowsの既定。名前の一部（例 "ヘッドセット"）か番号で指定できる。
# 内蔵マイクはスピーカーの音（動画や音楽）を拾うため、ヘッドセットの方が正確に認識できる。
INPUT_DEVICE = None
# CPUスレッド数。全コアを使うとかえって遅くなる（実測: 12スレッドは6スレッドの約2倍遅い）
CPU_THREADS = 6

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
    # macOS: デスクトップアプリは kind="app" + app= にアプリ名（.app の名前）。
    # ブラウザは Edge ではなく Chrome を既定にする。
    _DEFAULT_TARGETS = [
        {"key": "claude", "label": "Claude", "color": "#d97757",
         "kind": "app", "app": "Claude"},
        # タブ名は会話タイトルに変わるため URL でも判定する
        {"key": "chatgpt_web", "label": "ChatGPT Web", "color": "#3a8fd6",
         "kind": "browser_tab", "browser": "chrome",
         "tab": "chatgpt", "url": "chatgpt.com"},
        {"key": "gemini", "label": "Gemini", "color": "#8e6fd8",
         "kind": "browser_tab", "browser": "chrome",
         "tab": "gemini", "url": "gemini.google.com"},
        {"key": "claude_web", "label": "Claude Web", "color": "#b8622f",
         "kind": "browser_tab", "browser": "chrome",
         "tab": "claude", "url": "claude.ai"},
        # kind="click" は録音せず、ページ内のボタンを押すだけの宛先。
        # ChatGPTのライブ音声会話を起動する（押した瞬間に会話が始まる）。
        # macOSでは Chrome の「Apple Events からの JavaScript を許可」が必要。
        {"key": "gpt_voice", "label": "🎙 音声会話 開始", "color": "#0d8f6f",
         "kind": "click", "browser": "chrome",
         "tab": "chatgpt", "url": "chatgpt.com",
         "button": ["音声を開始する", "Start voice mode", "音声モードを開始"]},
        {"key": "gpt_voice_end", "label": "■ 音声会話 終了", "color": "#8a8f98",
         "kind": "click", "browser": "chrome",
         "tab": "chatgpt", "url": "chatgpt.com",
         "button": ["音声を終了する", "End voice mode", "音声モードを終了"]},
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
    ]

DEFAULT_CONFIG = {
    "engine": ENGINE,
    "punctuate": PUNCTUATE,
    # Voskのモデル。未指定なら小さい既定モデル(48MB)。
    # "vosk-model-ja-0.22" にすると大きい高精度モデル(約1.5GB・初回に自動取得)
    "vosk_model_name": None,
    "model_size": MODEL_SIZE,
    "language": LANGUAGE,
    "cpu_threads": CPU_THREADS,
    "input_device": INPUT_DEVICE,
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
ENGINE = (CONFIG.get("engine") or ENGINE).lower()
PUNCTUATE = CONFIG.get("punctuate", PUNCTUATE)
MODEL_SIZE = CONFIG.get("model_size", MODEL_SIZE)
LANGUAGE = CONFIG.get("language", LANGUAGE)
CPU_THREADS = CONFIG.get("cpu_threads", CPU_THREADS)
INPUT_DEVICE = CONFIG.get("input_device", INPUT_DEVICE)
HOTKEYS_ENABLED = CONFIG.get("hotkeys_enabled", HOTKEYS_ENABLED)
HOTKEY_SWALLOW = CONFIG.get("hotkey_swallow", HOTKEY_SWALLOW)
TARGET_LIST = CONFIG.get("targets", DEFAULT_CONFIG["targets"])
TARGETS = {t["key"]: t for t in TARGET_LIST}

# 認識の速度に効く設定。beam_size=1・タイムスタンプ無し・無音除去で大幅に短縮できる
TRANSCRIBE_OPTS = dict(
    language=LANGUAGE,
    beam_size=1,                    # 5→1 で大幅に短縮（精度はほぼ変わらず）
    without_timestamps=True,
    condition_on_previous_text=False,
    vad_filter=True,                # 無音を除いて処理量を減らす
    chunk_length=10,                # 既定30秒→10秒。短い発話でも30秒分処理するのを避ける
)


def resolve_input_device(spec):
    """設定のマイク指定（番号 or 名前の一部）を実際のデバイス番号に変換する。
    見つからなければ None（Windowsの既定マイク）を返す。"""
    if spec is None or spec == "":
        return None
    try:
        if isinstance(spec, int):
            return spec
        text = str(spec).strip()
        if text.isdigit():
            return int(text)
        low = text.lower()
        for i, d in enumerate(sd.query_devices()):
            if d["max_input_channels"] > 0 and low in d["name"].lower():
                return i
    except Exception:
        import traceback
        traceback.print_exc()
    return None


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
        self.peak = 0.0         # 直近の録音で観測した最大音量
        self.app_stt = None     # アプリ自身の音声入力を使用中の状態
        self.vosk = None        # 逐次認識エンジン（ENGINE="vosk" のとき）
        # 録音の開始/停止を直列化する。画面ボタンとテンキーが別スレッドから
        # 同時に叩くと、オーディオスレッドと競合してクラッシュし得る。
        self._rec_lock = threading.Lock()
        threading.Thread(target=self._load, daemon=True).start()

    def _open_mic_early(self):
        """起動時にマイクを開いておく。
        macOS のマイク許可ダイアログはマイクを開いた瞬間に出るため、
        最初の録音のときに出ると、その1回目の発話が丸ごと失われる。
        起動直後に済ませておけば、押して話した分は必ず録れる。"""
        try:
            with self._rec_lock:
                self._ensure_stream()
        except Exception:
            pass

    def _load(self):
        try:
            if ENGINE == "vosk":
                from vosk_stt import VoskEngine
                self.vosk = VoskEngine(lang=(LANGUAGE or "ja"),
                                       model_path=CONFIG.get("vosk_model_path"),
                                       model_name=CONFIG.get("vosk_model_name"))
                self.model = self.vosk        # 準備完了の目印として共用
                self._open_mic_early()
                return
            from faster_whisper import WhisperModel
            m = WhisperModel(MODEL_SIZE, device="cpu", compute_type="int8",
                             cpu_threads=CPU_THREADS)
            # 初回だけ極端に遅くならないよう、空音声で一度動かして温めておく
            try:
                segs, _ = m.transcribe(np.zeros(SAMPLE_RATE, dtype="float32"),
                                       **TRANSCRIBE_OPTS)
                list(segs)
            except Exception:
                pass
            self.model = m
            self._open_mic_early()
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
    # ---- アプリ自身の音声入力を使うモード ----
    def _app_stt_start(self, target):
        """送信先アプリの音声入力ボタンを押して録音を任せる。
        書き起こしはアプリ側（クラウド）が行うため、ローカル認識より速い。"""
        prep = sender.prepare_target(target)
        if prep.error is not None:
            self.app_stt = None
            return {"error": prep.error.msg}
        r = sender.click_named_button(prep.hwnd, target.get("stt_start", []),
                                      sender._is_browser(target), target["label"])
        if not r.ok:
            self.app_stt = None
            return {"error": r.msg}
        self.app_stt = {"target": target, "prep": prep}
        return True

    def _app_stt_stop(self, target):
        st = self.app_stt
        self.app_stt = None
        if st is None:
            return {"ok": False, "msg": "（音声入力が開始されていません）"}
        prep = st["prep"]
        page_only = sender._is_browser(target)
        r = sender.click_named_button(prep.hwnd, target.get("stt_submit", []),
                                      page_only, target["label"])
        if not r.ok:
            return {"ok": False, "msg": r.msg, "detail": r.detail}
        # アプリが書き起こして入力欄に入れるのを待つ
        text = ""
        if prep.composer is not None:
            text = sender.wait_for_composer_text(prep.composer)
        if not text:
            return {"ok": False, "msg": "（書き起こしが入りませんでした）"}
        sender.focus_window(prep.hwnd)
        time.sleep(0.15)
        if prep.composer is not None:
            sender.focus_composer(prep.composer, timeout=1.2)
        sender._key(sender.VK_RETURN)
        sender._key(sender.VK_RETURN, True)
        return {"ok": True, "text": text, "name": target["label"],
                "msg": f"✓ {target['label']} へ送信"}

    def start(self, key):
        target = TARGETS.get(key)
        if target is not None and target.get("stt") == "app":
            if self.recording or self.app_stt:
                return False
            return self._app_stt_start(target)
        if self.model is None:
            return False
        with self._rec_lock:
            if self.recording:
                return False
            err = self._ensure_stream()
            if err:
                return {"error": err}
            self.frames = []
            self.peak = 0.0
            if ENGINE == "vosk":
                self.vosk.start()          # 話している間に逐次認識させる
            self.recording = True
            return True

    def _ensure_stream(self):
        """入力ストリームを用意する。_rec_lock を保持して呼ぶこと。
        macOS注意: 録音のたびにストリームを開閉したり start/stop を繰り返すと、
        CoreAudio が数サイクルでハング（Pa_StopStream が無限ブロック）または
        SIGSEGV する。そのため一度開いたら起動しっぱなしにし、録音の on/off は
        recording フラグだけで制御する。録音していない間の音声はコールバックで
        その場で捨てられる（保存も認識も送信もされない）。"""
        if self.stream is not None:
            return None
        try:
            stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1,
                                    dtype="float32", callback=self._cb,
                                    device=resolve_input_device(INPUT_DEVICE))
            stream.start()
        except Exception as e:
            return str(e)
        self.stream = stream
        return None

    def _cb(self, indata, frames, t, status):
        # オーディオスレッドから呼ばれる。例外をC側へ漏らさない。
        try:
            if not self.recording:
                return
            # 音量を控えておく。認識できなかったときに「そもそも音が
            # 入っていない」のか「入っているが聞き取れなかった」のかを
            # 区別して伝えるため（マイク権限の切り分けに要る）。
            peak = float(np.abs(indata).max())
            if peak > self.peak:
                self.peak = peak
            if ENGINE == "vosk":
                self.vosk.feed(indata)     # 重い処理はエンジン側のワーカーが行う
            else:
                self.frames.append(indata.copy())
        except Exception:
            pass

    # マイクが無音とみなす閾値。実測: 静かな部屋の暗騒音でピーク約0.03、
    # 普通に話すと 0.2 以上になる。権限が無いときは完全な 0 が返る。
    SILENT_PEAK = 0.004

    def _no_speech_result(self):
        """認識結果が空だったときに、原因が分かる形で返す。"""
        if self.peak < self.SILENT_PEAK:
            return {"ok": False, "msg": "✗ マイクから音が入っていません",
                    "detail": "システム設定 > プライバシーとセキュリティ > "
                              "マイク でこのアプリを許可してください"}
        return {"ok": False, "msg": "（認識なし）",
                "detail": f"音は入っています(音量 {self.peak:.2f})。"
                          "もう少しはっきり話すか、話し始める前にボタンを押してください"}

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
        target0 = TARGETS.get(key)
        if target0 is not None and target0.get("stt") == "app":
            try:
                return self._app_stt_stop(target0)
            except Exception as e:
                import traceback
                traceback.print_exc()
                self.app_stt = None
                return {"ok": False, "msg": f"送信エラー: {e}"}
        # ストリームは閉じない（_ensure_stream のコメント参照）。録音を止めるのは
        # フラグだけで、以降の音声はコールバックで捨てられる。
        with self._rec_lock:
            if not self.recording:
                return {"ok": False, "msg": "（録音していません）"}
            self.recording = False

        target = TARGETS[key]

        # --- Vosk: 認識は録音中に終わっているので、ここでは結果を受け取るだけ ---
        if ENGINE == "vosk":
            prep_box = {}
            th = threading.Thread(
                target=lambda: prep_box.update(p=sender.prepare_target_threadsafe(target)),
                daemon=True)
            th.start()
            text = vocab.get(APP_DIR).apply(self.vosk.stop())
            if PUNCTUATE:
                text = punctuate(text)
            th.join(8.0)
            if not text:
                return self._no_speech_result()
            try:
                res = sender.send_text(target, text, press_enter=True, verify=True,
                                       prepared=prep_box.get("p"))
            except Exception as e:
                import traceback
                traceback.print_exc()
                return {"ok": False, "text": text, "msg": f"送信エラー: {e}"}
            return {"ok": res.ok, "text": text, "name": target["label"],
                    "msg": res.msg, "detail": res.detail}

        if not self.frames:
            return self._no_speech_result()
        audio = np.concatenate(self.frames, axis=0).flatten()
        if len(audio) < SAMPLE_RATE * 0.3:
            return {"ok": False, "msg": "（短すぎ）"}

        # 送信先の準備（ウィンドウ/タブ切替・入力欄フォーカス）は音声認識と並行して行う。
        # 直列にすると認識が終わってから数秒待つことになるため。
        prep_box = {}

        def _prepare():
            try:
                prep_box["p"] = sender.prepare_target_threadsafe(target)
            except Exception:
                import traceback
                traceback.print_exc()

        prep_thread = threading.Thread(target=_prepare, daemon=True)
        prep_thread.start()

        try:
            segs, _info = self.model.transcribe(audio, **TRANSCRIBE_OPTS)
            text = "".join(s.text for s in segs).strip()
            text = vocab.get(APP_DIR).apply(text)
            if PUNCTUATE:
                text = punctuate(text)
        except Exception as e:
            return {"ok": False, "msg": f"認識失敗: {e}"}

        prep_thread.join(8.0)
        prepared = prep_box.get("p")

        if not text:
            return self._no_speech_result()

        try:
            res = sender.send_text(target, text, press_enter=True, verify=True,
                                   prepared=prepared)
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
  html,body { margin:0; height:100%;
    font-family:"Yu Gothic UI","Segoe UI","Hiragino Sans","Hiragino Kaku Gothic ProN",sans-serif; }
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
  // JS→Python の pywebview ブリッジは macOS で稀に沈黙する（呼び出しや応答が
  // 失われ、復旧しない）。そのため操作は __vr_actions キューに積み、Python 側が
  // evaluate_js（こちらは安定）で吸い上げる。状態も Python から push される。
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

  // 画面が固まった原因が分からなくならないよう、JSのエラーは必ず表示する
  window.onerror = function(msg, src, line){
    status.textContent = 'JSエラー: ' + msg + ' (line ' + line + ')';
  };

  // --- Python から push される状態（pushTargets / pushReady / pushError） ---
  window.pushTargets = function(list){
    if(document.querySelectorAll('.btn').length) return true;   // 二重生成の防止
    buildButtons(list);
    list.forEach((t,i)=>{
      if(i<9){
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

  window.actionResult = function(res){
    status.textContent = (res && res.msg) ? res.msg : '起動できませんでした';
    detail.textContent = (res && res.detail) ? res.detail : '';
    bubble.textContent = (res && res.ok) ? '音声会話を開始しました' : '押しながら話してください';
  };

  function runAction(btn){
    if(!ready || active) return;
    status.innerHTML = '<b>'+btn.dataset.label+'</b> を起動中…';
    detail.textContent = '';
    bubble.textContent = '…';
    __vr_actions.push(["click", btn.dataset.key]);
  }

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
                if t.get("kind") == "click":
                    # 押すだけの宛先は1回だけ実行する（テンキーは離したときに1回）
                    if action in ("click", "up"):
                        res = api.trigger(key)
                        push_js(f"window.actionResult && actionResult({json.dumps(res)})")
                    continue
                if action == "down":
                    r = api.start(key)
                    # 録音を始められなかった理由（マイクが使えない等）は
                    # 黙って捨てず必ず画面に出す。
                    if isinstance(r, dict) and r.get("error"):
                        push_js("window.hotkeyResult && hotkeyResult("
                                + json.dumps({
                                    "ok": False,
                                    "msg": "✗ マイクを開けませんでした",
                                    "detail": str(r["error"])[:120]}) + ")")
                        continue
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

    targets_json = json.dumps(api.targets())
    sent_targets = False
    sent_ready = False
    sent_error = None
    if IS_MAC:
        set_dock_icon()     # pywebview 起動後でないとアイコンが戻される
    while True:
        time.sleep(0.08)
        try:
            if not sent_targets:
                if not window.evaluate_js(
                        f"window.pushTargets ? pushTargets({targets_json}) : false"):
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


def set_dock_icon():
    """macOS: Dock とアプリ切替(Cmd+Tab)のアイコンを Voice Router のものにする。
    Python は自身を Python.app として登録するため、.app から起動しても
    そのままでは Python のアイコンが出る。実行時に差し替えて回避する。

    pywebview の起動時にアイコンが戻されるため、必ず webview.start() の
    「後」に呼ぶこと。AppKit の描画はメインスレッドで行う必要があるので、
    別スレッドから呼ばれても安全なようにメインスレッドへ渡す。

    なお、ターミナルや .command から起動した場合は Python 自身のプロセスとして
    登録されるため、これでも Python のアイコンのままになることがある。
    アイコンまで含めて完全にするには build_app.command で .app を作る。"""
    icns = os.path.join(RES_DIR, "voice_router.icns")
    if not os.path.exists(icns):
        return
    try:
        import AppKit
        img = AppKit.NSImage.alloc().initWithContentsOfFile_(icns)
        if img is None:
            return
        AppKit.NSApplication.sharedApplication() \
            .performSelectorOnMainThread_withObject_waitUntilDone_(
                "setApplicationIconImage:", img, False)
    except Exception:
        pass


if __name__ == "__main__":
    api = Api()
    _win_ref = {}
    _q = make_dispatcher(api, lambda: _win_ref.get("w"))

    if HOTKEYS_ENABLED:
        setup_hotkeys(_q)

    _window = webview.create_window(
        "Voice Router", html=HTML,
        width=300 if len(TARGET_LIST) <= 4 else 340,
        height=245 + 66 * ((len(TARGET_LIST) + (3 if len(TARGET_LIST) > 4 else 2) - 1)
                           // (3 if len(TARGET_LIST) > 4 else 2)),
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
