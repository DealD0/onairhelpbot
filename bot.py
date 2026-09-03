import os
import json
import hmac
import hashlib
import logging
import asyncio
from urllib.parse import parse_qs

import aiohttp
from aiohttp import web
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

load_dotenv()

# ─── Config ───────────────────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
IMGBB_API_KEY = os.getenv("IMGBB_API_KEY", "").strip()
GOOGLE_SCRIPT_URL = os.getenv("GOOGLE_SCRIPT_URL", "").strip()
MINI_APP_URL = os.getenv("MINI_APP_URL", "").strip()
# Telegram принимает в WebApp-кнопке только https-ссылку. Если в переменной
# указали голый домен (без схемы) — подставляем https:// сами, чтобы не падать.
if MINI_APP_URL and not MINI_APP_URL.startswith(("http://", "https://")):
    MINI_APP_URL = "https://" + MINI_APP_URL
SERVER_HOST = os.getenv("SERVER_HOST", "0.0.0.0")
SERVER_PORT = int(os.getenv("SERVER_PORT", "8080"))
ALLOWED_USER_IDS_RAW = os.getenv("ALLOWED_USER_IDS", "").strip()
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
# Telegram держит long-poll до 50 сек. Чем длиннее, тем меньше запросов и логов.
POLL_TIMEOUT = int(os.getenv("POLL_TIMEOUT", "30"))
WATCHDOG_INTERVAL = int(os.getenv("WATCHDOG_INTERVAL", "60"))
WATCHDOG_MAX_RETRIES = int(os.getenv("WATCHDOG_MAX_RETRIES", "3"))

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=LOG_LEVEL)
# httpx пишет INFO на каждый getUpdates: при long-poll это строка раз в
# POLL_TIMEOUT секунд (тысячи в сутки), в которых тонут реальные ошибки.
for noisy in ("httpx", "httpcore"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
log = logging.getLogger(__name__)

# Проверка наличия переменных при запуске
def check_env():
    missing = []
    if not BOT_TOKEN: missing.append("BOT_TOKEN")
    if not IMGBB_API_KEY: missing.append("IMGBB_API_KEY")
    if not GOOGLE_SCRIPT_URL: missing.append("GOOGLE_SCRIPT_URL")
    if not MINI_APP_URL: missing.append("MINI_APP_URL")
    if not ALLOWED_USER_IDS_RAW: missing.append("ALLOWED_USER_IDS")
    
    if missing:
        log.error(f"❌ ОТСУТСТВУЮТ ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ: {', '.join(missing)}")
        log.error("Убедитесь, что вы добавили их во вкладку Variables в Railway!")
        return False
    return True

ALLOWED_USER_IDS = set(
    int(uid.strip()) for uid in ALLOWED_USER_IDS_RAW.split(",") if uid.strip()
)


# ─── Telegram initData validation ─────────────────────────────────────────────
def validate_init_data(init_data_raw: str) -> dict | None:
    """Validate Telegram Web App initData. Returns user dict or None."""
    try:
        parsed = parse_qs(init_data_raw)
        received_hash = parsed.get("hash", [None])[0]
        if not received_hash:
            return None

        # Build data-check-string (sorted, without hash)
        items = []
        for key, vals in parsed.items():
            if key != "hash":
                items.append(f"{key}={vals[0]}")
        items.sort()
        data_check_string = "\n".join(items)

        # HMAC validation
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        computed = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

        if not hmac.compare_digest(computed, received_hash):
            return None

        user_data = parsed.get("user", [None])[0]
        if user_data:
            return json.loads(user_data)
        return None
    except Exception as e:
        log.error(f"initData validation error: {e}")
        return None


def authorize_request(init_data_raw: str) -> tuple[dict | None, str | None]:
    """Validate initData and check whitelist. Returns (user, error_msg)."""
    if not init_data_raw:
        return None, "Missing initData"
    user = validate_init_data(init_data_raw)
    if not user:
        return None, "Invalid initData signature"
    if user.get("id") not in ALLOWED_USER_IDS:
        return None, "User not authorized"
    return user, None


# ─── API Handlers ─────────────────────────────────────────────────────────────
async def _gs_json(resp) -> dict:
    """Парсит ответ Google Apps Script как JSON. Если пришёл не JSON (например,
    HTML логина/ошибки Google) — кидаем понятную ошибку вместо невнятного
    'Expecting value: line 1 column 1 (char 0)'."""
    text = await resp.text()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        snippet = " ".join(text.split())[:200]
        raise RuntimeError(
            "Google Script вернул не JSON. Проверь GOOGLE_SCRIPT_URL и что веб-приложение "
            f"задеплоено с доступом 'Anyone'. Ответ (HTTP {resp.status}): {snippet}"
        )


async def handle_get_photos(request: web.Request) -> web.Response:
    """GET /api/photos?section=investor — get photos for a section."""
    section = request.query.get("section", "")
    if section not in ("investor", "trader"):
        return web.json_response({"error": "Invalid section"}, status=400)

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{GOOGLE_SCRIPT_URL}?action=list&section={section}") as resp:
                data = await _gs_json(resp)
        return web.json_response(data)
    except Exception as e:
        log.error(f"Get photos error: {e}")
        return web.json_response({"error": str(e)}, status=500)


async def handle_upload(request: web.Request) -> web.Response:
    """POST /api/upload — upload photos to ImgBB and save to Google Sheet."""
    try:
        reader = await request.multipart()
        init_data = ""
        section = ""
        files = []

        while True:
            part = await reader.next()
            if part is None:
                break
            if part.name == "initData":
                init_data = (await part.read()).decode()
            elif part.name == "section":
                section = (await part.read()).decode()
            elif part.name == "photos":
                file_data = await part.read()
                files.append({"data": file_data, "filename": part.filename or "photo.jpg"})

        # Auth
        user, err = authorize_request(init_data)
        if err:
            return web.json_response({"error": err}, status=403)

        if section not in ("investor", "trader"):
            return web.json_response({"error": "Invalid section"}, status=400)
        if not files:
            return web.json_response({"error": "No photos provided"}, status=400)

        # Upload each photo to ImgBB
        uploaded = []
        async with aiohttp.ClientSession() as session:
            for f in files:
                import base64
                b64 = base64.b64encode(f["data"]).decode()
                form = aiohttp.FormData()
                form.add_field("key", IMGBB_API_KEY)
                form.add_field("image", b64)
                form.add_field("name", f["filename"])

                async with session.post("https://api.imgbb.com/1/upload", data=form) as resp:
                    result = await resp.json()
                    if not result.get("success"):
                        return web.json_response({"error": f"ImgBB error: {result}"}, status=500)
                    import uuid
                    uploaded.append({
                        "url": result["data"]["url"],
                        "thumb": result["data"].get("thumb", {}).get("url", result["data"]["url"]),
                        "filename": f["filename"],
                        "rowId": str(uuid.uuid4())
                    })

            # Save to Google Sheet
            payload = {
                "action": "upload",
                "section": section,
                "photos": uploaded,
                "user": user.get("first_name", "Unknown"),
            }
            async with session.post(GOOGLE_SCRIPT_URL, json=payload) as resp:
                gs_result = await _gs_json(resp)

        if isinstance(gs_result, dict) and gs_result.get("error"):
            return web.json_response({"error": f"Google Sheets: {gs_result['error']}"}, status=502)

        log.info(f"User {user.get('id')} uploaded {len(uploaded)} photos to {section}")
        return web.json_response({"status": "ok", "photos": uploaded})

    except Exception as e:
        log.error(f"Upload error: {e}")
        return web.json_response({"error": str(e)}, status=500)


async def handle_delete(request: web.Request) -> web.Response:
    """POST /api/delete — delete selected photos."""
    try:
        data = await request.json()
        init_data = data.get("initData", "")
        section = data.get("section", "")
        row_ids = data.get("rowIds", [])

        user, err = authorize_request(init_data)
        if err:
            return web.json_response({"error": err}, status=403)

        if section not in ("investor", "trader"):
            return web.json_response({"error": "Invalid section"}, status=400)
        if not row_ids:
            return web.json_response({"error": "No photos selected"}, status=400)

        async with aiohttp.ClientSession() as session:
            payload = {"action": "delete", "section": section, "rowIds": row_ids}
            async with session.post(GOOGLE_SCRIPT_URL, json=payload) as resp:
                gs_result = await _gs_json(resp)

        log.info(f"User {user.get('id')} deleted rows {row_ids} from {section}")
        return web.json_response({"status": "ok"})

    except Exception as e:
        log.error(f"Delete error: {e}")
        return web.json_response({"error": str(e)}, status=500)


async def handle_reorder(request: web.Request) -> web.Response:
    """POST /api/reorder — rearrange photo rows."""
    try:
        data = await request.json()
        init_data = data.get("initData", "")
        section = data.get("section", "")
        row_ids = data.get("rowIds", []) # Ordered list of RowIDs

        user, err = authorize_request(init_data)
        if err:
            return web.json_response({"error": err}, status=403)

        if section not in ("investor", "trader"):
            return web.json_response({"error": "Invalid section"}, status=400)
        if not row_ids:
            return web.json_response({"error": "No order provided"}, status=400)

        async with aiohttp.ClientSession() as session:
            payload = {"action": "reorder", "section": section, "rowIds": row_ids}
            async with session.post(GOOGLE_SCRIPT_URL, json=payload) as resp:
                gs_result = await _gs_json(resp)

        log.info(f"User {user.get('id')} reordered photos in {section}")
        return web.json_response({"status": "ok"})

    except Exception as e:
        log.error(f"Reorder error: {e}")
        return web.json_response({"error": str(e)}, status=500)


# ─── Mini App (статика) ───────────────────────────────────────────────────────
INDEX_HTML = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")


async def handle_index(request: web.Request) -> web.Response:
    """Отдаёт Mini App (index.html) с того же домена, что и API — без хардкода URL."""
    return web.FileResponse(INDEX_HTML)


async def handle_health(request: web.Request) -> web.Response:
    """GET /health — без обращений к БД и внешним API.

    Отдельно показывает состояние поллера: HTTP-сервер может жить, когда
    Telegram-часть уже умерла, и снаружи это выглядело бы как «сервис Online».
    """
    updater = request.app["bot_app"].updater
    polling = bool(updater and updater.running)
    return web.json_response(
        {"status": "ok" if polling else "degraded", "telegram_polling": polling},
        status=200 if polling else 503,
    )


# ─── CORS middleware ──────────────────────────────────────────────────────────
@web.middleware
async def cors_middleware(request, handler):
    if request.method == "OPTIONS":
        resp = web.Response()
    else:
        try:
            resp = await handler(request)
        except web.HTTPException as e:
            resp = e
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp


# ─── Telegram Bot Handlers ────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    first_name = user.first_name or "коллега"

    if user_id not in ALLOWED_USER_IDS:
        log.warning(f"Unauthorized access attempt: {user_id}")
        await update.message.reply_text(f"Я не смогу вам помочь 😔. Вы не сотрудник нашей команды.")
        return

    # Клавиатура с кнопкой запуска Mini App
    keyboard = [
        [InlineKeyboardButton("🚀 Запустить приложение", web_app=WebAppInfo(url=MINI_APP_URL))]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        f"Привет, {first_name}. Я готов к работе — запускай приложение",
        reply_markup=reply_markup
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ALLOWED_USER_IDS:
        return
    await update.message.reply_text(
        "📖 *Инструкция*\n\n"
        "1. Нажмите кнопку «📸 Управление фото»\n"
        "2. Выберите раздел (Инвестор / Трейдер)\n"
        "3. Просматривайте, добавляйте или удаляйте фото\n\n"
        "📌 Загрузка — ровно 3 фото за раз\n"
        "📌 Удаление — выберите ненужные фото и удалите",
        parse_mode="Markdown",
    )


async def cmd_myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Ваш Telegram ID: `{update.effective_user.id}`", parse_mode="Markdown")


async def fallback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user and user.id not in ALLOWED_USER_IDS:
        await update.message.reply_text("⛔ Доступ запрещён.")


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Логирует ошибку хендлера с трейсбеком, иначе она уходит в никуда."""
    log.error("Ошибка при обработке апдейта %s", update, exc_info=context.error)


# ─── Main ─────────────────────────────────────────────────────────────────────
async def start_polling(bot_app: Application) -> None:
    await bot_app.updater.start_polling(
        allowed_updates=Update.ALL_TYPES,
        timeout=POLL_TIMEOUT,
    )


async def watchdog(bot_app: Application, fatal: asyncio.Event) -> None:
    """Следит, что поллер Telegram жив.

    Апдейтер может остановиться сам (сеть, конфликт двух инстансов, ошибка
    внутри PTB), и тогда процесс продолжает жить: HTTP-сервер отвечает,
    Railway считает сервис здоровым, а бот молчит. Пробуем поднять поллер,
    и если не выходит — валимся, чтобы Railway перезапустил контейнер.
    """
    failures = 0
    heartbeat_at = 0.0
    while True:
        await asyncio.sleep(WATCHDOG_INTERVAL)

        if bot_app.updater.running:
            failures = 0
            now = asyncio.get_running_loop().time()
            if now - heartbeat_at >= 3600:
                heartbeat_at = now
                log.info("Жив: поллер Telegram работает")
            continue

        failures += 1
        log.error("Поллер Telegram остановился — попытка %s/%s", failures, WATCHDOG_MAX_RETRIES)
        try:
            await start_polling(bot_app)
        except Exception:
            log.exception("Не удалось перезапустить поллер")
        else:
            log.info("Поллер Telegram восстановлен")
            failures = 0
            continue

        if failures >= WATCHDOG_MAX_RETRIES:
            log.error("Поллер не поднимается — выходим, пусть Railway перезапустит контейнер")
            fatal.set()
            return


async def main() -> int:
    if not check_env():
        return 1
    # Telegram bot
    bot_app = Application.builder().token(BOT_TOKEN).build()
    bot_app.add_handler(CommandHandler("start", cmd_start))
    bot_app.add_handler(CommandHandler("help", cmd_help))
    bot_app.add_handler(CommandHandler("myid", cmd_myid))
    bot_app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, fallback))
    bot_app.add_error_handler(on_error)

    # HTTP API server
    web_app = web.Application(middlewares=[cors_middleware])
    web_app["bot_app"] = bot_app
    web_app.router.add_get("/", handle_index)
    web_app.router.add_get("/health", handle_health)
    web_app.router.add_get("/api/photos", handle_get_photos)
    web_app.router.add_post("/api/upload", handle_upload)
    web_app.router.add_post("/api/delete", handle_delete)
    web_app.router.add_post("/api/reorder", handle_reorder)
    web_app.router.add_route("OPTIONS", "/api/{tail:.*}", lambda r: web.Response())

    log.info(f"Authorized users: {ALLOWED_USER_IDS}")
    log.info(f"API server starting on {SERVER_HOST}:{SERVER_PORT}")

    # Start API server
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, SERVER_HOST, SERVER_PORT)
    await site.start()

    exit_code = 0

    # Start Telegram bot using context manager (handles init/shutdown)
    async with bot_app:
        await bot_app.start()
        await start_polling(bot_app)
        log.info("🚀 Бот и API сервер запущены!")

        fatal = asyncio.Event()
        guard = asyncio.create_task(watchdog(bot_app, fatal))
        try:
            await fatal.wait()
            exit_code = 1
        except (KeyboardInterrupt, asyncio.CancelledError):
            log.info("Остановка...")
        finally:
            guard.cancel()
            if bot_app.updater.running:
                await bot_app.updater.stop()
            await bot_app.stop()
            await runner.cleanup()

    return exit_code


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        pass
