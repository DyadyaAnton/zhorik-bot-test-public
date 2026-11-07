"""Expandable recipe index. Entries are template variants, not verified recipes.

Do not pretend nutrition, portion weight or preparation times have been
individually verified. Only recipes with known ingredient calories may be
offered to the autonomous menu planner.
"""
from __future__ import annotations

import json
import re
import hashlib
import uuid
from calorie_catalog import energy

# Family, meals, canonical base recipes and optional variants. Amounts are
# grams per person of raw ingredients. The generated variants are explicitly
# marked as templates so editorial review can replace them in later patches.
FAMILIES = {
 "Каши": (35, "Завтрак", [
  ("Овсяная каша", [("Овсяные хлопья",90)]), ("Рисовая каша",[("Рис",90)]),
  ("Гречневая каша",[("Гречка",90)]), ("Молочная овсянка",[("Овсяные хлопья",85),("Молоко",150)]),
  ("Молочная рисовая",[("Рис",85),("Молоко",160)])],
  [("с яблоком",[("Яблоки",100)]),("с бананом",[("Бананы",100)]),
   ("с творогом",[("Творог",100)]),("со сливочным маслом",[("Масло сливочное",10)]),
   ("с сахаром",[("Сахар",12)]),("с яблоком и творогом",[("Яблоки",60),("Творог",65)]),
   ("с бананом и молоком",[("Бананы",60),("Молоко",100)])]),
 "Супы": (50,"Обед",[
  ("Суп куриный",[("Куриное филе",130),("Картофель",140),("Морковь",50)]),
  ("Щи",[("Капуста белокочанная",200),("Картофель",130),("Морковь",45)]),
  ("Суп картофельный",[("Картофель",210),("Морковь",60)]),
  ("Суп рисовый",[("Рис",65),("Картофель",110),("Морковь",55)]),
  ("Суп гречневый",[("Гречка",65),("Картофель",100),("Морковь",55)])],
  [("с курицей",[("Куриное филе",90)]),("с говядиной",[("Говядина",90)]),
   ("с овощами",[("Капуста белокочанная",90)]),("с томатами",[("Помидоры",90)]),
   ("с луком",[("Лук",45)]),("с рисом",[("Рис",35)]),
   ("с гречкой",[("Гречка",35)]),("с картофелем",[("Картофель",80)]),
   ("с курицей и луком",[("Куриное филе",65),("Лук",30)]),
   ("с морковью",[("Морковь",60)])]),
 "Из яиц": (30,"Завтрак",[
  ("Омлет",[("Яйца",110),("Молоко",50)]),
  ("Яичница",[("Яйца",120)]),
  ("Яичная запеканка",[("Яйца",110),("Творог",80)]),
  ("Яичный рулет",[("Яйца",110),("Мука",15)]),
  ("Яйца с картофелем",[("Яйца",100),("Картофель",150)])],
  [("с помидорами",[("Помидоры",95)]),("с сыром",[("Сыр твердый",35)]),
   ("с хлебом",[("Хлеб ржаной",55)]),("с овощами",[("Морковь",55)]),
   ("с курицей",[("Куриное филе",75)]),("с творогом",[("Творог",70)])]),
 "Из курицы и индейки": (55,"Обед",[
  ("Куриное филе запечённое",[("Куриное филе",190)]),
  ("Курица тушёная",[("Куриное филе",190),("Лук",40)]),
  ("Куриная запеканка",[("Куриное филе",160),("Картофель",110)]),
  ("Куриное рагу",[("Куриное филе",175),("Морковь",80)]),
  ("Индейка на пару",[("Индейка филе",190)])],
  [("с рисом",[("Рис",75)]),("с гречкой",[("Гречка",75)]),
   ("с картофелем",[("Картофель",180)]),("с макаронами",[("Макароны",85)]),
   ("с капустой",[("Капуста белокочанная",170)]),
   ("с помидорами",[("Помидоры",140)]),
   ("с морковью",[("Морковь",130)]),("с огурцами",[("Огурцы",130)]),
   ("с рисом и овощами",[("Рис",60),("Морковь",75)]),
   ("с гречкой и капустой",[("Гречка",60),("Капуста белокочанная",85)]),
   ("с картофелем и морковью",[("Картофель",130),("Морковь",80)])]),
 "Из мяса": (55,"Обед",[
  ("Говядина тушёная",[("Говядина",180)]),
  ("Говядина запечённая",[("Говядина",185)]),
  ("Свинина тушёная",[("Свинина",170)]),
  ("Свинина запечённая",[("Свинина",175)]),
  ("Мясное рагу",[("Говядина",155),("Морковь",70)])],
  [("с рисом",[("Рис",75)]),("с гречкой",[("Гречка",75)]),
   ("с картофелем",[("Картофель",180)]),("с макаронами",[("Макароны",85)]),
   ("с капустой",[("Капуста белокочанная",170)]),
   ("с томатами",[("Помидоры",130)]),("с луком",[("Лук",75)]),
   ("с морковью",[("Морковь",120)]),("с овощами",[("Огурцы",100),("Помидоры",85)]),
   ("с рисом и морковью",[("Рис",60),("Морковь",90)]),
   ("с картофелем и капустой",[("Картофель",110),("Капуста белокочанная",130)])]),
 "Рыба": (40,"Ужин",[
  ("Минтай запечённый",[("Минтай",200)]),
  ("Треска на пару",[("Треска",190)]),
  ("Горбуша запечённая",[("Горбуша",180)]),
  ("Хек тушёный",[("Хек",190)]),
  ("Скумбрия запечённая",[("Скумбрия",180)])],
  [("с рисом",[("Рис",75)]),("с картофелем",[("Картофель",170)]),
   ("с гречкой",[("Гречка",65)]),("с капустой",[("Капуста белокочанная",130)]),
   ("с морковью",[("Морковь",100)]),("с луком",[("Лук",70)]),
   ("с огурцами",[("Огурцы",140)]),("с томатами",[("Помидоры",140)])]),
 "Овощи и гарниры": (45,"Ужин",[
  ("Картофель тушёный",[("Картофель",250)]),
  ("Капуста тушёная",[("Капуста белокочанная",275)]),
  ("Гречневый гарнир",[("Гречка",90)]),
  ("Рисовый гарнир",[("Рис",90)]),
  ("Морковная запеканка",[("Морковь",220),("Яйца",55)])],
  [("с помидорами",[("Помидоры",100)]),("с луком",[("Лук",55)]),
   ("с капустой",[("Капуста белокочанная",115)]),("с курицей",[("Куриное филе",100)]),
   ("с сыром",[("Сыр твердый",40)]),("с яйцом",[("Яйца",65)]),
   ("с творогом",[("Творог",90)]),("с морковью",[("Морковь",100)]),
   ("с огурцами",[("Огурцы",110)])]),
 "Салаты": (35,"Ужин",[
  ("Салат огуречный",[("Огурцы",190)]),
  ("Салат томатный",[("Помидоры",190)]),
  ("Салат капустный",[("Капуста белокочанная",190)]),
  ("Салат морковный",[("Морковь",170)]),
  ("Салат картофельный",[("Картофель",150)])],
  [("с яйцом",[("Яйца",60)]),("с сыром",[("Сыр твердый",50)]),
   ("с курицей",[("Куриное филе",100)]),("с яблоком",[("Яблоки",85)]),
   ("с томатами",[("Помидоры",85)]),("с огурцом",[("Огурцы",85)]),
   ("с творогом",[("Творог",75)])]),
 "Макароны и запеканки": (30,"Обед",[
  ("Макароны запечённые",[("Макароны",100)]),
  ("Макароны отварные",[("Макароны",100)]),
  ("Запеканка картофельная",[("Картофель",220)]),
  ("Запеканка рисовая",[("Рис",105)]),
  ("Запеканка творожная",[("Творог",200),("Яйца",55)])],
  [("с сыром",[("Сыр твердый",45)]),("с курицей",[("Куриное филе",115)]),
   ("с говядиной",[("Говядина",100)]),("с помидорами",[("Помидоры",100)]),
   ("с творогом",[("Творог",80)]),("с овощами",[("Морковь",100)])]),
 "Блины, сырники и оладьи": (25,"Завтрак",[
  ("Сырники",[("Творог",180),("Мука",35),("Яйца",40)]),
  ("Оладьи",[("Мука",105),("Молоко",100),("Яйца",45)]),
  ("Блины",[("Мука",105),("Молоко",160),("Яйца",45)]),
  ("Творожные оладьи",[("Творог",150),("Мука",40),("Яйца",45)]),
  ("Яблочные оладьи",[("Яблоки",110),("Мука",75),("Яйца",45)])],
  [("с яблоком",[("Яблоки",80)]),("с бананом",[("Бананы",80)]),
   ("с творогом",[("Творог",70)]),("с сахаром",[("Сахар",15)]),
   ("с ягодами",[("Ягоды замороженные",85)])]),
 "Торты и десерты": (40,"Десерт",[
  ("Творожный торт",[("Творог",180),("Мука",60),("Яйца",50)]),
  ("Бисквитный торт",[("Мука",85),("Яйца",75),("Сахар",65)]),
  ("Яблочный пирог",[("Яблоки",160),("Мука",85),("Яйца",55)]),
  ("Творожный десерт",[("Творог",200)]),
  ("Сладкая запеканка",[("Творог",140),("Яйца",50),("Мука",50)])],
  [("с бананом",[("Бананы",80)]),("с яблоком",[("Яблоки",80)]),
   ("с шоколадом",[("Шоколад",30)]),("с ягодами",[("Ягоды замороженные",90)]),
   ("с сахаром",[("Сахар",25)]),("с грушей",[("Груши",85)]),
   ("с изюмом",[("Изюм",35)]),("с орехами",[("Орехи",35)])]),
 "Бутерброды и закуски": (25,"Завтрак",[
  ("Бутерброд с сыром",[("Хлеб ржаной",90),("Сыр твердый",50)]),
  ("Бутерброд с творогом",[("Хлеб ржаной",80),("Творог",90)]),
  ("Гренки",[("Хлеб белый",95),("Яйца",60)]),
  ("Закуска из яиц",[("Яйца",100)]),
  ("Хлеб с курицей",[("Хлеб ржаной",85),("Куриное филе",95)])],
  [("с огурцом",[("Огурцы",90)]),("с томатами",[("Помидоры",90)]),
   ("с сыром",[("Сыр твердый",35)]),("с яйцом",[("Яйца",55)]),
   ("с яблоком",[("Яблоки",75)])]),
}

PREPARATION = {
 "Каши":"Сварить крупу до готовности, добавить остальные компоненты.",
 "Супы":"Подготовить и проварить ингредиенты до полной готовности; мясо тщательно термически обработать.",
 "Из яиц":"Приготовить на сковороде или в духовке до полной готовности яйца.",
 "Из курицы и индейки":"Термически обработать птицу до полной готовности, приготовить гарнир.",
 "Из мяса":"Тушить или запекать мясо до полной готовности, приготовить гарнир.",
 "Рыба":"Приготовить рыбу до полной готовности и подать с гарниром.",
 "Овощи и гарниры":"Подготовить ингредиенты и приготовить до желаемой мягкости.",
 "Салаты":"Вымыть и подготовить продукты, смешать; сырые мясо и яйца не добавлять.",
 "Макароны и запеканки":"Отварить основу и при необходимости запечь с добавками до готовности.",
 "Блины, сырники и оладьи":"Смешать ингредиенты и обжарить или запечь до полной готовности.",
 "Торты и десерты":"Приготовить основу и начинку; при выпечке довести тесто и яйца до готовности.",
 "Бутерброды и закуски":"Подготовить компоненты и собрать закуску; мясо и яйца использовать готовыми.",
}


def recipes():
    """Stable IDs (category order, base order, variant order), 465 entries."""
    number = 0
    for category, (target, meal, bases, variants) in FAMILIES.items():
        made = 0
        for title, components in bases:
            for suffix, additions in variants:
                if made >= target:
                    break
                number += 1
                made += 1
                # Merge repeated base/topping ingredient quantities.
                merged = {}
                for name, amount in [*components, *additions]:
                    merged[name] = merged.get(name, 0) + amount
                ingredients = [{"product": name, "amount": grams, "unit": "г"}
                               for name, grams in merged.items()]
                known = all(energy(i["product"], i["amount"]) is not None for i in ingredients)
                yield {"id": f"R{number:04d}", "title": f"{title} {suffix}",
                       "category": category, "meal": meal,
                       "ingredients": ingredients, "preparation": PREPARATION[category],
                       "nutrition_status": "estimated_unverified" if known else "unknown",
                       "origin": "template_variant"}
            if made >= target:
                break
        if made != target:
            raise RuntimeError(f"Recipe family {category}: {made} != {target}")


def _better_preparation(category, ingredients):
    """Category-level guidance, not an invented chef-tested recipe."""
    if category == 'Каши':
        return ('Промыть крупу при необходимости. Добавить воду по инструкции на упаковке, '
                'варить до мягкости, помешивая. Фрукты добавить в конце, молоко прогреть, масло положить перед подачей.')
    if category == 'Супы':
        return ('На порцию ориентировочно 350–500 мл воды (не входит в расчёт цены). '
                'Овощи очистить, нарезать. Мясо полностью проварить, затем добавить '
                'крупы и овощи в порядке времени приготовления.')
    if category == 'Рыба':
        return ('Разморозить рыбу в холодильнике, удалить кости. Запечь, потушить '
                'или приготовить на пару до готовности. Гарнир приготовить отдельно.')
    if category == 'Торты и десерты':
        return ('Это предварительная комбинация, не проверенная технология. '
                'Нужны отдельные проверенные пропорции, форма, температура и время выпечки. '
                'Не готовить по одной этой инструкции.')
    if category in ('Из курицы и индейки','Из мяса'):
        return ('Нарезать продукты. Мясо или птицу тушить либо запекать до полной '
                'готовности. Овощи и гарнир приготовить отдельно или добавить '
                'на подходящем этапе. Проверить готовность перед подачей.')
    return PREPARATION[category]


def upgrade_template_preparations(db):
    """Upgrade only untouched original templates; preserve owner edits and U recipes."""
    for row in db.execute("""SELECT id,category,ingredients_json,preparation FROM zr_recipes
                            WHERE origin='template_variant'""").fetchall():
        category=row['category']
        if category in PREPARATION and row['preparation']==PREPARATION[category]:
            db.execute("UPDATE zr_recipes SET preparation=? WHERE id=? AND preparation=? AND origin='template_variant'",
                       (_better_preparation(category,json.loads(row['ingredients_json'])),row['id'],row['preparation']))
        items=json.loads(row['ingredients_json'])
        if all(energy(i['product'],i['amount']) is not None for i in items):
            db.execute("""UPDATE zr_recipes SET nutrition_status='estimated_unverified'
                          WHERE id=? AND origin='template_variant' AND nutrition_status='unknown'""",(row['id'],))

    # Fix representative tautologies without changing stable IDs or favorites.
    for old,new in [('Суп куриный с курицей','Куриный суп домашний'),
                    ('Суп рисовый с рисом','Рисовый суп домашний'),
                    ('Суп гречневый с гречкой','Гречневый суп домашний'),
                    ('Суп картофельный с картофелем','Картофельный суп домашний'),
                    ('Бутерброд с сыром с сыром','Бутерброд с двойным сыром'),
                    ('Бутерброд с творогом с творогом','Бутерброд с двойным творогом')]:
        if not db.execute('SELECT 1 FROM zr_recipes WHERE title=?',(new,)).fetchone():
            db.execute("UPDATE zr_recipes SET title=? WHERE title=? AND origin='template_variant'",(new,old))


def init(db):
    db.executescript("""
    CREATE TABLE IF NOT EXISTS zr_recipes (
      id TEXT PRIMARY KEY, title TEXT NOT NULL UNIQUE, category TEXT NOT NULL,
      meal TEXT NOT NULL, ingredients_json TEXT NOT NULL, preparation TEXT NOT NULL,
      nutrition_status TEXT NOT NULL, origin TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1);
    CREATE TABLE IF NOT EXISTS zr_favorites (
      user_id INTEGER NOT NULL, recipe_id TEXT NOT NULL REFERENCES zr_recipes(id),
      PRIMARY KEY(user_id,recipe_id));
    CREATE TABLE IF NOT EXISTS zr_recipe_yields (
      recipe_id TEXT PRIMARY KEY REFERENCES zr_recipes(id),
      raw_grams REAL, cooked_grams REAL, yield_status TEXT NOT NULL,
      standard_portion_grams REAL);
    """)
    db.executemany("""INSERT OR IGNORE INTO zr_recipes
      (id,title,category,meal,ingredients_json,preparation,nutrition_status,origin)
      VALUES (?,?,?,?,?,?,?,?)""", [
        (r["id"],r["title"],r["category"],r["meal"],
         json.dumps(r["ingredients"],ensure_ascii=False),r["preparation"],
         r["nutrition_status"],r["origin"]) for r in recipes()])
    # REV7 REV2: older U-recipes (e.g. imported by REV6) predate the yield
    # table; populate missing metadata without changing their original data.
    from recipe_yield import estimate_yield
    missing_yields=db.execute('''SELECT id, category, ingredients_json FROM zr_recipes
        WHERE id GLOB 'U[0-9]*' AND NOT EXISTS
        (SELECT 1 FROM zr_recipe_yields y WHERE y.recipe_id=zr_recipes.id)''').fetchall()
    for rid, category, ingredients_json in missing_yields:
        try:
            result=estimate_yield({'category':category,'ingredients':json.loads(ingredients_json)})
        except (TypeError,ValueError,KeyError,OverflowError):
            result={'raw_grams':None,'cooked_grams':None,'yield_status':'unverified',
                    'standard_portion_grams':250 if category=='Супы' else None}
        db.execute('''INSERT OR IGNORE INTO zr_recipe_yields
          (recipe_id,raw_grams,cooked_grams,yield_status,standard_portion_grams)
          VALUES(?,?,?,?,?)''',(rid,result['raw_grams'],result['cooked_grams'],
                         result['yield_status'],result['standard_portion_grams']))
    upgrade_template_preparations(db)
    return db.execute("SELECT COUNT(*) FROM zr_recipes WHERE active=1").fetchone()[0]


def export_json(db):
    """Human-editable archive; never export private favorites."""
    rows=[]
    for row in db.execute("SELECT id,title,category,meal,ingredients_json,preparation,nutrition_status,origin FROM zr_recipes ORDER BY id"):
        rows.append({"id":row[0],"title":row[1],"category":row[2],"meal":row[3],
                     "ingredients":json.loads(row[4]),"preparation":row[5],
                     "nutrition_status":row[6],"origin":row[7]})
    return json.dumps({"format":"zhorik_recipes_v1","total":len(rows),"recipes":rows},ensure_ascii=False,indent=2).encode("utf-8")


def _ensure_import_batches(db):
    db.execute('''CREATE TABLE IF NOT EXISTS zr_recipe_yields (
      recipe_id TEXT PRIMARY KEY REFERENCES zr_recipes(id),
      raw_grams REAL, cooked_grams REAL, yield_status TEXT NOT NULL,
      standard_portion_grams REAL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS zr_import_batches (
      batch_id TEXT PRIMARY KEY, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
      added_count INTEGER NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS zr_import_batch_items (
      batch_id TEXT NOT NULL REFERENCES zr_import_batches(batch_id),
      recipe_id TEXT NOT NULL UNIQUE REFERENCES zr_recipes(id),
      content_sha256 TEXT NOT NULL, PRIMARY KEY(batch_id, recipe_id))''')


def _recipe_digest(row):
    return hashlib.sha256(json.dumps(tuple(row), ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def rollback_import_batch(db, batch_id):
    """Remove exactly unchanged recipes inserted by this batch, atomically.

    A modified/favorited recipe is protected, not silently deleted. No U1% scans.
    """
    if not isinstance(batch_id, str) or not re.fullmatch('[a-f0-9]{32}', batch_id):
        raise ValueError('Неверный ID партии')
    db.execute('BEGIN IMMEDIATE')
    try:
        _ensure_import_batches(db)
        rows = db.execute('SELECT recipe_id, content_sha256 FROM zr_import_batch_items WHERE batch_id=?', (batch_id,)).fetchall()
        if not rows:
            raise ValueError('Партия не найдена или уже отменена')
        for rid, fingerprint in rows:
            row = db.execute('''SELECT id,title,category,meal,ingredients_json,preparation,nutrition_status,origin
                                FROM zr_recipes WHERE id=?''', (rid,)).fetchone()
            if row is None or _recipe_digest(row) != fingerprint:
                raise ValueError(f'Рецепт {rid} изменён после импорта; откат запрещён')
            if db.execute('SELECT 1 FROM zr_favorites WHERE recipe_id=?', (rid,)).fetchone():
                raise ValueError(f'Рецепт {rid} добавлен в любимые; откат запрещён')
        db.execute('DELETE FROM zr_import_batch_items WHERE batch_id=?', (batch_id,))
        for rid, _ in rows:
            db.execute('DELETE FROM zr_recipe_yields WHERE recipe_id=?', (rid,))
            db.execute('DELETE FROM zr_recipes WHERE id=?', (rid,))
        db.execute('DELETE FROM zr_import_batches WHERE batch_id=?', (batch_id,))
        db.execute('COMMIT')
        return len(rows)
    except Exception:
        db.execute('ROLLBACK')
        raise


def import_json(db, blob, *, append_only=True):
    """Safe append-only U import by default; legacy R edits require explicit opt-out."""
    if not isinstance(blob,bytes) or len(blob)>3_000_000:
        raise ValueError("Файл должен быть JSON до 3 МБ")
    try:document=json.loads(blob.decode("utf-8-sig"))
    except (UnicodeError,ValueError) as exc:raise ValueError("Нужен JSON UTF-8") from exc
    if not isinstance(document,dict) or document.get("format")!="zhorik_recipes_v1" or not isinstance(document.get("recipes"),list):
        raise ValueError("Неверный формат файла рецептов")
    from price_b_catalog import seeds
    from price_catalog import CATEGORIES
    known={name.casefold().replace("ё","е") for _,name,_,_ in seeds()}
    known.update(name.casefold().replace("ё","е") for values in CATEGORIES.values() for name in values.split(";"))
    prepared=[];seen=set()
    for r in document["recipes"]:
        if not isinstance(r,dict):raise ValueError("Неверная строка рецепта")
        rid=r.get("id")
        is_system=isinstance(rid,str) and re.fullmatch(r"R\d{4}",rid)
        if (not isinstance(rid,str) or not (re.fullmatch(r"R\d{4}",rid) or re.fullmatch(r"U\d{4,6}",rid))
                or rid in seen):
            raise ValueError("Нужен уникальный код R0001… или U0001…")
        if is_system and not db.execute("SELECT 1 FROM zr_recipes WHERE id=?",(rid,)).fetchone():
            raise ValueError("Нельзя создавать новые системные R-коды")
        seen.add(rid)
        title=r.get("title");category=r.get("category");meal=r.get("meal")
        instructions=r.get("preparation");items=r.get("ingredients")
        if not all(isinstance(v,str) and 2<=len(v.strip())<=200 for v in (title,category,meal,instructions)):
            raise ValueError("Неверное название, рубрика или описание рецепта")
        if meal not in ("Завтрак","Обед","Ужин","Десерт") or not isinstance(items,list) or not 1<=len(items)<=30:
            raise ValueError("Неверное назначение блюда или число ингредиентов")
        for item in items:
            if not isinstance(item,dict) or not isinstance(item.get("product"),str) or item["product"].casefold().replace("ё","е") not in known:
                raise ValueError("Ингредиент отсутствует в каталоге: "+str(item))
            if item.get("unit")!="г" or type(item.get("amount")) not in (int,float) or not 0<item["amount"]<=3000:
                raise ValueError("Пока принимаются только граммы сырого ингредиента, до 3000 г")
        status="estimated_unverified" if all(energy(i["product"],i["amount"]) is not None for i in items) else "unknown"
        prepared.append((rid,title.strip(),category.strip(),meal,json.dumps(items,ensure_ascii=False),instructions.strip(),status,"admin_review" if is_system else "admin_import"))
    # Importing an unchanged full export is a no-op (no unnecessary writes).
    prepared=[r for r in prepared if not (old:=db.execute(
        "SELECT title,category,meal,ingredients_json,preparation,nutrition_status FROM zr_recipes WHERE id=?",(r[0],)).fetchone())
        or tuple(old)!=r[1:7]]
    if append_only:
        # If a row already exists, only an exact byte-equivalent re-import is safe.
        # The old implementation silently overwrote an existing recipe with the same ID.
        all_ids = {r['id'] for r in document['recipes']}
        if any(re.fullmatch(r'R\d{4}', rid) for rid in all_ids):
            raise ValueError('Безопасный импорт принимает только U-коды; R-коды не изменяются')
        existing = [r[0] for r in prepared if db.execute('SELECT 1 FROM zr_recipes WHERE id=?', (r[0],)).fetchone()]
        if existing:
            raise ValueError('Конфликт ID: существующие рецепты нельзя перезаписывать: '+', '.join(existing[:6]))
    if not prepared:return (0,None) if append_only else 0
    titles={}
    for row in prepared:
        if row[1] in titles:
            raise ValueError('Название повторяется в импорте: '+row[1])
        titles[row[1]]=row[0]
        clash=db.execute('SELECT id FROM zr_recipes WHERE title=? AND id<>?',(row[1],row[0])).fetchone()
        if clash and clash['id'] not in {r[0] for r in prepared}:
            raise ValueError(f'Название занято рецептом {clash["id"]}: {row[1]}')
    db.execute("BEGIN IMMEDIATE")
    try:
        if append_only:
            _ensure_import_batches(db)
            batch_id=uuid.uuid4().hex
            db.execute('INSERT INTO zr_import_batches(batch_id,added_count) VALUES(?,?)', (batch_id,len(prepared)))
            # Concurrent importer cannot overwrite anything even if a race happened since validation.
            db.executemany('''INSERT INTO zr_recipes(id,title,category,meal,ingredients_json,preparation,nutrition_status,origin)
                              VALUES(?,?,?,?,?,?,?,?)''', prepared)
            db.executemany('''INSERT INTO zr_import_batch_items(batch_id,recipe_id,content_sha256)
                              VALUES(?,?,?)''', [(batch_id,r[0],_recipe_digest(r)) for r in prepared])
            from recipe_yield import estimate_yield
            by_id = {r['id']: r for r in document['recipes']}
            for recipe in prepared:
                result = estimate_yield(by_id[recipe[0]])
                db.execute('''INSERT INTO zr_recipe_yields
                  (recipe_id,raw_grams,cooked_grams,yield_status,standard_portion_grams)
                  VALUES(?,?,?,?,?)''', (recipe[0],result['raw_grams'],result['cooked_grams'],
                    result['yield_status'],result['standard_portion_grams']))
        else:
            db.executemany("""INSERT INTO zr_recipes(id,title,category,meal,ingredients_json,preparation,nutrition_status,origin)
           VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
           title=excluded.title,category=excluded.category,meal=excluded.meal,
           ingredients_json=excluded.ingredients_json,preparation=excluded.preparation,
           nutrition_status=excluded.nutrition_status,origin=excluded.origin""",prepared)
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise
    return (len(prepared),batch_id) if append_only else len(prepared)
