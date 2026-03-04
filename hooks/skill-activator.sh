#!/bin/bash
# Skill Semantic Auto-Activation (de9.6)
# Matches user prompts against skill index and suggests relevant skills
# Runs on UserPromptSubmit, after gap-detector

SKILL_INDEX="${HOME}/.claude/skill-index.json"
MATCH_THRESHOLD=1  # Minimum trigger matches to suggest (lowered from 2)

# Read the user prompt from stdin (JSON with "prompt" field)
INPUT=$(cat)
PROMPT=$(echo "$INPUT" | jq -r '.prompt // empty' | tr '[:upper:]' '[:lower:]')

# Exit silently if no prompt or index missing
[[ -z "$PROMPT" ]] && exit 0
[[ ! -f "$SKILL_INDEX" ]] && exit 0

# Skip trivial prompts
WORD_COUNT=$(echo "$PROMPT" | wc -w | tr -d ' ')
[[ "$WORD_COUNT" -lt 3 ]] && exit 0

# ============================================================================
# MATCH PROMPT AGAINST SKILL INDEX
# ============================================================================
BEST_SKILL=""
BEST_SCORE=0
BEST_COMMAND=""

# Read skills from index
while IFS= read -r skill_json; do
  name=$(echo "$skill_json" | jq -r '.name')
  command=$(echo "$skill_json" | jq -r '.command // empty')
  triggers=$(echo "$skill_json" | jq -r '.triggers[]' 2>/dev/null)

  score=0

  # Check each trigger against prompt
  while IFS= read -r trigger; do
    trigger_lower=$(echo "$trigger" | tr '[:upper:]' '[:lower:]')
    if [[ "$PROMPT" == *"$trigger_lower"* ]]; then
      score=$((score + 1))
    fi
  done <<< "$triggers"

  # Check if slash command is mentioned
  if [[ -n "$command" ]] && [[ "$PROMPT" == *"$command"* ]]; then
    score=$((score + 3))  # Strong signal
  fi

  # Update best match
  if [[ $score -gt $BEST_SCORE ]]; then
    BEST_SCORE=$score
    BEST_SKILL=$name
    BEST_COMMAND=$command
  fi
done < <(jq -c '.skills[]' "$SKILL_INDEX")

# ============================================================================
# SUGGEST IF MATCH FOUND
# ============================================================================
if [[ $BEST_SCORE -ge $MATCH_THRESHOLD ]]; then
  # Check both skills and commands directories
  SKILL_FILE="${HOME}/.claude/skills/${BEST_SKILL}.md"
  COMMAND_FILE="${HOME}/.claude/commands/${BEST_SKILL}.md"

  if [[ -f "$SKILL_FILE" ]]; then
    TARGET_FILE="$SKILL_FILE"
  elif [[ -f "$COMMAND_FILE" ]]; then
    TARGET_FILE="$COMMAND_FILE"
  else
    TARGET_FILE=""
  fi

  if [[ -n "$TARGET_FILE" ]]; then
    # Log contextual skill activation
    NOW=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
    LOG_FILE="${HOME}/.claude/usage.log"
    TYPE=$([[ "$TARGET_FILE" == *"/commands/"* ]] && echo "command_suggest" || echo "skill_suggest")
    echo "$NOW,$TYPE,$BEST_SKILL,score=$BEST_SCORE" >> "$LOG_FILE"

    # Extract skill title and first few lines of "When to Use"
    SKILL_TITLE=$(head -20 "$TARGET_FILE" | grep -E "^# " | head -1 | sed 's/^# //')
    SKILL_HINT=$(sed -n '/^## When to Use/,/^## /p' "$TARGET_FILE" | head -5 | tail -4 | tr '\n' ' ' | head -c 200)

    # Build suggestion context
    CONTEXT="💡 **Skill match:** ${SKILL_TITLE}\n"
    [[ -n "$BEST_COMMAND" ]] && CONTEXT+="_Invoke with \`${BEST_COMMAND}\`_\n"
    [[ -n "$SKILL_HINT" ]] && CONTEXT+="\n${SKILL_HINT}...\n"

    # Escape for JSON
    CONTEXT_ESCAPED=$(echo -e "$CONTEXT" | jq -Rs .)

    # Output as JSON for structured injection
    cat << EOF
{
  "hookSpecificOutput": {
    "hookEventName": "UserPromptSubmit",
    "additionalContext": ${CONTEXT_ESCAPED}
  }
}
EOF
  fi
fi

exit 0
