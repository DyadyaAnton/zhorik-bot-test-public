"""Q1.1 offline regression tests. No network or production SQLite writes."""
import asyncio
import inspect
import os
import sqlite3
import tempfile
import json
import threading
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(__file__))
import aiogram_stub
aiogram_stub.install()
from aiogram.types import InlineKeyboardButton

from menu_editorial_policy import (
    rejected_editorial_text, screen_review, validated_allergies)
from menu_generator import MenuInvalid, blocked
from price_b_catalog import seeds
from menu_ui import cancel_generation
import menu_ui
import card_registry
import llm_lab
import llm_service
import llm_usage
import llm_usage_async
import menu_qwen_gate


class AllergenReviewTests(unittest.TestCase):
    def test_all_known_dairy_catalog_items_are_blocked(self):
        dairy=[name for _,name,category,_ in seeds() if category=='Молоко и молочные']
        self.assertGreaterEqual(len(dairy),25)
        for name in dairy:
            with self.subTest(name=name):
                self.assertTrue(blocked(name,{'allergy':['молоко'],'dislike':[]}))

    def test_fish_catalog_and_nonfish_seafood_boundaries(self):
        nonfish={'Кальмар','Креветки','Мидии','Икра кабачковая'}
        for _,name,category,_ in seeds():
            if category!='Рыба и морепродукты':continue
            with self.subTest(name=name):
                self.assertEqual(blocked(name,{'allergy':['рыба'],'dislike':[]}),name not in nonfish)

    def test_allergen_false_positive_controls(self):
        for name,allergen in [('Масло оливковое','молоко'),('Икра кабачковая','рыба'),
                              ('Груши','орехи'),('Луковичные растения','лук'),
                              ('Хлебцы рисовые','молоко')]:
            with self.subTest(name=name,allergen=allergen):
                self.assertFalse(blocked(name,{'allergy':[allergen],'dislike':[]}))

    def test_wheat_gluten_egg_catalog_completeness(self):
        from menu_generator import _KNOWN_ALLERGEN_PRODUCTS
        expected={
            'пшеница':('Кускус','Полба','Булгур','Манная крупа','Лаваш','Тортилья',
                        'Макароны обычные','Мука пшеничная высший сорт'),
            'глютен':('Кускус','Полба','Булгур','Манная крупа','Лаваш','Тортилья',
                       'Перловая крупа','Ячневая крупа','Овсяная крупа','Геркулес',
                       'Мюсли без сахара','Гранола','Хлеб ржаной'),
            'яйца':('Лапша яичная','Яйца перепелиные','Яйца куриные C0'),
            'орехи':('Грецкие орехи','Миндаль'),
            'арахис':('Арахис','Арахисовая паста'),
        }
        known={name.casefold().replace('ё','е') for _,name,_,_ in seeds()}
        for allergen,names in expected.items():
            for name in names:
                with self.subTest(allergen=allergen,name=name):
                    self.assertIn(name.casefold().replace('ё','е'),known)
                    self.assertIn(name.casefold().replace('ё','е'),_KNOWN_ALLERGEN_PRODUCTS[allergen])
                    self.assertTrue(blocked(name,{'allergy':[allergen],'dislike':[]}))

    def test_gluten_and_wheat_false_positive_boundaries(self):
        for product,allergen in [('Гречневые хлопья','глютен'),
                                 ('Гречневая крупа','глютен'),('Киноа','глютен'),
                                 ('Мука рисовая','пшеница'),('Хлеб ржаной','пшеница'),
                                 ('Мука кукурузная','глютен'),('Рис бурый','глютен'),
                                 ('Фунчоза','пшеница'),('Фунчоза','глютен')]:
            with self.subTest(product=product,allergen=allergen):
                self.assertFalse(blocked(product,{'allergy':[allergen],'dislike':[]}))

    def test_milk_suggestions_are_rejected(self):
        review={'summary':'Разнообразие', 'issues':[], 'suggestions':[
            'Добавьте творог', 'Приготовьте сырники', 'Добавьте грушу']}
        screened, rejected=screen_review(review,['молоко'])
        self.assertEqual(screened['suggestions'],[])
        self.assertEqual(rejected,4)
        self.assertEqual(len(review['suggestions']),3)  # never mutate source

    def test_onion_variants(self):
        for word in ('лук', 'лук-порей', 'луковица', 'луковый порошок',
                     'лук репчатый'):
            with self.subTest(word=word):
                self.assertTrue(rejected_editorial_text('Добавить '+word,['лук']))
                self.assertTrue(blocked(word,{'allergy':['лук'],'dislike':[]}))
        for variant in ('лук-порей','луковица','луковый порошок'):
            with self.subTest(profile=variant):
                self.assertTrue(blocked('Лук репчатый',{
                    'allergy':[variant], 'dislike':[]}))

    def test_nuts_and_false_positive(self):
        self.assertTrue(rejected_editorial_text('Миндаль к завтраку',['орехи']))
        self.assertFalse(rejected_editorial_text('Яблоко и груша',['орехи']))
        self.assertFalse(rejected_editorial_text('Луковичные растения',[]))

    def test_unknown_allergen_fails_closed(self):
        with self.assertRaises(MenuInvalid):
            validated_allergies(['неизвестный пищевой аллерген'])

    def test_text_is_not_automatically_applied(self):
        review={'summary':'Суп', 'issues':['Повтор'], 'suggestions':['Рис']}
        safe, count=screen_review(review,['молоко'])
        self.assertEqual((safe['issues'],count),([],3))

    def test_all_editorial_fields_are_screened(self):
        review={'summary':'Вместо каши используйте сырники',
                'issues':['Добавьте лук-порей', 'Повторяется рис'],
                'suggestions':['Больше яблок']}
        safe,rejected=screen_review(review,['молоко','лук'])
        self.assertEqual(rejected,4)
        self.assertNotIn('сырники',safe['summary'])
        self.assertEqual(safe['issues'],[])

    def test_free_form_inflections_are_never_exposed(self):
        review={'summary':'Подать со сливками', 'issues':['Добавить сыра'],
                'suggestions':['Приправить йогуртом', 'Положить яблоко']}
        safe, hidden=screen_review(review,['молоко'])
        self.assertEqual((safe['issues'],safe['suggestions'],hidden),([],[],4))
        self.assertNotIn('сливками',safe['summary'])
        self.assertTrue(safe['editorial_hidden'])

    def test_no_restrictions_preserve_editorial_text(self):
        review={'summary':'Груша', 'issues':['Повторы'], 'suggestions':['Зелень']}
        safe, hidden=screen_review(review,[])
        self.assertEqual((safe,hidden),(review,0))

    def test_russian_inflections_do_not_pass(self):
        cases=[('Добавьте молочную кашу','молоко'),
               ('Подавайте с творожным кремом','молоко'),
               ('Добавьте луковую заправку','лук'),
               ('Посыпьте грецкими орехами','орехи'),
               ('Добавьте миндальную стружку','орехи')]
        for text,allergen in cases:
            with self.subTest(text=text):
                self.assertTrue(rejected_editorial_text(text,[allergen]))


class GenerationGuardTests(unittest.TestCase):
    def setUp(self):
        self.old_runs=menu_ui._runs.copy()
        self.old_tasks=menu_ui._tasks.copy()
        menu_ui._runs.clear()
        menu_ui._tasks.clear()

    def tearDown(self):
        menu_ui._runs.clear()
        menu_ui._runs.update(self.old_runs)
        menu_ui._tasks.clear()
        menu_ui._tasks.update(self.old_tasks)

    def test_stale_cancel_cannot_cancel_newer(self):
        class Task:
            cancelled=False
            def done(self): return False
            def cancel(self): self.cancelled=True
        task=Task()
        menu_ui._runs[7]={'id':'new', 'task':task, 'phase':'building', 'cancelled':False}
        self.assertEqual(cancel_generation(7,'old'),'stale')
        self.assertFalse(task.cancelled)
        self.assertEqual(cancel_generation(7,'new'),'cancelled')
        self.assertTrue(task.cancelled)

    def test_cancel_refused_at_commit_boundary(self):
        menu_ui._runs[7]={'id':'active','task':None,'phase':'saving','cancelled':False}
        self.assertEqual(cancel_generation(7,'active'),'saving')
        self.assertFalse(menu_ui._runs[7]['cancelled'])

    def test_changed_settings_rejected_in_write_transaction(self):
        db=sqlite3.connect(':memory:')
        db.row_factory=sqlite3.Row
        db.executescript('CREATE TABLE menu_runtime_settings(key TEXT PRIMARY KEY,value TEXT);'
                         'CREATE TABLE menus(id INTEGER PRIMARY KEY,owner_id INTEGER,'
                         'people_count INTEGER,budget REAL,calorie_target REAL,result_json TEXT);'
                         "INSERT INTO menu_runtime_settings VALUES('u7:settings_rev','9');")
        menu={'menu':{}}
        stats={'ingredients':{}}
        with patch.object(menu_ui,'connect_db',return_value=db), \
             patch.object(menu_ui,'_parameters_key',return_value='key'), \
             patch.object(menu_ui.menu_library,'remember'):
            with self.assertRaisesRegex(MenuInvalid,'Настройки питания изменились'):
                menu_ui._save_generated(7,1,6000,1700,'regular',{},menu,stats,8)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM menus').fetchone()[0],0)
        db.close()


class EditorialAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_qwen_gate_unknown_allergen_does_not_charge(self):
        with patch.object(menu_qwen_gate,'reserve_cancellation_safe',new=AsyncMock()) as reserve:
            with self.assertRaises(MenuInvalid):
                await menu_qwen_gate.review({'menu':{}},{'allergy':['неизвестное']})
            reserve.assert_not_awaited()

    async def test_qwen_gate_cancel_during_reserve_closes_reservation(self):
        entered=threading.Event()
        release=threading.Event()
        charged=threading.Event()
        def reserve():
            entered.set()
            if not release.wait(3):
                raise RuntimeError('test timeout')
            return 'gate-reservation'
        def settle(rid,tokens):
            self.assertEqual((rid,tokens),('gate-reservation',None))
            charged.set()
            return 2500,True
        with patch.object(llm_usage_async,'reserve',side_effect=reserve), \
             patch.object(llm_usage_async,'settle',side_effect=settle):
            task=asyncio.create_task(menu_qwen_gate.review({'menu':{}}))
            self.assertTrue(await asyncio.to_thread(entered.wait,2))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            release.set()
            self.assertTrue(await asyncio.to_thread(charged.wait,2))

    async def test_settlement_retries_temporary_database_lock(self):
        attempts=[]
        def settle(rid,tokens):
            attempts.append((rid,tokens))
            if len(attempts)<3:
                raise sqlite3.OperationalError('database is locked')
            return (15,False)
        with patch.object(llm_usage_async,'settle',side_effect=settle):
            self.assertEqual(await llm_usage_async.settle_reliably('rid',15),(15,False))
        self.assertEqual(len(attempts),3)

    async def test_qwen_receives_restrictions_and_settles(self):
        fake={'summary':'Оценка','issues':[],'suggestions':['Добавить творог','Груша']}
        with patch('menu_editorial_policy.load_allergies',return_value=['молоко']), \
             patch.object(llm_lab,'_reserve_cancellation_safe',new=AsyncMock(return_value='rid')), \
             patch.object(llm_lab,'_settle',new=AsyncMock(return_value=(70,False))) as settle, \
             patch.object(llm_service,'review_menu_json',new=AsyncMock(return_value=(fake,70))) as qwen:
            result,rejected=await llm_lab._editorial_review('Пн: каша',123)
            self.assertEqual(rejected,3)
            self.assertEqual(result['suggestions'],[])
            self.assertEqual(qwen.await_args.kwargs['allergies'],['молоко'])
            settle.assert_awaited_once_with('rid',70)

    async def test_qwen_error_still_settles(self):
        with patch('menu_editorial_policy.load_allergies',return_value=['лук']), \
             patch.object(llm_lab,'_reserve_cancellation_safe',new=AsyncMock(return_value='rid')), \
             patch.object(llm_lab,'_settle',new=AsyncMock()) as settle, \
             patch.object(llm_service,'review_menu_json',new=AsyncMock(side_effect=RuntimeError)):
            with self.assertRaises(RuntimeError):
                await llm_lab._editorial_review('Пн',123)
            settle.assert_awaited_once_with('rid',None)

    async def test_no_paid_call_for_unsupported_allergen(self):
        with patch('menu_editorial_policy.load_allergies',return_value=['неизвестное']), \
             patch.object(llm_lab,'_reserve_cancellation_safe',new=AsyncMock()) as reserve:
            with self.assertRaises(MenuInvalid):
                await llm_lab._editorial_review('Пн',123)
            reserve.assert_not_awaited()

    async def test_cancellation_during_reserve_settles_once(self):
        entered=threading.Event()
        proceed=threading.Event()
        settled=threading.Event()
        charges=[]
        def slow_reserve():
            entered.set()
            if not proceed.wait(3):
                raise RuntimeError('test synchronization timeout')
            return 'rid'
        def record_settle(rid,tokens):
            charges.append((rid,tokens))
            settled.set()
        with patch.object(llm_usage_async,'reserve',side_effect=slow_reserve), \
             patch.object(llm_usage_async,'settle',side_effect=record_settle):
            task=asyncio.create_task(llm_lab._reserve_cancellation_safe())
            self.assertTrue(await asyncio.to_thread(entered.wait,2))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            proceed.set()
            self.assertTrue(await asyncio.to_thread(settled.wait,2))
            self.assertEqual(charges,[('rid',None)])

    async def test_simultaneous_manual_review_uses_one_slot(self):
        owner=123
        c=SimpleNamespace(from_user=SimpleNamespace(id=owner),
                          message=SimpleNamespace(chat=SimpleNamespace(id=owner)),
                          answer=AsyncMock(return_value='busy'))
        entered=asyncio.Event()
        proceed=asyncio.Event()
        async def review_once(call,menu_id):
            entered.set()
            await proceed.wait()
            return 'done'
        with patch('config.settings',SimpleNamespace(sandbox_owner_id=owner)), \
             patch.object(llm_lab,'_review_selected_owned',side_effect=review_once):
            first=asyncio.create_task(llm_lab.review_selected_menu(c,1))
            await entered.wait()
            second=await llm_lab.review_selected_menu(c,1)
            self.assertEqual(second,'busy')
            proceed.set()
            self.assertEqual(await first,'done')
            self.assertNotIn((owner,owner),llm_lab._busy)

    async def test_cancelled_probe_settles_and_releases_slot(self):
        owner=456
        c=SimpleNamespace(from_user=SimpleNamespace(id=owner),
                          message=SimpleNamespace(chat=SimpleNamespace(id=owner)),
                          answer=AsyncMock())
        entered=asyncio.Event()
        async def slow_probe():
            entered.set()
            await asyncio.Future()
        with patch.object(llm_lab,'is_current',return_value=True), \
             patch.object(llm_lab,'_reserve_cancellation_safe',new=AsyncMock(return_value='rid')), \
             patch.object(llm_lab,'_settle',new=AsyncMock()) as settle, \
             patch.object(llm_service,'probe',side_effect=slow_probe):
            task=asyncio.create_task(llm_lab.handle(c,'test'))
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            settle.assert_awaited_once_with('rid',None)
            self.assertNotIn((owner,owner),llm_lab._busy)


class DurableAccountingTests(unittest.TestCase):
    def test_startup_recovery_is_atomic_and_idempotent(self):
        with tempfile.TemporaryDirectory(prefix='q11-accounting-') as directory:
            path=os.path.join(directory,'usage.sqlite3')
            def connect():
                db=sqlite3.connect(path,isolation_level=None)
                db.row_factory=sqlite3.Row
                return db
            with patch.object(llm_usage,'connect_db',side_effect=connect):
                with connect() as db:
                    llm_usage._init(db)
                    db.execute("INSERT INTO zm_llm_usage(period,reserved,spent,calls,uncertain) VALUES ('2026-11',5000,130,1,0)")
                    db.execute("INSERT INTO zm_llm_reservations VALUES ('orphan-1','2026-11',2500,0)")
                    db.execute("INSERT INTO zm_llm_reservations VALUES ('orphan-2','2026-11',2500,0)")
                    db.execute("INSERT INTO zm_llm_reservations VALUES ('settled','2026-11',2500,1)")
                    db.commit()
                self.assertEqual(llm_usage.recover_unsettled_on_startup(),2)
                self.assertEqual(llm_usage.recover_unsettled_on_startup(),0)
                with connect() as db:
                    row=db.execute("SELECT * FROM zm_llm_usage WHERE period='2026-11'").fetchone()
                    self.assertEqual((row['reserved'],row['spent'],row['calls'],row['uncertain']),
                                     (0,5130,3,2))
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM zm_llm_reservations WHERE settled=0').fetchone()[0],0)


class ShoppingListSqliteTests(unittest.TestCase):
    def test_repeated_save_is_idempotent_and_rollback_is_atomic(self):
        with tempfile.TemporaryDirectory(prefix='q11-lists-') as directory:
            path=os.path.join(directory,'lists.sqlite3')
            def connect():
                db=sqlite3.connect(path,isolation_level=None)
                db.row_factory=sqlite3.Row
                return db
            with connect() as db:
                db.executescript('''
                  CREATE TABLE menus(id INTEGER PRIMARY KEY,owner_id INTEGER,people_count INTEGER,budget REAL,calorie_target INTEGER,result_json TEXT);
                  CREATE TABLE lists(id INTEGER PRIMARY KEY,owner_id INTEGER,title TEXT,tags TEXT);
                  CREATE TABLE list_items(list_id INTEGER,text TEXT,position INTEGER,quantity REAL,unit TEXT,selected INTEGER);
                  CREATE TABLE menu_shopping_lists(menu_id INTEGER PRIMARY KEY,list_id INTEGER);
                ''')
                good=json.dumps({'stats':{'ingredients':[[['Рис','г'],120.0]]}})
                db.execute('INSERT INTO menus VALUES (1,7,1,6000,1700,?)',(good,))
                db.execute('INSERT INTO menus VALUES (2,7,1,6000,1700,?)',('{}',))
                db.commit()
            with patch.object(menu_ui,'connect_db',side_effect=connect):
                first=menu_ui._save_menu_shopping_list(7,1)
                self.assertEqual(menu_ui._save_menu_shopping_list(7,1),first)
                self.assertIsNone(menu_ui._save_menu_shopping_list(8,1))
                with self.assertRaises(KeyError):
                    menu_ui._save_menu_shopping_list(7,2)
            with connect() as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM lists').fetchone()[0],1)
                self.assertEqual(db.execute('SELECT COUNT(*) FROM list_items').fetchone()[0],1)
                self.assertEqual(db.execute('SELECT COUNT(*) FROM menu_shopping_lists').fetchone()[0],1)


class MiddlewareTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_keyboard_removal_is_preserved(self):
        class Edit:
            reply_markup=None
        with patch.object(card_registry,'EditMessageReplyMarkup',Edit):
            seen=[]
            async def send(bot,method):
                seen.append(method.reply_markup)
                return True
            await card_registry.RegistryMiddleware()(send,None,Edit())
            self.assertEqual(seen,[None])

    async def test_existing_close_not_duplicated(self):
        class Message:
            chat_id=-1
            message_id=1
            def __init__(self,markup): self.reply_markup=markup
            def model_copy(self,update):
                return Message(update['reply_markup'])
        with patch.object(card_registry,'SendMessage',Message), \
             patch.object(card_registry,'styled',side_effect=lambda *args: InlineKeyboardButton(
                 text=args[0],callback_data=args[1])):
            markup=card_registry.InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text='Закрыть',callback_data='ui:close')]])
            async def send(bot,method):
                self.assertEqual(len(method.reply_markup.inline_keyboard),1)
                return True
            await card_registry.RegistryMiddleware()(send,None,Message(markup))

    async def test_voice_and_video_gain_close(self):
        class Method:
            chat_id=-1
            message_id=1
            def __init__(self,markup=None): self.reply_markup=markup
            def model_copy(self,update): return Method(update['reply_markup'])
        with patch.object(card_registry,'SendVoice',Method), \
             patch.object(card_registry,'SendVideo',Method), \
             patch.object(card_registry,'styled',side_effect=lambda *args: InlineKeyboardButton(
                 text=args[0],callback_data=args[1])):
            async def send(bot,method):
                self.assertEqual(method.reply_markup.inline_keyboard[-1][0].callback_data,
                                 'ui:close')
                return True
            await card_registry.RegistryMiddleware()(send,None,Method())

    def test_automatic_qwen_not_called_from_generation(self):
        source=inspect.getsource(menu_ui.actions)
        self.assertIn("review_status='skipped_policy'",source)
        self.assertNotIn('qwen_review(menu)',source)


if __name__=='__main__':
    unittest.main()

