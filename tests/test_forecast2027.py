"""Проверки «Мой 2027» и регресс оплаты. Запуск из папки tarot-backend:
    python tests/test_forecast2027.py
База и Telegram не нужны: проверяется логика, которую можно проверить без них."""
import asyncio
import io
import os
import sys
from datetime import date

os.environ.update(TELEGRAM_BOT_TOKEN="123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
                  DATABASE_URL="postgresql://x:x@localhost/x", GROQ_API_KEY="",
                  FORECAST_2027_ENABLED="admins", ADMIN_IDS="1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


async def main():
    import main as m
    import forecast2027 as f

    # ── Регресс: пакеты Энергии и подписка разбираются как раньше ──
    assert m._parse_invoice_payload("pack_150:42:123456") == ("pack_150", 42, None)
    assert m._parse_invoice_payload("pack_150:42:123456:r7") == ("pack_150", 42, 7)
    sub = next(iter(m.SUBS))
    assert m._parse_invoice_payload(f"{sub}:42:123456") == (sub, 42, None)
    # Платёж-прогноз не похож на пакет и не уходит в начисление Энергии
    assert m._parse_invoice_payload("f27:tarot_2027:42:123456") is None
    assert f.is_payload("f27:tarot_2027:42:123456") and not f.is_payload("pack_150:42:1")
    assert f.parse_payload("f27:tarot_2027:42:123456") == ("tarot_2027", 42, False)
    assert f.parse_payload("f27:tarot_2027:42:123456:g") == ("tarot_2027", 42, True)   # подарок
    assert f.parse_payload("f27:tarot_2027:42:123456:x") is None
    assert m._parse_invoice_payload("f27:tarot_2027:42:123456:g") is None
    assert f.parse_payload("f27:pack_150:42:1") is None
    # Товары-прогнозы и пакеты Tribute не пересекаются по названию
    assert not set(f.PRODUCTS) & set(m.PACKS) and not set(f.PRODUCTS) & set(m.SUBS)
    print("✓ оплата: пакеты и подписка как раньше, прогнозы отдельно")

    # ── Флаг ──
    assert f.visible_for({"telegram_id": 1}) and not f.visible_for({"telegram_id": 2})
    os.environ["FORECAST_2027_ENABLED"] = "off"
    assert not f.visible_for({"telegram_id": 1})
    assert len(m.menu_kb({"telegram_id": 1, "balance": 0}).inline_keyboard) == 3   # меню как было
    os.environ["FORECAST_2027_ENABLED"] = "on"
    assert f.visible_for({"telegram_id": 2})
    os.environ["FORECAST_2027_ENABLED"] = "admins"
    print("✓ флаг off / admins / on")

    # ── Обработчики «2027» стоят раньше общих, но срабатывают только на свои кнопки и ввод ──
    assert m.dp.callback_query.handlers[0].callback.__name__ == "f27_cb"
    assert [h.callback.__name__ for h in m.dp.message.handlers][:4] == ["f27_name", "f27_time", "f27_city", "f27_date"]
    print("✓ порядок обработчиков")

    # ── Расчёты ──
    d = date(1993, 4, 12)
    t = f.teaser_calc(d)
    assert t == {"personal_year": 9, "year_arcana": 9, "year_card": 9}
    c = f.product_calc("tarot_2027", d)
    assert len(c["month_cards"]) == 12 and len(set(c["month_cards"])) == 12 and t["year_card"] not in c["month_cards"]
    assert f.deck_id(22) == 0 and f.deck_id(8) == 11 and f.deck_id(11) == 8
    assert f.can_buy("tarot_2027", set()) and not f.can_buy("tarot_2027", {"tarot_2027"})
    assert not f.can_buy("full_2027", {"numerology_2027"})
    print("✓ расчёты и правила покупки")

    # ── PDF на разных алфавитах ──
    from PIL import Image

    async def card_jpeg(cid, width=640):
        b = io.BytesIO()
        Image.new("RGB", (width, int(width * 1.7)), (60, 40, 90)).save(b, "JPEG")
        return b.getvalue()
    m.card_jpeg = card_jpeg
    for lang, txt in {"ru": "Год смелости и новых начал.", "ar": "هذا العام عن الشجاعة والبدايات الجديدة.",
                      "kk": "Бұл жыл батылдық туралы.", "es": "Año de valentía: ñ á é."}.items():
        pdf = await f.build_pdf("tarot_2027", c, [{"key": k, "text": txt} for k, _ in f.SECTIONS_RU],
                                {"name": "Анна"}, lang)
        assert pdf[:4] == b"%PDF" and len(pdf) > 20000, lang
    print("✓ PDF: ru, ar, kk, es")

    # ── Ссылка на скачивание PDF ──
    tk = f.pdf_token(5, "tarot_2027", "ru")
    assert f.check_token(tk) == (5, "tarot_2027", "ru") and f.check_token(tk[:-1] + "0") is None
    print("✓ ссылка на PDF подписана")

    # ── Этап 2: астро ──
    import astro2027 as a
    c = a.find_city("Алматы", 1)[0]
    assert c["tz"] == "Asia/Almaty"
    nat = a.natal(m._sky_load, m._lon, date(1993, 4, 12), "07:30", c["lat"], c["lon"], c["tz"])
    assert nat["points"]["sun"]["sign"] == "Овен" and nat["points"]["saturn"]["sign"] == "Водолей" and "asc" in nat
    tr = a.transits(m._sky_load, m._lon, nat, 2027)
    assert len(tr["months"]) == 12 and tr["months"][0]["planets"][1]["sign"] == "Овен"   # Сатурн в Овне в 2027
    assert f.needs_astro("astro_2027", {"birth_time": None, "birth_city": None})
    assert not f.needs_astro("astro_2027", {"birth_time": "?", "birth_city": "Алматы, KZ"})
    assert not f.needs_astro("tarot_2027", {})
    print("✓ астро: натал, транзиты, город")
    print("Все проверки пройдены.")


asyncio.run(main())
