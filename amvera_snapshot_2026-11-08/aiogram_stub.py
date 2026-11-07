"""Minimal aiogram stand-in: decorators return the function unchanged so handlers are directly callable."""
import sys, types, importlib.abc, importlib.machinery

class Magic:
    def __init__(self,*a,**k): self.args=a; self.kwargs=k
    def __getattr__(self,n):
        if n.startswith('__'): raise AttributeError(n)
        return Magic()
    def __call__(self,*a,**k): return Magic()
    def __eq__(self,o): return Magic()
    def __hash__(self): return id(self)
    def __and__(self,o): return Magic()
    def __or__(self,o): return Magic()
    def __invert__(self): return Magic()
    def __getitem__(self,i): return Magic()
class Router:
    def __init__(self,*a,**k): pass
    def __getattr__(self,n):
        if n.startswith('__'): raise AttributeError(n)
        def deco_factory(*a,**k):
            def deco(fn): return fn
            return deco
        return deco_factory
    def include_router(self,*a,**k): pass
class BaseFilter:
    def __init__(self,*a,**k): pass
class State:
    def __init__(self,*a,**k): pass
class StatesGroup: pass
class InlineKeyboardMarkup:
    def __init__(self,inline_keyboard=None,**k): self.inline_keyboard=inline_keyboard or []
class InlineKeyboardButton:
    def __init__(self,text='',callback_data=None,**k): self.text=text; self.callback_data=callback_data
class InlineKeyboardBuilder:
    def __init__(self): self.rows=[]
    def row(self,*b,**k): self.rows.append(list(b)); return self
    def add(self,*b,**k): self.rows.append(list(b)); return self
    def adjust(self,*a,**k): return self
    def as_markup(self): return InlineKeyboardMarkup(inline_keyboard=self.rows)
class BufferedInputFile:
    def __init__(self,data,filename=''): self.data=data; self.filename=filename
class TelegramError(Exception): pass

SPECIAL={'Router':Router,'BaseFilter':BaseFilter,'State':State,'StatesGroup':StatesGroup,
 'InlineKeyboardMarkup':InlineKeyboardMarkup,'InlineKeyboardButton':InlineKeyboardButton,
 'InlineKeyboardBuilder':InlineKeyboardBuilder,'BufferedInputFile':BufferedInputFile,
 'F':Magic(),'TelegramBadRequest':TelegramError,'TelegramNetworkError':TelegramError,
 'TelegramServerError':TelegramError,'TelegramRetryAfter':TelegramError,'TelegramAPIError':TelegramError,
 'TelegramForbiddenError':TelegramError}

class Mod(types.ModuleType):
    __path__=[]
    def __getattr__(self,n):
        if n.startswith('__'): raise AttributeError(n)
        if n in SPECIAL: return SPECIAL[n]
        if n and n[0].isupper() and n not in ('F',):
            cls=type(n,(Magic,),{}); setattr(self,n,cls); return cls
        return Magic()
class Finder(importlib.abc.MetaPathFinder,importlib.abc.Loader):
    def find_spec(self,name,path,target=None):
        if name=='aiogram' or name.startswith('aiogram.'):
            return importlib.machinery.ModuleSpec(name,self,is_package=True)
    def create_module(self,spec): return Mod(spec.name)
    def exec_module(self,m): pass
def install():
    sys.meta_path.insert(0,Finder())
