"""Read-only restrictions and conservative screening of editorial LLM output.

Free-form Qwen prose is NEVER executable advice and never modifies a menu.
The text screen only rejects known conflicts; a passing text is not certified
as suitable for allergy sufferers (implicit ingredients may be missing).
"""
import re

from database import connect_db
from menu_generator import ALLERGEN_NAME_MARKERS, _allergen_ingredients, MenuInvalid

# Common prepared dishes whose names imply ingredients not explicitly named.
# Keep this deliberately small: the recipe catalog, not free text, is the
# authoritative source for automated replacements.
IMPLIED_MARKERS = {
    'молоко': ('сырники', 'сырник', 'молочный коктейль', 'сливочный соус'),
    'яйца': ('омлет', 'яичница', 'яичный', 'яичная'),
    'орехи': ('ореховая паста',),
    'пшеница': ('булочка',),
    'глютен': ('булочка',),
    'лук': ('луковый', 'луковая', 'луковое', 'луковый порошок',
            'лук-порей', 'луковица', 'порей'),
}

# Known Russian inflections and adjective forms. Every expression starts at a
# word boundary: never match an arbitrary substring inside an unrelated word.
# This screen is advisory only; recipe ingredients remain authoritative.
INFLECTION_STEMS = {
    'молоко': ('молочн', 'молок', 'творожн', 'творог', 'сливочн',
               'сметан', 'кефирн', 'йогуртов', 'сырник', 'сырн'),
    'лук': ('луков', 'луковиц'),
    'лук-порей': ('луков', 'луковиц'),
    'луковица': ('луков', 'луковиц'),
    'луковый порошок': ('луков', 'луковиц'),
    'орехи': ('орех', 'миндальн', 'миндал', 'фисташков', 'фундучн',
              'кешью', 'пекан', 'макадам'),
    'яйца': ('яичн', 'яйц', 'яичниц'),
    'рыба': ('рыбн',),
    'пшеница': ('пшеничн',),
    'глютен': ('пшеничн', 'глютенов'),
}


def load_allergies(owner_id):
    """One short read-only snapshot, outside every LLM network operation."""
    with connect_db() as db:
        return [str(row[0]).strip().casefold() for row in db.execute(
            "SELECT value FROM preferences WHERE telegram_id=? "
            "AND preference_type='allergy' AND active=1", (owner_id,))]


def _normalized(value):
    return re.sub(r'\s+', ' ', str(value).casefold().replace('ё', 'е')).strip()


def _phrase_in(text, phrase):
    phrase = _normalized(phrase)
    return bool(re.search(r'(?<!\w)' + re.escape(phrase) + r'(?!\w)', text))


def validated_allergies(allergies):
    """No silent loss of an unsupported strict restriction."""
    result = []
    for item in allergies:
        name = _normalized(item)
        _allergen_ingredients(name)  # raises MenuInvalid if unsupported
        if name and name not in result:
            result.append(name)
    return result


def rejected_editorial_text(text, allergies):
    """Known conflict only; absence of a match does not prove safety."""
    normalized = _normalized(text)
    for name in validated_allergies(allergies):
        ingredients = _allergen_ingredients(name)
        markers = list(ALLERGEN_NAME_MARKERS.get(name, ()))
        markers.extend(ingredients)
        markers.extend(IMPLIED_MARKERS.get(name, ()))
        if any(_phrase_in(normalized, marker) for marker in markers):
            return True
        if any(re.search(r'(?<!\w)' + re.escape(stem) + r'\w*', normalized)
               for stem in INFLECTION_STEMS.get(name, ())):
            return True
    return False


def screen_review(review, allergies):
    """Never display untrusted free-form editorial text with strict allergens.

    Lexical matches cannot prove a free-form suggestion safe: e.g. `сыра`,
    `сливками`, `йогуртом` all evade simple dictionary-based filters.
    """
    strict = validated_allergies(allergies)
    if strict:
        hidden = int(bool(review.get('summary'))) + len(review.get('issues') or ()) + len(review.get('suggestions') or ())
        return {'summary': 'ИИ-анализ выполнен. При пищевых ограничениях свободные советы ИИ скрыты; меню не изменено.',
                'issues': [], 'suggestions': [], 'editorial_hidden': True}, hidden
    safe = dict(review)
    if rejected_editorial_text(review.get('summary',''), allergies):
        safe['summary'] = 'Редакторский обзор содержит предложение, отклонённое локальной проверкой.'
    accepted = []
    rejected = 0
    if safe['summary'] != review.get('summary',''):
        rejected += 1
    issue_lines = []
    for issue in review.get('issues', ()):
        if rejected_editorial_text(issue, allergies):
            rejected += 1
        else:
            issue_lines.append(issue)
    safe['issues'] = issue_lines
    for suggestion in review.get('suggestions', ()):
        if rejected_editorial_text(suggestion, allergies):
            rejected += 1
        else:
            accepted.append(suggestion)
    safe['suggestions'] = accepted
    return safe, rejected
