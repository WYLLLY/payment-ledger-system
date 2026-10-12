---
name: git-workflow
description: Coordinate collaborative Git and GitHub development with safe branches, conventional commits, pull requests, CI, independent review, conflict handling and user-approved merges.
---

# Git Workflow

Use this skill when preparing, committing, pushing, reviewing, or integrating code changes with Git and GitHub. Follow the repository's `AGENTS.md` rules and any stricter project or user instructions. This workflow applies to every Codex agent contributing to this repository.

## Protect the shared repository

Start by checking the repository, branch and working tree:

```sh
git status --short --branch
git branch --show-current
git diff
git diff --cached
```

Treat existing modifications and untracked files as another contributor's work unless their ownership is clear. Do not overwrite, stage, revert, stash, move, or include them without understanding their purpose. Stage only the paths belonging to the task. If another contributor's work overlaps or ownership is unclear, coordinate before editing.

Never develop directly on `main`. Use a focused branch named `feature/<topic>`, `fix/<topic>`, or `refactor/<topic>`. Check that the requested branch does not already contain another contributor's work before using it.

```sh
git switch main
git pull --ff-only origin main
git switch -c feature/<topic>
```

If a branch already exists, inspect it before switching or building on it:

```sh
git branch --list
git log --oneline --decorate -n 12
git switch <existing-branch>
```

Do not reset, clean, force-push, delete branches, or rewrite shared history. Do not use `git stash` as a way to hide or take ownership of unexplained work.

## Make reviewable commits

Keep changes focused and make small, atomic commits. Use Conventional Commit subjects such as:

- `feat: add transfer endpoint`
- `fix: prevent duplicate payment posting`
- `refactor: isolate ledger posting`
- `test: cover concurrent idempotency requests`
- `docs: clarify payment retry behavior`

Before committing, inspect the complete unstaged and staged diffs, check whitespace, confirm that only intended paths are staged, and run the relevant tests and checks:

```sh
git diff --check
git diff
git diff --cached
git status --short
git add path/to/changed-file
git diff --cached
git commit -m "feat: concise change description"
```

Never stage everything blindly when shared or unrelated changes may exist. Never commit secrets, credentials, tokens, private keys, or sensitive data. If a secret is found, avoid copying it into output and report its location safely. Do not amend commits that may have been shared unless explicitly requested.

## Push and open a pull request

Push the focused branch regularly after its commits are coherent and relevant checks have run. Never push directly to `main` and never force-push.

```sh
git push -u origin feature/<topic>
```

Open a pull request targeting `main`, with a clear summary, motivation, tests and checks, relevant risks, and links to related work:

```sh
gh pr create --base main --head feature/<topic> \
  --title "feat: concise change description" \
  --body-file /path/to/pr-description.md
```

If `gh` is unavailable or unauthenticated, report that limitation and provide the branch and PR details for the user; do not substitute an unapproved credential or expose a token.

## CI and independent review

Inspect the pull request and required checks with GitHub CLI:

```sh
gh pr view --web
gh pr view --json title,body,baseRefName,headRefName,state,reviewDecision,statusCheckRollup
gh pr checks
gh run list --branch feature/<topic>
gh run view <run-id>
```

Wait for required GitHub Actions and other required CI checks to pass. A pending, skipped, or failed required check is not a pass. Diagnose failures, make focused correction commits, push them, and rerun relevant local verification and CI.

An independent human or agent reviewer must review the proposed change; the implementation author cannot count its own review as independent. For financial changes, include both adversarial and financial-invariant review required by `AGENTS.md`. Address confirmed findings in follow-up commits and rerun verification and CI.

Do not approve your own pull request. Do not bypass branch protections, required reviews, or CI.

## Parallel work and conflicts

Use a separate focused branch for each independent task. Before integrating work, fetch and inspect remote updates:

```sh
git fetch origin
git status --short --branch
git log --oneline --graph --decorate --all -n 30
```

Coordinate before editing the same files or changing shared interfaces. Communicate file ownership, branch names, and integration order when parallel contributions overlap. Integrate only after each contribution is understood and reviewed.

When a conflict occurs, inspect every conflicted path and both sides before editing:

```sh
git status
git diff --name-only --diff-filter=U
git diff -- path/to/conflicted-file
```

Resolve a conflict by preserving both contributions where they are compatible and applying the approved design where they differ. Never choose one side wholesale merely to clear markers. If intent is unclear or financial behavior may change, stop and ask the relevant contributors or user. After resolving, inspect the full diff, run relevant tests, and only then continue the merge or rebase workflow.

Prefer `git pull --ff-only` for updating a local branch from its tracking branch. Do not rebase or rewrite a branch that others may use unless all affected contributors have coordinated and the user has authorized it. Never discard uncommitted work to resolve conflicts.

## Merge gate

Before any merge, confirm that:

- the pull request targets the intended base branch;
- required CI checks have passed;
- an independent review has been completed and findings resolved;
- the final diff and verification results are understood;
- no unrelated or uncommitted work is included.

The user must explicitly approve each merge. Do not merge, enable auto-merge, or run `gh pr merge` before that approval. After approval, use the repository's allowed merge method and verify the resulting pull request and branch state:

```sh
gh pr merge <number> --merge
gh pr view <number> --json state,mergedAt,mergeCommit
git fetch origin
git status --short --branch
```

If the repository requires squash or rebase merging, use only its configured method after approval. Never bypass protections or merge a pull request with failing or pending required checks.

## Handoff

Report the branch, commit identifiers, push and pull-request status, CI results, independent review status, resolved conflicts, and any remaining blockers. Do not report a merge unless GitHub confirms it and the user approved it.
