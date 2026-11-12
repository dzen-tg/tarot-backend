"""Тексты бота для пользователей. Править можно здесь, не трогая код.
Формат: ключ → {"ru": "...", "en": "..."}. В фигурных скобках – подстановки ({name}, {cost} и т.п.), их не удалять.
Тире – только короткое (–). Пол человека неизвестен: без «готов», «должен», «сам», «(а)».
Английский – для тех, у кого язык Telegram не русский, не украинский, не белорусский и не казахский."""

TEXTS = {
    # ── Старт и первый сеанс (этап 2) ──
    "start_new": {
        "ru": "Привет, {name} 🔮\nПодумай о том, что тебя сейчас волнует, и вытяни карту. Первый ответ бесплатно.",
        "en": "Hi, {name} 🔮\nThink about what's on your mind right now and draw a card. Your first answer is free.",
    },
    "start_new_noname": {
        "ru": "Привет 🔮\nПодумай о том, что тебя сейчас волнует, и вытяни карту. Первый ответ бесплатно.",
        "en": "Hi 🔮\nThink about what's on your mind right now and draw a card. Your first answer is free.",
    },
    "hello_short": {"ru": "Привет, {name} 🔮", "en": "Hi, {name} 🔮"},
    "hello_short_noname": {"ru": "Привет 🔮", "en": "Hi 🔮"},
    "terms_line": {
        "ru": "<i>Нажимая кнопку, ты принимаешь условия – /terms</i>",
        "en": "<i>By tapping the button you accept the terms – /terms</i>",
    },
    "terms_line_use": {
        "ru": "<i>Пользуясь ботом, ты принимаешь условия – /terms</i>",
        "en": "<i>By using the bot you accept the terms – /terms</i>",
    },
    "ref_bonus_line": {
        "ru": "✨ Ты по приглашению друга – тебе начислено +{bonus} Энергии.",
        "en": "✨ A friend invited you – you've got +{bonus} Energy.",
    },
    "btn_draw": {"ru": "🃏 Вытянуть карту", "en": "🃏 Draw a card"},
    "drawing": {"ru": "🃏 Тасую колоду…", "en": "🃏 Shuffling the deck…"},
    "first_card_caption": {"ru": "<b>{card}</b>\n\n<i>{message}</i>\n\n{text}", "en": "<b>{card}</b>\n\n<i>{message}</i>\n\n{text}"},
    "first_card_used": {
        "ru": "Первая карта уже вытянута. Задай картам вопрос – просто напиши его сюда.",
        "en": "Your first card is already drawn. Ask the cards a question – just type it here.",
    },
    "ob_fate": {
        "ru": "Хочешь узнать свой аркан по дате рождения? Это бесплатно, займёт 10 секунд.",
        "en": "Want to know your arcana by date of birth? It's free and takes 10 seconds.",
    },
    "btn_fate": {"ru": "🔢 Мой Код судьбы", "en": "🔢 My Fate Code"},
    "ob_push": {"ru": "Присылать тебе карту дня каждое утро?", "en": "Shall I send you a card of the day every morning?"},
    "btn_push_morning": {"ru": "☀️ В 8:00", "en": "☀️ At 8:00"},
    "btn_push_evening": {"ru": "🌙 В 21:00", "en": "🌙 At 21:00"},
    "btn_push_no": {"ru": "Не надо", "en": "No, thanks"},
    "push_set": {
        "ru": "Готово: карта дня будет приходить в {hour}:00{tz}. Время можно поменять в приложении, в Профиле.",
        "en": "Done: the card of the day will arrive at {hour}:00{tz}. You can change the time in the app, in Profile.",
    },
    "push_set_msk": {"ru": " по Москве", "en": " Moscow time"},
    "push_no": {
        "ru": "Хорошо, каждый день писать не буду.",
        "en": "Okay, I won't write every day.",
    },
    "ob_preset": {
        "ru": "Могу разложить три карты на твою ситуацию: что происходит, что мешает, что делать.",
        "en": "I can lay out three cards on your situation: what's happening, what's in the way, what to do.",
    },
    "btn_preset_free": {"ru": "🎴 Расклад на ситуацию – бесплатно", "en": "🎴 Situation spread – free"},
    "btn_preset_paid": {"ru": "🎴 Расклад на ситуацию – {cost} ⚡", "en": "🎴 Situation spread – {cost} ⚡"},

    # ── Возвращение и меню ──
    "welcome_back": {
        "ru": "С возвращением, {name} 🔮 Что смотрим сегодня?",
        "en": "Welcome back, {name} 🔮 What shall we look at today?",
    },
    "welcome_back_noname": {"ru": "С возвращением 🔮 Что смотрим сегодня?", "en": "Welcome back 🔮 What shall we look at today?"},
    "btn_menu_daily": {"ru": "🃏 Карта дня", "en": "🃏 Card of the day"},
    "btn_menu_ask": {"ru": "⚡ Задать вопрос", "en": "⚡ Ask a question"},
    "btn_menu_spreads": {"ru": "🎴 Расклады", "en": "🎴 Spreads"},
    "btn_menu_fate": {"ru": "🔢 Код судьбы", "en": "🔢 Fate Code"},
    "btn_menu_dream": {"ru": "🌙 Сон", "en": "🌙 Dream"},
    "btn_menu_balance": {"ru": "Баланс: {balance} ⚡", "en": "Balance: {balance} ⚡"},

    # ── Вопрос текстом в чат ──
    "ask_confirm": {"ru": "Вытянуть карту на этот вопрос?", "en": "Draw a card for this question?"},
    "btn_ask_free": {"ru": "🃏 Вытянуть карту – бесплатно", "en": "🃏 Draw a card – free"},
    "btn_ask_paid": {"ru": "🃏 Вытянуть карту – {cost} ⚡", "en": "🃏 Draw a card – {cost} ⚡"},
    "ask_expired": {
        "ru": "Не нашёл вопрос – напиши его ещё раз, и я вытяну карту.",
        "en": "I lost the question – send it again and I'll draw a card.",
    },
    "ask_hint": {
        "ru": "Напиши картам вопрос одним сообщением. Например: «Что мне важно понять в отношениях?»",
        "en": "Send the cards your question in one message. For example: “What should I understand about my relationship?”",
    },
    "no_energy": {
        "ru": "Не хватает Энергии: нужно {cost}, у тебя {balance}.",
        "en": "Not enough Energy: you need {cost}, you have {balance}.",
    },
    "btn_topup": {"ru": "⚡ Пополнить", "en": "⚡ Top up"},
    "btn_open_reading": {"ru": "📜 Открыть в приложении", "en": "📜 Open in the app"},
    "reading_failed": {
        "ru": "Карты не ответили – что-то пошло не так. Энергию вернул, попробуй ещё раз через минуту.",
        "en": "The cards didn't answer – something went wrong. Energy is refunded, try again in a minute.",
    },
    "minor_paid": {
        "ru": "Платные расклады доступны с 18 лет. Бесплатно остаются карта дня, Код судьбы и сонник раз в день.",
        "en": "Paid spreads are available from 18. The card of the day, Fate Code and one dream a day stay free.",
    },

    # ── Карта дня в чате (этапы 3 и 6) ──
    "daily_push_caption": {
        "ru": "{sky}<b>Карта дня – {card}</b>\n\n<i>{message}</i>",
        "en": "{sky}<b>Card of the day – {card}</b>\n\n<i>{message}</i>",
    },
    "btn_read_full": {"ru": "📖 Читать полностью", "en": "📖 Read in full"},
    "btn_share_card": {"ru": "📤 Поделиться картой", "en": "📤 Share the card"},
    "btn_ask_own": {"ru": "⚡ Спросить карты о своём", "en": "⚡ Ask the cards about yours"},
    "share_ready": {
        "ru": "Картинка готова 👆 Перешли её тому, кому хочешь показать.",
        "en": "The picture is ready 👆 Forward it to whoever you like.",
    },
    "share_caption_daily": {
        "ru": "Моя карта дня – {card}. Вытяни свою: {link}",
        "en": "My card of the day – {card}. Draw yours: {link}",
    },
    "share_caption_fate": {
        "ru": "Мой аркан – {arcana}. Узнай свой: {link}",
        "en": "My arcana – {arcana}. Find yours: {link}",
    },
    "btn_fate_quick_free": {"ru": "🎁 Задать вопрос картам – бесплатно", "en": "🎁 Ask the cards – free"},
    "btn_fate_quick": {"ru": "⚡ Задать вопрос картам – {cost} ⚡", "en": "⚡ Ask the cards – {cost} ⚡"},
    "btn_share_fate": {"ru": "📤 Поделиться арканом", "en": "📤 Share my arcana"},
    "evening_ask": {
        "ru": "Как сегодня отыгралась карта «{card}»?",
        "en": "How did “{card}” play out today?",
    },
    "btn_match": {"ru": "✅ Совпало", "en": "✅ It matched"},
    "btn_miss": {"ru": "❌ Мимо", "en": "❌ Missed"},
    "evening_thanks_match": {
        "ru": "Здорово, что карта попала в точку ✨ Хочешь заглянуть в завтра? Три карты покажут, что происходит, что мешает и что делать.",
        "en": "Great that the card hit the mark ✨ Want a look at tomorrow? Three cards will show what's happening, what's in the way and what to do.",
    },
    "evening_thanks_miss": {
        "ru": "Спасибо за честность 🙏 Карта дня – общий фон. Чтобы посмотреть на свою ситуацию точнее, разложи три карты на завтра.",
        "en": "Thanks for being honest 🙏 The card of the day is a general mood. For a closer look at your situation, lay out three cards for tomorrow.",
    },
    "btn_preset_tomorrow": {"ru": "🎴 Расклад на завтра", "en": "🎴 Spread for tomorrow"},

    # ── Код судьбы ──
    "fate_pending": {
        "ru": "Считаю твой Код судьбы – пришлю его сюда через пару минут 🔢",
        "en": "Working out your Fate Code – I'll send it here in a couple of minutes 🔢",
    },

    # ── Возврат и рассылки (этап 6) ──
    "return_card": {
        "ru": "Твоя карта ждёт 🃏 Подумай о том, что волнует, и вытяни её – первый ответ бесплатно.",
        "en": "Your card is waiting 🃏 Think of what's on your mind and draw it – the first answer is free.",
    },
    "return_fate_question": {
        "ru": "Вчера Код судьбы закончился вопросом к тебе. Давай посмотрим расклад на это – три карты покажут, что происходит и что делать.",
        "en": "Yesterday your Fate Code ended with a question for you. Let's look at it with a spread – three cards will show what's happening and what to do.",
    },
    "followup_week": {
        "ru": "🔮 Неделю назад был вопрос к картам:\n«{topic}»\n\nКак всё сложилось?",
        "en": "🔮 A week ago you asked the cards:\n“{topic}”\n\nHow did it turn out?",
    },
    "weekly_horo": {
        "ru": "🔭 Гороскоп на неделю готов – что ждёт твой знак в ближайшие дни.",
        "en": "🔭 Your weekly horoscope is ready – what the coming days hold for your sign.",
    },
    "btn_weekly_horo": {"ru": "🔭 Открыть гороскоп", "en": "🔭 Open the horoscope"},
    "weekly_card": {
        "ru": "🃏 Новая неделя – новая карта. Загляни, что она подскажет сегодня.",
        "en": "🃏 New week, new card. See what it suggests for today.",
    },
    "btn_open_daily": {"ru": "🃏 Открыть карту дня", "en": "🃏 Open the card of the day"},
}


def tr(key: str, lang: str = "ru", **kw) -> str:
    item = TEXTS.get(key) or {}
    text = item.get(lang) or item.get("ru") or key
    return text.format(**kw) if kw else text
