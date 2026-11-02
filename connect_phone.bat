@echo off
setlocal enabledelayedexpansion
rem ============================================================
rem  connect_phone.bat - connect to the paired phone via
rem  Wireless debugging (auto-discovery over mDNS).
rem  The phone must be unlocked and "Wireless debugging" ON
rem  (Developer options). Pairing (one time) is done via
rem  adb pair <IP:PAIRPORT> with the code from the phone.
rem ============================================================
set SERIAL=24301FDF600063

rem --- already connected? ---
for /f "skip=1 tokens=1" %%D in ('adb devices 2^>nul') do (
    echo %%D | findstr /c:"%SERIAL%" >nul && (
        echo Phone already connected: %%D
        adb devices
        goto :done
    )
)

rem --- discover the current Wireless debugging endpoint (IP:port changes every reboot) ---
echo Searching for the phone on the network...
set ENDPOINT=
for /f "tokens=*" %%L in ('adb mdns services 2^>nul') do (
    echo %%L | findstr /c:"%SERIAL%" | findstr /c:"tls-connect" >nul && for %%A in (%%L) do set ENDPOINT=%%A
)
if "%ENDPOINT%"=="" (
    echo Phone not found. Check: screen unlocked, Developer options ^> Wireless debugging ON.
    pause
    exit /b 1
)

echo Connecting to %ENDPOINT% ...
adb connect %ENDPOINT%
adb devices
:done
endlocal
