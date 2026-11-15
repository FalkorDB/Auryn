"""Claude Code agent hooks (distinct from ``core.hooks``, which installs git resync hooks).

``postuse_enrich`` is a **PostToolUse** hook: after the agent runs one of its own reflex tools
(Grep / Read / Bash / Edit / Write) it enriches the result with what those tools structurally
can't see -- a symbol's callers, callees, blast radius, and semantically-related code, pulled
from the code graph. The enrichment is delivered as ``hookSpecificOutput.additionalContext`` and
wrapped in a self-labelling ``⟦auryn⟧`` envelope so the model can always tell graph-injected
facts apart from the tool's own output (Claude Code injects additionalContext as an unattributed
system-reminder, so that provenance is on us to encode).
"""
