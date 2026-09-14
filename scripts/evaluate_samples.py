from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from huginn_leads.rules import evaluate


CONDITIONS = {
    "direction": "purchase", "product": "办公本", "aliases": ["ThinkBook"], "alias_mode": "any",
    "specs": {"memory_gb": 16, "storage_tb": 1}, "price": {"value": 4500},
    "quantity": {"value": 2, "unit": "台"}, "trade": {}, "source_ids": [1],
}


def corpus():
    samples = []
    for index in range(25):
        samples.append((f"现货 ThinkBook 批次{index + 1} 32G 1TB 4280元 2台", "match"))
        samples.append((f"求购 ThinkBook 批次{index + 1} 32G 1TB 4200元 2台", "conflict"))
        samples.append((f"ThinkBook 批次{index + 1} 32G 1TB 4400元 2台", "review"))
        samples.append((f"现货台式工作站 批次{index + 1} 32G 1TB 4200元 2台", "none"))
    return samples


def main() -> int:
    samples = corpus()
    correct = 0
    counts = {"match": 0, "conflict": 0, "review": 0, "none": 0}
    for text, expected in samples:
        result = evaluate(text, CONDITIONS)
        actual = result[0]["outcome"] if result else "none"
        counts[actual] += 1
        correct += actual == expected
    print(f"Samples: {len(samples)}")
    print(f"Exact outcome accuracy: {correct / len(samples):.1%}")
    print("Distribution:", counts)
    assert len(samples) >= 100
    assert correct == len(samples)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
