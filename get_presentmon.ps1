# NavyHUD: FPS測定に使う PresentMon (Intel/GameTechDev, MIT License) の「最新版」をダウンロードします
$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$out = Join-Path $PSScriptRoot "NavyHUD_PresentMon.exe"
if (Test-Path $out) { Write-Host "PresentMon already present."; exit 0 }
$headers = @{ "User-Agent" = "NavyHUD-build"; "Accept" = "application/vnd.github+json" }
try {
    Write-Host "Querying latest PresentMon release ..."
    $rel = Invoke-RestMethod -UseBasicParsing -Uri "https://api.github.com/repos/GameTechDev/PresentMon/releases/latest" -Headers $headers
    $asset = $rel.assets | Where-Object { $_.name -match '^PresentMon-[\d.]+-x64\.exe$' } | Select-Object -First 1
    if (-not $asset) { throw "x64 exe asset not found in release" }
    if (-not $asset.browser_download_url.StartsWith("https://github.com/GameTechDev/PresentMon/")) { throw "unexpected download url" }
    Write-Host "Downloading $($asset.name) ..."
    Invoke-WebRequest -UseBasicParsing -Uri $asset.browser_download_url -OutFile $out -Headers $headers
} catch {
    Write-Host "DOWNLOAD FAILED: $($_.Exception.Message)"
    if (Test-Path $out) { Remove-Item $out -Force }
    exit 1
}
if ((Get-Item $out).Length -lt 100000) { Remove-Item $out -Force; Write-Host "Downloaded file is invalid."; exit 1 }
Write-Host "PresentMon ready: $out"
