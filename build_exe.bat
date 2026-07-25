@echo off
rem Voice Router を単体の .exe にまとめる（配布用）
rem 実行後 dist\VoiceRouter.exe ができます。Python不要で配れます。
setlocal
cd /d "%~dp0"

echo [1/2] PyInstaller を用意します...
python -m pip install --upgrade pyinstaller || goto :err

echo [2/2] ビルドします（数分かかります）...
python -m PyInstaller ^
  --noconfirm --clean ^
  --name VoiceRouter ^
  --onefile ^
  --windowed ^
  --collect-all faster_whisper ^
  --collect-all ctranslate2 ^
  --collect-all onnxruntime ^
  --collect-all tokenizers ^
  --collect-all av ^
  --hidden-import uiautomation ^
  --hidden-import comtypes ^
  --hidden-import sounddevice ^
  voice_router.py || goto :err

echo.
echo 完了しました: dist\VoiceRouter.exe
echo ※ 音声認識モデルは初回起動時にダウンロードされます（約460MB）。
pause
goto :eof

:err
echo.
echo ビルドに失敗しました。上のエラーを確認してください。
pause
