@echo off
rem ============================================================
rem  Build the client agent with the in-box .NET Framework csc.exe.
rem  Output: client\dist\iscsi-broker-agent.exe (single exe, no runtime install needed on clients)
rem  Note: this file is intentionally ASCII-only (cmd.exe codepage safe).
rem        See client\README.md for the Chinese deployment guide.
rem ============================================================
setlocal
set CSC=%SystemRoot%\Microsoft.NET\Framework64\v4.0.30319\csc.exe
if not exist "%CSC%" set CSC=%SystemRoot%\Microsoft.NET\Framework\v4.0.30319\csc.exe
if not exist "%CSC%" (
  echo [ERROR] csc.exe not found. Run this on Windows 10/11.
  exit /b 1
)

set OUTDIR=%~dp0dist
if not exist "%OUTDIR%" mkdir "%OUTDIR%"

"%CSC%" /nologo /target:exe /platform:anycpu /optimize+ /warn:4 ^
  /out:"%OUTDIR%\iscsi-broker-agent.exe" ^
  /reference:System.dll ^
  /reference:System.Core.dll ^
  /reference:System.Web.Extensions.dll ^
  "%~dp0Agent.cs"
if errorlevel 1 (
  echo [ERROR] build failed.
  exit /b 1
)

if not exist "%OUTDIR%\agent.ini" copy /y "%~dp0agent.ini.example" "%OUTDIR%\agent.ini" >nul
echo.
echo Built: %OUTDIR%\iscsi-broker-agent.exe
echo Next : edit %OUTDIR%\agent.ini (server url + token), then
echo        iscsi-broker-agent.exe test
echo        iscsi-broker-agent.exe install
endlocal
