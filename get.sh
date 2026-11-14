#!/usr/bin/env bash
# get.sh: one-command setup of claude-config on Linux or macOS.
# Installs git, python3 and gh if needed, logs gh in, clones your
# private sync repo and runs its install.sh.
#
#   curl -fsSL URL/get.sh | bash -s -- OWNER/REPO [options]
#
# Under `curl | bash` stdin is this script, so every prompt and every
# child command reads from /dev/tty (or /dev/null without a terminal).
# Bash 3.2 compatible (stock macOS).
set -euo pipefail
set +x

GH_LINUX_URL="https://github.com/cli/cli/blob/trunk/docs/install_linux.md"

REPO=""
DIR=""
PROJECT_PATH=""
HAVE_PROJECT_PATH=0
PROFILE_NAME=""
HAVE_PROFILE=0
YES=0
DRY_RUN=0
SKIP_PY=0
TIER=""
OS=""
MGR=""
APT_UPDATED=0
SUDO=()

usage() {
  cat <<'EOF'
Usage: get.sh [OWNER/REPO] [--dir PATH] [--project-path PATH] [--profile NAME]
              [--tier basic|full] [--yes] [--dry-run] [--skip-python-deps] [--help]

Sets up claude-config on this machine in one go:
  1. checks the system      4. logs in to GitHub with gh
  2. git, python3 and gh    5. clones your private sync repo
  3. pip                    6. runs its install.sh

  OWNER/REPO          Your private sync repo. Else $CLAUDE_SYNC_REPO, else a prompt.
  --dir PATH          Where to clone it (default: $HOME/claude-config).
  --project-path PATH Passed to install.sh: your main Claude Code project folder.
  --profile NAME      Passed to install.sh: follow machines/NAME/.
  --tier basic|full   Passed to install.sh: what a new machine syncs (default full).
                      basic skips memory, the pip packages and ccusage; only gh is set up.
  --skip-python-deps  Passed to install.sh: skip the agent-memory plugin's pip packages.
  --yes               Accept every default answer here. install.sh still asks for
                      the project path on a terminal unless --project-path is given.
  --dry-run           Print the commands that change things instead of running them.
  --help              Show this help.

Example:
  curl -fsSL URL/get.sh | bash -s -- owner/name --yes
EOF
}

say() { printf '%s\n' "$*"; }
err() { printf 'get.sh: %s\n' "$*" >&2; }
# die "what failed" ["what to do" ...]: one line each, then exit 1.
die() {
  err "$1"
  shift
  local line
  for line in "$@"; do printf '  %s\n' "$line" >&2; done
  exit 1
}

# True when /dev/tty can be opened (a terminal is attached).
have_tty() { (exec </dev/tty) 2>/dev/null; }

# Runs "$@" with stdin from the terminal, else /dev/null, never this script.
run_in() {
  if have_tty; then
    "$@" </dev/tty
  else
    "$@" </dev/null
  fi
}

show_cmd() {
  local prefix="$1"
  shift
  printf '%s' "$prefix"
  printf ' %q' "$@"
  printf '\n'
}

# A command that changes something: printed, then run (or only printed).
run() {
  if [ "$DRY_RUN" -eq 1 ]; then
    show_cmd "[dry run] would run:" "$@"
    return 0
  fi
  show_cmd "+" "$@"
  run_in "$@"
}

# ask_yes "Question?" -> 0 for yes. Default yes; --yes accepts it.
ask_yes() {
  local question="$1" answer=""
  if [ "$YES" -eq 1 ]; then
    say "$question [Y/n] yes (--yes)"
    return 0
  fi
  if [ "$DRY_RUN" -eq 1 ]; then
    say "$question [Y/n] yes (dry run assumes the default)"
    return 0
  fi
  if ! have_tty; then
    die "No terminal to ask: $question" \
      "Re-run with --yes to accept the defaults (curl ... | bash -s -- OWNER/REPO --yes)."
  fi
  printf '%s [Y/n] ' "$question" >/dev/tty
  IFS= read -r answer </dev/tty || answer=""
  case "$answer" in
    "" | y | Y | yes | Yes | YES) return 0 ;;
    *) return 1 ;;
  esac
}

# A URL with any user:token@ part replaced, for printing.
redact_url() {
  local u="$1" scheme rest host
  case "$u" in
    *://*) ;;
    *)
      printf '%s' "$u"
      return 0
      ;;
  esac
  scheme="${u%%://*}"
  rest="${u#*://}"
  host="${rest%%/*}"
  case "$host" in
    *@*) printf '%s://***@%s%s' "$scheme" "${host##*@}" "${rest#"$host"}" ;;
    *) printf '%s' "$u" ;;
  esac
}

# First MAJOR.MINOR in the input, or nothing.
first_version() { grep -Eo '[0-9]+\.[0-9]+' | head -n 1 || true; }

# ver_ge "2.43" 2 40 -> 0 when the version is at least 2.40.
ver_ge() {
  local v="$1" want_major="$2" want_minor="$3" major minor
  [ -n "$v" ] || return 1
  major="${v%%.*}"
  minor="${v#*.}"
  minor="${minor%%.*}"
  [ "$major" -gt "$want_major" ] || { [ "$major" -eq "$want_major" ] && [ "$minor" -ge "$want_minor" ]; }
}

git_version() {
  command -v git >/dev/null 2>&1 || return 0
  git --version </dev/null 2>/dev/null | first_version
}

python_version() {
  command -v python3 >/dev/null 2>&1 || return 0
  python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])' </dev/null 2>/dev/null | first_version
}

parse_args() {
  while [ "$#" -gt 0 ]; do
    case "$1" in
      -h | --help)
        usage
        exit 0
        ;;
      --yes | -y) YES=1 ;;
      --dry-run) DRY_RUN=1 ;;
      --skip-python-deps) SKIP_PY=1 ;;
      --tier=*) TIER="${1#--tier=}" ;;
      --dir | --project-path | --profile | --tier)
        [ "$#" -ge 2 ] || die "$1 needs a value. See: get.sh --help"
        case "$1" in
          --dir) DIR="$2" ;;
          --project-path) PROJECT_PATH="$2" HAVE_PROJECT_PATH=1 ;;
          --profile) PROFILE_NAME="$2" HAVE_PROFILE=1 ;;
          --tier) TIER="$2" ;;
        esac
        shift
        ;;
      --dir=*) DIR="${1#--dir=}" ;;
      --project-path=*) PROJECT_PATH="${1#--project-path=}" HAVE_PROJECT_PATH=1 ;;
      --profile=*) PROFILE_NAME="${1#--profile=}" HAVE_PROFILE=1 ;;
      -*) die "Unknown option: $1. See: get.sh --help" ;;
      *)
        [ -z "$REPO" ] || die "Only one OWNER/REPO, got '$REPO' and '$1'. See: get.sh --help"
        REPO="$1"
        ;;
    esac
    shift
  done
}

resolve_repo() {
  if [ -z "$REPO" ]; then
    REPO="${CLAUDE_SYNC_REPO:-}"
  fi
  if [ -z "$REPO" ]; then
    have_tty || die "No repo given and no terminal to ask for one." \
      "Pass it as an argument (bash -s -- OWNER/REPO --yes) or set CLAUDE_SYNC_REPO=OWNER/REPO."
    printf 'Your private repo as OWNER/REPO: ' >/dev/tty
    IFS= read -r REPO </dev/tty || REPO=""
  fi
  case "$TIER" in
    "" | basic | full) ;;
    *) die "--tier must be basic or full, not '$TIER'." ;;
  esac
  case "$REPO" in
    "") die "A repo is required, as OWNER/REPO." ;;
    -* | */*/* | */ | /*) die "Not an OWNER/REPO name: '$REPO'. Give it as OWNER/REPO, not a URL." ;;
    */*) ;;
    *) die "Not an OWNER/REPO name: '$REPO'. Give it as OWNER/REPO, not a URL." ;;
  esac
  case "/$REPO/" in
    */./* | */../*) die "Not an OWNER/REPO name: '$REPO'." ;;
  esac
  if printf '%s' "$REPO" | grep -Eq '[^A-Za-z0-9._/-]'; then
    die "Not an OWNER/REPO name: '$REPO'. Use only letters, digits, '.', '_' and '-'."
  fi
}

# --- 1. system ---

detect_system() {
  say "[1/6] Checking the system"
  if [ -n "${VIRTUAL_ENV:-}" ]; then
    die "A Python virtualenv is active ($VIRTUAL_ENV)." \
      "The installer must use the system python3. Run 'deactivate' (or open a new shell), then re-run."
  fi
  case "$(uname -s)" in
    Linux) OS=linux ;;
    Darwin) OS=macos ;;
    *) die "This is for Linux and macOS. On Windows use get.ps1 (see the README)." ;;
  esac
  local m
  if [ "$OS" = linux ]; then
    for m in apt-get dnf pacman brew; do
      if command -v "$m" >/dev/null 2>&1; then
        MGR="$m"
        break
      fi
    done
  elif command -v brew >/dev/null 2>&1; then
    MGR=brew
  fi
  if [ "$(id -u)" -ne 0 ] && [ "$MGR" != brew ] && command -v sudo >/dev/null 2>&1; then
    SUDO=(sudo)
  fi
  say "OS: $OS, package manager: ${MGR:-none found}"
}

manual_steps() {
  if [ "$OS" = macos ]; then
    err "Install Homebrew from https://brew.sh, then re-run this script."
    err "  Or install them yourself: git 2.40+, python3 3.12+ and gh (https://cli.github.com)."
  else
    err "Install them with your package manager: git 2.40+, python3 3.12+ and gh."
    err "  gh: $GH_LINUX_URL"
    err "Then re-run this script."
  fi
}

# Runs a package manager command, with sudo when needed.
pkg_run() {
  if [ "$MGR" = brew ] || [ "$(id -u)" -eq 0 ]; then
    run "$@"
  elif [ "${#SUDO[@]}" -gt 0 ]; then
    run "${SUDO[@]}" "$@"
  else
    err "Not root and no sudo: cannot run $*"
    return 1
  fi
}

pkg_name() {
  case "$MGR:$1" in
    pacman:python3) echo python ;;
    pacman:gh) echo github-cli ;;
    brew:python3) echo python ;;
    *:pip) case "$MGR" in pacman) echo python-pip ;; *) echo python3-pip ;; esac ;;
    *) echo "$1" ;;
  esac
}

pkg_install() {
  local pkg
  pkg="$(pkg_name "$1")"
  case "$MGR" in
    apt-get)
      if [ "$APT_UPDATED" -eq 0 ]; then
        pkg_run apt-get update || return 1
        APT_UPDATED=1
      fi
      pkg_run env DEBIAN_FRONTEND=noninteractive apt-get install -y "$pkg"
      ;;
    dnf) pkg_run dnf install -y "$pkg" ;;
    pacman) pkg_run pacman -S --needed --noconfirm "$pkg" ;;
    brew) pkg_run brew install "$pkg" ;;
    *) return 1 ;;
  esac
}

# --- 2. git, python3, gh ---

python_too_old() {
  local found="$1" where
  where="$(command -v python3 || true)"
  err "python3 on PATH is $found ($where); claude-config needs 3.12 or newer."
  err "The Claude hooks run 'python3' from PATH, so 3.12+ must be the system python3."
  err "A uv or pyenv Python does not count: the hooks do not see it."
  if [ "$OS" = macos ]; then
    err "What to do: 'brew install python', put Homebrew's bin first on PATH"
    err "  (eval \"\$(brew shellenv)\" in your shell profile), open a new terminal, then re-run."
  else
    err "What to do: install Python 3.12+ as python3 (Debian 13 and Ubuntu 24.04 ship it;"
    err "  on Debian 12 or Ubuntu 22.04 upgrade the OS or use a trusted backport), then re-run."
  fi
  exit 1
}

check_tools() {
  say "[2/6] Checking git, python3 and gh"
  local gv pv missing="" tool
  gv="$(git_version)"
  pv="$(python_version)"

  # A too-old python3 is not fixed here, so stop before installing anything.
  local py_old=0
  if command -v python3 >/dev/null 2>&1 && [ -n "$pv" ] && ! ver_ge "$pv" 3 12; then
    if [ "$OS" = macos ] && [ "$MGR" = brew ]; then
      py_old=1 # offered below: brew install python, then checked again
    else
      python_too_old "$pv"
    fi
  fi

  if ver_ge "$gv" 2 40; then say "git $gv: ok"; else missing="$missing git"; fi
  if [ "$py_old" -eq 1 ]; then
    say "python3 $pv: too old (3.12 or newer is needed)"
    missing="$missing python3"
  elif [ -n "$pv" ]; then say "python3 $pv: ok"; else missing="$missing python3"; fi
  if command -v gh >/dev/null 2>&1; then say "gh: ok"; else missing="$missing gh"; fi
  [ -n "$missing" ] || return 0

  if [ -z "$MGR" ]; then
    err "Missing or too old:$missing"
    manual_steps
    exit 1
  fi
  for tool in $missing; do
    if [ "$tool" = git ] && [ -n "$gv" ]; then
      ask_yes "git $gv is older than 2.40. Upgrade it with $MGR?" || die "git 2.40+ is needed. Install it, then re-run."
    else
      ask_yes "$tool is missing or too old. Install it with $MGR?" || die "$tool is needed. Install it, then re-run."
    fi
    if ! pkg_install "$tool"; then
      err "Could not install $tool with $MGR."
      if [ "$tool" = gh ] && { [ "$MGR" = apt-get ] || [ "$MGR" = dnf ]; }; then
        err "gh may need GitHub's own package repo: $GH_LINUX_URL"
      fi
      manual_steps
      exit 1
    fi
  done
  [ "$DRY_RUN" -eq 0 ] || return 0

  # Check again: a stock repo can still be too old.
  hash -r
  gv="$(git_version)"
  pv="$(python_version)"
  if ! ver_ge "$gv" 2 40; then
    err "git is still ${gv:-missing} after the install; 2.40 or newer is needed."
    if [ "$OS" = macos ]; then
      err "Put Homebrew's bin first on PATH (eval \"\$(brew shellenv)\"), open a new terminal, then re-run."
    else
      err "Install git 2.40+ from your distro's backports or https://git-scm.com/download/linux, then re-run."
    fi
    exit 1
  fi
  [ -n "$pv" ] || die "python3 is still missing after the install. Install Python 3.12+, then re-run."
  ver_ge "$pv" 3 12 || python_too_old "$pv"
  command -v gh >/dev/null 2>&1 || die "gh is still missing after the install. See: $GH_LINUX_URL"
  say "git $gv, python3 $pv and gh: ok"
}

# --- 3. pip ---

check_pip() {
  say "[3/6] Checking pip"
  local found="" t
  for t in pipx uv; do
    if command -v "$t" >/dev/null 2>&1; then found="${found:+$found/}$t"; fi
  done
  [ -z "$found" ] || say "found $found: not needed, not used"

  if [ "${TIER:-${CLAUDE_SYNC_TIER:-}}" = basic ]; then
    say "basic tier: no pip packages, not checking pip."
    return 0
  fi
  if [ "$SKIP_PY" -eq 1 ]; then
    say "--skip-python-deps given: not checking pip."
    return 0
  fi
  if python3 -m pip --version </dev/null >/dev/null 2>&1; then
    say "pip: ok"
    return 0
  fi
  local ok=1
  if ask_yes "pip is not installed for python3. Install it?"; then
    case "$MGR" in
      apt-get | dnf | pacman) pkg_install pip || ok=0 ;;
      *) run python3 -m ensurepip --upgrade || ok=0 ;;
    esac
    if [ "$ok" -eq 1 ] && [ "$DRY_RUN" -eq 0 ] && ! python3 -m pip --version </dev/null >/dev/null 2>&1; then
      ok=0
    fi
    [ "$ok" -eq 1 ] && return 0
    err "Could not install pip."
  fi
  SKIP_PY=1
  say "Continuing with --skip-python-deps. Only what the agent-memory plugin needs"
  say "  (its pip packages and the python alias) is skipped; the sync itself is unaffected."
}

# --- 4. GitHub login ---

check_login() {
  say "[4/6] Checking the GitHub login"
  if ! command -v gh >/dev/null 2>&1; then
    # Only in a dry run: gh would have been installed in step 2.
    run gh auth login --hostname github.com --git-protocol https --web
    run gh auth setup-git
    return 0
  fi
  if gh auth status --hostname github.com </dev/null >/dev/null 2>&1; then
    say "gh is logged in to github.com: ok"
    return 0
  fi
  if [ "$DRY_RUN" -eq 0 ] && ! have_tty; then
    die "gh is not logged in to github.com and there is no terminal for 'gh auth login'." \
      "Run 'gh auth login' yourself (or set GH_TOKEN), then re-run."
  fi
  run gh auth login --hostname github.com --git-protocol https --web ||
    die "gh auth login failed. Run 'gh auth login' yourself, then re-run."
  run gh auth setup-git || die "gh auth setup-git failed. Run it yourself, then re-run."
}

# --- 5. clone ---

get_clone() {
  say "[5/6] Getting $REPO into $DIR"
  if [ -e "$DIR/.git" ]; then
    say "$DIR is already cloned, using it (left as it is)."
    local origin
    origin="$(git -C "$DIR" remote get-url origin 2>/dev/null </dev/null || true)"
    if [ -z "$origin" ]; then
      die "$DIR has no 'origin' remote, so the installer cannot use it." \
        "Move it away or pick another folder with --dir, then re-run."
    fi
    local slug
    slug="$(printf '%s' "$origin" | sed -E 's#^[A-Za-z+.-]+://([^@/]*@)?[^/]*/##; s#^[^@/]*@[^:/]*:##; s#/+$##; s#\.git$##')"
    if [ "$(printf '%s' "$slug" | tr '[:upper:]' '[:lower:]')" != "$(printf '%s' "$REPO" | tr '[:upper:]' '[:lower:]')" ]; then
      err "Warning: $DIR comes from $(redact_url "$origin"), not $REPO. Using it anyway."
    fi
    return 0
  fi
  if [ -e "$DIR" ] || [ -L "$DIR" ]; then
    die "$DIR exists but is not a git clone." \
      "Move it away, or pick another folder with --dir, then re-run."
  fi
  run gh repo clone "$REPO" "$DIR" ||
    die "Could not clone $REPO. Check the name and that this gh login can read it ('gh repo view $REPO')."
}

# --- 6. installer ---

run_installer() {
  say "[6/6] Running the installer"
  local url args=()
  if [ "$DRY_RUN" -eq 1 ] && [ ! -e "$DIR/.git" ]; then
    url="<origin URL of $DIR>"
  else
    url="$(git -C "$DIR" remote get-url origin </dev/null)"
  fi
  [ "$HAVE_PROJECT_PATH" -eq 0 ] || args+=(--project-path "$PROJECT_PATH")
  [ "$HAVE_PROFILE" -eq 0 ] || args+=(--profile "$PROFILE_NAME")
  [ -z "$TIER" ] || args+=(--tier "$TIER")
  [ "$SKIP_PY" -eq 0 ] || args+=(--skip-python-deps)

  # Printed with the URL redacted; run with the real one.
  local rc=0 shown
  shown="$(redact_url "$url")"
  if [ "$DRY_RUN" -eq 1 ]; then
    show_cmd "[dry run] would run:" env CLAUDE_SYNC_REPO_URL="$shown" CLAUDE_SYNC_REPO_DIR="$DIR" \
      "$DIR/install.sh" ${args[@]+"${args[@]}"}
  else
    show_cmd "+" env CLAUDE_SYNC_REPO_URL="$shown" CLAUDE_SYNC_REPO_DIR="$DIR" \
      "$DIR/install.sh" ${args[@]+"${args[@]}"}
    # Exported in a subshell, not passed through env(1), so a token in the
    # origin URL never shows in the process list.
    (
      export CLAUDE_SYNC_REPO_URL="$url" CLAUDE_SYNC_REPO_DIR="$DIR"
      run_in "$DIR/install.sh" ${args[@]+"${args[@]}"}
    ) || rc=$?
  fi
  if [ "$rc" -ne 0 ]; then
    err "install.sh failed (exit $rc). Fix what it says above, then re-run this script; the clone is kept."
    exit "$rc"
  fi
  say ""
  if [ "$DRY_RUN" -eq 1 ]; then
    say "Dry run finished: nothing was changed."
    return 0
  fi
  say "Done. Next steps:"
  say "  1. Merge this machine's sync PR (sync/<hostname>) on GitHub, if one was opened:"
  say "       gh pr list --repo $REPO"
  say "  2. Check the machine: $DIR/claude-sync.sh doctor"
}

main() {
  parse_args "$@"
  [ "$DRY_RUN" -eq 0 ] || say "Dry run: commands that change things are printed, not run."
  detect_system
  resolve_repo
  [ -n "${HOME:-}" ] || die "HOME is not set." "Set HOME, or pass --dir with an absolute path."
  [ -n "$DIR" ] || DIR="$HOME/claude-config"
  # A literal ~ from --dir=~/x is not expanded by the shell, so do it here.
  # shellcheck disable=SC2088
  case "$DIR" in
    "~") DIR="$HOME" ;;
    "~/"*) DIR="$HOME/${DIR#"~/"}" ;;
  esac
  case "$DIR" in
    /*) ;;
    *) DIR="$PWD/$DIR" ;;
  esac
  check_tools
  check_pip
  check_login
  get_clone
  run_installer
}

main "$@"
