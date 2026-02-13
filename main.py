import os
import time
import math
from fastapi import FastAPI, UploadFile, File, Request, HTTPException
from fastapi.responses import StreamingResponse, HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from pyrogram import Client, errors
from motor.motor_asyncio import AsyncIOMotorClient
import uuid
import uvicorn
import asyncio

# --- Config ---
# ID ကို Integer မပြောင်းခင် String အနေနဲ့ အရင်ယူမယ် (Error မတက်အောင်)
API_ID = os.environ.get("API_ID") 
API_HASH = os.environ.get("API_HASH")
BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHANNEL_ID_STR = os.environ.get("CHANNEL_ID") 
MONGO_URL = os.environ.get("MONGO_URL")

# --- Setup ---
app = FastAPI()
# Template Directory ကို "." (Current Folder) လို့ ပြောင်းထားပေးတယ်
templates = Jinja2Templates(directory="templates" if os.path.exists("templates") else ".")

# Database Setup
mongo_client = AsyncIOMotorClient(MONGO_URL)
db = mongo_client["fileshare_db"]
collection = db["files"]

# Telegram Client
# Parse Mode disabled to prevent markdown errors
bot = Client("my_bot", api_id=int(API_ID), api_hash=API_HASH, bot_token=BOT_TOKEN, in_memory=True)

@app.on_event("startup")
async def startup():
    print("Bot Starting...")
    await bot.start()
    
    # --- FIX: Force Resolve Channel on Startup ---
    # Bot စ run တာနဲ့ Channel ကို လှမ်းကြည့်ခိုင်းမယ်။ ဒါမှ Peer ID invalid မဖြစ်မှာ။
    try:
        # ID က -100 နဲ့စရင် Integer ပြောင်းမယ်
        if CHANNEL_ID_STR.startswith("-100"):
            chat_id = int(CHANNEL_ID_STR)
        else:
            chat_id = CHANNEL_ID_STR # Username (@channel) ဆိုရင် string အတိုင်းထားမယ်
            
        print(f"Connecting to Channel ID: {chat_id}...")
        chat = await bot.get_chat(chat_id)
        print(f"✅ Successfully connected to Channel: {chat.title}")
    except Exception as e:
        print(f"❌ CRITICAL ERROR: Bot cannot access the channel! Reason: {e}")

@app.on_event("shutdown")
async def shutdown():
    await bot.stop()

# --- Routes ---

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.post("/upload")
async def upload_file(file: UploadFile = File(...)):
    # ID Logic ပြန်ခေါ်မယ်
    if CHANNEL_ID_STR.startswith("-100"):
        target_chat_id = int(CHANNEL_ID_STR)
    else:
        target_chat_id = CHANNEL_ID_STR

    file_uid = str(uuid.uuid4())[:8]
    file_location = f"temp_{file.filename}"
    
    # Save temporarily
    with open(file_location, "wb") as f:
        f.write(await file.read())
        
    try:
        # Progress Callback မပါဘဲ ရိုးရိုးပို့မယ် (Error နည်းအောင်)
        msg = await bot.send_document(
            chat_id=target_chat_id,
            document=file_location,
            caption=f"Filename: {file.filename}\nUID: {file_uid}",
            force_document=True
        )
        file_id = msg.document.file_id
        file_size = msg.document.file_size
        
        # Temp file ဖျက်မယ်
        if os.path.exists(file_location):
            os.remove(file_location)

        # DB မှာ သိမ်းမယ်
        file_data = {
            "uid": file_uid,
            "file_id": file_id,
            "filename": file.filename,
            "size": file_size,
            "upload_date": time.time()
        }
        await collection.insert_one(file_data)

        return {"status": "success", "download_url": f"/dl/{file_uid}", "filename": file.filename}

    except errors.PeerIdInvalid:
        if os.path.exists(file_location): os.remove(file_location)
        return JSONResponse(status_code=400, content={"error": "Bot cannot access Channel. Please check ID or Admin rights."})
    except Exception as e:
        if os.path.exists(file_location): os.remove(file_location)
        print(f"Upload Error: {e}") # Log မှာ ကြည့်လို့ရအောင်
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.get("/dl/{uid}")
async def download_file(uid: str):
    file_data = await collection.find_one({"uid": uid})
    if not file_data:
        raise HTTPException(status_code=404, detail="File not found")

    async def file_streamer():
        try:
            async for chunk in bot.stream_media(file_data["file_id"]):
                yield chunk
        except Exception as e:
            print(f"Stream Error: {e}")

    return StreamingResponse(
        file_streamer(),
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{file_data["filename"]}"'}
    )

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
