"""Short, ingredient-aware instructions for porridge recipe families.

No ingredient is invented: water is stated only as a cooking medium and not
as a quantified purchased food. Existing instructions for other meals remain
untouched until an individual culinary review.
"""

GRAINS = {
    'овсяные хлопья': 'овсяные хлопья',
    'рис': 'рис',
    'гречка': 'гречку',
    'пшено': 'пшено',
    'кукурузная крупа': 'кукурузную крупу',
    'манка': 'манную крупу',
    'манная крупа': 'манную крупу',
}


def instructions(category, ingredients, original):
    """Return safe editorial directions only for recognized porridge bases."""
    if category != 'Каши' or not isinstance(ingredients, list):
        return original
    amounts = {}
    for item in ingredients:
        if not isinstance(item, dict) or not isinstance(item.get('product'), str):
            return original
        amounts[item['product'].strip().casefold().replace('ё', 'е')] = item
    grain = next(((name, label) for name, label in GRAINS.items() if name in amounts), None)
    if grain is None:
        return original
    name, label = grain
    other = set(amounts) - {name}
    allowed = {'молоко', 'бананы', 'яблоки', 'творог', 'масло сливочное', 'сахар',
               'ягоды замороженные', 'груши', 'изюм', 'орехи', 'мед', 'мёд'}
    if other - allowed:
        return original
    if any(item.get('unit') != 'г' for item in ingredients):
        return original
    if name in ('рис', 'гречка', 'пшено', 'кукурузная крупа'):
        base = f'Промыть {label}, залить водой и варить до мягкости, ориентируясь на инструкцию на упаковке.'
    elif name in ('манка', 'манная крупа'):
        base = 'Довести жидкость до лёгкого кипения, всыпать манную крупу тонкой струйкой, непрерывно помешивая, и варить до готовности.'
    else:
        base = 'Всыпать овсяные хлопья в горячую жидкость и варить, помешивая, согласно инструкции на упаковке.'
    steps = [base]
    if 'молоко' in other:
        steps.append('Молоко использовать как часть жидкости для варки; при необходимости долить воду. Следить, чтобы каша не пригорела.')
    if 'яблоки' in other:
        steps.append('Яблоко вымыть, удалить сердцевину, нарезать и добавить к готовой каше.')
    if 'бананы' in other:
        steps.append('Банан очистить, нарезать и добавить непосредственно перед подачей.')
    if 'груши' in other:
        steps.append('Грушу вымыть, удалить сердцевину и добавить кусочками перед подачей.')
    if 'ягоды замороженные' in other:
        steps.append('Замороженные ягоды прогреть до горячего состояния и добавить перед подачей.')
    if 'изюм' in other:
        steps.append('Изюм тщательно промыть и добавить к каше ближе к концу приготовления.')
    if 'творог' in other:
        steps.append('Снять кашу с огня, вмешать творог и подавать сразу.')
    if 'масло сливочное' in other:
        steps.append('Сливочное масло добавить в готовую горячую кашу.')
    if 'сахар' in other:
        steps.append('Сахар размешать в готовой горячей каше.')
    if 'орехи' in other:
        steps.append('Орехи измельчить и посыпать ими готовую кашу.')
    if 'мед' in other or 'мёд' in other:
        steps.append('Мёд добавить в готовую слегка остывшую кашу.')
    return ' '.join(steps)
