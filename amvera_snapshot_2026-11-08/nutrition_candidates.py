"""Review-required nutritional candidates from the bundled 160-row CSV.

This is a display-only staging catalog, not a database of verified product facts.
Source provenance is insufficient for auto-promotion to nutritional calculations.
"""
import csv
import math
from functools import lru_cache
from pathlib import Path

FILE = Path(__file__).with_name('kbju_candidates_v1.csv')


def _number(value):
    try:
        result=float(value)
    except (ValueError, TypeError):
        return None
    return result if math.isfinite(result) else None


@lru_cache(maxsize=1)
def candidates():
    """Read candidate entries, rejecting malformed/incomplete/duplicate rows."""
    result={}
    with FILE.open('r',encoding='utf-8-sig',newline='') as stream:
        reader=csv.DictReader(stream,delimiter=';')
        required={'canonical_name','kcal_100g','protein_g','fat_g','carbs_g','status','confidence'}
        if not required.issubset(reader.fieldnames or []):
            raise ValueError('KBJU candidate file has invalid header')
        for row in reader:
            name=str(row['canonical_name'] or '').strip()
            if not name or row['status'] != 'candidate':
                continue
            values=tuple(_number(row[key]) for key in ('kcal_100g','protein_g','fat_g','carbs_g'))
            if any(n is None for n in values):continue
            kcal,p,f,c=values
            if not 0<=kcal<=900 or not all(0<=n<=100 for n in (p,f,c)) or p+f+c>101:
                continue
            # Keep conflicting labels out of the exact-match lookup.
            key=' '.join(name.casefold().replace('ё','е').split())
            if key in result:
                result[key]=None
            else:
                result[key]=(name,values,row['confidence'])
    return {key:value for key,value in result.items() if value is not None}


def candidate_for(name):
    key=' '.join(str(name).casefold().replace('ё','е').split())
    return candidates().get(key)
