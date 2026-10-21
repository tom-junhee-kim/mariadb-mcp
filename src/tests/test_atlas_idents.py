import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from atlas import extract_idents  # noqa: E402

CASES = json.loads((Path(__file__).parent / "ident_cases.json").read_text(encoding="utf-8"))


class IdentCases(unittest.TestCase):
    def test_cases(self):
        for c in CASES:
            with self.subTest(text=c["text"]):
                got = {tok for tok, _t in extract_idents(c["text"])}
                self.assertEqual(got, set(c["expect"]))

    def test_cap_per_text(self):
        text = " ".join(f"a{i:06d}f" for i in range(200))
        self.assertLessEqual(len(extract_idents(text)), 60)


if __name__ == "__main__":
    unittest.main()
