"""dot-agents/skills/batch-run/batch.py: runs read-only commands in parallel.

The script is loaded by path. Most tests call main() in process and read the
printed summary; one runs it as a subprocess to check the real exit code.
Shell-based tests are POSIX only.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPT = (Path(__file__).resolve().parents[2]
          / "dot-agents" / "skills" / "batch-run" / "batch.py")
POSIX = pytest.mark.skipif(os.name != "posix", reason="uses /bin/sh syntax")

# Built up so the words never appear literally in this file.
RM = "r" + "m"


def _load():
    spec = importlib.util.spec_from_file_location("batch_run_for_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclasses look the module up
    spec.loader.exec_module(mod)
    return mod


batch = _load()


def _run(capsys, *argv: str) -> tuple[int, str, str]:
    code = batch.main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


@POSIX
def test_prints_in_given_order_and_runs_in_parallel(capsys):
    start = time.monotonic()
    code, out, _ = _run(
        capsys,
        "--", "sleep 1; echo slow",
        "--", "sleep 1; echo middle",
        "--", "echo fast",
    )
    elapsed = time.monotonic() - start
    assert code == 0
    assert out.index("[1]") < out.index("slow") < out.index("[2]") \
        < out.index("middle") < out.index("[3]") < out.index("fast")
    assert "== [1] sleep 1; echo slow (exit 0, " in out
    # Two one-second sleeps in parallel, with plenty of margin.
    assert elapsed < 1.9


@POSIX
def test_header_shows_failing_exit_code(capsys):
    code, out, _ = _run(capsys, "--", "echo oops >&2; exit 3")
    assert code == 0
    assert "(exit 3, " in out
    assert "oops" in out


@POSIX
def test_truncates_lines_head_and_tail(capsys):
    _, out, _ = _run(capsys, "--max-lines", "3", "--", "seq 1 10")
    body = out.splitlines()[1:]
    assert body == ["1", "2", "3", "... 7 more lines omitted"]

    _, out, _ = _run(capsys, "--max-lines", "3", "--tail", "--", "seq 1 10")
    body = out.splitlines()[1:]
    assert body == ["... 7 more lines omitted", "8", "9", "10"]


@POSIX
def test_truncates_by_chars(capsys):
    _, out, _ = _run(capsys, "--max-chars", "10", "--", "printf '%s\\n' abcdefghijklmnop q r")
    body = out.splitlines()[1:]
    assert body[0] == "abcdefghij"
    assert body[1].startswith("... 2 more lines omitted")


def test_trim_counts_omitted_lines():
    lines = [f"line{i}" for i in range(10)]
    assert batch.trim(lines, 20, 2000, False) == lines
    assert batch.trim(lines, 20, 17, False) == ["line0", "line1", "line2",
                                                "... 7 more lines omitted"]


def test_clean_strips_ansi_and_squeezes_blank_runs():
    text = "\x1b[31mred\x1b[0m  \n\n\n\nnext\r\nprogress 10%\rprogress 99%\n\n"
    assert batch.clean(text) == ["red", "", "next", "progress 99%"]


@POSIX
def test_json_keys_summarises_json(capsys):
    _, out, _ = _run(capsys, "--json-keys", "--",
                     """echo '{"name": "x", "items": [1, 2, 3], "meta": {"a": 1}}'""")
    body = out.splitlines()[1:]
    assert body == ['{object, 3 keys}', 'name: "x"', 'items: [array, 3 items]',
                    'meta: {object, 1 keys}']

    _, out, _ = _run(capsys, "--json-keys", "--", "echo '[1, 2, 3, 4, 5]'")
    body = out.splitlines()[1:]
    assert body == ["[array, 5 items]", "- 1", "- 2", "- 3", "- ... 2 more items"]


@POSIX
def test_json_keys_leaves_non_json_alone(capsys):
    _, out, _ = _run(capsys, "--json-keys", "--", "echo not json")
    assert out.splitlines()[1:] == ["not json"]


@POSIX
def test_timeout_kills_the_process_group(capsys):
    start = time.monotonic()
    code, out, _ = _run(capsys, "--timeout", "1", "--", "echo started; sleep 30; true")
    elapsed = time.monotonic() - start
    assert code == 0
    assert "(TIMEOUT, " in out
    assert "started" in out
    # If only the shell were killed, sleep would hold the pipe for 30s.
    assert elapsed < 10


@pytest.mark.parametrize("cmd", [
    f"{RM} -rf build",
    "ls; mv a b",
    "cat a | tee b",
    "echo x > out.txt",
    "echo x >> out.txt",
    "ls &> out.txt",
    "sed -i s/a/b/ f",
    "sed --in-place=.bak s/a/b/ f",
    "git push origin HEAD",
    "git -C /some/repo commit -m x",
    "gh pr create --fill",
    "gh issue comment 3 --body hi",
    f"docker {RM} web",
    "docker system prune -f",
    "curl -X POST https://example.invalid",
    "curl --request=DELETE https://example.invalid",
    "sudo ls",
    "chmod 600 f",
    "dd if=a of=b",
    f"find . -exec {RM} {{}} ;",
    f"ls | xargs -I{{}} {RM} {{}}",
    f'echo "$({RM} x)"',
    f'bash -c "{RM} x"',
    f"env FOO=1 {RM} x",
    "cp a b",
    "touch f",
    "mkdir d",
    "ln -s a b",
    "kill 123",
    "pkill foo",
    "eval ls",
    "source ./env.sh",
    ". ./env.sh",
    "git add .",
    "git stash",
    "gh pr ready 3",
    "docker restart web",
])
def test_check_refuses_mutating_commands(cmd):
    assert batch.check_command(cmd)


@pytest.mark.parametrize("cmd", [
    f'grep -rn "{RM} -rf" .',
    "awk '$1 > 5' data.txt",
    "jq '.a > 1' data.json",
    "git log --grep=commit",
    "git status --short",
    "git diff -- file",
    "ls 2>/dev/null",
    "ls 2>&1",
    "ls >/dev/null 2>&1",
    "grep '>' f",
    f"time grep -r {RM} .",
    "gh issue list --search edit",
    "gh pr view 3",
    "docker ps",
    "curl -s https://example.invalid",
    "sed -n 1,5p f",
    "echo hi",
])
def test_check_allows_read_only_commands(cmd):
    assert batch.check_command(cmd) is None


def test_refusal_runs_nothing(capsys, tmp_path):
    marker = tmp_path / "ran"
    code, out, err = _run(capsys, "--cwd", str(tmp_path),
                          "--", "touch ran", "--", "git push")
    assert code == 2
    assert out == ""
    assert "git push" in err
    assert "--allow-write" in err
    assert not marker.exists()


@POSIX
def test_allow_write_bypasses_the_check(capsys, tmp_path):
    code, out, _ = _run(capsys, "--allow-write", "--cwd", str(tmp_path),
                        "--", "echo hello > out.txt && cat out.txt")
    assert code == 0
    assert "(exit 0, " in out
    assert (tmp_path / "out.txt").read_text() == "hello\n"


@POSIX
def test_file_with_labels_comments_and_blank_lines(capsys, tmp_path):
    cmds = tmp_path / "cmds.txt"
    cmds.write_text(
        "# a comment\n"
        "\n"
        "greet=>echo hello\n"
        "   \n"
        "echo plain\n"
        "echo a=b\n",
        encoding="utf-8",
    )
    code, out, _ = _run(capsys, "-f", str(cmds))
    assert code == 0
    headers = [line for line in out.splitlines() if line.startswith("== ")]
    assert len(headers) == 3
    assert headers[0].startswith("== [1] greet (exit 0, ")
    assert headers[1].startswith("== [2] echo plain (exit 0, ")
    assert headers[2].startswith("== [3] echo a=b (exit 0, ")
    assert "hello" in out


def test_label_needs_a_simple_name():
    assert batch._parse_job("my.label=>git status").label == "my.label"
    job = batch._parse_job("""awk '{print "a=>b"}' f""")
    assert job.command == """awk '{print "a=>b"}' f"""


@pytest.mark.parametrize("argv", [
    [],
    ["--max-lines", "0", "--", "echo hi"],
    ["--jobs", "x", "--", "echo hi"],
    ["--", "echo hi", "--"],
    ["--cwd", "/no/such/dir/at/all", "--", "echo hi"],
    ["-f", "/no/such/file.txt"],
    ["--bogus", "--", "echo hi"],
])
def test_usage_errors_return_2(capsys, argv):
    code, out, err = _run(capsys, *argv)
    assert code == 2
    assert out == ""
    assert "usage:" in err


def test_file_and_commands_together_is_a_usage_error(capsys, tmp_path):
    cmds = tmp_path / "cmds.txt"
    cmds.write_text("echo hi\n", encoding="utf-8")
    code, _, err = _run(capsys, "-f", str(cmds), "--", "echo hi")
    assert code == 2
    assert "not both" in err


def test_real_exit_code_on_usage_error():
    proc = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True,
                          text=True, check=False)
    assert proc.returncode == 2
    assert "no commands given" in proc.stderr
