@echo off
rem Lichii Bench: bench.bat setup, check, run plan.json, prepare, restore-config session, zip session.
rem Drag a plan onto this file to run it. Uses bench\python if present, else the installed Python 3.
set BENCH_PY=
if exist "%~dp0bench\python\python.exe" set BENCH_PY="%~dp0bench\python\python.exe"
if not defined BENCH_PY if exist "C:\LichiiBench\python\python.exe" set BENCH_PY="C:\LichiiBench\python\python.exe"
if not defined BENCH_PY where py >NUL 2>NUL
if not defined BENCH_PY if not errorlevel 1 set BENCH_PY=py -3
if not defined BENCH_PY set BENCH_PY=python
%BENCH_PY% -I "%~dp0bench\oot_bench.py" %*
set BENCH_EXIT=%ERRORLEVEL%
if /i "%~x1"==".json" pause
exit /b %BENCH_EXIT%
