@echo off
rem Double-click to start EQP. Extra arguments are passed through.
py "%~dp0eqp.py" %*
if errorlevel 1 pause
