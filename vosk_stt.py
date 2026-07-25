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

    def __init__(self, lang="ja", model_path=None, model_name=None):
        from vosk import Model, SetLogLevel
        SetLogLevel(-1)
        # model_path: 手元のフォルダを直接指定（オフライン配布用）
        # model_name: 例 "vosk-model-ja-0.22"（大きい高精度モデル・約1.5GB）
        if model_path:
            self.model = Model(model_path=model_path)
        elif model_name:
            self.model = Model(model_name=model_name)
        else:
            self.model = Model(lang=lang)
        self._rec = None
        self._q = None
        self._worker = None
        self._running = False
        self._fed = False
        # 認識器(KaldiRecognizer)はスレッド安全ではない。AcceptWaveform と
        # FinalResult が同時に走ると Kaldi がアサートに失敗して abort() し、
        # プロセスごと落ちる。必ずこのロックの中だけで触ること。
        self._lock = threading.Lock()

    @property
    def ready(self):
        return self.model is not None

    # ---- 録音の開始・供給・停止 ----
    def _end_worker(self, timeout):
        """ワーカーに終了を伝えて待つ（認識器には触らない）。"""
        self._running = False
        q, w = self._q, self._worker
        if q is not None:
            q.put(None)                 # 待ち状態でも即座に抜けられるようにする
        if w is not None and w.is_alive():
            w.join(timeout)
        self._worker = None

    def start(self):
        from vosk import KaldiRecognizer
        self._end_worker(2.0)           # 前回のワーカーが残っていたら終わらせる
        with self._lock:
            q = queue.Queue()
            self._rec = KaldiRecognizer(self.model, SAMPLE_RATE)
            self._q = q
            self._fed = False
            self._running = True
            self._worker = threading.Thread(target=self._loop, args=(q,),
                                            daemon=True)
            self._worker.start()

    def feed(self, chunk):
        """録音コールバックから呼ぶ。重い処理はワーカーに渡し、録音を止めない。"""
        q = self._q
        if not self._running or q is None:
            return
        arr = np.asarray(chunk, dtype=np.float32).flatten()
        pcm = (np.clip(arr, -1.0, 1.0) * 32767).astype(np.int16).tobytes()
        q.put(pcm)

    def _loop(self, q):
        """自分の担当キューを引数で受け取る。start() で作り直されても
        古いワーカーが新しい認識器を触らないようにするため。"""
        while True:
            try:
                pcm = q.get(timeout=0.1)
            except queue.Empty:
                if not self._running:
                    return
                continue
            if pcm is None:             # 終了の合図
                return
            with self._lock:
                if self._rec is None or self._q is not q:
                    return              # 停止済み／別の録音に切り替わった
                try:
                    self._rec.AcceptWaveform(pcm)
                    self._fed = True
                except Exception:
                    pass

    def stop(self, timeout=5.0):
        """録音停止。残りを処理して認識結果を返す。"""
        self._end_worker(timeout)
        # ワーカーが時間内に終わらなかった場合でも、ロックを取ることで
        # AcceptWaveform の実行中に FinalResult を呼ぶことは無くなる。
        with self._lock:
            rec, self._rec = self._rec, None
            fed, self._fed = self._fed, False
            self._q = None
            if rec is None or not fed:
                return ""               # 一度も音声を渡していない状態で
                                        # FinalResult を呼ぶと落ちることがある
            try:
                text = json.loads(rec.FinalResult()).get("text", "")
            except Exception:
                text = ""
        # 日本語結果は単語が空白で区切られて返るため詰める
        return text.replace(" ", "").strip()

    def cancel(self):
        """結果を取らずに破棄する。"""
        self._end_worker(2.0)
        with self._lock:
            self._rec = None
            self._q = None
            self._fed = False
