import os
import time
import math
from fastapi import FastAPI, UploadFile, File, Request, HTTPException
from fastapi.responses import StreamingResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from pyrogram import Client
from motor.motor_asyncio import AsyncIOMotorClient
import uuid
import uvicorn

# --- Config (Render Environment Variables ကနေ ယူမယ်) ---
API_ID = int(os.environ.get("API_ID", "123456")) # ပြင်ရန်
API_HASH = os.environ.get("API_HASH", "your_api_hash") # ပြင်ရန်
BOT_TOKEN = os.environ.get("BOT_TOKEN", "your_bot_token") # ပြင်ရန်
CHANNEL_ID = int(os.environ.get("CHANNEL_ID", "-100xxxxxxx")) # ပြင်ရန်
MONGO_URL = os.environ.get("MONGO_URL", "your_mongodb_url") # ပြင်ရန်

# --- Setup ---
app = FastAPI()
templates = Jinja2Templates(directory="templates")

# Database Setup
mongo_client = AsyncIOMotorClient(MONGO_URL)
db = mongo_client["fileshare_db"]
collection = db["files"]

# Telegram Client (Bot)
bot = Client("my_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN, in_memory=True)

@app.on_event("startup")
async def startup():
    await bot.start()

@app.on_event("shutdown")
async def shutdown():
    await bot.stop()

# --- Routes ---

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.post("/upload")
async def upload_file(file: UploadFile = File(...)):
    # 1. Generate Unique ID
    file_uid = str(uuid.uuid4())[:8]
    
    # 2. Upload to Telegram (Stream upload would be complex, so we save temp then upload for simplicity on Render free tier limits)
    # Note: For very large files on Render Free, this might hit RAM limits. 
    # But for files < 500MB it's fine.
    
    file_location = f"temp_{file.filename}"
    
    # Save temporarily
    with open(file_location, "wb") as f:
        f.write(await file.read())
        
    # Send to Telegram Channel
    try:
        msg = await bot.send_document(
            chat_id=CHANNEL_ID,
            document=file_location,
            caption=f"File: {file.filename}\nID: {file_uid}",
            force_document=True 
        )
        file_id = msg.document.file_id
        file_size = msg.document.file_size
    except Exception as e:
        os.remove(file_location)
        return {"error": str(e)}
    
    # Remove temp file
    os.remove(file_location)

    # 3. Save to Database
    file_data = {
        "uid": file_uid,
        "file_id": file_id,
        "filename": file.filename,
        "size": file_size,
        "upload_date": time.time()
    }
    await collection.insert_one(file_data)

    # 4. Return Download Link
    return {"status": "success", "download_url": f"/dl/{file_uid}", "filename": file.filename}

# --- The Magic Streaming Download ---
@app.get("/dl/{uid}")
async def download_file(uid: str):
    file_data = await collection.find_one({"uid": uid})
    if not file_data:
        raise HTTPException(status_code=404, detail="File not found")

    async def file_streamer():
        # Stream file chunks from Telegram
        async for chunk in bot.stream_media(file_data["file_id"]):
            yield chunk

    return StreamingResponse(
        file_streamer(),
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{file_data["filename"]}"'}
    )

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
