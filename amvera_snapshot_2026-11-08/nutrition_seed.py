"""Build 18.2: controlled, opt-in import of reference product KBJU.

These are *source transcriptions*, not independently verified nutritional facts.
Never auto-run at bot startup. Only an operator may promote records to verified.
"""
import sqlite3
from pathlib import Path

SOURCE = 'Каллорийность продуктов.pdf'
# name, preparation, category, kcal, protein, fat, carbs, source page
ROWS = [
 ('Мука пшеничная высшего сорта','dry','Мука и крупы',334,10.3,1.08,69,1),
 ('Мука пшеничная первого сорта','dry','Мука и крупы',331,10.6,1.3,67.4,1),
 ('Крупа пшённая','dry','Мука и крупы',348,11.5,3.3,67.2,1),
 ('Гречневая крупа ядрица','dry','Мука и крупы',335,12.6,3.26,54.3,1),
 ('Рисовая крупа','dry','Мука и крупы',330,7,1,73.2,1),
 ('Крупа манная','dry','Мука и крупы',328,10.3,1,67.7,1),
 ('Крупа ячневая','dry','Мука и крупы',324,10,1.3,66.31,1),
 ('Крупа перловая','dry','Мука и крупы',320,9.3,1.13,67.5,1),
 ('Хлопья геркулес','dry','Мука и крупы',305,11,6.2,49.24,1),
 ('Макароны высшего сорта','dry','Мука и крупы',337,10.4,1.13,74.9,1),
 ('Хлеб пшеничный высшего сорта','ready','Хлеб',238,7.59,0.81,50.15,1),
 ('Хлеб ржаной формовой','ready','Хлеб',181,6.62,1.2,41.82,1),
 ('Говядина первой категории','raw','Мясо',218,18.5,16,0,1),
 ('Печень говяжья','raw','Мясо',105,17.9,3.7,0,1),
 ('Яйцо куриное','raw','Яйца',157,12.7,11.5,0.7,1),
 ('Молоко коровье 3,6%','liquid','Молочные',61,3.2,3.6,5.16,1),
 ('Сметана 30%','ready','Молочные',294,2.4,30,3.18,1),
 ('Сыр российский','ready','Молочные',360,23,29,0,1),
 ('Масло сливочное несолёное 82,5%','ready','Жиры',748,0.5,82.5,0.8,2),
 ('Картофель','raw','Овощи',80,2,0.4,18.1,2),
 ('Помидоры грунтовые','raw','Овощи',23,1.1,0.2,5,2),
 ('Капуста белокочанная ранняя','raw','Овощи',27,1.8,0.2,6.8,2),
 ('Морковь красная','raw','Овощи',34,1.3,0.1,9.3,2),
 ('Огурцы грунтовые','raw','Овощи',14,0.8,0.1,3.8,2),
 ('Свёкла','raw','Овощи',42,1.5,0.1,12.8,2),
 ('Яблоки свежие','raw','Фрукты',45,0.4,0.4,11.8,2),
 ('Апельсины','raw','Фрукты',40,0.9,0.2,10.3,2),
 ('Масло подсолнечное','liquid','Жиры',899,0,99.9,0,2),
 ('Сахар','dry','Прочее',379,0,0,99.8,2),
]

def validate(row):
    name, state, category, kcal, protein, fat, carbs, page = row
    if not name or not state or not category or page < 1: raise ValueError('Incomplete row')
    if not (0 <= kcal <= 900 and all(0 <= x <= 100 for x in (protein,fat,carbs))):
        raise ValueError('Out-of-range nutrition values: '+name)
    if protein+fat+carbs > 101: raise ValueError('Macro sum exceeds 100g: '+name)
    # This flags rather than rejects discrepancies: nutrition tables can differ
    # in fiber, rounding and conversion factors.
    calculated = 4*protein+9*fat+4*carbs
    return abs(kcal-calculated) > max(25, 0.2*kcal)

def import_rows(connection: sqlite3.Connection):
    """Requires nutrition_library.init() to have created the schema."""
    flagged=[]
    for row in ROWS:
        if validate(row): flagged.append(row[0])
        name,state,category,kcal,p,f,c,page=row
        connection.execute('''INSERT INTO nutrition_products
        (canonical_name,preparation,category,kcal_100g,protein_100g,fat_100g,carbs_100g,source,source_page,confidence)
        VALUES(?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(canonical_name,preparation) DO NOTHING''',
        (name,state,category,kcal,p,f,c,SOURCE,str(page),'unverified'))
    return flagged

if __name__ == '__main__':
    import argparse
    parser=argparse.ArgumentParser()
    parser.add_argument('--db',type=Path,required=True,help='Separate SQLite staging database; NOT the bot database')
    args=parser.parse_args()
    from nutrition_library import DDL
    args.db.parent.mkdir(parents=True,exist_ok=True)
    with sqlite3.connect(args.db) as conn:
        conn.executescript(DDL)
        flagged=import_rows(conn)
        print('Products:',conn.execute('SELECT COUNT(*) FROM nutrition_products').fetchone()[0])
        print('Macro/calorie discrepancies to review:',len(flagged),', '.join(flagged))
