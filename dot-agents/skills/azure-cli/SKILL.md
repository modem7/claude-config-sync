---
name: azure-cli
description: Use when the user mentions Azure, Azure DevOps, Azure Pipelines, or an Azure-hosted repo. Installs the az CLI (and its azure-devops extension) if missing, and covers common pipeline/repo operations.
---

# Azure CLI (az) + Azure DevOps extension

No Azure DevOps organization/project is on record yet for this user — the
first time one comes up, ask for the organization and project names and
record them (via memory) so future sessions don't need to ask again, the
same way the Woodpecker CI server URL is already on record.

## Check whether it's installed

```bash
which az && az --version
```

## Install if missing

Official Microsoft install script for Debian/Ubuntu:

```bash
curl -sL https://aka.ms/InstallAzureCLIDeb | sudo bash
az --version
```

## Add the Azure DevOps extension (needed for pipelines/repos/boards)

```bash
az extension add --name azure-devops
```

## Authentication

```bash
az login
```

This opens a device-code flow — on a headless VM, it prints a URL and code to
enter on another device. Once logged in, set defaults so `az pipelines`/
`az repos` commands don't need `--organization`/`--project` every time:

```bash
az devops configure --defaults organization=https://dev.azure.com/<org> project=<project>
```

## Common operations

```bash
# Pipelines
az pipelines list
az pipelines show --id <pipeline-id>
az pipelines run --id <pipeline-id>
az pipelines build list --top 10
az pipelines runs list --top 10

# Repos
az repos list
az repos pr list
az repos pr show --id <pr-id>
az repos pr create --title "..." --description "..." --source-branch <branch> --target-branch main

# Work items / boards
az boards work-item show --id <id>
```

## Related conventions

Never add a `Co-Authored-By` trailer to commits, and always open a PR rather
than pushing directly to a default branch — same global conventions that apply
to GitHub/GitLab work.
