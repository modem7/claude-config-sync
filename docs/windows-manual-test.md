# Windows manual test

A hands-on check of the Windows-only code paths in `claude_sync/` that CI does not run. Run it on a real Windows machine (Python 3.12 or newer, git 2.40 or newer; the `py` launcher is recommended, see the note in Set-up) in a **non-elevated** PowerShell. It takes about 20 minutes.

Nothing here touches your real profile. Every step uses a throwaway folder, a local bare repo as `origin`, a stub `gh`, and `USERPROFILE` pointed at the folder. Do not run `install.cmd` as part of this test: it runs `npm install -g` and writes into the real home.

The "Expect" lines come from the source and from the same flow run on Linux. None of them has been seen on Windows yet; the point of this page is to find out. If a result differs, record the exact output and the exit code (`$LASTEXITCODE`) in the table at the end and open an issue. Do not edit the tool to make a test pass.

## What CI shows, and what it does not

The `pytest (windows-latest)` job of `.github/workflows/ci.yml` runs `python -m pytest tests/py -v` on Windows. On the run for the Python port's merge commit it finished with 498 passed and 97 skipped, no failures. Windows-only tests that ran and passed: the lock (`test_lock.py`: two holders, a killed holder frees it, the handle is not inherited), the junction (`test_junction_created_on_windows`), a read-only file overwritten and a read-only tree deleted (`test_syncfiles.py`), the Git Bash and drive-letter path forms (`test_machineconf.py`, `test_memorypath.py`, `test_install.py`), and non-ASCII output to a narrow stream.

Skipped on Windows (so not tested there): everything that needs a real symlink, which includes every "refuse a link" test for profiles, overrides, `machines/` and the repo folders; the real-process signal tests (Ctrl-C, SIGTERM); the exec-bit tests; the `.sh` shim tests; the tool-installer tests (they use POSIX stubs).

| Windows-only path | CI | Manual test |
|---|---|---|
| `.cmd` shims actually run (exit code, quoting, clone path with spaces, `py` before `python`) | Only reads the file text (`test_cmd_shim_uses_py_launcher_first`); the shim never runs | W1 |
| `msvcrt.locking` timing and a hard-killed holder | `test_lock.py` | W2 (end to end through the shim) |
| Ctrl-C while waiting for the lock or during apply | Skipped | W3 |
| Junction made and removed without admin rights | Library level only | W4 |
| A junction inside a synced folder is never followed | Not run (the tests use symlinks) | W5 |
| A junction or link where a repo folder or override should be | Not run (skipped) | W6 |
| Read-only file overwritten, read-only tree deleted, through a real sync | Library level only | W7 |
| Drive-letter project paths, Git Bash form, `Z:` fallback to the home folder | Pure string tests; the real drive check is not run | W8 |
| Memory folder name for a path with spaces | String test only | W9 |
| CRLF and LF files stay byte for byte under `core.autocrlf=true` | Not run | W10 |
| Exec bit kept for a new hook committed from Windows | Skipped | W11 |
| Profile hooks ending in `.sh` skipped, `profile copy` lists every hook | Partly (`test_doctor.py`, `test_profile.py` skip some) | W12 |
| Non-ASCII hostname refused, non-ASCII output in a legacy code page | Yes | W13 |
| `install.cmd --repair` from a prompt outside the clone | Not run | Not in this test (see the note at the end) |

## Set up

Run these once in one PowerShell window. `$RepoUrl` is the URL or local path of the claude-config repo, on its default branch.

```powershell
$RepoUrl = "<repo-url-or-local-path>"
$T = Join-Path $env:TEMP "cs-test"
if (Test-Path $T) { cmd /c rmdir /s /q "$T" }
New-Item -ItemType Directory -Force "$T\project","$T\home","$T\gh","$T\precious" | Out-Null
$utf8 = New-Object Text.UTF8Encoding $false
function W($path, $text) { New-Item -ItemType Directory -Force (Split-Path $path) | Out-Null; [IO.File]::WriteAllText($path, $text, $utf8) }

# A tiny seed repo: the real code, with test content in place of the real config.
git clone -q -c core.autocrlf=false $RepoUrl "$T\seed"
git -C "$T\seed" remote remove origin
foreach ($d in "machines","dot-claude","dot-agents","memory","private","weekly-review","reports") { if (Test-Path "$T\seed\$d") { Remove-Item -Recurse -Force "$T\seed\$d" } }
W "$T\seed\dot-claude\CLAUDE.md" "# shared`n"
W "$T\seed\dot-claude\settings.json" "{`"theme`":`"dark`"}`n"
W "$T\seed\dot-claude\agents\a1.md" "agent`n"
W "$T\seed\dot-agents\skills\sk1\SKILL.md" "skill`n"
W "$T\seed\dot-agents\skills\.gitkeep" ""
W "$T\seed\machines\prof-test\hooks\linux-only.sh" "#!/usr/bin/env bash`necho linux`n"
W "$T\seed\machines\prof-test\hooks\win-ok.ps1" "Write-Output ok`n"
W "$T\seed\machines\prof-test\agents\pa.md" "profile agent`n"
W "$T\seed\machines\host-win.conf" "PRIMARY_PROJECT_PATH=$T\project`nPROFILE=prof-test`n"
W "$T\seed\memory\.gitkeep" ""
git init -q --bare -b master "$T\origin.git"
git -C "$T\seed" remote add origin "$T\origin.git"
git -C "$T\seed" add -A
git -C "$T\seed" -c user.name=t -c user.email=t@example.invalid commit -q -m seed
git -C "$T\seed" push -q origin HEAD:master
git clone -q -c core.autocrlf=false "$T\origin.git" "$T\repo"

# The Python to use (py launcher if present, else python).
$py = if (Get-Command py -ErrorAction SilentlyContinue) { py -3 -c "import sys; print(sys.executable)" } else { (Get-Command python).Source }

# A stub gh that records its calls (the real one is never used).
W "$T\gh\gh_stub.py" @"
import os, sys
open(os.environ["FAKE_GH_LOG"], "a").write("gh " + " ".join(sys.argv[1:]) + "\n")
a = sys.argv[1:3]
if a == ["auth", "status"]: print("Logged in")
elif a == ["pr", "create"]: print("https://github.example/t/r/pull/1")
elif a == ["pr", "list"] and "all" in sys.argv: print("nopr")
elif a == ["pr", "list"]: pass
else: sys.exit(1)
"@
W "$T\gh\gh.cmd" "@`"$py`" `"%~dp0gh_stub.py`" %*`r`n"

# The environment every test uses. Re-run this block in any new window.
$env:CLAUDE_SYNC_REPO_DIR = "$T\repo"
$env:CLAUDE_SYNC_CLAUDE_HOME = "$T\claude"
$env:CLAUDE_SYNC_AGENTS_HOME = "$T\agents"
$env:CLAUDE_SYNC_HOSTNAME = "host-win"
$env:CLAUDE_SYNC_PROJECT_PATH = "$T\project"
$env:CLAUDE_SYNC_REPO_URL = "$T\origin.git"
$env:CLAUDE_SYNC_GH = "$T\gh\gh.cmd"
$env:CLAUDE_SYNC_GH_REPO = "t/r"
$env:FAKE_GH_LOG = "$T\gh.log"
$env:CLAUDE_SYNC_LOCK_WAIT = "3"
$env:USERPROFILE = "$T\home"; $env:HOME = "$T\home"
$env:GIT_AUTHOR_NAME = "t"; $env:GIT_AUTHOR_EMAIL = "t@example.invalid"
$env:GIT_COMMITTER_NAME = "t"; $env:GIT_COMMITTER_EMAIL = "t@example.invalid"
$cs = "$T\repo\claude-sync.cmd"
$py; & $py --version        # Expect: a path, and Python 3.12 or newer
```
No `py` launcher (for example Python from the Microsoft Store, or `py` not on PATH)? That is fine: `$py` falls back to `python`, and every later step uses `$py`. The `.cmd` shims do the same (`py -3`, else `python`). Only the "`py` is used before `python`" check in W1 needs the launcher; skip it and note that in the results table. If `python` is only the Store stub, install Python 3.12 or newer first.

A live `~\.claude` for the test, with one local edit:

```powershell
W "$T\claude\CLAUDE.md" "# local edit`n"
```

## W1. The `.cmd` shim runs, with its arguments and exit code

```powershell
& $cs bogus; "exit=$LASTEXITCODE"
```
Expect: the usage text, first line `Usage: claude-sync.sh [push|pull|sync|bootstrap|doctor [remediate]|override add|remove|list [JQ_PATH]`, then `exit=1`.

```powershell
& $cs profile show "a b"; "exit=$LASTEXITCODE"
& $cs profile show "a&b"; "exit=$LASTEXITCODE"
```
Expect: `claude-sync: Not a valid profile name: a b` and then `... a&b`, each with exit 1, and no `'b' is not recognized` error (an unquoted `&` would run `b` as a command).

A clone path with spaces:
```powershell
git clone -q -c core.autocrlf=false "$T\origin.git" "$T\clone with spaces"
$env:CLAUDE_SYNC_REPO_DIR = "$T\clone with spaces"
& "$T\clone with spaces\claude-sync.cmd" profile list; "exit=$LASTEXITCODE"
$env:CLAUDE_SYNC_REPO_DIR = "$T\repo"
```
Expect: `host-win follows profile 'prof-test' (PROFILE= in machines/host-win.conf):` and its entries, `exit=0`.

`py` is used before `python`:
```powershell
New-Item -ItemType Directory -Force "$T\decoy" | Out-Null
W "$T\decoy\python.cmd" "@echo DECOY python used`r`n@exit /b 9`r`n"
$old = $env:PATH; $env:PATH = "$T\decoy;$old"
& $cs profile list | Select-Object -First 1
$env:PATH = $old
```
Expect: the normal `host-win follows ...` line and no `DECOY python used`.

## W2. The lock, end to end

```powershell
$hold = Start-Process $py -PassThru -WindowStyle Hidden -ArgumentList @("-c", "import sys,time; sys.path.insert(0, r'$T\repo'); from pathlib import Path; from claude_sync.lock import SyncLock; SyncLock(Path(r'$T\repo\.git\claude-sync.flock'), 0).__enter__(); time.sleep(300)")
Start-Sleep 2
(Measure-Command { & $cs sync; "exit=$LASTEXITCODE" }).TotalSeconds
```
Expect: `claude-sync: another claude-sync run is still working on <path of $T\repo> (waited 3s for its lock).`, `exit=1`, and a time between about 3.0 and 3.6 seconds.

Kill the holder hard, then run again at once:
```powershell
Stop-Process -Id $hold.Id -Force
& $cs sync; "exit=$LASTEXITCODE"
```
Expect: `claude-sync: pushed sync/host-win.` and `claude-sync: opened PR https://github.example/t/r/pull/1`, `exit=0`. This is also the first full sync, so `$T\claude`, `$T\agents` and `git -C "$T\origin.git" branch --list "sync/*"` now exist (`sync/host-win`). `Get-Content $T\gh.log` shows `gh auth status`, `gh pr list ...`, `gh pr create ...`.

A bad wait value stops every command:
```powershell
$env:CLAUDE_SYNC_LOCK_WAIT = "soon"; & $cs doctor; "exit=$LASTEXITCODE"; $env:CLAUDE_SYNC_LOCK_WAIT = "3"
```
Expect: `CLAUDE_SYNC_LOCK_WAIT must be a number of seconds, not 'soon'.`, exit 1.

## W3. Ctrl-C

Hold the lock as in W2 but with a long wait, then press Ctrl-C in the window that runs the sync:
```powershell
$hold = Start-Process $py -PassThru -WindowStyle Hidden -ArgumentList @("-c", "import sys,time; sys.path.insert(0, r'$T\repo'); from pathlib import Path; from claude_sync.lock import SyncLock; SyncLock(Path(r'$T\repo\.git\claude-sync.flock'), 0).__enter__(); time.sleep(300)")
Start-Sleep 2
$env:CLAUDE_SYNC_LOCK_WAIT = "60"
& $cs sync            # press Ctrl-C after a few seconds
"exit=$LASTEXITCODE"
```
Expect (unknown on Windows, so record what you see): no Python traceback. Ideally `claude-sync: stopped by Ctrl-C (exit 130). Re-run the command; ...` and `exit=130`. `cmd.exe` may ask `Terminate batch job (Y/N)?`; answer `Y` and note that it asked.

Then:
```powershell
Stop-Process -Id $hold.Id -Force; $env:CLAUDE_SYNC_LOCK_WAIT = "3"
& $cs doctor; "exit=$LASTEXITCODE"
```
Expect: `No issues found - sync is healthy.` plus a note that `sync/host-win` holds changes that are not merged, `exit=0`, and no `*.lock` file in `$T\repo\.git` (`Get-ChildItem $T\repo\.git -Filter *.lock`).

## W4. Junction for a skill, without admin rights

```powershell
([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)   # Expect: False
Get-Item "$T\claude\skills\sk1" | Format-List LinkType, Target
Get-Content "$T\claude\skills\sk1\SKILL.md"
```
Expect: `False`; `LinkType : Junction` and `Target : {<$T>\agents\skills\sk1}`; `skill`.

Remove the skill upstream and pull:
```powershell
git -C "$T\seed" rm -r -q dot-agents/skills/sk1
git -C "$T\seed" -c user.name=t -c user.email=t@example.invalid commit -q -m "drop sk1"
git -C "$T\seed" push -q origin HEAD:master
& $cs pull; "exit=$LASTEXITCODE"
Test-Path "$T\claude\skills\sk1"; Test-Path "$T\agents\skills\sk1"
```
Expect: `exit=0`; both `Test-Path` are `False` (the junction is gone, and the folder it pointed at is mirrored away by the pull, not by following the link). The Linux run of the same steps with a symlink gave this. The `.gitkeep` in the seed matters: git does not track an empty folder, and the tool skips a mirror whose repo folder is missing.

## W5. A junction in a synced folder is never followed

```powershell
"keep" | Set-Content "$T\precious\file.txt"
cmd /c mklink /J "$T\claude\hooks\evil" "$T\precious"   # works without admin rights
& $cs sync; "exit=$LASTEXITCODE"
Get-Content "$T\precious\file.txt"
git -C "$T\origin.git" ls-tree -r --name-only sync/host-win | Select-String evil
Test-Path "$T\claude\hooks\evil"
```
Expect (the Linux run with a symlink printed this): `claude-sync: warning: skipped link to a folder or missing target: <...>\hooks\evil`, then `claude-sync: pushed sync/host-win.`, `exit=0`; `keep`; no line from `Select-String`; `Test-Path` `False`. If `file.txt` is gone or `evil` appears in the pushed tree, that is a defect (the tool followed the junction).

## W6. A junction where a repo folder or an override should be

```powershell
Rename-Item "$T\repo\machines\prof-test" "prof-test.real"
cmd /c mklink /J "$T\repo\machines\prof-test" "$T\precious"
& $cs sync; "exit=$LASTEXITCODE"
& $cs profile show prof-test; "exit=$LASTEXITCODE"
Get-ChildItem "$T\precious"
```
Expect (unknown on Windows, record the text): a refusal naming the link, a non-zero exit, and `file.txt` as the only thing in `$T\precious` (nothing written through the junction). Undo: `cmd /c rmdir "$T\repo\machines\prof-test"; Rename-Item "$T\repo\machines\prof-test.real" prof-test`.

If you have Developer Mode on, repeat with `New-Item -ItemType SymbolicLink -Path "$T\repo\machines\host-win.settings.json" -Target "$T\precious\file.txt"` and `& $cs override list`. Expect a refusal: `... is a link or leads outside ...\machines.` Remove it afterwards.

## W7. Read-only files in the way

A read-only file that an upstream change must overwrite, and a read-only tree that must be deleted:
```powershell
W "$T\seed\dot-claude\agents\gone\x.md" "x`n"
W "$T\seed\dot-claude\agents\a1.md" "agent`nupstream line`n"
git -C "$T\seed" add -A; git -C "$T\seed" -c user.name=t -c user.email=t@example.invalid commit -q -m "add gone, edit a1.md"
git -C "$T\seed" push -q origin HEAD:master
& $cs pull; "exit=$LASTEXITCODE"
attrib +R "$T\claude\agents\a1.md"; attrib +R /S "$T\claude\agents\gone\*"
# Upstream: edit a1.md again and delete the folder.
W "$T\seed\dot-claude\agents\a1.md" "agent`nupstream line 2`n"
git -C "$T\seed" rm -r -q dot-claude/agents/gone
git -C "$T\seed" add -A; git -C "$T\seed" -c user.name=t -c user.email=t@example.invalid commit -q -m "edit a1.md again, drop gone"
git -C "$T\seed" push -q origin HEAD:master
& $cs pull; "exit=$LASTEXITCODE"
Get-Content "$T\claude\agents\a1.md"; Test-Path "$T\claude\agents\gone"
```
Expect: both pulls `exit=0` with no `PermissionError` or `Access is denied`; the last two commands print `agent`, `upstream line 2` and `False`. (The skill from W4 is gone upstream, so run W4 first; if you skip it, nothing else changes.)

## W8. Drive-letter project paths and the `Z:` fallback

These call the installer's own functions, so nothing is cloned or installed:
```powershell
& $py -c "from claude_sync.machineconf import windows_native_path as w; print(w('/z/projects'), w('z:/projects'), w('Z:\\projects'), w('/c/Users/x'))"
```
Expect: `Z:\projects Z:\projects Z:\projects C:\Users\x`.

```powershell
& $py -c "from pathlib import Path; from claude_sync.install import resolve_project_path as r; print(r(r'Q:\projects', Path.home())); print(r(r'$env:TEMP', Path.home()))"
```
Expect: first line is `$T\home` (no `Q:` drive, so the home folder, which here is the throwaway `USERPROFILE`), second line is your temp folder (it exists, so it is kept). On the real machine, run the same with `Z:\projects` in a fresh window **without** the `USERPROFILE` override: expect the real home folder when `Z:` is not mounted, and `Z:\projects` when it is (mapped drives are per logon session: an elevated window may not see `Z:`).

The real installer also asks `The project path ... does not exist. Create it? [y/N]` before it falls back, but only on a terminal: answer `n` (or pipe the input) and the fallback message above appears; answer `y` and it prints `Created ...` and stores that path.

Optional, in a window where `$env:USERPROFILE` is still the throwaway one: `--project-path` through the real installer prints `The project path Q:\projects does not exist; using your home folder ... instead.`, but the installer also runs `npm install -g`, so skip it unless you accept that.

Also by hand, on a machine with a read-only Microsoft Store Python: `install.cmd` prints `Retrying with --user: the system-wide install was refused.` and pip then installs `mcp<2 aiosqlite pyyaml` into the user site. (CI cannot show this: it needs a read-only Python folder.)

## W9. Memory folder for a path with spaces

```powershell
$P = "$T\my project"; New-Item -ItemType Directory -Force $P | Out-Null
$enc = $P -replace '[^A-Za-z0-9]', '-'
W "$T\claude\projects\$enc\memory\m1.md" "memory`n"
$env:CLAUDE_SYNC_PROJECT_PATH = $P
& $cs sync; "exit=$LASTEXITCODE"
git -C "$T\origin.git" show "sync/host-win:memory/m1.md"
$env:CLAUDE_SYNC_PROJECT_PATH = "$T\project"
```
Expect: `exit=0`, and `memory`. With the wrong folder name the file would not be captured and `git show` fails. Check the first word of `$enc` is your drive letter, then `--`, as in `C--Users-...`.

## W10. Line endings under `core.autocrlf=true`

```powershell
git config --global core.autocrlf      # note the value; the test needs it to be true or unset
attrib -R "$T\claude\agents\a1.md"   # left read-only by W7
[IO.File]::WriteAllText("$T\claude\CLAUDE.md", "one`r`ntwo`r`n", $utf8)
[IO.File]::WriteAllText("$T\claude\agents\a1.md", "one`ntwo`n", $utf8)
& $cs sync; "exit=$LASTEXITCODE"
& $py -c "import subprocess as s; g=lambda p: s.run(['git','-C',r'$T\origin.git','cat-file','blob','sync/host-win:'+p],capture_output=True).stdout; [print(p, d.count(b'\r\n'), d.count(b'\n')) for p,d in ((q,g(q)) for q in ('dot-claude/CLAUDE.md','dot-claude/agents/a1.md'))]"
```
Expect: `dot-claude/CLAUDE.md 2 2` (CRLF kept) and `dot-claude/agents/a1.md 0 2` (LF kept).

## W11. Exec bit for a new hook committed from Windows

```powershell
W "$T\claude\hooks\new-hook.sh" "#!/usr/bin/env bash`necho hi`n"
& $cs sync; "exit=$LASTEXITCODE"
git -C "$T\origin.git" ls-tree sync/host-win dot-claude/hooks/new-hook.sh
```
Expect: `100755 blob <sha>	dot-claude/hooks/new-hook.sh`. `100644` is a defect: the hook would arrive non-executable on Linux.

## W12. Profile hooks and `profile copy`

```powershell
Get-ChildItem "$T\claude\hooks" | Select-Object -ExpandProperty Name
& $cs profile show prof-test
& $cs profile copy prof-test --yes; "exit=$LASTEXITCODE"
```
Expect: `win-ok.ps1` is in the live `hooks` folder and `linux-only.sh` is not (a profile hook ending in `.sh` is skipped on Windows). `profile copy` lists every hook file under "These executable hook scripts will be copied" (both `linux-only.sh` and `win-ok.ps1`, since Windows has no exec bit), `exit=0`.

## W13. Hostname and encoding

```powershell
$env:CLAUDE_SYNC_HOSTNAME = "h$([char]0xF6)st"
$env:PYTHONIOENCODING = "ascii"      # stands in for a legacy code page
& $cs doctor 2> "$T\err.txt"; "exit=$LASTEXITCODE"
Get-Content "$T\err.txt"
Remove-Item Env:PYTHONIOENCODING; $env:CLAUDE_SYNC_HOSTNAME = "host-win"
```
Expect: `exit=1`, `claude-sync: Not a valid hostname: h\xf6st` in the file, and no `Traceback` or `UnicodeEncodeError`.

## Override round trip

```powershell
W "$T\claude\settings.json" "{`"theme`":`"light`",`"localOnly`":1}`n"
& $cs override add ".localOnly"; & $cs override list; & $cs override remove ".localOnly"; & $cs override list
```
Expect: `.localOnly is now specific to host-win; ...`, `.localOnly = 1`, `.localOnly on host-win now follows the shared settings.`, `No overrides for prof-test.`

## Clean up

```powershell
Remove-Item Env:CLAUDE_SYNC_* , Env:FAKE_GH_LOG
cmd /c rmdir "$T\claude\hooks\evil" 2>$null
cmd /c rmdir /s /q "$T"
```
Close the window afterwards: it holds throwaway values for `USERPROFILE` and `HOME`.

## Results

| Test | Pass / fail | Exact output and exit code where it differed |
|---|---|---|
| W1 shim | | |
| W2 lock | | |
| W3 Ctrl-C | | |
| W4 junction, no admin | | |
| W5 junction not followed | | |
| W6 linked repo folder refused | | |
| W7 read-only | | |
| W8 paths and `Z:` fallback | | |
| W9 memory folder | | |
| W10 line endings | | |
| W11 exec bit | | |
| W12 profile hooks | | |
| W13 hostname and encoding | | |

## Not covered here

- `install.cmd --repair`: it deletes and re-clones the repo folder, runs the tool installs and works on the real home. Run it on a spare user account, from a prompt outside the clone, after reading `docs/runbook.md`.
- The `python` fallback of the `.cmd` shims when `py` is missing: only testable on a machine without the launcher.
- Windows Terminal and PowerShell 5.1 differ in how they pass embedded double quotes to a native program. W1 uses spaces and `&`, which both pass the same way.
