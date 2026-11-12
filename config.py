"""Все числа проекта, которые можно менять без правки логики: бонусы, интервалы, лимиты.
Переносим сюда по этапам: сначала новые числа, старые – по мере того, как трогаем их код.
Значение из переменной окружения Render (если задана) важнее значения здесь."""
import os


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _ids(name: str, default: str) -> set:
    return {int(x) for x in os.getenv(name, default).replace(" ", "").split(",") if x.strip().isdigit()}


# ── Этап 1. Измерение ──
# Тестовые аккаунты: не попадают в воронку и когорты (поле users.is_test)
TEST_USER_IDS = _ids("TEST_USER_IDS", "58624555,5241713217,7757317298")
TEST_USERNAME_PREFIXES = ("TributeModerator",)   # модераторы Tribute, проверяющие оплату
# Часовой пояс для когорт, если у человека он неизвестен (минуты от UTC): Москва
COHORT_TZ_MIN = _int("COHORT_TZ_MIN", 180)
# Сколько недель показывать в /cohorts
COHORT_WEEKS = _int("COHORT_WEEKS", 8)

# ── Этап 2. Первый сеанс ──
# Часовой пояс по умолчанию, пока человек не открыл приложение (минуты от UTC): Москва
DEFAULT_TZ_MIN = _int("DEFAULT_TZ_MIN", 180)

# ── Этап 6. Возврат и рассылки ──
MARATHON_AUTOSTART = os.getenv("MARATHON_AUTOSTART", "1") == "1"   # марафон стартует сам у каждого нового

# ── Этап 3. Карта дня ──
EVENING_ASK_HOUR = _int("EVENING_ASK_HOUR", 20)   # «Как сегодня отыгралась карта?» – с 20:00 до 22:00 по местному
FOLLOWUP_DAYS = _int("FOLLOWUP_DAYS", 7)          # «Как сложилось?» – через неделю после расклада по вопросу
WINBACK_IDLE_DAYS = _int("WINBACK_IDLE_DAYS", 1)  # возврат тех, у кого есть Энергия или подарок, – через сутки
RETURN_AFTER_HOURS = _int("RETURN_AFTER_HOURS", 24)   # «Твоя карта ждёт» / вопрос из Кода судьбы – через сутки
SILENT_USER_MAX_MSGS = _int("SILENT_USER_MAX_MSGS", 2)   # кто ничего не сделал – не больше двух сообщений всего
