"""Per-machine settings overrides.

dot-claude/settings.json is shared by every machine. A machine can layer
machines/<name>.settings.json on top of it:

  apply:   local settings = shared * override (deep merge: objects merge key
           by key, arrays and scalars replace, and a null value deletes the
           key).
  capture: local settings are split back into shared + override. Every leaf
           the override mentions is owned by this machine: its shared value
           stays as it was, and a local change to it goes back into the
           override instead. Everything else flows to the shared file.

A "leaf" is a path the override reaches without descending into a non-object
or an empty object, so {"enabledPlugins": {"x@y": true}} owns only
enabledPlugins["x@y"]. The root is never a leaf: an empty override owns
nothing.

The path helpers below copy jq's getpath/setpath/delpaths, including where jq
raises (indexing into a number, string or array with a key).
"""

from __future__ import annotations

import copy
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from claude_sync.syncfiles import TMP_SUFFIX
from claude_sync.util import Fail, check_real_dir

PathT = tuple[str | int, ...]

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_INT = re.compile(r"-?[0-9]+")
_PATH_EXAMPLE = """(example: '.enabledPlugins["name@marketplace"]')"""


# --- JSON I/O ----------------------------------------------------------------


def dump_json(obj: Any) -> bytes:
    """2-space indent, non-ASCII kept as is, LF line ends, trailing newline."""
    return (json.dumps(obj, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _no_constant(name: str) -> Any:
    raise ValueError(f"{name} is not valid JSON")


def load_json(path: Path, *, require_object: bool = True) -> Any:
    """Read a JSON file (a UTF-8 BOM is allowed, as jq allows it)."""
    path = Path(path)
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        raise Fail(f"{path} not found.", hint="Check the file exists, then try again.") from None
    try:
        obj = json.loads(data.decode("utf-8-sig"), parse_constant=_no_constant)
    except (UnicodeDecodeError, ValueError) as e:
        raise Fail(
            f"{path} is not valid JSON.",
            hint=f"{e}. Fix or restore the file, then try again.",
        ) from None
    if require_object and not isinstance(obj, dict):
        raise Fail(
            f"{path} must contain a JSON object.",
            hint="Its top level must be {...}. Fix or restore the file, then try again.",
        )
    return obj


def _write(path: Path, data: bytes) -> None:
    """Write bytes through a temp file and os.replace, so a kill never leaves half a file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=TMP_SUFFIX, dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        if path.exists():
            os.chmod(tmp_name, path.stat().st_mode & 0o7777)
        else:
            os.chmod(tmp_name, 0o644)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


# --- jq-like path primitives -------------------------------------------------


class _PathError(Exception):
    pass


def _json_eq(a: Any, b: Any) -> bool:
    """jq equality: like ==, but true is not 1 (1 and 1.0 stay equal)."""
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(_json_eq(a[k], b[k]) for k in a)
    if isinstance(a, list):
        return len(a) == len(b) and all(_json_eq(x, y) for x, y in zip(a, b))
    return a == b


def _index(node: Any, key: str | int) -> Any:
    """One step of jq getpath: null for a missing key or a null node."""
    if node is None:
        return None
    if isinstance(key, str):
        if isinstance(node, dict):
            return node.get(key)
        raise _PathError(f"Cannot index {type(node).__name__} with string {key!r}")
    if isinstance(node, list):
        return node[key] if -len(node) <= key < len(node) else None
    raise _PathError(f"Cannot index {type(node).__name__} with number")


def _getpath(obj: Any, path: PathT) -> Any:
    for key in path:
        obj = _index(obj, key)
    return obj


def has_path(obj: Any, path: PathT) -> bool:
    """True when the parent of `path` is an object holding its last key. Never raises."""
    if not path or not isinstance(path[-1], str):
        return False
    try:
        parent = _getpath(obj, path[:-1])
    except _PathError:
        return False
    return isinstance(parent, dict) and path[-1] in parent


def _setpath(obj: Any, path: PathT, value: Any) -> Any:
    """jq setpath on string keys: creates missing objects, raises through a non-object."""
    if not path:
        return value
    key = path[0]
    if obj is None:
        obj = {}
    if not isinstance(obj, dict) or not isinstance(key, str):
        raise _PathError(f"Cannot index {type(obj).__name__} with {key!r}")
    obj[key] = _setpath(obj.get(key), path[1:], value)
    return obj


def _delpath(obj: Any, path: PathT) -> None:
    """jq delpaths for one path of string keys: a missing or null step is a no-op."""
    node = obj
    for key in path[:-1]:
        node = _index(node, key)
        if node is None:
            return
    if node is None:
        return
    if not isinstance(node, dict):
        raise _PathError(f"Cannot delete fields from {type(node).__name__}")
    node.pop(path[-1], None)


def leaf_paths(obj: Any) -> list[PathT]:
    """Paths an override owns, in key order (see the module docstring)."""
    out: list[PathT] = []

    def walk(node: Any, prefix: PathT) -> None:
        if isinstance(node, dict) and node:
            for k, v in node.items():
                walk(v, prefix + (k,))
        elif prefix:
            out.append(prefix)

    walk(obj, ())
    return out


def parse_path(expr: str) -> PathT:
    """Parse the jq path syntax the CLI documents: `.a.b`, `.a["x@y"]`, `.["k"]`, `.a[0]`.

    `.` alone is the root, (). Anything else (pipes, `[]`, `?`, functions)
    raises Fail. Bracket strings take JSON escapes.
    """
    bad = Fail(f"Not a valid jq path: {expr}", hint=_PATH_EXAMPLE)
    s = expr.strip()
    if s == ".":
        return ()
    if not s.startswith("."):
        raise bad
    out: list[str | int] = []
    i, n = 0, len(s)
    while i < n:
        if s[i] == ".":
            i += 1
            m = _IDENT.match(s, i)
            if m:
                out.append(m.group(0))
                i = m.end()
                continue
            if i < n and s[i] == "[":
                continue  # `.["k"]`
            raise bad
        if s[i] == "[":
            i += 1
            if i < n and s[i] == '"':
                j = i + 1
                while j < n and s[j] != '"':
                    j += 2 if s[j] == "\\" else 1
                if j >= n:
                    raise bad
                try:
                    out.append(json.loads(s[i : j + 1]))
                except ValueError:
                    raise bad from None
                i = j + 1
            else:
                m = _INT.match(s, i)
                if not m:
                    raise bad
                out.append(int(m.group(0)))
                i = m.end()
            if i >= n or s[i] != "]":
                raise bad
            i += 1
            continue
        raise bad
    return tuple(out)


# --- merge and split ---------------------------------------------------------


def _require_object(obj: Any, what: str) -> None:
    if not isinstance(obj, dict):
        raise Fail(
            f"{what} must contain a JSON object.",
            hint="Its top level must be {...}. Fix or restore the file, then try again.",
        )


def _deep_merge(a: Any, b: Any) -> Any:
    """jq `a * b`: objects merge key by key, anything else is replaced by b."""
    if isinstance(a, dict) and isinstance(b, dict):
        out = dict(a)
        for k, v in b.items():
            out[k] = _deep_merge(a[k], v) if k in a else v
        return out
    return b


def overlay_merge(shared: dict, override: dict) -> dict:
    """shared * override, then every null leaf of override is deleted. Inputs are not changed."""
    _require_object(shared, "The shared settings")
    _require_object(override, "The settings override")
    merged = copy.deepcopy(_deep_merge(shared, override))
    for p in leaf_paths(override):
        if _getpath(override, p) is None:
            _delpath(merged, p)
    return merged


def overlay_split(
    local: dict, shared_before: dict, override_before: dict, override: dict | None
) -> tuple[dict, dict]:
    """Split local settings into (shared, override). Inputs are not changed.

    shared_before    the shared settings as last applied (pre-capture)
    override_before  the override the last apply layered on ({} if none)
    override         the override as it is now (None when the file is missing)

    A local change is judged against what the last apply produced, not
    against `override`, so a key hand-added to the override (not applied
    yet) is not taken for a local deletion, and a key hand-removed from it
    does not leak its old value into the shared file.
    """
    ov_now = {} if override is None else override
    for obj, what in (
        (local, "The local settings"),
        (shared_before, "The shared settings"),
        (override_before, "The applied settings override"),
        (ov_now, "The settings override"),
    ):
        _require_object(obj, what)
    shared = shared_before
    base = override_before
    new_shared = copy.deepcopy(local)
    new_ov = copy.deepcopy(ov_now)

    def applied(p: PathT) -> tuple[bool, Any]:
        if has_path(base, p):
            v = _getpath(base, p)
            return (False, None) if v is None else (True, v)
        if has_path(shared, p):
            return True, _getpath(shared, p)
        return False, None

    try:
        for p in sorted(set(leaf_paths(ov_now)) | set(leaf_paths(base))):
            present = has_path(local, p)
            lv = _getpath(local, p)
            a_present, a_v = applied(p)
            unchanged = present == a_present and (not present or _json_eq(lv, a_v))
            # Owned now and edited locally since the last apply: keep the
            # edit, machine-side.
            if has_path(ov_now, p) and not unchanged:
                new_ov = _setpath(new_ov, p, copy.deepcopy(lv) if present else None)
            # Owned now or before: the shared value stays as it was.
            if has_path(shared, p):
                new_shared = _setpath(new_shared, p, copy.deepcopy(_getpath(shared, p)))
            else:
                _delpath(new_shared, p)
            # Deleting a nested leaf can leave its parents behind as empty
            # objects; drop any the shared file never had.
            for i in range(len(p) - 1, 0, -1):
                q = p[:i]
                if (
                    has_path(new_shared, q)
                    and _getpath(new_shared, q) == {}
                    and not has_path(shared, q)
                ):
                    _delpath(new_shared, q)
    except _PathError as e:
        raise Fail(
            f"Could not split the local settings into shared and machine parts: {e}.",
            hint="A key the override owns sits under a value that is not an object "
            "in the local settings.json. Fix that value, then try again.",
        ) from None
    return new_shared, new_ov


def merge_files(shared_file: Path, override_file: Path) -> bytes:
    """overlay_merge of two files, as the bytes to write for the local settings."""
    override = load_json(override_file)
    shared = load_json(shared_file)
    return dump_json(overlay_merge(shared, override))


def split_files(
    local_file: Path,
    shared_before_file: Path,
    override_before_file: Path,
    override_file: Path,
    shared_out_file: Path,
) -> None:
    """The bash overlay_split, same five arguments in the same order.

    Writes the shared part of local_file to shared_out_file. override_file is
    rewritten only when its content changed, and created only when missing
    and the new override is not empty, so a no-op sync neither reformats a
    hand-written file nor creates an empty one. On failure nothing is written.
    """
    override_file = Path(override_file)
    existed = override_file.is_file()
    ov_now = load_json(override_file) if existed else None
    new_shared, new_ov = overlay_split(
        load_json(local_file),
        load_json(shared_before_file),
        load_json(override_before_file),
        ov_now,
    )
    check_override_path(override_file)  # before any write, so a refusal writes nothing
    _write(Path(shared_out_file), dump_json(new_shared))
    if existed:
        if not _json_eq(ov_now, new_ov):
            _write(override_file, dump_json(new_ov))
    elif new_ov:
        _write(override_file, dump_json(new_ov))


# --- override add / remove / list --------------------------------------------


_NAME = re.compile(r"[A-Za-z0-9._-]+")  # profile.profile_valid_name (no import: cycle)


def overlay_path(repo_dir: Path, name: str) -> Path:
    """machines/<name>.settings.json (name is the hostname or profile).

    Every read and write of an override goes through here, so a name that
    could lead outside machines/, or an override (or machines/ folder) that
    is a link or resolves elsewhere, raises Fail: its target's bytes must
    never be merged into live settings, copied or pushed. A missing file
    is fine."""
    if not _NAME.fullmatch(name) or name in (".", ".."):
        raise Fail(
            f"'{name}' is not a valid profile name.",
            hint="Use only letters, digits, '.', '_' and '-' (not '.' or '..'),"
            " and fix PROFILE= in machines/<hostname>.conf or CLAUDE_SYNC_PROFILE.",
        )
    return check_override_path(Path(repo_dir) / "machines" / f"{name}.settings.json")


def check_override_path(path: Path) -> Path:
    """Raise Fail unless `path` (an override file) and its folder are real:
    the folder (machines/) must not be a link, whether or not the file
    exists yet, and the file, if it exists, must not be a link or resolve
    elsewhere. Called again right before each write or delete, so a link
    made after the path was built is still refused."""
    path = Path(path)
    machines = check_real_dir(path.parent)
    if os.path.lexists(path) and (
        path.is_symlink()
        or path.is_junction()
        or path.resolve() != machines.resolve() / path.name
    ):
        raise Fail(
            f"{path} is a link or leads outside {machines}.",
            hint="A settings override must be a real file in machines/, not a link."
            " Replace it with a real file (or delete it), then try again.",
        )
    return path


def overlay_add(repo_dir: Path, name: str, local_settings: Path, expr: str) -> None:
    """Make `expr` machine-specific: record its local value in the override
    and remove it from the shared settings, so other machines drop it."""
    override = overlay_path(repo_dir, name)
    shared_file = Path(repo_dir) / "dot-claude" / "settings.json"
    p = parse_path(expr)
    local = load_json(local_settings, require_object=False)
    if not has_path(local, p):
        raise Fail(
            f"{expr} isn't set in {local_settings}; set it locally first.",
            hint="Set the value in your local settings.json, then run override add again.",
        )
    ov = load_json(override) if override.is_file() else {}
    shared = load_json(shared_file)
    try:
        ov = _setpath(ov, p, copy.deepcopy(_getpath(local, p)))
        _delpath(shared, p)
    except _PathError as e:
        raise Fail(
            f"Could not move {expr} into {override}: {e}.",
            hint="A parent of that key in the override or shared settings is not an object; fix it first.",
        ) from None
    check_override_path(override)  # again: it may have changed since overlay_path
    _write(override, dump_json(ov))
    _write(shared_file, dump_json(shared))


def overlay_remove(repo_dir: Path, name: str, expr: str) -> None:
    """Stop overriding `expr`: it takes the shared value (or disappears) on the
    next apply. Deletes the override file when nothing is left in it."""
    override = overlay_path(repo_dir, name)
    if not override.is_file():
        raise Fail(f"No overrides for {name}.", hint="override list shows what is overridden.")
    p = parse_path(expr)
    ov = load_json(override)
    if not has_path(ov, p):
        raise Fail(f"{expr} isn't overridden on {name}.", hint="override list shows what is overridden.")
    _delpath(ov, p)
    for i in range(len(p) - 1, 0, -1):
        if _getpath(ov, p[:i]) == {}:
            _delpath(ov, p[:i])
    check_override_path(override)  # again: it may have changed since overlay_path
    if ov:
        _write(override, dump_json(ov))
    else:
        override.unlink()


def _show_key(key: str) -> str:
    # As bash: no escaping inside the brackets.
    return f".{key}" if _IDENT.fullmatch(key) else f'["{key}"]'


def overlay_list(repo_dir: Path, name: str) -> list[str]:
    """One "path = value" line per owned leaf, or a single "No overrides" line."""
    override = overlay_path(repo_dir, name)
    if not override.is_file():
        return [f"No overrides for {name}."]
    ov = load_json(override)
    return [
        "".join(_show_key(k) for k in p)
        + " = "
        + json.dumps(_getpath(ov, p), ensure_ascii=False, separators=(",", ":"))
        for p in leaf_paths(ov)
    ]
