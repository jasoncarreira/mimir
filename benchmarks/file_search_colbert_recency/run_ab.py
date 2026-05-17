"""3-arm A/B runner for chainlink #141 Slice 2.

Measures file_search@10 directly across three retrieval configurations:

  Arm A:  no ColBERT (legacy BM25 + dense weighted-sum path)
  Arm B:  ColBERT-fused RRF, alpha=0.0  (= PR #184 as shipped)
  Arm C:  ColBERT-fused RRF, alpha=0.3  (post-RRF recency multiplier)

Inputs:
  --home PATH        MIMIR_HOME (default: $MIMIR_HOME or /mimir-home)
  --probes PATH      probes JSON (default: ./probes.json)
  --out PATH         results JSON dump (default: ./results.json)
  --k INT            top-k per probe (default: 10)
  --skip-build       reuse the existing .colbert-index/ instead of rebuilding

The ColBERT sidecar is built once at the start; Arms B and C reuse it.
Arm A uses a ``_DisabledColBERTChannel`` so it gets the legacy
weighted-sum branch.

Embeddings route through saga's configured provider (voyage by
default per saga.toml). The BM25 + dense candidate set comes from
the existing ``<home>/.mimir/index.db``. If that DB is missing
this script bails — re-build via ``mimir setup`` or your usual
ingest path before running.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

# Bootstrap the package path when invoked as ``python run_ab.py``.
_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mimir.search import (  # noqa: E402
    ColBERTHit,
    Indexer,
    SagaProviderEmbedder,
)


log = logging.getLogger("colbert-ab")


# ---------------------------------------------------------------------------
# Channels
# ---------------------------------------------------------------------------


class _DisabledColBERTChannel:
    """Arm A: ColBERT off. The Indexer treats an empty hit list as
    "no third channel"; the no-ColBERT branch keeps the legacy
    weighted-sum scoring intact.
    """

    def search(self, query: str, k: int = 10) -> list[ColBERTHit]:
        return []


class _ColBERTChannelAdapter:
    """Wraps an opened ``ColBERTIndex`` so its ``.search()`` shape
    matches the ``ColBERTChannel`` protocol the Indexer expects.
    Identical to ``mimir.search._LazyColBERTChannel`` minus the
    lazy-probe — we pre-build and pre-open the index once at runner
    startup.
    """

    def __init__(self, idx):
        self._idx = idx

    def search(self, query: str, k: int = 10) -> list[ColBERTHit]:
        raw = self._idx.search(query, k=k)
        return [
            ColBERTHit(path=row[0].path, chunk_no=row[0].chunk_no, score=row[1])
            for row in raw
        ]


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def _hit_rank(returned_paths: list[str], expected_paths: list[str]) -> int | None:
    """1-based rank of the first returned path that contains any of
    the expected_paths as a substring. ``None`` if no match.
    """
    for i, p in enumerate(returned_paths, start=1):
        for exp in expected_paths:
            if exp in p:
                return i
    return None


@dataclass
class ProbeOutcome:
    probe_id: int
    query: str
    category: str
    expected_paths: list[str]
    returned_paths: list[str]
    hit_rank: int | None  # None = miss
    elapsed_ms: float

    def hit(self) -> bool:
        return self.hit_rank is not None

    def reciprocal_rank(self) -> float:
        return 1.0 / self.hit_rank if self.hit_rank else 0.0


@dataclass
class ArmResult:
    arm: str
    config: dict
    total_runtime_s: float
    outcomes: list[ProbeOutcome]

    def hit_rate_at(self, k: int = 10) -> float:
        if not self.outcomes:
            return 0.0
        return sum(1 for o in self.outcomes if o.hit() and o.hit_rank <= k) / \
            len(self.outcomes)

    def mrr_at(self, k: int = 10) -> float:
        if not self.outcomes:
            return 0.0
        return sum(
            o.reciprocal_rank() for o in self.outcomes
            if o.hit_rank and o.hit_rank <= k
        ) / len(self.outcomes)

    def hit_rate_by_category(self, k: int = 10) -> dict[str, tuple[int, int]]:
        """Return ``{category: (hits, total)}`` so the report can
        render per-category hit-rate@k."""
        buckets: dict[str, list[ProbeOutcome]] = {}
        for o in self.outcomes:
            buckets.setdefault(o.category, []).append(o)
        return {
            cat: (
                sum(1 for o in outs if o.hit() and o.hit_rank <= k),
                len(outs),
            )
            for cat, outs in buckets.items()
        }


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def _run_arm(
    name: str,
    home: Path,
    probes: list[dict],
    colbert_channel,
    recency_fuse_alpha: float,
    k: int,
    embedder=None,
) -> ArmResult:
    """Spin up an Indexer with the given config, run all probes,
    return an ArmResult. The Indexer is constructed fresh per arm
    so the LRU query-embedding cache doesn't leak between arms
    (we want each arm's wall-clock to reflect cold-cache work on
    every probe).
    """
    started = time.time()
    log.info("arm %s: starting (alpha=%.2f, channel=%s)",
             name, recency_fuse_alpha, type(colbert_channel).__name__)
    if embedder is None:
        embedder = SagaProviderEmbedder()
    indexer = Indexer(
        home=home,
        embedder=embedder,
        colbert_provider=colbert_channel,
        recency_fuse_alpha=recency_fuse_alpha,
    )

    import asyncio

    async def _go() -> list[ProbeOutcome]:
        # Don't run the initial sweep — the index.db is already
        # populated and we don't want a 30+s reindex per arm.
        # Don't fire the periodic sweep loop either.
        await indexer.start(run_initial_sweep=False, sweep_loop=False)
        try:
            outs: list[ProbeOutcome] = []
            for probe in probes:
                t0 = time.time()
                results = await indexer.search(
                    probe["query"], scope="all", k=k,
                )
                elapsed_ms = (time.time() - t0) * 1000.0
                paths = [r.path for r in results]
                rank = _hit_rank(paths, probe["expected_paths"])
                outs.append(ProbeOutcome(
                    probe_id=probe["id"],
                    query=probe["query"],
                    category=probe["category"],
                    expected_paths=probe["expected_paths"],
                    returned_paths=paths,
                    hit_rank=rank,
                    elapsed_ms=elapsed_ms,
                ))
                hit_marker = "HIT" if rank else "miss"
                log.info("  probe %d/%d [%s] r=%s %s — %s",
                         probe["id"], len(probes), probe["category"],
                         rank if rank else "—", hit_marker,
                         probe["query"][:60])
            return outs
        finally:
            await indexer.stop()

    outcomes = asyncio.run(_go())
    total = time.time() - started
    log.info("arm %s: done in %.1fs (%d/%d hits)",
             name, total,
             sum(1 for o in outcomes if o.hit()), len(outcomes))
    return ArmResult(
        arm=name,
        config={
            "channel": type(colbert_channel).__name__,
            "recency_fuse_alpha": recency_fuse_alpha,
        },
        total_runtime_s=total,
        outcomes=outcomes,
    )


def _build_colbert(home: Path) -> None:
    """One-shot ColBERT sidecar build. ~13min on aarch64 CPU per
    chainlink-141 recon."""
    from mimir.colbert import ColBERTIndex

    log.info("colbert: starting build under %s (~13min wall on aarch64 CPU)",
             home)
    t0 = time.time()

    def _progress(done: int, total: int) -> None:
        elapsed = time.time() - t0
        if done == 0:
            log.info("  colbert: 0/%d chunks queued", total)
            return
        eta = elapsed * (total - done) / max(done, 1)
        log.info("  colbert: %d/%d (%.0f%%) elapsed=%.0fs eta=%.0fs",
                 done, total, 100 * done / max(total, 1), elapsed, eta)

    ColBERTIndex.build_from_corpus(
        home=home, progress_every=50, progress_cb=_progress,
    )
    log.info("colbert: build complete in %.1fs", time.time() - t0)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, default=None)
    parser.add_argument("--probes", type=Path,
                        default=_HERE / "probes.json")
    parser.add_argument("--out", type=Path,
                        default=_HERE / "results.json")
    parser.add_argument("--k", type=int, default=10)
    parser.add_argument("--skip-build", action="store_true",
                        help="reuse existing .colbert-index instead of rebuilding")
    parser.add_argument("--alpha", type=float, default=0.3,
                        help="recency_fuse_alpha for Arm C (default: 0.3)")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    home = args.home or Path(os.environ.get("MIMIR_HOME", "/mimir-home"))
    home = home.resolve()
    log.info("home=%s probes=%s k=%d alpha=%s",
             home, args.probes, args.k, args.alpha)

    probes_doc = json.loads(args.probes.read_text())
    probes = probes_doc["probes"]
    log.info("loaded %d probes", len(probes))

    # Sanity: index.db must exist (BM25 + dense need a corpus).
    if not (home / ".mimir" / "index.db").is_file():
        log.error("missing %s — run `mimir setup` or build the index "
                  "before running this harness", home / ".mimir" / "index.db")
        return 2

    # Build ColBERT sidecar unless reusing.
    from mimir.colbert import ColBERTIndex, default_index_dir, index_available
    idx_dir = default_index_dir(home)
    if args.skip_build:
        if not index_available(home):
            log.error("--skip-build set but no index at %s", idx_dir)
            return 2
        log.info("colbert: reusing existing index at %s", idx_dir)
    else:
        if index_available(home):
            log.info("colbert: existing index found at %s — rebuilding "
                     "(use --skip-build to reuse)", idx_dir)
        _build_colbert(home)

    colbert_idx = ColBERTIndex.open(idx_dir)
    colbert_channel = _ColBERTChannelAdapter(colbert_idx)

    # Run all three arms. Share a single SagaProviderEmbedder so the
    # provider gets loaded once (saves the cold-start latency across
    # arms and means all three arms hit the same query-embedding
    # cosine space).
    embedder = SagaProviderEmbedder()
    arms: list[ArmResult] = []
    arms.append(_run_arm(
        "A (no ColBERT)", home, probes,
        colbert_channel=_DisabledColBERTChannel(),
        recency_fuse_alpha=0.0,
        k=args.k,
        embedder=embedder,
    ))
    arms.append(_run_arm(
        "B (ColBERT, alpha=0)", home, probes,
        colbert_channel=colbert_channel,
        recency_fuse_alpha=0.0,
        k=args.k,
        embedder=embedder,
    ))
    arms.append(_run_arm(
        f"C (ColBERT, alpha={args.alpha})", home, probes,
        colbert_channel=colbert_channel,
        recency_fuse_alpha=args.alpha,
        k=args.k,
        embedder=embedder,
    ))

    # Serialize results.
    out_doc = {
        "home": str(home),
        "k": args.k,
        "alpha_arm_c": args.alpha,
        "probe_count": len(probes),
        "arms": [
            {
                "arm": a.arm,
                "config": a.config,
                "total_runtime_s": a.total_runtime_s,
                "hit_rate_at_k": a.hit_rate_at(args.k),
                "mrr_at_k": a.mrr_at(args.k),
                "hit_rate_by_category": {
                    cat: {"hits": h, "total": t, "rate": h / t if t else 0.0}
                    for cat, (h, t) in a.hit_rate_by_category(args.k).items()
                },
                "outcomes": [asdict(o) for o in a.outcomes],
            }
            for a in arms
        ],
    }
    args.out.write_text(json.dumps(out_doc, indent=2))
    log.info("wrote %s", args.out)

    # Brief summary to stdout.
    for a in arms:
        print(f"{a.arm}: hit-rate@{args.k}={a.hit_rate_at(args.k):.1%} "
              f"MRR@{args.k}={a.mrr_at(args.k):.3f} "
              f"runtime={a.total_runtime_s:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
