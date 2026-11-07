"""Conservative, dependency-free routing of colloquial Russian requests.

Speech-to-text is upstream. This module NEVER changes data; uncertain commands
remain previews for the user to correct. Menu intents are informational only.
"""

from __future__ import annotations

from dataclasses import dataclass
import re

from phrase_parser import INTRO, parse_phrase


@dataclass(frozen=True)
class VoiceIntent:
    intent: str
    confidence: str
    target_list_number: int | None = None
    days: int | None = None
    people: int | None = None
    budget_rub: int | None = None
    shopping_text: str | None = None
    needs_clarification: bool = False


_ORDINALS = {"первому": 1, "первый": 1, "второму": 2, "второй": 2,
             "третьему": 3, "третий": 3, "четвертому": 4, "четвёртому": 4,
             "пятому": 5, "пятый": 5}
_WORDS = {"одного": 1, "один": 1, "одну": 1, "двоих": 2, "двух": 2,
          "два": 2, "троих": 3, "трёх": 3, "трех": 3, "три": 3,
          "семь": 7, "семерых": 7}
_THOUSANDS = {"пять": 5, "шесть": 6, "семь": 7, "восемь": 8,
              "девять": 9, "десять": 10}


def _number(word: str) -> int | None:
    return int(word) if word.isdecimal() else _WORDS.get(word)


def parse_voice_intent(text: str) -> VoiceIntent:
    """Classify rather than hallucinate an action; no LLM/network dependency."""
    raw = (text or "").strip()
    s = re.sub(r"\s+", " ", raw.casefold())
    if not s:
        return VoiceIntent("CLARIFY", "low", needs_clarification=True)

    number = None
    ordinal = re.search(r"\b(?:к|ко|в|во)\s+(\d+|первому|первый|второму|второй|третьему|третий|четвертому|четвёртому|пятому|пятый)\s+спис(?:ок|ку)\b", s)
    if ordinal:
        token = ordinal.group(1)
        number = int(token) if token.isdecimal() else _ORDINALS.get(token)

    # Meta corrections must not be interpreted as shopping items.
    if re.search(r"\b(?:оговорка|ничего добавлять не нужно|я сказал,? что|это замечание|технический момент)\b", s):
        return VoiceIntent("CLARIFY", "high", needs_clarification=True)

    list_reference = bool(re.search(r"\b(?:спис(?:ок|ка|ку|ки)|покупк\w*)\b", s))
    add = bool(re.search(r"\b(?:добавь|добавить|допиши|запиши)\b", s))
    if add and list_reference:
        # Long narrated instructions often contain side comments and several
        # different actions. Never offer a one-tap list target for them.
        is_simple = (len(s) <= 110 and not re.search(
            r"\b(?:там что-то|вспомню|не знаю|потом|надо предупредить|по номеру)\b", s))
        # Only a strictly shaped command may skip manual target selection.
        # Never pass the words 'к третьему списку добавь' as a shopping item.
        payload = None
        if ordinal and is_simple:
            # Match on the unmodified source: slicing raw by a length measured
            # in whitespace-collapsed s silently chops product names.
            direct = re.fullmatch(
                r"\s*(?:вот\s+)?(?:к|ко|в|во)\s+"
                r"(?:\d+|первому|первый|второму|второй|третьему|третий|"
                r"четвертому|четвёртому|пятому|пятый)\s+спис(?:ок|ку)"
                r"[\s,:;.-]+(?:пожалуйста\s+)?(?:добавь|добавить|допиши|запиши)\s+(.+?)\s*",
                raw, flags=re.I | re.S)
            if direct:
                payload = direct.group(1).strip()
        return VoiceIntent("LIST_ADD", "medium", target_list_number=number,
                           shopping_text=payload,
                           needs_clarification=number is None or not is_simple or payload is None)

    menu = bool(re.search(r"\b(?:меню|завтрак\w*|обед\w*|ужин\w*|приготовить|покулинарить)\b", s))
    if menu:
        if re.search(r"\b(?:прошл\w*|стар\w*|повтори|покажи,? что было|на той неделе|гратен)\b", s):
            intent = "MENU_HISTORY"
        elif re.search(r"\b(?:холодильник\w*|из того,? что|у меня есть|минут за 15)\b", s):
            intent = "MENU_PANTRY"
        elif re.search(r"\b(?:неделю|недельное|на семь дней)\b", s):
            intent = "MENU_WEEK"
        else:
            intent = "MENU_SHORT"
        days = None
        if intent == "MENU_WEEK":
            days = 7
        else:
            match = re.search(r"\bна\s+(\d+|пару|два|три)\s+дн", s)
            if match:
                days = 2 if match.group(1) == "пару" else _number(match.group(1))
        people = None
        match = re.search(r"\bна\s+(\d+|одного|двоих|двух|троих|трёх|трех)\b", s)
        if match:
            people = _number(match.group(1))
        match = re.search(r"\b(?:нас\s+трое|двое взрослых и ребён\w*)\b", s)
        if match:
            people = 3
        if re.search(r"\bдвух\s+человек\s+с\s+ребёнк\w*", s):
            people = 3
        budget = None
        # Approximate budgets are only hints, never silently committed.
        match = re.search(r"\b(?:тысяч\s+на\s+|на\s+)(\d{1,2})\s+тысяч|\b(\d{4,6})\s*(?:руб|₽)", s)
        if match:
            budget = int(match.group(1)) * 1000 if match.group(1) else int(match.group(2))
        if budget is None:
            match = re.search(r"\bтысяч\s+на\s+(пять|шесть|семь|восемь|девять|десять)\b", s)
            if match:
                budget = _THOUSANDS[match.group(1)] * 1000
        return VoiceIntent(intent, "medium", days=days, people=people,
                           budget_rub=budget, needs_clarification=True)

    # Reuse the already deployed shopping command grammar. Quantity parsing
    # remains entirely in lists.split_items / phrase_parser, not duplicated.
    shopping = re.match(r"^(?:слушай[, ]+)?(?:" + INTRO['shopping'] + r")\b[\s,:;—-]*", raw, flags=re.I)
    if shopping:
        cleaned = raw[shopping.end():]
        cleaned = re.sub(r"^на\s+(?:завтра|сегодня)\s+", "", cleaned, flags=re.I)
        parsed = parse_phrase(cleaned, 'shopping')
        # This only helps a manual preview. No auto-save or guessing list names.
        return VoiceIntent("LIST_CREATE", "low" if parsed.uncertain else "medium",
                           shopping_text=cleaned, needs_clarification=bool(parsed.uncertain))

    if re.search(r"\b(?:корм для кошки|кошачий туалет|купить хлеб|пакетов\s+\d+)\b", s):
        return VoiceIntent("LIST_CREATE", "low", needs_clarification=True)
    return VoiceIntent("OTHER", "low", needs_clarification=True)


_LABELS = {
    "LIST_CREATE": "новый список покупок", "LIST_ADD": "дополнить список",
    "LIST_EDIT": "исправить список", "MENU_WEEK": "меню на неделю",
    "MENU_SHORT": "меню на несколько дней или вечер",
    "MENU_PANTRY": "ужин из имеющихся продуктов",
    "MENU_HISTORY": "найти старое меню", "RECIPE_SEARCH": "найти рецепт",
    "PRICE_QUERY": "оценить цены", "CLARIFY": "уточнение или исправление",
    "OTHER": "нестандартный запрос",
}


def describe_intent(result: VoiceIntent) -> str:
    title = _LABELS.get(result.intent, result.intent)
    if result.target_list_number is not None:
        title += f" №{result.target_list_number}"
    return title
