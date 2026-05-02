"""Deterministic synthetic dataset generator for graph store benchmarks.

Generates:
  - 100,000 message_versions (synthetic, no real message content)
  - 50,000 derived triples extracted via a rule-based extractor (no LLM)

All outputs are written to bench/graph-store/data/ as JSONL files:
  - message_versions.jsonl — one JSON object per line
  - triples.jsonl          — one JSON object per line

Determinism guarantee: given the same BENCH_SEED, this script always produces
the same output. Set BENCH_SEED in the environment to override the default (42).

Usage:
    python bench/graph-store/seed.py
    python bench/graph-store/seed.py --seed 99 --output-dir /tmp/bench-data
    python bench/graph-store/seed.py --message-count 1000 --triple-count 500  # smoke test
"""

from __future__ import annotations

import argparse
import json
import os
import random
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_SEED: int = int(os.environ.get("BENCH_SEED", "42"))
DEFAULT_MESSAGE_COUNT: int = 100_000
DEFAULT_TRIPLE_COUNT: int = 50_000
DEFAULT_OUTPUT_DIR: str = str(Path(__file__).parent / "data")

# Predicate vocabulary from Phase 10 §2 candidate edge types.
PREDICATES: tuple[str, ...] = (
    "MENTIONS",
    "AUTHORED",
    "KNOWS_ABOUT",
    "RELATED_TO",
    "SUPPORTS",
    "DERIVED_FROM",
    "PART_OF",
    "DECIDED",
    "ASKED",
    "ANSWERED",
    "CONTRADICTS",
    "SUPERSEDES",
)

# Entity vocabulary: 2,000 named entities (persons, topics, projects, decisions).
# Generated deterministically so the power-law degree distribution is reproducible.
ENTITY_TYPES: tuple[str, ...] = ("Person", "Topic", "Project", "Decision", "Question")

# First-name / topic word lists for synthetic entity name generation.
_FIRST_NAMES: tuple[str, ...] = (
    "Alice", "Bob", "Carol", "Dave", "Eve", "Frank", "Grace", "Heidi",
    "Ivan", "Judy", "Karl", "Laura", "Mallory", "Niaj", "Olivia", "Peggy",
    "Quinn", "Rupert", "Sybil", "Trent", "Uma", "Victor", "Wendy", "Xavier",
    "Yvonne", "Zara",
)
_TOPIC_WORDS: tuple[str, ...] = (
    "Architecture", "Testing", "Deployment", "Security", "Performance",
    "Privacy", "Governance", "Memory", "Search", "Extraction", "Catalog",
    "Graph", "Butler", "Onboarding", "Moderation", "Vouching", "Intro",
    "Digest", "Observation", "Wiki", "Phase", "Sprint", "Release", "Migration",
    "Schema", "Index", "Cascade", "Tombstone", "Ingestion", "Replay",
)


def _build_entity_vocab(rng: random.Random, size: int = 2_000) -> list[dict]:
    """Build a deterministic vocabulary of named entities.

    Returns a list of dicts: {"id": str, "type": str, "name": str}.
    Size split: 40% Person, 30% Topic, 15% Project, 10% Decision, 5% Question.
    """
    entities: list[dict] = []
    type_counts = {
        "Person": int(size * 0.40),
        "Topic": int(size * 0.30),
        "Project": int(size * 0.15),
        "Decision": int(size * 0.10),
        "Question": size - int(size * 0.40) - int(size * 0.30) - int(size * 0.15) - int(size * 0.10),
    }
    for entity_type, count in type_counts.items():
        for i in range(count):
            if entity_type == "Person":
                first = rng.choice(_FIRST_NAMES)
                last = rng.choice(_TOPIC_WORDS)
                name = f"{first} {last}"
            elif entity_type in ("Topic", "Project", "Decision"):
                word1 = rng.choice(_TOPIC_WORDS)
                word2 = rng.choice(_TOPIC_WORDS)
                name = f"{word1}-{word2}-{i}"
            else:
                word = rng.choice(_TOPIC_WORDS)
                name = f"Q: What is {word} {i}?"
            entities.append({
                "id": f"{entity_type.lower()}_{i:04d}",
                "type": entity_type,
                "name": name,
            })
    rng.shuffle(entities)
    return entities


@dataclass
class MessageVersion:
    """Minimal synthetic message_version row for benchmark purposes.

    No real message content is stored — text is a placeholder string that
    identifies the row for triple extraction without containing any user data.
    """
    id: str               # UUID
    chat_id: int
    user_id: int
    normalized_text: str  # Synthetic placeholder — not real content
    content_hash: str     # SHA-256 hex of normalized_text (simulated)
    created_at: str       # ISO 8601 timestamp


@dataclass
class Triple:
    """A graph triple derived from a message_version.

    subject and object are entity IDs from the entity vocabulary.
    predicate is from the PREDICATES tuple.
    source_message_version_id ties back to a MessageVersion.
    """
    id: str                          # UUID for this triple
    subject: str                     # entity id
    predicate: str
    object: str                      # entity id
    source_message_version_id: str   # UUID of the originating MessageVersion
    confidence: float                # 0.0–1.0, deterministic from rng
    projection_run_id: int           # constant 1 for benchmark dataset


def _synthetic_hash(text: str, seq: int) -> str:
    """Deterministic fake hash for benchmark rows (not a real SHA-256)."""
    return f"hash_{seq:08x}"


def _iso_ts(base_ts: float, offset_seconds: int) -> str:
    """Return an ISO 8601 timestamp string."""
    import datetime
    dt = datetime.datetime.utcfromtimestamp(base_ts + offset_seconds)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def generate(
    seed: int = DEFAULT_SEED,
    message_count: int = DEFAULT_MESSAGE_COUNT,
    triple_count: int = DEFAULT_TRIPLE_COUNT,
    output_dir: str = DEFAULT_OUTPUT_DIR,
) -> None:
    """Generate synthetic dataset and write to output_dir.

    Triple extraction rule (deterministic, no LLM):
    - For each triple, pick two random entities from the vocabulary and a random predicate.
    - Assign to a random source message_version.
    - Power-law degree distribution is emergent from random assignment with a zipf-weighted
      entity sampler: a small fraction of entities (~5%) are chosen 10x more often than
      others.
    """
    rng = random.Random(seed)
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    print(f"[seed.py] seed={seed}, messages={message_count}, triples={triple_count}, output={out}")

    # Build entity vocabulary
    entities = _build_entity_vocab(rng)
    entity_ids = [e["id"] for e in entities]

    # Power-law weight: 5% of entities get 10x weight
    n_hub = max(1, len(entity_ids) // 20)
    weights = [10.0] * n_hub + [1.0] * (len(entity_ids) - n_hub)
    # Shuffle so hubs are not all the first entities
    paired = list(zip(entity_ids, weights))
    rng.shuffle(paired)
    entity_ids_weighted = [p[0] for p in paired]
    entity_weights = [p[1] for p in paired]

    # Base timestamp: 2026-01-01T00:00:00Z
    base_ts = 1767225600.0  # 2026-01-01 UTC

    # --- Generate message_versions ---
    print(f"[seed.py] generating {message_count} message_versions ...")
    mv_ids: list[str] = []
    mv_path = out / "message_versions.jsonl"
    t0 = time.perf_counter()
    with mv_path.open("w") as f:
        # Use a fixed UUID namespace for reproducibility
        ns = uuid.UUID("12345678-1234-5678-1234-567812345678")
        for i in range(message_count):
            mv_id = str(uuid.uuid5(ns, f"mv_{seed}_{i}"))
            mv_ids.append(mv_id)
            mv = MessageVersion(
                id=mv_id,
                chat_id=rng.randint(1, 50),
                user_id=rng.randint(1, 500),
                normalized_text=f"bench_msg_{i:07d}",  # no real content
                content_hash=_synthetic_hash(f"bench_msg_{i}", i),
                created_at=_iso_ts(base_ts, i * 60),  # 1 minute apart
            )
            f.write(json.dumps(asdict(mv)) + "\n")
    elapsed = time.perf_counter() - t0
    print(f"[seed.py] message_versions done in {elapsed:.2f}s → {mv_path}")

    # --- Generate triples ---
    print(f"[seed.py] generating {triple_count} triples ...")
    triples_path = out / "triples.jsonl"
    t0 = time.perf_counter()
    with triples_path.open("w") as f:
        triple_ns = uuid.UUID("87654321-4321-8765-4321-876543218765")
        for i in range(triple_count):
            # Pick subject and object with power-law weights
            subject_id = rng.choices(entity_ids_weighted, weights=entity_weights, k=1)[0]
            # Object must differ from subject
            while True:
                object_id = rng.choices(entity_ids_weighted, weights=entity_weights, k=1)[0]
                if object_id != subject_id:
                    break
            predicate = rng.choice(PREDICATES)
            source_mv_id = rng.choice(mv_ids)
            confidence = round(rng.uniform(0.5, 1.0), 3)
            triple_id = str(uuid.uuid5(triple_ns, f"triple_{seed}_{i}"))
            t = Triple(
                id=triple_id,
                subject=subject_id,
                predicate=predicate,
                object=object_id,
                source_message_version_id=source_mv_id,
                confidence=confidence,
                projection_run_id=1,
            )
            f.write(json.dumps(asdict(t)) + "\n")
    elapsed = time.perf_counter() - t0
    print(f"[seed.py] triples done in {elapsed:.2f}s → {triples_path}")

    # Write entities for reference (not loaded into graph directly — triples carry entity ids)
    entities_path = out / "entities.jsonl"
    with entities_path.open("w") as f:
        for e in entities:
            f.write(json.dumps(e) + "\n")
    print(f"[seed.py] entities written → {entities_path}")

    # Write metadata
    meta = {
        "seed": seed,
        "message_count": message_count,
        "triple_count": triple_count,
        "entity_count": len(entities),
        "predicates": list(PREDICATES),
    }
    meta_path = out / "seed_meta.json"
    meta_path.write_text(json.dumps(meta, indent=2))
    print(f"[seed.py] metadata written → {meta_path}")
    print("[seed.py] done.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate synthetic benchmark dataset")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--message-count", type=int, default=DEFAULT_MESSAGE_COUNT)
    parser.add_argument("--triple-count", type=int, default=DEFAULT_TRIPLE_COUNT)
    parser.add_argument("--output-dir", type=str, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    generate(
        seed=args.seed,
        message_count=args.message_count,
        triple_count=args.triple_count,
        output_dir=args.output_dir,
    )


if __name__ == "__main__":
    main()
