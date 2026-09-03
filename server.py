import os
import asyncio
from aiohttp import web
import socketio
import aiohttp

sio = socketio.AsyncServer(async_mode='aiohttp', cors_allowed_origins='*')
app = web.Application()
sio.attach(app)

TELEGRAM_API = "https://api.telegram.org/bot"
SERVER_URL = os.getenv("RENDER_EXTERNAL_URL", "https://worker-jc06.onrender.com")
BOT_TOKENS = [t.strip() for t in os.getenv("BOT_TOKENS", "").split(",") if t.strip()]
OWNER_ID = str(os.getenv("OWNER_ID", ""))

workers = {}
user_routing = {}
user_bot_tokens = {}
allowed_users = set()
if OWNER_ID:
    allowed_users.add(OWNER_ID)

async def set_webhooks():
    for token in BOT_TOKENS:
        url = f"{TELEGRAM_API}{token}/setWebhook?url={SERVER_URL}/webhook/{token}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                result = await resp.json()
                print(f"Webhook set for {token[:10]}... : {result}")

async def send_tg_message(bot_token, chat_id, text):
    url = f"{TELEGRAM_API}{bot_token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    async with aiohttp.ClientSession() as session:
        await session.post(url, json=payload)

@sio.event
async def connect(sid, environ):
    worker_name = environ.get('HTTP_X_WORKER_NAME', f"Node-{sid[:4]}")
    workers[sid] = {"sid": sid, "name": worker_name}
    print(f"[+] Worker connected: {worker_name} ({sid})")

@sio.event
async def disconnect(sid):
    if sid in workers:
        print(f"[-] Worker disconnected: {workers[sid]['name']}")
        del workers[sid]

@sio.event
async def worker_output(sid, data):
    chat_id = data.get("chat_id")
    text = data.get("text")
    if chat_id and text:
        bot_token = user_bot_tokens.get(chat_id)
        if bot_token:
            await send_tg_message(bot_token, chat_id, text)

async def tg_webhook(request):
    bot_token = request.match_info.get('bot_token')
    try:
        update = await request.json()
    except:
        return web.Response(text="OK")

    msg = update.get("message")
    if not msg:
        return web.Response(text="OK")
    
    chat_id = msg["chat"]["id"]
    user_id = str(msg.get("from", {}).get("id", ""))
    text = msg.get("text", "")
    
    user_bot_tokens[chat_id] = bot_token

    # Access control
    if allowed_users and user_id not in allowed_users:
        if text == "/start":
            await send_tg_message(bot_token, chat_id, f"❌ You are not authorized. Your ID is: {user_id}")
        return web.Response(text="OK")

    # Owner commands
    if user_id == OWNER_ID and text.startswith("/allow "):
        new_user = text.split(" ")[1].strip()
        allowed_users.add(new_user)
        await send_tg_message(bot_token, chat_id, f"✅ User {new_user} added to allowed list.")
        return web.Response(text="OK")
    elif user_id == OWNER_ID and text.startswith("/disallow "):
        old_user = text.split(" ")[1].strip()
        if old_user in allowed_users and old_user != OWNER_ID:
            allowed_users.remove(old_user)
            await send_tg_message(bot_token, chat_id, f"✅ User {old_user} removed from allowed list.")
        return web.Response(text="OK")

    if text.startswith("/start") or text.startswith("/nodes"):
        if not workers:
            await send_tg_message(bot_token, chat_id, "No AI nodes are currently online. Please start Termux on your phone.")
            return web.Response(text="OK")
        reply = "🟢 Online AI Nodes:\n\n"
        for wsid, w in workers.items():
            reply += f"/select_{wsid[:6]} - {w['name']}\n"
        reply += "\nClick a node to connect to it!"
        await send_tg_message(bot_token, chat_id, reply)
        return web.Response(text="OK")

    if text.startswith("/select_"):
        short_id = text.split("_")[1]
        for wsid, w in workers.items():
            if wsid.startswith(short_id):
                user_routing[chat_id] = wsid
                await send_tg_message(bot_token, chat_id, f"✅ Connected to {w['name']}!")
                return web.Response(text="OK")
        await send_tg_message(bot_token, chat_id, "❌ Node not found or offline.")
        return web.Response(text="OK")

    worker_sid = user_routing.get(chat_id)
    if not worker_sid or worker_sid not in workers:
        await send_tg_message(bot_token, chat_id, "⚠️ You are not connected to any Node. Send /nodes to select one.")
        return web.Response(text="OK")

    await sio.emit("user_message", {"chat_id": chat_id, "text": text}, to=worker_sid)
    return web.Response(text="OK")

async def on_startup(app):
    asyncio.create_task(set_webhooks())

app.router.add_post('/webhook/{bot_token}', tg_webhook)
app.on_startup.append(on_startup)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    web.run_app(app, port=port)
