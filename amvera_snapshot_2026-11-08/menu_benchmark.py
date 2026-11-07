import os, sys, asyncio, collections, statistics as st, json
"""Benchmark of the REAL menu pipeline (autonomous_menu.build -> menu_ui._finalize_generated).
Qwen, Telegram and the network are NOT used.

USAGE (always on a COPY of the database: the script inserts a test user into user_settings):
    cp /data/zhorik.sqlite3 /tmp/zh_copy/zhorik.sqlite3
    ZH_ROOT=<папка с кодом> ZH_DATA=/tmp/zh_copy python3 tools/menu_benchmark.py
"""
ROOT=os.environ['ZH_ROOT']; D=os.environ['ZH_DATA']
os.environ.update(DATA_DIR=D,DB_PATH=D+'/zhorik.sqlite3',TELEGRAM_BOT_TOKEN='x',SANDBOX_OWNER_ID='1',LOG_LEVEL='CRITICAL')
sys.path[:0]=[ROOT,os.path.dirname(os.path.abspath(__file__))]
import aiogram_stub; aiogram_stub.install()
from database import connect_db
UID=900001
with connect_db() as c:
    c.execute('INSERT OR IGNORE INTO users(telegram_id) VALUES(?)',(UID,))
    c.execute('INSERT INTO user_settings(telegram_id,calorie_target) VALUES(?,2000) ON CONFLICT(telegram_id) DO UPDATE SET calorie_target=2000',(UID,))
import autonomous_menu as A, menu_ui as U
from menu_generator import DAYS,MEALS,MenuInvalid
VEG={'морковь','капуста белокочанная','огурцы','помидоры','перец сладкий','свекла','кабачки','лук','брокколи','тыква','баклажаны'}
def metrics(menu,stats,people,budget):
    names=[];per=[];ings=collections.Counter();vegdays=0;weak=0;veg_meals=0
    for day in DAYS:
        seen=False
        for meal in MEALS:
            ds=menu['menu'][day][meal]; per.append(len(ds))
            names+= [d['name'] for d in ds]
            pr={i['product'].casefold() for d in ds for i in d['ingredients']}
            for i in pr: ings[i]+=1
            if pr&VEG: seen=True; veg_meals+=1
            if meal=='Ужин' and len(pr)<=2: weak+=1
        vegdays+=0 if seen else 1
    prot=[p for p in ings if p in {'куриное филе','говядина','свинина','индейка филе','минтай','треска','горбуша','хек','скумбрия','яйца'}]
    return dict(titles=len(set(names)),per=sum(per)/len(per),ings=len(ings),vegdays=vegdays,veg_meals=veg_meals,weak=weak,
                ratio=stats['purchase_estimate']/budget,kcal=stats['known_kcal']/people/7,prot=len(prot),multi=stats.get('multi_dish'),
                top=ings.most_common(4))
async def one(people,budget,cal,v,diet='regular'):
    menu,stats,ctx=await A.build(UID,people,budget,cal,variation=v,diet_mode=diet)
    products,prefs,prices,_=ctx
    menu,stats=U._finalize_generated(menu,stats,products,prefs,prices,people,budget,cal,diet)
    return menu,stats
def run(label,people,budget,cal,n=12,diet='regular'):
    ok=[];fail=collections.Counter()
    for v in range(1,n+1):
        try:
            menu,stats=asyncio.run(one(people,budget,cal,v,diet)); ok.append((menu,metrics(menu,stats,people,budget)))
        except Exception as e:
            fail[type(e).__name__+': '+str(e)[:90]]+=1
    print(f'=== {label}: people={people} budget={budget} kcal={cal} diet={diet}  OK {len(ok)}/{n}')
    for k,v in fail.items(): print('   FAIL x%d  %s'%(v,k))
    if ok:
        M=[m for _,m in ok]
        f=lambda k: st.mean(x[k] for x in M)
        print('   dishes/meal %.2f | distinct titles %.1f/21 | distinct ingredients %.1f | veg-free days %.1f/7 | meals with veg %.1f/21 | weak dinners(<=2 ingr) %.1f/7 | proteins types %.1f'%(f('per'),f('titles'),f('ings'),f('vegdays'),f('veg_meals'),f('weak'),f('prot')))
        print('   purchase/budget mean %.2f (min %.2f max %.2f) | kcal/person/day mean %.0f | multi_dish True in %d/%d'%(f('ratio'),min(x['ratio'] for x in M),max(x['ratio'] for x in M),f('kcal'),sum(1 for x in M if x['multi']),len(M)))
        print('   most used ingredients (week 1):',M[0]['top'])
        # same-parameters weeks: how different are they from each other?
        sig=[set(d['name'] for day in DAYS for meal in MEALS for d in o[0]['menu'][day][meal]) for o in ok]
        if len(sig)>1:
            j=[len(a&b)/len(a|b) for i,a in enumerate(sig) for b in sig[i+1:]]
            print('   Jaccard overlap of dish titles between different weeks: mean %.2f'%st.mean(j))
    return ok
if __name__=='__main__':
    # без аргументов: набор сценариев; с аргументами: menu_benchmark.py ЛЮДИ БЮДЖЕТ [ККАЛ] [ДИЕТА] [ЧИСЛО_ЗАПУСКОВ]
    if len(sys.argv)>=3:
        run('сценарий',int(sys.argv[1]),float(sys.argv[2]),float(sys.argv[3]) if len(sys.argv)>3 else 2000,
            n=int(sys.argv[5]) if len(sys.argv)>5 else 12,diet=sys.argv[4] if len(sys.argv)>4 else 'regular')
    else:
        for label,p,b in (('1 чел.',1,6000),('1 чел.',1,6500),('2 чел.',2,6000),('2 чел.',2,7500),('2 чел.',2,9000),('1 чел.',1,9000),('2 чел.',2,10000)):
            run(label,p,b,2000)
