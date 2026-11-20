$ErrorActionPreference = "Stop"
$Repo = "Phatdz-coding/Scraper"
$Asset = "scrape-windows-x64.zip"
$Url = "https://github.com/$Repo/releases/latest/download/$Asset"
$InstallDir = "$env:LOCALAPPDATA\scrape"
$ZipPath = "$env:TEMP\$Asset"

Write-Host "Downloading $Asset..."
Invoke-WebRequest -Uri $Url -OutFile $ZipPath

New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
Expand-Archive -Path $ZipPath -DestinationPath $InstallDir -Force
Remove-Item $ZipPath -Force

# Add the installation directory to the user PATH if it is not already present
$UserPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($UserPath -notlike "*$InstallDir*") {
    [Environment]::SetEnvironmentVariable("Path", "$UserPath;$InstallDir", "User")
}

Write-Host "Installation successful! Reopen PowerShell and run: scrape --help"