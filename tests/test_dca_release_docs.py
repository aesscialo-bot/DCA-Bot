import json
import re
import unittest
from pathlib import Path

from dca_config import TARGET_KEYS, validate_rules_map


class ReleaseDocumentationTests(unittest.TestCase):
    def test_operator_rule_examples_are_valid_four_target_paused_maps(self):
        root = Path(__file__).resolve().parents[1]
        for name in ("README.md", "00_START_HERE.md"):
            with self.subTest(document=name):
                text = (root / name).read_text(encoding="utf-8")
                examples = re.findall(r"```json\s*\n(.*?)\n```", text, re.DOTALL)
                self.assertTrue(examples)
                rules = validate_rules_map(json.loads(examples[0]))
                self.assertEqual(set(rules), set(TARGET_KEYS))
                self.assertTrue(all(not rule["BUY_ENABLED"] for rule in rules.values()))
                self.assertEqual(rules["DOGE_GBP"]["REGIME_AMOUNTS_GBP"], {"LOW": 0, "MID": 0, "UP": 0})
                self.assertIn("DCA_CRON_ENABLED=false", text)
                self.assertIn("LAST_REAL_CANDLE_AT", text)
                self.assertIn("COVERAGE_THROUGH", text)
                self.assertIn("expected_decision_id", text)


if __name__ == "__main__":
    unittest.main()
