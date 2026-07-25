# -*- coding: utf-8 -*-
"""
vocab.py — 専門用語・固有名詞の認識ゆれを補正する。

音声認識は一般語彙で学習されているため、専門用語や固有名詞は
別の語に置き換わりやすい。
ここでは認識後のテキストに対して、利用者が用意した対応表で置き換える。

対応表 `vocabulary.json` は **公開対象に含めない**（個人情報や未公開の内容を含むため）。
形式:
    {
      "replacements": {
        "ジーサス": "GitHub",
        "パイソン": "Python"
      }
    }
長い語から順に置き換えるので、短い語が先に消費されることはない。
"""

import json
import os

VOCAB_FILENAME = "vocabulary.json"


class Vocabulary:
    def __init__(self, path):
        self.path = path
        self.pairs = []          # [(誤認識, 正しい語)] を長い順に並べたもの
        self.mtime = None
        self.load()

    def load(self):
        try:
            if not os.path.exists(self.path):
                self.pairs = []
                self.mtime = None
                return
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            rep = data.get("replacements", {}) if isinstance(data, dict) else {}
            self.pairs = sorted(
                ((k, v) for k, v in rep.items() if k and v),
                key=lambda kv: len(kv[0]), reverse=True)
            self.mtime = os.path.getmtime(self.path)
        except Exception:
            import traceback
            traceback.print_exc()
            self.pairs = []

    def _reload_if_changed(self):
        """使うたびに更新を確認する。アプリを再起動せずに用語を追加できる。"""
        try:
            if not os.path.exists(self.path):
                return
            m = os.path.getmtime(self.path)
            if m != self.mtime:
                self.load()
        except Exception:
            pass

    def apply(self, text):
        if not text:
            return text
        self._reload_if_changed()
        for wrong, right in self.pairs:
            if wrong in text:
                text = text.replace(wrong, right)
        return text


_cache = {}


def get(app_dir):
    """アプリのフォルダにある vocabulary.json を読み込む（無ければ何もしない）。"""
    path = os.path.join(app_dir, VOCAB_FILENAME)
    if path not in _cache:
        _cache[path] = Vocabulary(path)
    return _cache[path]
