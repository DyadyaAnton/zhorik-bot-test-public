"""Единые правила допуска бюджета меню.

Решение владельца от 03.11.2026: закупка по упаковкам не может превышать бюджет
больше чем на 5%. Правило прежней сборки «до +20%» отменено.
Все модули используют ЭТИ функции, чтобы допуск менялся в одном месте.
"""
import hashlib
import math

BUDGET_TOLERANCE = 0.05
BUDGET_FACTOR = 1.0 + BUDGET_TOLERANCE
_EPS = 0.005  # копейка на округление


def budget_limit(budget):
    """Наибольшая допустимая стоимость закупки при данном бюджете."""
    return float(budget) * BUDGET_FACTOR + _EPS


def is_over_budget(cost, budget):
    """True, если закупка выше допуска. Недопустимые числа (NaN, бесконечность, отрицательная сумма,
    нулевой или отрицательный бюджет, нечисло) считаются превышением: проверка не должна пропускать мусор."""
    try:
        cost = float(cost)
        budget = float(budget)
    except (TypeError, ValueError):
        return True
    if not (math.isfinite(cost) and math.isfinite(budget)) or budget <= 0 or cost < 0:
        return True
    return cost > budget_limit(budget)


def suggested_budget(needed, step=100):
    """Бюджет, при котором закупка на сумму `needed` укладывается без превышения (округление вверх до step)."""
    return int(math.ceil(float(needed) / step) * step)


def params_token(people, budget, cal, diet, rev=0):
    """Короткая отметка настроек меню (люди, бюджет, калории, тип питания и редакция настроек).

    Кнопки «пути выхода» несут отметку настроек, при которых они были предложены. Если настройки с тех пор
    менялись (даже с возвратом к прежним значениям; редакция `rev` растёт при любом изменении, включая
    предпочтения и аллергены), старая кнопка отклоняется и не перезаписывает более новые значения."""
    raw = f"{int(people)}|{int(round(float(budget or 0)))}|{int(float(cal or 0))}|{diet}|{int(rev or 0)}"
    return hashlib.sha1(raw.encode('utf-8')).hexdigest()[:6]
