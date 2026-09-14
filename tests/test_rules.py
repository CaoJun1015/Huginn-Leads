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

    def test_non_candidate_is_empty(self):
        self.assertFalse(is_candidate("戴尔台式机现货", CONDITIONS))
        self.assertEqual(evaluate("戴尔台式机现货", CONDITIONS), [])

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
