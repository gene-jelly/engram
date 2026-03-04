#!/usr/bin/env python3
"""
Anticipatory Memory Loader - Pre-loads likely-needed context at session start.

The brain's "priming" mechanism: before you even ask a question, the most
likely-relevant memories are warmed up and ready. Uses:
- Time-of-day patterns (5 AM = beads/tasks, evening = development)
- High-confidence observation types (facts, decisions > discoveries)
- Graph centrality (observations with many links are structural memories)
- Recency + decay-adjusted confidence
- Last 3 session summaries for continuity
- Recently-accessed entity context (7-day window)

Stores preloaded IDs for lite-mode novelty checking.

Usage:
    python3 anticipatory-loader.py              # Pre-load top observations
    python3 anticipatory-loader.py --json       # JSON output for hooks
    python3 anticipatory-loader.py --stats      # Show loading statistics
"""

import sqlite3
import json
import sys
import time
from collections import Counter
from pathlib import Path
from datetime import datetime

DB_PATH = Path.home() / ".claude-mem" / "claude-mem.db"
PRELOADED_IDS_PATH = Path.home() / ".claude-mem" / "preloaded_ids.txt"

# Configuration
PRELOAD_LIMIT = 25              # Observations to pre-load (was 10)
SESSION_SUMMARY_LIMIT = 3       # Recent session summaries
ENTITY_LOOKBACK_DAYS = 7        # Entity recency window
CONFIDENCE_WEIGHT = 0.25
RECENCY_WEIGHT = 0.25
GRAPH_WEIGHT = 0.25
STRENGTH_WEIGHT = 0.15
ENTITY_WEIGHT = 0.10

# Time-of-day topic signatures
TIME_SIGNATURES = {
    range(3, 7): {'preferred_types': ['decision', 'fact'], 'topic_boost': ['task', 'bead', 'workflow']},
    range(7, 12): {'preferred_types': ['decision', 'fact', 'discovery'], 'topic_boost': ['briefing', 'email', 'calendar']},
    range(12, 18): {'preferred_types': ['bugfix', 'feature', 'discovery'], 'topic_boost': ['development', 'code', 'testing']},
    range(18, 24): {'preferred_types': ['feature', 'decision', 'discovery'], 'topic_boost': ['architecture', 'memory', 'system']},
    range(0, 3): {'preferred_types': ['decision', 'fact'], 'topic_boost': ['wrap', 'consolidation']},
}


def get_time_signature():
    hour = datetime.now().hour
    for time_range, sig in TIME_SIGNATURES.items():
        if hour in time_range:
            return sig
    return {'preferred_types': [], 'topic_boost': []}


def get_recent_session_summaries(conn, limit=SESSION_SUMMARY_LIMIT):
    """Fetch last N session summaries for continuity context."""
    cursor = conn.cursor()
    cursor.execute('''
        SELECT id, request, learned, completed, next_steps, created_at
        FROM session_summaries
        ORDER BY created_at_epoch DESC
        LIMIT ?
    ''', (limit,))
    summaries = []
    for row in cursor.fetchall():
        sid, request, learned, completed, next_steps, created_at = row
        parts = []
        if request:
            parts.append(f"Request: {request[:80]}")
        if learned:
            parts.append(f"Learned: {learned[:80]}")
        if completed:
            parts.append(f"Completed: {completed[:60]}")
        if next_steps:
            parts.append(f"Next: {next_steps[:60]}")
        summaries.append({
            'id': sid,
            'summary': ' | '.join(parts),
            'created_at': created_at,
        })
    return summaries


def get_recently_accessed_entities(conn, lookback_days=ENTITY_LOOKBACK_DAYS):
    """Find entities linked to recently-accessed observations."""
    cursor = conn.cursor()
    cutoff_ms = int((time.time() - lookback_days * 86400) * 1000)
    cursor.execute('''
        SELECT DISTINCT e.id, e.name, e.type, COUNT(ol.id) as link_count
        FROM entities e
        JOIN observation_links ol ON ol.target_id = e.id AND ol.target_type = 'entity'
        JOIN observations o ON ol.source_id = o.id
        WHERE o.last_accessed_epoch > ?
          AND o.superseded_by IS NULL
        GROUP BY e.id
        ORDER BY link_count DESC
        LIMIT 10
    ''', (cutoff_ms,))
    entities = []
    for row in cursor.fetchall():
        eid, ename, etype, links = row
        entities.append({'id': eid, 'name': ename, 'type': etype, 'links': links})
    return entities


def get_anticipatory_observations(conn, limit=PRELOAD_LIMIT):
    """Query observations optimized for anticipatory loading."""
    cursor = conn.cursor()
    now_ms = int(time.time() * 1000)
    now_s = int(time.time())
    sig = get_time_signature()

    type_cases = []
    for i, t in enumerate(sig['preferred_types']):
        type_cases.append(f"WHEN '{t}' THEN {10 - i}")
    type_case_sql = f"CASE o.type {' '.join(type_cases)} ELSE 1 END" if type_cases else "1"

    cursor.execute(f'''
        SELECT
            o.id, o.type, o.title, o.confidence, o.decay_rate,
            o.last_accessed_epoch, o.created_at_epoch,
            COUNT(DISTINCT ol.id) as link_count,
            {type_case_sql} as type_priority
        FROM observations o
        LEFT JOIN observation_links ol ON (ol.source_id = o.id OR ol.target_id = o.id)
        WHERE
            COALESCE(o.valid_from_epoch, 0) <= ?
            AND (o.valid_until_epoch IS NULL OR o.valid_until_epoch > ?)
            AND o.superseded_by IS NULL
            AND o.title IS NOT NULL
            AND LENGTH(o.title) > 5
        GROUP BY o.id
        ORDER BY
            type_priority DESC,
            o.confidence DESC,
            link_count DESC,
            o.last_accessed_epoch DESC NULLS LAST
        LIMIT ?
    ''', (now_ms, now_ms, limit * 3))

    candidates = []
    for row in cursor.fetchall():
        oid, otype, title, confidence, decay_rate, last_accessed, created_epoch, links, type_pri = row

        conf_score = (confidence or 0.5) * CONFIDENCE_WEIGHT

        if last_accessed and last_accessed > 0:
            days_since = (now_s - last_accessed / 1000) / 86400
            recency_score = max(0, 1.0 - days_since / 30) * RECENCY_WEIGHT
        else:
            recency_score = 0

        graph_score = min(links / 10.0, 1.0) * GRAPH_WEIGHT
        strength_score = (type_pri / 10.0) * STRENGTH_WEIGHT

        topic_bonus = 0
        title_lower = title.lower() if title else ''
        for topic in sig['topic_boost']:
            if topic in title_lower:
                topic_bonus += 0.1

        total_score = conf_score + recency_score + graph_score + strength_score + topic_bonus

        candidates.append({
            'id': oid,
            'type': otype,
            'title': title or '(no title)',
            'score': round(total_score, 3),
            'confidence': confidence or 0.5,
            'links': links,
            'topic_match': topic_bonus > 0,
        })

    candidates.sort(key=lambda x: x['score'], reverse=True)
    return candidates[:limit]


def save_preloaded_ids(observations, entities):
    """Store preloaded IDs for lite-mode novelty checking."""
    ids = [str(obs['id']) for obs in observations]
    entity_ids = [e['id'] for e in entities]
    try:
        with open(PRELOADED_IDS_PATH, 'w') as f:
            f.write('\n'.join(ids))
            if entity_ids:
                f.write('\n# entities\n')
                f.write('\n'.join(entity_ids))
    except Exception:
        pass


def format_preload_context(observations, summaries, entities):
    """Format pre-loaded observations for hook injection."""
    lines = []

    # Session continuity
    if summaries:
        lines.append("📋 **Recent sessions** (continuity context):")
        for s in summaries:
            lines.append(f"  * S{s['id']}: {s['summary'][:120]}")
        lines.append("")

    # Entity context
    if entities:
        lines.append("🔗 **Active entities** (7-day window):")
        entity_strs = [f"{e['name']} ({e['type']}, {e['links']} links)" for e in entities[:5]]
        lines.append(f"  {', '.join(entity_strs)}")
        lines.append("")

    # Pre-loaded observations
    if observations:
        lines.append("🔮 **Anticipatory context** (pre-loaded based on time-of-day patterns):")
        lines.append("")
        for obs in observations:
            match_icon = "⭐" if obs['topic_match'] else "•"
            lines.append(f"{match_icon} **#{obs['id']}** [{obs['type']}] {obs['title'][:60]}")

    sig = get_time_signature()
    hour = datetime.now().hour
    lines.append(f"\n_Pre-loaded {len(observations)} obs + {len(summaries)} sessions + {len(entities)} entities for {hour}:00 ({', '.join(sig['preferred_types'][:2])} priority)_")
    return '\n'.join(lines)


def print_stats(conn):
    cursor = conn.cursor()
    now_ms = int(time.time() * 1000)

    cursor.execute('SELECT COUNT(*) FROM observations WHERE (valid_until_epoch IS NULL OR valid_until_epoch > ?)', (now_ms,))
    valid = cursor.fetchone()[0]
    cursor.execute('SELECT COUNT(*) FROM observations')
    total = cursor.fetchone()[0]
    cursor.execute('SELECT type, COUNT(*), AVG(confidence) FROM observations WHERE (valid_until_epoch IS NULL OR valid_until_epoch > ?) GROUP BY type ORDER BY COUNT(*) DESC', (now_ms,))
    types = cursor.fetchall()
    cursor.execute('SELECT COUNT(*) FROM observation_links')
    total_links = cursor.fetchone()[0]
    cursor.execute('SELECT COUNT(*) FROM session_summaries')
    total_sessions = cursor.fetchone()[0]
    cursor.execute('SELECT COUNT(*) FROM entities')
    total_entities = cursor.fetchone()[0]

    sig = get_time_signature()
    hour = datetime.now().hour

    print("### Anticipatory Loading Stats")
    print()
    print(f"**Time:** {hour}:00 ({datetime.now().strftime('%A')})")
    print(f"**Profile:** {', '.join(sig['preferred_types'][:3])} priority")
    print(f"**Topic boost:** {', '.join(sig['topic_boost'][:3])}")
    print()
    print(f"**Valid observations:** {valid:,} of {total:,} ({total - valid:,} archived)")
    print(f"**Graph links:** {total_links:,}")
    print(f"**Session summaries:** {total_sessions:,}")
    print(f"**Entities:** {total_entities:,}")
    print()
    print("**Type distribution (valid):**")
    for t, cnt, avg_conf in types:
        print(f"  {t}: {cnt:,} (avg confidence: {avg_conf:.2f})")


def main():
    args = set(sys.argv[1:])
    json_mode = '--json' in args
    stats_mode = '--stats' in args

    conn = sqlite3.connect(str(DB_PATH))

    if stats_mode:
        print_stats(conn)
        return

    observations = get_anticipatory_observations(conn)
    summaries = get_recent_session_summaries(conn)
    entities = get_recently_accessed_entities(conn)

    # Save preloaded IDs for lite-mode novelty checking
    save_preloaded_ids(observations, entities)

    if json_mode:
        print(json.dumps({
            'timestamp': datetime.now().isoformat(),
            'hour': datetime.now().hour,
            'observations': observations,
            'session_summaries': [s['summary'] for s in summaries],
            'entities': [{'name': e['name'], 'type': e['type']} for e in entities],
        }, indent=2))
    else:
        context = format_preload_context(observations, summaries, entities)
        if context:
            print(context)
        else:
            print("No observations to pre-load.")


if __name__ == '__main__':
    main()
