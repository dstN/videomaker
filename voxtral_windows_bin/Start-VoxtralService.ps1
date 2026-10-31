# Start-VoxtralService.ps1
# Startet den Voxtral FastAPI-Wrapper auf Windows (Port 8700).

$env:GGUF_PATH        = "\\wsl.localhost\Ubuntu_V2\home\dstn\github\videomaker\data\models\voxtral\voxtral-tts-q4.gguf"
$env:TOKENIZER_PATH   = "\\wsl.localhost\Ubuntu_V2\home\dstn\github\videomaker\data\models\voxtral\tekken.json"
$env:VOXTRAL_BIN      = "\\wsl.localhost\Ubuntu_V2\home\dstn\github\videomaker\voxtral_windows_bin\voxtral.exe"
$env:DEFAULT_EULER_STEPS = "3"
$env:MESA_D3D12_DEFAULT_ADAPTER_NAME = "NVIDIA"

Write-Host "-------------------------------------------------" -ForegroundColor Cyan
Write-Host "  Voxtral TTS Service - Port 8700" -ForegroundColor Cyan
Write-Host "  Docker erreicht diesen Service via:" -ForegroundColor Cyan
Write-Host "  http://host.docker.internal:8700" -ForegroundColor Green
Write-Host "-------------------------------------------------" -ForegroundColor Cyan
Write-Host ""

$env:PYTHONPATH = "\\wsl.localhost\Ubuntu_V2\home\dstn\github\videomaker"
Set-Location "\\wsl.localhost\Ubuntu_V2\home\dstn\github\videomaker"
python -m uvicorn voxtral_service.main:app --host 0.0.0.0 --port 8700
