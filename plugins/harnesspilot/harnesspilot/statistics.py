from __future__ import annotations

import random
import statistics


def paired(rows: list[dict], *, seed: int = 42, repetitions: int = 5000) -> dict:
    if not rows:
        return {"n": 0, "claim": "缺少配对结果"}
    families = {}
    for row in rows:
        families.setdefault(row["family"], []).append(
            int(row["candidate"]) - int(row["baseline"]))
    values = list(families.values())
    rng = random.Random(seed)
    boot = sorted(statistics.mean(v for group in rng.choices(values, k=len(values))
                                  for v in group) for _ in range(repetitions))
    delta = statistics.mean(int(r["candidate"]) - int(r["baseline"]) for r in rows)
    return {"n": len(rows), "families": len(values), "delta": delta,
            "baseline_success": statistics.mean(int(r["baseline"]) for r in rows),
            "candidate_success": statistics.mean(int(r["candidate"]) for r in rows),
            "ci95": [boot[int(repetitions * .025)], boot[int(repetitions * .975)]],
            "seed": seed, "bootstrap_repetitions": repetitions,
            "wins": sum(r["candidate"] and not r["baseline"] for r in rows),
            "regressions": sum(r["baseline"] and not r["candidate"] for r in rows),
            "claim": "合成冒烟结果；样本量与任务来源不足以证明泛化提升"}
