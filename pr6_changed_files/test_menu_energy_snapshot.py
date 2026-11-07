"""Regression tests for one-time PRICE07 energy snapshots."""
import copy
import asyncio
from io import BytesIO
import json
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from unittest.mock import AsyncMock
from types import SimpleNamespace
import aiogram_stub
aiogram_stub.install()
import menu_ui

from menu_energy_snapshot import (build_energy_snapshot, saved_energy_snapshot,
                                  SnapshotMismatch, DAYS, MEALS)


def week_menu(product='новый продукт'):
    return {'menu': {day: {meal: [{'name': 'Блюдо', 'ingredients': [
        {'product': product, 'amount': 100, 'unit': 'г'}]}] for meal in MEALS}
                     for day in DAYS}}


class EnergySnapshotTests(unittest.TestCase):
    def test_exact_final_portions_and_people(self):
        menu=week_menu()
        catalog={'новый продукт': {'price07_code': 'P7', 'price07_kcal_100g': 350}}
        snapshot=build_energy_snapshot(menu,catalog,people=2)
        self.assertEqual(snapshot['days'][DAYS[0]]['meals'][MEALS[0]]['kcal'],350)
        self.assertEqual(snapshot['week']['kcal'],7350)
        self.assertEqual(snapshot['week']['all_people_kcal'],14700)
        self.assertEqual(saved_energy_snapshot(menu,{'energy_snapshot':snapshot}),snapshot)

    def test_unknown_does_not_mean_zero(self):
        menu=week_menu('неизвестный ингредиент')
        snapshot=build_energy_snapshot(menu,{},lookup=lambda *args: None)
        self.assertIsNone(snapshot['week']['kcal'])
        self.assertEqual(snapshot['week']['known_kcal'],0)
        self.assertTrue(snapshot['missing'])

    def test_no_legacy_silent_refresh(self):
        self.assertIsNone(saved_energy_snapshot(week_menu(),{'known_kcal':9999}))

    def test_changed_portions_or_snapshot_rejected(self):
        menu=week_menu()
        snapshot=build_energy_snapshot(menu,{},lookup=lambda *args: 40)
        changed=copy.deepcopy(menu)
        changed['menu'][DAYS[0]][MEALS[0]][0]['ingredients'][0]['amount']=110
        with self.assertRaises(SnapshotMismatch):
            saved_energy_snapshot(changed,{'energy_snapshot':snapshot})
        tampered=copy.deepcopy(snapshot)
        tampered['week']['kcal']=1
        with self.assertRaises(SnapshotMismatch):
            saved_energy_snapshot(menu,{'energy_snapshot':tampered})

    def test_day_card_and_export_text_keep_frozen_price07_kcal(self):
        menu=week_menu()
        catalog={'новый продукт': {'price07_code':'P7','price07_kcal_100g':350}}
        snapshot=build_energy_snapshot(menu,catalog)
        stats={'energy_snapshot':snapshot,'known_cost':100,'purchase_estimate':100,
               'missing_prices':[],'missing_kcal':[],'estimated_prices':[],
               'diet_mode':'regular'}
        before=menu_ui.day_text(menu,0,1,1700,stats)
        self.assertIn('350 ккал/чел.',before)
        # Simulates TXT/DOCX text source, which must not re-read the catalog.
        combined='\n\n'.join(menu_ui.day_text(menu,i,1,1700,stats) for i in range(7))
        self.assertEqual(combined.count('350 ккал/чел.'),21)
        self.assertIn('1050 ккал/чел.',combined)
        old=menu_ui.day_text(menu,0,1,1700,stats={})
        self.assertIn('старое меню без снимка',old)
        self.assertNotIn('350 ккал/чел.',old)
        summary=menu_ui.summary(menu,stats,1,6000)
        self.assertIn('1050 ккал',summary)

    def test_sqlite_save_then_reprice_preserves_frozen_energy(self):
        menu=week_menu()
        catalog={'новый продукт': {'price07_code':'P7','price07_kcal_100g':350}}
        snapshot=build_energy_snapshot(menu,catalog)
        stats={'ingredients':{('новый продукт','г'):2100},'known_cost':1,
               'purchase_estimate':1,'known_kcal':7350,'missing_kcal':[],
               'missing_prices':[],'missing_purchase_prices':[],
               'energy_snapshot':snapshot,'diet_mode':'regular'}
        with tempfile.TemporaryDirectory() as folder:
            path=folder+'/menus.sqlite'
            def connection():
                db=sqlite3.connect(path)
                db.row_factory=sqlite3.Row
                return db
            with connection() as db:
                db.executescript('''
                    CREATE TABLE menus(id INTEGER PRIMARY KEY,owner_id INTEGER,
                      people_count INTEGER,budget REAL,calorie_target REAL,result_json TEXT);
                    CREATE TABLE menu_runtime_settings(key TEXT PRIMARY KEY,value TEXT);
                    CREATE TABLE zm_menu_price_versions(parent_menu_id INTEGER,new_menu_id INTEGER);
                    CREATE TABLE zm_menu_latest(owner_id INTEGER,menu_id INTEGER);
                    INSERT INTO menu_runtime_settings VALUES('u7:settings_rev','0');
                ''')
            fresh_stats={**stats,'ingredients':{('новый продукт','г'):2100},
                         'known_kcal':1,'missing_kcal':['новый продукт']}
            with (patch.object(menu_ui,'connect_db',connection),
                  patch.object(menu_ui,'_parameters_key',return_value='p'),
                  patch.object(menu_ui.menu_library,'remember'),
                  patch.object(menu_ui.menu_library,'settings_rev',return_value=0),
                  patch.object(menu_ui,'context',return_value=([],{}, {},None)),
                  patch.object(menu_ui,'validate',return_value=fresh_stats),
                  patch.object(menu_ui,'validate_diet')):
                original=menu_ui._save_generated(7,1,6000,1700,'regular',{},menu,stats)
                revised=menu_ui._reprice_saved_menu(7,original)
            self.assertIsNotNone(revised)
            with connection() as db:
                original_saved=json.loads(db.execute('SELECT result_json FROM menus WHERE id=?',(original,)).fetchone()[0])
                revised_saved=json.loads(db.execute('SELECT result_json FROM menus WHERE id=?',(revised[0],)).fetchone()[0])
            self.assertEqual(original_saved['stats']['energy_snapshot'],snapshot)
            self.assertEqual(revised_saved['stats']['energy_snapshot'],snapshot)
            self.assertEqual(revised_saved['stats']['known_kcal'],7350)
            self.assertIn('350 ккал/чел.',menu_ui.day_text(revised_saved['menu'],0,1,1700,revised_saved['stats']))

    def test_real_txt_and_docx_callbacks_from_saved_sqlite(self):
        from docx import Document
        menu=week_menu()
        snapshot=build_energy_snapshot(menu,{'новый продукт': {'price07_code':'P7','price07_kcal_100g':350}})
        stats={'ingredients':{('новый продукт','г'):2100},'energy_snapshot':snapshot,
               'known_cost':100,'purchase_estimate':100,'known_kcal':7350,
               'missing_kcal':[],'missing_prices':[],'missing_purchase_prices':[],
               'estimated_prices':[],'diet_mode':'regular'}
        with tempfile.TemporaryDirectory() as folder:
            path=folder+'/menu.sqlite'
            def connect():
                db=sqlite3.connect(path);db.row_factory=sqlite3.Row
                return db
            with connect() as db:
                db.executescript('''CREATE TABLE menus(id INTEGER PRIMARY KEY,owner_id INTEGER,
                  people_count INTEGER,budget REAL,calorie_target REAL,result_json TEXT);
                  CREATE TABLE menu_runtime_settings(key TEXT PRIMARY KEY,value TEXT);
                  INSERT INTO menu_runtime_settings VALUES('u7:settings_rev','0');''')
            with (patch.object(menu_ui,'connect_db',connect),
                  patch.object(menu_ui,'_parameters_key',return_value='p'),
                  patch.object(menu_ui.menu_library,'remember')):
                saved_id=menu_ui._save_generated(7,1,6000,1700,'regular',{},menu,stats)
            async def fake_safe_call(fn,*args,**kwargs):
                return await fn(*args,**kwargs)
            sent=[]
            for ext in ('txt','docx'):
                message=SimpleNamespace(answer_document=AsyncMock())
                callback=SimpleNamespace(data=f'menugen:export:{saved_id}:{ext}',
                    from_user=SimpleNamespace(id=7),message=message,answer=AsyncMock())
                with (patch.object(menu_ui,'connect_db',connect),
                      patch.object(menu_ui,'check_callback',new=AsyncMock(return_value=True)),
                      patch.object(menu_ui,'safe_call',new=fake_safe_call)):
                    asyncio.run(menu_ui.actions(callback,SimpleNamespace()))
                self.assertEqual(message.answer_document.await_count,1)
                item=message.answer_document.await_args.args[0]
                self.assertEqual(item.filename,f'zhorik_menu_{saved_id}.{ext}')
                sent.append(item.data)
            txt=sent[0].decode('utf-8-sig')
            self.assertIn('350 ккал/чел.',txt)
            self.assertEqual(txt.count('350 ккал/чел.'),21)
            doc=Document(BytesIO(sent[1]))
            paragraphs='\n'.join(p.text for p in doc.paragraphs)
            self.assertEqual(paragraphs.count('350 ккал/чел.'),21)
            self.assertIn('1050 ккал/чел.',paragraphs)


if __name__=='__main__':
    unittest.main()
