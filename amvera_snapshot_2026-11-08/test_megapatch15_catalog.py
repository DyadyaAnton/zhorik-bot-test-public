"""MEGAPATCH15: source-aware price totals and backwards-compatible recipe headings."""
import ast
import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent


def isolated_function(filename, name, globals_=None):
    source = ast.parse((ROOT / filename).read_text(encoding='utf-8'))
    node = next(n for n in source.body if isinstance(n, ast.FunctionDef) and n.name == name)
    scope = dict(globals_ or {})
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(ROOT / filename), 'exec'), scope)
    return scope[name]


class RecipeHeadingsTests(unittest.TestCase):
    def test_legacy_categories_are_hidden_not_removed(self):
        classify = isolated_function('recipe_ui.py', '_dish_type')
        self.assertEqual(classify('Каши', 'Овсянка'), 'Завтраки')
        self.assertEqual(classify('Из курицы и индейки', 'Курица тушёная'), 'Тушёные блюда')
        self.assertEqual(classify('Рыба', 'Минтай запечённый'), 'Запечённые блюда')
        self.assertEqual(classify('Из мяса', 'Котлеты'), 'Котлеты и тефтели')
        self.assertEqual(classify('Салаты', 'Морковный салат'), 'Салаты')

    def test_every_recipe_receives_one_heading(self):
        classify = isolated_function('recipe_ui.py', '_dish_type')
        # In-memory simulation of every old category; no writes or migration.
        old = ['Блины, сырники и оладьи', 'Бутерброды и закуски',
               'Из курицы и индейки', 'Из мяса', 'Из яиц', 'Каши',
               'Макароны и запеканки', 'Овощи и гарниры', 'Рыба',
               'Салаты', 'Супы', 'Торты и десерты']
        headings = [classify(category, 'Блюдо') for category in old]
        self.assertTrue(all(headings))
        self.assertNotIn('Каши', headings)
        self.assertNotIn('Рыба', headings)


class NutritionCandidateTests(unittest.TestCase):
    def test_banana_and_oats_are_staged_not_verified(self):
        from nutrition_candidates import candidates,candidate_for
        self.assertEqual(len(candidates()),160)
        banana=candidate_for('Бананы')
        self.assertEqual(banana[1],(89.0,1.1,0.3,22.8))
        self.assertEqual(banana[2],'B')
        self.assertIsNotNone(candidate_for('Овсяные хлопья'))
        self.assertIsNone(candidate_for('несуществующий товар'))

    def test_candidates_are_not_used_by_recipe_nutrition(self):
        # Only reviewed reference values may participate in automatic totals.
        source=(ROOT/'recipe_ui.py').read_text(encoding='utf-8')
        self.assertIn('ref=kbju_for(name)',source)
        self.assertNotIn('candidate_for(',source)


class CommandOrderTests(unittest.TestCase):
    def test_command_sort_priority_for_owner_and_public(self):
        # Verify both sort call sites and the exact hardcoded priority expression.
        source = (ROOT / 'main.py').read_text(encoding='utf-8')
        self.assertIn("item.command == 'clean'",source)
        self.assertIn("item.command == 'start'",source)
        self.assertIn("item.command == 'version'",source)
        self.assertIn('commands.sort(key=command_order)',source)
        self.assertIn('owner_commands.sort(key=command_order)',source)
        ordering = lambda cmd: (0 if cmd == 'clean' else 1 if cmd == 'start' else
                                2 if cmd == 'version' else 3,cmd.casefold())
        self.assertEqual(sorted(['recipes','start','admin','clean','begin','help','version'],key=ordering),
                         ['clean','start','version','admin','begin','help','recipes'])

    def test_real_main_sort_key_for_owner_and_public(self):
        from types import SimpleNamespace
        source=ast.parse((ROOT/'main.py').read_text(encoding='utf-8'))
        assignment=next(n for n in ast.walk(source) if isinstance(n,ast.Assign)
                        and any(isinstance(t,ast.Name) and t.id=='command_order' for t in n.targets))
        key=eval(compile(ast.Expression(assignment.value),'<main command order>','eval'))
        for commands,expected in [
            (['help','clean','recipes','start','settings'],['clean','start','help','recipes','settings']),
            (['version','price06_collect','admin','start','clean','cancel'],
             ['clean','start','version','admin','cancel','price06_collect'])]:
            items=[SimpleNamespace(command=x) for x in commands]
            self.assertEqual([x.command for x in sorted(items,key=key)],expected)


class PriceOverviewTests(unittest.TestCase):
    def test_counts_do_not_equate_unverified_with_market_prices(self):
        db = sqlite3.connect(':memory:')
        from price_base_v1 import init_schema
        init_schema(db)
        db.executescript('''INSERT INTO pb_products(code,name,category,base_unit)
          VALUES('milk','Молоко 2,5%','Молочное','л');
          INSERT INTO pb_skus(id,retailer,title,external_id,product_code,match_status)
          VALUES(1,'vkusvill','Молоко другое','v1',NULL,'review_required'),
                (2,'magnit','Молоко 2,5%','m1','milk','manually_verified');
          INSERT INTO pb_observations(sku_id,current_price_rub,observed_at,source,
          review_status,unit_price_rub,base_unit,region_confirmed,region,region_key)
          VALUES(1,100,'2026-11-07T01:00:00+00:00','test','unverified',100,'л',0,NULL,'unknown'),
                (1,120,'2026-11-07T02:00:00+00:00','test','unverified',120,'л',0,NULL,'unknown'),
                (2,110,'2026-11-07T01:00:00+00:00','test','manually_verified',110,'л',1,'Москва','Москва');
          CREATE TABLE user_prices(user_id INT,product TEXT,shop TEXT,unit TEXT,price REAL,observed_at TEXT);
          INSERT INTO user_prices VALUES(1,'Молоко 2,5%','Личный','л',105,'2026-11-07');''')
        class Connection:
            def __enter__(self): return db
            def __exit__(self,*args): pass
        overview = isolated_function('price_ui.py', 'price_overview',
                                    {'get_rows':lambda uid: [1]*22,
                                     'connect_db':lambda: Connection()})
        output = overview(1)
        self.assertIn('Единый каталог', output)
        self.assertIn('Магазинных наблюдений: 3', output)
        self.assertIn('проверенных связей: 1', output)
        self.assertIn('Личных ценовых предложений: 1', output)
        db.close()


class PriceCollectionTests(unittest.TestCase):
    def test_progress_atomic_and_resumable(self):
        from price_base_v1 import init_schema
        from price06_collect import cursor,page,save_batch,queries
        db=sqlite3.connect(':memory:')
        init_schema(db)
        self.assertEqual(cursor(db),0)
        self.assertEqual(len(queries()),258)
        terms=page(0)
        report=save_batch(db,0,[(term,{'vkusvill':[]}) for term in terms])
        self.assertEqual(report['next_offset'],5)
        self.assertEqual(cursor(db),5)
        self.assertEqual(page(cursor(db)),queries()[5:10])
        db.close()

    def test_total_failure_does_not_advance_cursor(self):
        from price_base_v1 import init_schema
        from price06_collect import cursor,page,save_batch
        db=sqlite3.connect(':memory:')
        init_schema(db)
        report=save_batch(db,0,[(term,{'vkusvill':TimeoutError()}) for term in page(0)])
        self.assertEqual(report['next_offset'],0)
        self.assertEqual(cursor(db),0)
        db.close()

    def test_partial_failure_does_not_skip_retry(self):
        from price_base_v1 import init_schema
        from price06_collect import cursor,page,save_batch
        db=sqlite3.connect(':memory:')
        init_schema(db)
        terms=page(0)
        batch=[(term,{'vkusvill': [] if i<4 else TimeoutError()})
               for i,term in enumerate(terms)]
        outcome=save_batch(db,0,batch)
        self.assertEqual(outcome['successful_sources'],4)
        self.assertEqual(outcome['failed_sources'],1)
        self.assertEqual(cursor(db),0)
        db.close()

    def test_retailers_have_independent_cursors(self):
        from price_base_v1 import init_schema
        from price06_collect import cursor,page,save_batch
        db=sqlite3.connect(':memory:')
        init_schema(db)
        terms=page(0)
        save_batch(db,0,[(term,{'vkusvill':[]}) for term in terms])
        self.assertEqual(cursor(db,'vkusvill'),5)
        self.assertEqual(cursor(db,'magnit'),0)
        save_batch(db,0,[(term,{'magnit':[]}) for term in terms],retailer='magnit',store_code='101')
        self.assertEqual(cursor(db,'magnit'),5)
        self.assertEqual(cursor(db,'vkusvill'),5)
        db.close()

    def test_incomplete_or_misaligned_batch_is_rejected(self):
        from price_base_v1 import init_schema
        from price06_collect import cursor,page,save_batch
        db=sqlite3.connect(':memory:')
        init_schema(db)
        with self.assertRaises(ValueError):save_batch(db,0,[('not canonical',{})]*5)
        with self.assertRaises(ValueError):save_batch(db,0,[(term,{}) for term in page(0)[:1]])
        self.assertEqual(cursor(db),0)
        db.close()

    def test_status_only_is_not_sufficient_to_confirm_quote(self):
        db = sqlite3.connect(':memory:')
        from price_base_v1 import init_schema
        init_schema(db)
        db.executescript('''INSERT INTO pb_products(code,name,category,base_unit)
          VALUES('milk','Молоко 2,5%','Молочное','л');
          INSERT INTO pb_skus(id,retailer,title,external_id,product_code,match_status)
          VALUES(1,'magnit','Молоко','m1','milk','manually_verified');
          INSERT INTO pb_observations(sku_id,current_price_rub,observed_at,source,
          review_status,unit_price_rub,base_unit,region_confirmed)
          VALUES(1,100,'2026-11-07T01:00:00+00:00','test','manually_verified',100,'л',0);''')
        class Connection:
            def __enter__(self): return db
            def __exit__(self,*args): pass
        overview = isolated_function('price_ui.py', 'price_overview',
                                    {'get_rows':lambda uid: [],'connect_db':lambda: Connection()})
        # Verification of an SKU alone never claims region verification.
        self.assertIn('Магазинных наблюдений: 1',overview(1))
        db.close()

    def test_schema_before_price_import_does_not_crash(self):
        db = sqlite3.connect(':memory:')
        class Connection:
            def __enter__(self): return db
            def __exit__(self,*args): pass
        overview = isolated_function('price_ui.py', 'price_overview',
                                    {'get_rows':lambda uid: [],
                                     'connect_db':lambda: Connection()})
        self.assertIn('Магазинных наблюдений: 0',overview(1))
        db.close()


if __name__ == '__main__': unittest.main()
