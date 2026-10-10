@echo off
rem Removes the virtual camera "HD Camera iPhone" (needs admin). The app installs it again on its next start.
net session >nul 2>&1
if %errorlevel% neq 0 (
  powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)
set "DST=%ProgramData%\HDCameraiPhone"
%SystemRoot%\System32\regsvr32.exe /s /u /n /i:UnityCaptureName="HD Camera iPhone" "%DST%\HDCameraiPhone64.dll"
if exist %SystemRoot%\SysWOW64\regsvr32.exe %SystemRoot%\SysWOW64\regsvr32.exe /s /u /n /i:UnityCaptureName="HD Camera iPhone" "%DST%\HDCameraiPhone32.dll"
%SystemRoot%\System32\regsvr32.exe /s /u /n /i:UnityCaptureName="HD Camera USC-CAM" "%DST%\HDCameraiPhone64.dll" >nul 2>&1
echo Removed.
pause
