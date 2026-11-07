"""Integration gates for reviewed new foods in weekly menus."""
import sqlite3
import unittest
import asyncio
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from price07_rev2_core import init as price07_init
from price07_nutrition_review import init as nutrition_init, verify_discovered_nutrition
from price07_menu_products import reviewed_menu_products, menu_energy, menu_blocked
from menu_generator import validate, MenuInvalid, blocked, DAYS, MEALS, context, build
from llm_service import LLMError


class NewFoodMenuTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        price07_init(self.db)
        self.db.execute('''INSERT INTO p7_discovered_products
            (code,canonical_name,title_fingerprint,first_retailer,first_sku_id)
            VALUES('NPTEST123','Печенье уникальное','reviewed-fingerprint','vkusvill','sku100')''')
        self.db.commit()
        nutrition_init(self.db)

    def tearDown(self):
        self.db.close()

    def review(self):
        verify_discovered_nutrition(self.db, code='NPTEST123', actor_id=1234,
             kcal_100g=450,protein_100g=7,fat_100g=20,carbs_100g=60,
             allergens=['пшеница','молоко'], source='https://example.com/label',
             note='Владелец сверил полную маркировку и таблицу КБЖУ')

    def test_no_owner_review_no_recipe_access(self):
        self.assertEqual([], reviewed_menu_products(self.db))
        self.db.execute('''UPDATE p7_discovered_products SET review_status='verified',
            kbju_verified=1,allergens_verified=1 WHERE code='NPTEST123' ''')
        self.assertEqual([], reviewed_menu_products(self.db))

    def test_verified_product_has_evidence_energy_without_guessed_price(self):
        self.review()
        products = reviewed_menu_products(self.db)
        self.assertEqual(1,len(products))
        self.assertEqual('г',products[0]['unit'])
        catalog = {products[0]['name'].casefold():products[0]}
        self.assertEqual(225.0,menu_energy('Печенье уникальное',50,catalog))
        self.assertEqual([],reviewed_menu_products(self.db, ['печенье уникальное']))

    def test_allergic_user_fail_closed_even_when_allergen_synonym_is_unknown(self):
        self.review()
        product=reviewed_menu_products(self.db)[0]
        catalog={product['name'].casefold():product}
        self.assertTrue(menu_blocked(product['name'],{'allergy':['молоко'],'dislike':[]},catalog,blocked))
        self.assertTrue(menu_blocked(product['name'],{'allergy':['глютен'],'dislike':[]},catalog,blocked))
        self.assertFalse(menu_blocked(product['name'],{'allergy':[],'dislike':[]},catalog,blocked))

    def test_validator_accepts_verified_grams_but_rejects_pieces_and_allergy(self):
        self.review()
        product=reviewed_menu_products(self.db)[0]
        name=product['name']
        def menu(unit):
            return {'menu':{day:{meal:[{'name':'Перекус','ingredients':[
                {'product':name,'amount':50,'unit':unit}]}] for meal in MEALS} for day in DAYS}}
        prefs={'allergy':[],'dislike':[],'like':[]}
        stats=validate(menu('г'),[product],prefs,1,{})
        self.assertGreater(stats['known_kcal'],0)
        self.assertIn(name,stats['missing_prices'])
        with self.assertRaises(MenuInvalid):validate(menu('шт'),[product],prefs,1,{})
        with self.assertRaises(MenuInvalid):validate(menu('г'),[product],{**prefs,'allergy':['молоко']},1,{})

    def test_build_prompt_contains_reviewed_product_only_without_allergies(self):
        self.review()
        product=reviewed_menu_products(self.db)[0]
        recorded=[]
        async def intercept(messages,provider=None):
            recorded.append(json.loads(messages[1]['content']))
            raise LLMError('Проверка без внешнего ИИ')
        def exercise(allergies):
            prefs={'allergy':allergies,'dislike':[],'like':[]}
            with patch('menu_generator.context',return_value=([product],prefs,{},None)), \
                 patch('menu_generator.generate_json',side_effect=intercept):
                with self.assertRaises(MenuInvalid):
                    asyncio.run(build(1234,1,6000,1700))
            return recorded[-1]['products']
        self.assertIn(product['name'],[p['name'] for p in exercise([])])
        self.assertNotIn(product['name'],[p['name'] for p in exercise(['молоко'])])

    def test_context_on_amvera_copy_does_not_guess_new_product_price(self):
        original=os.environ.get('JORIK_TEST_DB_COPY')
        if not original or not Path(original).is_file():
            self.skipTest('Read-only Amvera snapshot not supplied in CI')
        with tempfile.TemporaryDirectory() as temp:
            testdb=Path(temp)/'stage.sqlite3'
            with sqlite3.connect(f'file:{Path(original).resolve()}?mode=ro',uri=True) as source:
                with sqlite3.connect(testdb) as target:
                    source.backup(target)
            with sqlite3.connect(testdb) as setup:
                price07_init(setup)
                setup.commit()
                nutrition_init(setup)
                setup.execute('''INSERT OR IGNORE INTO p7_discovered_products
                    (code,canonical_name,title_fingerprint,first_retailer,first_sku_id)
                    VALUES('NPTEST456','Экспериментальная крупа QA','qa-isolated-fingerprint',
                           'vkusvill','qa-local-sku')''')
                verify_discovered_nutrition(setup,code='NPTEST456',actor_id=1234,
                    kcal_100g=320,protein_100g=10,fat_100g=5,carbs_100g=50,
                    allergens=['глютен'],source='https://example.com/qa-label',
                    note='Проверена маркировка тестовой копии базы')
                setup.commit()
            def connect_copy():
                db=sqlite3.connect(testdb)
                db.row_factory=sqlite3.Row
                return db
            with patch('menu_generator.connect_db',side_effect=connect_copy):
                products,_,prices,_=context(1234)
            self.assertIn('Экспериментальная крупа QA',[p['name'] for p in products])
            self.assertNotIn(('экспериментальная крупа qa','г'),prices)


if __name__ == '__main__':
    unittest.main()
