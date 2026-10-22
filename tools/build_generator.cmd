@echo off
setlocal
cd /d "%~dp0.."
if not exist bin mkdir bin
if not exist build mkdir build
set "LAB_VCVARS=C:\Program Files (x86)\Microsoft Visual Studio\18\BuildTools\VC\Auxiliary\Build\vcvars64.bat"
if not exist "%LAB_VCVARS%" (
  echo Visual Studio C++ Build Tools were not found at "%LAB_VCVARS%".
  exit /b 1
)
call "%LAB_VCVARS%" >nul
cl /nologo /O2 /W3 /D_FILE_OFFSET_BITS=64 vendor\gps-sdr-sim\gpssim.c vendor\gps-sdr-sim\getopt.c /Fe:bin\gps-sdr-sim.exe /Fo:build\
exit /b %errorlevel%
