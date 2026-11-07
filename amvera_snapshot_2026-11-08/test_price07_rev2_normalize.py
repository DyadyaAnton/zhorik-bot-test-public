import unittest
from price07_rev2_normalize import normalized_unit_price


class NormalizeRev2Tests(unittest.TestCase):
    def test_multipack(self):
        value = normalized_unit_price('Крупа ЛЕНТА в пакетиках, 5х80г', 49.99)
        self.assertEqual(value['base_unit'], 'кг')
        self.assertEqual(value['unit_price_rub'], 124.98)

    def test_milk_and_different_units(self):
        self.assertEqual(normalized_unit_price('Молоко 2,5% 950мл', 99.99)['base_unit'], 'л')
        self.assertEqual(normalized_unit_price('Молоко 2,5% 900г', 59.99)['base_unit'], 'кг')

    def test_unknown_size(self):
        self.assertIsNone(normalized_unit_price('Сыр Премиум', 99))
        self.assertIsNone(normalized_unit_price('Сыр 300г', None))

    def test_ambiguous_more_than_one_package_quantity_is_not_normalized(self):
        self.assertIsNone(normalized_unit_price('Набор: 2 шт + 500 мл', 200))
        self.assertIsNone(normalized_unit_price('Молоко 1 л + 500 мл', 150))
        self.assertIsNone(normalized_unit_price('Печенье 5х80г + подарок 20г', 100))

    def test_extreme_numeric_price_cannot_crash_queue(self):
        self.assertIsNone(normalized_unit_price('Молоко 1л', '1e999'))


if __name__ == '__main__':
    unittest.main()
