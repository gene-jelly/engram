#!/usr/bin/env python3
"""
Engram Superbrain — Multi-layer memory query orchestrator
==========================================================
Full 9-layer memory pipeline:

  Layer 1: Honcho (strategic facts via Dialectic API) — optional
  Layer 2: claude-mem (episodic observations via worker API)
  Layer 3: ChromaDB (semantic vector similarity search)
  Layer 4: A-Mem (conceptual Zettelkasten links via Ollama) — optional
  Layer 5: Entity Graph (Neo4j structured relationships) — optional
  Layer 6: Bi-temporal filter (exclude expired observations)
  Layer 7: RRF ranking (reciprocal rank fusion across signals)
  Layer 8: Retrieval strength boost (confidence x (1 - decay))
  Layer 9: Reconsolidation (retrieval strengthens memory)

Usage:
    python3 superbrain.py "your query here"
    python3 superbrain.py --search-only --json "your query"
    python3 superbrain.py --full "wait for all layers"

Output:
    Synthesized context from all layers, ready for gap-detector injection.

Configuration:
    All credentials and paths are loaded from environment variables.
    See .env.example for the full list.
"""
import asyncio
import sys
import os
import re
import time
import json
import sqlite3
import logging
import hashlib
from typing import List, Dict, Any, Optional, Set
from dataclasses import dataclass, field
from pathlib import Path

# ============================================================================
# Configuration (all from environment or sensible defaults)
# ============================================================================

DB_PATH = Path(os.environ.get("ENGRAM_DB_PATH", str(Path.home() / ".claude-mem" / "claude-mem.db")))
LOG_DIR = Path(os.environ.get("ENGRAM_LOG_DIR", str(Path.home() / ".claude-mem" / "logs")))
LOG_FILE = LOG_DIR / "superbrain.log"
FAST_TIER_ONLY_FLAG = Path.home() / ".claude-mem" / "FAST_TIER_ONLY"

LOG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s: %(message)s',
    handlers=[logging.FileHandler(LOG_FILE)]
)
logger = logging.getLogger("superbrain")

# ============================================================================
# Data Models
# ============================================================================

@dataclass
class QueryResult:
    """Result from a single memory layer."""
    layer: str
    items: List[Dict[str, Any]]
    latency_ms: int
    success: bool
    error: Optional[str] = None

@dataclass
class SynthesizedContext:
    """Final synthesized context from all layers."""
    layers_queried: int
    layers_succeeded: int
    total_latency_ms: int
    context_items: List[Dict[str, Any]]
    raw_results: Dict[str, QueryResult]
    observation_ids: List[int] = field(default_factory=list)

# ============================================================================
# Layer Adapters (1-5: Data Sources)
# ============================================================================

class HonchoAdapter:
    """Layer 1: Query Honcho strategic facts via Dialectic API.

    Optional layer — requires honcho-ai package and HONCHO_API_KEY.
    If not configured, this layer is silently skipped.
    """

    def __init__(self):
        self.client = None
        self._initialized = False
        self._init_error = None

    def _lazy_init(self):
        if self._initialized:
            return
        try:
            api_key = os.environ.get("HONCHO_API_KEY")
            if not api_key:
                self._init_error = "HONCHO_API_KEY not set"
                self._initialized = True
                return
            from honcho import Honcho
            workspace_id = os.environ.get("HONCHO_WORKSPACE_ID", "claude-memory")
            self.client = Honcho(api_key=api_key, workspace_id=workspace_id)
            self._initialized = True
        except ImportError:
            self._init_error = "honcho-ai not installed"
            self._initialized = True
        except Exception as e:
            self._init_error = f"Honcho init failed: {e}"
            self._initialized = True

    async def query(self, query: str, k: int = 5) -> QueryResult:
        start = time.time()
        try:
            self._lazy_init()
            if self._init_error:
                return QueryResult(layer="honcho", items=[], latency_ms=0,
                                   success=True, error=self._init_error)
            if not self.client:
                raise Exception("Honcho client not initialized")
            peer_name = os.environ.get("HONCHO_PEER_NAME", "user")
            peer = self.client.peer(peer_name)
            response = peer.chat(query)
            items = []
            if response:
                items.append({"content": str(response), "type": "dialectic_insight", "source": "peer.chat"})
            try:
                representation = peer.working_rep(search_query=query, search_top_k=k)
                if representation:
                    rep_str = str(representation)
                    if rep_str and rep_str != str(response):
                        items.append({"content": rep_str, "type": "representation", "source": "working_rep"})
            except Exception:
                pass
            return QueryResult(layer="honcho", items=items,
                               latency_ms=int((time.time() - start) * 1000), success=True)
        except Exception as e:
            return QueryResult(layer="honcho", items=[],
                               latency_ms=int((time.time() - start) * 1000), success=False, error=str(e))


class ClaudeMemAdapter:
    """Layer 2: Query claude-mem observations via worker API.

    This is the primary retrieval layer. The claude-mem worker must be running
    (it starts automatically with the claude-mem plugin).
    """

    async def query(self, query: str, k: int = 5) -> QueryResult:
        start = time.time()
        try:
            import urllib.parse
            import urllib.request
            worker_port = os.environ.get("CLAUDE_MEM_WORKER_PORT", "37777")
            worker_host = os.environ.get("CLAUDE_MEM_WORKER_HOST", "127.0.0.1")
            query_encoded = urllib.parse.quote(query[:150])
            api_url = f"http://{worker_host}:{worker_port}/api/search/observations?query={query_encoded}&format=index&limit={k}"
            with urllib.request.urlopen(api_url, timeout=5) as response:
                data = json.loads(response.read().decode())
                text_content = data.get("content", [{}])[0].get("text", "")
                obs_ids = [int(x) for x in re.findall(r'#(\d+)', text_content)]
                items = [{"content": text_content, "observation_ids": obs_ids}] if text_content else []
                return QueryResult(layer="claude-mem", items=items,
                                   latency_ms=int((time.time() - start) * 1000), success=True)
        except Exception as e:
            return QueryResult(layer="claude-mem", items=[],
                               latency_ms=int((time.time() - start) * 1000), success=False, error=str(e))


class ChromaDBAdapter:
    """Layer 3: Query ChromaDB for semantically similar observations.

    Uses the same vector database maintained by claude-mem. Finds observations
    that are conceptually related even when keywords don't match.
    """

    CHROMA_PATH = Path(os.environ.get("ENGRAM_CHROMA_PATH", str(Path.home() / ".claude-mem" / "vector-db")))
    COLLECTION_NAME = "cm__claude-mem"
    OVERFETCH_FACTOR = 2

    CACHE_FILE = Path.home() / ".claude-mem" / "chromadb_query_cache.json"
    CACHE_TTL_SEC = 300  # 5 minutes
    CACHE_MAX_ENTRIES = 100

    def __init__(self):
        self.client = None
        self.collection = None
        self.available = False
        self._query_cache = self._load_cache()
        self._init()

    def _load_cache(self) -> dict:
        """Load query cache from disk, pruning expired entries."""
        try:
            if self.CACHE_FILE.exists():
                with open(self.CACHE_FILE, 'r') as f:
                    cache = json.load(f)
                now = time.time()
                return {k: v for k, v in cache.items()
                        if now - v.get('ts', 0) < self.CACHE_TTL_SEC}
        except Exception:
            pass
        return {}

    def _save_cache(self):
        """Persist query cache to disk."""
        try:
            if len(self._query_cache) > self.CACHE_MAX_ENTRIES:
                sorted_keys = sorted(self._query_cache, key=lambda k: self._query_cache[k].get('ts', 0))
                for k in sorted_keys[:len(self._query_cache) - self.CACHE_MAX_ENTRIES]:
                    del self._query_cache[k]
            with open(self.CACHE_FILE, 'w') as f:
                json.dump(self._query_cache, f)
        except Exception:
            pass

    def _cache_key(self, query: str, k: int) -> str:
        return hashlib.md5(f"{query}:{k}".encode()).hexdigest()

    def _init(self):
        try:
            import chromadb
            if self.CHROMA_PATH.exists():
                self.client = chromadb.PersistentClient(path=str(self.CHROMA_PATH))
                self.collection = self.client.get_collection(name=self.COLLECTION_NAME)
                self.available = True
        except Exception:
            pass

    def _get_active_ids(self, candidate_ids: List[int]) -> set:
        if not candidate_ids:
            return set()
        try:
            with sqlite3.connect(str(DB_PATH), timeout=5) as conn:
                placeholders = ','.join('?' * len(candidate_ids))
                cursor = conn.execute(
                    f"SELECT id FROM observations WHERE id IN ({placeholders}) "
                    f"AND superseded_by IS NULL AND valid_until_epoch IS NULL",
                    candidate_ids
                )
                return {row[0] for row in cursor.fetchall()}
        except Exception:
            return set()

    async def query(self, query: str, k: int = 5) -> QueryResult:
        start = time.time()

        cache_key = self._cache_key(query, k)
        if cache_key in self._query_cache:
            cached = self._query_cache[cache_key]
            if time.time() - cached.get('ts', 0) < self.CACHE_TTL_SEC:
                logger.info(f"CHROMADB CACHE HIT: {query[:50]}")
                return QueryResult(layer="chromadb", items=cached['items'],
                                   latency_ms=int((time.time() - start) * 1000), success=True)

        if not self.available or not self.collection:
            return QueryResult(layer="chromadb", items=[], latency_ms=0, success=False, error="ChromaDB not available")
        try:
            fetch_k = min(k * self.OVERFETCH_FACTOR, 50)
            results = self.collection.query(query_texts=[query], n_results=fetch_k, include=["metadatas", "distances"])
            candidates = []
            if results.get("metadatas") and results["metadatas"][0]:
                for meta, dist in zip(results["metadatas"][0], results["distances"][0]):
                    if meta:
                        sqlite_id = meta.get("sqlite_id")
                        if sqlite_id:
                            candidates.append((int(sqlite_id), meta, dist))
            candidate_ids = [c[0] for c in candidates]
            active_ids = self._get_active_ids(candidate_ids)
            items = []
            seen_ids = set()
            for sqlite_id, meta, dist in candidates:
                if sqlite_id in active_ids and sqlite_id not in seen_ids:
                    seen_ids.add(sqlite_id)
                    similarity = 1.0 / (1.0 + dist)
                    items.append({
                        "id": sqlite_id, "title": meta.get("title", ""),
                        "type": meta.get("type", "observation"),
                        "score": round(similarity, 3), "source": "chromadb"
                    })
                    if len(items) >= k:
                        break

            self._query_cache[cache_key] = {'items': items, 'ts': time.time()}
            self._save_cache()

            return QueryResult(layer="chromadb", items=items,
                               latency_ms=int((time.time() - start) * 1000), success=True)
        except Exception as e:
            return QueryResult(layer="chromadb", items=[],
                               latency_ms=int((time.time() - start) * 1000), success=False, error=str(e))


class AMemAdapter:
    """Layer 4: Query A-Mem conceptual links via Ollama.

    Optional layer — requires A-Mem + Ollama installed locally.
    Uses Zettelkasten-style conceptual linking to find non-obvious connections.
    """

    def __init__(self):
        self.memory_system = None
        self._initialized = False

    def _lazy_init(self):
        if self._initialized:
            return
        try:
            amem_path = Path(os.environ.get("AMEM_PATH", str(Path.home() / ".claude/tools/A-mem")))
            venv_python = amem_path / "venv/bin/python3"
            if not venv_python.exists():
                raise Exception(f"A-Mem venv not found at {venv_python}")
            sys.path.insert(0, str(amem_path))
            # Find site-packages dynamically
            venv_lib = amem_path / "venv" / "lib"
            if venv_lib.exists():
                for sp in venv_lib.glob("python*/site-packages"):
                    sys.path.insert(0, str(sp))
                    break
            from agentic_memory.memory_system import AgenticMemorySystem
            llm_model = os.environ.get("AMEM_LLM_MODEL", "llama3.2:latest")
            self.memory_system = AgenticMemorySystem(
                model_name='all-MiniLM-L6-v2', llm_backend="ollama", llm_model=llm_model
            )
            self._initialized = True
        except Exception as e:
            logger.warning(f"A-Mem init failed: {e}")
            self._initialized = True

    async def query(self, query: str, k: int = 5) -> QueryResult:
        start = time.time()
        try:
            self._lazy_init()
            if not self.memory_system:
                raise Exception("A-Mem not initialized")
            results = self.memory_system.search_agentic(query, k=k)
            items = []
            for r in results:
                if isinstance(r, dict) and 'content' in r:
                    items.append({"content": r['content'], "id": r.get('id')})
                elif hasattr(r, 'content'):
                    items.append({"content": r.content, "id": getattr(r, 'id', None)})
            return QueryResult(layer="a-mem", items=items,
                               latency_ms=int((time.time() - start) * 1000), success=True)
        except Exception as e:
            return QueryResult(layer="a-mem", items=[],
                               latency_ms=int((time.time() - start) * 1000), success=False, error=str(e))


class EntityGraphAdapter:
    """Layer 5: Query Entity Graph (Neo4j) for structured relationships.

    Optional layer — requires Neo4j running locally.
    Searches for entities matching query terms and returns their relationships.
    """

    def __init__(self):
        self.neo4j_url = os.environ.get("NEO4J_URL", "http://localhost:7474/db/neo4j/tx/commit")
        self.auth = (
            os.environ.get("NEO4J_USER", "neo4j"),
            os.environ.get("NEO4J_PASSWORD", "neo4j")
        )

    async def query(self, query: str, k: int = 5) -> QueryResult:
        start = time.time()
        try:
            import urllib.request
            import urllib.error
            import base64

            terms = [t.lower() for t in query.split()
                     if len(t) >= 3 and t.lower() not in
                     {'the', 'and', 'for', 'are', 'was', 'were', 'with', 'this', 'that', 'from'}]
            if not terms:
                return QueryResult(layer="entity-graph", items=[],
                                   latency_ms=int((time.time() - start) * 1000), success=True)

            cypher = f"""
                MATCH (n)
                WHERE any(term IN $terms WHERE toLower(n.name) CONTAINS term)
                WITH n LIMIT 10
                OPTIONAL MATCH (n)-[r]-(related)
                RETURN labels(n)[0] as type, n.name as name,
                       collect(DISTINCT {{rel: type(r), target: related.name}})[0..3] as relationships
                LIMIT {k}
            """
            payload = json.dumps({
                "statements": [{"statement": cypher, "parameters": {"terms": terms[:3]}}]
            }).encode('utf-8')
            auth_string = base64.b64encode(f"{self.auth[0]}:{self.auth[1]}".encode()).decode()
            req = urllib.request.Request(
                self.neo4j_url, data=payload,
                headers={"Content-Type": "application/json", "Authorization": f"Basic {auth_string}"}
            )
            with urllib.request.urlopen(req, timeout=3) as response:
                data = json.loads(response.read().decode())
            items = []
            if data.get("results") and data["results"][0].get("data"):
                for row in data["results"][0]["data"]:
                    row_data = row.get("row", [])
                    if len(row_data) >= 3:
                        node_type, name, rels = row_data
                        content = f"[{node_type}] {name}"
                        if rels:
                            rel_strs = [f"{r.get('rel')}->{r.get('target')}" for r in rels if r.get('target')]
                            if rel_strs:
                                content += f" ({', '.join(rel_strs[:2])})"
                        items.append({"content": content, "type": node_type, "name": name})
            return QueryResult(layer="entity-graph", items=items,
                               latency_ms=int((time.time() - start) * 1000), success=True)
        except Exception as e:
            return QueryResult(layer="entity-graph", items=[],
                               latency_ms=int((time.time() - start) * 1000), success=False, error=str(e))


# ============================================================================
# Post-Processing Layers (6-9)
# ============================================================================

def collect_observation_ids(results: Dict[str, QueryResult]) -> List[int]:
    """Extract all observation IDs from layer results."""
    ids = set()
    for layer, result in results.items():
        if not result.success:
            continue
        for item in result.items:
            if 'observation_ids' in item:
                ids.update(item['observation_ids'])
            if 'id' in item and isinstance(item['id'], int):
                ids.add(item['id'])
    return sorted(ids)


def bitemporal_filter(obs_ids: List[int]) -> List[int]:
    """Layer 6: Filter out observations where valid_until_epoch has passed.

    Engram uses bi-temporal validity: observations have a valid_from and
    valid_until epoch. This prevents stale or superseded facts from being
    injected into context.
    """
    if not obs_ids or not DB_PATH.exists():
        return obs_ids
    try:
        now_ms = int(time.time() * 1000)
        conn = sqlite3.connect(str(DB_PATH), timeout=5)
        placeholders = ','.join('?' * len(obs_ids))
        cursor = conn.execute(f"""
            SELECT id FROM observations
            WHERE id IN ({placeholders})
              AND COALESCE(valid_from_epoch, 0) <= ?
              AND (valid_until_epoch IS NULL OR valid_until_epoch > ?)
        """, obs_ids + [now_ms, now_ms])
        valid = {row[0] for row in cursor.fetchall()}
        conn.close()
        filtered = [oid for oid in obs_ids if oid in valid]
        if len(filtered) < len(obs_ids):
            logger.info(f"BI-TEMPORAL: Filtered {len(obs_ids) - len(filtered)} expired observations")
        return filtered
    except Exception as e:
        logger.warning(f"Bi-temporal filter failed: {e}")
        return obs_ids


def rrf_rank(obs_ids: List[int], k: int = 60) -> List[int]:
    """Layer 7: Reciprocal Rank Fusion across semantic + certainty + graph signals.

    RRF is a rank aggregation method that combines multiple ranked lists.
    Each observation gets a score of 1/(k + rank) from each signal,
    and the scores are summed. This is more robust than any single ranking.
    """
    if len(obs_ids) <= 1 or not DB_PATH.exists():
        return obs_ids
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=5)
        cursor = conn.cursor()

        placeholders = ','.join('?' * len(obs_ids))

        cursor.execute(f"""
            SELECT id, certainty_level, created_at_epoch
            FROM observations WHERE id IN ({placeholders})
        """, obs_ids)
        certainty_order = {'explicit': 1, 'deductive': 2, 'inductive': 3, 'abductive': 4}
        cert_data = []
        for row in cursor.fetchall():
            oid, cert, created = row
            cert_data.append((oid, certainty_order.get(cert or 'inductive', 5), created or 0))
        cert_data.sort(key=lambda x: (x[1], -x[2]))
        cert_ranks = {oid: rank + 1 for rank, (oid, _, _) in enumerate(cert_data)}

        cursor.execute(f"""
            SELECT source_id, COUNT(*) as cnt
            FROM observation_links
            WHERE source_id IN ({placeholders})
            GROUP BY source_id
            ORDER BY cnt DESC
        """, obs_ids)
        graph_data = {row[0]: row[1] for row in cursor.fetchall()}
        graph_sorted = sorted(obs_ids, key=lambda x: graph_data.get(x, 0), reverse=True)
        graph_ranks = {oid: rank + 1 for rank, oid in enumerate(graph_sorted)}

        conn.close()

        scores = {}
        for rank, oid in enumerate(obs_ids):
            semantic_rank = rank + 1
            cert_rank = cert_ranks.get(oid, 9999)
            graph_rank = graph_ranks.get(oid, 9999)
            scores[oid] = (1.0 / (k + semantic_rank) +
                           1.0 / (k + cert_rank) +
                           1.0 / (k + graph_rank))

        ranked = sorted(scores.keys(), key=lambda x: scores[x], reverse=True)
        logger.info(f"RRF: Re-ranked {len(obs_ids)} observations by semantic+certainty+graph")
        return ranked
    except Exception as e:
        logger.warning(f"RRF ranking failed: {e}")
        return obs_ids


def retrieval_strength_boost(obs_ids: List[int]) -> List[int]:
    """Layer 8: Blend RRF position (85%) with retrieval strength (15%).

    Retrieval strength = confidence * (1 - decay_rate).
    This gives a slight edge to observations that have been repeatedly
    accessed and confirmed, modeling Hebbian learning.
    """
    if len(obs_ids) <= 2 or not DB_PATH.exists():
        return obs_ids
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=5)
        cursor = conn.cursor()
        placeholders = ','.join('?' * len(obs_ids))
        cursor.execute(f"""
            SELECT id, confidence, decay_rate
            FROM observations WHERE id IN ({placeholders})
        """, obs_ids)
        obs_data = {row[0]: (row[1], row[2]) for row in cursor.fetchall()}
        conn.close()

        scores = {}
        for i, oid in enumerate(obs_ids):
            conf, decay = obs_data.get(oid, (0.7, 0.8))
            conf = conf if conf is not None else 0.7
            decay = decay if decay is not None else 0.8
            rrf_score = 1.0 / (60 + i)
            strength = conf * (1.0 - decay)
            scores[oid] = rrf_score + 0.15 * strength

        ranked = sorted(scores.keys(), key=lambda x: scores[x], reverse=True)
        logger.info("STRENGTH: Re-weighted by retrieval strength (confidence x (1-decay))")
        return ranked
    except Exception as e:
        logger.warning(f"Strength boost failed: {e}")
        return obs_ids


def reconsolidate(obs_ids: List[int]):
    """Layer 9: Retrieval strengthens memory.

    This is the bio-inspired core of Engram: every time an observation is
    retrieved, its confidence increases slightly. Frequently-accessed memories
    become stronger over time, while unused ones fade via Ebbinghaus decay
    (applied separately in nightly consolidation).

    The confidence bump depends on certainty level:
    - explicit facts: +0.02 (strong reinforcement)
    - deductive: +0.015
    - inductive: +0.01
    - abductive: +0.005 (weakest — speculative memories grow slowly)
    """
    if not obs_ids or not DB_PATH.exists():
        return
    try:
        now_ms = int(time.time() * 1000)
        id_list = ','.join(str(oid) for oid in obs_ids)
        conn = sqlite3.connect(str(DB_PATH), timeout=5)
        conn.execute("PRAGMA busy_timeout = 3000")
        conn.execute("BEGIN EXCLUSIVE")
        conn.execute(f"""
            UPDATE observations
            SET last_accessed_epoch = {now_ms},
                confidence = MIN(
                  COALESCE(confidence, 0.7) +
                  CASE COALESCE(certainty_level, 'inductive')
                    WHEN 'explicit' THEN 0.02
                    WHEN 'deductive' THEN 0.015
                    WHEN 'inductive' THEN 0.01
                    ELSE 0.005
                  END,
                  1.0
                )
            WHERE id IN ({id_list})
            AND superseded_by IS NULL
        """)
        conn.execute("COMMIT")
        conn.close()
        logger.info(f"RECONSOLIDATION: Updated access time + confidence for {len(obs_ids)} observations")
    except Exception as e:
        logger.warning(f"Reconsolidation failed: {e}")


def log_context_fetch(query: str, obs_ids: List[int]):
    """Utility: Record what was fetched for later analysis."""
    if not DB_PATH.exists():
        return
    try:
        from datetime import datetime, timezone
        session_id = os.environ.get("CLAUDE_SESSION_ID", "unknown")
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        now_epoch = int(time.time())
        query_snippet = query[:100].replace("'", "''")
        id_str = ','.join(str(oid) for oid in obs_ids)
        conn = sqlite3.connect(str(DB_PATH), timeout=5)
        conn.execute("""
            INSERT INTO context_fetches (
                claude_session_id, query, tool_name, topics_detected,
                observation_ids, observation_count, created_at, created_at_epoch,
                trigger_type, trigger_context, outcome_signal
            ) VALUES (?, ?, 'superbrain-full', NULL, ?, ?, ?, ?, 'auto-superbrain',
                      'Superbrain multi-layer injection', 'pending')
        """, (session_id, query_snippet, id_str, len(obs_ids), now, now_epoch))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning(f"Context fetch logging failed: {e}")


def search_file_index(query: str) -> str:
    """Search semantic file index for relevant vault files."""
    vault_dir = os.environ.get("ENGRAM_VAULT_DIR", str(Path.home() / "Documents" / "vault"))
    file_index = Path(vault_dir) / "semantic-file-index.md"

    if not file_index.exists():
        return ""

    try:
        stop_words = {'the', 'a', 'an', 'is', 'are', 'was', 'were', 'be', 'been', 'have', 'has',
                      'do', 'does', 'did', 'will', 'would', 'could', 'should', 'can', 'may',
                      'to', 'of', 'in', 'for', 'on', 'with', 'at', 'by', 'from', 'as', 'or',
                      'and', 'but', 'if', 'then', 'so', 'than', 'that', 'this', 'it', 'its',
                      'what', 'where', 'when', 'how', 'why', 'which', 'who', 'find', 'show',
                      'get', 'help', 'me', 'please'}
        terms = [w.lower() for w in re.sub(r'[^a-zA-Z ]', '', query).split()
                 if len(w) >= 3 and w.lower() not in stop_words][:5]
        if not terms:
            return ""

        pattern = re.compile('|'.join(re.escape(t) for t in terms), re.IGNORECASE)
        matches = []
        with open(file_index, 'r') as f:
            for line in f:
                if pattern.search(line):
                    matches.append(line.strip()[:200])
                    if len(matches) >= 5:
                        break

        if matches:
            result = "\n\n**Relevant files from index:**\n"
            for m in matches:
                result += f"* {m}\n"
            return result
    except Exception as e:
        logger.warning(f"File index search failed: {e}")
    return ""


# ============================================================================
# Query Orchestrator
# ============================================================================

class SuperbrainOrchestrator:
    """Coordinates parallel queries across all memory layers + post-processing."""

    SLOW_TIER_TIMEOUT_SECS = 5.0

    FAST_TIER = ["claude-mem", "entity-graph", "chromadb"]
    SLOW_TIER = ["honcho", "a-mem"]

    def __init__(self):
        self.honcho = HonchoAdapter()
        self.claude_mem = ClaudeMemAdapter()
        self.amem = AMemAdapter()
        self.entity_graph = EntityGraphAdapter()
        self.chromadb = ChromaDBAdapter()

        self._adapters = {
            "honcho": self.honcho,
            "claude-mem": self.claude_mem,
            "a-mem": self.amem,
            "entity-graph": self.entity_graph,
            "chromadb": self.chromadb
        }

    def _query_with_timeout(self, adapter, name: str, query: str, k: int, timeout: float) -> QueryResult:
        import concurrent.futures

        def sync_query():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                return loop.run_until_complete(adapter.query(query, k))
            finally:
                loop.close()

        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            future = executor.submit(sync_query)
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            return QueryResult(layer=name, items=[], latency_ms=int(timeout * 1000),
                               success=False, error=f"Timeout after {timeout}s")
        except Exception as e:
            return QueryResult(layer=name, items=[], latency_ms=0, success=False, error=str(e))
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    async def query(self, query: str, k: int = 5, skip_reconsolidation: bool = False) -> SynthesizedContext:
        """Full 9-layer pipeline."""
        start = time.time()

        # Layers 1-5: Parallel data source queries
        all_results = []

        fast_tasks = [
            asyncio.to_thread(self._query_with_timeout, self._adapters[name], name, query, k, 10.0)
            for name in self.FAST_TIER
        ]
        fast_results = await asyncio.gather(*fast_tasks, return_exceptions=True)
        for i, result in enumerate(fast_results):
            if isinstance(result, Exception):
                all_results.append(QueryResult(layer=self.FAST_TIER[i], items=[], latency_ms=0,
                                               success=False, error=str(result)))
            else:
                all_results.append(result)

        if not FAST_TIER_ONLY_FLAG.exists():
            slow_tasks = [
                asyncio.to_thread(self._query_with_timeout, self._adapters[name], name, query, k,
                                  self.SLOW_TIER_TIMEOUT_SECS)
                for name in self.SLOW_TIER
            ]
            slow_results = await asyncio.gather(*slow_tasks, return_exceptions=True)
            for i, result in enumerate(slow_results):
                if isinstance(result, Exception):
                    all_results.append(QueryResult(layer=self.SLOW_TIER[i], items=[], latency_ms=0,
                                                   success=False, error=str(result)))
                else:
                    all_results.append(result)

        raw_results = {r.layer: r for r in all_results}

        # Layer 6: Bi-temporal filter
        obs_ids = collect_observation_ids(raw_results)
        obs_ids = bitemporal_filter(obs_ids)

        # Layer 7: RRF ranking
        obs_ids = rrf_rank(obs_ids)

        # Layer 8: Retrieval strength boost
        obs_ids = retrieval_strength_boost(obs_ids)

        # Layer 9: Reconsolidation
        if not skip_reconsolidation and obs_ids:
            reconsolidate(obs_ids)

        if obs_ids:
            log_context_fetch(query, obs_ids)

        synthesized = self._synthesize(all_results)
        total_latency_ms = int((time.time() - start) * 1000)

        return SynthesizedContext(
            layers_queried=len(all_results),
            layers_succeeded=sum(1 for r in all_results if r.success),
            total_latency_ms=total_latency_ms,
            context_items=synthesized,
            raw_results=raw_results,
            observation_ids=obs_ids
        )

    def _synthesize(self, results: List[QueryResult]) -> List[Dict[str, Any]]:
        """Priority-based synthesis with deduplication."""
        priority = {"claude-mem": 5, "chromadb": 4, "a-mem": 3, "honcho": 2, "entity-graph": 1}
        all_items = []
        for result in results:
            if result.success and result.items:
                for item in result.items:
                    all_items.append({
                        "layer": result.layer,
                        "priority": priority.get(result.layer, 0),
                        "content": item.get("content", str(item)),
                        "metadata": {k: v for k, v in item.items() if k != "content"}
                    })
        all_items.sort(key=lambda x: x["priority"], reverse=True)

        synthesized = []
        seen_content = set()
        for item in all_items:
            content_key = item["content"][:100]
            if content_key not in seen_content:
                synthesized.append(item)
                seen_content.add(content_key)
                if len(synthesized) >= 10:
                    break
        return synthesized


# ============================================================================
# Output Formatters
# ============================================================================

def format_hook_output(context: SynthesizedContext, query: str) -> str:
    """Format for gap-detector hook injection."""
    lines = []
    lines.append("**Engram context** (multi-layer synthesis):")
    lines.append("")
    lines.append(f"Queried {context.layers_queried} layers, {context.layers_succeeded} succeeded ({context.total_latency_ms}ms)")
    lines.append("")

    for layer, result in context.raw_results.items():
        status = "+" if result.success else "x"
        count = len(result.items) if result.success else 0
        msg = f"{result.latency_ms}ms" if result.success else (result.error or "failed")[:40]
        lines.append(f"  {status} {layer}: {count} results ({msg})")

    lines.append("")

    for i, item in enumerate(context.context_items, 1):
        layer_tag = f"[{item['layer']}]"
        content = item['content'][:200]
        lines.append(f"{i}. {layer_tag} {content}")
        lines.append("")

    if not context.context_items:
        lines.append("(No context items found)")

    file_context = search_file_index(query)
    if file_context:
        lines.append(file_context)

    return "\n".join(lines)


def format_json_output(context: SynthesizedContext) -> str:
    """Format for programmatic consumption."""
    return json.dumps({
        "layers_queried": context.layers_queried,
        "layers_succeeded": context.layers_succeeded,
        "total_latency_ms": context.total_latency_ms,
        "observation_ids": context.observation_ids,
        "context_items": [
            {
                "layer": item["layer"],
                "content": item["content"][:500],
                "metadata": item.get("metadata", {})
            }
            for item in context.context_items
        ],
        "layer_details": {
            layer: {
                "success": result.success,
                "item_count": len(result.items),
                "latency_ms": result.latency_ms,
                "error": result.error
            }
            for layer, result in context.raw_results.items()
        }
    }, indent=2)


# ============================================================================
# CLI Interface
# ============================================================================

async def main():
    import argparse

    parser = argparse.ArgumentParser(description="Engram multi-layer memory query")
    parser.add_argument("query", nargs="+", help="Query string")
    parser.add_argument("--full", action="store_true",
                        help="Wait for all layers (no timeout)")
    parser.add_argument("--timeout", type=float, default=5.0,
                        help="Slow tier timeout in seconds (default: 5.0)")
    parser.add_argument("--search-only", action="store_true",
                        help="Skip reconsolidation (for benchmarks)")
    parser.add_argument("--json", action="store_true",
                        help="Output structured JSON")

    args = parser.parse_args()
    query = " ".join(args.query)

    orchestrator = SuperbrainOrchestrator()

    if args.timeout != 5.0:
        orchestrator.SLOW_TIER_TIMEOUT_SECS = args.timeout

    context = await orchestrator.query(
        query, k=5,
        skip_reconsolidation=args.search_only
    )

    if args.json:
        print(format_json_output(context))
    else:
        print(format_hook_output(context, query))


if __name__ == "__main__":
    asyncio.run(main())
