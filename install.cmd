@echo off
rem Windows shim: runs the Python installer, `python -m claude_sync.install`,
rem with every argument passed through. Uses the py launcher (py -3) when it
rem is installed, else python. The package imports from this clone through
rem PYTHONPATH; the working directory is left alone, so the shim itself
rem does not hold the clone open while --repair deletes it (run --repair
rem from outside the clone: Windows cannot delete a shell's folder). Each run is the last thing the script does
rem (cmd.exe re-reads this file after every command).
rem
rem install.cmd [--repair|--reinstall] [--profile NAME] [--project-path PATH] [--tier basic|full] [--skip-python-deps]
setlocal
set "PYTHONPATH=%~dp0."
set "PYTHONSAFEPATH=1"
where py >nul 2>nul
if errorlevel 1 goto nopy
py -3 -m claude_sync.install %* & exit /b
:nopy
python -m claude_sync.install %* & exit /b
