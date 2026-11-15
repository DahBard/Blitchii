@echo off
setlocal
rem Lichii Bench: bench.bat setup, check, run plan.json, prepare, restore-config session, zip session.
rem Drag a plan onto this file to run it. It sits in the build folder next to bench\ (or inside bench\).
rem Uses bench\python if present, else C:\LichiiBench\python, else the installed Python 3.
set BENCH_DIR=%~dp0bench\
if exist "%~dp0oot_bench.py" set BENCH_DIR=%~dp0
set BENCH_PY=
if exist "%BENCH_DIR%python\python.exe" set BENCH_PY="%BENCH_DIR%python\python.exe"
if not defined BENCH_PY if exist "C:\LichiiBench\python\python.exe" set BENCH_PY="C:\LichiiBench\python\python.exe"
if not defined BENCH_PY where py >NUL 2>NUL
if not defined BENCH_PY if not errorlevel 1 set BENCH_PY=py -3
if not defined BENCH_PY set BENCH_PY=python
%BENCH_PY% -I "%BENCH_DIR%oot_bench.py" %*
set BENCH_EXIT=%ERRORLEVEL%
if /i "%~x1"==".json" pause
exit /b %BENCH_EXIT%
