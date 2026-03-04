#!/bin/bash
# Gap Detector: Smart routing shell for memory context injection
# Uses Lite (fast FTS5) by default, upgrades to Full (9-layer) for deep-recall prompts
#
# Routing logic:
#   FULL when prompt has: recall patterns, technical terms, complex questions, novel topics
#   LITE for everything else (casual, operational, short prompts)
#
# Flags:
#   DISABLE ALL:    touch ~/.claude-mem/GAP_DETECTOR_OFF
#   FORCE LITE:     touch ~/.claude-mem/USE_SUPERBRAIN_LITE   (always lite, never full)
#   FORCE FULL:     touch ~/.claude-mem/USE_SUPERBRAIN_FULL   (always full, never lite)
#   FAST TIER:      touch ~/.claude-mem/FAST_TIER_ONLY        (skip slow layers in full mode)

# ── Kill switch ──
if [[ -f "${HOME}/.claude-mem/GAP_DETECTOR_OFF" ]]; then
  exit 0
fi

# ── Read prompt from stdin ──
INPUT=$(cat)
PROMPT=$(echo "$INPUT" | jq -r '.prompt // empty')

# Exit silently if no prompt or trivial (< 3 words)
if [[ -z "$PROMPT" ]]; then
  exit 0
fi
WORD_COUNT=$(echo "$PROMPT" | wc -w | tr -d ' ')
if [[ "$WORD_COUNT" -lt 3 ]]; then
  exit 0
fi

# ── Decide: Lite or Full? ──
USE_FULL=false

# Force flags override smart routing
if [[ -f "${HOME}/.claude-mem/USE_SUPERBRAIN_LITE" ]]; then
  USE_FULL=false
elif [[ -f "${HOME}/.claude-mem/USE_SUPERBRAIN_FULL" ]]; then
  USE_FULL=true
else
  # Smart routing: check if prompt needs deep recall
  PROMPT_LOWER=$(echo "$PROMPT" | tr '[:upper:]' '[:lower:]')

  # Pattern 1: Recall language (asking about past work)
  if echo "$PROMPT_LOWER" | grep -qE '(last time|remember when|how did we|before we|previously|earlier today|yesterday|last session|last week|we discussed|we decided|we built|we fixed|what was|where did)'; then
    USE_FULL=true
  # Pattern 2: Complex question (>12 words with question mark)
  elif [[ "$WORD_COUNT" -gt 12 ]] && echo "$PROMPT" | grep -q '?'; then
    USE_FULL=true
  # Pattern 3: Technical deep-dive (error messages, stack traces, package names)
  elif echo "$PROMPT_LOWER" | grep -qE '(error|exception|traceback|stack trace|TypeError|ImportError|ModuleNotFoundError|npm|pip|brew|neo4j|chromadb|sqlite|postgres)'; then
    USE_FULL=true
  # Pattern 4: Architecture/design questions
  elif echo "$PROMPT_LOWER" | grep -qE '(architect|design|refactor|migrate|benchmark|performance|optimize|trade.?off)'; then
    USE_FULL=true
  fi
fi

# ── Route to backend ──
if [[ "$USE_FULL" == "true" ]]; then
  SEARCH_TEXT=$(python3 "${HOME}/.claude/scripts/superbrain_query.py" "$PROMPT" 2>/dev/null)
else
  SEARCH_TEXT=$(python3 "${HOME}/.claude/scripts/superbrain-lite.py" "$PROMPT" 2>/dev/null)
fi

if [[ -z "$SEARCH_TEXT" ]]; then
  exit 0
fi

# ── Output as hook JSON ──
ESCAPED_TEXT=$(echo "$SEARCH_TEXT" | jq -sR .)
echo "{\"hookSpecificOutput\":{\"hookEventName\":\"UserPromptSubmit\",\"additionalContext\":${ESCAPED_TEXT}}}"
exit 0
