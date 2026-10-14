@echo off
rem ============================================================
rem  Build the client agent with the in-box .NET Framework csc.exe.
rem  Output: client\dist\iscsi-broker-agent.exe (single exe, no runtime install needed on clients)
rem
rem  !! KEEP THIS FILE PURE ASCII !!
rem  cmd.exe reads .bat files in the OEM code page (936/GBK on zh-CN Windows).
rem  Non-ASCII bytes are decoded wrongly and can swallow the quotes/spaces/newlines
rem  that follow them, which shreds the command line below and produces nothing.
rem  Chinese deployment guide: client\README.md
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
set OUTEXE=%OUTDIR%\iscsi-broker-agent.exe

rem Delete any previous output FIRST. If the compile below fails we must not leave a
rem stale exe behind that looks like a fresh build - that once caused an old 1.1 build
rem to be published under the version number 1.2 (identical sha256, clients kept
rem reporting 1.1).
del /F /Q "%OUTEXE%" >nul 2>&1

"%CSC%" /nologo /target:exe /platform:anycpu /optimize+ /warn:4 ^
  /out:"%OUTEXE%" ^
  /win32icon:"%~dp0agent.ico" ^
  /resource:"%~dp0agent.ico",agent.ico ^
  /reference:System.dll ^
  /reference:System.Core.dll ^
  /reference:System.Drawing.dll ^
  /reference:System.Windows.Forms.dll ^
  /reference:System.Web.Extensions.dll ^
  "%~dp0Agent.cs"
set CSC_RC=%ERRORLEVEL%
if not "%CSC_RC%"=="0" (
  echo [ERROR] csc failed with exit code %CSC_RC%
  exit /b %CSC_RC%
)
if not exist "%OUTEXE%" (
  echo [ERROR] csc returned success but %OUTEXE% was not produced.
  exit /b 1
)

if not exist "%OUTDIR%\agent.ini" copy /y "%~dp0agent.ini.example" "%OUTDIR%\agent.ini" >nul
echo.
echo Built: %OUTEXE%
echo.
echo VERSION compiled into this exe:
"%OUTEXE%" version
echo.
echo SHA256 of this exe - after publishing, compare it with the web admin page
echo to be sure the file you uploaded is really this build:
certutil -hashfile "%OUTEXE%" SHA256
echo.
echo The number above is the client VERSION. When publishing from the web admin,
echo fill in EXACTLY this number as the version, otherwise clients will loop
echo forever re-downloading the same build (see client\README.md section 1).
echo.
echo Next : edit %OUTDIR%\agent.ini (server url + token), then
echo        iscsi-broker-agent.exe test
echo        iscsi-broker-agent.exe install
exit /b 0
