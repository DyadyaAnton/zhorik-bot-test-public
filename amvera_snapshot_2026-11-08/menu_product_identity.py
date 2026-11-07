"""Read-only ingredient identity for menu diversity; not an allergen classifier.

Specific product codes live in zb_canonical_products. Legacy generic names
cannot safely be assigned a fat-content-specific CP code: use family keys.
"""
import re

_ALIASES = {
    'яблоко': 'яблоки', 'яблоки': 'яблоки', 'яблок': 'яблоки',
    'банан': 'бананы', 'бананы': 'бананы',
    'груша': 'груши', 'груши': 'груши',
    'ягоды': 'ягоды замороженные', 'ягоды замороженные': 'ягоды замороженные',
    'творог': 'творог', 'творог 5%': 'творог', 'творог 9%': 'творог',
    'творог 0%': 'творог', 'творог 2%': 'творог',
    'йогурт': 'йогурт', 'йогурт натуральный': 'йогурт',
    'йогурт греческий': 'йогурт',
    'сыр твердый': 'сыр твердый', 'сыр твёрдый': 'сыр твердый',
    'молоко': 'молоко',
}

FRUITS = frozenset(('яблоки', 'бананы', 'груши', 'апельсины', 'мандарины',
                    'виноград', 'киви', 'хурма', 'ягоды замороженные'))
DAIRY = frozenset(('творог', 'йогурт', 'молоко', 'сыр твердый'))


def load_codes():
    """Take one read-only codebook snapshot for a generation (no DB mutation)."""
    from database import connect_db
    with connect_db() as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='zb_canonical_products'").fetchone():
            return {}
        return {str(name).casefold().replace('ё','е'):code for code,name in
                db.execute('SELECT code,name FROM zb_canonical_products WHERE active=1')}


def _normalized(name):
    return re.sub(r'\s+', ' ', str(name).casefold().replace('ё', 'е')).strip()


def code_for(name, codebook):
    """Exact catalog CP code only; never guess a generic product's variant."""
    return codebook.get(_normalized(name)) if codebook else None


def key(name, codebook=None):
    """Weekly repetition identity, possibly broader than a specific CP code.

    For example, generic Творог and Творог 5% share a frequency key,
    while code_for(Творог 5%) still returns its exact registered CP code.
    """
    clean = _normalized(name)
    canonical=_ALIASES.get(clean, clean)
    if clean.startswith('творог ') or canonical=='творог':
        return 'family:творог'
    if clean.startswith('йогурт ') or canonical=='йогурт':
        return 'family:йогурт'
    if codebook:
        # A singular apple and a plural catalog apple must have one key.
        return codebook.get(canonical,codebook.get(clean,canonical))
    return canonical


def repetition_penalty(name, previous):
    """Soft penalty: safe diets remain feasible if alternatives are scarce."""
    kind = key(name)
    if kind == 'family:творог':
        return max(0, previous - 1) * 0.90
    if kind == 'family:йогурт':
        return max(0, previous - 3) * 0.22
    if kind == 'яблоки':
        return max(0, previous - 1) * 1.15
    if kind == 'творог':
        return max(0, previous - 1) * 0.90
    if kind in FRUITS:
        return max(0, previous - 2) * 0.48
    if kind in DAIRY:
        return max(0, previous - 3) * 0.22
    return max(0, previous - 4) * 0.09
