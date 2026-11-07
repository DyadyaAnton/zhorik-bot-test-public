"""Presentation-only corrections for legacy template recipe titles.

Do not rewrite zr_recipes.title: it is a stable unique key and owners may have
edited individual R-codes. Display aliases leave imports, favorites and IDs
untouched.
"""


def display_title(raw):
    text=str(raw or "").strip()
    if text=='Бутерброд с творогом с томатами':
        return 'Ржаной бутерброд с творогом и помидорами'
    if text.startswith('Бутерброд ') and text.endswith(' с томатами'):
        return text[:-len(' с томатами')]+' и помидорами'
    if text=='Суп куриный с курицей':
        return 'Куриный суп'
    return text
