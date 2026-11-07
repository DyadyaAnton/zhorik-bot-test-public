"""Conservative canonicalization: equivalent word order, not arbitrary fuzzy matches."""
import re

ALIASES = {
    'бедро куриное': 'Куриное бедро',
    'куриное бедро': 'Куриное бедро',
    'фарш говяжий': 'Говяжий фарш',
    'говяжий фарш': 'Говяжий фарш',
    'филе куриное': 'Куриное филе',
    'куриное филе': 'Куриное филе',
    'филе курицы': 'Куриное филе',
    'куриная грудка': 'Куриная грудка',
    'грудка куриная': 'Куриная грудка',
    'голень куриная': 'Куриная голень',
    'куриная голень': 'Куриная голень',
    'печень куриная': 'Куриная печень',
    'куриная печень': 'Куриная печень',
    'крылья куриные': 'Куриные крылья',
    'куриные крылья': 'Куриные крылья',
    'сердечки куриные': 'Куриные сердечки',
    'куриные сердечки': 'Куриные сердечки',
}

def canonical(name: str) -> str:
    cleaned = re.sub(r'\s+', ' ', name.strip()).strip(' .,;')
    if not cleaned:
        return ''
    key = cleaned.casefold().replace('ё', 'е')
    if key in ALIASES:
        return ALIASES[key]
    # Only reorder known, strictly equivalent adjective/noun combinations.
    tokens = key.split()
    if len(tokens) == 2:
        reversed_key = ' '.join(reversed(tokens))
        if reversed_key in ALIASES:
            return ALIASES[reversed_key]
    return cleaned[0].upper() + cleaned[1:]
