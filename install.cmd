@echo off
setlocal
set SRC=%~dp0
set BIN=%USERPROFILE%\.local\bin
set SKILL=%USERPROFILE%\.claude\skills\organize-pc
if not exist "%BIN%" mkdir "%BIN%"
if not exist "%SKILL%" mkdir "%SKILL%"
copy /y "%SRC%pc.py" "%SKILL%\pc.py" >nul
copy /y "%SRC%SKILL.md" "%SKILL%\SKILL.md" >nul
if not exist "%SKILL%\policy.json" copy /y "%SRC%policy.json" "%SKILL%\policy.json" >nul
> "%BIN%\pc.cmd" echo @echo off
>> "%BIN%\pc.cmd" echo python "%%USERPROFILE%%\.claude\skills\organize-pc\pc.py" %%*
echo installed: %BIN%\pc.cmd and %SKILL%
echo make sure %BIN% is on PATH, then: pc scan
