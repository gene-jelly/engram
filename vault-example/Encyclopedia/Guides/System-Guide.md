# System Guide

How Engram's memory system works.

## Overview

Engram gives Claude Code persistent memory across sessions. Instead of starting fresh each time, the AI remembers past conversations, decisions, and discoveries.

## How Memory Flows

### Session Start
1. **Anticipatory loader** pre-loads ~25 observations from the database, using time-of-day patterns and recency
2. **Context index** (from claude-mem plugin) injects ~50 recent observations
3. **CLAUDE.md + MEMORY.md** provide static context (policies, system facts)
4. **Skills** are synced from the vault

### Every Prompt
1. **Gap detector** analyzes your prompt and decides: LITE or FULL search?
2. **LITE** (~80% of prompts, ~159ms): FTS5 keyword search only
3. **FULL** (~20%): FTS5 + ChromaDB vectors + Neo4j graph, fused with RRF
4. Results injected as invisible context before Claude sees your message

### On Demand
- Claude can call MCP tools: `search`, `timeline`, `get_observations`
- Claude can update MEMORY.md with new persistent facts

### Nightly
- **Hebbian learning**: Frequently-accessed observations get stronger
- **Ebbinghaus decay**: Unused observations fade over time

## Storage Backends

| Backend | What It Stores | Speed |
|---------|---------------|-------|
| SQLite (FTS5) | All observations, full-text indexed | ~159ms |
| ChromaDB | Semantic vectors for similarity search | ~634ms cold, ~0ms cached |
| Neo4j | Entity-relationship graph | ~200ms |

## Key Concepts

- **Observation**: A unit of memory (a fact, decision, discovery, bug fix, etc.)
- **Bi-temporal validity**: Observations have valid_from and valid_until — stale facts are automatically filtered
- **Reconsolidation**: Every retrieval strengthens the retrieved memory (bio-inspired)
- **RRF (Reciprocal Rank Fusion)**: Combines rankings from multiple search backends
