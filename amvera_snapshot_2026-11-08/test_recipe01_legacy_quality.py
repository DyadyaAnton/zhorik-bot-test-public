import json
import sqlite3
import unittest
from recipe01_legacy_quality import quality_flags,audit_legacy_recipes,safe_for_new_generation


class LegacyQualityTests(unittest.TestCase):
    def test_quarantine_tautology_but_keep_unreviewed_ordinary_legacy(self):
        raw='[{"product":"Капуста", "amount":120, "unit":"г"}]'
        self.assertFalse(safe_for_new_generation('Капуста тушёная с капустой','','',raw))
        self.assertTrue(safe_for_new_generation('Капуста тушёная с морковью','','',raw))
    def test_tautology_and_component_are_flagged_without_renaming(self):
        ingredients=json.dumps([{'product':'Морковь','amount':100,'unit':'г'}])
        flags=quality_flags('Морковная запеканка с морковью','Овощи и гарниры',
                            'Смешать ингредиенты',ingredients)
        self.assertIn('redundant_title',flags)
        self.assertIn('component_not_full_meal',flags)

    def test_read_only_and_no_false_approval(self):
        db=sqlite3.connect(':memory:')
        db.executescript('''CREATE TABLE zr_recipes
           (id TEXT,title TEXT,category TEXT,preparation TEXT,ingredients_json TEXT,
            nutrition_status TEXT,active INTEGER);
           INSERT INTO zr_recipes VALUES
           ('R1','Картофель с картофелем','Овощи и гарниры','Готовить',
            '[{"product":"Картофель","amount":100,"unit":"г"}]','estimated_unverified',1);''')
        changes=db.total_changes
        report=audit_legacy_recipes(db)
        self.assertEqual(changes,db.total_changes)
        self.assertIn('redundant_title',report[0]['quality_flags'])
        self.assertIn('nutrition_unverified',report[0]['quality_flags'])
        self.assertEqual('R1',report[0]['recipe_id'])

    def test_invalid_nonfinite_amount_is_flagged(self):
        ingredient='[{"product":"Рис","amount":NaN,"unit":"г"}]'
        self.assertIn('invalid_ingredient_quantity',quality_flags('Рис', 'Каши', 'Варить', ingredient))


if __name__=='__main__':unittest.main()
