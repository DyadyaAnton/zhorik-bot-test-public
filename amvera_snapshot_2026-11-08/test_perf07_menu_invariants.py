"""PERF07: preserve hard validation while avoiding redundant work."""
import unittest
import inspect

import autonomous_menu as planner


class Perf07MenuInvariantTests(unittest.TestCase):
    def test_canonical_recipe_features_are_stable(self):
        # Repeated names map to the same diversity family, not distinct dishes.
        from menu_product_identity import key
        self.assertEqual(key('Творог'), key('Творог 5%'))

    def test_single_recipe_builder_keeps_validation_default(self):
        self.assertIs(inspect.signature(planner._build_one).parameters['validate_initial'].default, True)

    def test_diversity_ranking_never_admits_over_budget_basket(self):
        from menu_rules import budget_limit
        budget=6000
        skeleton={'menu': {day: {meal: [{'name':'X','ingredients':[
            {'product':'Рис','amount':100,'unit':'г'}]}] for meal in planner.MEALS}
            for day in planner.DAYS}}
        result=planner._rank_feasible_baskets(
            [(budget_limit(budget)+1, skeleton, {}),
             (budget_limit(budget)-1, skeleton, {})], budget)
        self.assertTrue(all(cost<=budget_limit(budget) for cost,*_ in result))


if __name__=='__main__':
    unittest.main()
