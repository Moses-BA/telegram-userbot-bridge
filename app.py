import os
import json
import asyncio
import requests
from aiohttp import web
from pyrogram import Client, filters
from pyrogram.types import Message

# Load environment variables
API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
API_HASH = os.getenv("TELEGRAM_API_HASH", "")
N8N_WEBHOOK_URL = os.getenv("N8N_WEBHOOK_URL", "")
TARGET_GROUP_CHAT_ID = int(os.getenv("TARGET_GROUP_CHAT_ID", "0"))
PORT = int(os.getenv("PORT", "8080"))

# Parse JSON session strings
SESSIONS_RAW = os.getenv("SESSIONS_JSON", "{}")
SESSIONS = json.loads(SESSIONS_RAW)

clients = {}

for account_id, session_str in SESSIONS.items():
    clients[account_id] = Client(
        name=account_id,
        api_id=API_ID,
        api_hash=API_HASH,
        session_string=session_str,
        in_memory=True
    )

# Health Check Handler for Render & UptimeRobot
async def health_check_handler(request):
    return web.json_response({
        "status": "ok",
        "service": "telegram-userbot-bridge",
        "loaded_accounts": list(clients.keys())
    })

# Inbound Group Message Listener
async def register_listeners():
    if not clients:
        print("No active client sessions found.")
        return

    primary_client = list(clients.values())[0]

    @primary_client.on_message(filters.chat(TARGET_GROUP_CHAT_ID))
    async def handle_incoming(client: Client, message: Message):
        payload = {
            "telegram_msg_id": message.id,
            "sender_user_id": str(message.from_user.id) if message.from_user else None,
            "sender_username": message.from_user.username if message.from_user else "Unknown",
            "text": message.text or message.caption or "",
            "reply_to_message_id": message.reply_to_message.id if message.reply_to_message else None,
            "chat_id": message.chat.id
        }
        try:
            # Wrapped in asyncio.to_thread so HTTP requests don't freeze the Pyrogram listener
            await asyncio.to_thread(requests.post, N8N_WEBHOOK_URL, json=payload, timeout=5)
        except Exception as e:
            print(f"Error sending payload to n8n: {e}")

# Outbound Reply Handler
async def send_message_handler(request):
    try:
        data = await request.json()
        account_id = data.get("account_id")
        text = data.get("text")
        reply_to_id = data.get("reply_to_id")

        client = clients.get(account_id)
        if not client:
            return web.json_response({"status": "error", "message": f"Account {account_id} not loaded"}, status=400)

        sent_msg = await client.send_message(
            chat_id=TARGET_GROUP_CHAT_ID,
            text=text,
            reply_to_message_id=reply_to_id
        )
        return web.json_response({
            "status": "success",
            "telegram_msg_id": sent_msg.id,
            "account_id": account_id
        })
    except Exception as e:
        return web.json_response({"status": "error", "message": str(e)}, status=500)

async def main():
    for acc_id, client in clients.items():
        print(f"Starting session for {acc_id}...")
        await client.start()

    await register_listeners()

    app = web.Application()

    # Registered routes
    app.router.add_get("/", health_check_handler)
    app.router.add_get("/healthz", health_check_handler)
    app.router.add_post("/send", send_message_handler)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    print(f"Bridge active on port {PORT}...")
    await site.start()

    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
