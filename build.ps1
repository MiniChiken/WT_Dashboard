# Builds a single portable wt-dashboard.exe (bundles Python + deps + the
# frontend). Output: dist\wt-dashboard.exe - copy that one file anywhere and
# run it, no install needed. Run this from the wt-dashboard/ directory.

$ErrorActionPreference = "Stop"

pip install -r backend/requirements.txt pyinstaller

pyinstaller `
  --onefile `
  --name wt-dashboard `
  --add-data "frontend;frontend" `
  --hidden-import uvicorn.logging `
  --hidden-import uvicorn.loops `
  --hidden-import uvicorn.loops.auto `
  --hidden-import uvicorn.protocols `
  --hidden-import uvicorn.protocols.http `
  --hidden-import uvicorn.protocols.http.auto `
  --hidden-import uvicorn.protocols.websockets `
  --hidden-import uvicorn.protocols.websockets.auto `
  --hidden-import uvicorn.lifespan `
  --hidden-import uvicorn.lifespan.on `
  backend/main.py

Write-Output ""
Write-Output "Built: dist\wt-dashboard.exe"
