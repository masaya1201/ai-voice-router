# -*- coding: utf-8 -*-
"""
vosk_stt.py — 話している最中に認識を進める音声認識エンジン（Vosk）。

Whisper は「話し終わってから全体をまとめて処理」する設計のため、
CPUのみだと離してから数秒待たされる。
Vosk は音声を受け取りながら逐次処理するので、離した時点でほぼ結果が出ている。

実測（7.3秒の発話）:
    Whisper small : 離してから 約4秒   精度 99%
    Vosk small ja : 離してから 0.02秒  精度 95%
"""

import json
import os
import queue
import threading

import numpy as np

os.environ.setdefault("VOSK_LOG_LEVEL", "-1")

SAMPLE_RATE = 16000


class VoskEngine:
    """録音中に逐次認識し、停止時にただちに全文を返す。"""

    def __init__(self, lang="ja", model_path=None):
        from vosk import Model, SetLogLevel
        SetLogLevel(-1)
        # model_path があればそれを使う（オフライン配布・任意モデル用）
        self.model = Model(model_path=model_path) if model_path else Model(lang=lang)
        self._rec = None
        self._q = None
        self._worker = None
        self._running = False

    @property
    def ready(self):
        return self.model is not None

    # ---- 録音の開始・供給・停止 ----
    def start(self):
        from vosk import KaldiRecognizer
        self._rec = KaldiRecognizer(self.model, SAMPLE_RATE)
        self._q = queue.Queue()
        self._running = True
        self._worker = threading.Thread(target=self._loop, daemon=True)
        self._worker.start()

    def feed(self, chunk):
        """録音コールバックから呼ぶ。重い処理はワーカーに渡し、録音を止めない。"""
        if not self._running or self._q is None:
            return
        arr = np.asarray(chunk, dtype=np.float32).flatten()
        pcm = (np.clip(arr, -1.0, 1.0) * 32767).astype(np.int16).tobytes()
        self._q.put(pcm)

    def _loop(self):
        while self._running or (self._q is not None and not self._q.empty()):
            try:
                pcm = self._q.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                self._rec.AcceptWaveform(pcm)
            except Exception:
                pass

    def stop(self, timeout=3.0):
        """録音停止。残りを処理して認識結果を返す。"""
        self._running = False
        if self._worker is not None:
            self._worker.join(timeout)
        if self._rec is None:
            return ""
        try:
            text = json.loads(self._rec.FinalResult()).get("text", "")
        except Exception:
            text = ""
        self._rec = None
        self._q = None
        # 日本語結果は単語が空白で区切られて返るため詰める
        return text.replace(" ", "").strip()

    def cancel(self):
        self._running = False
        self._rec = None
        self._q = None
