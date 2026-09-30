"""Calibrate the semantic-cache threshold on Quora Question Pairs (GLUE QQP).

QQP pairs are human-labelled as duplicate / not duplicate. A cache hit on a
non-duplicate would serve the wrong answer, so for each threshold we report:
  precision  share of hits that are true duplicates (wrong-answer risk = 1 - precision)
  recall     share of duplicates the cache would catch
Scores use the same normalisation as Upstash Vector: (1 + cosine) / 2.

    python bench/cache_eval.py --pairs 400
Needs GEMINI_API_KEY and `pip install pyarrow`.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import time
import urllib.request
from pathlib import Path

import httpx

QQP_URL = "https://huggingface.co/datasets/nyu-mll/glue/resolve/main/qqp/validation-00000-of-00001.parquet"
EMBED_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:batchEmbedContents"
HERE = Path(__file__).resolve().parent


def load_pairs(n: int, seed: int) -> list[tuple[str, str, int]]:
    import pyarrow.parquet as pq

    cache = HERE / "data" / "qqp_validation.parquet"
    if not cache.exists():
        cache.parent.mkdir(parents=True, exist_ok=True)
        urllib.request.urlretrieve(QQP_URL, cache)
    t = pq.read_table(cache).to_pydict()
    rows = [(a, b, int(y)) for a, b, y in zip(t["question1"], t["question2"], t["label"], strict=True)]
    rng = random.Random(seed)
    dup = [r for r in rows if r[2] == 1]
    non = [r for r in rows if r[2] == 0]
    return rng.sample(dup, n // 2) + rng.sample(non, n // 2)


def embed_all(texts: list[str], key: str, model: str) -> list[list[float]]:
    out: list[list[float]] = []
    with httpx.Client(timeout=60) as c:
        for i in range(0, len(texts), 100):
            batch = texts[i : i + 100]
            body = {
                "requests": [
                    {
                        "model": f"models/{model}",
                        "content": {"parts": [{"text": t}]},
                        "taskType": "SEMANTIC_SIMILARITY",
                        "outputDimensionality": 768,
                    }
                    for t in batch
                ]
            }
            for attempt in range(6):  # free tier: back off on per-minute limits
                r = c.post(EMBED_URL.format(model=model), params={"key": key}, json=body)
                if r.status_code == 200 and "embeddings" in r.json():
                    break
                time.sleep(20 * (attempt + 1))
            else:
                raise RuntimeError(f"embedding failed: {r.status_code} {r.text[:200]}")
            out += [e["values"] for e in r.json()["embeddings"]]
    return out


def cos(a, b):
    return sum(x * y for x, y in zip(a, b, strict=True)) / (
        math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pairs", type=int, default=400)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--model", default="gemini-embedding-2")
    args = ap.parse_args()
    pairs = load_pairs(args.pairs, args.seed)
    n = len(pairs)
    score_cache = HERE / "data" / f"qqp_scores_{args.model}_{n}_{args.seed}.json"
    if score_cache.exists():
        scored = [tuple(x) for x in json.loads(score_cache.read_text())]
    else:
        vecs = embed_all([p[0] for p in pairs] + [p[1] for p in pairs], os.environ["GEMINI_API_KEY"], args.model)
        scored = [((1 + cos(vecs[i], vecs[n + i])) / 2, pairs[i][2]) for i in range(n)]
        score_cache.write_text(json.dumps(scored))

    rows = []
    for th in [0.90, 0.93, 0.95, 0.96, 0.97, 0.975, 0.98, 0.985, 0.99]:
        hits = [y for s, y in scored if s >= th]
        tp = sum(hits)
        dups = sum(y for _, y in scored)
        rows.append(
            {
                "threshold": th,
                "hits": len(hits),
                "precision": round(tp / len(hits), 4) if hits else None,
                "recall": round(tp / dups, 4),
                "false_hits": len(hits) - tp,
            }
        )
    result = {
        "dataset": "GLUE QQP validation",
        "pairs": n,
        "duplicates": sum(y for _, y in scored),
        "embedding": f"{args.model} (768d)",
        "score": "(1 + cosine) / 2",
        "thresholds": rows,
    }
    out = HERE / "results" / f"cache_eval_{args.model}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(result, indent=2))
    print(f"{'threshold':>9} {'hits':>5} {'precision':>9} {'recall':>7} {'false hits':>10}")
    for r in rows:
        p = f"{r['precision']:.1%}" if r["precision"] is not None else "—"
        print(f"{r['threshold']:>9.3f} {r['hits']:>5} {p:>9} {r['recall']:>7.1%} {r['false_hits']:>10}")


if __name__ == "__main__":
    main()
