"""Small manually transcribed reference subset from user-supplied nutrition PDF.

These are reference-book figures for raw/generic products, NOT certified
retailer-specific nutrition facts. No automatically invented macro values.
Source: «Таблица калорийности продуктов», supplied PDF, pp. 1–2.
"""
# canonical title -> kcal, protein g, fat g, carbohydrates g per 100 g
REFERENCE_KBJU={
    'Гречневая крупа':(335,12.6,3.26,54.30),
    'Рис':(330,7.0,1.0,73.20),
    'Рисовая крупа':(330,7.0,1.0,73.20),
    'Манная крупа':(328,10.3,1.0,67.70),
    'Перловая крупа':(320,9.30,1.13,67.50),
    'Макароны':(337,10.4,1.13,74.90),
    'Картофель':(80,2.0,0.40,18.10),
    'Капуста белокочанная':(27,1.8,0.1,6.8),
    'Лук':(41,1.4,0.0,10.4),
    'Огурцы':(14,0.8,0.1,3.8),
    'Морковь':(34,1.3,0.1,9.3),
    'Груши':(49,0.4,0.3,10.9),
    'Изюм':(262,1.8,0.0,66.0),
    'Сахар':(379,0.0,0.0,99.8),
    'Масло подсолнечное':(899,0.0,99.9,0.0),
    'Масло сливочное':(748,0.5,82.5,0.8),
    'Апельсины':(40,0.9,0.2,10.3),
    'Треска':(76,17.0,0.4,0.0),
}


def kbju_for(name):
    return REFERENCE_KBJU.get(name)
