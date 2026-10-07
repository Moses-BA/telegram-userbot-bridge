import os
import json
import asyncio
import aiohttp
from aiohttp import web
from pyrogram import Client, filters, idle
from pyrogram.handlers import MessageHandler
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

# Inbound Message Handler (Catch-All)
async def handle_incoming(client: Client, message: Message):
    sender_name = message.from_user.username if message.from_user else "Unknown"
    print(f"[INCOMING] Account '{client.name}' caught message from @{sender_name} in Chat ID ({message.chat.id}): '{message.text or message.caption or ''}'")

    # Filter out messages not from our target group if configured
    if TARGET_GROUP_CHAT_ID != 0 and message.chat.id != TARGET_GROUP_CHAT_ID:
        print(f"[DEBUG] Ignored message from non-target chat: {message.chat.id}")
        return

    if not N8N_WEBHOOK_URL:
        print("[ERROR] N8N_WEBHOOK_URL environment variable is empty. Message not forwarded.")
        return

    payload = {
        "telegram_msg_id": message.id,
        "sender_user_id": str(message.from_user.id) if message.from_user else None,
        "sender_username": sender_name,
        "text": message.text or message.caption or "",
        "reply_to_message_id": message.reply_to_message.id if message.reply_to_message else None,
        "chat_id": message.chat.id,
        "handled_by_account": client.name
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(N8N_WEBHOOK_URL, json=payload, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                print(f"[WEBHOOK] Forwarded message {message.id} to n8n (HTTP Status: {resp.status})")
    except Exception as e:
        print(f"[ERROR] Failed to forward payload to n8n: {e}")

# Manual Test Trigger Endpoint (Render -> n8n Verification)
async def test_trigger_handler(request):
    if not N8N_WEBHOOK_URL:
        return web.json_response({"status": "error", "message": "N8N_WEBHOOK_URL is empty"}, status=400)
    
    dummy_payload = {
        "telegram_msg_id": 99999,
        "sender_user_id": "123456789",
        "sender_username": "test_user",
        "text": "Manual trigger test from Render bridge",
        "reply_to_message_id": None,
        "chat_id": TARGET_GROUP_CHAT_ID,
        "handled_by_account": "SYSTEM_TEST"
    }
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(N8N_WEBHOOK_URL, json=dummy_payload, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                print(f"[TEST TRIGGER] Manual test sent to n8n. HTTP Status: {resp.status}")
                return web.json_response({"status": "success", "n8n_http_status": resp.status})
    except Exception as e:
        print(f"[TEST TRIGGER ERROR] Failed to hit n8n: {e}")
        return web.json_response({"status": "error", "message": str(e)}, status=500)

# Health Check Handler
async def health_check_handler(request):
    return web.json_response({
        "status": "ok",
        "service": "telegram-userbot-bridge",
        "loaded_accounts": list(clients.keys()),
        "target_group_id": TARGET_GROUP_CHAT_ID,
        "webhook_configured": bool(N8N_WEBHOOK_URL)
    })

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
        print(f"[ERROR] /send endpoint failed: {e}")
        return web.json_response({"status": "error", "message": str(e)}, status=500)

async def main():
    if not clients:
        print("[CRITICAL] No client sessions found in SESSIONS_JSON environment variable.")
        return

    # Attach unfiltered message handler to ALL clients
    for acc_id, client in clients.items():
        client.add_handler(MessageHandler(handle_incoming))

    # Start sessions and cache peers
    for acc_id, client in clients.items():
        print(f"Starting Pyrogram session for {acc_id}...")
        await client.start()

        if TARGET_GROUP_CHAT_ID != 0:
            try:
                cached = False
                async for dialog in client.get_dialogs(limit=100):
                    if dialog.chat.id == TARGET_GROUP_CHAT_ID:
                        print(f"[{acc_id}] Group '{dialog.chat.title}' found and cached!")
                        cached = True
                        async for _ in client.get_chat_history(TARGET_GROUP_CHAT_ID, limit=1):
                            pass
                        break
                
                if not cached:
                    await client.get_chat(TARGET_GROUP_CHAT_ID)
                    print(f"[{acc_id}] Group peer cached via direct lookup.")
            except Exception as e:
                print(f"[{acc_id}] Warning: Could not resolve peer for group {TARGET_GROUP_CHAT_ID}: {e}")

    app = web.Application()

    # Registered routes
    app.router.add_get("/", health_check_handler)
    app.router.add_get("/healthz", health_check_handler)
    app.router.add_get("/test-trigger", test_trigger_handler)
    app.router.add_post("/send", send_message_handler)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    print(f"Bridge active on port {PORT}...")
    await site.start()

    print("Pyrogram active. Entering idle event loop...")
    await idle()

if __name__ == "__main__":
    asyncio.run(main())
