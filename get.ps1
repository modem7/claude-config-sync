<#
.SYNOPSIS
One-command setup of claude-config on Windows (PowerShell 5.1 and 7).

.DESCRIPTION
Installs git, Python 3.12+ and gh with winget if needed, checks pip and
Git Bash, logs gh in, clones your private sync repo and runs its
install.cmd. Each step prints a short "[n/6] ..." line.

`irm URL | iex` cannot pass parameters, so the settings can also come
from environment variables: CLAUDE_SYNC_REPO (OWNER/REPO) and
CLAUDE_SYNC_DIR (the clone folder). The installer itself also reads
CLAUDE_SYNC_PROJECT_PATH and CLAUDE_SYNC_PROFILE.

.PARAMETER Repo
Your private sync repo as OWNER/REPO. Else $env:CLAUDE_SYNC_REPO, else a prompt.
.PARAMETER Dir
Where to clone it. Else $env:CLAUDE_SYNC_DIR, else $HOME\claude-config.
.PARAMETER ProjectPath
Passed to install.cmd as --project-path.
.PARAMETER ProfileName
Use it as -Profile NAME. Passed to install.cmd as --profile. (The
variable is $ProfileName, so $PROFILE in your session is never touched.)
.PARAMETER Yes
Accept every default answer here. install.cmd still asks for the project
path on a terminal unless -ProjectPath is given.
.PARAMETER DryRun
Print the commands that change things instead of running them.
.PARAMETER Tier
basic or full. Passed to install.cmd as --tier: what a new machine syncs
(default full). basic skips memory, the pip packages and ccusage.
.PARAMETER SkipPythonDeps
Passed to install.cmd as --skip-python-deps.

.EXAMPLE
& ([scriptblock]::Create((irm URL/get.ps1))) -Repo owner/name -Yes
.EXAMPLE
$env:CLAUDE_SYNC_REPO = 'owner/name'; irm URL/get.ps1 | iex
.EXAMPLE
powershell -NoProfile -ExecutionPolicy Bypass -Command "irm URL/get.ps1 | iex"
#>
[CmdletBinding()]
param(
    [string]$Repo = '',
    [string]$Dir = '',
    [string]$ProjectPath = '',
    [Alias('Profile')][string]$ProfileName = '',
    [ValidateSet('', 'basic', 'full')][string]$Tier = '',
    [switch]$Yes,
    [switch]$DryRun,
    [switch]$SkipPythonDeps
)

# Everything lives in this function, so under `iex` (which runs in the
# caller's scope) no setting or helper leaks into the user's session.
# The exit code goes back through $State.Rc; the function writes nothing
# to its output, so interactive programs keep the console.
function Invoke-ClaudeConfigGet {
    param(
        [hashtable]$State,
        [string]$Repo,
        [string]$Dir,
        [string]$ProjectPath,
        [string]$ProfileName,
        [string]$Tier,
        [bool]$Yes,
        [bool]$DryRun,
        [bool]$SkipPythonDeps
    )
    Set-StrictMode -Version 2.0
    # Continue, never Stop: 5.1 turns a native command's stderr into a
    # terminating error under Stop. Exit codes are checked instead.
    $ErrorActionPreference = 'Continue'
    $State.Rc = 1
    $marker = 'CCGET: '
    $st = @{ Ok = $false }

    function Say([string]$Text) { Write-Host $Text }
    function Warn([string]$Text) { [Console]::Error.WriteLine("get.ps1: $Text") }
    function Fail([string]$Message, [string[]]$Hints = @()) {
        $lines = @($Message) + @($Hints | ForEach-Object { "  $_" })
        throw ($marker + ($lines -join "`n"))
    }
    function ConvertTo-ArgText([string]$Text) {
        if ($Text -eq '' -or $Text -match '[\s"]') { return '"' + ($Text -replace '"', '\"') + '"' }
        return $Text
    }
    function Show-Cmd([string]$Exe, [string[]]$ArgList) {
        return ((@($Exe) + @($ArgList)) | ForEach-Object { ConvertTo-ArgText $_ }) -join ' '
    }
    function Test-Command([string]$Name) {
        return [bool](Get-Command $Name -CommandType Application -ErrorAction SilentlyContinue)
    }
    # Read-only: runs a program and captures its output. Rc -1 when it
    # could not start.
    function Get-Output([string]$Exe, [string[]]$ArgList = @()) {
        $result = @{ Rc = -1; Out = '' }
        try {
            $lines = & $Exe @ArgList 2>&1
            $result.Rc = $LASTEXITCODE
            $result.Out = (@($lines) | ForEach-Object { "$_" }) -join "`n"
        } catch {
            $result.Rc = -1
        }
        return $result
    }
    # A command that changes something: printed, then run on the console
    # (or only printed). Sets $st.Ok. Call it as a statement, never assign
    # it: capturing would take the console away from gh and the installer.
    function Invoke-Step([string]$Exe, [string[]]$ArgList = @()) {
        $shown = Show-Cmd $Exe $ArgList
        if ($DryRun) {
            Say "[dry run] would run: $shown"
            $st.Ok = $true
            return
        }
        Say "+ $shown"
        $st.Ok = $false
        try {
            & $Exe @ArgList
            $st.Ok = ($LASTEXITCODE -eq 0)
        } catch {
            Warn "Could not start ${Exe}: $($_.Exception.Message)"
        }
    }
    function Test-CanPrompt { return -not [Console]::IsInputRedirected }
    # Read-YesNo "Question?" $default -> $true for yes. -Yes and -DryRun take the default.
    function Read-YesNo([string]$Question, [bool]$Default) {
        $hint = '[y/N]'
        if ($Default) { $hint = '[Y/n]' }
        $word = 'no'
        if ($Default) { $word = 'yes' }
        if ($Yes) { Say "$Question $hint $word (-Yes)"; return $Default }
        if ($DryRun) { Say "$Question $hint $word (dry run assumes the default)"; return $Default }
        if (-not (Test-CanPrompt)) {
            Fail "No terminal to ask: $Question" @('Re-run with -Yes to accept the defaults.')
        }
        $answer = Read-Host "$Question $hint"
        if ([string]::IsNullOrWhiteSpace($answer)) { return $Default }
        return ($answer.Trim() -match '^(y|yes)$')
    }
    # A URL with any user:token@ part replaced, for printing.
    function Hide-UrlSecret([string]$Url) { return ($Url -replace '://[^/]*@', '://***@') }
    function Get-Version([string]$Text) {
        $m = [regex]::Match($Text, '(\d+)\.(\d+)')
        if (-not $m.Success) { return $null }
        return [version]("{0}.{1}" -f $m.Groups[1].Value, $m.Groups[2].Value)
    }
    # $env:Path from the Machine and User values, so tools winget just
    # installed are found without a new terminal. Entries only in this
    # session's Path are kept at the end.
    function Update-SessionPath {
        $machine = [Environment]::GetEnvironmentVariable('Path', 'Machine')
        $user = [Environment]::GetEnvironmentVariable('Path', 'User')
        $fresh = @(@($machine, $user) | Where-Object { $_ } | ForEach-Object { $_ -split ';' } | Where-Object { $_ })
        if ($fresh.Count -eq 0) { return }
        $old = @($env:Path -split [IO.Path]::PathSeparator | Where-Object { $_ })
        $extra = @($old | Where-Object { $fresh -notcontains $_ })
        $env:Path = (@($fresh) + @($extra)) -join [IO.Path]::PathSeparator
    }

    # Python: `py -3` when the launcher exists (install.cmd uses it first),
    # else `python`. Only a command that prints a real version counts: the
    # Microsoft Store stub exits 9009 or prints nothing.
    function Find-Python {
        $found = @{ Cmd = @(); Version = $null; Label = '' }
        if (Test-Command 'py') {
            $r = Get-Output 'py' @('-3', '--version')
            $v = $null
            if ($r.Rc -eq 0 -and $r.Out -match 'Python \d') { $v = Get-Version $r.Out }
            $found.Cmd = @('py', '-3'); $found.Version = $v; $found.Label = 'py -3'
            return $found
        }
        if (Test-Command 'python') {
            $r = Get-Output 'python' @('--version')
            if ($r.Rc -eq 0 -and $r.Out -match 'Python \d') {
                $found.Cmd = @('python'); $found.Version = Get-Version $r.Out; $found.Label = 'python'
            }
        }
        return $found
    }
    function Invoke-Py([hashtable]$Py, [string[]]$ArgList) {
        $prefix = @($Py.Cmd | Select-Object -Skip 1)
        return Get-Output $Py.Cmd[0] (@($prefix) + @($ArgList))
    }
    function Find-GitBash {
        $candidates = @()
        if ($env:ProgramFiles) {
            $candidates += (Join-Path (Join-Path (Join-Path $env:ProgramFiles 'Git') 'bin') 'bash.exe')
        }
        if (Test-Command 'git') {
            $r = Get-Output 'git' @('--exec-path')
            if ($r.Rc -eq 0 -and $r.Out.Trim()) {
                # ...\Git\mingw64\libexec\git-core: walk up to the folder with bin\bash.exe.
                $p = $r.Out.Trim()
                for ($i = 0; $i -lt 4 -and $p; $i++) {
                    $p = Split-Path -Parent $p
                    if ($p) { $candidates += (Join-Path (Join-Path $p 'bin') 'bash.exe') }
                }
            }
        }
        foreach ($c in $candidates) {
            if (Test-Path -LiteralPath $c -PathType Leaf) { return $c }
        }
        return $null
    }
    function Test-BashPython([string]$Bash) {
        $r = Get-Output $Bash @('-lc', 'python3 --version')
        $v = $null
        if ($r.Rc -eq 0 -and $r.Out -match 'Python \d') { $v = Get-Version $r.Out }
        return @{ Ok = ($null -ne $v -and $v -ge [version]'3.12'); Out = $r.Out.Trim(); Rc = $r.Rc }
    }

    try {
        if ($DryRun) { Say 'Dry run: commands that change things are printed, not run.' }

        # --- 1. system ---
        Say '[1/6] Checking the system'
        if ($env:VIRTUAL_ENV) {
            Fail "A Python virtualenv is active ($env:VIRTUAL_ENV)." @(
                'The installer must use the system Python. Run "deactivate" (or open a new terminal), then re-run.')
        }
        $hasWinget = Test-Command 'winget'
        $wingetText = 'not found'
        if ($hasWinget) { $wingetText = 'found' }
        Say "PowerShell $($PSVersionTable.PSVersion), winget: $wingetText"

        if (-not $Repo) { $Repo = "$env:CLAUDE_SYNC_REPO" }
        if (-not $Repo) {
            if (-not (Test-CanPrompt)) {
                Fail 'No repo given and no terminal to ask for one.' @(
                    'Pass -Repo OWNER/REPO, or set $env:CLAUDE_SYNC_REPO = "OWNER/REPO", then re-run.')
            }
            $Repo = (Read-Host 'Your private repo as OWNER/REPO').Trim()
        }
        if ($Repo -notmatch '^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$' -or $Repo.StartsWith('-')) {
            Fail "Not an OWNER/REPO name: '$Repo'." @('Give it as OWNER/REPO, not a URL.')
        }
        if (-not $Dir) { $Dir = "$env:CLAUDE_SYNC_DIR" }
        if (-not $Dir) { $Dir = Join-Path $HOME 'claude-config' }
        $Dir = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Dir)

        # --- 2. git, Python, gh ---
        Say '[2/6] Checking git, Python and gh'
        $packages = [ordered]@{ git = 'Git.Git'; python = 'Python.Python.3.12'; gh = 'GitHub.cli' }
        $missing = @()
        $gitVersion = $null
        if (Test-Command 'git') { $gitVersion = Get-Version (Get-Output 'git' @('--version')).Out }
        if ($null -ne $gitVersion -and $gitVersion -ge [version]'2.40') { Say "git ${gitVersion}: ok" } else { $missing += 'git' }
        $py = Find-Python
        if ($null -ne $py.Version -and $py.Version -ge [version]'3.12') {
            Say "Python $($py.Version) ($($py.Label)): ok"
        } else {
            $missing += 'python'
        }
        if (Test-Command 'gh') { Say 'gh: ok' } else { $missing += 'gh' }

        if ($missing.Count -gt 0) {
            if (-not $hasWinget) {
                Fail "Missing or too old: $($missing -join ', '). winget is not available to install them." @(
                    'Install them yourself: git 2.40+ (https://git-scm.com/download/win),',
                    'Python 3.12+ (https://www.python.org/downloads/windows/) and gh (https://cli.github.com),',
                    'then open a new terminal and re-run.')
            }
            foreach ($tool in $missing) {
                $why = "$tool is not installed."
                if ($tool -eq 'git' -and $null -ne $gitVersion) { $why = "git $gitVersion is older than 2.40." }
                if ($tool -eq 'python' -and $null -ne $py.Version) { $why = "Python $($py.Version) ($($py.Label)) is older than 3.12." }
                if (-not (Read-YesNo "$why Install $($packages[$tool]) with winget?" $true)) {
                    Fail "$tool is needed. Install it, then re-run."
                }
                Invoke-Step 'winget' @('install', '--id', $packages[$tool], '-e',
                    '--accept-package-agreements', '--accept-source-agreements')
                if (-not $st.Ok) {
                    Fail "winget could not install $($packages[$tool])." @(
                        "Install it yourself (winget install --id $($packages[$tool]) -e), then open a new terminal and re-run.")
                }
            }
            if (-not $DryRun) {
                Update-SessionPath
                $gitVersion = $null
                if (Test-Command 'git') { $gitVersion = Get-Version (Get-Output 'git' @('--version')).Out }
                $py = Find-Python
                $still = @()
                if ($null -eq $gitVersion -or $gitVersion -lt [version]'2.40') { $still += 'git 2.40+' }
                if ($null -eq $py.Version -or $py.Version -lt [version]'3.12') { $still += 'Python 3.12+' }
                if (-not (Test-Command 'gh')) { $still += 'gh' }
                if ($still.Count -gt 0) {
                    Fail "Installed, but still not found on PATH: $($still -join ', ')." @(
                        'Close and reopen the terminal, then re-run this.')
                }
                Say "git $gitVersion, Python $($py.Version) ($($py.Label)) and gh: ok"
            }
        }

        # Git Bash: the Claude hooks run claude-sync.sh in it, with python3.
        $bash = Find-GitBash
        if (-not $bash) {
            if ($DryRun -and $missing -contains 'git') {
                Say 'Git Bash: comes with Git for Windows (dry run: not checked).'
            } else {
                Warn 'Git Bash (bash.exe) not found. It comes with Git for Windows: winget install --id Git.Git -e'
                Warn '  The Claude session hooks need it; the install itself carries on.'
            }
        } else {
            $check = Test-BashPython $bash
            if ($check.Ok) {
                Say "Git Bash python3: ok ($($check.Out))"
            } else {
                Warn "In Git Bash, 'python3 --version' did not print 3.12+ (got: '$($check.Out)', exit $($check.Rc))."
                Warn '  The Claude hooks run in Git Bash and need a real python3 there.'
                Warn '  The Microsoft Store stub does not count.'
                $pyExe = $null
                if ($py.Cmd.Count -gt 0) {
                    $r = Invoke-Py $py @('-c', 'import sys; print(sys.executable)')
                    if ($r.Rc -eq 0 -and $r.Out.Trim()) { $pyExe = $r.Out.Trim() }
                }
                if ($pyExe) {
                    $target = Join-Path (Split-Path -Parent $pyExe) 'python3.exe'
                    if (Test-Path -LiteralPath $target) {
                        Warn "  $target exists, so its folder is not first on Git Bash's PATH."
                        Warn '  Move that folder before WindowsApps in PATH, or turn off the python3'
                        Warn '  App execution alias (Settings, Apps, Advanced app settings).'
                    } elseif (Read-YesNo "Copy $pyExe to $target so Git Bash finds python3?" $false) {
                        if ($DryRun) {
                            Say "[dry run] would copy $pyExe to $target"
                        } else {
                            Copy-Item -LiteralPath $pyExe -Destination $target
                            $check = Test-BashPython $bash
                            if ($check.Ok) {
                                Say "Git Bash python3: ok now ($($check.Out))"
                            } else {
                                Warn "  Still no python3 3.12+ in Git Bash. Put $(Split-Path -Parent $pyExe) on PATH,"
                                Warn '  before WindowsApps, or turn off the python3 App execution alias.'
                            }
                        }
                    } else {
                        Warn '  Not copied. The install carries on; fix python3 in Git Bash before the hooks run.'
                    }
                } else {
                    Warn '  Install Python 3.12+ and make python3 work in Git Bash, then the hooks will run.'
                }
            }
        }

        # --- 3. pip ---
        Say '[3/6] Checking pip'
        $extras = @(@('pipx', 'uv') | Where-Object { Test-Command $_ })
        if ($extras.Count -gt 0) { Say "found $($extras -join '/'): not needed, not used" }
        if ($Tier -eq 'basic' -or "$env:CLAUDE_SYNC_TIER" -eq 'basic') {
            Say 'basic tier: no pip packages, not checking pip.'
        } elseif ($SkipPythonDeps) {
            Say '-SkipPythonDeps given: not checking pip.'
        } elseif ($py.Cmd.Count -eq 0) {
            Say '[dry run] Python is not installed yet, so pip is not checked.'
        } else {
            $pipOk = (Invoke-Py $py @('-m', 'pip', '--version')).Rc -eq 0
            if ($pipOk) {
                Say 'pip: ok'
            } else {
                if (Read-YesNo "pip is not installed for $($py.Label). Install it?" $true) {
                    $prefix = @($py.Cmd | Select-Object -Skip 1)
                    Invoke-Step $py.Cmd[0] (@($prefix) + @('-m', 'ensurepip', '--upgrade'))
                    $pipOk = $st.Ok
                    if ($pipOk -and -not $DryRun) { $pipOk = (Invoke-Py $py @('-m', 'pip', '--version')).Rc -eq 0 }
                    if (-not $pipOk) { Warn 'Could not install pip.' }
                }
                if (-not $pipOk) {
                    $SkipPythonDeps = $true
                    Say 'Continuing with -SkipPythonDeps. Only what the agent-memory plugin needs'
                    Say '  (its pip packages) is skipped; the sync itself is unaffected.'
                }
            }
        }

        # --- 4. GitHub login ---
        Say '[4/6] Checking the GitHub login'
        $loggedIn = $false
        if (Test-Command 'gh') {
            $loggedIn = (Get-Output 'gh' @('auth', 'status', '--hostname', 'github.com')).Rc -eq 0
        }
        if ($loggedIn) {
            Say 'gh is logged in to github.com: ok'
        } else {
            if (-not $DryRun -and -not (Test-CanPrompt)) {
                Fail "gh is not logged in to github.com and there is no terminal for 'gh auth login'." @(
                    "Run 'gh auth login' yourself (or set GH_TOKEN), then re-run.")
            }
            Invoke-Step 'gh' @('auth', 'login', '--hostname', 'github.com', '--git-protocol', 'https', '--web')
            if (-not $st.Ok) { Fail 'gh auth login failed.' @("Run 'gh auth login' yourself, then re-run.") }
            Invoke-Step 'gh' @('auth', 'setup-git')
            if (-not $st.Ok) { Fail 'gh auth setup-git failed.' @('Run it yourself, then re-run.') }
        }

        # --- 5. clone ---
        Say "[5/6] Getting $Repo into $Dir"
        if (Test-Path -LiteralPath (Join-Path $Dir '.git')) {
            Say "$Dir is already cloned, using it (left as it is)."
            $origin = (Get-Output 'git' @('-C', $Dir, 'remote', 'get-url', 'origin'))
            if ($origin.Rc -ne 0 -or -not $origin.Out.Trim()) {
                Fail "$Dir has no 'origin' remote, so the installer cannot use it." @(
                    'Move it away or pick another folder with -Dir, then re-run.')
            }
            if ($origin.Out.IndexOf($Repo, [StringComparison]::OrdinalIgnoreCase) -lt 0) {
                Warn "Warning: $Dir comes from $(Hide-UrlSecret $origin.Out.Trim()), not $Repo. Using it anyway."
            }
        } elseif (Test-Path -LiteralPath $Dir) {
            Fail "$Dir exists but is not a git clone." @('Move it away, or pick another folder with -Dir, then re-run.')
        } else {
            # Everything after -- goes to git: keep line endings as they are.
            Invoke-Step 'gh' @('repo', 'clone', $Repo, $Dir, '--', '-c', 'core.autocrlf=false')
            if (-not $st.Ok) {
                Fail "Could not clone $Repo." @("Check the name and that this gh login can read it (gh repo view $Repo).")
            }
        }

        # --- 6. installer ---
        Say '[6/6] Running the installer'
        if ($DryRun -and -not (Test-Path -LiteralPath (Join-Path $Dir '.git'))) {
            $url = "<origin URL of $Dir>"
        } else {
            $url = (Get-Output 'git' @('-C', $Dir, 'remote', 'get-url', 'origin')).Out.Trim()
        }
        $pass = @()
        if ($ProjectPath) { $pass += @('--project-path', $ProjectPath) }
        if ($ProfileName) { $pass += @('--profile', $ProfileName) }
        if ($Tier) { $pass += @('--tier', $Tier) }
        if ($SkipPythonDeps) { $pass += '--skip-python-deps' }
        $installer = Join-Path $Dir 'install.cmd'

        $saved = @{ CLAUDE_SYNC_REPO_URL = $env:CLAUDE_SYNC_REPO_URL; CLAUDE_SYNC_REPO_DIR = $env:CLAUDE_SYNC_REPO_DIR }
        $rc = 0
        try {
            if ($DryRun) {
                Say "[dry run] would set CLAUDE_SYNC_REPO_URL=$(Hide-UrlSecret $url) CLAUDE_SYNC_REPO_DIR=$Dir"
                Say "[dry run] would run: $(Show-Cmd $installer $pass)"
            } else {
                $env:CLAUDE_SYNC_REPO_URL = $url
                $env:CLAUDE_SYNC_REPO_DIR = $Dir
                Say "+ CLAUDE_SYNC_REPO_URL=$(Hide-UrlSecret $url) CLAUDE_SYNC_REPO_DIR=$Dir"
                Say "+ $(Show-Cmd $installer $pass)"
                & $installer @pass
                $rc = $LASTEXITCODE
            }
        } finally {
            foreach ($k in @($saved.Keys)) {
                if ($null -eq $saved[$k]) {
                    Remove-Item -LiteralPath "Env:$k" -ErrorAction SilentlyContinue
                } else {
                    Set-Item -LiteralPath "Env:$k" -Value $saved[$k]
                }
            }
        }
        if ($rc -ne 0) {
            Warn "install.cmd failed (exit $rc). Fix what it says above, then re-run this; the clone is kept."
            $State.Rc = $rc
            return
        }
        Say ''
        if ($DryRun) {
            Say 'Dry run finished: nothing was changed.'
        } else {
            Say 'Done. Next steps:'
            Say "  1. Merge this machine's sync PR (sync/<hostname>) on GitHub, if one was opened:"
            Say "       gh pr list --repo $Repo"
            Say "  2. Check the machine: & `"$(Join-Path $Dir 'claude-sync.cmd')`" doctor"
        }
        $State.Rc = 0
    } catch {
        $text = "$($_.Exception.Message)"
        if ($text.StartsWith($marker)) {
            $lines = $text.Substring($marker.Length) -split "`n"
            Warn $lines[0]
            foreach ($l in ($lines | Select-Object -Skip 1)) { [Console]::Error.WriteLine($l) }
        } else {
            Warn "unexpected error: $text"
            [Console]::Error.WriteLine('  This is a bug in get.ps1. Report it with the command you ran and this message.')
        }
        $State.Rc = 1
    }
}

$ccgetState = @{ Rc = 1 }
Invoke-ClaudeConfigGet -State $ccgetState -Repo $Repo -Dir $Dir -ProjectPath $ProjectPath `
    -ProfileName $ProfileName -Tier $Tier -Yes ([bool]$Yes) -DryRun ([bool]$DryRun) -SkipPythonDeps ([bool]$SkipPythonDeps)

# `exit` only when run as this file (-File or .\get.ps1), never under iex,
# a scriptblock or dot-sourcing, where it would close the user's window.
$ccgetFile = $null
try { $ccgetFile = $MyInvocation.MyCommand.Path } catch { $ccgetFile = $null }
$ccgetAsFile = $false
if ($ccgetFile -and $MyInvocation.InvocationName -ne '.' -and (Test-Path -LiteralPath $ccgetFile)) {
    $ccgetAsFile = [bool](Select-String -LiteralPath $ccgetFile -SimpleMatch 'function Invoke-ClaudeConfigGet' -Quiet)
}
$ccgetRc = $ccgetState.Rc
Remove-Variable -Name ccgetState, ccgetFile -ErrorAction SilentlyContinue
Remove-Item -LiteralPath Function:\Invoke-ClaudeConfigGet -ErrorAction SilentlyContinue
if ($ccgetAsFile) {
    Remove-Variable -Name ccgetAsFile -ErrorAction SilentlyContinue
    exit $ccgetRc
}
$global:LASTEXITCODE = $ccgetRc
Remove-Variable -Name ccgetAsFile, ccgetRc -ErrorAction SilentlyContinue
