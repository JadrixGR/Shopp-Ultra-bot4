$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Archive = Join-Path $Root "MIGRACION_RENDER\import_once.zip"

Write-Host "============================================================"
Write-Host "  SHOP ULTRA - SUBIR DATA AL DISCO DE RENDER"
Write-Host "============================================================"
Write-Host ""

if (-not (Test-Path $Archive)) {
    throw "No existe $Archive. Ejecute primero PREPARAR_MIGRACION_RENDER.bat."
}

$Target = Read-Host "Pegue solo el destino SSH de Render (ej. srv-xxxx@ssh.oregon.render.com)"
if ([string]::IsNullOrWhiteSpace($Target) -or $Target -notmatch "@ssh\..+\.render\.com$") {
    throw "El destino SSH no tiene el formato esperado."
}

Write-Host "Subiendo el paquete como archivo temporal..."
& scp -s $Archive "${Target}:/var/data/import_once.zip.upload"
if ($LASTEXITCODE -ne 0) {
    throw "SCP fallo con codigo $LASTEXITCODE."
}

Write-Host "Publicando el archivo de forma atomica..."
& ssh $Target "mv /var/data/import_once.zip.upload /var/data/import_once.zip && ls -lh /var/data/import_once.zip"
if ($LASTEXITCODE -ne 0) {
    throw "SSH fallo con codigo $LASTEXITCODE."
}

Write-Host ""
Write-Host "Carga completada. El entrypoint detectara el ZIP, validara la base,"
Write-Host "la importara y despues iniciara el bot automaticamente."
Write-Host "Revise Render > su servicio > Logs."
