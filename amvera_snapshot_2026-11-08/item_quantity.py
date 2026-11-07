"""Conservative quantity extraction. Unrecognized text remains unchanged."""
import re
UNITS={'шт':'шт.','штук':'шт.','штуки':'шт.','пак':'пак.','пакет':'пак.','пакета':'пак.','пакетов':'пак.','кг':'кг','килограмм':'кг','килограмма':'кг','г':'г','грамм':'г','л':'л','литр':'л','литра':'л','мл':'мл','бутылка':'бут.','бутылки':'бут.','бутылок':'бут.','бут':'бут.'}
PAT=re.compile(r'(?<!\w)(\d+(?:[.,]\d+)?)\s*('+ '|'.join(sorted(map(re.escape,UNITS),key=len,reverse=True))+r')\.?\b',re.I)
def parse_quantity(text):
    m=PAT.search(text)
    if not m:return text,1.0,'шт.'
    qty=float(m.group(1).replace(',','.'))
    if qty<0 or qty>100000:return text,1.0,'шт.'
    unit=UNITS[m.group(2).lower()]
    name=(text[:m.start()]+text[m.end():]).strip(' ,-')
    return (name or text),qty,unit
