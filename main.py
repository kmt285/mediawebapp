import os
import time
import uuid
import glob
import uvicorn
import aiofiles
import mimetypes
import math
from typing import Optional, List, Union
from datetime import datetime, timedelta
from authlib.integrations.starlette_client import OAuth
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import Request

from fastapi import FastAPI, UploadFile, File, HTTPException, Depends, Form
from fastapi.responses import StreamingResponse, HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from fastapi.middleware.cors import CORSMiddleware

from pyrogram import Client
from pyrogram.types import Message
from motor.motor_asyncio import AsyncIOMotorClient
from passlib.context import CryptContext
from jose import JWTError, jwt
from pydantic import BaseModel

# --- Config ---
API_ID = int(os.environ.get("API_ID", 12345)) # Change to your ID
API_HASH = os.environ.get("API_HASH", "your_hash")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "your_bot_token")
CHANNEL_ID_STR = os.environ.get("CHANNEL_ID", "-100xxxxxxx") 
MONGO_URL = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
SECRET_KEY = os.environ.get("SECRET_KEY", "supersecret")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 3000
MAX_FILE_SIZE = 2000 * 1024 * 1024  # 2GB (Telegram Limit)

app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY)

# CORS for local testing
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

templates = Jinja2Templates(directory="templates" if os.path.exists("templates") else ".")

# --- Google Auth ---
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")
oauth = OAuth()
oauth.register(
    name='google',
    client_id=GOOGLE_CLIENT_ID,
    client_secret=GOOGLE_CLIENT_SECRET,
    server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
    client_kwargs={'scope': 'openid email profile'}
)

# Database
mongo_client = AsyncIOMotorClient(MONGO_URL)
db = mongo_client["fileshare_db"]
files_collection = db["files"]
folders_collection = db["folders"]
users_collection = db["users"]

# Auth
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token", auto_error=False)

# Telegram Client
bot = Client("my_bot_session", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN, in_memory=True)

# --- Models ---
class CreateFolderRequest(BaseModel):
    name: str
    parent_id: Optional[str] = "root"

class RenameRequest(BaseModel):
    uid: str
    new_name: str
    type: str

class SetPasswordRequest(BaseModel):
    uid: str
    password: Optional[str] = None

# --- Helpers ---
def get_password_hash(password): return pwd_context.hash(password)
def verify_password(plain, hashed): return pwd_context.verify(plain, hashed)

def create_access_token(data: dict):
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

async def get_current_user(token: Optional[str] = Depends(oauth2_scheme)):
    if not token: return None
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username is None: return None
        return await users_collection.find_one({"username": username})
    except JWTError: return None

# --- Improved Streaming Helper (Fixes the crash) ---
async def file_streamer(file_id: str, offset: int = 0, length: int = -1):
    """
    Telegram မှ File ကို Chunk အလိုက်ဆွဲပြီး Stream လုပ်ပေးမည့် Generator
    """
    async for chunk in bot.stream_media(file_id, offset=offset, limit=length):
        yield chunk

async def delete_recursive(folder_uid: str, owner: str):
    await files_collection.delete_many({"parent_id": folder_uid, "owner": owner})
    async for sub_folder in folders_collection.find({"parent_id": folder_uid, "owner": owner}):
        await delete_recursive(sub_folder["uid"], owner)
    await folders_collection.delete_many({"parent_id": folder_uid, "owner": owner})

# --- Startup ---
@app.on_event("startup")
async def startup():
    print("🚀 Starting Bot...")
    await bot.start()
    
    # Check Channel Access
    try:
        cid = int(CHANNEL_ID_STR) if CHANNEL_ID_STR.lstrip('-').isdigit() else CHANNEL_ID_STR
        await bot.get_chat(cid)
        print("✅ Connected to Telegram Channel")
    except Exception as e:
        print(f"❌ Telegram Error: {e}. Check CHANNEL_ID and Bot Admin rights.")

@app.on_event("shutdown")
async def shutdown():
    print("🛑 Stopping Bot...")
    await bot.stop()

# --- Routes ---
@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.post("/register")
async def register(username: str = Form(...), password: str = Form(...)):
    if await users_collection.find_one({"username": username}):
        return JSONResponse(status_code=400, content={"error": "Username already taken"})
    await users_collection.insert_one({"username": username, "password": get_password_hash(password)})
    return {"message": "Success"}

@app.post("/token")
async def login(form_data: OAuth2PasswordRequestForm = Depends()):
    user = await users_collection.find_one({"username": form_data.username})
    if not user or not verify_password(form_data.password, user["password"]):
        raise HTTPException(status_code=400, detail="Incorrect username or password")
    return {"access_token": create_access_token({"sub": user["username"]}), "token_type": "bearer", "username": user["username"]}

# Google Auth
@app.get("/login/google")
async def login_google(request: Request):
    # Detect Localhost or Production automatically
    base_url = str(request.base_url).rstrip("/")
    redirect_uri = f"{base_url}/auth/google"
    return await oauth.google.authorize_redirect(request, redirect_uri)

@app.get("/auth/google")
async def auth_google(request: Request):
    try:
        token = await oauth.google.authorize_access_token(request)
        user_info = token.get('userinfo')
        if not user_info: raise HTTPException(status_code=400, detail="Google Auth Failed")
        
        email = user_info.get("email")
        if not await users_collection.find_one({"username": email}):
            await users_collection.insert_one({"username": email, "auth_type": "google", "created_at": time.time()})
        
        access_token = create_access_token({"sub": email})
        html_content = f"""
        <script>
            localStorage.setItem('token', '{access_token}');
            localStorage.setItem('username', '{email}');
            window.location.href = '/';
        </script>
        """
        return HTMLResponse(content=html_content)
    except Exception as e:
        return HTMLResponse(content=f"Auth Error: {str(e)}")

# --- FIXED UPLOAD ENDPOINT ---
@app.post("/upload")
async def upload_files(
    files: List[UploadFile] = File(...), 
    token: Optional[str] = Form(None), 
    parent_id: Optional[str] = Form(None)
):
    # Determine User (Guest or Registered)
    user = await get_current_user(token)
    username = user["username"] if user else "guest"
    
    # Determine Target Chat
    try:
        target_id = int(CHANNEL_ID_STR) if CHANNEL_ID_STR.lstrip('-').isdigit() else CHANNEL_ID_STR
    except:
        return JSONResponse(status_code=500, content={"error": "Invalid Channel ID Config"})

    uploaded_results = [] 

    for file in files:
        file.file.seek(0, 2)
        file_size = file.file.tell()
        file.file.seek(0)
        
        # Max Size Limit (Example: 50MB for Guest, 2GB for User)
        limit = MAX_FILE_SIZE if user else 50 * 1024 * 1024
        if file_size > limit:
            uploaded_results.append({"status": "failed", "filename": file.filename, "error": "File too large"})
            continue

        file_ext = os.path.splitext(file.filename)[1]
        temp_name = f"{uuid.uuid4()}{file_ext}"
        
        try:
            # Save to temp
            async with aiofiles.open(temp_name, 'wb') as out_file:
                while content := await file.read(1024 * 1024): 
                    await out_file.write(content)
            
            # Send to Telegram
            msg: Message = await bot.send_document(
                chat_id=target_id,
                document=temp_name,
                file_name=file.filename,
                caption=f"Owner: {username} | Size: {file_size}",
                force_document=True
            )
            
            # Retrieve File ID and Thumb ID
            # Priority: Document -> Video -> Audio -> Photo
            tg_file = msg.document or msg.video or msg.audio or msg.photo
            file_id_str = tg_file.file_id
            
            thumb_id_str = None
            if hasattr(tg_file, 'thumbs') and tg_file.thumbs:
                thumb_id_str = tg_file.thumbs[-1].file_id # Best quality thumb

            # --- VITAL: INSERT INTO DATABASE ---
            uid = str(uuid.uuid4())[:8]
            file_doc = {
                "uid": uid,
                "filename": file.filename,
                "size": file_size,
                "owner": username,
                "parent_id": parent_id if parent_id and parent_id != 'root' else None,
                "file_id": file_id_str,
                "thumb_id": thumb_id_str,
                "upload_date": time.time(),
                "msg_id": msg.id,
                "share_password": None
            }
            await files_collection.insert_one(file_doc)

            uploaded_results.append({
                "status": "success", 
                "filename": file.filename, 
                "uid": uid
            })

        except Exception as e:
            print(f"Upload Error: {e}")
            uploaded_results.append({"status": "failed", "filename": file.filename, "error": str(e)})
        
        finally:
            if os.path.exists(temp_name): os.remove(temp_name)

    return {"results": uploaded_results}

# --- Drive API ---
@app.get("/api/content")
async def get_content(folder_id: Optional[str] = "root", q: Optional[str] = None, token: str = Depends(oauth2_scheme)):
    user = await get_current_user(token)
    if not user: raise HTTPException(status_code=401)
    
    # Search Logic (Fixed)
    filter_query = {"owner": user["username"]}
    if q:
        filter_query["filename"] = {"$regex": q, "$options": "i"}
    else:
        filter_query["parent_id"] = None if folder_id == "root" else folder_id

    # Get Folders (Only if not searching files specifically, or implement folder search too)
    folders = []
    if not q:
        async for f in folders_collection.find({"owner": user["username"], "parent_id": filter_query.get("parent_id")}).sort("name", 1):
            folders.append({"uid": f["uid"], "name": f["name"], "type": "folder"})
    
    # Get Files
    files = []
    async for f in files_collection.find(filter_query).sort("upload_date", -1):
        files.append({
            "uid": f["uid"],
            "name": f["filename"],
            "size": f"{round(f['size']/1024/1024, 2)} MB",
            "type": "file",
            "date": time.strftime('%Y-%m-%d', time.localtime(f['upload_date'])),
            "has_thumb": bool(f.get("thumb_id")),
            "has_password": bool(f.get("share_password"))
        })
    return {"folders": folders, "files": files}

@app.post("/api/folder")
async def create_folder(req: CreateFolderRequest, token: str = Depends(oauth2_scheme)):
    user = await get_current_user(token)
    if not user: raise HTTPException(status_code=401)
    await folders_collection.insert_one({
        "uid": str(uuid.uuid4())[:8],
        "name": req.name,
        "owner": user["username"],
        "parent_id": req.parent_id if req.parent_id != "root" else None,
        "created_at": time.time()
    })
    return {"message": "Created"}

@app.put("/api/rename")
async def rename_item(req: RenameRequest, token: str = Depends(oauth2_scheme)):
    user = await get_current_user(token)
    if not user: raise HTTPException(status_code=401)
    col = folders_collection if req.type == "folder" else files_collection
    await col.update_one({"uid": req.uid, "owner": user["username"]}, {"$set": {"name" if req.type == "folder" else "filename": req.new_name}})
    return {"message": "Renamed"}

@app.delete("/api/delete/{uid}")
async def delete_item(uid: str, type: str, token: str = Depends(oauth2_scheme)):
    user = await get_current_user(token)
    if not user: raise HTTPException(status_code=401)
    
    if type == "folder":
        await delete_recursive(uid, user["username"])
        await folders_collection.delete_one({"uid": uid, "owner": user["username"]})
    else:
        # Optional: Delete from Telegram too if you want to save space
        # file_doc = await files_collection.find_one({"uid": uid})
        # await bot.delete_messages(chat_id, file_doc['msg_id'])
        await files_collection.delete_one({"uid": uid, "owner": user["username"]})
    
    return {"message": "Deleted"}

@app.put("/api/file/password")
async def set_file_password(req: SetPasswordRequest, token: str = Depends(oauth2_scheme)):
    user = await get_current_user(token)
    if not user: raise HTTPException(status_code=401)
    await files_collection.update_one({"uid": req.uid, "owner": user["username"]}, {"$set": {"share_password": req.password}})
    return {"message": "Password updated"}

# --- File Access & Streaming ---
def get_password_prompt_html(uid: str, action: str, error: str = ""):
    # (Same HTML string from your original code)
    return f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Protected File</title>
        <script src="https://cdn.tailwindcss.com"></script>
        <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css" rel="stylesheet">
    </head>
    <body class="bg-gray-900 h-screen flex items-center justify-center font-sans">
        <div class="bg-gray-800 p-8 rounded-xl shadow-2xl w-80 text-center border border-gray-700">
            <i class="fas fa-lock text-5xl text-yellow-500 mb-4 drop-shadow-lg"></i>
            <h2 class="text-white text-xl font-bold mb-2">Protected File</h2>
            <p class="text-gray-400 text-sm mb-6">Enter password to access this file</p>
            {"<p class='text-red-400 text-xs mb-3 bg-red-900/30 py-1 rounded'>" + error + "</p>" if error else ""}
            <form action="/{action}/{uid}" method="GET" class="flex flex-col gap-3">
                <input type="password" name="pwd" placeholder="Enter Password" required class="bg-gray-900 border border-gray-600 rounded p-2 text-white outline-none focus:border-blue-500 text-center">
                <button type="submit" class="bg-blue-600 hover:bg-blue-500 text-white font-bold py-2 px-4 rounded transition">
                    Unlock File
                </button>
            </form>
        </div>
    </body>
    </html>
    """

@app.get("/dl/{uid}")
async def download_file(uid: str, pwd: Optional[str] = None):
    file_data = await files_collection.find_one({"uid": uid})
    if not file_data: raise HTTPException(status_code=404)
    
    if file_data.get("share_password"):
        if pwd != file_data["share_password"]: return HTMLResponse(get_password_prompt_html(uid, "dl", "Incorrect Password" if pwd else ""))

    return StreamingResponse(
        bot.stream_media(file_data["file_id"]), 
        media_type="application/octet-stream", 
        headers={"Content-Disposition": f'attachment; filename="{file_data["filename"]}"'}
    )

@app.get("/view/{uid}")
async def view_file(uid: str, pwd: Optional[str] = None):
    file_data = await files_collection.find_one({"uid": uid})
    if not file_data: raise HTTPException(status_code=404)

    if file_data.get("share_password"):
        if pwd != file_data["share_password"]: return HTMLResponse(get_password_prompt_html(uid, "view", "Incorrect Password" if pwd else ""))

    mime_type, _ = mimetypes.guess_type(file_data["filename"])
    return StreamingResponse(
        bot.stream_media(file_data["file_id"]), 
        media_type=mime_type or "application/octet-stream",
        headers={"Content-Disposition": f'inline; filename="{file_data["filename"]}"'}
    )

@app.get("/thumb/{uid}")
async def get_thumbnail(uid: str):
    file_data = await files_collection.find_one({"uid": uid})
    if not file_data or not file_data.get("thumb_id"): 
        # Return a placeholder or 404
        raise HTTPException(status_code=404)
    
    return StreamingResponse(bot.stream_media(file_data["thumb_id"]), media_type="image/jpeg")

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
