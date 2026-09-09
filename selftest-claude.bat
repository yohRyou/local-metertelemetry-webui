@echo off
title AC Telemetry Bridge - 自己診断 (claude)
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

"%PYEXE%" selftest_claude.py
