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
  /win32icon:"%~dp0agent.ico" ^
  /resource:"%~dp0agent.ico",agent.ico ^
  /reference:System.dll ^
  /reference:System.Core.dll ^
  /reference:System.Drawing.dll ^
  /reference:System.Windows.Forms.dll ^
  /reference:System.Web.Extensions.dll ^
  "%~dp0Agent.cs"
if errorlevel 1 (
  echo [ERROR] build failed.
  exit /b 1
)

if not exist "%OUTDIR%\agent.ini" copy /y "%~dp0agent.ini.example" "%OUTDIR%\agent.ini" >nul
echo.
echo Built: %OUTDIR%\iscsi-broker-agent.exe
rem 打印编译进 exe 的 VERSION —— 后台上传客户端时【版本号必须填这个数】，
rem 填错会让客户机无限循环更新（换上去仍报旧版本，服务器以为它还有新版）。
"%OUTDIR%\iscsi-broker-agent.exe" version
echo ^^^^^^^^^^ 后台上传时“版本号”就填上面这个数（必须与 client\Agent.cs 里的 VERSION 一致）
echo.
echo Next : edit %OUTDIR%\agent.ini (server url + token), then
echo        iscsi-broker-agent.exe test
echo        iscsi-broker-agent.exe install
endlocal
