# Design decisions — M3 Supply-Chain Command Center

One entry per decision that changes behaviour: what was decided, what else was considered, why, and the
evidence. Newest last. Project A's log is in `m3-trusted-data-foundation/docs/decisions.md`; decisions shared by
both repositories are recorded in whichever repo implements them and referenced from the other.

### B-001 · 2026-09-16 · Scaffold reuses Project A's proven infrastructure, with its own data model and ports

- **Decision:** the repository starts from the parts of `m3-trusted-data-foundation` that are already proven in CI and production, copied rather than imported: the numbered plain-SQL migration runner (`db/migrate.py`, advisory lock, checksums), settings loaded from the environment with a `.env` that wins over the shell, the server-side role dependency (`app/auth.py`), the lint runner, the repo-hygiene guard and its banned-term list, and the GitHub Actions workflow.
  - **What differs, deliberately:** schemas are `gold`, `metrics` and `ops` (no medallion bronze/silver: this system reads gold and never ingests); settings carry `GOLD_SOURCE` (standalone or external), `POLICY_FILE`, `JIRA_MODE` and console URL; the local stack publishes Postgres on **5433**, the API on **8010** and the console on **8511**, so it runs beside Project A without a port clash; the database is `m3cc`.
  - **Copied, not imported.** The two repositories stay independently runnable and deployable; a shared library would make Project B unable to start without Project A, which is exactly what B14 (standalone mode) exists to prevent.
  - The public plan and the addendum are the same files as in Project A, retitled: one governing document, two copies, no divergence.
- **Alternatives:** a shared internal package for the common infrastructure (couples the repos and complicates deployment for perhaps 300 lines); starting from scratch (re-solving migrations, config precedence and CI for no gain); reusing Project A's ports (the two stacks could not run side by side, and every demo needs both).
- **Reason:** CLAUDE.md's session ritual and the plan's week-6 definition of done. **Verified:** migrations create the three schemas, re-running is a no-op, an edited applied migration is refused, and a non-Postgres URL is rejected; lint is clean; the repo-hygiene guard scans every text file for engagement-identifying terms.
