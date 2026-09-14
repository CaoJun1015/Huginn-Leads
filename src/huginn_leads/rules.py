from __future__ import annotations

import hashlib
import re
from typing import Any

PROCESSOR_VERSION = "supply-demand/1"

FIELD_TYPES = {"text", "number", "select", "date", "boolean"}
BUILT_IN_ROLES = {
    "direction", "product", "cpu", "gpu", "memory_gb", "storage_tb",
    "price", "quantity", "quantity_unit", "region",
}

DEFAULT_TEMPLATE_FIELDS = [
    {"key": "direction", "label": "任务方向", "type": "select", "required": True, "show_in_list": True, "match": True,
     "options": [{"value": "purchase", "label": "我要采购"}, {"value": "sell", "label": "我有库存待售"}], "role": "direction"},
    {"key": "product", "label": "商品描述", "type": "text", "required": True, "show_in_list": True, "match": False, "role": "product"},
    {"key": "cpu", "label": "CPU", "type": "text", "required": False, "show_in_list": False, "match": True, "role": "cpu"},
    {"key": "gpu", "label": "GPU", "type": "text", "required": False, "show_in_list": False, "match": True, "role": "gpu"},
    {"key": "memory_gb", "label": "内存下限 GB", "type": "number", "required": False, "show_in_list": True, "match": True, "role": "memory_gb"},
    {"key": "storage_tb", "label": "硬盘下限 TB", "type": "number", "required": False, "show_in_list": False, "match": True, "role": "storage_tb"},
    {"key": "price", "label": "预算/价格", "type": "number", "required": False, "show_in_list": True, "match": True, "role": "price"},
    {"key": "quantity", "label": "数量下限", "type": "number", "required": False, "show_in_list": True, "match": True, "role": "quantity"},
    {"key": "quantity_unit", "label": "数量单位", "type": "select", "required": False, "show_in_list": False, "match": False,
     "options": [{"value": value, "label": value} for value in ("台", "个", "套", "件")], "role": "quantity_unit"},
    {"key": "region", "label": "交付地区", "type": "text", "required": False, "show_in_list": True, "match": True,
     "help": "采购时代表目标收货地，销售时代表可交付地。", "role": "region"},
]

SUPPLY_WORDS = re.compile(r"(?:现货|到货|可出|出售|出一?批|供应|有货|清仓|转让)", re.I)
DEMAND_WORDS = re.compile(r"(?:求购|采购|收一?批|高价收|需求|寻货|要货)", re.I)
PRICE = re.compile(r"(?:[¥￥]\s*(\d+(?:\.\d+)?)|(\d+(?:\.\d+)?)\s*(?:元|块)(?:/([\u4e00-\u9fa5A-Za-z]+))?)", re.I)
QUANTITY = re.compile(r"(\d+(?:\.\d+)?)\s*(台|个|套|件|箱|块|张|条)")
MEMORY = re.compile(r"(?:内存|RAM)?\s*(\d{1,3})\s*G(?:B)?(?:\s*(?:内存|RAM))?", re.I)
STORAGE = re.compile(r"(?:硬盘|SSD|存储)?\s*(\d+(?:\.\d+)?)\s*(T|TB)(?:\s*(?:硬盘|SSD|存储))?", re.I)
CPU = re.compile(r"\b(?:i[3579][- ]?\d{4,5}[A-Za-z]{0,2}|Ultra\s*[3579]\s*\d{3}[A-Za-z]{0,2}|R[3579][- ]?\d{4}[A-Za-z]{0,2}|Ryzen\s*[3579]\s*\d{4}[A-Za-z]{0,2})\b", re.I)
GPU = re.compile(r"\b(?:RTX|GTX|RX)\s*\d{3,4}(?:\s*(?:Ti|SUPER))?\b", re.I)
REGION = re.compile(r"(?:交付|发货|地区|位置)[:：\s]*([\u4e00-\u9fa5]{2,8})")


def normalize(value: str) -> str:
    return re.sub(r"\s+", "", value).casefold()


def validate_template_fields(fields: Any) -> list[dict[str, Any]]:
    if not isinstance(fields, list) or len(fields) > 30:
        raise ValueError("模板字段必须是最多 30 项的列表")
    cleaned, keys, roles = [], set(), set()
    for raw in fields:
        if not isinstance(raw, dict):
            raise ValueError("模板字段格式无效")
        key = str(raw.get("key", "")).strip()
        label = str(raw.get("label", "")).strip()
        field_type = str(raw.get("type", ""))
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", key) or key in keys:
            raise ValueError("字段标识必须唯一，并使用小写字母、数字或下划线")
        if not label or len(label) > 80 or field_type not in FIELD_TYPES:
            raise ValueError("字段名称或类型无效")
        role = raw.get("role")
        if role is not None and (role not in BUILT_IN_ROLES or role in roles):
            raise ValueError("内置字段角色无效或重复")
        options: list[dict[str, str]] = []
        if field_type == "select":
            for option in raw.get("options") or []:
                if isinstance(option, dict):
                    value, option_label = str(option.get("value", "")).strip(), str(option.get("label", "")).strip()
                else:
                    value = option_label = str(option).strip()
                if value and option_label and value not in {item["value"] for item in options}:
                    options.append({"value": value[:80], "label": option_label[:80]})
            if not options:
                raise ValueError(f"单选字段“{label}”至少需要一个选项")
        item = {
            "key": key, "label": label, "type": field_type,
            "required": bool(raw.get("required")), "show_in_list": bool(raw.get("show_in_list")),
            "match": bool(raw.get("match")),
        }
        if options:
            item["options"] = options
        help_text = str(raw.get("help", "")).strip()
        if help_text:
            item["help"] = help_text[:240]
        if role:
            item["role"] = role
            roles.add(role)
        cleaned.append(item)
        keys.add(key)
    return cleaned


def validate_conditions(data: dict[str, Any], fields: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    if fields is not None:
        aliases = [str(value).strip() for value in data.get("aliases", []) if str(value).strip()]
        alias_mode = data.get("alias_mode", "any")
        source_ids = data.get("source_ids", [])
        if not aliases:
            raise ValueError("至少需要一个触发关键词")
        if alias_mode not in {"any", "all"}:
            raise ValueError("关键词组合只能是 any 或 all")
        if not source_ids or any(not isinstance(value, int) for value in source_ids):
            raise ValueError("至少选择一个有效来源")
        raw_values = data.get("values") or {}
        if not isinstance(raw_values, dict):
            raise ValueError("任务字段值格式无效")
        values: dict[str, Any] = {}
        for field in fields:
            value = raw_values.get(field["key"])
            empty = value is None or value == ""
            if field["required"] and empty:
                raise ValueError(f"{field['label']}不能为空")
            if empty:
                continue
            if field["type"] == "number":
                if isinstance(value, bool):
                    raise ValueError(f"{field['label']}必须是数字")
                try:
                    value = float(value)
                    value = int(value) if value.is_integer() else value
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"{field['label']}必须是数字") from exc
            elif field["type"] == "boolean":
                if not isinstance(value, bool):
                    raise ValueError(f"{field['label']}必须是开关值")
            else:
                value = str(value).strip()
                if len(value) > 500:
                    raise ValueError(f"{field['label']}内容过长")
                if field["type"] == "select" and value not in {option["value"] for option in field.get("options", [])}:
                    raise ValueError(f"{field['label']}选项无效")
            values[field["key"]] = value
        return {
            "aliases": list(dict.fromkeys(aliases)), "alias_mode": alias_mode,
            "source_ids": list(dict.fromkeys(source_ids)), "values": values,
        }

    direction = data.get("direction")
    product = str(data.get("product", "")).strip()
    aliases = [str(value).strip() for value in data.get("aliases", []) if str(value).strip()]
    alias_mode = data.get("alias_mode", "any")
    source_ids = data.get("source_ids", [])
    if direction not in {"purchase", "sell"}:
        raise ValueError("任务方向无效")
    if not product:
        raise ValueError("商品描述不能为空")
    if not aliases:
        raise ValueError("至少需要一个关键词或别名")
    if alias_mode not in {"any", "all"}:
        raise ValueError("关键词组合只能是 any 或 all")
    if not source_ids or any(not isinstance(value, int) for value in source_ids):
        raise ValueError("至少选择一个有效来源")
    cleaned = dict(data)
    cleaned.update(
        direction=direction,
        product=product,
        aliases=list(dict.fromkeys(aliases)),
        alias_mode=alias_mode,
        source_ids=list(dict.fromkeys(source_ids)),
    )
    return cleaned


def is_candidate(text: str, conditions: dict[str, Any]) -> bool:
    haystack = normalize(text)
    found = [normalize(alias) in haystack for alias in conditions["aliases"]]
    return all(found) if conditions.get("alias_mode") == "all" else any(found)


def _direction(text: str) -> str:
    supply = bool(SUPPLY_WORDS.search(text))
    demand = bool(DEMAND_WORDS.search(text))
    if supply == demand:
        return "unknown"
    return "supply" if supply else "demand"


def _extract(text: str) -> dict[str, Any]:
    price_match = PRICE.search(text)
    quantity_match = QUANTITY.search(text)
    memory_match = MEMORY.search(text)
    storage_match = STORAGE.search(text)
    region_match = REGION.search(text)
    cpu_match = CPU.search(text)
    gpu_match = GPU.search(text)
    price = None
    if price_match:
        price = {
            "value": float(price_match.group(1) or price_match.group(2)),
            "currency": "CNY",
            "unit": price_match.group(3) or None,
            "evidence": price_match.group(0),
        }
    quantity = None
    if quantity_match:
        quantity = {
            "value": float(quantity_match.group(1)),
            "unit": quantity_match.group(2),
            "evidence": quantity_match.group(0),
        }
    specs: dict[str, Any] = {}
    if cpu_match:
        specs["cpu"] = {"value": cpu_match.group(0), "evidence": cpu_match.group(0)}
    if gpu_match:
        specs["gpu"] = {"value": gpu_match.group(0), "evidence": gpu_match.group(0)}
    if memory_match:
        specs["memory_gb"] = {"value": int(memory_match.group(1)), "evidence": memory_match.group(0)}
    if storage_match:
        specs["storage_tb"] = {"value": float(storage_match.group(1)), "evidence": storage_match.group(0)}
    return {
        "price": price,
        "quantity": quantity,
        "specs": specs,
        "region": {"value": region_match.group(1), "evidence": region_match.group(0)} if region_match else None,
    }


def _compare(conditions: dict[str, Any], direction: str, extracted: dict[str, Any]) -> list[dict[str, str]]:
    comparisons: list[dict[str, str]] = []
    wanted_direction = "supply" if conditions["direction"] == "purchase" else "demand"
    direction_state = "unknown" if direction == "unknown" else ("match" if direction == wanted_direction else "conflict")
    comparisons.append({"field": "direction", "state": direction_state, "expected": wanted_direction, "actual": direction})

    for name, expected in (conditions.get("specs") or {}).items():
        if expected in (None, ""):
            continue
        actual = extracted["specs"].get(name)
        if not actual:
            state, value = "unknown", ""
        elif name in {"memory_gb", "storage_tb"}:
            state, value = ("match" if float(actual["value"]) >= float(expected) else "conflict"), str(actual["value"])
        else:
            state, value = ("match" if normalize(str(expected)) in normalize(str(actual["value"])) else "conflict"), str(actual["value"])
        comparisons.append({"field": name, "state": state, "expected": str(expected), "actual": value})

    price_condition = conditions.get("price") or {}
    if price_condition.get("value") not in (None, ""):
        actual = extracted.get("price")
        if not actual:
            state, value = "unknown", ""
        else:
            expected = float(price_condition["value"])
            if conditions["direction"] == "purchase":
                state = "match" if actual["value"] <= expected else "conflict"
            else:
                state = "match" if actual["value"] >= expected else "conflict"
            value = str(actual["value"])
        comparisons.append({"field": "price", "state": state, "expected": str(price_condition["value"]), "actual": value})

    quantity_condition = conditions.get("quantity") or {}
    if quantity_condition.get("value") not in (None, ""):
        actual = extracted.get("quantity")
        if not actual:
            state, value = "unknown", ""
        elif quantity_condition.get("unit") and quantity_condition["unit"] != actual["unit"]:
            state, value = "conflict", f"{actual['value']}{actual['unit']}"
        else:
            state = "match" if actual["value"] >= float(quantity_condition["value"]) else "conflict"
            value = f"{actual['value']}{actual['unit']}"
        comparisons.append({"field": "quantity", "state": state, "expected": str(quantity_condition["value"]), "actual": value})

    region = (conditions.get("trade") or {}).get("region")
    if region:
        actual = extracted.get("region")
        state = "unknown" if not actual else ("match" if normalize(region) in normalize(actual["value"]) else "conflict")
        comparisons.append({"field": "region", "state": state, "expected": region, "actual": actual["value"] if actual else ""})
    return comparisons


def _literal_comparisons(text: str, conditions: dict[str, Any], fields: list[dict[str, Any]]) -> list[dict[str, str]]:
    values, haystack, comparisons = conditions.get("values") or {}, normalize(text), []
    for field in fields:
        if field.get("role") or not field.get("match"):
            continue
        value = values.get(field["key"])
        if value in (None, "", False):
            continue
        expected = field["label"] if field["type"] == "boolean" else str(value)
        comparisons.append({
            "field": field["label"], "state": "match" if normalize(expected) in haystack else "unknown",
            "expected": expected, "actual": expected if normalize(expected) in haystack else "",
        })
    return comparisons


def _legacy_from_values(conditions: dict[str, Any], fields: list[dict[str, Any]]) -> dict[str, Any]:
    values = conditions.get("values") or {}
    matched_roles = {
        field.get("role") for field in fields
        if field.get("match") and values.get(field["key"]) not in (None, "")
    }
    return {
        "direction": values.get("direction", "purchase"),
        "product": values.get("product") or conditions["aliases"][0],
        "aliases": conditions["aliases"], "alias_mode": conditions.get("alias_mode", "any"),
        "source_ids": conditions["source_ids"],
        "specs": {key: values.get(key) for key in ("cpu", "gpu", "memory_gb", "storage_tb") if key in matched_roles and values.get(key) not in (None, "")},
        "price": {"value": values["price"]} if "price" in matched_roles and values.get("price") not in (None, "") else {},
        "quantity": {"value": values["quantity"], "unit": values.get("quantity_unit", "台")} if "quantity" in matched_roles and values.get("quantity") not in (None, "") else {},
        "trade": {"region": values["region"]} if "region" in matched_roles and values.get("region") else {},
    }


def evaluate(text: str, conditions: dict[str, Any], fields: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    if fields is not None and "values" in conditions:
        has_dealer_rules = any(field.get("role") == "direction" for field in fields)
        if has_dealer_rules:
            results = evaluate(text, _legacy_from_values(conditions, fields))
            values = conditions.get("values") or {}
            matched_roles = {
                field.get("role") for field in fields
                if field.get("match") and values.get(field["key"]) not in (None, "")
            }
            for item in results:
                if "price" not in matched_roles:
                    item["ambiguous"] = False
                item["comparisons"] = [entry for entry in item["comparisons"] if entry["field"] != "direction" or "direction" in matched_roles]
                extra = _literal_comparisons(item["text"], conditions, fields)
                item["comparisons"].extend(extra)
                states = {entry["state"] for entry in item["comparisons"]}
                item["outcome"] = "conflict" if "conflict" in states else ("review" if "unknown" in states or item.get("ambiguous") else "match")
                item["reason"] = "存在明确冲突" if item["outcome"] == "conflict" else ("信息不完整，需要人工核对" if item["outcome"] == "review" else "已知条件均满足")
            return results
        candidate_lines = [line.strip() for line in text.splitlines() if line.strip() and is_candidate(line, conditions)]
        items = candidate_lines or ([text.strip()] if is_candidate(text, conditions) else [])
        results = []
        for index, item in enumerate(items):
            comparisons = _literal_comparisons(item, conditions, fields)
            outcome = "review" if any(entry["state"] == "unknown" for entry in comparisons) else "match"
            results.append({
                "item_key": hashlib.sha256(f"{index}:{normalize(item)}".encode()).hexdigest()[:24],
                "text": item, "direction": _direction(item), "extracted": {"values": conditions.get("values", {})},
                "comparisons": comparisons, "outcome": outcome,
                "reason": "信息不完整，需要人工核对" if outcome == "review" else "已知条件均满足",
                "ambiguous": False,
            })
        return results

    candidate_lines = [line.strip() for line in text.splitlines() if line.strip() and is_candidate(line, conditions)]
    items = candidate_lines or ([text.strip()] if is_candidate(text, conditions) else [])
    if not items:
        return []
    results = []
    for index, item in enumerate(items):
        ambiguous = len(PRICE.findall(item)) > 1
        direction = _direction(item)
        extracted = _extract(item)
        comparisons = _compare(conditions, direction, extracted)
        states = {entry["state"] for entry in comparisons}
        outcome = "conflict" if "conflict" in states else ("review" if "unknown" in states or ambiguous else "match")
        reason = "存在明确冲突" if outcome == "conflict" else ("信息不完整，需要人工核对" if outcome == "review" else "已知条件均满足")
        item_key = hashlib.sha256(f"{index}:{normalize(item)}".encode()).hexdigest()[:24]
        results.append(
            {
                "item_key": item_key,
                "text": item,
                "direction": direction,
                "extracted": extracted,
                "comparisons": comparisons,
                "outcome": outcome,
                "reason": reason,
                "ambiguous": ambiguous,
            }
        )
    return results
