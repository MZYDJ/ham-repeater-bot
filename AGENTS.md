# Repository instructions for AI coding agents

## Mandatory rule: follow the repository SOP for every change

For every task that changes code, tests, configuration, documentation, deployment files, or CI in this repository, including small follow-up edits:

1. Before editing, read this file and `MODIFICATION_CHECKLIST.md` on the **target branch**. Do not rely only on conversation memory or an earlier copy.
2. Confirm the repository, target branch, remote HEAD SHA, relevant files, current CI status, and task acceptance criteria. Use the user-requested branch when specified; otherwise default to `feat/net-control`.
3. Before editing, identify affected files and cross-file dependencies; state the implementation/test plan, regression risks, branch-sync decision, and rollback path when material.
4. Implement the change together with necessary tests, README/config-example updates, deployment/CI changes, and SOP updates. Use the checklist's change-type matrix.
5. Run checks from SOP §5 that apply, plus syntax checks and targeted tests. Never claim a check passed unless it was actually run and passed. Clearly mark blocked or unrun checks.
6. Inspect the final diff/file list for correctness, unrelated changes, accidental deletions, and secrets. Commit/push only to the authorized target branch; verify the resulting commit SHA.
7. Check GitHub Actions for the **exact latest commit SHA** and wait for required runs to finish. An earlier green run does not validate a later commit.
8. Apply SOP §1 before deciding whether to sync `main`. `main` is the frozen pure-broadcast line: only port genuinely broadcast-core fixes, and never merge point-name-only functionality. Mixed commits must not be transplanted wholesale.
9. Treat production verification separately from tests/CI. Do not claim production success without evidence from the target runtime.
10. End with a concise delivery report: changed files/behavior, branch and commit link, actual test and CI results, production status, outstanding risks, and next steps for anything blocked.

Do not skip the SOP because the request is urgent, small, documentation-only, or a follow-up. If a required step cannot be performed with available tools, explain the limitation and mark it unverified rather than silently omitting it. Do not stop to ask the user to repeat these standing requirements.

## Branch policy

- Default development branch: `feat/net-control`.
- `main`: frozen pure-broadcast line; follow `MODIFICATION_CHECKLIST.md` §1 before any port.
- Branch resets, force-pushes, destructive operations, and production actions with material impact require explicit user authorization first.
- Prefer the repository's GitHub Connector/API for repository operations when available; report permission or tooling failures honestly.
