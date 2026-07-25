#!/bin/bash
# Voice Router を単体の .app にまとめる（配布用 / Windows版 build_exe.bat の macOS 版）
# 実行後 dist/Voice Router.app ができます。Python不要で配れます。
# アイコン・マイク権限・Dockの表示名まで正しくなるのはこの方法だけです。
set -e
cd "$(dirname "$0")"
HERE="$(pwd)"
APP_NAME="Voice Router"

if [ -x .venv/bin/python ]; then
  PY=".venv/bin/python"
elif command -v python3.11 >/dev/null 2>&1; then
  PY="$(command -v python3.11)"
else
  PY="$(command -v python3)"
fi
echo "使用する Python: $PY"

echo "[1/4] PyInstaller と依存を用意します..."
"$PY" -m pip install --upgrade pip pyinstaller >/dev/null
"$PY" -m pip install -r requirements.txt >/dev/null

echo "[2/4] アイコンを用意します..."
if [ ! -f voice_router.icns ] && [ -f voice_router.ico ]; then
  TMP="$(mktemp -d)"; ICONSET="$TMP/voice_router.iconset"; mkdir -p "$ICONSET"
  sips -s format png voice_router.ico --out "$TMP/base.png" >/dev/null 2>&1
  for SZ in 16 32 64 128 256 512; do
    sips -z $SZ $SZ "$TMP/base.png" --out "$ICONSET/icon_${SZ}x${SZ}.png" >/dev/null 2>&1
    HALF=$((SZ / 2))
    [ $HALF -ge 16 ] && cp "$ICONSET/icon_${SZ}x${SZ}.png" "$ICONSET/icon_${HALF}x${HALF}@2x.png"
  done
  iconutil -c icns "$ICONSET" -o voice_router.icns >/dev/null 2>&1 || true
  rm -rf "$TMP"
fi

echo "[3/4] ビルドします（数分かかります）..."
"$PY" -m PyInstaller \
  --noconfirm --clean \
  --name "$APP_NAME" \
  --windowed \
  --icon voice_router.icns \
  --osx-bundle-identifier local.voicerouter \
  --add-data "voice_router.icns:." \
  --collect-all vosk \
  --collect-all faster_whisper \
  --collect-all ctranslate2 \
  --collect-all onnxruntime \
  --collect-all tokenizers \
  --collect-all av \
  --collect-all webview \
  --hidden-import sounddevice \
  --hidden-import pynput \
  --hidden-import sender_mac \
  --hidden-import hotkeys_mac \
  --hidden-import punctuate \
  --hidden-import vocab \
  --hidden-import vosk_stt \
  voice_router.py

PLIST="dist/$APP_NAME.app/Contents/Info.plist"
echo "[4/4] 権限の説明文を書き込みます..."
/usr/libexec/PlistBuddy -c "Add :NSMicrophoneUsageDescription string 押しながら話した音声を、この端末の中だけで文字にするために使います。" "$PLIST" 2>/dev/null || \
/usr/libexec/PlistBuddy -c "Set :NSMicrophoneUsageDescription 押しながら話した音声を、この端末の中だけで文字にするために使います。" "$PLIST"
/usr/libexec/PlistBuddy -c "Add :NSAppleEventsUsageDescription string 送信先のAIアプリやブラウザのタブを前面に出して文字を送るために使います。" "$PLIST" 2>/dev/null || \
/usr/libexec/PlistBuddy -c "Set :NSAppleEventsUsageDescription 送信先のAIアプリやブラウザのタブを前面に出して文字を送るために使います。" "$PLIST"

echo ""
echo "完了しました: $HERE/dist/$APP_NAME.app"
echo "アプリケーションフォルダにドラッグすれば、通常のアプリとして使えます。"
echo "※ 音声認識モデルは初回起動時にダウンロードされます（Vosk 小: 約48MB）。"
echo "※ 設定とログは ~/Library/Application Support/VoiceRouter に作られます。"
echo ""
read -r -p "Enter キーで終了"
