# AGENTS.md resolution in H1

Athena resolves project instructions without reading outside the explicit workspace:

1. Load `<workspace>/AGENTS.md` when present.
2. For every file or directory discovered through a validated tool call, walk from the
   workspace root to that target's parent and load each `AGENTS.md` encountered.
3. Deduplicate instruction files by canonical path.
4. Present instructions root-first and closest-to-target last; later, more specific
   instructions take precedence when they conflict.
5. Never follow an instruction file reached through a symlink that resolves outside the
   workspace.
6. Stop adding instruction text at the configured character limit. Repository contents are
   never loaded wholesale as context.
