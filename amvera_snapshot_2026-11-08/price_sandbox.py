"""Offline-capable standalone entrypoint: python price_sandbox.py [--live] [--db path]."""
import argparse
import csv
import json
import sqlite3
from pathlib import Path
from vkusvill_prices import search, save

PRODUCTS = ['молоко','яйца','рис','гречка','куриное филе','картофель','морковь','яблоки','овсяные хлопья','творог']

def collect(queries, finder=search):
    results, errors = [], {}
    for q in queries:
        try:
            results.extend(finder(q))
        except Exception as exc:
            errors[q] = str(exc)
    return results, errors

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--live',action='store_true',help='Make live MCP requests')
    ap.add_argument('--db',help='Optional isolated SQLite path for price candidates')
    ap.add_argument('--out',default='vkusvill_sandbox',help='Output file prefix')
    args=ap.parse_args()
    if not args.live:
        print('Dry run. Add --live to query VkusVill. No database changed.')
        return
    rows,errors=collect(PRODUCTS)
    prefix=Path(args.out)
    prefix.with_suffix('.json').write_text(json.dumps({'products':rows,'errors':errors},ensure_ascii=False,indent=2),encoding='utf-8')
    with prefix.with_suffix('.csv').open('w',encoding='utf-8-sig',newline='') as f:
        columns=['query','name','product_id','price_rub','weight','unit','url','observed_at','source','availability_confirmed']
        writer=csv.DictWriter(f,fieldnames=columns);writer.writeheader();writer.writerows(rows)
    if args.db:
        with sqlite3.connect(args.db) as db:
            save(db,rows)
    print(f'Products: {len(rows)}; errors: {len(errors)}; files: {prefix}.json, {prefix}.csv')
    for q,e in errors.items():print(f'  {q}: {e}')

if __name__=='__main__':main()
