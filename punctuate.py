# -*- coding: utf-8 -*-
"""
punctuate.py — 認識結果に句点を補う。

Vosk は句読点を出さないため、文末表現のあとに「。」を入れて読みやすくする。
誤って文の途中で切らないよう、接続が続く場合（〜ますが、〜ですから 等）は入れない。
"""

import re

# 文末になりうる表現（長いものから順に見る）
ENDINGS = (
    "ませんでした", "ましょうか", "ませんか", "でしょうか", "ましたか",
    "でしたか", "ください", "ましょう", "でしょう",
    "ませんね", "ですね", "ますね", "ですか", "ますか", "でした",
    "ました", "ません", "です", "ます", "だろう", "しよう",
)

# これらが後ろに続くときは文が途中なので切らない
CONTINUATIONS = (
    "が", "けど", "けれど", "けれども", "から", "ので", "のに", "し",
    "て", "で", "と", "たら", "なら", "より", "ば", "つつ", "ものの",
    "ため", "うえ", "うち", "あと", "まま", "ながら",
    # 終助詞など。ここで切ると「ですよ」「ますね」が分断される
    "よ", "ね", "な", "さ", "わ", "も", "か",
    # 「ですから」は「ですか」+「ら」と誤認しやすいので除外する
    "ら",
)

_END_RE = re.compile("(" + "|".join(sorted(ENDINGS, key=len, reverse=True)) + ")")


def punctuate(text):
    """文末表現のあとに「。」を補って返す。"""
    if not text:
        return text
    text = text.strip()
    if not text:
        return text
    # 既に句読点があるならそのまま尊重する
    if "。" in text or "、" in text:
        return text

    out = []
    pos = 0
    for m in _END_RE.finditer(text):
        end = m.end()
        rest = text[end:]
        if not rest:
            break                       # 末尾はまとめて後で付ける
        if rest.startswith(CONTINUATIONS):
            continue                    # 文が続いている
        if rest.startswith(ENDINGS):
            continue                    # 「ますます」のような重なりで切らない
        out.append(text[pos:end])
        pos = end
    out.append(text[pos:])
    result = "。".join(s for s in out if s)

    # 末尾の句点（疑問で終わるなら「？」）
    if not result.endswith(("。", "？", "?", "！", "!")):
        result += "？" if re.search(r"(ですか|ますか|でしょうか|ましたか|でしたか)$", result) else "。"
    return result
