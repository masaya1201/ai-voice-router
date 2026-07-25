# Voice Router — macOS版

Windows専用だった Voice Router の macOS 移植です。
使い方は同じ：**送りたいAIのボタンを押しながら話して、離すだけ。**

## Windows版との違い

| | Windows版 | macOS版 |
|---|---|---|
| 送信のしくみ | UI Automation で入力欄を特定し、貼り付きを検証してから Enter | AppleScript でアプリ/タブを前面化し、前面化を検証してから Cmd+V → Enter |
| 貼り付き検証 | 入力欄の中身を読み返して確認 | 前面アプリの確認のみ（macOSでは入力欄の中身を汎用的に読めないため） |
| 対応ブラウザ | Edge / Chrome / Brave / Vivaldi | Chrome / Edge / Brave / Vivaldi / **Safari** |
| ホットキー | テンキー1〜4（低レベルフック） | テンキー1〜4（pynput / Quartzイベントタップ） |
| デスクトップアプリの指定 | `"kind": "proc", "proc": "claude.exe"` | `"kind": "app", "app": "Claude"`（アプリ名） |

ChatGPT / Claude / Gemini のWebページはページ全体で貼り付けを受けて
入力欄に入れるため、タブが前面になっていれば貼り付けは届きます。

## セットアップ

```bash
git clone https://github.com/tsuchitaka-star/ai-voice-router
cd ai-voice-router
./VoiceRouter.command   # 初回は venv 構築 + 依存インストール + モデルDL（数分）
```

2回目以降は Finder で `VoiceRouter.command` をダブルクリックするだけです。

### 必要な権限（初回のみ）

システム設定 > プライバシーとセキュリティ で、**起動元のアプリ**
（ターミナル / iTerm など）に以下を許可してください。

1. **マイク** — 録音のため（初回録音時にダイアログが出ます）
2. **アクセシビリティ** — Cmd+V / Enter のキー入力を送るため（必須）
3. **オートメーション** — Chrome や System Events の操作。初回にダイアログが出るので「許可」
4. **入力監視** — テンキー1〜4のグローバルホットキーを使う場合のみ

権限が無い場合でも画面のボタンは使えます（ホットキーだけ無効になります）。

## 設定 (`voice_router_config.json`)

初回起動時に自動生成されます。macOSでの既定の送信先：

```json
{
  "targets": [
    { "key": "claude", "label": "Claude", "color": "#d97757",
      "kind": "app", "app": "Claude" },

    { "key": "gemini", "label": "Gemini", "color": "#8e6fd8",
      "kind": "browser_tab", "browser": "chrome",
      "tab": "gemini", "url": "gemini.google.com" }
  ]
}
```

- `kind: "app"` … デスクトップアプリ。`app` に /Applications のアプリ名
- `kind: "browser_tab"` … ブラウザのタブ。`url` はURLの一部（推奨）、`tab` はタブ名の一部
- `browser` に指定できる値: `chrome` / `edge` / `brave` / `vivaldi` / `safari` / `any`

その他の設定（`model_size` / `language` / `hotkeys_enabled` / `hotkey_swallow`）は
Windows版と同じです。README.md を参照してください。

## うまく動かないとき

| 症状 | 対処 |
|------|------|
| 「貼り付けできませんでした」 | アクセシビリティ権限を起動元アプリに付与 → アプリ再起動 |
| タブが見つからない | 対象ブラウザでそのサイトのタブを開いておく。`url` 指定を推奨 |
| テンキーが効かない | 入力監視の権限を確認。MacBook本体にテンキーは無いので画面ボタンを使う |
| 貼り付け先がおかしい | ブラウザのアドレスバーにフォーカスがある状態だと誤爆します。一度ページ内をクリックしてから使ってください |

## macOS版の実装メモ

- **マイクは初回録音後、開きっぱなしになります**（メニューバーにマイク使用中の
  表示が残ります）。macOSのCoreAudioは録音ストリームの開閉を繰り返すと
  ハング/クラッシュするため、ストリームを1本起動しっぱなしにして
  録音フラグだけで制御しています。**録音していない間の音声はその場で捨てられ、
  保存も送信もされません。**
- pywebview の JS→Python ブリッジは macOS で稀に沈黙するため、UIとPythonの
  連携はすべて Python 側からの evaluate_js（プッシュ＆ポーリング）で行っています。
- グローバルホットキー（pynput）は pywebview と同一プロセスで共存できないため、
  別プロセスのワーカーで監視しています（親終了時に自動終了）。

## 制限事項（macOS版）

- 入力欄の中身の読み返し検証はありません（前面化の検証のみ）
- ブラウザ側でアドレスバー等にフォーカスがあると、まれに貼り付け先を誤ることがあります
- Firefox は AppleScript でタブ操作ができないため未対応です
