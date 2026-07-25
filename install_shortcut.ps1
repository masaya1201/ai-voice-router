# Voice Router のショートカットをデスクトップとスタートメニューに作成する。
# 通常のアプリと同じように、アイコンをダブルクリック（または検索）で起動できるようになる。

$ErrorActionPreference = "Stop"
$here   = Split-Path -Parent $MyInvocation.MyCommand.Path
$script = Join-Path $here "voice_router.py"
$icon   = Join-Path $here "voice_router.ico"

# --- pythonw.exe を探す（コンソールを出さずに起動するため） ---
$py = $null
$cand = @()
$cmd = Get-Command pythonw -ErrorAction SilentlyContinue
if ($cmd) { $cand += $cmd.Source }
$cand += @(
  "$env:LOCALAPPDATA\Programs\Python\Python313\pythonw.exe",
  "$env:LOCALAPPDATA\Programs\Python\Python312\pythonw.exe",
  "$env:LOCALAPPDATA\Programs\Python\Python311\pythonw.exe",
  "$env:LOCALAPPDATA\Programs\Python\Python310\pythonw.exe",
  "$env:USERPROFILE\anaconda3\pythonw.exe",
  "$env:USERPROFILE\miniconda3\pythonw.exe",
  "$env:LOCALAPPDATA\anaconda3\pythonw.exe",
  "$env:PROGRAMDATA\anaconda3\pythonw.exe"
)
foreach ($c in $cand) { if ($c -and (Test-Path $c)) { $py = $c; break } }
if (-not $py) {
  Write-Host "Python が見つかりませんでした。python.org からインストールしてください。"
  Read-Host "Enter キーで終了"
  exit 1
}

$ws = New-Object -ComObject WScript.Shell
$targets = @(
  (Join-Path ([Environment]::GetFolderPath("Desktop")) "Voice Router.lnk"),
  (Join-Path ([Environment]::GetFolderPath("StartMenu")) "Programs\Voice Router.lnk")
)

foreach ($lnkPath in $targets) {
  $dir = Split-Path -Parent $lnkPath
  if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force $dir | Out-Null }
  $lnk = $ws.CreateShortcut($lnkPath)
  $lnk.TargetPath       = $py
  $lnk.Arguments        = '"' + $script + '"'
  $lnk.WorkingDirectory = $here
  $lnk.Description      = "押しながら話して離すだけで各AIへ音声送信"
  if (Test-Path $icon) { $lnk.IconLocation = $icon }
  $lnk.Save()
  Write-Host "作成しました: $lnkPath"
}

Write-Host ""
Write-Host "デスクトップのアイコン、または スタートメニューで「Voice Router」と検索して起動できます。"
