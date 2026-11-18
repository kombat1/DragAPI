"""Справка по выражениям (кнопка «?» у полей). Каждый пример проверяется тестом на настоящем вычислителе
(tests/test_exprdocs.py), поэтому результаты в справке всегда совпадают с поведением платформы.

Пример: {"expr": ..., "result": ...} или {"template": ..., "result": ...}; spaces — где пример уместен
(api / automation / bot); пусто — везде.
"""

from __future__ import annotations

# Значения переменных, на которых считаются примеры (показываются в справке рядом с ними).
SAMPLE = {
    "text": "Сколько стоит доставка?",
    "answer": "  Да, конечно ",
    "age": 25,
    "name": "Аня",
    "user_name": "Аня",
    "role": "moderator",
    "is_admin": False,
    "is_group": True,
    "is_chat_admin": False,
    "args": "@ivan 30",
    "saved": {"name": "Аня", "visits": 3},
    "file": {"kind": "photo", "name": "cat.jpg", "size": 120000, "file_id": "AgAC…"},
    "phone": "+79001234567",
    "email": "anna@example.com",
    "price": 199.9,
    "qty": 3,
    "order": None,
    "items": ["чай", "кофе", "сок"],
    "i": 2,
    "n": 4,
    "response": {"status": 200, "ok": True, "json": {"price": 99, "currency": "RUB"}},
    "choice": "Цены",
}
SAMPLE_ENV = {"API_KEY": "sk-123", "ADMIN_CHAT": "123456789"}

SECTIONS = [
    {
        "id": "basics",
        "title": "Основы",
        "text": [
            "Выражение — короткая формула на упрощённом Python. Переменные пишутся по имени (age, text), "
            "строки — в кавычках ('да' или \"да\"), числа — как есть (10, 2.5).",
            "Сравнение — двумя знаками: ==. Ещё: != (не равно), >, <, >=, <=. Несколько условий соединяются "
            "словами and, or, not. Скобки задают порядок.",
            "В блоке «Условие» выражение должно дать «истину» или «ложь»: True — выход «да», False — «нет». "
            "Пустая строка, 0, None и пустой список считаются ложью.",
        ],
        "examples": [
            {"expr": "age >= 18", "result": True, "note": "число больше или равно"},
            {"expr": "role == 'moderator'", "result": True, "note": "точное совпадение строки"},
            {"expr": "role != 'banned'", "result": True, "note": "не равно"},
            {"expr": "age >= 18 and age < 65", "result": True, "note": "оба условия"},
            {"expr": "is_admin or role == 'moderator'", "result": True, "note": "хотя бы одно",
             "spaces": ["bot"]},
            {"expr": "not is_admin", "result": True, "note": "отрицание"},
            {"expr": "order is None", "result": True, "note": "значения нет (запись не найдена и т.п.)",
             "spaces": ["api"]},
            {"expr": "price * qty", "result": 599.7, "note": "арифметика: + - * / ** (степень)"},
            {"expr": "n % 2 == 0", "result": True, "note": "% — остаток от деления: чётное ли число"},
            {"expr": "17 // 5", "result": 3, "note": "// — деление нацело"},
        ],
    },
    {
        "id": "strings",
        "title": "Сравнение строк",
        "text": [
            "Строки сравниваются буква в букву: 'Да' и 'да' — разные, пробелы тоже важны. Поэтому ответ "
            "пользователя обычно сначала «нормализуют»: strip убирает пробелы по краям, lower — переводит в "
            "нижний регистр. Строку справа пишите маленькими буквами.",
            "contains(где, что) и что in где — есть ли кусок текста внутри строки (или элемент в списке). "
            "Внимание: 'да' найдётся и в «никогда» — для целого слова используйте список вариантов или "
            "регулярное выражение.",
        ],
        "examples": [
            {"expr": "answer == 'Да'", "result": False, "note": "точно — не совпало из-за пробелов и запятой"},
            {"expr": "lower(strip(answer)) == 'да, конечно'", "result": True, "note": "без регистра и пробелов"},
            {"expr": "contains(lower(answer), 'да')", "result": True, "note": "содержит кусок текста"},
            {"expr": "'да' in lower(answer)", "result": True, "note": "то же самое через in"},
            {"expr": "contains(text, '?')", "result": True, "note": "сообщение — вопрос", "spaces": ["bot"]},
            {"expr": "lower(strip(choice)) in ['цены', 'прайс', 'стоимость']", "result": True,
             "note": "один из нескольких вариантов"},
            {"expr": "contains(lower(text), 'цен') or contains(lower(text), 'стои')", "result": True,
             "note": "по основе слова: «цена», «цены», «стоит», «стоимость»…", "spaces": ["bot"]},
            {"expr": "contains(lower(text), 'доставк') and not contains(lower(text), 'отмен')", "result": True,
             "note": "одно слово есть, другого нет", "spaces": ["bot"]},
            {"expr": "len(strip(answer)) >= 3", "result": True, "note": "длина строки"},
            {"expr": "strip(answer) != ''", "result": True, "note": "ответ не пустой"},
            {"expr": "text[0:7]", "result": "Сколько", "note": "часть строки: символы с 0 по 6"},
            {"expr": "upper(name)", "result": "АНЯ", "note": "заглавными"},
        ],
    },
    {
        "id": "regex",
        "title": "Шаблоны (регулярные выражения)",
        "text": [
            "regex_match(шаблон, строка) — подходит ли ВСЯ строка под шаблон. Чтобы искать внутри текста, "
            "добавьте .* по краям. \\d — цифра, \\b — граница слова, {10,12} — от 10 до 12 раз, "
            "(а|б) — или, ? — необязательный символ.",
            "Шаблон пишите с буквой r перед кавычками: r'…'. Без неё \\b превращается в служебный символ, и "
            "проверка молча не срабатывает.",
        ],
        "examples": [
            {"expr": "regex_match(r'\\+?\\d{10,12}', phone)", "result": True, "note": "похоже на телефон"},
            {"expr": "regex_match(r'[^@ ]+@[^@ ]+\\.[a-z]{2,}', email)", "result": True, "note": "похоже на e-mail"},
            {"expr": "regex_match(r'.*\\b(да|ага|конечно)\\b.*', lower(answer))", "result": True,
             "note": "целое слово «да» в ответе"},
            {"expr": "regex_match(r'.*\\b(да|ага)\\b.*', 'никогда')", "result": False,
             "note": "…а «никогда» не подходит — в отличие от contains"},
            {"expr": "regex_match(r'\\d+', strip(answer))", "result": False, "note": "только цифры"},
        ],
    },
    {
        "id": "numbers",
        "title": "Числа",
        "text": [
            "Ответ пользователя — всегда строка. Чтобы сравнивать как число, в «Спросить» выберите «Ожидаем: "
            "Число» (бот сам переспросит, если прислали не число) или переведите вручную: int('25'), "
            "float('2,5') не сработает — нужна точка.",
        ],
        "examples": [
            {"expr": "int('25') + 1", "result": 26, "note": "строку в целое число"},
            {"expr": "float('2.5') * 2", "result": 5.0, "note": "строку в дробное"},
            {"expr": "round(price * 0.9, 2)", "result": 179.91, "note": "скидка 10%, два знака", "spaces": ["api", "bot"]},
            {"expr": "max(qty, 1)", "result": 3, "note": "не меньше 1"},
            {"expr": "min(age, 100)", "result": 25, "note": "не больше 100"},
            {"expr": "abs(-5)", "result": 5, "note": "модуль"},
            {"expr": "str(qty) + ' шт.'", "result": "3 шт.", "note": "число в строку и склейка"},
            {"expr": "100 * i + 50", "result": 250, "note": "координата от номера повтора",
             "spaces": ["automation"]},
        ],
    },
    {
        "id": "data",
        "title": "Списки, словари, сохранённые данные",
        "text": [
            "Список — [1, 2, 3], элемент по номеру — items[0] (с нуля), последний — items[-1]. Словарь — "
            "{'ключ': значение}, значение по ключу — saved['name']. Обращение к несуществующему ключу — "
            "ошибка, поэтому сначала проверьте: 'ключ' in saved.",
            "Методы через точку (saved.get(...), text.lower()) не поддерживаются — используйте функции.",
        ],
        "examples": [
            {"expr": "items[0]", "result": "чай", "note": "первый элемент"},
            {"expr": "len(items)", "result": 3, "note": "сколько элементов"},
            {"expr": "'кофе' in items", "result": True, "note": "есть ли в списке"},
            {"expr": "saved['name']", "result": "Аня", "note": "сохранённое блоком «Запомнить»",
             "spaces": ["bot"]},
            {"expr": "saved['visits'] + 1 if 'visits' in saved else 1", "result": 4,
             "note": "счётчик визитов: с проверкой, что ключ есть", "spaces": ["bot"]},
            {"expr": "'phone' in saved", "result": False, "note": "сохранено ли значение", "spaces": ["bot"]},
            {"expr": "file['kind'] == 'photo' and file['size'] < 5000000", "result": True,
             "note": "прислали фото меньше 5 МБ", "spaces": ["bot"]},
            {"expr": "response['status'] == 200", "result": True, "note": "ответ блока «HTTP-запрос»",
             "spaces": ["automation", "bot"]},
            {"expr": "response['json']['price']", "result": 99, "note": "значение из JSON-ответа",
             "spaces": ["automation", "bot"]},
            {"expr": "default(order, 'нет заказа')", "result": "нет заказа", "note": "запасное значение вместо None"},
        ],
    },
    {
        "id": "ternary",
        "title": "Выбор значения: … if … else …",
        "text": ["Значение по условию в одну строку: ЕСЛИ_ДА if условие else ЕСЛИ_НЕТ. Удобно в «Переменных»."],
        "examples": [
            {"expr": "'взрослый' if age >= 18 else 'ребёнок'", "result": "взрослый"},
            {"expr": "'Здравствуйте, ' + saved['name'] if 'name' in saved else 'Как вас зовут?'",
             "result": "Здравствуйте, Аня", "note": "приветствие знакомого пользователя", "spaces": ["bot"]},
            {"expr": "price * 0.9 if qty >= 3 else price", "result": 179.91, "note": "скидка от трёх штук",
             "spaces": ["api", "bot"]},
        ],
    },
    {
        "id": "templates",
        "title": "Текст с подстановкой {…}",
        "text": [
            "В полях «Текст», «Сообщение», «Адрес» и т.п. пишется обычный текст, а в фигурных скобках — "
            "выражение: его значение подставится. Внутри {…} работает всё из этой справки. Сами фигурные скобки "
            "как текст — {{ и }}.",
            "В API-проектах вместо этого используют f-строки в выражении: f'Привет, {name}!'.",
        ],
        "examples": [
            {"template": "Привет, {user_name}!", "result": "Привет, Аня!", "spaces": ["bot", "automation"]},
            {"template": "Итого: {round(price * qty, 2)} ₽", "result": "Итого: 599.7 ₽"},
            {"template": "Вы с нами {saved['visits']}-й раз", "result": "Вы с нами 3-й раз", "spaces": ["bot"]},
            {"template": "Шаг {i} из {n}", "result": "Шаг 2 из 4", "spaces": ["automation"]},
            {"template": "{'Доброе утро' if age < 30 else 'Здравствуйте'}, {name}", "result": "Доброе утро, Аня"},
            {"template": "Скобки как текст: {{пример}}", "result": "Скобки как текст: {пример}"},
            {"expr": "f'Привет, {name}!'", "result": "Привет, Аня!", "note": "f-строка в выражении",
             "spaces": ["api"]},
            {"expr": "f'{qty:03d}'", "result": "003", "note": "формат: ведущие нули"},
        ],
    },
    {
        "id": "env",
        "title": "Переменные окружения и секреты",
        "text": [
            "Переменные из «Настройки проекта → Переменные» доступны как env['ИМЯ'] — в т.ч. секретные. "
            "В ответы и логи они сами по себе не попадают.",
        ],
        "examples": [
            {"expr": "env['ADMIN_CHAT']", "result": "123456789", "note": "например, ID чата администратора"},
            {"expr": "env['API_KEY'] != ''", "result": True, "note": "ключ задан"},
        ],
    },
]

FUNCTIONS = [
    {"name": "lower", "signature": "lower(строка)", "description": "в нижний регистр",
     "expr": "lower('Привет')", "result": "привет"},
    {"name": "upper", "signature": "upper(строка)", "description": "в верхний регистр",
     "expr": "upper('Привет')", "result": "ПРИВЕТ"},
    {"name": "strip", "signature": "strip(строка)", "description": "убрать пробелы и переводы строк по краям",
     "expr": "strip('  да ')", "result": "да"},
    {"name": "contains", "signature": "contains(где, что)", "description": "есть ли кусок в строке / элемент в списке",
     "expr": "contains('доставка', 'став')", "result": True},
    {"name": "regex_match", "signature": "regex_match(шаблон, строка)",
     "description": "подходит ли вся строка под регулярное выражение", "expr": "regex_match(r'\\d{3}', '123')",
     "result": True},
    {"name": "len", "signature": "len(x)", "description": "длина строки или списка", "expr": "len('привет')",
     "result": 6},
    {"name": "int", "signature": "int(x)", "description": "в целое число (строку «12» → 12)", "expr": "int('12')",
     "result": 12},
    {"name": "float", "signature": "float(x)", "description": "в дробное число", "expr": "float('2.5')",
     "result": 2.5},
    {"name": "str", "signature": "str(x)", "description": "в строку", "expr": "str(42) + '!'", "result": "42!"},
    {"name": "bool", "signature": "bool(x)", "description": "истина/ложь: пустое и 0 — False", "expr": "bool('')",
     "result": False},
    {"name": "round", "signature": "round(x, знаков)", "description": "округлить", "expr": "round(3.14159, 2)",
     "result": 3.14},
    {"name": "abs", "signature": "abs(x)", "description": "модуль числа", "expr": "abs(-7)", "result": 7},
    {"name": "min", "signature": "min(a, b, …)", "description": "наименьшее", "expr": "min(5, 3, 9)", "result": 3},
    {"name": "max", "signature": "max(a, b, …)", "description": "наибольшее", "expr": "max([5, 3, 9])", "result": 9},
    {"name": "sum", "signature": "sum(список)", "description": "сумма", "expr": "sum([1, 2, 3])", "result": 6},
    {"name": "default", "signature": "default(значение, запасное)", "description": "запасное, если значение None",
     "expr": "default(None, 'гость')", "result": "гость"},
]

MISTAKES = [
    {"wrong": "answer = 'да'", "right": "answer == 'да'", "why": "сравнение — двумя знаками ="},
    {"wrong": "answer == да", "right": "answer == 'да'", "why": "строку нужно взять в кавычки, иначе это имя переменной"},
    {"wrong": "answer.lower() == 'да'", "right": "lower(answer) == 'да'", "why": "методы через точку не поддерживаются"},
    {"wrong": "lower(answer) == 'Да'", "right": "lower(answer) == 'да'", "why": "после lower слева всегда маленькие буквы"},
    {"wrong": "saved['phone']", "right": "saved['phone'] if 'phone' in saved else ''",
     "why": "если ключа ещё нет, будет ошибка — проверьте через in"},
    {"wrong": "age > '18'", "right": "age > 18", "why": "число сравнивают с числом, без кавычек"},
    {"wrong": "contains('да', answer)", "right": "contains(answer, 'да')", "why": "сначала — где ищем, потом — что"},
    {"wrong": "regex_match('.*\\bда\\b.*', answer)", "right": "regex_match(r'.*\\bда\\b.*', answer)",
     "why": "в шаблонах нужна буква r перед кавычками, иначе \\b и подобное понимаются неправильно"},
]


def docs() -> dict:
    return {"sections": SECTIONS, "functions": FUNCTIONS, "mistakes": MISTAKES, "sample": SAMPLE,
            "sample_env": SAMPLE_ENV}
