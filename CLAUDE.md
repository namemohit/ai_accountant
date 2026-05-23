# Project memory — AI Accountant (YantrAI)

## Git checkpoint / rollback policy (IMPORTANT)
Whenever the user asks to push code to git and/or deploy to Cloud Run, ALSO create a
dated, named git **tag** marking that known-good state, so we can always roll back
to a specific point if a later push breaks something.

- Tag format: `good-YYYY-MM-DD` (append `-N` if multiple tags land the same day, e.g. `good-2026-05-23-2`).
- Create the tag on the commit being pushed, and push tags too:
  - `git tag -a good-2026-05-23 -m "known-good checkpoint"` then `git push origin --tags`
- When the user wants to "go back to <date>", find the matching `good-<date>` tag
  (or the nearest commit) and prefer NON-destructive recovery:
  - inspect/branch: `git switch -c recover-<date> good-<date>`
  - roll live code back keeping history: `git revert --no-commit good-<date>..HEAD` then commit
- NEVER use `git reset --hard` / `git push --force` to roll back without explicit user permission.
- Reminder: only *committed* states are recoverable; uncommitted local edits are not.
  Tags only land on commit points, not arbitrary mid-day moments.

## Standing user rules (also in global CLAUDE.md)
- **Commit locally only.** NEVER `git push` or deploy (Cloud Run / gcloud) unless the
  user explicitly says to in that message. Default after any change = `git commit`
  (+ dated `good-` tag) locally, then stop and wait. Do not auto-push.
- Never deploy to Cloud Run / gcloud without explicit per-request permission.
- Never delete anything (files, DB rows, tables, columns) without explicit permission.
- Restarting the local server (kill PID on :8000 + relaunch `python server.py`) is
  pre-authorized — no need to ask each time.
- Keep replies concise; avoid verbosity.

## Service worker cache
`static/sw.js` `CACHE_NAME` must be bumped on every front-end change (currently the
versioning scheme is `yantrai-accounting-vNN`) or browsers keep stale JS/CSS.
