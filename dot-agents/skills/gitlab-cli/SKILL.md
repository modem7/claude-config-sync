---
name: gitlab-cli
description: Use when the user mentions GitLab, GitLab CI/CD pipelines, merge requests, or a GitLab-hosted repo. Installs the glab CLI if missing and covers common merge-request/pipeline operations.
---

# GitLab CLI (glab)

Covers both gitlab.com and self-hosted GitLab instances. No self-hosted GitLab
server URL is on record yet for this user — the first time one comes up, ask
for the host and record it (via memory) so future sessions don't need to ask
again, the same way the Woodpecker CI server URL is already on record.

## Check whether it's installed

```bash
which glab && glab --version
```

## Install if missing

```bash
sudo apt update && sudo apt install -y glab
```

The original `profclems/glab` GitHub repo is archived — GitLab now maintains
`glab` at `gitlab.com/gitlab-org/cli`, not on GitHub, so don't fetch release
assets from a GitHub URL. If apt's version (check with `apt-cache policy glab`)
is too old for something you need, get the latest `.deb` from
`https://gitlab.com/gitlab-org/cli/-/releases` directly rather than guessing a
GitHub release URL.

## Authentication

```bash
# gitlab.com
glab auth login

# self-hosted
glab auth login --hostname <gitlab.example.com>

# check current auth state
glab auth status
```

## Common operations

```bash
# Clone a repo
glab repo clone <group>/<project>

# Merge requests
glab mr list
glab mr view <mr-number>
glab mr create --title "..." --description "..."
glab mr checkout <mr-number>

# Pipelines (CI/CD)
glab ci list
glab ci view                  # current branch's latest pipeline
glab ci status
glab ci retry <job-id>
glab ci trace <job-id>        # stream a job's log

# Issues
glab issue list
glab issue view <issue-number>
```

## Related conventions

Never add a `Co-Authored-By` trailer to commits, and always open an MR rather
than pushing directly to a default branch — same global conventions that apply
to GitHub work.
