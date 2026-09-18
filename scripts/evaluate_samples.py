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
MEMORY_CONDITIONS = CONDITIONS | {"specs": {"memory_gb": 16}, "price": {}, "quantity": {}}


def corpus():
    samples = []
    for index in range(25):
        samples.append((f"现货 ThinkBook 批次{index + 1} 32G 1TB 4280元 2台", "match", CONDITIONS))
        samples.append((f"求购 ThinkBook 批次{index + 1} 32G 1TB 4200元 2台", "conflict", CONDITIONS))
        samples.append((f"ThinkBook 批次{index + 1} 32G 1TB 4400元 2台", "review", CONDITIONS))
        samples.append((f"现货台式工作站 批次{index + 1} 32G 1TB 4200元 2台", "none", CONDITIONS))
    samples.extend((text, expected, MEMORY_CONDITIONS) for text, expected in (
        ("现货 ThinkBook 512GB SSD", "review"),
        ("现货 ThinkBook 内存8GB SSD512GB", "conflict"),
        ("现货 ThinkBook 内存16GB SSD512GB", "match"),
        ("现货 ThinkBook RTX 4060 显存8GB", "review"),
        ("现货 ThinkBook SSD512GB 内存16GB", "match"),
        ("现货 ThinkBook 512GB SSD 16GB内存", "match"),
        ("现货 ThinkBook ssd512gb ram16gb", "match"),
        ("现货 ThinkBook 8GB VRAM 1TB硬盘", "review"),
        ("现货 ThinkBook 512GB", "review"),
        ("现货 ThinkBook 16G 1TB", "match"),
        ("现货 ThinkBook 内存8GB RAM16GB SSD512GB", "review"),
    ))
    return samples


def main() -> int:
    samples = corpus()
    correct = 0
    counts = {"match": 0, "conflict": 0, "review": 0, "none": 0}
    for text, expected, conditions in samples:
        result = evaluate(text, conditions)
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
