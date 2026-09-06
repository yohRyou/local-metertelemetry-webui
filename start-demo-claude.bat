@echo off
title AC Telemetry Bridge - DEMO (claude)
cd /d "%~dp0"

rem ---- Python を探す（py ランチャー優先） ----
set "PYEXE="
where py >nul 2>nul
if not errorlevel 1 set "PYEXE=py"
if defined PYEXE goto found
where python >nul 2>nul
if not errorlevel 1 set "PYEXE=python"
:found

if not defined PYEXE (
    echo.
    echo   Python が見つかりませんでした。
    echo   https://www.python.org/downloads/windows/ からインストールし、
    echo   インストーラの「Add python.exe to PATH」に必ずチェックを入れてください。
    echo.
    pause
    exit /b 1
)

echo.
echo   ============================================================
echo     デモモードで起動します
echo   ============================================================
echo   Assetto Corsa を起動していなくても疑似データが流れます。
echo   画面レイアウトやメーター設定の確認用です。
echo.
echo   数秒後、このPCの既定ブラウザでポータルページが開きます。
echo   スマホなどからは、下に表示される URL を開いてください。
echo.
echo   終了するには Ctrl+C を押すか、このウィンドウを閉じてください。
echo.

"%PYEXE%" bridge_claude.py --demo --open

echo.
echo   ブリッジが終了しました。
pause
