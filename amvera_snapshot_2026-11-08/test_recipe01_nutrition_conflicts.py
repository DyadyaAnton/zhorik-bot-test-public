import unittest
from recipe01_nutrition_conflicts import numeric_cell,compare_candidates


class NutritionConflictsTests(unittest.TestCase):
    def test_dash_range_and_invalid_are_not_zero(self):
        for val in ('-','—','0,2-0,4','следы','NaN',float('nan'),-1,''):
            with self.subTest(val=val):self.assertIsNone(numeric_cell(val))
        self.assertEqual(0,numeric_cell('0'))
        self.assertEqual(12.6,numeric_cell('12,6'))

    def test_food_state_or_basis_mismatch_is_not_comparable(self):
        raw={'state':'dry','basis':'per_100g_edible','kcal':'333'}
        cooked={'state':'cooked','basis':'per_100g_edible','kcal':'130'}
        self.assertEqual('not_comparable',compare_candidates(raw,cooked)['status'])
        self.assertFalse(compare_candidates(raw,cooked)['verified'])
        self.assertEqual('not_comparable',compare_candidates(
            {'state':'','basis':'','kcal':'100'},
            {'state':'','basis':'','kcal':'100'})['status'])

    def test_buckwheat_disagreement_never_becomes_verified(self):
        a=dict(state='dry',basis='per_100g_edible',protein='12,6',fat='3,3',carbs='57,1',kcal='308')
        b=dict(state='dry',basis='per_100g_edible',protein='12,6',fat='3,26',carbs='54,3',kcal='335')
        comparison=compare_candidates(a,b)
        self.assertEqual('disagreement',comparison['status'])
        self.assertEqual(8.06,comparison['details']['kcal']['difference_percent'])
        self.assertFalse(comparison['verified'])
        strict=compare_candidates(a,b,tolerance_percent=5)
        self.assertEqual('disagreement',strict['status'])

    def test_missing_macro_never_passes_as_agreement(self):
        a=dict(state='raw',basis='per_100g_edible',protein='15,9',fat='0,9',carbs='-',kcal='72')
        b=dict(state='raw',basis='per_100g_edible',protein='16',fat='1',carbs='0',kcal='73')
        self.assertEqual('incomplete',compare_candidates(a,b)['status'])


if __name__=='__main__':unittest.main()
