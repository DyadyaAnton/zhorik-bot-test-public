"""Store-neutral, warning-only matching for public grocery catalog candidates.

No SKU approval, filtering, or price writes. Compatible statuses: review_required,
likely_unrelated. Never use review_required as proof of a match.
"""
import html
import re
from decimal import Decimal, InvalidOperation

STOP = {'вес', 'набор', 'шт', 'штук', 'по', 'для', 'из', 'с', 'со', 'и', 'на', 'в',
        'г', 'гр', 'кг', 'мл', 'л', 'упаковка', 'уп', 'бутылок', 'бутылки',
        'пакетиков', 'пакетика', 'пакетик', 'фасовка', 'кусочки', 'кусок'}
FORMS = {
    'груши':'груша','груш':'груша','грушевый':'груша',
    'орехи':'орех','орехов':'орех','орехами':'орех',
    'яблоки':'яблоко','яблок':'яблоко',
    'помидоры':'помидор','помидоров':'помидор','томаты':'помидор','томат':'помидор',
    'огурцы':'огурец','огурцов':'огурец','яйца':'яйцо','яиц':'яйцо',
    'конфеты':'конфета','конфет':'конфета',
    'куриная':'куриный','куриное':'куриный','куриные':'куриный','куриного':'куриный',
    'курицы':'курица','курицу':'курица','грецкие':'грецкий','грецких':'грецкий',
    'пекинская':'пекинский','пекинской':'пекинский',
    'творожная':'творожный','творожной':'творожный',
    'сливочное':'сливочный','сливочная':'сливочный',
    'холодного':'холодный','горячего':'горячий',
    'копчения':'копчение','копченая':'копчение','копченый':'копчение',
    'йогурты':'йогурт','йогуртов':'йогурт',
    'пельменей':'пельмени','пельменями':'пельмени',
    'крупы':'крупа','круп':'крупа',
    'манная':'манный','манной':'манный',
    'гречневая':'гречневый','гречневой':'гречневый','гречневого':'гречневый',
    'масса':'масса','массой':'масса','изюмом':'изюм','изюма':'изюм',
    'семги':'семга','сёмга':'семга','сёмги':'семга',
    'печени':'печень','печенью':'печень',
    'гречка':'гречневый','гречки':'гречневый','гречку':'гречневый',
    'гречневая':'гречневый',
    'рикотты':'рикотта','сулугуни':'сулугуни',
    'газированная':'газированный','газированную':'газированный',
    'негазированная':'негазированный','негазированную':'негазированный',
}
STEMS = ('молок','кефир','ряженк','простокваш','ацидофилин','биойогурт',
         'йогурт','творог','сметан','сливк','скумбри','форел','груш',
         'яблок','огур','капуст','печен','конфет','мармелад','пастил',
         'куриц','говядин','свинин','пельмен','вареник')
UNITS = {'г':('кг',Decimal('.001')),'гр':('кг',Decimal('.001')),
         'кг':('кг',Decimal(1)),'мл':('л',Decimal('.001')),'л':('л',Decimal(1))}
U = r'(кг|гр|г|мл|л)\b'
QTY = re.compile(r'(?<![\d.,])(\d+(?:[.,]\d+)?)\s*'+U,re.I)
MULT = re.compile(r'(\d+)\s*(?:шт\.?|штук)\s*(?:[xх×*]|по)\s*(\d+(?:[.,]\d+)?)\s*'+U,re.I)
REVERSE = re.compile(r'(\d+(?:[.,]\d+)?)\s*'+U+r'\s*[xх×*]\s*(\d+)(?:\s*шт\.?)?',re.I)
COUNT = re.compile(r'\b(\d+)\s*(?:шт\.?|штук|яиц|яйца)\b',re.I)
FAT = re.compile(r'(?<![\d.,])(\d+(?:[.,]\d+)?)\s*%',re.I)

# Only clear, product-specific distinctions. No blanket exclusion of descriptions.
PRODUCT_GROUPS = (
    {'скумбрия','нерка','кета','горбуша','осетр','дорадо','мойва','семга','форель','сельдь'},
    {'кефир','ряженка','простокваша','ацидофилин','биойогурт','йогурт','биолакт','бифилайф'},
    {'рикотта','маскарпоне','сулугуни','тильзитер','моцарелла','пармезан'},
    {'кешью','миндаль','фундук','фисташка','грецкий'},
)
# Primary product vs prepared food / seeds / products containing it as an ingredient.
PREPARED = {'салат','пицца','паштет','пирожки','пирожок','котлеты','котлета',
            'сырок','голубцы','пельмени','торт','лимонад','сок','смузи',
            'семена','мороженое','запеканка','суп','наггетсы','финик'}
RAW_FOOD = {'груша','капуста','печень','орех','сыр','молоко','масло','скумбрия',
            'куриный','гречневый','крупа','сгущенка','сулугуни','рикотта'}


def _clean(s):
    return ' '.join(html.unescape(str(s or '')).replace('\xa0',' ').split())


def _words(value):
    raw = re.findall(r'[а-яёa-z]+',_clean(value).lower().replace('ё','е'))
    return [FORMS.get(w,w) for w in raw if len(w)>2 and w not in STOP]


def _same(a,b):
    return a == b or any(a.startswith(st) and b.startswith(st) for st in STEMS)


def _number(s):
    try: return Decimal(s.replace(',','.'))
    except (InvalidOperation,AttributeError): return None


def _pack(text):
    """Per-item amount and explicit count (not aggregate amount)."""
    s = _clean(text).lower()
    m = MULT.search(s)
    if m:
        return (UNITS[m.group(3)][0],_number(m.group(2))*UNITS[m.group(3)][1],int(m.group(1)))
    m = REVERSE.search(s)
    if m:
        return (UNITS[m.group(2)][0],_number(m.group(1))*UNITS[m.group(2)][1],int(m.group(3)))
    m = QTY.search(s)
    c = COUNT.search(s)
    if m:
        return (UNITS[m.group(2)][0],_number(m.group(1))*UNITS[m.group(2)][1],int(c.group(1)) if c else None)
    return (None,None,int(c.group(1)) if c else None)


def _primary_conflict(q,n):
    qset,nset = set(q),set(n)
    for group in PRODUCT_GROUPS:
        qa,na = qset & group,nset & group
        if qa and na and qa.isdisjoint(na):
            return True
    # A ready-made food is not interchangeable with its named raw ingredient.
    if qset & RAW_FOOD and not (qset & PREPARED) and nset & PREPARED:
        return True
    # A named subtype is not interchangeable with an unrelated subtype.
    if 'сыр' in qset and 'сыр' in nset:
        variants = PRODUCT_GROUPS[2]
        if (qset & variants) and not (nset & (qset & variants)) and (nset & variants):
            return True
    if 'гхи' in qset and 'масло' in qset and 'сливочный' in nset and 'гхи' not in nset:
        return True
    if 'сливочный' in qset and 'масло' in qset and 'гхи' in nset and 'сливочный' not in nset:
        return True
    return False


def assess_match(query,name):
    """Return only a warning status; never confirms product equivalence."""
    q,n = _words(query),_words(name)
    if not q: return {'match_status':'review_required'}
    if _primary_conflict(q,n): return {'match_status':'likely_unrelated'}
    if not any(_same(a,b) for a in q for b in n):
        return {'match_status':'likely_unrelated'}
    qset,nset = set(q),set(n)
    if ('холодный' in qset and 'горячий' in nset) or ('горячий' in qset and 'холодный' in nset):
        return {'match_status':'likely_unrelated'}
    if ('газированный' in qset and 'негазированный' in nset) or ('негазированный' in qset and 'газированный' in nset):
        return {'match_status':'likely_unrelated'}
    # Natural phrases 'с газом' / 'без газа'. Do not conflate a negated gas token.
    qtxt,ntxt = _clean(query).lower(),_clean(name).lower()
    def gas(t):
        if re.search(r'\bбез\s+газа\b|\bнегаз',t): return 'no'
        if re.search(r'\bс\s+газом\b|\bгазирован',t): return 'yes'
        return None
    if gas(qtxt) and gas(ntxt) and gas(qtxt)!=gas(ntxt):
        return {'match_status':'likely_unrelated'}
    qdim,qa,qcount = _pack(query)
    ndim,na,ncount = _pack(name)
    if qdim and ndim and (qdim != ndim or qa != na):
        return {'match_status':'likely_unrelated'}
    if qcount is not None and ncount is not None and qcount != ncount:
        return {'match_status':'likely_unrelated'}
    if qcount is not None and qcount > 1 and ncount is None and ndim is not None:
        return {'match_status':'likely_unrelated'}
    qfat,nfat = FAT.findall(query),FAT.findall(name)
    if len(qfat)==len(nfat)==1 and _number(qfat[0]) != _number(nfat[0]):
        return {'match_status':'likely_unrelated'}
    return {'match_status':'review_required'}
