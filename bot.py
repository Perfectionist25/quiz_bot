import logging
import os
import random
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
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
PAGE_SIZE = 10
BASE_DIR = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(BASE_DIR / "templates"))
STATIC_DIR = BASE_DIR / "static"
WEB_HOST = os.getenv("WEB_HOST", "0.0.0.0")
WEB_PORT = int(os.getenv("WEB_PORT", "8000"))
ADMIN_COOKIE_NAME = "quiz_admin"
USER_COOKIE_NAME = "quiz_user_id"

web_app = FastAPI(title="QuizzBot Web")
web_app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


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


def create_web_user() -> int:
    name = f"Гость{random.randint(1000, 9999)}"
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO users (username, first_name) VALUES (?, ?)",
            (None, name),
        )
        return int(cur.lastrowid)


def get_web_user(request: Request):
    raw_user_id = request.cookies.get(USER_COOKIE_NAME)
    if raw_user_id and raw_user_id.isdigit():
        with get_conn() as conn:
            user = conn.execute(
                "SELECT user_id, username, first_name FROM users WHERE user_id = ?",
                (int(raw_user_id),),
            ).fetchone()
            if user:
                return user, None
    user_id = create_web_user()
    with get_conn() as conn:
        user = conn.execute(
            "SELECT user_id, username, first_name FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    return user, str(user_id)


def render_template(request: Request, template: str, context: dict):
    user, cookie_value = get_web_user(request)
    tpl = TEMPLATES.TemplateResponse(template, {**base_context(request), **context})
    if cookie_value:
        tpl.set_cookie(USER_COOKIE_NAME, cookie_value, max_age=60 * 60 * 24 * 365)
    return tpl, user


def create_attempt_record(test_id: int, user_id: int, timer_seconds: int) -> int:
    return create_attempt(test_id=test_id, user_id=user_id, timer_seconds=timer_seconds)


def fetch_attempt(attempt_id: int):
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM attempts WHERE id = ?",
            (attempt_id,),
        ).fetchone()


def answered_count(attempt_id: int) -> int:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as count FROM attempt_answers WHERE attempt_id = ?",
            (attempt_id,),
        ).fetchone()
        return int(row["count"] or 0)


def current_question_state(attempt_id: int):
    attempt = fetch_attempt(attempt_id)
    if not attempt:
        return None
    questions = fetch_questions_with_options(attempt["test_id"])
    index = answered_count(attempt_id)
    if index >= len(questions):
        return None
    question = questions[index]
    ensure_options_shuffled(question)
    return {
        "attempt": attempt,
        "question": question,
        "index": index,
        "total": len(questions),
    }


def check_user_owns_attempt(user_id: int, attempt) -> bool:
    return attempt is not None and int(attempt["user_id"]) == user_id


def is_admin_verified(request: Request) -> bool:
    return request.cookies.get(ADMIN_COOKIE_NAME) == "1"


def base_context(request: Request, message: Optional[str] = None) -> dict:
    return {
        "request": request,
        "message": message,
        "menu": [
            {"title": "Главная", "href": "/"},
            {"title": "Список тестов", "href": "/tests"},
            {"title": "Мои тесты", "href": "/my-tests"},
            {"title": "Моя статистика", "href": "/stats"},
            {"title": "Общая статистика", "href": "/global-stats"},
            {"title": "Формат TXT", "href": "/format"},
            {"title": "Загрузить тест", "href": "/upload-test"},
        ],
    }


@web_app.on_event("startup")
def startup_event():
    load_env_file(BASE_DIR / ".env")
    init_db()


@web_app.get("/favicon.ico")
def favicon():
    icon_path = STATIC_DIR / "favicon.svg"
    if icon_path.exists():
        return FileResponse(icon_path, media_type="image/svg+xml")
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)


@web_app.get("/", response_class=HTMLResponse)
def index(request: Request):
    # show recent tests and summary stats on the homepage
    recent = fetch_tests(limit=6)
    stats = global_stats()
    response, _ = render_template(
        request,
        "index.html",
        {"recent_tests": recent, "stats": stats},
    )
    return response


@web_app.get("/format", response_class=HTMLResponse)
def show_format(request: Request):
    response, _ = render_template(request, "format.html", {})
    return response


@web_app.get("/tests", response_class=HTMLResponse)
def list_tests(request: Request):
    response, _ = render_template(
        request,
        "tests.html",
        {"title": "Список тестов", "tests": fetch_tests(), "mine": False},
    )
    return response


@web_app.get("/my-tests", response_class=HTMLResponse)
def my_tests(request: Request):
    user, cookie_value = get_web_user(request)
    tpl = TEMPLATES.TemplateResponse(
        "tests.html",
        {
            **base_context(request),
            "title": "Мои тесты",
            "tests": fetch_my_tests(int(user["user_id"] or 0)),
            "mine": True,
            "user": user,
        },
    )
    if cookie_value:
        tpl.set_cookie(USER_COOKIE_NAME, cookie_value, max_age=60 * 60 * 24 * 365)
    return tpl


@web_app.get("/upload-test", response_class=HTMLResponse)
def upload_test_page(request: Request):
    response, _ = render_template(request, "upload.html", {"title": "Загрузить тест"})
    return response


@web_app.post("/upload-test", response_class=HTMLResponse)
async def upload_test(request: Request, file: UploadFile = File(...)):
    user, cookie_value = get_web_user(request)
    if not file.filename.lower().endswith(".txt"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Только TXT файлы поддерживаются.")
    contents = await file.read()
    if len(contents) > MAX_FILE_SIZE:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Файл слишком большой. Лимит 1MB.")
    try:
        text = contents.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Файл должен быть в UTF-8.")
    fallback_title = Path(file.filename).stem
    try:
        parsed = parse_test_txt(text, fallback_title=fallback_title)
    except ValueError as exc:
        tpl = TEMPLATES.TemplateResponse(
            "upload.html",
            {
                **base_context(request, str(exc)),
                "title": "Загрузить тест",
            },
        )
        return tpl
    test_id = save_test(int(user["user_id"] or 0), file.filename, parsed)
    response = RedirectResponse(url=f"/my-tests/{test_id}", status_code=status.HTTP_303_SEE_OTHER)
    if cookie_value:
        response.set_cookie(USER_COOKIE_NAME, cookie_value, max_age=60 * 60 * 24 * 365)
    return response


@web_app.get("/test/{test_id}", response_class=HTMLResponse)
def test_detail(request: Request, test_id: int):
    test = fetch_test_details(test_id)
    if not test:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Тест не найден.")
    response, _ = render_template(
        request,
        "test_detail.html",
        {
            "title": test["title"],
            "test": test,
            "timer_options": TIMER_PRESETS,
        },
    )
    return response


@web_app.post("/test/{test_id}/start")
def start_attempt_web(request: Request, test_id: int, timer_seconds: int = Form(0), shuffle: Optional[str] = Form(None)):
    user, cookie_value = get_web_user(request)
    test = fetch_test_details(test_id)
    if not test:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Тест не найден.")
    questions = fetch_questions_with_options(test_id)
    if not questions:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="В тесте нет вопросов.")
    if shuffle == "shuffle":
        random.shuffle(questions)
    attempt_id = create_attempt_record(test_id=test_id, user_id=int(user["user_id"] or 0), timer_seconds=timer_seconds)
    response = RedirectResponse(url=f"/attempt/{attempt_id}", status_code=status.HTTP_303_SEE_OTHER)
    if cookie_value:
        response.set_cookie(USER_COOKIE_NAME, cookie_value, max_age=60 * 60 * 24 * 365)
    return response


@web_app.get("/attempt/{attempt_id}", response_class=HTMLResponse)
def show_attempt(request: Request, attempt_id: int):
    user, cookie_value = get_web_user(request)
    attempt = fetch_attempt(attempt_id)
    if not attempt or not check_user_owns_attempt(int(user["user_id"] or 0), attempt):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Попытка не найдена.")
    if attempt["completed_at"] is not None:
        return RedirectResponse(url=f"/results/{attempt_id}", status_code=status.HTTP_303_SEE_OTHER)
    current = current_question_state(attempt_id)
    if current is None:
        finish_attempt(attempt_id)
        return RedirectResponse(url=f"/results/{attempt_id}", status_code=status.HTTP_303_SEE_OTHER)
    question = current["question"]
    question_start = datetime.now(timezone.utc).timestamp()
    tpl = TEMPLATES.TemplateResponse(
        "attempt.html",
        {
            **base_context(request),
            "title": f"Вопрос {current['index'] + 1}",
            "attempt": attempt,
            "question": question,
            "index": current["index"],
            "total": current["total"],
            "question_start": int(question_start),
            "timer_seconds": int(attempt["timer_seconds"]),
            "user": user,
        },
    )
    if cookie_value:
        tpl.set_cookie(USER_COOKIE_NAME, cookie_value, max_age=60 * 60 * 24 * 365)
    return tpl
    


@web_app.post("/attempt/{attempt_id}/answer")
def submit_answer(request: Request, attempt_id: int, option_id: int = Form(...), question_start: int = Form(...), timer_seconds: int = Form(...)):
    user, cookie_value = get_web_user(request)
    attempt = fetch_attempt(attempt_id)
    if not attempt or not check_user_owns_attempt(int(user["user_id"] or 0), attempt):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Попытка не найдена.")
    if attempt["completed_at"] is not None:
        return RedirectResponse(url=f"/results/{attempt_id}", status_code=status.HTTP_303_SEE_OTHER)
    elapsed = max(0, datetime.now(timezone.utc).timestamp() - question_start)
    timed_out = timer_seconds > 0 and elapsed > timer_seconds
    question_state = current_question_state(attempt_id)
    if question_state is None:
        finish_attempt(attempt_id)
        return RedirectResponse(url=f"/results/{attempt_id}", status_code=status.HTTP_303_SEE_OTHER)
    question = question_state["question"]
    if timed_out:
        store_answer(
            attempt_id=attempt_id,
            question_id=question["id"],
            selected_option_id=None,
            is_correct=False,
            response_seconds=float(timer_seconds),
            timed_out=True,
        )
    else:
        selected = next((opt for opt in question["options"] if opt["id"] == option_id), None)
        if selected is None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Вариант не найден.")
        store_answer(
            attempt_id=attempt_id,
            question_id=question["id"],
            selected_option_id=option_id,
            is_correct=bool(selected["is_correct"]),
            response_seconds=round(elapsed, 2),
            timed_out=False,
        )
    response = RedirectResponse(url=f"/attempt/{attempt_id}", status_code=status.HTTP_303_SEE_OTHER)
    if cookie_value:
        response.set_cookie(USER_COOKIE_NAME, cookie_value, max_age=60 * 60 * 24 * 365)
    return response


@web_app.post("/attempt/{attempt_id}/timeout")
def submit_timeout(request: Request, attempt_id: int, question_start: int = Form(...), timer_seconds: int = Form(...)):
    user, cookie_value = get_web_user(request)
    attempt = fetch_attempt(attempt_id)
    if not attempt or not check_user_owns_attempt(int(user["user_id"] or 0), attempt):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Попытка не найдена.")
    question_state = current_question_state(attempt_id)
    if question_state is None:
        finish_attempt(attempt_id)
        return RedirectResponse(url=f"/results/{attempt_id}", status_code=status.HTTP_303_SEE_OTHER)
    question = question_state["question"]
    elapsed = max(0, datetime.now(timezone.utc).timestamp() - question_start)
    store_answer(
        attempt_id=attempt_id,
        question_id=question["id"],
        selected_option_id=None,
        is_correct=False,
        response_seconds=float(min(elapsed, timer_seconds or elapsed)),
        timed_out=True,
    )
    response = RedirectResponse(url=f"/attempt/{attempt_id}", status_code=status.HTTP_303_SEE_OTHER)
    if cookie_value:
        response.set_cookie(USER_COOKIE_NAME, cookie_value, max_age=60 * 60 * 24 * 365)
    return response


@web_app.post("/attempt/{attempt_id}/stop")
def stop_attempt(request: Request, attempt_id: int):
    user, cookie_value = get_web_user(request)
    attempt = fetch_attempt(attempt_id)
    if not attempt or not check_user_owns_attempt(int(user["user_id"] or 0), attempt):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Попытка не найдена.")
    finish_attempt(attempt_id)
    response = RedirectResponse(url=f"/results/{attempt_id}", status_code=status.HTTP_303_SEE_OTHER)
    if cookie_value:
        response.set_cookie(USER_COOKIE_NAME, cookie_value, max_age=60 * 60 * 24 * 365)
    return response


@web_app.get("/results/{attempt_id}", response_class=HTMLResponse)
def view_result(request: Request, attempt_id: int, page: int = 1):
    user, cookie_value = get_web_user(request)
    attempt = fetch_attempt(attempt_id)
    if not attempt or not check_user_owns_attempt(int(user["user_id"] or 0), attempt):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Попытка не найдена.")
    if attempt["completed_at"] is None:
        finish_attempt(attempt_id)
        attempt = fetch_attempt(attempt_id)
    result = fetch_attempt_result(attempt_id)
    answers = fetch_attempt_answers_details(attempt_id)
    pages = (len(answers) + PAGE_SIZE - 1) // PAGE_SIZE
    page = max(1, min(page, pages or 1))
    start = (page - 1) * PAGE_SIZE
    page_items = answers[start:start + PAGE_SIZE]
    tpl = TEMPLATES.TemplateResponse(
        "results.html",
        {
            **base_context(request),
            "title": "Результат теста",
            "result": result,
            "answers": page_items,
            "page": page,
            "pages": pages,
            "section": "all",
            "attempt_id": attempt_id,
        },
    )
    if cookie_value:
        tpl.set_cookie(USER_COOKIE_NAME, cookie_value, max_age=60 * 60 * 24 * 365)
    return tpl


@web_app.get("/results/{attempt_id}/correct", response_class=HTMLResponse)
def view_correct(request: Request, attempt_id: int, page: int = 1):
    user, cookie_value = get_web_user(request)
    attempt = fetch_attempt(attempt_id)
    if not attempt or not check_user_owns_attempt(int(user["user_id"] or 0), attempt):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Попытка не найдена.")
    rows = fetch_correct_answers(attempt_id)
    pages = (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE
    page = max(1, min(page, pages or 1))
    start = (page - 1) * PAGE_SIZE
    page_items = rows[start:start + PAGE_SIZE]
    tpl = TEMPLATES.TemplateResponse(
        "results.html",
        {
            **base_context(request),
            "title": "Правильные ответы",
            "answers": page_items,
            "page": page,
            "pages": pages,
            "section": "correct",
            "attempt_id": attempt_id,
        },
    )
    if cookie_value:
        tpl.set_cookie(USER_COOKIE_NAME, cookie_value, max_age=60 * 60 * 24 * 365)
    return tpl


@web_app.get("/results/{attempt_id}/wrong", response_class=HTMLResponse)
def view_wrong(request: Request, attempt_id: int, page: int = 1):
    user, cookie_value = get_web_user(request)
    attempt = fetch_attempt(attempt_id)
    if not attempt or not check_user_owns_attempt(int(user["user_id"] or 0), attempt):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Попытка не найдена.")
    rows = fetch_wrong_answers(attempt_id)
    pages = (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE
    page = max(1, min(page, pages or 1))
    start = (page - 1) * PAGE_SIZE
    page_items = rows[start:start + PAGE_SIZE]
    tpl = TEMPLATES.TemplateResponse(
        "results.html",
        {
            **base_context(request),
            "title": "Ошибки",
            "answers": page_items,
            "page": page,
            "pages": pages,
            "section": "wrong",
            "attempt_id": attempt_id,
        },
    )
    if cookie_value:
        tpl.set_cookie(USER_COOKIE_NAME, cookie_value, max_age=60 * 60 * 24 * 365)
    return tpl


@web_app.get("/stats", response_class=HTMLResponse)
def my_stats(request: Request):
    user, cookie_value = get_web_user(request)
    stats = user_stats(int(user["user_id"] or 0))
    tpl = TEMPLATES.TemplateResponse(
        "stats.html",
        {
            **base_context(request),
            "title": "Моя статистика",
            "stats": stats,
            "mode": "personal",
            "user": user,
        },
    )
    if cookie_value:
        tpl.set_cookie(USER_COOKIE_NAME, cookie_value, max_age=60 * 60 * 24 * 365)
    return tpl


@web_app.get("/global-stats", response_class=HTMLResponse)
def global_stats_page(request: Request):
    user, cookie_value = get_web_user(request)
    gs = global_stats()
    top = leaderboard()
    tpl = TEMPLATES.TemplateResponse(
        "stats.html",
        {
            **base_context(request),
            "title": "Общая статистика",
            "stats": gs,
            "top": top,
            "mode": "global",
            "user": user,
        },
    )
    if cookie_value:
        tpl.set_cookie(USER_COOKIE_NAME, cookie_value, max_age=60 * 60 * 24 * 365)
    return tpl


@web_app.get("/admin", response_class=HTMLResponse)
def admin_login(request: Request, error: Optional[str] = None):
    user, cookie_value = get_web_user(request)
    tpl = TEMPLATES.TemplateResponse(
        "admin.html",
        {**base_context(request), "title": "Админ-доступ", "error": error},
    )
    if cookie_value:
        tpl.set_cookie(USER_COOKIE_NAME, cookie_value, max_age=60 * 60 * 24 * 365)
    return tpl


@web_app.post("/admin", response_class=HTMLResponse)
def admin_auth(request: Request, password: str = Form(...)):
    user, cookie_value = get_web_user(request)
    if password != get_admin_password():
        tpl = TEMPLATES.TemplateResponse(
            "admin.html",
            {**base_context(request), "title": "Админ-доступ", "error": "Неверный пароль."},
        )
        if cookie_value:
            tpl.set_cookie(USER_COOKIE_NAME, cookie_value, max_age=60 * 60 * 24 * 365)
        return tpl
    response = RedirectResponse(url="/admin/stats", status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(ADMIN_COOKIE_NAME, "1", max_age=60 * 30)
    if cookie_value:
        response.set_cookie(USER_COOKIE_NAME, cookie_value, max_age=60 * 60 * 24 * 365)
    return response


@web_app.get("/admin/stats", response_class=HTMLResponse)
def admin_stats(request: Request):
    if not is_admin_verified(request):
        return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)
    user, cookie_value = get_web_user(request)
    tpl = TEMPLATES.TemplateResponse(
        "admin.html",
        {
            **base_context(request),
            "title": "Полная статистика",
            "admin": True,
            "stats": global_stats(),
            "users": fetch_all_users(limit=30),
            "active_attempts": fetch_active_attempts(limit=30),
            "user": user,
        },
    )
    if cookie_value:
        tpl.set_cookie(USER_COOKIE_NAME, cookie_value, max_age=60 * 60 * 24 * 365)
    return tpl


@web_app.get("/my-tests/{test_id}", response_class=HTMLResponse)
def my_test_detail(request: Request, test_id: int):
    response, user = render_template(request, "my_test_detail.html", {})
    test = fetch_test_details(test_id)
    if not test or int(test["creator_id"]) != int(user["user_id"] or 0):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Тест не найден или доступ запрещен.")
    tpl = TEMPLATES.TemplateResponse(
        "my_test_detail.html",
        {**base_context(request), "title": test["title"], "test": test},
    )
    return tpl


@web_app.get("/my-tests/{test_id}/rename", response_class=HTMLResponse)
def rename_test_page(request: Request, test_id: int):
    response, user = render_template(request, "rename_test.html", {})
    test = fetch_test_details(test_id)
    if not test or int(test["creator_id"]) != int(user["user_id"] or 0):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Тест не найден или доступ запрещен.")
    tpl = TEMPLATES.TemplateResponse(
        "rename_test.html",
        {**base_context(request), "title": "Переименовать тест", "test": test},
    )
    return tpl


@web_app.post("/my-tests/{test_id}/rename")
def rename_test_submit(request: Request, test_id: int, new_title: str = Form(...)):
    user, cookie_value = get_web_user(request)
    if not rename_test(test_id, int(user["user_id"] or 0), new_title):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Не удалось переименовать тест.")
    response = RedirectResponse(url=f"/my-tests/{test_id}", status_code=status.HTTP_303_SEE_OTHER)
    if cookie_value:
        response.set_cookie(USER_COOKIE_NAME, cookie_value, max_age=60 * 60 * 24 * 365)
    return response


@web_app.get("/my-tests/{test_id}/append", response_class=HTMLResponse)
def append_test_page(request: Request, test_id: int):
    response, user = render_template(request, "append_test.html", {})
    test = fetch_test_details(test_id)
    if not test or int(test["creator_id"]) != int(user["user_id"] or 0):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Тест не найден или доступ запрещен.")
    tpl = TEMPLATES.TemplateResponse(
        "append_test.html",
        {**base_context(request), "title": "Добавить вопросы", "test": test},
    )
    return tpl


@web_app.post("/my-tests/{test_id}/append", response_class=HTMLResponse)
async def append_test_upload(request: Request, test_id: int, file: UploadFile = File(...)):
    user, cookie_value = get_web_user(request)
    test = fetch_test_details(test_id)
    if not test or int(test["creator_id"]) != int(user["user_id"] or 0):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Тест не найден или доступ запрещен.")
    if not file.filename.lower().endswith(".txt"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Только TXT файлы поддерживаются.")
    content = await file.read()
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Файл должен быть в UTF-8.")
    parsed = parse_test_txt(text, fallback_title=Path(file.filename).stem)
    if not append_questions_to_test(test_id=test_id, parsed=parsed):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Не удалось добавить вопросы.")
    response = RedirectResponse(url=f"/my-tests/{test_id}", status_code=status.HTTP_303_SEE_OTHER)
    if cookie_value:
        response.set_cookie(USER_COOKIE_NAME, cookie_value, max_age=60 * 60 * 24 * 365)
    return response


@web_app.post("/my-tests/{test_id}/delete")
def delete_test_submit(request: Request, test_id: int):
    user, cookie_value = get_web_user(request)
    if not delete_test(test_id, int(user["user_id"] or 0)):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Не удалось удалить тест.")
    response = RedirectResponse(url="/my-tests", status_code=status.HTTP_303_SEE_OTHER)
    if cookie_value:
        response.set_cookie(USER_COOKIE_NAME, cookie_value, max_age=60 * 60 * 24 * 365)
    return response


def run_web_server() -> None:
    uvicorn.run(web_app, host=WEB_HOST, port=WEB_PORT, log_level="info")





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
    if len(questions) > 250:
        raise ValueError("Слишком много вопросов. Лимит: 250.")

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


def fetch_correct_answers(attempt_id: int) -> List[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT q.position, q.text, ao.text as selected_text,
                   ac.text as correct_text, aa.timed_out
            FROM attempt_answers aa
            JOIN questions q ON q.id = aa.question_id
            LEFT JOIN options ao ON ao.id = aa.selected_option_id
            LEFT JOIN options ac ON ac.question_id = q.id AND ac.is_correct = 1
            WHERE aa.attempt_id = ? AND aa.is_correct = 1
            ORDER BY q.position
            """,
            (attempt_id,),
        ).fetchall()


def fetch_attempt_answers_details(attempt_id: int) -> List[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT q.position, q.text as question_text, ao.text as selected_text,
                   ac.text as correct_text, aa.is_correct, aa.timed_out, aa.response_seconds
            FROM attempt_answers aa
            JOIN questions q ON q.id = aa.question_id
            LEFT JOIN options ao ON ao.id = aa.selected_option_id
            LEFT JOIN options ac ON ac.question_id = q.id AND ac.is_correct = 1
            WHERE aa.attempt_id = ?
            ORDER BY q.position
            """,
            (attempt_id,),
        ).fetchall()


def _paginate(items: List, page: int, per_page: int) -> Tuple[List, int]:
    total = len(items)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))
    start = (page - 1) * per_page
    end = start + per_page
    return items[start:end], total_pages


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


def fetch_all_users(limit: int = 50) -> List[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT u.user_id, u.username, u.first_name, u.created_at,
                   COUNT(a.id) as attempts,
                   SUM(CASE WHEN a.completed_at IS NOT NULL THEN 1 ELSE 0 END) as completed,
                   AVG(CASE WHEN a.completed_at IS NOT NULL THEN a.score END) as avg_score,
                   MAX(a.score) as best_score
            FROM users u
            LEFT JOIN attempts a ON a.user_id = u.user_id
            GROUP BY u.user_id
            ORDER BY attempts DESC, avg_score DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()


    def fetch_active_attempts(limit: int = 50) -> List[sqlite3.Row]:
        with get_conn() as conn:
            return conn.execute(
                """
                SELECT a.id, a.test_id, a.user_id, a.started_at, t.title as test_title, u.first_name as user_name
                FROM attempts a
                LEFT JOIN tests t ON t.id = a.test_id
                LEFT JOIN users u ON u.user_id = a.user_id
                WHERE a.completed_at IS NULL
                ORDER BY a.started_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()


def delete_test(test_id: int, user_id: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM tests WHERE id = ? AND creator_id = ?",
            (test_id, user_id),
        )
        return cur.rowcount > 0


def rename_test(test_id: int, user_id: int, new_title: str) -> bool:
    new_title = new_title.strip()[:120]
    if not new_title:
        return False
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE tests SET title = ? WHERE id = ? AND creator_id = ?",
            (new_title, test_id, user_id),
        )
        return cur.rowcount > 0


def append_questions_to_test(test_id: int, parsed: ParsedTest) -> bool:
    if not parsed.questions:
        return False
    with get_conn() as conn:
        cur = conn.cursor()
        existing_count = conn.execute(
            "SELECT COUNT(*) as count FROM questions WHERE test_id = ?",
            (test_id,),
        ).fetchone()["count"]
        next_position = existing_count + 1
        for q in parsed.questions:
            cur.execute(
                "INSERT INTO questions (test_id, position, text) VALUES (?, ?, ?)",
                (test_id, next_position, q.text),
            )
            question_id = cur.lastrowid
            for o_idx, (opt_text, is_correct) in enumerate(q.options, start=1):
                cur.execute(
                    "INSERT INTO options (question_id, position, text, is_correct) VALUES (?, ?, ?, ?)",
                    (question_id, o_idx, opt_text, int(is_correct)),
                )
            next_position += 1
        cur.execute(
            "UPDATE tests SET question_count = question_count + ? WHERE id = ?",
            (len(parsed.questions), test_id),
        )
        return True


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


def reset_user_states(user_data: dict) -> None:
    for key in (
        "awaiting_txt",
        "awaiting_rename_test_id",
        "awaiting_append_test_id",
        "awaiting_admin_password",
    ):
        user_data.pop(key, None)


def main_menu_keyboard() -> InlineKeyboardMarkup:
    # compute site URL: prefer explicit WEB_URL env, fallback to host:port
    site_url = os.getenv("WEB_URL")
    if site_url:
        site_url = site_url.rstrip("/") + "/tests"
    else:
        host = os.getenv("WEB_HOST", "127.0.0.1")
        port = os.getenv("WEB_PORT", "8000")
        site_url = f"http://{host}:{port}/tests"

    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🌐 Перейти на сайт", url=site_url)],
            [InlineKeyboardButton("📚 Список тестов", callback_data="menu_tests")],
            [InlineKeyboardButton("➕ Создать тест из TXT", callback_data="menu_create")],
            [InlineKeyboardButton("🗂 Мои тесты", callback_data="menu_my_tests")],
            [InlineKeyboardButton("📊 Моя статистика", callback_data="menu_my_stats")],
            [InlineKeyboardButton("🌐 Общая статистика", callback_data="menu_global_stats")],
            [InlineKeyboardButton("🔐 Все пользователи", callback_data="menu_admin_stats")],
            [InlineKeyboardButton("ℹ️ Как загрузить TXT", callback_data="menu_format")],
        ]
    )


def tests_keyboard(tests: List[sqlite3.Row], mine: bool = False) -> InlineKeyboardMarkup:
    buttons = []
    for row in tests:
        callback = f"open_my_test:{row['id']}" if mine else f"open_test:{row['id']}"
        buttons.append([InlineKeyboardButton(f"▶️ {row['title']} ({row['question_count']} вопр.)", callback_data=callback)])
    buttons.append([InlineKeyboardButton("⬅️ В меню", callback_data="go_menu")])
    return InlineKeyboardMarkup(buttons)


def timer_keyboard(test_id: int) -> InlineKeyboardMarkup:
    labels = {0: "Без таймера", 10: "10с", 15: "15с", 20: "20с", 30: "30с", 45: "45с", 60: "60с"}
    rows = []
    for val in TIMER_PRESETS:
        rows.append(
            [
                InlineKeyboardButton(labels[val], callback_data=f"pick_timer:{test_id}:{val}:r"),
                InlineKeyboardButton(f"{labels[val]} 🔀", callback_data=f"pick_timer:{test_id}:{val}:s"),
            ]
        )
    rows.append([InlineKeyboardButton("⬅️ К тестам", callback_data="menu_tests")])
    return InlineKeyboardMarkup(rows)


def build_question_markup(session: dict, question: dict) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(opt["text"][:80], callback_data=f"answer:{session['attempt_id']}:{question['id']}:{opt['id']}")]
        for opt in question["options"]
    ]
    rows.append([InlineKeyboardButton("⛔ Завершить тест", callback_data=f"stop_attempt:{session['attempt_id']}")])
    return InlineKeyboardMarkup(rows)


def build_question_text(question: dict, session: dict, remaining_seconds: int) -> str:
    idx = session["index"]
    total = len(session["questions"])
    progress = f"{idx + 1}/{total}"
    if session["timer_seconds"] <= 0:
        timer_line = "⏳ Без таймера"
    else:
        bar_length = 10
        filled = max(0, min(bar_length, int(round((remaining_seconds / session["timer_seconds"]) * bar_length))))
        timer_line = (
            f"⏳ Таймер: {remaining_seconds:02d}s  "
            + "█" * filled
            + "░" * (bar_length - filled)
        )
    return f"Вопрос {progress}\n{timer_line}\n\n{question['text']}"


def ensure_options_shuffled(question: dict) -> None:
    if not question.get("_shuffled"):
        random.shuffle(question["options"])
        question["_shuffled"] = True


def schedule_timer(context: CallbackContext, session: dict) -> None:
    for job_name in (session["timeout_job_name"], session["countdown_job_name"]):
        for job in context.job_queue.get_jobs_by_name(job_name):
            job.schedule_removal()
    if session["timer_seconds"] > 0:
        context.job_queue.run_once(
            on_timeout,
            when=session["timer_seconds"],
            data={"user_id": session["user_id"], "attempt_id": session["attempt_id"]},
            name=session["timeout_job_name"],
        )
        context.job_queue.run_repeating(
            update_timer_display,
            interval=1,
            first=1,
            data={"user_id": session["user_id"], "attempt_id": session["attempt_id"]},
            name=session["countdown_job_name"],
        )


async def ask_next_question(context: CallbackContext, session: dict, edit_query=None) -> None:
    idx = session["index"]
    questions = session["questions"]
    if idx >= len(questions):
        await complete_attempt(context, session, edit_query)
        return

    q = questions[idx]
    ensure_options_shuffled(q)
    remaining = session["timer_seconds"]
    text = build_question_text(q, session, remaining)
    markup = build_question_markup(session, q)

    if edit_query:
        message = await edit_query.edit_message_text(text, reply_markup=markup)
    else:
        message = await context.bot.send_message(chat_id=session["chat_id"], text=text, reply_markup=markup)

    if message:
        session["message_id"] = message.message_id

    session["started_question_at"] = datetime.utcnow().timestamp()
    schedule_timer(context, session)


async def complete_attempt(context: CallbackContext, session: dict, query=None) -> None:
    for job_name in (session["timeout_job_name"], session["countdown_job_name"]):
        for job in context.job_queue.get_jobs_by_name(job_name):
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
            [InlineKeyboardButton("📄 Полный отчет", callback_data=f"view_result:{session['attempt_id']}:1")],
            [InlineKeyboardButton("✅ Правильные", callback_data=f"view_correct:{session['attempt_id']}:1")],
            [InlineKeyboardButton("❌ Ошибки", callback_data=f"view_wrong:{session['attempt_id']}:1")],
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


async def view_attempt_result(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logging.info(f"view_attempt_result called with data: {update.callback_query.data if update.callback_query else 'NO QUERY'}")
    query = update.callback_query
    if not query or not query.data:
        logging.warning("view_attempt_result: query or query.data is missing")
        return
    try:
        await query.answer()
    except Exception as e:
        logging.error(f"query.answer() failed in view_attempt_result: {e}")
    
    try:
        parts = query.data.split(":")
        attempt_id = int(parts[1]) if len(parts) > 1 else 0
        logging.info(f"view_attempt_result: callback_data={query.data}, attempt_id={attempt_id}, parts={parts}")
        
        result = fetch_attempt_result(attempt_id)
        if not result:
            logging.warning(f"view_attempt_result: attempt {attempt_id} not found in DB")
            await query.edit_message_text("Результат не найден.", reply_markup=main_menu_keyboard())
            return
        logging.info(f"view_attempt_result: fetched result for attempt {attempt_id}")
        
        answers = fetch_attempt_answers_details(attempt_id)
        logging.info(f"view_attempt_result: fetched {len(answers) if answers else 0} answer details")
        
        page = 1
        if len(parts) >= 3:
            try:
                page = int(parts[2])
            except Exception:
                page = 1

        page_items, total_pages = _paginate(answers, page, PAGE_SIZE)
        text = (
            f"📄 Полный отчет: {result['test_title']}\n\n"
            f"Результат: {result['score']}%\n"
            f"Правильных: {result['correct_answers']} из {result['total_questions']}\n\n"
            f"Страница {page}/{total_pages}\n\n"
        )
        for row in page_items:
            sel = row['selected_text'] or ('время вышло' if row['timed_out'] else 'нет ответа')
            correct = row['correct_text'] or 'не указано'
            mark = '✅' if row['is_correct'] else '❌'
            text += f"{row['position']}. {row['question_text'][:120]}\n{mark} Ваш ответ: {sel[:120]} | Правильно: {correct[:120]}\n\n"

        nav_buttons = []
        if page > 1:
            nav_buttons.append(InlineKeyboardButton("⬅️ Назад", callback_data=f"view_result:{attempt_id}:{page-1}"))
        if page < total_pages:
            nav_buttons.append(InlineKeyboardButton("Вперед ➡️", callback_data=f"view_result:{attempt_id}:{page+1}"))

        markup = InlineKeyboardMarkup(
            [
                nav_buttons,
                [InlineKeyboardButton("✅ Правильные", callback_data=f"view_correct:{attempt_id}:1"), InlineKeyboardButton("❌ Ошибки", callback_data=f"view_wrong:{attempt_id}:1")],
                [InlineKeyboardButton("🏠 Главное меню", callback_data="go_menu")],
            ]
        )
        try:
            await query.edit_message_text(text, reply_markup=markup)
            logging.info(f"view_attempt_result: message edited successfully for attempt {attempt_id}")
        except Exception as e:
            logging.error(f"view_attempt_result: edit_message_text failed: {e}")
            # If editing fails (message too long or other issues), send as new message
            if query.message and query.message.chat:
                chat_id = query.message.chat.id
                # truncate if still too long
                if len(text) > 3900:
                    text = text[:3900] + "\n\n...текст обрезан..."
                await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=markup)
                logging.info(f"view_attempt_result: sent as new message instead")
    except Exception as e:
        logging.error(f"view_attempt_result: unexpected error: {e}", exc_info=True)
        try:
            await query.edit_message_text(f"Ошибка: {str(e)[:100]}", reply_markup=main_menu_keyboard())
        except:
            pass


async def view_correct_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        logging.warning("view_correct_list: query or query.data is missing")
        return
    try:
        await query.answer()
    except Exception as e:
        logging.error(f"query.answer() failed in view_correct_list: {e}")
    
    try:
        parts = query.data.split(":")
        attempt_id = int(parts[1]) if len(parts) > 1 else 0
        logging.info(f"view_correct_list: callback_data={query.data}, attempt_id={attempt_id}, parts={parts}")
        
        rows = fetch_correct_answers(attempt_id)
        logging.info(f"view_correct_list: fetched {len(rows) if rows else 0} correct answers")
        
        page = 1
        if len(parts) >= 3:
            try:
                page = int(parts[2])
            except Exception:
                page = 1

        if not rows:
            logging.info(f"view_correct_list: no rows for attempt {attempt_id}")
            await query.edit_message_text("Нет правильных ответов.", reply_markup=main_menu_keyboard())
            return

        page_items, total_pages = _paginate(rows, page, PAGE_SIZE)
        text = f"✅ Список правильных ответов (страница {page}/{total_pages}):\n\n"
        for r in page_items:
            sel = r['selected_text'] or ('время вышло' if r['timed_out'] else 'нет ответа')
            corr = r['correct_text'] or 'не указано'
            text += f"{r['position']}. {r['text'][:120]}\nВаш ответ: {sel[:120]} | Правильно: {corr[:120]}\n\n"

        nav_buttons = []
        if page > 1:
            nav_buttons.append(InlineKeyboardButton("⬅️ Назад", callback_data=f"view_correct:{attempt_id}:{page-1}"))
        if page < total_pages:
            nav_buttons.append(InlineKeyboardButton("Вперед ➡️", callback_data=f"view_correct:{attempt_id}:{page+1}"))

        markup = InlineKeyboardMarkup(
            [
                nav_buttons,
                [InlineKeyboardButton("📄 Полный отчет", callback_data=f"view_result:{attempt_id}:1"), InlineKeyboardButton("❌ Ошибки", callback_data=f"view_wrong:{attempt_id}:1")],
                [InlineKeyboardButton("🏠 Главное меню", callback_data="go_menu")],
            ]
        )
        try:
            await query.edit_message_text(text, reply_markup=markup)
            logging.info(f"view_correct_list: message edited successfully for attempt {attempt_id}")
        except Exception as e:
            logging.error(f"view_correct_list: edit_message_text failed: {e}")
            if query.message and query.message.chat:
                chat_id = query.message.chat.id
                if len(text) > 3900:
                    text = text[:3900] + "\n\n...текст обрезан..."
                await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=markup)
                logging.info(f"view_correct_list: sent as new message instead")
    except Exception as e:
        logging.error(f"view_correct_list: unexpected error: {e}", exc_info=True)
        try:
            await query.edit_message_text(f"Ошибка: {str(e)[:100]}", reply_markup=main_menu_keyboard())
        except:
            pass


async def view_wrong_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        logging.warning("view_wrong_list: query or query.data is missing")
        return
    try:
        await query.answer()
    except Exception as e:
        logging.error(f"query.answer() failed in view_wrong_list: {e}")
    
    try:
        parts = query.data.split(":")
        attempt_id = int(parts[1]) if len(parts) > 1 else 0
        logging.info(f"view_wrong_list: callback_data={query.data}, attempt_id={attempt_id}, parts={parts}")
        
        rows = fetch_wrong_answers(attempt_id)
        logging.info(f"view_wrong_list: fetched {len(rows) if rows else 0} wrong answers")
        
        page = 1
        if len(parts) >= 3:
            try:
                page = int(parts[2])
            except Exception:
                page = 1

        if not rows:
            logging.info(f"view_wrong_list: no rows for attempt {attempt_id}")
            await query.edit_message_text("Нет ошибок. Отлично!", reply_markup=main_menu_keyboard())
            return

        page_items, total_pages = _paginate(rows, page, PAGE_SIZE)
        text = f"❌ Список ошибок (страница {page}/{total_pages}):\n\n"
        for r in page_items:
            sel = r['selected_text'] or ('время вышло' if r['timed_out'] else 'нет ответа')
            corr = r['correct_text'] or 'не указано'
            text += f"{r['position']}. {r['text'][:120]}\nВаш ответ: {sel[:120]} | Правильно: {corr[:120]}\n\n"

        nav_buttons = []
        if page > 1:
            nav_buttons.append(InlineKeyboardButton("⬅️ Назад", callback_data=f"view_wrong:{attempt_id}:{page-1}"))
        if page < total_pages:
            nav_buttons.append(InlineKeyboardButton("Вперед ➡️", callback_data=f"view_wrong:{attempt_id}:{page+1}"))

        markup = InlineKeyboardMarkup(
            [
                nav_buttons,
                [InlineKeyboardButton("📄 Полный отчет", callback_data=f"view_result:{attempt_id}:1"), InlineKeyboardButton("✅ Правильные", callback_data=f"view_correct:{attempt_id}:1")],
                [InlineKeyboardButton("🏠 Главное меню", callback_data="go_menu")],
            ]
        )
        try:
            await query.edit_message_text(text, reply_markup=markup)
            logging.info(f"view_wrong_list: message edited successfully for attempt {attempt_id}")
        except Exception as e:
            logging.error(f"view_wrong_list: edit_message_text failed: {e}")
            if query.message and query.message.chat:
                chat_id = query.message.chat.id
                if len(text) > 3900:
                    text = text[:3900] + "\n\n...текст обрезан..."
                await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=markup)
                logging.info(f"view_wrong_list: sent as new message instead")
    except Exception as e:
        logging.error(f"view_wrong_list: unexpected error: {e}", exc_info=True)
        try:
            await query.edit_message_text(f"Ошибка: {str(e)[:100]}", reply_markup=main_menu_keyboard())
        except:
            pass


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

    for job_name in (session["timeout_job_name"], session["countdown_job_name"]):
        for job in context.job_queue.get_jobs_by_name(job_name):
            job.schedule_removal()

    await context.bot.send_message(chat_id=session["chat_id"], text="⏰ Время вышло. Следующий вопрос.")
    await ask_next_question(context, session, edit_query=None)


async def update_timer_display(context: CallbackContext) -> None:
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
        for job_name in (session["timeout_job_name"], session["countdown_job_name"]):
            for job in context.job_queue.get_jobs_by_name(job_name):
                job.schedule_removal()
        return

    q = session["questions"][idx]
    ensure_options_shuffled(q)
    elapsed = int(datetime.utcnow().timestamp() - session["started_question_at"])
    remaining = max(session["timer_seconds"] - elapsed, 0)
    text = build_question_text(q, session, remaining)
    markup = build_question_markup(session, q)

    if not session.get("message_id"):
        return

    try:
        await context.bot.edit_message_text(
            chat_id=session["chat_id"],
            message_id=session["message_id"],
            text=text,
            reply_markup=markup,
        )
    except Exception:
        pass


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
        "# Название теста (необязательно, одно на весь файл)\n"
        "1) Первый вопрос\n"
        "- Неправильный вариант\n"
        "+ Правильный вариант\n"
        "- Еще один вариант\n\n"
        "2) Второй вопрос\n"
        "- Ответ A\n"
        "- Ответ B *\n"
        "- Ответ C\n\n"
        "Правильный ответ можно пометить одним из способов:\n"
        "• поставить '+' в начале строки\n"
        "• поставить '*' в конце строки\n"
        "• поставить '(+)' в конце строки\n\n"
        "Пример корректного файла:\n"
        "# Мой тест по языку Python\n"
        "1) Какой результат 2 + 2?\n"
        "- 3\n"
        "+ 4\n"
        "- 5\n\n"
        "2) Какой тип у 10 / 2?\n"
        "- int\n"
        "- str\n"
        "- float *\n\n"
        "Требования:\n"
        "• файл в UTF-8\n"
        "• минимум 2 варианта ответа на вопрос\n"
        "• минимум 1 правильный вариант на вопрос\n"
        "• размер файла до 1MB\n\n"
        "Порядок вариантов в файле не важен: бот показывает ответы в случайном порядке, так что правильный вариант не будет всегда первым."
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
        markup = tests_keyboard(tests, mine=mine)

    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=markup)
    else:
        await update.effective_message.reply_text(text, reply_markup=markup)


async def show_my_test_details(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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

    user = query.from_user
    if not user or test["creator_id"] != user.id:
        await query.edit_message_text("У вас нет прав на управление этим тестом.", reply_markup=main_menu_keyboard())
        return

    text = (
        f"🛠 Тест: {test['title']}\n"
        f"Вопросов: {test['question_count']}\n"
        f"Автор: {test['creator_name'] or 'Unknown'}\n\n"
        "Выберите действие:"
    )
    markup = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("▶️ Начать тест", callback_data=f"open_test:{test_id}")],
            [InlineKeyboardButton("✏️ Переименовать", callback_data=f"rename_test:{test_id}")],
            [InlineKeyboardButton("➕ Добавить вопросы", callback_data=f"append_test:{test_id}")],
            [InlineKeyboardButton("🗑 Удалить тест", callback_data=f"delete_test:{test_id}")],
            [InlineKeyboardButton("⬅️ Мои тесты", callback_data="menu_my_tests")],
        ]
    )
    await query.edit_message_text(text, reply_markup=markup)


async def prompt_admin_password(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()
    reset_user_states(context.user_data)
    context.user_data["awaiting_admin_password"] = True
    await query.edit_message_text(
        "Введите пароль для доступа к полной статистике и списку пользователей:",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ В меню", callback_data="go_menu")]]),
    )


async def show_full_admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    users = fetch_all_users(limit=30)
    gs = global_stats()
    parts = [
        "🔐 Полная статистика:\n",
        f"Тестов создано: {int(gs['tests_total'] or 0)}\n",
        f"Всего попыток: {int(gs['attempts_total'] or 0)}\n",
        f"Завершенных: {int(gs['completed_total'] or 0)}\n",
        f"Средний результат: {float(gs['avg_score'] or 0):.1f}%\n",
        f"Активных пользователей: {int(gs['active_users'] or 0)}\n\n",
        "Пользователи (до 30):\n",
    ]
    if not users:
        parts.append("Пока нет зарегистрированных пользователей.")
    else:
        for idx, row in enumerate(users, start=1):
            username = row["username"] or "-"
            first_name = row["first_name"] or "User"
            parts.append(
                f"{idx}. {first_name} (@{username}) — попыток {int(row['attempts'] or 0)}, "
                f"завершено {int(row['completed'] or 0)}, средний {float(row['avg_score'] or 0):.1f}%, лучш. {int(row['best_score'] or 0)}%\n"
            )
    text = "".join(parts)
    await update.effective_message.reply_text(text, reply_markup=main_menu_keyboard())


async def handle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return
    await query.answer()
    user = query.from_user
    upsert_user(user.id, user.username, user.first_name)

    if query.data == "go_menu":
        reset_user_states(context.user_data)
        await query.edit_message_text("Главное меню:", reply_markup=main_menu_keyboard())
        return

    if query.data == "menu_tests":
        reset_user_states(context.user_data)
        await show_test_list(update, mine=False)
        return

    if query.data == "menu_my_tests":
        reset_user_states(context.user_data)
        await show_test_list(update, mine=True)
        return

    if query.data == "menu_create":
        reset_user_states(context.user_data)
        context.user_data["awaiting_txt"] = True
        await query.edit_message_text(
            "Отправьте TXT-файл с вопросами. Максимальный размер 1MB.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ В меню", callback_data="go_menu")]]),
        )
        return

    if query.data == "menu_my_stats":
        reset_user_states(context.user_data)
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
        reset_user_states(context.user_data)
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

    if query.data == "menu_admin_stats":
        await prompt_admin_password(update, context)
        return

    if query.data == "menu_format":
        reset_user_states(context.user_data)
        await show_format(update, edit=True)
        return


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

    _, test_raw, timer_raw, order_flag = query.data.split(":")
    test_id = int(test_raw)
    timer_seconds = int(timer_raw)
    shuffle_questions = order_flag == "s"
    user = query.from_user
    chat = query.message.chat if query.message else None
    if not chat:
        return

    upsert_user(user.id, user.username, user.first_name)
    questions = fetch_questions_with_options(test_id)
    if not questions:
        await query.edit_message_text("В тесте нет вопросов.", reply_markup=main_menu_keyboard())
        return

    if shuffle_questions:
        random.shuffle(questions)

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
        "timeout_job_name": f"timeout:{attempt_id}",
        "countdown_job_name": f"countdown:{attempt_id}",
        "message_id": None,
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
    # More vivid feedback shown as ephemeral tooltip instead of new message
    feedback = "✅ Верно! 🎉" if is_correct else "❌ Неверно. 😢"
    try:
        await query.answer(feedback, show_alert=False)
    except Exception:
        # fallback to silent answer
        await query.answer()
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

    awaiting_append_test_id = context.user_data.get("awaiting_append_test_id")
    if not context.user_data.get("awaiting_txt") and awaiting_append_test_id is None:
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

    if awaiting_append_test_id is not None:
        if append_questions_to_test(test_id=awaiting_append_test_id, parsed=parsed):
            reset_user_states(context.user_data)
            await message.reply_text(
                f"✅ В тест добавлено {len(parsed.questions)} вопросов.",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [InlineKeyboardButton("▶️ Начать тест", callback_data=f"open_test:{awaiting_append_test_id}")],
                        [InlineKeyboardButton("🗂 Мои тесты", callback_data="menu_my_tests")],
                        [InlineKeyboardButton("🏠 Главное меню", callback_data="go_menu")],
                    ]
                ),
            )
        else:
            await message.reply_text(
                "Не удалось добавить вопросы. Убедитесь, что вы обновили правильный тест.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ В меню", callback_data="go_menu")]]),
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


async def handle_test_actions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return
    await query.answer()
    user = query.from_user
    if not user:
        return

    action, test_id_raw = query.data.split(":")
    test_id = int(test_id_raw)
    test = fetch_test_details(test_id)
    if not test:
        await query.edit_message_text("Тест не найден.", reply_markup=main_menu_keyboard())
        return
    if test["creator_id"] != user.id:
        await query.edit_message_text("У вас нет прав на управление этим тестом.", reply_markup=main_menu_keyboard())
        return

    if action == "rename_test":
        reset_user_states(context.user_data)
        context.user_data["awaiting_rename_test_id"] = test_id
        await query.edit_message_text(
            "Отправьте новое название для теста:",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Мои тесты", callback_data="menu_my_tests")]]),
        )
        return

    if action == "append_test":
        reset_user_states(context.user_data)
        context.user_data["awaiting_append_test_id"] = test_id
        await query.edit_message_text(
            "Отправьте TXT-файл с дополнительными вопросами для этого теста:",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Мои тесты", callback_data="menu_my_tests")]]),
        )
        return

    if action == "delete_test":
        await query.edit_message_text(
            "Вы уверены, что хотите удалить тест? Это действие нельзя отменить.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("Да, удалить", callback_data=f"confirm_delete_test:{test_id}")],
                    [InlineKeyboardButton("Нет, назад", callback_data="menu_my_tests")],
                ]
            ),
        )
        return

    if action == "confirm_delete_test":
        if delete_test(test_id=test_id, user_id=user.id):
            reset_user_states(context.user_data)
            await query.edit_message_text(
                "✅ Тест удален.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Мои тесты", callback_data="menu_my_tests")]]),
            )
        else:
            await query.edit_message_text("Не удалось удалить тест.", reply_markup=main_menu_keyboard())
        return


async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not update.effective_message or not update.effective_message.text:
        return
    text = update.effective_message.text.strip()

    rename_test_id = context.user_data.get("awaiting_rename_test_id")
    if rename_test_id is not None:
        if rename_test(rename_test_id, user.id, text):
            reset_user_states(context.user_data)
            await update.effective_message.reply_text(
                f"✅ Название теста обновлено на: {text}",
                reply_markup=main_menu_keyboard(),
            )
        else:
            await update.effective_message.reply_text(
                "Не удалось переименовать тест. Убедитесь, что вы являетесь автором и отправьте название снова.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Мои тесты", callback_data="menu_my_tests")]]),
            )
        return

    if context.user_data.get("awaiting_admin_password"):
        if text == get_admin_password():
            reset_user_states(context.user_data)
            await show_full_admin_stats(update, context)
        else:
            await update.effective_message.reply_text(
                "Неверный пароль. Попробуйте снова или вернитесь в меню.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ В меню", callback_data="go_menu")]]),
            )
        return

    if context.user_data.get("awaiting_append_test_id"):
        await update.effective_message.reply_text(
            "Нужен TXT-файл с вопросами. Пожалуйста, отправьте документ.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Мои тесты", callback_data="menu_my_tests")]]),
        )
        return

    if context.user_data.get("awaiting_txt"):
        await update.effective_message.reply_text(
            "Отправьте TXT-документ с тестом, а не текст.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ В меню", callback_data="go_menu")]]),
        )
        return

    await update.effective_message.reply_text(
        "Неизвестная команда. Выберите действие через меню.",
        reply_markup=main_menu_keyboard(),
    )


def build_app(token: str) -> Application:
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(
        CallbackQueryHandler(
            handle_menu,
            pattern=r"^(go_menu|menu_tests|menu_create|menu_my_stats|menu_global_stats|menu_my_tests|menu_format|menu_admin_stats)$",
        )
    )
    app.add_handler(CallbackQueryHandler(show_my_test_details, pattern=r"^open_my_test:\d+$"))
    app.add_handler(CallbackQueryHandler(handle_test_open, pattern=r"^open_test:\d+$"))
    app.add_handler(CallbackQueryHandler(start_attempt, pattern=r"^pick_timer:\d+:\d+:(?:r|s)$"))
    app.add_handler(CallbackQueryHandler(handle_answer, pattern=r"^answer:\d+:\d+:\d+$"))
    app.add_handler(CallbackQueryHandler(handle_stop_attempt, pattern=r"^stop_attempt:\d+$"))
    app.add_handler(CallbackQueryHandler(handle_test_actions, pattern=r"^(rename_test|append_test|delete_test|confirm_delete_test):\d+$"))
    app.add_handler(CallbackQueryHandler(view_attempt_result, pattern=r"^view_result:\d+(?::\d+)?$"))
    app.add_handler(CallbackQueryHandler(view_correct_list, pattern=r"^view_correct:\d+(?::\d+)?$"))
    app.add_handler(CallbackQueryHandler(view_wrong_list, pattern=r"^view_wrong:\d+(?::\d+)?$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    return app


def get_admin_password() -> str:
    return os.getenv("ADMIN_PASSWORD", "admin123")


def main() -> None:
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
        level=logging.INFO,
    )
    load_env_file(Path(__file__).with_name(".env"))
    init_db()
    web_thread = threading.Thread(target=run_web_server, daemon=True)
    web_thread.start()
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("Укажите BOT_TOKEN в переменных окружения или в файле .env.")
    app = build_app(token)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()


