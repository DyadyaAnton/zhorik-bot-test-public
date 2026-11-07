"""Сопоставление SKU -> канонический продукт.
1) ключевые слова (быстро и точно) 2) нечёткое сравнение названий 3) всё сомнительное -> очередь на ручную проверку."""
import sqlite3
try:
    from rapidfuzz import fuzz, process
    def _best(name, choices):
        r = process.extractOne(name, choices, scorer=fuzz.token_set_ratio)
        return (r[0], r[1]) if r else (None, 0)
except ImportError:                      # запасной вариант без зависимостей
    import difflib
    def _best(name, choices):
        best, score = None, 0
        for c in choices:
            s = difflib.SequenceMatcher(None, name.lower(), c.lower()).ratio() * 100
            if s > score:
                best, score = c, s
        return best, score

AUTO_THRESHOLD = 80    # выше — принимаем автоматически
REVIEW_THRESHOLD = 55  # между — в очередь на проверку


def match_all(db: sqlite3.Connection):
    canon = {row[1]: (row[0], (row[2] or "")) for row in
             db.execute("SELECT id, name, keywords FROM canonical_products")}
    names = list(canon)
    review = []
    for sku_id, name in db.execute(
            "SELECT id, name FROM skus WHERE canonical_id IS NULL AND match_status='auto'").fetchall():
        low = name.lower()
        # 1. ключевые слова: все слова из keywords должны встретиться в названии
        hit = None
        for cname, (cid, kw) in canon.items():
            words = [w.strip().lower() for w in kw.split(",") if w.strip()]
            if words and all(w in low for w in words):
                hit = (cname, 100); break
        if not hit:
            hit = _best(name, names)
        cname, score = hit
        if cname and score >= AUTO_THRESHOLD:
            db.execute("UPDATE skus SET canonical_id=?, match_score=? WHERE id=?",
                       (canon[cname][0], score, sku_id))
        elif cname and score >= REVIEW_THRESHOLD:
            review.append((sku_id, name, cname, score))
    db.commit()
    return review   # покажите человеку / отправьте в LLM для однократной разметки
