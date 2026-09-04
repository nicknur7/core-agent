# Ship

Commit and push current Core changes to GitHub.

## Steps

1. **Check status:** `git status` and `git diff --stat` — summarize what's changed.

2. **Confirm with the operator:** Show the changed files. Ask: "Ship all of these?" before proceeding.

3. **Stage and commit:** Stage all changed files by name (not `git add -A`). Write a commit message in the format:
   ```
   Session YYYY-MM-DD: <one-line summary of what changed>
   ```

4. **Push:** `git push` to origin/main. Report success or any errors.

5. **Log:** Note the push in `memory/decisions-log.md` only if it included a significant architectural decision. Routine session pushes don't need a log entry.

## Rules

- Never skip hooks (`--no-verify`)
- Never force-push to main
- Never commit `.env`, API keys, or credential files — scan for `sk-`, `sk-ant-`, `ghp_` before staging
- If hooks fail, fix the issue and recommit — do NOT use `--no-verify` to bypass
