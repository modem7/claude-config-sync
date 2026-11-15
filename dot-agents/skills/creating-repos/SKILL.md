---
name: creating-repos
description: Use when the user asks to create, start, scaffold, bootstrap or standardise a new GitHub repository, or to bring an existing repo up to the standard starter files.
---

# Creating repos

New repos start from `modem7/DefaultRepo` (local: `~/project/DefaultRepo`). It is the single source of truth for starter files. Read it live; do not work from a remembered file list, because it changes (Renovate preset, auto-assign workflow and labels have all moved recently).

DefaultRepo is a plain repo, not a GitHub template, so copy files rather than using `--template`.

## Steps

1. **Repo state.** `git -C ~/project/DefaultRepo pull --ff-only` and check `git status`. Check the target name is free: `gh repo view modem7/NAME`.
2. **Confirm with the user before creating anything on GitHub:** name, one-line description, public or private, topics, and stack (none, Python, Node). State that the first commit goes straight to `master` because an empty repo has no base for a PR; every later change is a PR.
3. **Create and clone:** `gh repo create modem7/NAME --public|--private --clone` into `~/project/NAME`.
4. **Copy the baseline** from DefaultRepo, excluding `.git`: `rsync -a --exclude .git ~/project/DefaultRepo/ ~/project/NAME/`.
5. **Customise** (the "Things to update" table in DefaultRepo's README is the authority; these are the usual edits):
   - `.wakatime-project`: repo name.
   - `.github/settings.yml`: `name`, `description`, `homepage`, `topics`, `private`. Keep the top line `_extends: .github` (labels and defaults come from `modem7/.github`; do not add a labels block) and keep the full `repository:` block, because the Settings App only merges labels, not other keys. Keep `default_branch: master` and delete-branch-on-merge.
   - `README.md`: replace with real docs (what it is, install, usage). Remove the DefaultRepo-only sections (line endings, "things to update").
   - `LICENSE`: file name is `LICENSE` (not `LICENCE.txt`, which only the older 2021 Docker repos use), MIT, text `Copyright (c) <current year> modem7`. DefaultRepo's copy carries an old year, so always update it.
   - `renovate.json`: leave as `extends github>modem7/renovate-config`; add `:docker`, `:automergePatch` or `:automergeDigest` presets only where the stack needs them.
   - `.github/CODEOWNERS`, `autoassign.yml`, issue and PR templates: keep as is. `FUNDING.yml` is inherited from `modem7/.github`, so do not copy one.
6. **Add what the baseline lacks:**
   - `.desloppify/` in `.gitignore`, if the copied one lacks it. Every GitHub repo gets it; ssc (subseacraft) repos are never on GitHub, so there is no exception to check for.
   - Ask whether to add `CLAUDE.md` / `AGENTS.md`. Only claude-config and claude-config-sync are "AI safe" by default; others get one only if the user wants it.
   - Stack extras are repo dependent; ask rather than assume. Python gets `pyproject.toml` via `uv init`, `requirements-dev.txt`/lockfile, a `ci.yml` workflow; Node gets `package.json` and a `ci.yml`.
7. **First commit:** stage by name, commit "Initial commit from DefaultRepo", `git push -u origin master` on its own line (the branch guard blocks commands containing the word master alongside other flags). Commit message and any later PR body go in the scratchpad and are passed with `-F` / `--body-file`. No AI attribution lines.
8. **Verify** and report each result:
   - `gh api repos/modem7/NAME --jq '{default_branch,private,delete_branch_on_merge}'`
   - Settings app applied: description and topics match `.github/settings.yml`, and labels match those in `modem7/.github`.
   - `git ls-files | git check-attr -a --stdin | grep "text eol=lf"` finds no unmatched files.
   - Renovate onboarding or the Dependency Dashboard appears (can lag; say so if not yet there).

## Never

- Enable branch protection rules. The PR workflow plus the local push-guard hook cover it.
- Push to the default branch after the initial commit.
- Create the GitHub repo before step 2 is confirmed.

## Quick reference

| Item | Standard |
|---|---|
| Default branch | `master` |
| Owner / assignee / CODEOWNER | `modem7` |
| Licence | MIT, `LICENSE`, `Copyright (c) <year> modem7` |
| Line endings | LF, `.gitattributes` + `.editorconfig` |
| Dependency updates | Renovate via `modem7/renovate-config` |
| Repo settings | `.github/settings.yml` with `_extends: .github` (GitHub Settings App) |
| CI | GitHub Actions in `.github/workflows/` |

## Keeping this in step

If DefaultRepo drifts from reality (a standard file gets added to most repos but not to DefaultRepo), fix DefaultRepo first via PR, then this skill.