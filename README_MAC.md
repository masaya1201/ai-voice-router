# Voice Router — macOS版

Windows専用だった Voice Router の macOS 移植です。
使い方は同じ：**送りたいAIのボタンを押しながら話して、離すだけ。**

## Windows版との違い

| | Windows版 | macOS版 |
|---|---|---|
| 送信のしくみ | UI Automation で入力欄を特定し、貼り付きを検証してから Enter | AppleScript でアプリ/タブを前面化し、前面化を検証してから Cmd+V → Enter |
| 貼り付き検証 | 入力欄の中身を読み返して確認 | 前面アプリの確認のみ（macOSでは入力欄の中身を汎用的に読めないため） |
| 入力欄フォーカス | UI Automation | ブラウザはページ内JavaScript（許可時のみ・任意） |
| 対応ブラウザ | Edge / Chrome / Brave / Vivaldi | Chrome / Edge / Brave / Vivaldi / **Safari** |
| ホットキー | テンキー1〜9（低レベルフック） | テンキー1〜9（pynput / Quartzイベントタップ・別プロセス） |
| デスクトップアプリの指定 | `"kind": "proc", "proc": "claude.exe"` | `"kind": "app", "app": "Claude"`（アプリ名） |
| ボタン押下 (`kind:"click"`) | UI Automation でボタンを押す | ブラウザはページ内JS、アプリは System Events |

ChatGPT / Claude / Gemini のWebページはページ全体で貼り付けを受けて
入力欄に入れるため、タブが前面になっていれば貼り付けは届きます。

音声認識は本家と同じく **Vosk**（話している最中に逐次認識するので、
離した瞬間に送信される）が既定です。`engine` を `"whisper"` にすると
faster-whisper に切り替わります。句読点補正（punctuate.py）と
用語補正（vocab.py）はそのまま動きます。

## セットアップ

```bash
git clone https://github.com/masaya1201/ai-voice-router
cd ai-voice-router
./VoiceRouter.command   # 初回は venv 構築 + 依存インストール + モデルDL（数分）
```

2回目以降は Finder で `VoiceRouter.command` をダブルクリックするだけです。

### アプリとして使いたいとき

Windows版の `ショートカットを作成.bat` に当たるものが2つあります。

| | 作られるもの | 時間 | アイコン・権限 |
|---|---|---|---|
| `ショートカットを作成.command` | `~/Applications/Voice Router.app`（この場所のコードを呼ぶだけの薄い殻） | 数秒 | Finderのアイコンは専用。**起動中のDockアイコンはPythonのもの**、権限も「Python」に付く |
| `build_app.command` | `dist/Voice Router.app`（Python同梱の単体アプリ） | 数分 | すべて専用。**配布もできる** |

手軽に済ませたいなら前者、ちゃんとしたアプリにしたいなら後者です。

> Python は自分自身を `Python.app` として登録するため、薄い殻の .app から
> 起動しても実行中はPythonのアプリとして扱われます。これはPyInstallerで
> 固めた `build_app.command` の方でしか解消できません。

### 必要な権限（初回のみ）

システム設定 > プライバシーとセキュリティ で、**起動元のアプリ**
（ターミナル / iTerm など）に以下を許可してください。

1. **マイク** — 録音のため（初回録音時にダイアログが出ます）
2. **アクセシビリティ** — Cmd+V / Enter のキー入力を送るため（必須）
3. **オートメーション** — Chrome や System Events の操作。初回にダイアログが出るので「許可」
4. **入力監視** — テンキー1〜9のグローバルホットキーを使う場合のみ

権限が無い場合でも画面のボタンは使えます（ホットキーだけ無効になります）。

さらに `kind: "click"`（🎙 音声会話 開始／終了）を使う場合は、ブラウザ側で
**Apple Events からの JavaScript を許可**してください。

- Chrome/Edge/Brave/Vivaldi: 表示 > 開発 / 管理 > 「Apple Events からの JavaScript を許可」
- Safari: 開発 > 「Apple Events からの JavaScript を許可」

（この設定は音声送信そのものには不要です。有効にすると、貼り付け前に
入力欄へフォーカスを移せるようになり、アドレスバーへの誤爆も減ります。）

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
  `.app` に固めた場合はスクリプトのパスを渡せないため、`--hotkey-worker` 引数を
  `voice_router.py` の先頭で拾ってワーカーに切り替えています
  （これをしないとアプリが二重起動します）。
- `.app` から起動したときの設定とログは
  `~/Library/Application Support/VoiceRouter` に作られます
  （`.app` の中は書き込み先として適切でないため）。

## Windows版のファイルと macOS版の対応

| Windows | macOS | 状態 |
|---|---|---|
| `VoiceRouter.bat` | `VoiceRouter.command` | ✅ |
| `ショートカットを作成.bat` / `install_shortcut.ps1` | `ショートカットを作成.command` | ✅ |
| `build_exe.bat` | `build_app.command` | ✅ |
| `sender.py`（UI Automation） | `sender_mac.py`（AppleScript） | ✅ |
| `hotkeys.py`（低レベルフック） | `hotkeys_mac.py`（Quartz・別プロセス） | ✅ |
| `voice_router.ico` | `voice_router.icns`（.icoから自動生成） | ✅ |
| `vosk_stt.py` / `punctuate.py` / `vocab.py` | 同じものがそのまま動く | ✅ |

## 制限事項（macOS版）

- 入力欄の中身の読み返し検証はありません（前面化の検証のみ）
- ブラウザ側でアドレスバー等にフォーカスがあると、まれに貼り付け先を誤ることがあります
  （ブラウザのJavaScript実行を許可すると、貼る前に入力欄へフォーカスを移すので減ります）
- Firefox は AppleScript でタブ操作ができないため未対応です
- `stt: "app"`（送信先アプリ自身の書き起こしを使うモード）は、ブラウザのタブが対象で
  JavaScript実行を許可している場合のみ動きます。デスクトップアプリでは入力欄の中身を
  読めないため使えません
- MacBook 本体にはテンキーがないため、外付けキーボードが無ければ画面のボタンを使います
