#!/bin/bash
# Voice Router を macOS のアプリ（.app）として作り、アプリケーションフォルダに置く。
# 通常のアプリと同じように Launchpad / Spotlight / Dock から起動できるようになる。
# （Windows版の install_shortcut.ps1 に相当）
set -e
cd "$(dirname "$0")"
HERE="$(pwd)"

APP_NAME="Voice Router"
BUILD="$HERE/$APP_NAME.app"
DEST="$HOME/Applications/$APP_NAME.app"

# --- Python を探す（venv があればそれを使う） ---
if [ -x "$HERE/.venv/bin/python" ]; then
  PY="$HERE/.venv/bin/python"
elif command -v python3.11 >/dev/null 2>&1; then
  PY="$(command -v python3.11)"
elif command -v python3 >/dev/null 2>&1; then
  PY="$(command -v python3)"
else
  echo "Python が見つかりませんでした。先に ./VoiceRouter.command を一度実行してください。"
  read -r -p "Enter キーで終了"
  exit 1
fi

echo "使用する Python: $PY"

# --- .app の骨組みを作る ---
rm -rf "$BUILD"
mkdir -p "$BUILD/Contents/MacOS" "$BUILD/Contents/Resources"

cat > "$BUILD/Contents/MacOS/VoiceRouter" <<LAUNCHER
#!/bin/bash
cd "$HERE"
exec "$PY" voice_router.py
LAUNCHER
chmod +x "$BUILD/Contents/MacOS/VoiceRouter"

# マイク権限のダイアログに出す説明文。これが無いとmacOSが録音を拒否する。
cat > "$BUILD/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key>              <string>Voice Router</string>
  <key>CFBundleDisplayName</key>       <string>Voice Router</string>
  <key>CFBundleIdentifier</key>        <string>local.voicerouter</string>
  <key>CFBundleVersion</key>           <string>1.0</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundlePackageType</key>       <string>APPL</string>
  <key>CFBundleExecutable</key>        <string>VoiceRouter</string>
  <key>CFBundleIconFile</key>          <string>voice_router</string>
  <key>NSHighResolutionCapable</key>   <true/>
  <key>NSMicrophoneUsageDescription</key>
  <string>押しながら話した音声をこの端末の中だけで文字にするために使います。</string>
  <key>NSAppleEventsUsageDescription</key>
  <string>送信先のAIアプリやブラウザのタブを前面に出して文字を送るために使います。</string>
</dict>
</plist>
PLIST

# --- アイコン（voice_router.ico → voice_router.icns）を用意する ---
# 実行中の Dock アイコンにも使うため、.app の中だけでなく本体フォルダにも置く。
if [ ! -f "$HERE/voice_router.icns" ] && [ -f "$HERE/voice_router.ico" ]; then
  TMP="$(mktemp -d)"
  ICONSET="$TMP/voice_router.iconset"
  mkdir -p "$ICONSET"
  sips -s format png "$HERE/voice_router.ico" --out "$TMP/base.png" >/dev/null 2>&1
  for SZ in 16 32 64 128 256 512; do
    sips -z $SZ $SZ "$TMP/base.png" --out "$ICONSET/icon_${SZ}x${SZ}.png" >/dev/null 2>&1
    HALF=$((SZ / 2))
    [ $HALF -ge 16 ] && cp "$ICONSET/icon_${SZ}x${SZ}.png" \
      "$ICONSET/icon_${HALF}x${HALF}@2x.png"
  done
  iconutil -c icns "$ICONSET" -o "$HERE/voice_router.icns" >/dev/null 2>&1 \
    || echo "（アイコンの変換に失敗しました。既定のアイコンで作ります）"
  rm -rf "$TMP"
fi
[ -f "$HERE/voice_router.icns" ] && \
  cp "$HERE/voice_router.icns" "$BUILD/Contents/Resources/voice_router.icns"

# --- アプリケーションフォルダへ置く ---
mkdir -p "$HOME/Applications"
rm -rf "$DEST"
cp -R "$BUILD" "$DEST"
touch "$DEST"                      # Finder にアイコンを読み直させる

echo ""
echo "作成しました: $DEST"
echo "Launchpad / Spotlight で「Voice Router」と検索して起動できます。"
echo "Dock に置きたい場合は、起動中のアイコンを右クリック > オプション > Dock に追加。"
echo ""
echo "【重要】アプリとして起動すると、権限は「ターミナル」ではなく"
echo "「Voice Router」に対して求められます。システム設定 > プライバシーとセキュリティ の"
echo "  ・マイク（初回録音時にダイアログが出ます）"
echo "  ・アクセシビリティ（Cmd+V / Enter を送るため。必須）"
echo "  ・入力監視（テンキー1〜9を使う場合のみ）"
echo "で「Voice Router」を許可してください。"
echo ""
read -r -p "Enter キーで終了"
