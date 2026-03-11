# Session Lifecycle

What happens when a Claude Code session starts, runs, and ends.

## Session Start

1. **Anticipatory loader** pre-loads ~25 observations based on time-of-day patterns
2. **Skills sync** pulls latest skills from the vault
3. **CLAUDE.md** and **MEMORY.md** loaded into context

## Every Prompt

1. **Gap detector** routes to LITE or FULL pipeline
2. 5-8 observations injected as invisible context
3. **Skill activator** suggests relevant skills

## Session End

1. Skills pushed back to vault
2. Observations persisted via claude-mem

## Overnight

- Hebbian learning strengthens frequently-accessed memories
- Ebbinghaus decay fades stale observations
- Consolidation merges related observations
