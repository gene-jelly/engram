#!/bin/bash
# ╔════════════════════════════════════════════════════════════════════════════╗
# ║  📧 EMAIL SEND GUARD                                                      ║
# ║                                                                            ║
# ║  Blocks mcp__apple-apps__mail "send" operations.                           ║
# ║  Forces draft-first workflow via AppleScript.                              ║
# ║                                                                            ║
# ║  WHY: The MCP mail tool sends immediately with no draft preview,           ║
# ║  no attachment support, and no confirmation. AppleScript creates a         ║
# ║  visible draft that the user reviews before sending.                      ║
# ║                                                                            ║
# ║  DISABLE:  touch ~/.claude/hooks/EMAIL_GUARD_OFF                           ║
# ║  ENABLE:   rm ~/.claude/hooks/EMAIL_GUARD_OFF                              ║
# ╚════════════════════════════════════════════════════════════════════════════╝

GUARD_FLAG="${HOME}/.claude/hooks/EMAIL_GUARD_OFF"
if [[ -f "$GUARD_FLAG" ]]; then
  exit 0
fi

# Read tool input from stdin
input=$(cat)

# Fail closed: if input is empty or python3 missing, block
if [[ -z "$input" ]]; then
  echo "BLOCKED: email-send-guard received empty input — failing closed." >&2
  exit 2
fi
if ! command -v python3 &>/dev/null; then
  echo "BLOCKED: email-send-guard requires python3 — failing closed." >&2
  exit 2
fi

# Check if this is a mail send operation
operation=$(echo "$input" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    tool_input = data.get('tool_input', {})
    print(tool_input.get('operation', ''))
except (json.JSONDecodeError, KeyError, TypeError) as e:
    print('PARSE_ERROR', file=sys.stderr)
    sys.exit(1)
" 2>&1)

# If python parse failed, fail closed
if [[ $? -ne 0 ]]; then
  echo "BLOCKED: email-send-guard could not parse input — failing closed." >&2
  exit 2
fi

if [ "$operation" = "send" ]; then
    echo "BLOCKED: Email sending via MCP is disabled for safety." >&2
    echo "" >&2
    echo "Use AppleScript to create a visible DRAFT instead:" >&2
    echo "  osascript -e 'tell application \"Mail\" ...'" >&2
    echo "" >&2
    echo "This prevents accidental sends without attachments or confirmation." >&2
    echo "You must manually review and click Send." >&2
    exit 2
fi

# Allow all other mail operations (search, unread, latest, etc.)
exit 0
