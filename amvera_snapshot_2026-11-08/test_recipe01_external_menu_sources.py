import csv
import tempfile
import unittest
from pathlib import Path

from recipe01_external_menu_sources import read_source_candidates


class IntakeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = Path(__file__).parent / 'recipe01_external_menu_candidates.csv'

    def test_supplied_five_day_candidates_remain_unverified(self):
        rows = read_source_candidates(self.fixture)
        self.assertEqual(len(rows), 26)
        self.assertEqual({r['source_day'] for r in rows}, set('12345'))
        self.assertTrue(all(r['nutrition_status'] == 'unverified' for r in rows))

    def test_rejects_fake_verified_macro_status(self):
        with self.fixture.open(encoding='utf-8', newline='') as stream:
            rows = list(csv.DictReader(stream))
        rows[0]['nutrition_status'] = 'verified'
        with tempfile.TemporaryDirectory() as folder:
            dst = Path(folder) / 'bad.csv'
            with dst.open('w', encoding='utf-8', newline='') as stream:
                writer = csv.DictWriter(stream, fieldnames=rows[0].keys())
                writer.writeheader()
                writer.writerows(rows)
            with self.assertRaisesRegex(ValueError, 'pre-approved'):
                read_source_candidates(dst)


if __name__ == '__main__':
    unittest.main()
