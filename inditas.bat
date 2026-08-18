@echo off
rem ===================================================================
rem  Weboldal-letolto inditasa Windows alatt (v2)
rem  A fajl szandekosan ekezet nelkuli (tiszta ASCII), hogy a cmd.exe
rem  kodlapjatol fuggetlenul mindig helyesen jelenjen meg.
rem  Sorvegek: CRLF
rem ===================================================================
setlocal EnableExtensions
title Weboldal-letolto
cd /d "%~dp0"

set "SCRIPT=letolto.py"
set "VENVDIR=.venv"
set "PIPOPT="
set "NEEDED=httpx"

echo ===============================================
echo   Weboldal-letolto - inditas
echo ===============================================
echo.

rem --- 1. A Python szkript megvan-e? --------------------------------
if not exist "%SCRIPT%" (
    echo [HIBA] Nem talalhato: %SCRIPT%
    echo Tedd ezt a .bat fajlt a letolto.py melle, ugyanabba a mappaba.
    goto :fail
)

rem --- 2. Python kereses (3.11 vagy ujabb kell) ----------------------
set "PYLAUNCH="
py -3 --version >nul 2>&1 && set "PYLAUNCH=py -3"
if not defined PYLAUNCH (
    python --version >nul 2>&1 && set "PYLAUNCH=python"
)
if not defined PYLAUNCH (
    echo [HIBA] Nem talaltam Python telepitest.
    echo Toltsd le a https://www.python.org/downloads/ oldalrol,
    echo es a telepitesnel pipald be az "Add python.exe to PATH" opciot.
    echo.
    echo Megnyitom a letoltesi oldalt...
    start "" https://www.python.org/downloads/
    goto :fail
)

%PYLAUNCH% -c "import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)" >nul 2>&1
if errorlevel 1 (
    echo [HIBA] Legalabb Python 3.11 szukseges. A telepitett verzio:
    %PYLAUNCH% --version
    echo Frissits a https://www.python.org/downloads/ oldalrol.
    goto :fail
)

for /f "delims=" %%v in ('%PYLAUNCH% -c "import sys;print(sys.version.split()[0])"') do set "PYVER=%%v"
echo [OK] Python %PYVER% megtalalva.

rem --- 3. Virtualis kornyezet ---------------------------------------
if not exist "%VENVDIR%\Scripts\python.exe" (
    echo [..] Virtualis kornyezet letrehozasa a .venv mappaban ^(egyszeri^)...
    %PYLAUNCH% -m venv "%VENVDIR%"
)

set PYCMD="%CD%\%VENVDIR%\Scripts\python.exe"
if not exist %PYCMD% (
    echo [FIGYELEM] A virtualis kornyezet nem jott letre.
    echo            A rendszer-Pythont hasznalom, --user telepitessel.
    set PYCMD=%PYLAUNCH%
    set "PIPOPT=--user"
) else (
    echo [OK] Virtualis kornyezet keszen all.
)

rem --- 4. Fuggosegek ------------------------------------------------
rem  Csak a httpx kell. A tkinter a Python resze (Windowson alapbol
rem  telepitve), a HTML-elemzest a szabvanykonyvtar vegzi.
%PYCMD% -c "import httpx, tkinter" >nul 2>&1
if errorlevel 1 (
    echo [..] Hianyzo fuggoseg telepitese: %NEEDED%
    echo      ^(ez az elso inditasnal par percig tarthat^)
    echo.
    %PYCMD% -m pip install --upgrade pip %PIPOPT% >nul 2>&1
    %PYCMD% -m pip install %PIPOPT% %NEEDED%
    if errorlevel 1 (
        echo.
        echo [HIBA] A telepites nem sikerult.
        echo Lehetseges okok: nincs internetkapcsolat, tuzfal vagy proxy blokkol.
        goto :fail
    )
    %PYCMD% -c "import httpx" >nul 2>&1
    if errorlevel 1 (
        echo [HIBA] A httpx telepites utan sem importalhato.
        goto :fail
    )
    %PYCMD% -c "import tkinter" >nul 2>&1
    if errorlevel 1 (
        echo [HIBA] Hianyzik a tkinter modul.
        echo Telepitsd ujra a Pythont, es hagyd bekapcsolva a "tcl/tk and IDLE" opciot.
        goto :fail
    )
    echo [OK] Fuggosegek telepitve.
) else (
    echo [OK] Fuggosegek rendben.
)

rem --- 5. Windows hosszu utvonalak (MAX_PATH) ----------------------
rem  A program magatol a 240 karakteres korlat alatt tartja az utakat,
rem  de erdemes jelezni, ha a rendszeren nincs bekapcsolva a hosszu ut.
for /f "tokens=3" %%a in ('reg query "HKLM\SYSTEM\CurrentControlSet\Control\FileSystem" /v LongPathsEnabled 2^>nul') do set "LP=%%a"
if not "%LP%"=="0x1" (
    echo [INFO] A hosszu utvonalak tamogatasa nincs bekapcsolva a rendszeren.
    echo        A program ezert 240 karakter ala rovidit minden utvonalat.
    echo        Ha teljes fanevekre van szukseged, rendszergazdakent:
    echo        reg add "HKLM\SYSTEM\CurrentControlSet\Control\FileSystem" /v LongPathsEnabled /t REG_DWORD /d 1 /f
)

rem --- 6. Inditas ---------------------------------------------------
echo.
echo [..] Program inditasa...
echo.
%PYCMD% "%SCRIPT%"
set "RC=%ERRORLEVEL%"

if not "%RC%"=="0" (
    echo.
    echo A program hibaval lepett ki ^(kod: %RC%^).
    echo A fenti hibauzenet segit a hiba okanak azonositasaban.
    echo.
    pause
    exit /b %RC%
)

endlocal
exit /b 0

:fail
echo.
pause
endlocal
exit /b 1
