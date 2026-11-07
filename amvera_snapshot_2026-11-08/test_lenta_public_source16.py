import unittest

from lenta_public_source16 import LentaSourceError, search, stores_for_city


class TestLentaSource16(unittest.TestCase):
    def test_moscow_only_and_no_tomsk(self):
        def fetch(path, params):
            self.assertEqual(path, '/v1/stores')
            return {'stores': [
                {'id': 99, 'cityName': 'Томск', 'address': 'Пушкина'},
                {'id': '321', 'cityName': 'Москва', 'address': 'Московская'},
                {'id': '322', 'cityName': 'Москва', 'type': 'zoo'},
                {'id': 'bad', 'cityName': 'Москва'}]}
        self.assertEqual(stores_for_city(fetch=fetch), [{'id': 321, 'city': 'Москва', 'address': 'Московская'}])

    def test_contract_price_kopecks_and_status(self):
        def fetch(path, params):
            self.assertEqual(path, '/catalog/v1/items')
            self.assertEqual(params['stores'], 321)
            self.assertEqual(params['channel'], 'lo')
            self.assertEqual(params['retailBrand'], 'lo')
            return {'items': [{'id': 7, 'name': 'Молоко 1 л', 'prices': {'price': 9990}},
                              {'id': 8, 'name': 'Молоко без цены'}]}
        rows = search('молоко', 321, fetch=fetch)
        self.assertEqual(rows[0]['price_rub'], 99.9)
        self.assertIsNone(rows[1]['price_rub'])
        self.assertTrue(all(r['review_status'] == 'review_required' for r in rows))
        self.assertTrue(all(r['region'] == 'не подтверждён' for r in rows))

    def test_reject_default_and_bad_channel(self):
        for store in (0, -1, None, True, '99'):
            with self.assertRaises(ValueError):
                search('молоко', store, fetch=lambda *a: {})
        for channel in ('cc', 'CC', ''):
            with self.assertRaises(ValueError):
                search('молоко', 321, channel=channel, fetch=lambda *a: {})

    def test_reject_oversize_and_broken_shape(self):
        with self.assertRaises(ValueError):
            search('a' * 81, 321, fetch=lambda *a: {})
        with self.assertRaises(LentaSourceError):
            search('молоко', 321, fetch=lambda *a: {'items': {}})
        with self.assertRaises(LentaSourceError):
            stores_for_city(fetch=lambda *a: {'stores': None})


if __name__ == '__main__':
    unittest.main()
