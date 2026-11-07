"""Context-aware, conservative Russian product phrase parser (Build 15).

Borrowed architectural ideas from Budilnik/Nezabudka's parse_layer:
normalize speech first, resolve explicit commands before generic text, reject
unsupported residues, keep the parsing core independent of UI/DB/Telegram.
"""
from dataclasses import dataclass
import re

@dataclass(frozen=True)
class ParsedPhrase:
    items: tuple[str, ...]
    uncertain: tuple[str, ...]
    intent: str

# Ordered: negations must precede affirmative variants.
INTRO = {
    'dislike': r'(?:мне\s+не\s+нравятся|мне\s+не\s+нравится|я\s+не\s+люблю|мои\s+нелюбимые\s+продукты|нелюбимые\s+продукты|добавь(?:те)?\s+в\s+нелюбимые)',
    'allergy': r'(?:у\s+меня\s+аллергия\s+на|мои\s+аллергены|аллергены|добавь(?:те)?\s+в\s+аллергены)',
    'like': r'(?:мне\s+нравятся|мне\s+нравится|я\s+люблю|мои\s+любимые\s+продукты|любимые\s+продукты|добавь(?:те)?\s+в\s+любимые)',
    'shopping': r'(?:не\s+забыть\s+купить|надо\s+купить|нужно\s+купить|хочу\s+купить|купить|купи(?:те)?|возьми(?:те)?|добавь(?:те)?\s+в\s+список\s+покупок)',
}
FILLER = re.compile(r'^(?:(?:ну|значит|пожалуйста|ещё|еще|добавь(?!\s+в\b)|добавьте(?!\s+в\b))\s+)+', re.I)
NUMBERED = re.compile(r'^\s*(?:[•–-]|\d+[.)])\s*')

# Longest match first; compound names are not split into single-word products.
COMPOUNDS = (
    'масло сливочное', 'масло растительное', 'сыр творожный', 'сыр плавленый',
    'вода минеральная', 'молоко сгущённое', 'молоко сгущенное',
    'лук репчатый', 'зелёный лук', 'зеленый лук', 'сок яблочный',
    'хлеб ржаной', 'куриное филе', 'филе куриное', 'сметана 20%',
    'сыр твердый', 'сыр твёрдый', 'молоко безлактозное', 'чай зелёный',
    'чай зеленый', 'кофе молотый', 'кофе растворимый',
)
PRODUCTS = '''шашлык шашлыка дрова дров мясо мяса молоко молока уголь угля
кетчуп майонез майонеза лук лука огурцы огурцов картошка картошки
яйца яиц яйцо сыр сыра вода воды воду хлеб хлеба соль сахар чай кофе
курица курицы гречка гречки спички бананы банан бананов яблоки яблоко яблок
помидоры помидор масло колбаса рыба рыбу рис сок салфетки пакеты
печенье пиво пива морковь орехи орехов мёд мед шоколад шоколада
клубника клубнику клубники чеснок чеснока арахис арахиса кунжут кунжута
сливки сливок творог творога мука муку макароны макарон кефир кефира
йогурт йогурта картофель картофеля батон сосиски сосисок груши груш
лимон лимоны апельсин апельсины сметана сметаны филе индейка индейки
говядина говядины свинина свинины фарш фарша вода воды уголь угля'''
KNOWN = set(PRODUCTS.split())
# All unit expressions are kept separate from product tokens.
UNIT = r'(?:килограмм(?:а|ов)?|кг|полкило|полкилограмма|грамм(?:а|ов)?|г|литр(?:а|ов)?|л|миллилитр(?:а|ов)?|мл|штук(?:и)?|шт\.?|пакет(?:а|ов)?|пачк(?:а|и|у|ек)|бутылк(?:а|и|у|ок)|бут\.?|упаковк(?:а|и|у|ок)|банк(?:а|и|у|ок))'
NUM = r'(?:\d+(?:[.,]\d+)?|один|одна|одно|одну|два|две|три|четыре|пять|шесть|семь|восемь|девять|десять|полтора|полторы|пару|пара)'
QUANTITY = re.compile(r'(?<!\w)(?:(?:'+NUM+r')\s+)?'+UNIT+r'(?!\w)', re.I)
NUMBERS = {'один':'1','одна':'1','одно':'1','одну':'1','два':'2','две':'2','три':'3','четыре':'4','пять':'5','шесть':'6','семь':'7','восемь':'8','девять':'9','десять':'10','пара':'2','пару':'2','полтора':'1.5','полторы':'1.5'}
UNITS = {'килограмм':'кг','килограмма':'кг','килограммов':'кг','кг':'кг','полкило':'кг','полкилограмма':'кг',
         'грамм':'г','грамма':'г','граммов':'г','г':'г','литр':'л','литра':'л','литров':'л','л':'л',
         'миллилитр':'мл','миллилитра':'мл','миллилитров':'мл','мл':'мл',
         'штук':'шт.','штуки':'шт.','шт':'шт.','шт.':'шт.',
         'пакет':'пак.','пакета':'пак.','пакетов':'пак.',
         'пачка':'пач.','пачки':'пач.','пачку':'пач.','пачек':'пач.',
         'бутылка':'бут.','бутылки':'бут.','бутылку':'бут.','бутылок':'бут.','бут':'бут.','бут.':'бут.',
         'упаковка':'упак.','упаковки':'упак.','упаковку':'упак.','упаковок':'упак.',
         'банка':'бан.','банки':'бан.','банку':'бан.','банок':'бан.'}


def _quantity(match):
    value = match.group().strip().lower()
    if value in ('полкило','полкилограмма'): return '0.5 кг'
    parts = value.split()
    if len(parts) == 1:
        number, unit = '1', parts[0]
    else:
        number, unit = parts[0], parts[1]
    number = NUMBERS.get(number,number).replace(',','.')
    if number in ('пара','пару'):number='2'
    try:
        amount=float(number)
        if not 0 < amount <= 100000:return None
    except ValueError:return None
    normalized=UNITS.get(unit.rstrip('.') if unit not in UNITS else unit)
    if not normalized:return None
    return f'{number} {normalized}'


def parse_phrase(text: str, source: str) -> ParsedPhrase:
    if source not in INTRO:raise ValueError('Unsupported parser context')
    raw = re.sub(r'[ \t]+', ' ', (text or '').strip().lower())
    raw = NUMBERED.sub('',raw)
    raw = FILLER.sub('',raw)
    # Never interpret an explicit command for another screen as current-screen data.
    for context, pattern in INTRO.items():
        match = re.match(r'^(?:'+pattern+r')\b[\s,:;—-]*',raw)
        if match:
            if context != source:return ParsedPhrase((),(raw,),source)
            raw=raw[match.end():].strip();break
    raw=raw.strip(' .,:;')
    if not raw:return ParsedPhrase((),(),source)
    # Longest-match lexical scan; delimiters and conjunctions are boundaries.
    compounds=sorted(COMPOUNDS,key=lambda x:len(x.split()),reverse=True)
    result=[];uncertain=[]

    # Tokenize units before products to keep quantities attached to the previous product.
    segments=re.split(r'[,;\n]+',raw)
    for segment in segments:
        segment=NUMBERED.sub('',segment.strip())
        if not segment:continue
        # Split by explicit conjunction, preserving compounds.
        for part in re.split(r'\s+и\s+',segment):
            part=part.strip(' .!?')
            if not part:continue
            # Replace recognized quantities with placeholders before lexical product matching.
            quantities=[]
            def mask(m):
                quantities.append(_quantity(m))
                return ' qplaceholder'+str(len(quantities)-1)+' '
            part=QUANTITY.sub(mask,part)
            words=part.split()
            i=0;name=None;qty=None
            while i<len(words):
                word=words[i].strip(' .!?«»')
                if re.fullmatch(r'qplaceholder\d+',word):
                    q=quantities[int(word[12:])]
                    if q is None:uncertain.append(word)
                    elif name is None:uncertain.append(q)
                    else:qty=q
                    i+=1;continue
                matched=None;length=0
                for phrase in compounds:
                    parts=phrase.split()
                    if [w.strip(' .!?«»') for w in words[i:i+len(parts)]]==parts:
                        matched=phrase;length=len(parts);break
                if matched is None and word in KNOWN:matched=word;length=1
                if matched is None:
                    uncertain.append(word);i+=1;continue
                if name is not None:result.append(name+(' '+qty if source=='shopping' and qty else ''))
                name=matched;qty=None;i+=length
            if name is not None:result.append(name+(' '+qty if source=='shopping' and qty else ''))
    return ParsedPhrase(tuple(dict.fromkeys(result)),tuple(dict.fromkeys(uncertain)),source)
