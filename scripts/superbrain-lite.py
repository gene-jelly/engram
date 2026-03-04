#!/usr/bin/env python3
"""
Superbrain Lite - Lightweight per-prompt memory injection
==========================================================
Designed for speed: FTS5-only search with novelty checking.
No background services needed (no Neo4j, ChromaDB, Ollama).

Pipeline:
  1. Novelty check: Compare prompt FTS5 hits against preloaded IDs
     - Skip if >60% overlap (anticipatory loader already covered it)
  2. FTS5 search: Query claude-mem worker API (keyword only, no vectors)
  3. Simple temporal filter: Check valid_until_epoch
  4. Format and return: Hook JSON

Target latency: <500ms (vs 2-5s for full superbrain)

Usage:
    python3 superbrain-lite.py "your query here"
    python3 superbrain-lite.py --search-only --json "your query"

Toggle:
    touch ~/.claude-mem/USE_SUPERBRAIN_LITE   (enable lite mode)
    rm ~/.claude-mem/USE_SUPERBRAIN_LITE      (back to full mode)
"""

import sys
import os
import re
import json
import time
import sqlite3
import logging
from pathlib import Path
from typing import List, Dict, Set

# ============================================================================
# Configuration
# ============================================================================

DB_PATH = Path.home() / ".claude-mem" / "claude-mem.db"
PRELOADED_IDS_PATH = Path.home() / ".claude-mem" / "preloaded_ids.txt"
WORKER_PORT = int(os.environ.get("CLAUDE_MEM_WORKER_PORT", "37777"))
WORKER_HOST = os.environ.get("CLAUDE_MEM_WORKER_HOST", "127.0.0.1")
LOG_DIR = Path.home() / ".claude-mem" / "logs"
LOG_FILE = LOG_DIR / "superbrain-lite.log"

FETCH_LIMIT = 5
NOVELTY_THRESHOLD = 0.6  # Skip if >60% of results already preloaded

LOG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s: %(message)s',
    handlers=[logging.FileHandler(LOG_FILE)]
)
logger = logging.getLogger("superbrain-lite")


# ============================================================================
# Preloaded ID Management
# ============================================================================

def load_preloaded_ids() -> Set[str]:
    """Load observation IDs that were preloaded at session start."""
    if not PRELOADED_IDS_PATH.exists():
        return set()
    try:
        with open(PRELOADED_IDS_PATH, 'r') as f:
            ids = set()
            for line in f:
                line = line.strip()
                if line.startswith('#') or not line:
                    break  # Stop at entity section
                if line.isdigit():
                    ids.add(line)
            return ids
    except Exception:
        return set()


# ============================================================================
# FTS5 Search (Worker API)
# ============================================================================

def fts5_search(query: str, limit: int = FETCH_LIMIT) -> List[Dict]:
    """Query claude-mem worker API for keyword matches."""
    import urllib.parse
    import urllib.request

    query_encoded = urllib.parse.quote(query[:150])
    api_url = f"http://{WORKER_HOST}:{WORKER_PORT}/api/search/observations?query={query_encoded}&format=index&limit={limit}"

    try:
        with urllib.request.urlopen(api_url, timeout=3) as response:
            data = json.loads(response.read().decode())
            text_content = data.get("content", [{}])[0].get("text", "")
            obs_ids = re.findall(r'#(\d+)', text_content)
            return [{"id": oid, "content": text_content} for oid in obs_ids] if obs_ids else []
    except Exception as e:
        logger.warning(f"FTS5 search failed: {e}")
        return []


# ============================================================================
# Temporal Filter
# ============================================================================

def temporal_filter(obs_ids: List[str]) -> List[str]:
    """Filter out expired observations."""
    if not obs_ids or not DB_PATH.exists():
        return obs_ids
    try:
        now_ms = int(time.time() * 1000)
        int_ids = [int(oid) for oid in obs_ids]
        conn = sqlite3.connect(str(DB_PATH), timeout=3)
        placeholders = ','.join('?' * len(int_ids))
        cursor = conn.execute(f"""
            SELECT id FROM observations
            WHERE id IN ({placeholders})
              AND COALESCE(valid_from_epoch, 0) <= ?
              AND (valid_until_epoch IS NULL OR valid_until_epoch > ?)
              AND superseded_by IS NULL
        """, int_ids + [now_ms, now_ms])
        valid = {str(row[0]) for row in cursor.fetchall()}
        conn.close()
        return [oid for oid in obs_ids if oid in valid]
    except Exception as e:
        logger.warning(f"Temporal filter failed: {e}")
        return obs_ids


# ============================================================================
# Novelty Check
# ============================================================================

def check_novelty(result_ids: List[str], preloaded: Set[str]) -> bool:
    """Return True if results are novel (worth injecting)."""
    if not preloaded or not result_ids:
        return True  # No preloaded context = everything is novel

    overlap = sum(1 for rid in result_ids if rid in preloaded)
    overlap_ratio = overlap / len(result_ids)

    if overlap_ratio > NOVELTY_THRESHOLD:
        logger.info(f"NOVELTY: Skipping — {overlap}/{len(result_ids)} ({overlap_ratio:.0%}) already preloaded")
        return False

    logger.info(f"NOVELTY: Novel content — {overlap}/{len(result_ids)} ({overlap_ratio:.0%}) overlap")
    return True


# ============================================================================
# Output Formatting
# ============================================================================

def format_lite_output(results: List[Dict], latency_ms: int) -> str:
    """Format for hook injection."""
    if not results:
        return ""

    # The first result has the full content from the API
    content = results[0].get("content", "")
    if not content:
        return ""

    lines = [
        f"🧠 **Lite memory context** ({latency_ms}ms, {len(results)} obs):",
        "",
        content,
        "",
        "_Use /mem-search for more. Lite mode active — `rm ~/.claude-mem/USE_SUPERBRAIN_LITE` for full._"
    ]
    return "\n".join(lines)


def format_json_output(results: List[Dict], latency_ms: int) -> str:
    """Format for benchmark consumption."""
    return json.dumps({
        "mode": "lite",
        "total_latency_ms": latency_ms,
        "observation_ids": [r["id"] for r in results],
        "result_count": len(results),
        "content": results[0].get("content", "")[:500] if results else "",
    }, indent=2)


# ============================================================================
# Main Pipeline
# ============================================================================

def run_lite_pipeline(query: str, search_only: bool = False, json_mode: bool = False) -> str:
    """Execute the lite pipeline."""
    start = time.time()

    # Step 1: Load preloaded IDs
    preloaded = load_preloaded_ids()

    # Step 2: FTS5 search
    results = fts5_search(query)
    if not results:
        return ""

    result_ids = [r["id"] for r in results]

    # Step 3: Novelty check
    if not check_novelty(result_ids, preloaded):
        return ""  # Already covered by anticipatory loader

    # Step 4: Temporal filter
    valid_ids = temporal_filter(result_ids)
    results = [r for r in results if r["id"] in valid_ids]

    if not results:
        return ""

    # Step 5: Log context fetch (unless search-only mode)
    if not search_only and results:
        try:
            from datetime import datetime, timezone
            session_id = os.environ.get("CLAUDE_SESSION_ID", "unknown")
            now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            now_epoch = int(time.time())
            id_str = ','.join(r["id"] for r in results)
            conn = sqlite3.connect(str(DB_PATH), timeout=3)
            conn.execute("""
                INSERT INTO context_fetches (
                    claude_session_id, query, tool_name, topics_detected,
                    observation_ids, observation_count, created_at, created_at_epoch,
                    trigger_type, trigger_context, outcome_signal
                ) VALUES (?, ?, 'superbrain-lite', NULL, ?, ?, ?, ?, 'auto-lite',
                          'Lite mode FTS5 injection', 'pending')
            """, (session_id, query[:100], id_str, len(results), now, now_epoch))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.warning(f"Context fetch logging failed: {e}")

    latency_ms = int((time.time() - start) * 1000)
    logger.info(f"LITE: {len(results)} results in {latency_ms}ms")

    # Step 6: Format output
    if json_mode:
        return format_json_output(results, latency_ms)
    else:
        return format_lite_output(results, latency_ms)


# ============================================================================
# CLI
# ============================================================================

def main():
    import argparse

    parser = argparse.ArgumentParser(description="Superbrain Lite - fast FTS5 memory injection")
    parser.add_argument("query", nargs="+", help="Query string")
    parser.add_argument("--search-only", action="store_true", help="Skip logging (for benchmarks)")
    parser.add_argument("--json", action="store_true", help="Output structured JSON")

    args = parser.parse_args()
    query = " ".join(args.query)

    result = run_lite_pipeline(query, search_only=args.search_only, json_mode=args.json)
    if result:
        print(result)


if __name__ == "__main__":
    main()
