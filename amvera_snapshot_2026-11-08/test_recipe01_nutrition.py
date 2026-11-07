import unittest

from recipe01_nutrition import (Nutrients, IncompleteNutrition,
                                calculate_recipe, assess_meal_balance)


class Recipe01NutritionTests(unittest.TestCase):
    def setUp(self):
        self.catalog = {
            ('rice','dry'):{'status':'verified','allergens_verified':True,
                            'kcal':330,'protein':7,'fat':1,'carbs':73.2,'allergens':[],
                            'source':'reference book p.3','review_id':'audit-101'},
            ('milk','liquid'):{'status':'verified','allergens_verified':True,
                               'kcal':50,'protein':3,'fat':2.5,'carbs':4.7,'allergens':['молоко'],
                               'source':'reference book p.4','review_id':'audit-102'},
        }
        self.ingredients=[{'product_id':'rice','state':'dry','grams':100},
                          {'product_id':'milk','state':'liquid','grams':200}]

    def test_cooked_yield_changes_density_not_total_energy(self):
        a=calculate_recipe(self.ingredients,self.catalog,servings=2,cooked_yield_g=500)
        b=calculate_recipe(self.ingredients,self.catalog,servings=2,cooked_yield_g=600)
        self.assertAlmostEqual(a['dish'].kcal,430)
        self.assertAlmostEqual(b['dish'].kcal,430)
        self.assertAlmostEqual(a['portion'].kcal,215)
        self.assertAlmostEqual(a['per_100g_cooked'].kcal,86)
        self.assertAlmostEqual(b['per_100g_cooked'].kcal,430/6)
        self.assertEqual(a['allergens'],('молоко',))

    def test_no_unverified_or_missing_nutrients(self):
        bad=dict(self.catalog)
        bad[('rice','dry')]=dict(bad[('rice','dry')],status='candidate')
        with self.assertRaises(IncompleteNutrition):
            calculate_recipe(self.ingredients,bad,servings=2,cooked_yield_g=500)
        with self.assertRaises(IncompleteNutrition):
            calculate_recipe(self.ingredients,{},servings=2,cooked_yield_g=500)
        with self.assertRaises(IncompleteNutrition):
            calculate_recipe(self.ingredients,{('rice','dry'):self.catalog[('rice','dry')]},servings=2,cooked_yield_g=500)

    def test_invalid_portions_and_macros(self):
        for servings in (0, 2.5, True):
            with self.assertRaises(ValueError):
                calculate_recipe(self.ingredients,self.catalog,servings=servings,cooked_yield_g=500)
        bad=dict(self.catalog)
        bad[('rice','dry')]=dict(bad[('rice','dry')],protein=100,fat=30)
        with self.assertRaises(ValueError):
            calculate_recipe(self.ingredients,bad,servings=2,cooked_yield_g=500)

    def test_cannot_fake_verified_without_audit(self):
        bad=dict(self.catalog)
        bad[('rice','dry')]=dict(bad[('rice','dry')],review_id='')
        with self.assertRaises(IncompleteNutrition):
            calculate_recipe(self.ingredients,bad,servings=2,cooked_yield_g=500)

    def test_energy_conflict_warns_without_rewriting_source(self):
        bad=dict(self.catalog)
        bad[('rice','dry')]=dict(bad[('rice','dry')],kcal=500)
        result=calculate_recipe(self.ingredients,bad,servings=2,cooked_yield_g=500)
        self.assertAlmostEqual(result['dish'].kcal,600)
        self.assertTrue(result['warnings'])

    def test_soft_shares(self):
        expected=assess_meal_balance({'Завтрак':459,'Обед':646,'Ужин':595},1700)
        self.assertTrue(all(r['status']=='within_range' for r in expected.values()))
        skew=assess_meal_balance({'Завтрак':781,'Обед':693,'Ужин':226},1700)
        self.assertEqual(skew['Завтрак']['status'],'soft_deviation')
        self.assertEqual(skew['Ужин']['status'],'soft_deviation')
        self.assertEqual(skew['Обед']['status'],'within_range')
        self.assertEqual(assess_meal_balance({},1700)['Обед']['status'],'incomplete')
        lower_total=assess_meal_balance({'Завтрак':270,'Обед':380,'Ужин':350},1700)
        self.assertTrue(all(r['status']=='within_range' for r in lower_total.values()))
        self.assertEqual(lower_total['Завтрак']['actual_daily_kcal'],1000)


if __name__=='__main__':
    unittest.main()
