@echo off
set "QKD_PYTHON=%USERPROFILE%\miniconda3\envs\niDaqENV\pythonw.exe"
if exist "%QKD_PYTHON%" (
    start "" "%QKD_PYTHON%" "%~dp0launch.py"
) else (
    echo Could not find the niDaqENV Python environment.
    echo Open launch.py in VS Code, select the experiment Python interpreter, and click Run Python File.
    pause
)
