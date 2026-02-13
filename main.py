import os
import time
import uuid
import uvicorn
from typing import Optional
from datetime import datetime, timedelta

from fastapi import FastAPI, UploadFile, File, Request, HTTPException, Depends, status, Form
from fastapi.responses import StreamingResponse, HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm

from pyrogram import Client, errors
from motor.motor_asyncio import AsyncIOMotorClient
from passlib.context import CryptContext
from jose import JWTError, jwt

# --- Config ---
API_ID = os.environ.get("API_ID") 
API_HASH = os.environ.get("API_HASH")
BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHANNEL_ID_STR = os.environ.get("CHANNEL_ID") 
MONGO_URL = os.environ.get("MONGO_URL")

# Security Config (Login အတွက်)
SECRET_KEY = os.environ.get("SECRET_KEY", "supersecretkey12345") # Render Env မှာ ထပ်ထည့်သင့်
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 3000

# --- Setup ---
app = FastAPI()
templates = Jinja2Templates(directory="templates" if os.path.exists("templates") else ".")

# Database
mongo_client = AsyncIOMotorClient(MONGO_URL)
db = mongo_client["fileshare_db"]
files_collection = db["files"]
users_collection = db["users"]

# Auth Tools
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token", auto_error=False)

# Telegram Client
bot = Client("my_bot", api_id=int(API_ID), api_hash=API_HASH, bot_token=BOT_TOKEN, in_memory=True)

# --- Helper Functions ---
def verify_password(plain_password, hashed_password):
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password):
    return pwd_context.hash(password)

def create_access_token(data: dict):
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

async def get_current_user(token: Optional[str] = Depends(oauth2_scheme)):
    if not token:
        return None
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            return None
    except JWTError:
        return None
    user = await users_collection.find_one({"username": username})
    return user

# --- Events ---
@app.on_event("startup")
async def startup():
    print("Bot Starting...")
    await bot.start()
    try:
        if CHANNEL_ID_STR.startswith("-100"):
            chat_id = int(CHANNEL_ID_STR)
        else:
            chat_id = CHANNEL_ID_STR
        chat = await bot.get_chat(chat_id)
        print(f"✅ Connected to Channel: {chat.title}")
    except Exception as e:
        print(f"❌ Channel Error: {e}")

@app.on_event("shutdown")
async def shutdown():
    await bot.stop()

# --- Routes ---

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

# 1. Register
@app.post("/register")
async def register(username: str = Form(...), password: str = Form(...)):
    existing_user = await users_collection.find_one({"username": username})
    if existing_user:
        return JSONResponse(status_code=400, content={"error": "Username already exists"})
    
    hashed_password = get_password_hash(password)
    await users_collection.insert_one({"username": username, "password": hashed_password})
    return {"message": "User created successfully"}

# 2. Login
@app.post("/token")
async def login(form_data: OAuth2PasswordRequestForm = Depends()):
    user = await users_collection.find_one({"username": form_data.username})
    if not user or not verify_password(form_data.password, user["password"]):
        raise HTTPException(status_code=400, detail="Incorrect username or password")
    
    access_token = create_access_token(data={"sub": user["username"]})
    return {"access_token": access_token, "token_type": "bearer", "username": user["username"]}

# 3. Upload (Supports both Guest & User)
@app.post("/upload")
async def upload_file(file: UploadFile = File(...), token: Optional[str] = Form(None)):
    user = await get_current_user(token) if token else None
    
    # ID Logic
    target_chat_id = int(CHANNEL_ID_STR) if CHANNEL_ID_STR.startswith("-100") else CHANNEL_ID_STR

    file_uid = str(uuid.uuid4())[:8]
    file_location = f"temp_{file.filename}"
    
    with open(file_location, "wb") as f:
        f.write(await file.read())
        
    try:
        msg = await bot.send_document(
            chat_id=target_chat_id,
            document=file_location,
            caption=f"File: {file.filename}\nUID: {file_uid}",
            force_document=True
        )
        
        if os.path.exists(file_location): os.remove(file_location)

        file_data = {
            "uid": file_uid,
            "file_id": msg.document.file_id,
            "filename": file.filename,
            "size": msg.document.file_size,
            "upload_date": time.time(),
            "owner": user["username"] if user else None  # Track owner
        }
        await files_collection.insert_one(file_data)

        return {"status": "success", "download_url": f"/dl/{file_uid}", "filename": file.filename}

    except Exception as e:
        if os.path.exists(file_location): os.remove(file_location)
        return JSONResponse(status_code=500, content={"error": str(e)})

# 4. Get User Files (Dashboard)
@app.get("/api/myfiles")
async def get_my_files(token: str):
    user = await get_current_user(token)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid Token")
    
    cursor = files_collection.find({"owner": user["username"]}).sort("upload_date", -1)
    files = []
    async for document in cursor:
        files.append({
            "uid": document["uid"],
            "filename": document["filename"],
            "size": f"{round(document['size']/1024/1024, 2)} MB",
            "date": time.strftime('%Y-%m-%d', time.localtime(document['upload_date']))
        })
    return files

# 5. Delete File
@app.delete("/api/delete/{uid}")
async def delete_file(uid: str, token: str):
    user = await get_current_user(token)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    result = await files_collection.delete_one({"uid": uid, "owner": user["username"]})
    if result.deleted_count == 0:
        return JSONResponse(status_code=404, content={"error": "File not found or not yours"})
    return {"message": "Deleted"}

# 6. Download
@app.get("/dl/{uid}")
async def download_file(uid: str):
    file_data = await files_collection.find_one({"uid": uid})
    if not file_data:
        raise HTTPException(status_code=404, detail="File not found")

    async def file_streamer():
        async for chunk in bot.stream_media(file_data["file_id"]):
            yield chunk

    return StreamingResponse(
        file_streamer(),
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{file_data["filename"]}"'}
    )

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
