@echo off
echo ========================================
echo   Usuwanie tla - Buty na biale tlo
echo ========================================
echo.
echo Folder wejsciowy: input\
echo Folder wyjsciowy: done\[ID_produktu]\
echo.
python "%~dp0process.py"
echo.
pause
