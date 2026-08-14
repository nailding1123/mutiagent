# MultiAgent one-click installer for Windows PowerShell.
# Copies the application into an isolated user installation and creates a
# user-level `multiagent.cmd`. No network access is needed.

[CmdletBinding()]
param(
    [switch]$NoPath,
    [switch]$InstallAgents
)

$ErrorActionPreference = 'Stop'
$ProjectDir = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$InstallRoot = if ($env:MULTIAGENT_INSTALL_ROOT) { $env:MULTIAGENT_INSTALL_ROOT } else { Join-Path $env:LOCALAPPDATA 'MultiAgent' }
$VenvDir = if ($env:MULTIAGENT_VENV_DIR) { $env:MULTIAGENT_VENV_DIR } else { Join-Path $InstallRoot 'venv' }
$BinDir = if ($env:MULTIAGENT_BIN_DIR) { $env:MULTIAGENT_BIN_DIR } else { Join-Path $InstallRoot 'bin' }

$PythonExe = $null
$PythonArgs = @()
$py = Get-Command py -ErrorAction SilentlyContinue
if ($py) {
    $PythonExe = $py.Source
    $PythonArgs = @('-3')
} else {
    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($python) {
        $PythonExe = $python.Source
    }
}
if (-not $PythonExe) {
    throw '找不到 Python 3。请先安装 Python 3.9 或更高版本，并勾选加入 PATH。'
}

$version = (& $PythonExe @PythonArgs -c 'import sys; print("%d.%d" % sys.version_info[:2])').Trim()
if ([version]$version -lt [version]'3.9') {
    throw "Python 版本过低：$version；需要 Python 3.9 或更高版本。"
}

Write-Host "使用 Python：$PythonExe $($PythonArgs -join ' ')"
Write-Host "项目目录：$ProjectDir"
Write-Host "创建或复用虚拟环境：$VenvDir"

if (-not (Test-Path -LiteralPath (Join-Path $VenvDir 'Scripts\python.exe'))) {
    & $PythonExe @PythonArgs -m venv $VenvDir
    if ($LASTEXITCODE -ne 0) {
        throw "Python 命令失败，退出码：$LASTEXITCODE"
    }
}

$VenvPython = Join-Path $VenvDir 'Scripts\python.exe'
if (-not (Test-Path -LiteralPath $VenvPython)) {
    throw "虚拟环境创建失败：$VenvPython"
}

$SitePackages = (& $VenvPython -c 'import sysconfig; print(sysconfig.get_path("purelib"))').Trim()
$PackageSource = Join-Path $ProjectDir 'multiagent_cli'
$PackageTarget = Join-Path $SitePackages 'multiagent_cli'
$copyCode = @'
from pathlib import Path
import shutil
import sys

source = Path(sys.argv[1]).resolve()
target = Path(sys.argv[2]).resolve()
if not (source / "__init__.py").is_file():
    raise SystemExit(f"MultiAgent 程序目录不存在：{source}")
if target.exists():
    shutil.rmtree(target)
shutil.copytree(
    source,
    target,
    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
)
'@
& $VenvPython -c $copyCode $PackageSource $PackageTarget
if ($LASTEXITCODE -ne 0) {
    throw "安装 MultiAgent 失败，退出码：$LASTEXITCODE"
}

New-Item -ItemType Directory -Force -Path $BinDir | Out-Null
$Launcher = Join-Path $BinDir 'multiagent.cmd'
if (Test-Path -LiteralPath $Launcher) {
    $existing = Get-Content -LiteralPath $Launcher -Raw -ErrorAction SilentlyContinue
    if ($existing -notmatch 'MultiAgent installer managed') {
        throw "$Launcher 已存在且不是 MultiAgent 安装入口。请先手动移除它，或设置 MULTIAGENT_BIN_DIR。"
    }
}

$quotedVenvPython = '"' + $VenvPython + '"'
$launcherContent = @"
@echo off
rem MultiAgent installer managed
call $quotedVenvPython -m multiagent_cli.web_launcher %*
"@
[System.IO.File]::WriteAllText($Launcher, $launcherContent, [System.Text.UTF8Encoding]::new($false))

if (-not $NoPath) {
    $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    $pathItems = @($userPath -split ';' | Where-Object { $_ })
    if ($pathItems -notcontains $BinDir) {
        $newUserPath = (($pathItems + $BinDir) -join ';')
        [Environment]::SetEnvironmentVariable('Path', $newUserPath, 'User')
    }
    if (($env:Path -split ';') -notcontains $BinDir) {
        $env:Path = "$BinDir;$env:Path"
    }
}

& $Launcher --help | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "命令入口验证失败，退出码：$LASTEXITCODE"
}

$agentSpecs = @(
    [pscustomobject]@{
        Command = 'claude'
        DisplayName = 'Claude Code'
        Package = '@anthropic-ai/claude-code'
    },
    [pscustomobject]@{
        Command = 'codex'
        DisplayName = 'Codex CLI'
        Package = '@openai/codex'
    }
)
$missingAgents = @($agentSpecs | Where-Object {
    -not (Get-Command $_.Command -ErrorAction SilentlyContinue)
})
$agentInstallFailed = $false

if ($missingAgents.Count -eq 0) {
    Write-Host '已检测到 Claude Code 和 Codex CLI。'
} elseif (-not $InstallAgents) {
    foreach ($agent in $missingAgents) {
        Write-Host "提示：未检测到 $($agent.DisplayName)（$($agent.Command)）。"
    }
    Write-Host '如需自动安装缺失的 Agent，请重新运行：'
    Write-Host 'powershell -ExecutionPolicy Bypass -File .\install.ps1 -InstallAgents'
} else {
    $npmCommand = Get-Command npm.cmd -ErrorAction SilentlyContinue
    if (-not $npmCommand) {
        $npmCommand = Get-Command npm -ErrorAction SilentlyContinue
    }
    if (-not $npmCommand) {
        Write-Warning '自动安装 Agent 需要 Node.js 和 npm。请先安装 Node.js 后重试。'
        $agentInstallFailed = $true
    } else {
        foreach ($agent in $missingAgents) {
            Write-Host "正在安装 $($agent.DisplayName) 官方 npm 包…"
            & $npmCommand.Source install --global $agent.Package
            if ($LASTEXITCODE -ne 0) {
                Write-Warning "$($agent.DisplayName) 安装失败；安装器不会自动提升为管理员权限。"
                $agentInstallFailed = $true
            }
        }

        $npmPrefix = (& $npmCommand.Source prefix --global).Trim()
        if ($npmPrefix) {
            $npmPathItems = @($env:Path -split ';' | Where-Object { $_ })
            if ($npmPathItems -notcontains $npmPrefix) {
                $env:Path = "$npmPrefix;$env:Path"
            }
            $npmUserPath = [Environment]::GetEnvironmentVariable('Path', 'User')
            $npmUserPathItems = @($npmUserPath -split ';' | Where-Object { $_ })
            if ($npmUserPathItems -notcontains $npmPrefix) {
                [Environment]::SetEnvironmentVariable(
                    'Path',
                    (($npmUserPathItems + $npmPrefix) -join ';'),
                    'User'
                )
            }
        }
        foreach ($agent in $agentSpecs) {
            if (-not (Get-Command $agent.Command -ErrorAction SilentlyContinue)) {
                Write-Warning "安装后仍找不到 $($agent.Command) 命令，请检查 npm 全局目录是否在 PATH 中。"
                $agentInstallFailed = $true
            }
        }
        if (-not $agentInstallFailed) {
            Write-Host 'Claude Code 与 Codex CLI 已准备完成。'
            Write-Host '首次使用时请分别运行 claude 和 codex，按官方流程完成登录或授权。'
        }
    }
}

Write-Host ''
Write-Host 'MultiAgent 安装完成。'
Write-Host "程序目录：$InstallRoot"
Write-Host "命令入口：$Launcher"
if ($NoPath) {
    Write-Host '已跳过 PATH 修改；请手动将该目录加入 PATH：' $BinDir
} else {
    Write-Host '已将命令目录加入当前用户 PATH；新开的终端即可运行 multiagent。'
}
Write-Host '现在可以运行：multiagent'
if ($agentInstallFailed) {
    exit 4
}
