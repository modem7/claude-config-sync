# Contributing

## Issues

- Search existing issues before opening a new one.
- Use the provided templates for bug reports and feature requests.

## Pull Requests

- Fork the repo and create your branch from `master`.
- Develop in a separate `git worktree` cut from `origin/master`, not in the live clone: while a sync PR is pending, the live clone's working tree carries that pending state.
- Keep PRs focused — one change per PR.
- Ensure your changes don't break existing functionality.
- Run `python -m pytest tests/py` before opening the PR, and `bats tests` too if you touched the legacy bash (`lib/`, `legacy/`). CI runs pytest on Linux and a small Windows smoke subset (links, install, shims, lock) on PRs that touch those paths; it does not run bats.
- Update documentation if needed.

## Code Style

- Follow the `.editorconfig` settings (LF line endings, UTF-8, trailing newline).
- Shell scripts should be POSIX-compatible where possible, or explicitly target bash.
