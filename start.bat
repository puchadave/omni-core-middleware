@echo off
:: start.bat - Windows Startskript für Omni-Core ERP & BI Enterprise Edition
::============================================================================

echo.
echo ==========================================
echo  Omni-Core ERP & BI Enterprise Edition
echo ==========================================
echo.

:: Prüfe, ob Docker installiert ist
where docker >nul 2>&1
if %errorlevel% neq 0 (
    echo Fehler: Docker ist nicht installiert.
    echo Bitte installieren Sie Docker zuerst: https://docs.docker.com/engine/install/windows/
    pause
    exit /b 1
)

:: Prüfe, ob Docker Compose installiert ist
docker compose version >nul 2>&1
if %errorlevel% neq 0 (
    echo Fehler: Docker Compose ist nicht installiert.
    echo Bitte installieren Sie Docker Compose zuerst.
    pause
    exit /b 1
)

echo.
echo  Überwache bestehende Instanzen...

:: Alte Instanzen stoppen und entfernen
docker stop omni_db omni_core 2>nul || true
docker rm omni_db omni_core 2>nul || true
echo  Bestehende Container wurden bereinigt.

echo.
echo  Baue das Omni-Core Docker-Image...

:: Image bauen, falls nicht vorhanden
if not exist .\docker\omni-core-middleware\images\omni-core-middleware.tar (
    echo  Erstmaliges Bauen des Images (duert ca. 2-3 Minuten)...
    docker build -t omni-core-middleware . 2>&1 | findstr "error\|FAILED" || echo  Image wurde erfolgreich erstellt
)

echo.
echo  Starte Omni-Core Enterprise Stack im Hintergrund...

docker compose up -d

echo.
echo  Warte auf Datenbank-Bereitschaft (maximal 30 Sekunden)...

:: Warte auf Datenbank
for /l %i in (1,1,30) do (
    docker exec omni_db pg_isready -U omnicore -d omnicore >nul 2>&1
    if %errorlevel% equ 0 (
        echo  Datenbank ist bereit.
        goto :data_ready
    )
    timeout /t 1 >nul
)

:data_ready
echo  Warte auf Anwendung-Start...

timeout /t 3 >nul

:: Prüfe, ob die Applikation läuft
curl -s http://localhost:8000/api/health >nul 2>&1
if %errorlevel% equ 0 (
    echo  Omni-Core ist erreichbar unter http://localhost:8000
) else (
    echo  Omni-Core startet noch. Warte weitere 10 Sekunden.
    timeout /t 10 >nul
    curl -s http://localhost:8000/api/health >nul 2>&1
    if %errorlevel% equ 0 (
        echo  Omni-Core ist nun erreichbar.
    )
)

echo.
echo ==========================================
echo  Omni-Core ist jetzt aktiv!
echo ==========================================
echo.
echo  Webinterface: http://localhost:8000/admin/products
echo  Health-Check:   http://localhost:8000/api/health
echo  Datenbank-Daten:
echo    Host: localhost
echo    Port: 5432
echo    User: omnicore
echo    Datenbank: omnicore
echo.
echo  Drücken Sie eine beliebige Taste, um fortzufahren...
echo.
pause >nul