import logging
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackContext,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

DB_PATH = Path(__file__).with_name("quizzbot.db")
MAX_FILE_SIZE = 1_000_000
TIMER_PRESETS = [0, 10, 15, 20, 30, 45, 60]


@dataclass
class ParsedQuestion:
    text: str
    options: List[Tuple[str, bool]]


@dataclass
class ParsedTest:
    title: str
    questions: List[ParsedQuestion]


def load_env_file(env_path: Path) -> None:
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue

        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]

        os.environ.setdefault(key, value)


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS tests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                creator_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                source_name TEXT NOT NULL,
                question_count INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (creator_id) REFERENCES users(user_id)
            );

            CREATE TABLE IF NOT EXISTS questions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                test_id INTEGER NOT NULL,
                position INTEGER NOT NULL,
                text TEXT NOT NULL,
                FOREIGN KEY (test_id) REFERENCES tests(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS options (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                question_id INTEGER NOT NULL,
                position INTEGER NOT NULL,
                text TEXT NOT NULL,
                is_correct INTEGER NOT NULL CHECK (is_correct IN (0, 1)),
                FOREIGN KEY (question_id) REFERENCES questions(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                test_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                timer_seconds INTEGER NOT NULL,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                score INTEGER DEFAULT 0,
                total_questions INTEGER DEFAULT 0,
                correct_answers INTEGER DEFAULT 0,
                FOREIGN KEY (test_id) REFERENCES tests(id) ON DELETE CASCADE,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            );

            CREATE TABLE IF NOT EXISTS attempt_answers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                attempt_id INTEGER NOT NULL,
                question_id INTEGER NOT NULL,
                selected_option_id INTEGER,
                is_correct INTEGER NOT NULL CHECK (is_correct IN (0, 1)),
                response_seconds REAL,
                answered_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                timed_out INTEGER NOT NULL DEFAULT 0 CHECK (timed_out IN (0, 1)),
                FOREIGN KEY (attempt_id) REFERENCES attempts(id) ON DELETE CASCADE,
                FOREIGN KEY (question_id) REFERENCES questions(id) ON DELETE CASCADE,
                FOREIGN KEY (selected_option_id) REFERENCES options(id)
            );
            """
        )


def upsert_user(user_id: int, username: Optional[str], first_name: Optional[str]) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO users (user_id, username, first_name)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name
            """,
            (user_id, username, first_name),
        )


def parse_test_txt(content: str, fallback_title: str) -> ParsedTest:
    lines = [line.rstrip() for line in content.splitlines()]
    lines = [line for line in lines if line.strip()]

    if not lines:
        raise ValueError("Файл пустой.")

    title = fallback_title
    first = lines[0].strip()
    if first.startswith("#"):
        title = first.lstrip("# ").strip() or fallback_title
        lines = lines[1:]

    questions: List[ParsedQuestion] = []
    current_q: Optional[str] = None
    current_options: List[Tuple[str, bool]] = []

    def flush_question() -> None:
        nonlocal current_q, current_options
        if not current_q:
            return
        if len(current_options) < 2:
            raise ValueError(f"У вопроса '{current_q[:40]}...' меньше двух вариантов.")
        if not any(is_correct for _, is_correct in current_options):
            raise ValueError(f"У вопроса '{current_q[:40]}...' не отмечен правильный ответ.")
        questions.append(ParsedQuestion(text=current_q, options=current_options))
        current_q = None
        current_options = []

    q_pattern = re.compile(r"^(?:Q:|В:|Вопрос:|\d+[.)])\s*(.+)$", re.IGNORECASE)

    for raw in lines:
        line = raw.strip()
        q_match = q_pattern.match(line)
        if q_match:
            flush_question()
            current_q = q_match.group(1).strip()
            continue

        if line.startswith(("-", "*", "•", "+")):
            if current_q is None:
                raise ValueError("Вариант ответа найден до первого вопроса.")
            opt = line.lstrip("-*•+ ").strip()
            if not opt:
                continue
            is_correct = False
            if opt.endswith("*"):
                opt = opt[:-1].strip()
                is_correct = True
            if opt.lower().endswith("(+)"):
                opt = opt[:-3].strip()
                is_correct = True
            if line.startswith("+"):
                is_correct = True
            current_options.append((opt, is_correct))
            continue

        flush_question()
        current_q = line

    flush_question()

    if not questions:
        raise ValueError("Не удалось распознать вопросы. Проверь формат.")
    if len(questions) > 200:
        raise ValueError("Слишком много вопросов. Лимит: 200.")

    return ParsedTest(title=title[:120], questions=questions)


def save_test(creator_id: int, source_name: str, parsed: ParsedTest) -> int:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO tests (creator_id, title, source_name, question_count) VALUES (?, ?, ?, ?)",
            (creator_id, parsed.title, source_name, len(parsed.questions)),
        )
        test_id = cur.lastrowid
        for q_idx, q in enumerate(parsed.questions, start=1):
            cur.execute(
                "INSERT INTO questions (test_id, position, text) VALUES (?, ?, ?)",
                (test_id, q_idx, q.text),
            )
            question_id = cur.lastrowid
            for o_idx, (opt_text, is_correct) in enumerate(q.options, start=1):
                cur.execute(
                    "INSERT INTO options (question_id, position, text, is_correct) VALUES (?, ?, ?, ?)",
                    (question_id, o_idx, opt_text, int(is_correct)),
                )
        return int(test_id)


def fetch_tests(limit: int = 30) -> List[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT t.id, t.title, t.question_count
            FROM tests t
            ORDER BY t.created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()


def fetch_my_tests(user_id: int, limit: int = 30) -> List[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT id, title, question_count, created_at
            FROM tests
            WHERE creator_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()


def fetch_test_details(test_id: int) -> Optional[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT t.*, u.first_name as creator_name
            FROM tests t
            LEFT JOIN users u ON u.user_id = t.creator_id
            WHERE t.id = ?
            """,
            (test_id,),
        ).fetchone()


def fetch_questions_with_options(test_id: int) -> List[dict]:
    with get_conn() as conn:
        q_rows = conn.execute(
            "SELECT id, position, text FROM questions WHERE test_id = ? ORDER BY position",
            (test_id,),
        ).fetchall()
        questions = []
        for q in q_rows:
            opts = conn.execute(
                "SELECT id, position, text, is_correct FROM options WHERE question_id = ? ORDER BY position",
                (q["id"],),
            ).fetchall()
            questions.append(
                {
                    "id": q["id"],
                    "position": q["position"],
                    "text": q["text"],
                    "options": [dict(x) for x in opts],
                }
            )
        return questions


def create_attempt(test_id: int, user_id: int, timer_seconds: int) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO attempts (test_id, user_id, timer_seconds, started_at) VALUES (?, ?, ?, ?)",
            (test_id, user_id, timer_seconds, datetime.utcnow().isoformat()),
        )
        return int(cur.lastrowid)


def store_answer(
    attempt_id: int,
    question_id: int,
    selected_option_id: Optional[int],
    is_correct: bool,
    response_seconds: Optional[float],
    timed_out: bool = False,
) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO attempt_answers (attempt_id, question_id, selected_option_id, is_correct, response_seconds, timed_out)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (attempt_id, question_id, selected_option_id, int(is_correct), response_seconds, int(timed_out)),
        )


def finish_attempt(attempt_id: int) -> None:
    with get_conn() as conn:
        stats = conn.execute(
            "SELECT COUNT(*) as total, SUM(is_correct) as correct FROM attempt_answers WHERE attempt_id = ?",
            (attempt_id,),
        ).fetchone()
        total = int(stats["total"] or 0)
        correct = int(stats["correct"] or 0)
        score = int(round((correct / total) * 100)) if total else 0
        conn.execute(
            """
            UPDATE attempts
            SET completed_at = ?, score = ?, total_questions = ?, correct_answers = ?
            WHERE id = ?
            """,
            (datetime.utcnow().isoformat(), score, total, correct, attempt_id),
        )


def fetch_attempt_result(attempt_id: int) -> sqlite3.Row:
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT a.id, a.score, a.total_questions, a.correct_answers, a.started_at, a.completed_at,
                   t.title as test_title
            FROM attempts a
            JOIN tests t ON t.id = a.test_id
            WHERE a.id = ?
            """,
            (attempt_id,),
        ).fetchone()


def fetch_wrong_answers(attempt_id: int) -> List[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT q.position, q.text, ao.text as selected_text,
                   ac.text as correct_text, aa.timed_out
            FROM attempt_answers aa
            JOIN questions q ON q.id = aa.question_id
            LEFT JOIN options ao ON ao.id = aa.selected_option_id
            LEFT JOIN options ac ON ac.question_id = q.id AND ac.is_correct = 1
            WHERE aa.attempt_id = ? AND aa.is_correct = 0
            ORDER BY q.position
            """,
            (attempt_id,),
        ).fetchall()


def user_stats(user_id: int) -> sqlite3.Row:
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT COUNT(*) as attempts,
                   SUM(CASE WHEN completed_at IS NOT NULL THEN 1 ELSE 0 END) as completed,
                   AVG(CASE WHEN completed_at IS NOT NULL THEN score END) as avg_score,
                   MAX(score) as best_score
            FROM attempts
            WHERE user_id = ?
            """,
            (user_id,),
        ).fetchone()


def global_stats() -> sqlite3.Row:
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT (SELECT COUNT(*) FROM tests) as tests_total,
                   (SELECT COUNT(*) FROM attempts) as attempts_total,
                   (SELECT COUNT(*) FROM attempts WHERE completed_at IS NOT NULL) as completed_total,
                   (SELECT AVG(score) FROM attempts WHERE completed_at IS NOT NULL) as avg_score,
                   (SELECT COUNT(DISTINCT user_id) FROM attempts) as active_users
            """
        ).fetchone()


def leaderboard(limit: int = 5) -> List[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT u.first_name, u.username, AVG(a.score) as avg_score, COUNT(*) as cnt
            FROM attempts a
            JOIN users u ON u.user_id = a.user_id
            WHERE a.completed_at IS NOT NULL
            GROUP BY a.user_id
            ORDER BY avg_score DESC, cnt DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📚 Список тестов", callback_data="menu_tests")],
            [InlineKeyboardButton("➕ Создать тест из TXT", callback_data="menu_create")],
            [InlineKeyboardButton("🗂 Мои тесты", callback_data="menu_my_tests")],
            [InlineKeyboardButton("📊 Моя статистика", callback_data="menu_my_stats")],
            [InlineKeyboardButton("🌐 Общая статистика", callback_data="menu_global_stats")],
            [InlineKeyboardButton("ℹ️ Формат TXT", callback_data="menu_format")],
        ]
    )


def tests_keyboard(tests: List[sqlite3.Row]) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(f"▶️ {row['title']} ({row['question_count']} вопр.)", callback_data=f"open_test:{row['id']}")]
        for row in tests
    ]
    buttons.append([InlineKeyboardButton("⬅️ В меню", callback_data="go_menu")])
    return InlineKeyboardMarkup(buttons)


def timer_keyboard(test_id: int) -> InlineKeyboardMarkup:
    labels = {0: "Без таймера", 10: "10с", 15: "15с", 20: "20с", 30: "30с", 45: "45с", 60: "60с"}
    rows = [[InlineKeyboardButton(labels[val], callback_data=f"pick_timer:{test_id}:{val}")] for val in TIMER_PRESETS]
    rows.append([InlineKeyboardButton("⬅️ К тестам", callback_data="menu_tests")])
    return InlineKeyboardMarkup(rows)


def build_question_markup(session: dict, question: dict) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(opt["text"][:80], callback_data=f"answer:{session['attempt_id']}:{question['id']}:{opt['id']}")]
        for opt in question["options"]
    ]
    rows.append([InlineKeyboardButton("⛔ Завершить тест", callback_data=f"stop_attempt:{session['attempt_id']}")])
    return InlineKeyboardMarkup(rows)


def schedule_timer(context: CallbackContext, session: dict) -> None:
    for job in context.job_queue.get_jobs_by_name(session["job_name"]):
        job.schedule_removal()
    if session["timer_seconds"] > 0:
        context.job_queue.run_once(
            on_timeout,
            when=session["timer_seconds"],
            data={"user_id": session["user_id"], "attempt_id": session["attempt_id"]},
            name=session["job_name"],
        )


async def ask_next_question(context: CallbackContext, session: dict, edit_query=None) -> None:
    idx = session["index"]
    questions = session["questions"]
    if idx >= len(questions):
        await complete_attempt(context, session, edit_query)
        return

    q = questions[idx]
    timer = session["timer_seconds"]
    progress = f"{idx + 1}/{len(questions)}"
    timer_text = "∞" if timer == 0 else f"{timer} сек"
    text = f"Вопрос {progress}\nТаймер: {timer_text}\n\n{q['text']}"
    markup = build_question_markup(session, q)

    if edit_query:
        await edit_query.edit_message_text(text, reply_markup=markup)
    else:
        await context.bot.send_message(chat_id=session["chat_id"], text=text, reply_markup=markup)

    session["started_question_at"] = datetime.utcnow().timestamp()
    schedule_timer(context, session)


async def complete_attempt(context: CallbackContext, session: dict, query=None) -> None:
    for job in context.job_queue.get_jobs_by_name(session["job_name"]):
        job.schedule_removal()

    finish_attempt(session["attempt_id"])
    result = fetch_attempt_result(session["attempt_id"])
    wrong = fetch_wrong_answers(session["attempt_id"])

    started = datetime.fromisoformat(result["started_at"])
    completed = datetime.fromisoformat(result["completed_at"])
    total_seconds = int((completed - started).total_seconds())
    minutes, seconds = divmod(total_seconds, 60)

    text = (
        f"🏁 Тест завершен: {result['test_title']}\n\n"
        f"Результат: {result['score']}%\n"
        f"Правильных: {result['correct_answers']} из {result['total_questions']}\n"
        f"Время прохождения: {minutes}м {seconds}с\n"
        f"Ошибок: {len(wrong)}"
    )

    if wrong:
        text += "\n\nРазбор ошибок:"
        for row in wrong[:5]:
            selected = "время вышло" if row["timed_out"] else (row["selected_text"] or "нет ответа")
            text += (
                f"\n\n{row['position']}. {row['text'][:50]}"
                f"\nВаш ответ: {selected[:60]}"
                f"\nПравильно: {(row['correct_text'] or 'не указано')[:60]}"
            )
        if len(wrong) > 5:
            text += f"\n\n...и еще {len(wrong) - 5} ошибок."

    markup = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔁 Пройти снова", callback_data=f"open_test:{session['test_id']}")],
            [InlineKeyboardButton("📚 К списку тестов", callback_data="menu_tests")],
            [InlineKeyboardButton("🏠 Главное меню", callback_data="go_menu")],
        ]
    )

    if query:
        await query.edit_message_text(text, reply_markup=markup)
    else:
        await context.bot.send_message(chat_id=session["chat_id"], text=text, reply_markup=markup)

    context.application.user_data[session["user_id"]].pop("active_attempt", None)


async def on_timeout(context: CallbackContext) -> None:
    job_data = context.job.data
    user_id = job_data["user_id"]
    attempt_id = job_data["attempt_id"]

    user_ctx = context.application.user_data.get(user_id)
    if not user_ctx:
        return
    session = user_ctx.get("active_attempt")
    if not session or session["attempt_id"] != attempt_id:
        return

    idx = session["index"]
    if idx >= len(session["questions"]):
        return

    q = session["questions"][idx]
    store_answer(
        attempt_id=attempt_id,
        question_id=q["id"],
        selected_option_id=None,
        is_correct=False,
        response_seconds=session["timer_seconds"],
        timed_out=True,
    )
    session["index"] += 1
    await context.bot.send_message(chat_id=session["chat_id"], text="⏰ Время вышло. Следующий вопрос.")
    await ask_next_question(context, session, edit_query=None)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return
    upsert_user(user.id, user.username, user.first_name)
    text = (
        "Привет. Это QuizzBot.\n\n"
        "Загружай тесты из TXT, выбирай таймер на каждый вопрос и смотри подробную статистику."
    )
    await update.effective_message.reply_text(text, reply_markup=main_menu_keyboard())


async def show_format(update: Update, edit: bool = True) -> None:
    text = (
        "Формат TXT:\n\n"
        "# Название теста (необязательно)\n"
        "1) Вопрос?\n"
        "- Вариант 1\n"
        "+ Вариант 2\n"
        "- Вариант 3\n\n"
        "2) Следующий вопрос\n"
        "- Ответ A *\n"
        "- Ответ B\n\n"
        "Как пометить правильный вариант:\n"
        "• префикс '+'\n"
        "• суффикс '*'\n"
        "• суффикс '(+)'\n\n"
        "Минимум 2 варианта на вопрос, минимум 1 правильный."
    )
    if edit and update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=main_menu_keyboard())
    else:
        await update.effective_message.reply_text(text, reply_markup=main_menu_keyboard())


async def show_test_list(update: Update, mine: bool = False) -> None:
    user = update.effective_user
    if not user:
        return

    tests = fetch_my_tests(user.id) if mine else fetch_tests()
    title = "🗂 Ваши созданные тесты:" if mine else "📚 Доступные тесты:"

    if not tests:
        text = f"{title}\n\nПока пусто."
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ В меню", callback_data="go_menu")]])
    else:
        text = title
        markup = tests_keyboard(tests)

    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=markup)
    else:
        await update.effective_message.reply_text(text, reply_markup=markup)


async def handle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return
    await query.answer()
    user = query.from_user
    upsert_user(user.id, user.username, user.first_name)

    if query.data == "go_menu":
        await query.edit_message_text("Главное меню:", reply_markup=main_menu_keyboard())
        return

    if query.data == "menu_tests":
        await show_test_list(update, mine=False)
        return

    if query.data == "menu_my_tests":
        await show_test_list(update, mine=True)
        return

    if query.data == "menu_create":
        context.user_data["awaiting_txt"] = True
        await query.edit_message_text(
            "Отправьте TXT-файл с вопросами. Максимальный размер 1MB.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ В меню", callback_data="go_menu")]]),
        )
        return

    if query.data == "menu_my_stats":
        stats = user_stats(user.id)
        text = (
            "📊 Ваша статистика\n\n"
            f"Попыток: {int(stats['attempts'] or 0)}\n"
            f"Завершено: {int(stats['completed'] or 0)}\n"
            f"Средний балл: {float(stats['avg_score'] or 0):.1f}%\n"
            f"Лучший балл: {int(stats['best_score'] or 0)}%"
        )
        await query.edit_message_text(text, reply_markup=main_menu_keyboard())
        return

    if query.data == "menu_global_stats":
        gs = global_stats()
        leaders = leaderboard()
        top = "\n".join(
            f"{i}. {row['first_name'] or row['username'] or 'User'} — {float(row['avg_score']):.1f}% ({row['cnt']} попыток)"
            for i, row in enumerate(leaders, start=1)
        )
        if not top:
            top = "Пока нет данных"
        text = (
            "🌐 Общая статистика\n\n"
            f"Тестов создано: {int(gs['tests_total'] or 0)}\n"
            f"Всего попыток: {int(gs['attempts_total'] or 0)}\n"
            f"Завершенных: {int(gs['completed_total'] or 0)}\n"
            f"Средний результат: {float(gs['avg_score'] or 0):.1f}%\n"
            f"Активных пользователей: {int(gs['active_users'] or 0)}\n\n"
            f"Топ участников:\n{top}"
        )
        await query.edit_message_text(text, reply_markup=main_menu_keyboard())
        return

    if query.data == "menu_format":
        await show_format(update, edit=True)


async def handle_test_open(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return
    await query.answer()
    _, test_id_raw = query.data.split(":")
    test_id = int(test_id_raw)
    test = fetch_test_details(test_id)
    if not test:
        await query.edit_message_text("Тест не найден.", reply_markup=main_menu_keyboard())
        return

    text = (
        f"🧩 {test['title']}\n"
        f"Вопросов: {test['question_count']}\n"
        f"Автор: {test['creator_name'] or 'Unknown'}\n\n"
        "Выберите таймер на каждый вопрос:"
    )
    await query.edit_message_text(text, reply_markup=timer_keyboard(test_id))


async def start_attempt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return
    await query.answer()

    _, test_raw, timer_raw = query.data.split(":")
    test_id = int(test_raw)
    timer_seconds = int(timer_raw)
    user = query.from_user
    chat = query.message.chat if query.message else None
    if not chat:
        return

    upsert_user(user.id, user.username, user.first_name)
    questions = fetch_questions_with_options(test_id)
    if not questions:
        await query.edit_message_text("В тесте нет вопросов.", reply_markup=main_menu_keyboard())
        return

    attempt_id = create_attempt(test_id=test_id, user_id=user.id, timer_seconds=timer_seconds)
    context.user_data["active_attempt"] = {
        "attempt_id": attempt_id,
        "test_id": test_id,
        "user_id": user.id,
        "chat_id": chat.id,
        "questions": questions,
        "index": 0,
        "started_question_at": datetime.utcnow().timestamp(),
        "timer_seconds": timer_seconds,
        "job_name": f"attempt:{attempt_id}",
    }
    await ask_next_question(context, context.user_data["active_attempt"], edit_query=query)


async def handle_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return

    _, attempt_raw, question_raw, option_raw = query.data.split(":")
    attempt_id = int(attempt_raw)
    question_id = int(question_raw)
    option_id = int(option_raw)

    session = context.user_data.get("active_attempt")
    if not session or session["attempt_id"] != attempt_id:
        await query.answer("Эта попытка уже завершена.", show_alert=True)
        return

    idx = session["index"]
    if idx >= len(session["questions"]):
        await query.answer("Тест уже завершен.", show_alert=True)
        return

    current_q = session["questions"][idx]
    if current_q["id"] != question_id:
        await query.answer("Этот вопрос уже закрыт.", show_alert=True)
        return

    selected = next((o for o in current_q["options"] if o["id"] == option_id), None)
    if not selected:
        await query.answer("Вариант не найден.", show_alert=True)
        return

    for job in context.job_queue.get_jobs_by_name(session["job_name"]):
        job.schedule_removal()

    is_correct = bool(selected["is_correct"])
    response_seconds = round(datetime.utcnow().timestamp() - session["started_question_at"], 2)
    store_answer(
        attempt_id=attempt_id,
        question_id=question_id,
        selected_option_id=option_id,
        is_correct=is_correct,
        response_seconds=response_seconds,
        timed_out=False,
    )
    session["index"] += 1
    await query.answer("✅ Верно" if is_correct else "❌ Неверно")
    await ask_next_question(context, session, edit_query=query)


async def handle_stop_attempt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return
    await query.answer()

    _, attempt_raw = query.data.split(":")
    attempt_id = int(attempt_raw)
    session = context.user_data.get("active_attempt")
    if not session or session["attempt_id"] != attempt_id:
        await query.answer("Попытка уже закрыта.", show_alert=True)
        return

    await complete_attempt(context, session, query=query)


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return
    if not context.user_data.get("awaiting_txt"):
        return

    doc = message.document
    if not doc:
        await message.reply_text("Нужен именно TXT-документ.")
        return
    if not (doc.file_name or "").lower().endswith(".txt"):
        await message.reply_text("Поддерживаются только .txt файлы.")
        return
    if doc.file_size and doc.file_size > MAX_FILE_SIZE:
        await message.reply_text("Файл слишком большой. Лимит 1MB.")
        return

    telegram_file = await doc.get_file()
    content_bytes = await telegram_file.download_as_bytearray()
    try:
        content = bytes(content_bytes).decode("utf-8")
    except UnicodeDecodeError:
        await message.reply_text("Файл должен быть в UTF-8.")
        return

    fallback_title = Path(doc.file_name).stem
    try:
        parsed = parse_test_txt(content, fallback_title=fallback_title)
    except ValueError as exc:
        await message.reply_text(
            f"Не удалось распознать тест:\n{exc}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("ℹ️ Показать формат", callback_data="menu_format")]]),
        )
        return

    test_id = save_test(creator_id=user.id, source_name=doc.file_name, parsed=parsed)
    context.user_data["awaiting_txt"] = False
    await message.reply_text(
        f"✅ Тест '{parsed.title}' создан.\nВопросов: {len(parsed.questions)}",
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("▶️ Начать тест", callback_data=f"open_test:{test_id}")],
                [InlineKeyboardButton("📚 Все тесты", callback_data="menu_tests")],
                [InlineKeyboardButton("🏠 Главное меню", callback_data="go_menu")],
            ]
        ),
    )


def build_app(token: str) -> Application:
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(
        CallbackQueryHandler(
            handle_menu,
            pattern=r"^(go_menu|menu_tests|menu_create|menu_my_stats|menu_global_stats|menu_my_tests|menu_format)$",
        )
    )
    app.add_handler(CallbackQueryHandler(handle_test_open, pattern=r"^open_test:\d+$"))
    app.add_handler(CallbackQueryHandler(start_attempt, pattern=r"^pick_timer:\d+:\d+$"))
    app.add_handler(CallbackQueryHandler(handle_answer, pattern=r"^answer:\d+:\d+:\d+$"))
    app.add_handler(CallbackQueryHandler(handle_stop_attempt, pattern=r"^stop_attempt:\d+$"))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    return app


def main() -> None:
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
        level=logging.INFO,
    )
    load_env_file(Path(__file__).with_name(".env"))
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("Укажите BOT_TOKEN в переменных окружения или в файле .env.")

    init_db()
    app = build_app(token)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
