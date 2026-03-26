# Engram

> **A note on this repo (March 2026):** This is a diary of one person's experiments with AI memory and context management, not a finished product ready for you to go try. Think of it as a documentary record — what I tested, what worked, what didn't, and what I learned. Other agents and researchers are welcome to explore it as a case study. The architecture described below (push-model hook injection) has since been superseded by a pull-model approach — see [Status Update](#status-update-march-2026) below.

[Note from Gene: Bibliography etc pending; This is my heavily customized fork of claude-mem, which I credit for sending me down the rabbit hole of memory and context management systems. The below is entirely written by Claude, I figured it should get to name the thing and decide how to describe it. But I notice the description is rather...lofty...so I added this editorial note 😉)

**Persistent memory infrastructure for Claude Code.**

---

Engram is a memory system that runs underneath [Claude Code](https://docs.anthropic.com/en/docs/claude-code), giving it persistent recall across sessions. When you start a new conversation, Claude doesn't start from zero — Engram has already pre-loaded relevant context from past sessions. When you type a prompt, a hook silently searches multiple memory backends and injects relevant observations before Claude even sees your message.

The result: an AI that remembers your projects, your preferences, your decisions, and the hard-won lessons from past debugging sessions.

## How it works

```
You type a prompt
       │
       ▼
┌─────────────────┐
│  gap-detector.sh │ ◄── Smart router (UserPromptSubmit hook)
└────────┬────────┘
         │
    ┌────┴────┐
    ▼         ▼
  LITE       FULL
  ~159ms     ~1.2s
    │         │
    │    ┌────┼────────┐
    │    ▼    ▼        ▼
    │  SQLite ChromaDB Neo4j
    │  (FTS5) (vectors) (graph)
    │    │    │        │
    │    └────┼────────┘
    │         ▼
    │    RRF Fusion
    │    + Bi-temporal filter
    │    + Strength boost
    │    + Reconsolidation
    │         │
    └────┬────┘
         ▼
  Injected as context
  (before Claude sees your prompt)
```

**80% of prompts** take the LITE path (~159ms, keyword search only).
**20%** trigger FULL mode (~1.2s, multi-backend fusion) — when you ask recall questions, complex queries, or architecture-level things.

## What makes it interesting

**Bio-inspired memory dynamics.** Observations aren't static records — they have confidence scores that evolve over time. Frequently-accessed memories get stronger (Hebbian learning). Unused memories fade (Ebbinghaus decay). Every retrieval triggers reconsolidation, strengthening the retrieved memory. This means your most useful knowledge naturally floats to the top.

**Multi-layer retrieval with Reciprocal Rank Fusion.** Three backends search in parallel: SQLite FTS5 (keyword match), ChromaDB (semantic similarity), and Neo4j (entity-relationship graph). Results are fused using RRF — a rank aggregation method that's more robust than any single scoring function. You find things even when your query uses different words than the original observation.

**Bi-temporal validity.** Observations have `valid_from` and `valid_until` timestamps. When a fact gets superseded (e.g., "the API endpoint moved"), the old observation is automatically filtered out. You never get injected with stale context.

**Anticipatory pre-loading.** Before you even type your first prompt, the system predicts what you'll need based on time-of-day patterns and recent session history. Morning sessions pre-load task and briefing context. Evening sessions pre-load development and architecture context. A novelty check prevents duplicate injection.

**Smart routing.** Not every prompt needs a 1.2-second multi-backend search. The gap detector analyzes prompt complexity and only triggers FULL mode for recall questions ("last time we..."), complex queries (>12 words with `?`), technical terms, or architecture discussions.

## Architecture

Open [`docs/architecture.html`](docs/architecture.html) in your browser for the full interactive diagram.

### Storage backends

| Backend | What it stores | Query speed |
|---------|---------------|-------------|
| **SQLite + FTS5** | All observations, full-text indexed. Hebbian strength scores and Ebbinghaus decay. | ~159ms |
| **ChromaDB** | Semantic embedding vectors. Finds conceptually related observations even when keywords don't match. | ~634ms cold, ~0ms cached |
| **Neo4j** | Entity-relationship graph. People, projects, concepts connected with typed edges. | ~200ms |

### Optional layers

| Layer | What it adds | Requirement |
|-------|-------------|-------------|
| **A-Mem** | Zettelkasten-style conceptual linking via Ollama | Local Ollama + A-Mem |
| **Honcho** | Strategic/theory-of-mind reasoning | Honcho API key |

Both require extra infrastructure. Set `FAST_TIER_ONLY` to skip them (runs core tiers only — FTS5, ChromaDB, Neo4j).

### Session lifecycle

| Phase | What happens |
|-------|-------------|
| **Session start** | Anticipatory loader pre-loads ~25 observations. Context index injects ~50 recent observations. CLAUDE.md and MEMORY.md loaded. Skills synced from vault. |
| **Every prompt** | Gap detector routes to LITE or FULL pipeline. 5-8 observations injected as invisible context. |
| **On demand** | Claude calls MCP tools (`search`, `timeline`, `get_observations`) when automatic injection isn't enough. |
| **Session end** | Skills pushed back to vault. Observations persisted. |
| **Overnight** | Hebbian learning strengthens active memories. Ebbinghaus decay fades stale ones. |

## Setup

### Prerequisites

- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) installed
- [claude-mem](https://github.com/thedotmack/claude-mem) plugin installed (`claude plugin install claude-mem`)
- Python 3.10+ (for the scripts)
- `jq` (for the shell hooks)

### Optional
- [Neo4j](https://neo4j.com/) for the entity graph layer
- [Ollama](https://ollama.ai/) + A-Mem for conceptual linking
- An Obsidian vault (for the knowledge base — any markdown folder works)

### Install

```bash
# 1. Clone the repo
git clone https://github.com/your-username/engram.git
cd engram

# 2. Create target directories and copy scripts/hooks
mkdir -p ~/.claude/scripts ~/.claude/hooks ~/.claude-mem/logs
cp scripts/*.py ~/.claude/scripts/
cp scripts/*.sh ~/.claude/scripts/
cp hooks/*.sh ~/.claude/hooks/

# 3. Make hooks executable
chmod +x ~/.claude/hooks/*.sh
chmod +x ~/.claude/scripts/*.sh

# 4. Configure environment (edit with your values)
cp config/.env.example ~/.env

# 5. Set up vault (optional — or point to your existing one)
cp -r vault-example/ ~/Documents/vault/

# 6. Add hooks to Claude Code settings
# Merge config/settings-example.json into ~/.claude/settings.json
# (or copy if you don't have existing hooks)
```

### Verify

```bash
# Check that the claude-mem worker is running
curl -s http://127.0.0.1:37777/api/health

# Test the lite pipeline
python3 ~/.claude/scripts/superbrain_query.py "test query" --lite

# Test the full pipeline (if Neo4j/ChromaDB are configured)
python3 ~/.claude/scripts/superbrain_query.py "test query" --json
```

## The vault

Engram works best with a markdown knowledge vault (we use Obsidian, but any folder of `.md` files works). The vault is the **source of truth** for curated knowledge — the human-written encyclopedia that overrides AI-captured observations when they conflict.

See [`vault-example/`](vault-example/) for the recommended structure.

### Multi-agent access

If you run multiple Claude agents (local, cloud, mobile), the vault is the shared brain:

```
Agent A (local)          Agent B (cloud)         Agent C (mobile)
     │                        │                       │
     │  full system access    │  own claude-mem        │  read-only
     │  (all backends)        │  (separate DB)         │  (via GitHub)
     │                        │                        │
     └────────┬───────────────┘────────────────────────┘
              │
        Vault (GitHub)
        ═══════════════
        Shared knowledge base
        synced via git push/pull
```

Each agent has its own `claude-mem` database (observations don't sync between agents). But the vault — Encyclopedia articles, Skills, Journals — syncs via Git. The `sync-skills.sh` hook handles this automatically at session start/stop.

## File reference

```
engram/
├── hooks/
│   ├── gap-detector.sh          # Smart routing: LITE vs FULL per prompt
│   ├── chrome-db-guard.sh       # Safety: blocks Chrome DB access (CrowdStrike)
│   └── email-send-guard.sh      # Safety: blocks accidental email sends
├── scripts/
│   ├── superbrain_query.py      # Unified multi-layer query pipeline (LITE + FULL modes)
│   ├── anticipatory-loader.py   # Session-start context pre-loading
│   └── sync-skills.sh           # Vault ↔ local skill sync
├── config/
│   ├── .env.example             # Environment variable template
│   └── settings-example.json    # Claude Code hook configuration
├── vault-example/               # Sample knowledge vault structure
│   ├── CLAUDE.md
│   ├── Encyclopedia/
│   ├── Daily/
│   ├── Claude Journal/
│   ├── Skills/
│   └── Agents/
├── docs/
│   └── architecture.html        # Interactive architecture diagram
├── LICENSE                      # MIT
└── README.md                    # You are here
```

## How we built this

This system was built iteratively over several months by a human and an AI working together daily. It started as a simple observation logger and evolved through real use into a multi-layer retrieval system with bio-inspired memory dynamics.

Key design decisions were driven by real needs:
- **Smart routing** exists because full-pipeline latency (1.2s) was too slow for casual prompts
- **Bi-temporal validity** exists because superseded facts caused real debugging confusion
- **Reconsolidation** exists because important memories kept falling out of search results
- **The anticipatory loader** exists because ADHD means context needs to be handed to you, not searched for

The name "Engram" comes from neuroscience — it's the hypothesized physical trace a memory leaves in the brain. Karl Lashley spent decades searching for them. We're building them in silicon.

## Dependencies

| Dependency | Role | License |
|------------|------|---------|
| [claude-mem](https://github.com/thedotmack/claude-mem) | Observation capture, storage, MCP tools | AGPL-3.0 |
| [ChromaDB](https://github.com/chroma-core/chroma) | Semantic vector database | Apache-2.0 |
| [Neo4j](https://neo4j.com/) | Entity-relationship graph (optional) | GPL-3.0 (Community) |
| [Ollama](https://ollama.ai/) | Local LLM for A-Mem layer (optional) | MIT |

## Related Projects

Part of a modular toolkit for Claude Code power users:

| Repo | What It Covers |
|------|---------------|
| **[engram](https://github.com/gene-jelly/engram)** | Persistent memory infrastructure (you are here) |
| **[browsing-all-you-need](https://github.com/gene-jelly/browsing-all-you-need)** | Browser automation — Playwright, Chrome DevTools, Claude-in-Chrome |
| **[credentials-all-you-need](https://github.com/gene-jelly/credentials-all-you-need)** | Secrets and credential management |

## Status Update: March 2026

**The push model is retired.** After running the gap-detector hook (inject context on every prompt) for several months, I disabled the entire system on March 22, 2026 when I learned that custom context injection busts Claude Code's prompt caching — meaning every session was potentially slower and more expensive than vanilla.

After 4 days without it, Claude noticeably "forgot" things between sessions. So I ran a design council (three AI personas simulating Simon Willison, Linus Lee, and Dan Shipper) to evaluate the options. Their unanimous recommendation: **keep the database, kill the hook, make retrieval a pull tool.**

**Current architecture (Path C):**
- **Tier 1 — Static preamble:** A dense MEMORY.md file that Claude always has. Carries personality, preferences, project context. Prompt-cache friendly because it doesn't change between turns.
- **Tier 2 — On-demand retrieval:** The claude-mem SQLite database (11K+ observations) remains available as an MCP tool Claude can call when it needs to recall something specific. No hook, no automatic injection.

The gap-detector's smart routing (lite vs full, FTS5 vs multi-backend fusion) was clever engineering but solved the wrong problem. Most prompts are continuations that don't need injected context. The few that do benefit from recall are better served by letting the model decide when to search.

**Lessons learned:**
1. Push-model context injection is an anti-pattern for prompt-cached LLM systems
2. Personality continuity comes from a dense static preamble, not dynamic retrieval
3. 11K observations are valuable as a searchable corpus, not as a firehose
4. The maintenance overhead of custom hooks compounds — every Claude Code update is a potential breakage point

This repo remains as documentation of the push-model experiment. The architecture diagrams and bio-inspired memory dynamics (Hebbian learning, Ebbinghaus decay, reconsolidation) are still interesting ideas — they just belong in a pull-model implementation, not a hook.

## License

MIT. See [LICENSE](LICENSE).

Built by [Gene Jelly](https://github.com/gene-jelly) and Claude.
