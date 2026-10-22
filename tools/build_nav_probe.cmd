@echo off
setlocal
cd /d "%~dp0.."
if not exist build mkdir build
call "C:\Program Files (x86)\Microsoft Visual Studio\18\BuildTools\VC\Auxiliary\Build\vcvars64.bat" >nul
cl /nologo /O2 /W3 tests\nav_probe.c vendor\gps-sdr-sim\getopt.c /Fe:build\nav_probe.exe /Fo:build\
exit /b %errorlevel%
