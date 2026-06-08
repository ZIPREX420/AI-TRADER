# Architecture Decision Records

Short, dated records of decisions with lasting structural impact. Each ADR
captures the context, the decision, and its consequences so the *why*
survives even when the code changes.

Format: one Markdown file per decision, `NNNN-kebab-title.md`, numbered in
the order accepted. ADRs are immutable once accepted — to revise a decision,
add a new ADR that supersedes the old one and update the old one's status.

## Index

| ADR | Title | Status |
|-----|-------|--------|
| [0001](0001-solana-py-dependency.md) | Resolve the `solana-py` / `websockets` conflict by version bump, standardize on `solders` | Accepted |
