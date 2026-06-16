param(
    [switch]$Fresh,
    [switch]$ForceProjectFiles
)

$ErrorActionPreference = "Stop"

try {
    [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
} catch {
}

$Handoff = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Split-Path -Parent $Handoff
$Runtime = Join-Path $Root ".handoff-runtime"
$ProjectFiles = Join-Path $Handoff "project-files"

function Write-Utf8NoBom($Path, $Text) {
    $dir = Split-Path -Parent $Path
    if (-not (Test-Path -LiteralPath $dir)) {
        New-Item -ItemType Directory -Path $dir | Out-Null
    }
    $enc = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Text, $enc)
}

function Ensure-Dir($Path) {
    if (-not (Test-Path -LiteralPath $Path)) {
        New-Item -ItemType Directory -Path $Path | Out-Null
    }
}

Ensure-Dir $Handoff
Ensure-Dir (Join-Path $Handoff "tools")
Ensure-Dir $ProjectFiles
Ensure-Dir (Join-Path $Handoff "prompts")

if ($Fresh -and (Test-Path -LiteralPath $Runtime)) {
    if ((Split-Path -Leaf $Runtime) -ne ".handoff-runtime") {
        throw "Refusing to remove unexpected runtime path: $Runtime"
    }
    Remove-Item -LiteralPath $Runtime -Recurse -Force
    Write-Host "Removed previous runtime state: $Runtime"
}

Ensure-Dir $Runtime
Ensure-Dir (Join-Path $Runtime "notes")
Ensure-Dir (Join-Path $Runtime "claims")
Ensure-Dir (Join-Path $Runtime "locks")
Ensure-Dir (Join-Path $Runtime "cursors")
Ensure-Dir (Join-Path $Runtime "archive")

foreach ($name in @("claude-to-codex.jsonl", "codex-to-claude.jsonl")) {
    $path = Join-Path $Runtime $name
    if (-not (Test-Path -LiteralPath $path)) {
        Write-Utf8NoBom $path ""
    }
}

foreach ($name in @(".codex-cursor", ".codex-seq", ".claude-cursor", ".claude-seq")) {
    $path = Join-Path $Runtime $name
    if (-not (Test-Path -LiteralPath $path)) {
        Write-Utf8NoBom $path "0`n"
    }
}

foreach ($name in @("PROJECT.md", "AGENTS.md", "CLAUDE.md")) {
    $src = Join-Path $ProjectFiles $name
    $dst = Join-Path $Root $name
    if (Test-Path -LiteralPath $src) {
        if ($ForceProjectFiles -or -not (Test-Path -LiteralPath $dst)) {
            Copy-Item -LiteralPath $src -Destination $dst -Force
            Write-Host "Wrote $name"
        } else {
            Write-Host "Kept existing $name"
        }
    }
}

Write-Host ""
Write-Host "Handoff setup complete."
Write-Host "Protocol/template dir: $Handoff"
Write-Host "Runtime state dir:     $Runtime"
Write-Host "Next: fill PROJECT.md if needed, then start Claude and Codex sessions; they will read CLAUDE.md / AGENTS.md and .handoff/PROTOCOL.md."
