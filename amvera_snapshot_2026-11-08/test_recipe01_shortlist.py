import sqlite3
import unittest
from recipe01_shortlist import select_editorial_pilot


class Recipe01ShortlistTests(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:')
        self.db.executescript('''CREATE TABLE zr_recipes (
          id TEXT PRIMARY KEY,title TEXT,category TEXT,meal TEXT,
          ingredients_json TEXT,preparation TEXT,nutrition_status TEXT,active INTEGER);
          INSERT INTO zr_recipes VALUES
          ('1','Рис с яблоком','Каша','Завтрак','[{"product":"Рис","amount":80,"unit":"г"}]','Варить','estimated_unverified',1),
          ('2','Яйцо с томатом','Яйца','Завтрак','[{"product":"Яйцо","amount":60,"unit":"г"}]','Жарить','estimated_unverified',1),
          ('3','Салат','Салаты','Ужин','[{"product":"Огурец","amount":80,"unit":"г"}]','Нарезать','estimated_unverified',1);
          ''')

    def tearDown(self):self.db.close()

    def test_selection_is_stable_and_never_verified(self):
        targets={'Завтрак':2,'Ужин':1}
        a=select_editorial_pilot(self.db,targets)
        self.assertEqual(a,select_editorial_pilot(self.db,targets))
        self.assertEqual(3,len(a))
        self.assertTrue(all(i['review_status']=='needs_editorial_review' and
                            i['nutrition_status']=='unverified' for i in a))
        self.assertEqual('salad',a[-1]['component_role'])
        self.assertEqual(3,self.db.execute('SELECT count(*) FROM zr_recipes').fetchone()[0])

    def test_insufficient_candidates_is_explicit(self):
        with self.assertRaises(ValueError):
            select_editorial_pilot(self.db,{'Обед':1})

    def test_invalid_grams_and_units_are_excluded(self):
        invalids=['{"product":"Рис","amount":-1,"unit":"г"}',
                  '{"product":"Рис","amount":"80","unit":"г"}',
                  '{"product":"Рис","amount":80,"unit":"шт"}',
                  '{"product":"Рис","amount":80}']
        for raw in invalids:
            with self.subTest(raw=raw):
                self.db.execute('UPDATE zr_recipes SET ingredients_json=? WHERE id=?',
                                ('['+raw+']','1'))
                with self.assertRaises(ValueError):
                    select_editorial_pilot(self.db,{'Завтрак':2})


if __name__=='__main__':unittest.main()
