@echo off
title AC Telemetry Bridge (claude)
cd /d "%~dp0"

rem ---- Python を探す ----
rem   1) 同梱ランタイム python-claude\python.exe（インターネット無しの環境向け）
rem   2) py ランチャー
rem   3) PATH 上の python
set "PYEXE="
if exist "%~dp0python-claude\python.exe" set "PYEXE=%~dp0python-claude\python.exe"
if defined PYEXE goto found
where py >nul 2>nul
if not errorlevel 1 set "PYEXE=py"
if defined PYEXE goto found
where python >nul 2>nul
if not errorlevel 1 set "PYEXE=python"
:found

if not defined PYEXE (
    echo.
    echo   Python が見つかりませんでした。
    echo.
    echo   [インターネットに繋がっている場合]
    echo     https://www.python.org/downloads/windows/ からインストールし、
    echo     インストーラの「Add python.exe to PATH」に必ずチェックを入れてください。
    echo.
    echo   [インターネットに繋がっていない場合]
    echo     別のPCで python-3.14.x-embed-amd64.zip をダウンロードし、
    echo     このフォルダの python-claude\ に展開してください。
    echo     詳しい手順は README-claude.md の「インターネットの無いPCへの導入」を
    echo     参照してください。
    echo.
    pause
    exit /b 1
)

echo.
echo   ============================================================
echo     Assetto Corsa テレメトリーブリッジ
echo   ============================================================
echo   Assetto Corsa を起動してセッションに入ると数値が流れ始めます。
echo   （ゲーム側より先にこれを起動しておいて問題ありません）
echo.
echo   数秒後、このPCの既定ブラウザでポータルページが開きます。
echo   スマホなどからは、下に表示される URL を開いてください。
echo.
echo   走行は 60Hz で logs-claude フォルダに自動記録されます。
echo   （エンジニア画面の REC で停止、ログ ボタンで再生できます）
echo.
echo   終了するには Ctrl+C を押すか、このウィンドウを閉じてください。
echo.

"%PYEXE%" bridge_claude.py --open

echo.
echo   ブリッジが終了しました。
pause
