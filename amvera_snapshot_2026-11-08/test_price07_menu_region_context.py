"""Menu must use verified quotes only for the user's explicit region/stores."""
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import menu_generator
from menu_ui import estimate_text
from price02_pipeline import ensure, stage


class MenuRegionContextTests(unittest.TestCase):
    def test_region_and_selected_stores_are_forwarded_to_price_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / 'offline.sqlite3'
            with sqlite3.connect(database) as db:
                db.executescript('''
                    CREATE TABLE products(name TEXT, unit TEXT, category TEXT);
                    CREATE TABLE preferences(telegram_id INTEGER, value TEXT,
                        preference_type TEXT, active INTEGER);
                    CREATE TABLE user_prices(user_id INTEGER, product TEXT,
                        unit TEXT, price REAL, observed_at TEXT, shop TEXT);
                    CREATE TABLE user_settings(telegram_id INTEGER, calorie_target INTEGER);
                    CREATE TABLE user_cities(user_id INTEGER PRIMARY KEY, city TEXT);
                    CREATE TABLE user_shops(user_id INTEGER, shop TEXT);
                    INSERT INTO user_cities VALUES(77,'Москва');
                    INSERT INTO user_shops VALUES(77,'ВкусВилл');
                    INSERT INTO user_shops VALUES(77,'Лента');
                ''')

            def connect():
                handle=sqlite3.connect(database)
                handle.row_factory=sqlite3.Row
                return handle

            def planner(db, level='standard', *, city=None, selected_shops=(), include_provenance=False):
                self.assertEqual('Москва', city)
                self.assertEqual({'ВкусВилл','Лента'},set(selected_shops))
                self.assertTrue(include_provenance)
                return {}, set(), {}

            with patch.object(menu_generator,'connect_db',side_effect=connect),\
                 patch.object(menu_generator,'prices_for_menu',side_effect=planner):
                menu_generator.context(77)

    def test_validation_preserves_verified_price_provenance_and_missing(self):
        menu={'menu':{day:{meal:[{'name':'Простое блюдо','ingredients':[
            {'product':'Молоко','amount':200,'unit':'мл'},
            {'product':'Груша','amount':100,'unit':'г'}]}]
            for meal in menu_generator.MEALS} for day in menu_generator.DAYS}}
        prices=menu_generator.PriceMap({('молоко','л'):120},provenance={
            ('молоко','л'):{'quality':'verified_market','retailer':'vkusvill',
                            'region':'Москва','observed_at':'2026-11-07T10:00:00Z'}})
        products=[{'name':'Молоко'},{'name':'Груша'}]
        with patch.object(menu_generator,'purchase_estimate',return_value={
            'purchase_estimate':100.0,'unpriced':['Груша']}):
            stats=menu_generator.validate(menu,products,{'allergy':[],'dislike':[]},1,prices)
        by_name={row['product']:row for row in stats['price_provenance']}
        self.assertEqual(by_name['Молоко']['quality'],'verified_market')
        self.assertEqual(by_name['Молоко']['region'],'Москва')
        self.assertEqual(by_name['Груша']['quality'],'missing')
        text=estimate_text(stats,6000)
        self.assertIn('vkusvill · Москва · 2026-11-07',text)
        self.assertIn('отсутствуют — 1',text)
        self.assertIn('Позиций без рыночного подтверждения: 1',text)

    def test_personal_quotes_are_not_claimed_as_market_verified(self):
        stats={'known_cost':500,'purchase_estimate':550,'missing_prices':[],
               'price_provenance':[{'product':'Яйца','quality':'personal','shops':['Личный']}]}
        text=estimate_text(stats,6000)
        self.assertIn('Позиций без рыночного подтверждения: 1',text)
        self.assertIn('личные — 1',text)
        self.assertNotIn('подтверждённые магазинные — 1',text)

    def test_sqlite_verified_quote_flows_through_context_and_menu_estimate(self):
        with tempfile.TemporaryDirectory() as directory:
            database=Path(directory)/'price02.sqlite3'
            with sqlite3.connect(database) as db:
                db.executescript('''
                    CREATE TABLE products(name TEXT,unit TEXT,category TEXT);
                    CREATE TABLE preferences(telegram_id INTEGER,value TEXT,preference_type TEXT,active INTEGER);
                    CREATE TABLE user_prices(user_id INTEGER,product TEXT,unit TEXT,price REAL,observed_at TEXT,shop TEXT);
                    CREATE TABLE user_settings(telegram_id INTEGER,calorie_target INTEGER);
                    CREATE TABLE user_cities(user_id INTEGER PRIMARY KEY,city TEXT);
                    CREATE TABLE user_shops(user_id INTEGER,shop TEXT);
                    INSERT INTO products VALUES('Молоко','л','Молочные');
                    INSERT INTO user_cities VALUES(77,'Москва');
                    INSERT INTO user_shops VALUES(77,'ВкусВилл');
                ''')
                ensure(db)
                stamp=datetime.now(timezone.utc).isoformat()
                stage(db,'vkusvill','Молоко 2,5%',[{
                    'product_id':'verified-milk-test','name':'Молоко 2,5%',
                    'price_rub':117,'base_unit':'л','price_per_base_unit':117,
                    'match_status':'likely_match','observed_at':stamp,
                }],region='Москва',region_confirmed=True)
                db.execute("UPDATE pb_skus SET match_status='manually_verified'")
                db.execute("UPDATE pb_observations SET review_status='manually_verified'")
                db.commit()

            def connect():
                db=sqlite3.connect(database)
                db.row_factory=sqlite3.Row
                return db
            with patch.object(menu_generator,'connect_db',side_effect=connect):
                products,prefs,prices,_=menu_generator.context(77)
            self.assertEqual(117,prices[('молоко','л')])
            menu={'menu':{day:{meal:[{'name':'Молоко','ingredients':[
                {'product':'Молоко','amount':200,'unit':'мл'}]}]
                for meal in menu_generator.MEALS} for day in menu_generator.DAYS}}
            stats=menu_generator.validate(menu,products,prefs,1,prices)
            self.assertEqual('verified_market',stats['price_provenance'][0]['quality'])
            self.assertIn('vkusvill · Москва',estimate_text(stats,6000))
