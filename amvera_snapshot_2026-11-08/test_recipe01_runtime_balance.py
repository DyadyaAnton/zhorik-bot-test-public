import unittest
from recipe01_runtime_balance import balance_for_menu


class RuntimeBalanceTests(unittest.TestCase):
    def test_empty_recipe_is_incomplete_not_zero_kcal(self):
        menu={'menu':{'Понедельник':{
            'Завтрак':[{'ingredients':[]}],
            'Обед':[{'ingredients':[{'product':'Рис','amount':100,'unit':'г'}]}],
            'Ужин':[{'ingredients':[{'product':'Гречка','amount':100,'unit':'г'}]}]}}}
        self.assertEqual(balance_for_menu(menu,1700)['Понедельник']['Завтрак']['status'],'incomplete')

    def test_unknown_component_never_appears_as_zero(self):
        menu={'menu':{'Понедельник':{
            'Завтрак':[{'ingredients':[{'product':'Овсяные хлопья','amount':100,'unit':'г'}]}],
            'Обед':[{'ingredients':[{'product':'совершенно неизвестный продукт','amount':100,'unit':'г'}]}],
            'Ужин':[{'ingredients':[{'product':'Гречка','amount':100,'unit':'г'}]}]}}}
        report=balance_for_menu(menu,1700)['Понедельник']
        self.assertEqual(report['Обед']['status'],'incomplete')
        self.assertEqual(report['Завтрак']['status'],'incomplete')

    def test_complete_meals_report_soft_deviations(self):
        # 100g of each product has a known approximate energy; all three
        # groups must yield shares and never hard-reject a menu.
        menu={'menu':{'Понедельник':{meal:[{'ingredients':[{'product':product,
             'amount':100,'unit':'г'}]}] for meal,product in (
             ('Завтрак','Овсяные хлопья'),('Обед','Гречка'),('Ужин','Рис'))}}}
        report=balance_for_menu(menu,1700)['Понедельник']
        self.assertTrue(all(report[meal]['status'] in ('within_range','soft_deviation')
                            for meal in ('Завтрак','Обед','Ужин')))
        self.assertAlmostEqual(sum(report[m]['actual_share'] for m in report),1)

if __name__=='__main__':unittest.main()
