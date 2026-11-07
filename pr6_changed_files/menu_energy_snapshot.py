"""PPLX-QA-0003 proposal R1, 2026-11-08. NOT AN INSTALLATION PATCH.
Author: Тестировщик Жорика Perplexity.
Baseline: 655a4c5fb48b79be9a15228ddeda89fe9cc7d629.

Build once AFTER final portions, save inside result_json, read without a
catalog afterwards. No DB/network writes, new product catalog, BJU claims,
allergen decisions or changes to the validated 0001/0002 fixes.
Default lookup is the EXISTING transition menu_energy; later replace that
adapter with the approved canonical nutrition API, not another dictionary.
Integration in generation/UI/exports and independent QA are still required.
"""
from __future__ import annotations
import hashlib
import json
import math

DAYS=('Понедельник','Вторник','Среда','Четверг','Пятница','Суббота','Воскресенье')
MEALS=('Завтрак','Обед','Ужин')
class SnapshotMismatch(ValueError): pass

def _hash(value):
    raw=json.dumps(value,ensure_ascii=False,sort_keys=True,
                   separators=(',',':'),allow_nan=False).encode('utf-8')
    return hashlib.sha256(raw).hexdigest()

def _number(value):
    return type(value) in (int,float) and math.isfinite(value)

def _aggregate(entries):
    complete=bool(entries) and all(e['complete'] for e in entries)
    known=round(sum(e['known_kcal'] for e in entries),1)
    return {'known_kcal':known,'kcal':known if complete else None,'complete':complete}

def build_energy_snapshot(menu,catalog,*,people=1,lookup=None,
                          expected_days=DAYS,expected_meals=MEALS,
                          resolver_version='transition-menu-energy-v1'):
    if type(people) is not int or not 1<=people<=100:
        raise ValueError('Positive integer people required')
    days,meals=tuple(expected_days),tuple(expected_meals)
    if (not days or len(set(days))!=len(days) or not meals or len(set(meals))!=len(meals)):
        raise ValueError('Unique expected day and meal names required')
    week=menu.get('menu') if isinstance(menu,dict) else None
    if not isinstance(week,dict) or set(week)!=set(days):
        raise ValueError('Unexpected week shape; nothing silently omitted')
    if not isinstance(resolver_version,str) or not resolver_version.strip():
        raise ValueError('Resolver version required')
    if lookup is None:
        from price07_menu_products import menu_energy
        lookup=menu_energy
    fingerprint=_hash(menu)
    output={};missing=[]
    for day in days:
        daily=week[day]
        if not isinstance(daily,dict) or set(daily)!=set(meals):
            raise ValueError('Unexpected meal shape')
        meal_rows={}
        for meal in meals:
            dishes=daily[meal]
            if not isinstance(dishes,list):raise ValueError('Dishes must be a list')
            dish_rows=[]
            if not dishes:missing.append([day,meal,'empty_meal'])
            for di,dish in enumerate(dishes):
                if not isinstance(dish,dict):raise ValueError('Malformed dish')
                ingredients=dish.get('ingredients')
                if not isinstance(ingredients,list):raise ValueError('Ingredients must be a list')
                entries=[]
                if not ingredients:missing.append([day,meal,di,'empty_dish'])
                for ii,item in enumerate(ingredients):
                    if not isinstance(item,dict):raise ValueError('Malformed ingredient')
                    name,amount,unit=(item.get(k) for k in ('product','amount','unit'))
                    if (not isinstance(name,str) or not name.strip() or
                        not _number(amount) or amount<=0 or not isinstance(unit,str)):
                        raise ValueError('Invalid ingredient identity/quantity')
                    grams=amount*(1000 if unit=='кг' else 1) if unit in ('г','кг') else None
                    if grams is not None and not math.isfinite(grams):
                        raise ValueError('Ingredient mass overflow')
                    value=lookup(name,grams,catalog) if grams is not None else None
                    if value is not None and (not _number(value) or value<0):
                        raise ValueError('Invalid energy returned by resolver')
                    if value is None:missing.append([day,meal,di,ii,name,unit])
                    kcal=round(float(value),1) if value is not None else None
                    entries.append({'product':name,'amount':amount,'unit':unit,
                                    'kcal':kcal,'known_kcal':kcal if kcal is not None else 0.0,
                                    'complete':kcal is not None})
                dish_rows.append({'name':dish.get('name'),**_aggregate(entries),'ingredients':entries})
            meal_rows[meal]={**_aggregate(dish_rows),'dishes':dish_rows}
        output[day]={**_aggregate(list(meal_rows.values())),'meals':meal_rows}
    snapshot={'schema_version':1,'menu_fingerprint':fingerprint,'people':people,
              'basis':'per_person_ingredient_amounts','quality':'estimated_energy_not_full_kbju',
              'resolver_version':resolver_version,'days':output,'missing':missing,
              'week':_aggregate(list(output.values()))}
    total=snapshot['week']['kcal']
    snapshot['week']['all_people_kcal']=round(total*people,1) if total is not None else None
    snapshot['snapshot_hash']=_hash(snapshot)
    return snapshot

def check_snapshot(menu,snapshot):
    if not isinstance(snapshot,dict) or snapshot.get('schema_version')!=1:
        raise SnapshotMismatch('Unsupported snapshot schema')
    content={k:v for k,v in snapshot.items() if k!='snapshot_hash'}
    try:
        content_hash=_hash(content);menu_hash=_hash(menu)
    except (TypeError,ValueError,OverflowError) as exc:
        raise SnapshotMismatch('Snapshot/menu cannot be serialized safely') from exc
    if snapshot.get('snapshot_hash')!=content_hash:
        raise SnapshotMismatch('Snapshot content changed')
    if snapshot.get('menu_fingerprint')!=menu_hash:
        raise SnapshotMismatch('Menu changed after energy calculation')
    return snapshot

def saved_energy_snapshot(menu,stats):
    # Legacy history: explicitly return None, never fetch current catalog.
    snapshot=stats.get('energy_snapshot') if isinstance(stats,dict) else None
    return None if snapshot is None else check_snapshot(menu,snapshot)

def balance_from_snapshot(snapshot,daily_target,*,assess=None):
    # Caller must first check_snapshot against the saved menu.
    if assess is None:
        from recipe01_nutrition import assess_meal_balance
        assess=assess_meal_balance
    return {day:assess({meal:row['kcal'] for meal,row in d['meals'].items()
                       if row['complete']},daily_target)
            for day,d in snapshot['days'].items()}
