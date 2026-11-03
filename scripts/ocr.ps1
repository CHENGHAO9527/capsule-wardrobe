# Screenshot OCR via the Windows built-in OCR engine (Windows.Media.Ocr).
#
# Why this exists: the coding agent cannot always read images directly, but the
# user frequently sends screenshots of console windows / error dialogs. Windows
# ships an OCR engine that needs no install and works offline.
#
# Usage:
#   powershell -File scripts\ocr.ps1 -Path "C:\path\to\shot.png"
#   powershell -File scripts\ocr.ps1 -Path "shot.png" -Out "shot.txt"
#
# Notes:
#  - This file is intentionally pure ASCII. Keep it that way (see start.bat for
#    the long story about cmd.exe / PowerShell and multi-byte characters).
#  - Language comes from the user profile. On a Chinese Windows that is zh-Hans,
#    which recognises both Chinese and Latin text.
#  - Runs on Windows PowerShell 5.1. Windows PowerShell 7 dropped the WinRT
#    projections, so use powershell.exe, not pwsh.exe.

param(
  [Parameter(Mandatory = $true)][string]$Path,
  [string]$Out
)

$ErrorActionPreference = 'Stop'

if (-not (Test-Path -LiteralPath $Path)) {
  Write-Error "file not found: $Path"
  exit 1
}
$Path = (Resolve-Path -LiteralPath $Path).Path

Add-Type -AssemblyName System.Runtime.WindowsRuntime | Out-Null

$null = [Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime]
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics, ContentType = WindowsRuntime]
$null = [Windows.Globalization.Language, Windows.Foundation, ContentType = WindowsRuntime]

$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and
    $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
  })[0]

function Await($WinRtTask, $ResultType) {
  $asTask = $asTaskGeneric.MakeGenericMethod($ResultType)
  $netTask = $asTask.Invoke($null, @($WinRtTask))
  $netTask.Wait(-1) | Out-Null
  if ($netTask.IsFaulted) { throw $netTask.Exception.InnerException }
  $netTask.Result
}

$file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($Path)) ([Windows.Storage.StorageFile])
$stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
$decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
$bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])

$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
if ($null -eq $engine) {
  Write-Error "no OCR engine available for the current user profile languages"
  exit 2
}

$result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])

$lines = @()
$lines += "engine language : " + $engine.RecognizerLanguage.LanguageTag
$lines += "image           : " + $Path
$lines += "lines           : " + $result.Lines.Count
$lines += "--------------------------------------------------"
foreach ($line in $result.Lines) { $lines += $line.Text }
$text = $lines -join "`r`n"

if ($Out) {
  $text | Out-File -FilePath $Out -Encoding utf8
  Write-Output "written: $Out"
} else {
  Write-Output $text
}
