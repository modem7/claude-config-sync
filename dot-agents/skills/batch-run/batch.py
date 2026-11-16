#!/usr/bin/env python3
"""Run several read-only shell commands in parallel and print one compact summary.

This lets a model gather a lot of context from a single tool call, instead of
many round trips that each return large raw output.

Usage:
  batch.py [options] -- CMD1 -- CMD2 ...
  batch.py [options] -f FILE        (FILE "-" reads standard input)

Options:
  --max-lines N   keep at most N lines per command (default 20)
  --max-chars N   keep at most N characters per command (default 2000)
  --timeout SECS  kill a command after SECS seconds (default 60)
  --jobs N        run at most N commands at once (default 8)
  --cwd DIR       run every command in DIR
  --tail          keep the last lines instead of the first
  --json-keys     if the output is JSON, print its top-level keys or array
                  length and the first few items, not the body (--tail is
                  ignored for that summary)
  --allow-write   skip the safety check (only for commands the user approved)

Commands:
  Each command after a "--" is one command. Pass it as a single quoted string
  (that keeps pipes and any "--" inside it). If a command is given as several
  words they are quoted and joined. In a FILE, put one command per line; blank
  lines and lines starting with "#" are ignored. A command may start with
  "LABEL=>" (label made of letters, digits, "_", ".", ":" or "-") to set the
  header label.

  Commands run through /bin/sh -c on POSIX and cmd /c on Windows, with stdin
  closed. Standard output and standard error are merged.

Output:
  For each command, in the order given:
    == [i] label (exit N, 1.2s) ==
  then the output with ANSI escapes removed, trailing spaces trimmed, runs of
  blank lines squeezed to one, and leading or trailing blank lines dropped.
  When output is cut, a line "... N more lines omitted" shows how much.
  A timed-out command shows TIMEOUT in place of the exit code.

Exit status:
  0 whenever the commands were run, whatever their own exit codes. Read
  failures from the headers. 2 for a usage error or a refused command.

Safety:
  Before running anything, every command is checked against obvious mutating
  patterns (rm, mv, dd, chmod, sudo, git push, gh pr create, docker rm, sed -i,
  tee, curl -X POST, output redirection to a file and so on). If any command
  matches, nothing runs. This is a guard against accidents, not a sandbox: a
  determined or obfuscated command can get past it. --allow-write turns the
  check off and is meant only for cases the user has approved.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

PROG = "batch.py"
IS_WINDOWS = os.name == "nt"
LABEL_RE = re.compile(r"^([\w.:-]+)=>(.*)$", re.DOTALL)
ANSI_RE = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]"  # CSI
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC
    r"|\x1b[PX^_][^\x1b]*\x1b\\"  # DCS, SOS, PM, APC
    r"|\x1b[@-Z\\-_]"  # other two-byte escapes
)
LABEL_WIDTH = 60
KILL_GRACE = 5.0


class UsageError(Exception):
    """Bad arguments: print the message and exit 2."""


@dataclass
class Job:
    label: str
    command: str


@dataclass
class Result:
    status: str  # "exit N", "TIMEOUT" or "error: ..."
    elapsed: float
    output: str


# ---------------------------------------------------------------- safety check

SEP = "sep"
WORD = "word"
REDIR = "redir"

# Commands refused wherever they sit in a command position.
BAD_COMMANDS = {
    "rm", "rmdir", "mv", "dd", "shred", "truncate", "chmod", "chown",
    "chgrp", "sudo", "doas", "su", "tee",
    "cp", "touch", "mkdir", "ln", "install", "unlink", "mkfifo", "mknod",
    "rsync", "patch", "kill", "killall", "pkill", "eval", "source", ".",
    # Windows cmd
    "del", "erase", "rd", "move", "ren", "rename", "format",
}
# Wrappers whose arguments contain another command.
WRAPPERS = {
    "env", "nohup", "time", "nice", "ionice", "command", "builtin", "exec",
    "timeout", "stdbuf", "watch", "xargs", "parallel", "strace", "ltrace",
    "chroot", "unbuffer",
}
SHELL_KEYWORDS = {
    "if", "then", "else", "elif", "fi", "do", "done", "while", "until",
    "for", "case", "esac", "!", "{", "}", "[[", "]]",
}
SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "fish", "ash"}
GIT_BAD = {
    "push", "commit", "reset", "checkout", "merge", "rebase", "clean",
    "rm", "mv", "restore", "switch", "pull", "cherry-pick", "revert", "am",
    "add", "stash", "apply", "init", "clone", "gc", "prune", "update-ref",
}
GIT_OPTS_WITH_ARG = {"-C", "-c", "--git-dir", "--work-tree", "--namespace",
                     "--exec-path", "--config-env"}
GH_BAD = {"create", "merge", "close", "delete", "edit", "comment", "reopen",
          "ready", "review", "transfer", "archive", "rename", "sync", "fork",
          "upload"}
GH_OPTS_WITH_ARG = {"-R", "--repo", "--hostname"}
WRAPPER_OPTS_WITH_ARG = {"-I", "-n", "-P", "-d", "-L", "-a", "-E", "-s",
                         "-k", "-u", "-g", "-o", "-e", "-c", "-i", "-C",
                         "--signal", "--kill-after", "--interval", "--chdir",
                         "--unset", "--max-args", "--max-procs"}
ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
DURATION_RE = re.compile(r"^\d+(?:\.\d+)?[smhd]?$")
DOCKER_BAD = {"rm", "rmi", "prune", "stop", "kill", "restart", "start", "run",
              "exec"}
DOCKER_OPTS_WITH_ARG = {"-H", "--host", "--context", "-c", "--config",
                        "-l", "--log-level", "-f", "--file", "-p",
                        "--project-name", "--project-directory", "--env-file"}
CURL_BAD_METHODS = {"POST", "PUT", "DELETE", "PATCH"}
CURL_DATA_FLAGS = {"-d", "--data", "--data-raw", "--data-binary",
                   "--data-urlencode", "--json", "-F", "--form", "-T",
                   "--upload-file"}
WRITE_REDIRS = {">", ">>", ">|", "&>", "&>>", "<>"}
SAFE_TARGETS = {"/dev/null", "/dev/stdout", "/dev/stderr", "nul", "NUL"}


def _read_balanced(text: str, i: int) -> tuple[str, int]:
    """Text inside "(...)" starting just after the "(" at i-1; returns end index."""
    depth = 1
    start = i
    while i < len(text):
        c = text[i]
        if c == "\\":
            i += 2
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return text[start:i], i + 1
        i += 1
    raise ValueError("unbalanced $(")


def lex(cmd: str) -> tuple[list[tuple[str, str]], list[str]]:
    """Split a shell command into word, separator and redirection tokens.

    Returns the tokens and any command substitutions found inside double
    quotes, which the caller checks separately.
    """
    tokens: list[tuple[str, str]] = []
    subs: list[str] = []
    word: list[str] = []
    in_word = False
    quoted = False
    i = 0
    n = len(cmd)

    def end_word() -> None:
        nonlocal word, in_word, quoted
        if in_word:
            tokens.append((WORD, "".join(word)))
        word, in_word, quoted = [], False, False

    while i < n:
        c = cmd[i]
        if c in " \t":
            end_word()
            i += 1
        elif c == "\\" and not IS_WINDOWS:
            if i + 1 < n and cmd[i + 1] == "\n":
                i += 2
                continue
            word.append(cmd[i + 1] if i + 1 < n else "")
            in_word = quoted = True
            i += 2
        elif c == "'":
            j = cmd.find("'", i + 1)
            if j < 0:
                raise ValueError("unbalanced single quote")
            word.append(cmd[i + 1:j])
            in_word = quoted = True
            i = j + 1
        elif c == '"':
            i += 1
            in_word = quoted = True
            while True:
                if i >= n:
                    raise ValueError("unbalanced double quote")
                d = cmd[i]
                if d == '"':
                    i += 1
                    break
                if d == "\\" and not IS_WINDOWS and i + 1 < n:
                    word.append(cmd[i + 1])
                    i += 2
                    continue
                if d == "$" and cmd.startswith("$(", i):
                    inner, i = _read_balanced(cmd, i + 2)
                    subs.append(inner)
                    word.append("$(...)")
                    continue
                if d == "`":
                    j = cmd.find("`", i + 1)
                    if j < 0:
                        raise ValueError("unbalanced backtick")
                    subs.append(cmd[i + 1:j])
                    word.append("`...`")
                    i = j + 1
                    continue
                word.append(d)
                i += 1
        elif c == "#" and not in_word:
            j = cmd.find("\n", i)
            i = n if j < 0 else j
        elif c in "\n;|()`":
            end_word()
            if cmd.startswith("||", i) or cmd.startswith("|&", i):
                i += 2
            else:
                i += 1
            tokens.append((SEP, c))
        elif c == "$" and cmd.startswith("$(", i):
            end_word()
            tokens.append((SEP, "$("))
            i += 2
        elif c == "&":
            if cmd.startswith("&>>", i):
                end_word()
                tokens.append((REDIR, "&>>"))
                i += 3
            elif cmd.startswith("&>", i):
                end_word()
                tokens.append((REDIR, "&>"))
                i += 2
            else:
                end_word()
                i += 2 if cmd.startswith("&&", i) else 1
                tokens.append((SEP, "&"))
        elif c in "<>":
            # A bare number just before the operator is a file descriptor.
            if in_word and not quoted and "".join(word).isdigit():
                word, in_word = [], False
            end_word()
            for op in (">>", ">&", ">|", "<<<", "<<", "<&", "<>", ">", "<"):
                if cmd.startswith(op, i):
                    break
            tokens.append((REDIR, op))
            i += len(op)
        else:
            word.append(c)
            in_word = True
            i += 1
    end_word()
    return tokens, subs


def _base(word: str) -> str:
    name = re.split(r"[\\/]", word)[-1].lower()
    for ext in (".exe", ".cmd", ".bat"):
        name = name.removesuffix(ext)
    return name


def _positional(words: list[str], opts_with_arg: set[str]) -> list[str]:
    """Words that are not options, skipping the values of known options."""
    out = []
    i = 0
    while i < len(words):
        w = words[i]
        if w in opts_with_arg:
            i += 2
            continue
        if not w.startswith("-"):
            out.append(w)
        i += 1
    return out


def _check_simple(words: list[str]) -> str | None:
    """Reason a simple command is mutating, or None."""
    i = 0
    while i < len(words):
        w = words[i]
        if w in SHELL_KEYWORDS or ASSIGN_RE.match(w):
            i += 1
            continue
        break
    words = words[i:]
    if not words:
        return None
    name = _base(words[0])
    args = words[1:]

    if name in BAD_COMMANDS or name.startswith("mkfs"):
        return f"'{name}' changes files or privileges"
    if name in WRAPPERS:
        # Skip the wrapper's own options and values, then check what it runs.
        j = 1
        while j < len(words):
            w = words[j]
            if w in WRAPPER_OPTS_WITH_ARG:
                j += 2
            elif w.startswith("-") or ASSIGN_RE.match(w) or DURATION_RE.match(w):
                j += 1
            else:
                break
        return _check_simple(words[j:])
    if name in SHELLS or name in {"cmd", "powershell", "pwsh"}:
        for j, a in enumerate(args):
            if re.fullmatch(r"-[a-z]*c|/[cCkK]|-[Cc]ommand", a) and j + 1 < len(args):
                return check_command(args[j + 1])
        return None
    if name == "find":
        for j, a in enumerate(args):
            if a == "-delete":
                return "'find -delete' removes files"
            if a in {"-exec", "-execdir", "-ok", "-okdir"}:
                reason = _check_simple(args[j + 1:])
                if reason:
                    return reason
        return None
    if name == "git":
        pos = _positional(args, GIT_OPTS_WITH_ARG)
        if pos and pos[0] in GIT_BAD:
            return f"'git {pos[0]}' changes the repository"
        return None
    if name == "gh":
        pos = _positional(args, GH_OPTS_WITH_ARG)
        if len(pos) > 1 and pos[1] in GH_BAD:
            return f"'gh {pos[0]} {pos[1]}' changes remote state"
        if args and args[0] == "api":
            for j, a in enumerate(args):
                m = re.match(r"^(?:-X|--method)=?(.*)$", a)
                if m:
                    method = m.group(1) or (args[j + 1] if j + 1 < len(args) else "")
                    if method.upper() != "GET":
                        return f"'gh api' with method {method or '?'}"
                if a in {"-f", "-F", "--field", "--raw-field", "--input"}:
                    return f"'gh api {a}' sends data"
        return None
    if name in {"docker", "podman"}:
        for a in _positional(args, DOCKER_OPTS_WITH_ARG)[:3]:
            if a in DOCKER_BAD:
                return f"'{name} ... {a}' removes or stops things"
        return None
    if name == "curl":
        for j, a in enumerate(args):
            method = None
            if a in {"-X", "--request"}:
                method = args[j + 1] if j + 1 < len(args) else ""
            elif a.startswith("--request="):
                method = a.split("=", 1)[1]
            elif a.startswith("-X") and len(a) > 2:
                method = a[2:]
            if method is not None and method.upper() in CURL_BAD_METHODS:
                return f"'curl -X {method.upper()}' changes remote state"
            if a in CURL_DATA_FLAGS or any(a.startswith(f + "=") for f in CURL_DATA_FLAGS
                                           if f.startswith("--")):
                return f"'curl {a}' sends data"
        return None
    if name in {"sed", "gsed", "perl"}:
        # Short option clusters that can carry -i before any value option.
        cluster = r"^-[nErsuz]*i" if name != "perl" else r"^-[nplaws0-9]*i"
        for a in args:
            if a == "--in-place" or a.startswith("--in-place="):
                return f"'{name} --in-place' edits files"
            if re.match(cluster, a):
                return f"'{name} -i' edits files"
        return None
    return None


def check_command(cmd: str) -> str | None:
    """Reason a command looks mutating, or None if it looks read-only."""
    try:
        tokens, subs = lex(cmd)
    except ValueError as exc:
        return f"could not parse ({exc})"
    words: list[str] = []
    segments: list[list[str]] = []
    k = 0
    while k < len(tokens):
        kind, value = tokens[k]
        if kind == SEP:
            segments.append(words)
            words = []
        elif kind == REDIR:
            target = tokens[k + 1][1] if k + 1 < len(tokens) and tokens[k + 1][0] == WORD else None
            if value in WRITE_REDIRS and target not in SAFE_TARGETS:
                return f"output redirection '{value}' to {target or 'a file'}"
            if value == ">&" and target is not None and not re.fullmatch(r"\d+|-", target) \
                    and target not in SAFE_TARGETS:
                return f"output redirection '>&' to {target}"
            if value == ">&" and target is None:
                return "output redirection '>&'"
            if target is not None:
                k += 1  # the target is not a command word
        else:
            words.append(value)
        k += 1
    segments.append(words)
    for seg in segments:
        reason = _check_simple(seg)
        if reason:
            return reason
    for sub in subs:
        reason = check_command(sub)
        if reason:
            return reason
    return None


# ---------------------------------------------------------------- arguments

def _parse_job(text: str) -> Job:
    m = LABEL_RE.match(text.strip())
    if m and m.group(2).strip():
        return Job(m.group(1), m.group(2).strip())
    command = text.strip()
    label = " ".join(command.split())
    if len(label) > LABEL_WIDTH:
        label = label[: LABEL_WIDTH - 3] + "..."
    return Job(label, command)


def _join(words: list[str]) -> str:
    if len(words) == 1:
        return words[0]
    return subprocess.list2cmdline(words) if IS_WINDOWS else shlex.join(words)


def _read_file(path: str) -> list[Job]:
    try:
        if path == "-":
            text = sys.stdin.read()
        else:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
    except OSError as exc:
        raise UsageError(f"cannot read {path}: {exc.strerror or exc}") from None
    jobs = []
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        jobs.append(_parse_job(line))
    return jobs


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):  # type: ignore[override]
        raise UsageError(message)


def _positive(kind):
    def conv(text: str):
        try:
            value = kind(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"not a number: {text}") from None
        if value <= 0:
            raise argparse.ArgumentTypeError(f"must be above zero: {text}")
        return value
    return conv


def parse_args(argv: list[str]) -> tuple[argparse.Namespace, list[Job]]:
    if "--" in argv:
        cut = argv.index("--")
        opts, rest = argv[:cut], argv[cut + 1:]
        has_sep = True
    else:
        opts, rest, has_sep = argv, [], False

    p = _Parser(prog=PROG, description="Run read-only commands in parallel.",
                add_help=True)
    p.add_argument("--max-lines", type=_positive(int), default=20)
    p.add_argument("--max-chars", type=_positive(int), default=2000)
    p.add_argument("--timeout", type=_positive(float), default=60.0)
    p.add_argument("--jobs", type=_positive(int), default=8)
    p.add_argument("--cwd")
    p.add_argument("--tail", action="store_true")
    p.add_argument("--json-keys", action="store_true")
    p.add_argument("--allow-write", action="store_true")
    p.add_argument("-f", "--file", dest="file")
    args = p.parse_args(opts)

    jobs: list[Job] = []
    if has_sep:
        groups: list[list[str]] = [[]]
        for tok in rest:
            if tok == "--":
                groups.append([])
            else:
                groups[-1].append(tok)
        for g in groups:
            if not g or not _join(g).strip():
                raise UsageError("empty command between '--' separators")
            jobs.append(_parse_job(_join(g)))
    if args.file and jobs:
        raise UsageError("use either -f FILE or '--' commands, not both")
    if args.file:
        jobs = _read_file(args.file)
    if not jobs:
        raise UsageError("no commands given")
    if args.cwd is not None and not os.path.isdir(args.cwd):
        raise UsageError(f"--cwd is not a directory: {args.cwd}")
    return args, jobs


# ---------------------------------------------------------------- running

def _kill_tree(proc: subprocess.Popen) -> None:
    try:
        if IS_WINDOWS:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           check=False)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass
    try:
        proc.kill()
    except OSError:
        pass


def run_one(command: str, timeout: float, cwd: str | None) -> Result:
    start = time.monotonic()
    kwargs: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "cwd": cwd,
    }
    if IS_WINDOWS:
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        argv: str | list[str] = command
        kwargs["shell"] = True  # runs COMSPEC /c
    else:
        kwargs["start_new_session"] = True
        argv = ["/bin/sh", "-c", command]
    try:
        proc = subprocess.Popen(argv, **kwargs)
    except OSError as exc:
        return Result(f"error: {exc.strerror or exc}", time.monotonic() - start, "")
    try:
        out, _ = proc.communicate(timeout=timeout)
        status = f"exit {proc.returncode}"
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        try:
            out, _ = proc.communicate(timeout=KILL_GRACE)
        except subprocess.TimeoutExpired:
            # Something escaped the process group and holds the pipe.
            if proc.stdout:
                proc.stdout.close()
            proc.wait()
            out = b""
        status = "TIMEOUT"
    text = (out or b"").decode("utf-8", errors="replace")
    return Result(status, time.monotonic() - start, text)


# ---------------------------------------------------------------- formatting

def clean(text: str) -> list[str]:
    """Strip ANSI, resolve carriage returns, squeeze blank runs to one."""
    text = ANSI_RE.sub("", text).replace("\r\n", "\n")
    lines: list[str] = []
    for raw in text.split("\n"):
        line = raw.rsplit("\r", 1)[-1] if "\r" in raw else raw
        line = line.rstrip()
        if not line and (not lines or not lines[-1]):
            continue
        lines.append(line)
    while lines and not lines[-1]:
        lines.pop()
    return lines


def _short(value: object, width: int = 80) -> str:
    if isinstance(value, dict):
        return f"{{object, {len(value)} keys}}"
    if isinstance(value, list):
        return f"[array, {len(value)} items]"
    text = json.dumps(value, ensure_ascii=False)
    return text if len(text) <= width else text[: width - 3] + "..."


def json_summary(text: str) -> list[str] | None:
    """Shape of a JSON document, or None if the text is not JSON."""
    stripped = text.strip()
    if not stripped or stripped[0] not in "[{":
        return None
    try:
        data = json.loads(stripped)
    except ValueError:
        return None
    if isinstance(data, dict):
        out = [f"{{object, {len(data)} keys}}"]
        out += [f"{k}: {_short(v)}" for k, v in data.items()]
        return out
    out = [f"[array, {len(data)} items]"]
    for item in data[:3]:
        if isinstance(item, dict):
            line = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
            if len(line) > 200:
                line = "{keys: " + ", ".join(list(item)[:12]) + "}"
        else:
            line = _short(item, 200)
        out.append(f"- {line}")
    if len(data) > 3:
        out.append(f"- ... {len(data) - 3} more items")
    return out


def trim(lines: list[str], max_lines: int, max_chars: int, tail: bool) -> list[str]:
    total = len(lines)
    kept = lines[-max_lines:] if tail else lines[:max_lines]
    if tail:
        kept = list(reversed(kept))
    out: list[str] = []
    used = 0
    cut_chars = False
    for line in kept:
        cost = len(line) + (1 if out else 0)
        if used + cost > max_chars:
            room = max_chars - used - (1 if out else 0)
            if not out and room > 0:
                # A single huge line: keep part of it.
                piece = line[-room:] if tail else line[:room]
                out.append(piece)
                cut_chars = True
            break
        out.append(line)
        used += cost
    if tail:
        out.reverse()
    hidden = total - len(out)
    if hidden <= 0 and not cut_chars:
        return out
    if hidden <= 0:
        note = f"... line cut at {max_chars} chars"
    else:
        note = f"... {hidden} more lines omitted"
        if cut_chars:
            note += f" (one line cut at {max_chars} chars)"
    return [note] + out if tail else out + [note]


def format_result(i: int, job: Job, res: Result, args: argparse.Namespace) -> str:
    header = f"== [{i}] {job.label} ({res.status}, {res.elapsed:.1f}s) =="
    body = clean(res.output)
    tail = args.tail
    if args.json_keys:
        summary = json_summary("\n".join(body))
        if summary is not None:
            body, tail = summary, False  # a shape reads top down
    body = trim(body, args.max_lines, args.max_chars, tail)
    if not body:
        body = ["(no output)"]
    return "\n".join([header, *body])


# ---------------------------------------------------------------- main

def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        args, jobs = parse_args(argv)
    except UsageError as exc:
        print(f"{PROG}: {exc}", file=sys.stderr)
        print(f"usage: {PROG} [options] -- CMD1 -- CMD2 ...  |  {PROG} [options] -f FILE",
              file=sys.stderr)
        return 2
    except SystemExit as exc:  # --help
        return 0 if exc.code in (0, None) else 2

    if not args.allow_write:
        refused = []
        for i, job in enumerate(jobs, 1):
            reason = check_command(job.command)
            if reason:
                refused.append(f"  [{i}] {job.command}\n      {reason}")
        if refused:
            print(f"{PROG}: refused, these look like they change something:",
                  file=sys.stderr)
            print("\n".join(refused), file=sys.stderr)
            print("Nothing was run. This helper is for read-only commands. Use "
                  "--allow-write only if the user approved these.", file=sys.stderr)
            return 2

    try:
        sys.stdout.reconfigure(errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

    with ThreadPoolExecutor(max_workers=min(args.jobs, len(jobs))) as pool:
        futures = [pool.submit(run_one, j.command, args.timeout, args.cwd) for j in jobs]
        try:
            for i, (job, fut) in enumerate(zip(jobs, futures), 1):
                print(format_result(i, job, fut.result(), args), flush=True)
        except KeyboardInterrupt:
            for fut in futures:
                fut.cancel()
            return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
