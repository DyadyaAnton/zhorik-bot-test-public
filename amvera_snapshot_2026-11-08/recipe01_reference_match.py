"""Conservative read-only lookup of nutrition-table rows for editor review.

Never an authoritative nutrition source: rows may describe variants, dry food,
cooked food, or a different denominator. No numeric values are promoted.
"""
from __future__ import annotations

import re
from collections import defaultdict

# Human-curated search aliases only. A hit means "candidate for review".
ALIASES={
 'бананы':('бананы','банан'), 'говядина':('говядина 1 категории','говядина 2 категории','говядина, вырезка'),
 'горбуша':('горбуша',), 'гречка':('крупа гречневая ядрица',),
 'груши':('груша','груши'), 'индейка филе':('индейка, грудка',),
 'капуста белокочанная':('капуста белокочанная',),
 'картофель':('картофель',), 'куриное филе':('куриная грудка','курица, грудка'),
 'лук':('лук репчатый',), 'макароны':('макаронные изделия в/с','макаронные изделия в/с,вареные'),
 'масло подсолнечное':('масло подсолнечное',),
 'масло сливочное':('масло сливочное',), 'минтай':('минтай',),
 'молоко':('молоко 1,5% жирности','молоко 2,5%','молоко 3,2%'), 'морковь':('морковь',),
 'мука':('мука пшеничная в/с',), 'овсяные хлопья':('овсяные хлопья',),
 'огурцы':('огурцы грунтовые','огурцы тепличные'),
 'помидоры':('помидоры грунтовые','помидоры тепличные'),
 'рис':('рис белый шлифованный',), 'сахар':('сахар-песок',),
 'свинина':('свинина мясная','свинина, вырезка'), 'скумбрия':('скумбрия',),
 'сыр твердый':('сыр "российский"','сыр "голландский" брусковый','сыр "гауда"'),
 'творог':('творог 5%','творог 9%'),
 'треска':('треска атлантическая','треска тихоокеанская'),
 'хек':('хек',), 'хлеб ржаной':('хлеб ржаной',),
 'яблоки':('яблоки',), 'ягоды замороженные':('ягоды',),
 'яйца':('яйцо куриное',),
}


def normalize(value):
    return ' '.join(value.casefold().replace('ё','е').split())


def find_reference_rows(text, product_names):
    """Return candidates with page and raw row, never data verified flags."""
    pages=text.split('\f')
    results=defaultdict(list)
    for product in product_names:
        aliases=ALIASES.get(normalize(product),())
        for page_number,page in enumerate(pages,1):
            for line_number,line in enumerate(page.splitlines(),1):
                fields=re.split(r'\s{2,}',line.strip())
                if len(fields)<4:
                    continue
                label=normalize(fields[0])
                if any(label==normalize(alias) for alias in aliases):
                    results[product].append({
                        'source_page':page_number,'source_line':line_number,
                        'source_label':fields[0],
                        'raw_values':tuple(fields[1:]),
                        'measurement_basis':'needs_editorial_review',
                        'food_state':'unknown',
                        'review_status':'unverified',
                    })
    return {p:tuple(results.get(p,())) for p in product_names}
