"""Conservative product segmentation. Ambiguities remain editable before save."""
import re
from price_catalog import CATEGORIES
KNOWN={w.lower() for v in CATEGORIES.values() for w in v.split(';')}
KNOWN.update(('мясо','молоко','яйца','картошка','хлеб','уголь','спички','мангал','вода','сыр','кофе','чай','соль','сахар','батон','помидоры','огурцы','яблоки','бананы','колбаса','сосиски','лук','морковь','масло','салфетки','пакеты','сок','печенье','пиво','рыба'))
# Common inflections and multiword grocery names; avoid guessing unknown boundaries.
UNITS=r'(?:килограмм(?:а|ов)?|килограмма|кг|грамм(?:а|ов)?|г|литр(?:а|ов)?|л|бутыл(?:ка|ки|ок|ку)|пач(?:ка|ки|ек|ку)|штук(?:и)?|шт|бан(?:ка|ки|ок|ку)|упаков(?:ка|ки|ок|ку)|буханк(?:а|и|у)|пакет(?:а|ов)?|полкило)'
AMOUNTS=r'(?:\d+(?:[.,]\d+)?|один|одна|одно|две|два|три|четыре|пять|шесть|семь|восемь|девять|десять|пару|пара|полтора|полторы)'
END=re.compile(r'(?:\b(?:'+AMOUNTS+r')\s*'+UNITS+r'\b|\bполкило\b)',re.I)

def normalize_title(s):
    s=re.sub(r'^\s*(?:создай\s+|создать\s+|новый\s+|сделай\s+)?(?:список\s+)(?:покупок\s+)?', '',s.strip(),flags=re.I)
    return s.strip(' .,:;«»')

# Build 07: conservative natural-speech segmentation. Keep this independent of UI.
PRODUCT_FORMS={
 'шашлык','шашлыка','дрова','дров','мясо','мяса','молоко','молока','уголь','угля',
 'кетчуп','майонез','майонеза','лук','лука','огурцы','огурцов','картошка','картошки',
 'яйца','яиц','сыр','сыра','вода','воды','хлеб','хлеба','соль','сахар','чай','кофе',
 'курица','курицы','гречка','гречки','спички','бананы','яблоки','помидоры','масло',
 'колбаса','рыба','рис','сок','салфетки','пакеты','печенье','пиво','морковь'
}
PRODUCT_FORMS.update(KNOWN)
FILLER=re.compile(r'^\s*(?:значит|ну|ещё|еще|пожалуйста|нужно|купи|возьми|добавь)\s+',re.I)

def parse_items(raw):
    raw=raw.strip()
    if not raw:return []
    raw=FILLER.sub('',raw)
    pieces=[x.strip() for x in re.split(r'[,;\n]+',raw) if x.strip()]
    if len(pieces)>1:
        out=[]
        for p in pieces:out.extend(parse_items(p))
        return out
    words=raw.split();starts=[0]
    for i in range(1,len(words)):
        word=words[i].lower().strip('.,;!?')
        if word not in PRODUCT_FORMS:continue
        # Do not break known multi-word products or product after modifier.
        prev=words[i-1].lower().strip('.,;!?')
        if prev in ('куриное','сливочное','твердый','белый','ржаной','газированная','питьевая'):continue
        prefix=' '.join(words[starts[-1]:i]).strip()
        if not prefix:continue
        # If prior phrase ends with a quantity/unit or an independent product,
        # the new product starts a new entry. Ambiguous cases remain editable.
        if END.search(prefix) or any(x in PRODUCT_FORMS for x in prefix.lower().split()) or prev in ('и','ещё','еще'):
            starts.append(i)
    starts.append(len(words));out=[]
    for a,b in zip(starts,starts[1:]):
        s=' '.join(words[a:b]).strip(' ,.;')
        s=re.sub(r'^(?:и|ещё|еще|значит)\s+','',s,flags=re.I)
        s=re.sub(r'\s+(?:и|ещё|еще)$','',s,flags=re.I).strip()
        if s:out.append(s)
    return out
