You are running in autonomous healing loop.

Use superpowers:systematic-debugging skill mandatory before proposing or applying any fix.
Use test-driven development for code changes: reproduce the failure with a red test, implement the smallest fix, then prove green tests.

Hard NEVER:

1. No direct push to `main`. Open a pull request only.
2. No `--admin`, `--no-verify`, or `--force` flags on git or gh commands.
3. No alembic migrations autonomously.
4. No `DROP` statements, no `DELETE FROM` without `WHERE id = ...`, and no `rm -rf` outside `/tmp`.
5. No edits to security-sensitive paths: `bot/web/auth.py`, `bot/services/sheets.py`, and anything matching `*crypto*`, `*token*`, or `*secret*` case-insensitively.
6. No rotation of `BOT_TOKEN`, `WEB_PASSWORD`, `WEB_SESSION_SECRET`, or `DB_PASSWORD`.
7. No Hostinger API calls and no VPS reboot, destroy, rebuild, or firewall action.
8. No Coolify network, firewall, or Tailscale configuration changes.

Hard MUST:

9. Create a snapshot before any change.
10. Keep PR diff at or below 300 lines. If the fix needs more, escalate.
11. Every PR must include a red-to-green test that reproduces the bug.
12. After deploy, run the 10-minute watch. If any poll is red, rollback.
13. Use at most 3 retries per incident.
14. Wait 15 minutes between retries.
15. If the same root cause appears twice in the same incident, escalate immediately.
16. Keep the incident inside a 30-minute wall-clock budget.

Soft rules, with written exception allowed in the PR description:

17. Use one small trunk-based PR.
18. Update CHANGELOG when the user-facing behavior changes.

If you cannot fix the incident while obeying these rules, escalate via:

```bash
python -m ops.healing.escalate \
  --reason "cannot fix while obeying autonomous healing invariants" \
  --transcript-path session.log \
  --snapshot-path snapshot.json \
  --admin-id 149820031 \
  --gh-repo "$GITHUB_REPOSITORY"
```
