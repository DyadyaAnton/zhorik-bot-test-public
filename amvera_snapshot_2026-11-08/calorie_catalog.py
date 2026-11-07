"""Conservative raw-food energy references (kcal per 100 g); absent = unknown.
Values are illustrative reference estimates, not verified nutritional labels.
"""
from functools import lru_cache

KCAL_100G = {
 'овсяные хлопья':366,'рис':344,'гречка':343,'макароны':350,
 'молоко':60,'кефир':53,'творог':121,'сыр твердый':350,'масло сливочное':748,
 'куриное филе':113,'бедро куриное':185,'курица целая':190,
 'яйца':143,'картофель':77,'морковь':41,'лук':40,'капуста белокочанная':25,
 'огурцы':15,'помидоры':18,'яблоки':52,'бананы':89,
 'хлеб белый':265,'хлеб ржаной':210,'масло подсолнечное':899,
 'сахар':387,'мука':364,'говядина':187,'свинина':242,
 # Raw, generic food reference estimates. Species, fat content and preparation
 # materially affect the real values; never treat these as label-verified.
 'минтай':72,'треска':76,'горбуша':140,'хек':86,'скумбрия':191,
 'индейка филе':110,'ягоды замороженные':45,'шоколад':540,
 'орехи':610,'груши':57,'изюм':299,
}

@lru_cache(maxsize=4096)
def energy(name, amount_g):
    value=KCAL_100G.get(name.casefold())
    return None if value is None else round(value*amount_g/100,1)
