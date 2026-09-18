import unittest

from huginn_leads.rules import evaluate, is_candidate, validate_conditions


CONDITIONS = {
    "direction": "purchase",
    "product": "14 英寸办公本",
    "aliases": ["ThinkBook", "战66"],
    "alias_mode": "any",
    "specs": {"memory_gb": 16, "storage_tb": 1},
    "price": {"value": 4500, "currency": "CNY"},
    "quantity": {"value": 2, "unit": "台"},
    "trade": {},
    "source_ids": [1],
}


class RuleTests(unittest.TestCase):
    def test_supply_match(self):
        result = evaluate("现货 ThinkBook 32G 1TB，4280元，2台", CONDITIONS)[0]
        self.assertEqual(result["direction"], "supply")
        self.assertEqual(result["outcome"], "match")

    def test_demand_conflicts_with_purchase_task(self):
        result = evaluate("求购 ThinkBook 32G 1TB，预算4200元，2台", CONDITIONS)[0]
        self.assertEqual(result["direction"], "demand")
        self.assertEqual(result["outcome"], "conflict")

    def test_unknown_is_not_promoted_to_match(self):
        result = evaluate("ThinkBook 32G 1TB，4200元，2台", CONDITIONS)[0]
        self.assertEqual(result["direction"], "unknown")
        self.assertEqual(result["outcome"], "review")

    def test_multiple_prices_are_reviewed(self):
        result = evaluate("现货 ThinkBook 16G 1TB，单台4200元，两台共8200元", CONDITIONS)[0]
        self.assertEqual(result["outcome"], "review")

    def test_multiple_prices_in_one_of_several_lines_are_reviewed(self):
        results = evaluate(
            "现货 ThinkBook A 16G 1TB，4200元，2台，另报价4100元\n"
            "现货 ThinkBook B 16G 1TB，4300元，2台",
            CONDITIONS,
        )
        self.assertEqual([item["outcome"] for item in results], ["review", "match"])

    def test_memory_storage_and_vram_are_not_confused(self):
        conditions = CONDITIONS | {"specs": {"memory_gb": 16}, "price": {}, "quantity": {}}
        cases = [
            ("现货 ThinkBook 512GB SSD", "review", None, 0.5),
            ("现货 ThinkBook 内存8GB SSD512GB", "conflict", 8, 0.5),
            ("现货 ThinkBook 内存16GB SSD512GB", "match", 16, 0.5),
            ("现货 ThinkBook RTX 4060 显存8GB", "review", None, None),
            ("现货 ThinkBook SSD512GB 内存16GB", "match", 16, 0.5),
            ("现货 ThinkBook 512GB SSD 16GB内存", "match", 16, 0.5),
            ("现货 ThinkBook ssd512gb ram16gb", "match", 16, 0.5),
            ("现货 ThinkBook 8GB VRAM 1TB硬盘", "review", None, 1),
            ("现货 ThinkBook 512GB", "review", None, None),
        ]
        for text, outcome, memory, storage in cases:
            with self.subTest(text=text):
                result = evaluate(text, conditions)[0]
                specs = result["extracted"]["specs"]
                self.assertEqual(result["outcome"], outcome)
                self.assertEqual(specs.get("memory_gb", {}).get("value"), memory)
                self.assertEqual(specs.get("storage_tb", {}).get("value"), storage)

    def test_capacity_evidence_and_existing_shorthand_are_preserved(self):
        result = evaluate("现货 ThinkBook 16G 1TB", CONDITIONS | {"price": {}, "quantity": {}})[0]
        self.assertEqual(result["outcome"], "match")
        self.assertEqual(result["extracted"]["specs"], {
            "memory_gb": {"value": 16, "evidence": "16G"},
            "storage_tb": {"value": 1.0, "evidence": "1TB"},
        })

    def test_ambiguous_and_multiline_capacities_stay_with_their_item(self):
        conditions = CONDITIONS | {"specs": {"memory_gb": 16}, "price": {}, "quantity": {}}
        ambiguous = evaluate("现货 ThinkBook 内存8GB RAM16GB SSD512GB", conditions)[0]
        self.assertEqual(ambiguous["outcome"], "review")
        self.assertNotIn("memory_gb", ambiguous["extracted"]["specs"])

        results = evaluate(
            "现货 ThinkBook A 512GB SSD\n"
            "现货 ThinkBook B 内存16GB SSD512GB",
            conditions,
        )
        self.assertEqual([item["outcome"] for item in results], ["review", "match"])
        self.assertNotIn("memory_gb", results[0]["extracted"]["specs"])
        self.assertEqual(results[1]["extracted"]["specs"]["memory_gb"]["value"], 16)

    def test_non_candidate_is_empty(self):
        self.assertFalse(is_candidate("戴尔台式机现货", CONDITIONS))
        self.assertEqual(evaluate("戴尔台式机现货", CONDITIONS), [])

    def test_colloquial_demand_is_collected_for_sell_task(self):
        conditions = {
            "direction": "sell", "product": "来酷战7000", "aliases": ["来酷战7000"], "weak_aliases": ["战7000"],
            "alias_mode": "any", "specs": {"cpu": "I7 14650HX", "gpu": "X5060", "memory_gb": 16, "storage_tb": 1},
            "price": {}, "quantity": {}, "trade": {}, "source_ids": [1],
        }
        for text in ("谁有来酷战7000", "找一台战7000", "来个战7000", "有战7000的联系", "战7000什么价"):
            with self.subTest(text=text):
                result = evaluate(text, conditions)[0]
                self.assertEqual((result["direction"], result["outcome"]), ("demand", "review"))
        self.assertEqual(evaluate("找一台战7000", conditions)[0]["extracted"]["trigger"]["kind"], "weak")
        result = evaluate("来酷战7000 I7 14650HX X5060 16G 1T 什么价", conditions)[0]
        self.assertEqual((result["direction"], result["outcome"]), ("demand", "match"))

    def test_weak_alias_requires_trade_intent(self):
        conditions = {
            "direction": "sell", "product": "来酷战7000", "aliases": ["来酷战7000"], "weak_aliases": ["战7000"],
            "alias_mode": "any", "specs": {}, "price": {}, "quantity": {}, "trade": {}, "source_ids": [1],
        }
        self.assertEqual(evaluate("来酷战7000", conditions)[0]["extracted"]["trigger"]["kind"], "strong")
        self.assertEqual(evaluate("战7000", conditions), [])
        self.assertEqual(evaluate("大战7000次", conditions), [])
        self.assertEqual(evaluate("战7000不要了", conditions), [])
        self.assertEqual(evaluate("我有战7000", conditions)[0]["outcome"], "conflict")

    def test_supply_questions_and_negations_are_distinguished(self):
        conditions = {
            "direction": "sell", "product": "战7000", "aliases": ["战7000"], "alias_mode": "any",
            "specs": {}, "price": {}, "quantity": {}, "trade": {}, "source_ids": [1],
        }
        self.assertEqual(evaluate("战7000有货吗", conditions)[0]["direction"], "demand")
        self.assertEqual(evaluate("我有战7000现货", conditions)[0]["direction"], "supply")
        self.assertEqual(evaluate("战7000不要了", conditions), [])
        self.assertEqual(evaluate("战7000没货，我也在找战7000", conditions)[0]["direction"], "demand")

    def test_region_is_not_extracted_or_compared(self):
        conditions = CONDITIONS | {"trade": {"region": "北京"}}
        result = evaluate("现货 ThinkBook 32G 1TB，4280元，2台，发货上海", conditions)[0]
        self.assertEqual(result["outcome"], "match")
        self.assertNotIn("region", result["extracted"])
        self.assertNotIn("region", {item["field"] for item in result["comparisons"]})

    def test_validation_rejects_missing_sources(self):
        with self.assertRaises(ValueError):
            validate_conditions(CONDITIONS | {"source_ids": []})

    def test_custom_fields_use_literal_matching(self):
        fields = [
            {"key": "channel", "label": "渠道等级", "type": "text", "required": False,
             "show_in_list": True, "match": True}
        ]
        conditions = {
            "aliases": ["显卡"], "alias_mode": "any", "source_ids": [1],
            "values": {"channel": "核心"},
        }
        self.assertEqual(evaluate("显卡现货，核心渠道", conditions, fields)[0]["outcome"], "match")
        self.assertEqual(evaluate("显卡现货，普通渠道", conditions, fields)[0]["outcome"], "review")
        self.assertEqual(evaluate("打印机现货，核心渠道", conditions, fields), [])


if __name__ == "__main__":
    unittest.main()
