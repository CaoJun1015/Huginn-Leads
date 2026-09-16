from __future__ import annotations

import argparse
import re
from typing import Any


CPU = re.compile(r"\b(?:i[3579][- ]?\d{4,5}[A-Za-z]{0,2}|Ultra\s*[3579]\s*\d{3}[A-Za-z]{0,2}|R[3579][- ]?\d{4}[A-Za-z]{0,2}|Ryzen\s*[3579]\s*\d{4}[A-Za-z]{0,2})\b", re.I)
GPU = re.compile(r"\b(?:RTX|GTX|RX|X)\s*\d{3,4}(?:\s*(?:Ti|SUPER))?\b", re.I)
MEMORY = re.compile(r"(?:内存|RAM)?\s*(\d{1,3})\s*G(?:B)?(?:\s*(?:内存|RAM))?", re.I)
STORAGE = re.compile(r"(?:硬盘|SSD|存储)?\s*(\d+(?:\.\d+)?)\s*(?:T|TB)(?:\s*(?:硬盘|SSD|存储))?", re.I)
PRICE = re.compile(r"(?:[¥￥]\s*\d+(?:\.\d+)?|\d+(?:\.\d+)?\s*(?:元|块))", re.I)
QUANTITY = re.compile(r"\d+(?:\.\d+)?\s*(?:台|个|套|件|箱|块|张|条)")

OUTCOME_LABELS = {
    "match": "命中",
    "review": "待核对",
    "conflict": "冲突",
    "not_match": "未命中",
}
INTENT_LABELS = {
    "demand": "求购",
    "supply": "供货",
    "ambiguous": "方向不明确",
    "negative": "否定或交易已结束",
    "unknown": "未知",
}


def normalize(value: str) -> str:
    return re.sub(r"[\W_]+", "", value, flags=re.UNICODE).casefold()


def extract_specs(text: str) -> dict[str, Any]:
    cpu, gpu, memory, storage = CPU.search(text), GPU.search(text), MEMORY.search(text), STORAGE.search(text)
    price, quantity = PRICE.search(text), QUANTITY.search(text)
    return {
        "cpu": cpu.group(0) if cpu else None,
        "gpu": gpu.group(0) if gpu else None,
        "memory_gb": int(memory.group(1)) if memory else None,
        "storage_tb": float(storage.group(1)) if storage else None,
        "price": price.group(0) if price else None,
        "quantity": quantity.group(0) if quantity else None,
    }


def detect_intent(text: str, aliases: list[str]) -> tuple[str, list[str]]:
    value = normalize(text)
    demand_reasons, supply_reasons = [], []
    stock_negative = False

    for alias in aliases:
        item = re.escape(normalize(alias))
        if not item or item not in value:
            continue
        terminal = [
            rf"(?:不要|不收|不找|不用)(?:了)?{item}",
            rf"{item}(?:不要了?|不收了?|不找了?|不用了?|已买到|买到了?|已找到|找到了?)",
            rf"(?:已经|已)(?:买到|找到|收到){item}",
        ]
        if any(re.search(pattern, value) for pattern in terminal):
            return "negative", ["出现不要、不找或已经买到等结束表达"]

        demand_patterns = {
            "询问谁有": rf"(?:谁有|哪位有|有没有){item}",
            "主动寻找": rf"(?:找|求购?|采购|收|想要|需要|寻)(?:一|两|三|\d+)?(?:台|个|套|件)?{item}",
            "口语下单": rf"来(?:一|两|三|\d+)?(?:台|个|套|件)?{item}",
            "询问是否有货": rf"有{item}吗",
            "请求联系": rf"有{item}(?:的)?(?:联系(?:我)?|私我|报价)",
            "询问价格或库存": rf"{item}.{{0,40}}(?:有没有|有没|有吗|有货吗|有现货吗|现货吗|什么价|多少钱|报个价|价格多少|能出吗|可出吗)",
            "寻找可出者": rf"(?:能出|可以出){item}(?:的)?(?:联系(?:我)?|私我|报价)",
        }
        supply_patterns = {
            "表明持有": rf"(?:我有|手上有|手里有|现有){item}",
            "表明出售": rf"(?:出|卖|出售|处理)(?:一|两|三|\d+)?(?:台|个|套|件)?{item}",
            "表明库存": rf"{item}.{{0,40}}(?:现货(?!吗)|到货|有货(?!吗)|可出(?!吗)|出售|可发|在库)",
            "库存前置": rf"(?:现货|到货|有货|可出|出售|供应){item}",
        }
        demand_reasons.extend(label for label, pattern in demand_patterns.items() if re.search(pattern, value))
        supply_reasons.extend(label for label, pattern in supply_patterns.items() if re.search(pattern, value))
        stock_negative |= bool(re.search(rf"(?:{item}(?:没货|没有货|无货|不出|卖完|已出)|(?:没货|无货|不出){item})", value))

    if stock_negative and not demand_reasons:
        return "negative", ["出现没货、不出或已经卖完等表达"]
    if demand_reasons and supply_reasons:
        return "ambiguous", list(dict.fromkeys(demand_reasons + supply_reasons))
    if demand_reasons:
        return "demand", list(dict.fromkeys(demand_reasons))
    if supply_reasons:
        return "supply", list(dict.fromkeys(supply_reasons))
    return "unknown", []


def judge(
    text: str,
    task_direction: str,
    strong_aliases: list[str],
    weak_aliases: list[str],
    expected_specs: dict[str, Any],
) -> dict[str, Any]:
    value = normalize(text)
    strong_hits = [alias for alias in strong_aliases if normalize(alias) in value]
    weak_hits = [
        alias for alias in weak_aliases
        if normalize(alias) in value and not any(normalize(alias) in normalize(hit) for hit in strong_hits)
    ]
    hits = strong_hits + weak_hits
    specs = extract_specs(text)

    if not hits:
        return {"outcome": "not_match", "intent": "unknown", "recorded": False, "aliases": [], "specs": specs, "reason": "未出现商品别名"}

    intent, intent_reasons = detect_intent(text, hits)
    if intent == "negative":
        return {"outcome": "not_match", "intent": intent, "recorded": False, "aliases": hits, "specs": specs, "reason": intent_reasons[0]}
    if not strong_hits and intent == "unknown":
        return {"outcome": "not_match", "intent": intent, "recorded": False, "aliases": hits, "specs": specs, "reason": "弱别名缺少交易意图"}
    if intent in {"unknown", "ambiguous"}:
        reason = "同时出现求购和供货表达" if intent == "ambiguous" else "商品已识别，但交易方向未知"
        return {"outcome": "review", "intent": intent, "recorded": True, "aliases": hits, "specs": specs, "reason": reason}

    wanted_intent = "supply" if task_direction == "purchase" else "demand"
    if intent != wanted_intent:
        return {"outcome": "conflict", "intent": intent, "recorded": True, "aliases": hits, "specs": specs, "reason": f"任务需要{INTENT_LABELS[wanted_intent]}信息，文本却是{INTENT_LABELS[intent]}"}

    missing, conflicts = [], []
    for key, expected in expected_specs.items():
        actual = specs.get(key)
        if actual is None:
            missing.append(key)
        elif normalize(str(actual)) != normalize(str(expected)):
            conflicts.append(f"{key}: 期望 {expected}，实际 {actual}")
    if conflicts:
        return {"outcome": "conflict", "intent": intent, "recorded": True, "aliases": hits, "specs": specs, "reason": "；".join(conflicts)}
    if missing:
        return {"outcome": "review", "intent": intent, "recorded": True, "aliases": hits, "specs": specs, "reason": "缺少规格：" + "、".join(missing)}
    reason = "；".join(intent_reasons) + "；已知规格均符合"
    return {"outcome": "match", "intent": intent, "recorded": True, "aliases": hits, "specs": specs, "reason": reason}


def print_result(text: str, result: dict[str, Any]) -> None:
    specs = {key: value for key, value in result["specs"].items() if value is not None}
    print(f"\n文本：{text}")
    print(f"商品：{', '.join(result['aliases']) or '未识别'}")
    print(f"意图：{INTENT_LABELS[result['intent']]}")
    print(f"规格：{specs or '未提取到'}（地区不提取）")
    print(f"结果：{OUTCOME_LABELS[result['outcome']]}；收录：{'是' if result['recorded'] else '否'}")
    print(f"原因：{result['reason']}")


def self_test() -> None:
    expected_specs = {
        key: value for key, value in extract_specs("I7 14650HX X5060 16G 1T").items()
        if value is not None
    }
    config = ("sell", ["来酷战7000"], ["战7000"], expected_specs)
    expected = {
        "谁有来酷战7000": ("review", "demand", True),
        "找一台战7000": ("review", "demand", True),
        "来个战7000": ("review", "demand", True),
        "有战7000的联系": ("review", "demand", True),
        "战7000什么价": ("review", "demand", True),
        "有战7000吗": ("review", "demand", True),
        "战7000有货吗": ("review", "demand", True),
        "来酷战7000 I7 14650HX X5060 16G 1T 什么价": ("match", "demand", True),
        "我有战7000 I7 14650HX X5060 16G 1T": ("conflict", "supply", True),
        "战7000现货": ("conflict", "supply", True),
        "战7000没货，我也在找战7000": ("review", "demand", True),
        "战7000不要了": ("not_match", "negative", False),
        "战7000": ("not_match", "unknown", False),
        "来酷战7000": ("review", "unknown", True),
        "大猪": ("not_match", "unknown", False),
    }
    for text, wanted in expected.items():
        result = judge(text, *config)
        actual = result["outcome"], result["intent"], result["recorded"]
        assert actual == wanted, f"{text}: {actual} != {wanted}"
    print(f"自检通过：{len(expected)} 个案例")


def main() -> None:
    parser = argparse.ArgumentParser(description="Huginn Leads 文本判断 Demo（不读写数据库）")
    parser.add_argument("texts", nargs="*", help="直接判断的文本；不提供时进入交互模式")
    parser.add_argument("--task-direction", choices=("sell", "purchase"), default="sell", help="sell=库存待售，purchase=我要采购")
    parser.add_argument("--strong", default="来酷战7000", help="逗号分隔的强别名")
    parser.add_argument("--weak", default="战7000", help="逗号分隔的弱别名")
    parser.add_argument("--expected", default="I7 14650HX X5060 16G 1T", help="用于核对的预期规格；传空字符串可关闭")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return

    strong = [item.strip() for item in re.split(r"[,，]", args.strong) if item.strip()]
    weak = [item.strip() for item in re.split(r"[,，]", args.weak) if item.strip()]
    expected_specs = {key: value for key, value in extract_specs(args.expected).items() if key in {"cpu", "gpu", "memory_gb", "storage_tb"} and value is not None}
    config = (args.task_direction, strong, weak, expected_specs)

    print("Huginn Leads 文本判断 Demo")
    print(f"任务方向：{'库存待售（寻找求购）' if args.task_direction == 'sell' else '我要采购（寻找供货）'}")
    print(f"强别名：{strong or '无'}；弱别名：{weak or '无'}；预期规格：{expected_specs or '不核对'}")
    print("每行输入一个案例；直接回车或输入 exit 结束。")

    if args.texts:
        for text in args.texts:
            print_result(text, judge(text, *config))
        return
    while True:
        try:
            text = input("\n测试文本> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not text or text.casefold() in {"exit", "quit"}:
            return
        print_result(text, judge(text, *config))


if __name__ == "__main__":
    main()
