import os
import time
import math
import uuid
import uvicorn
import aiofiles
import mimetypes
from typing import Optional, List
from datetime import datetime, timedelta
from authlib.integrations.starlette_client import OAuth
from starlette.config import Config
from starlette.middleware.sessions import SessionMiddleware

from fastapi import FastAPI, UploadFile, File, Request, HTTPException, Depends, Form, Body
from fastapi.responses import StreamingResponse, HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm

from pyrogram import Client
from motor.motor_asyncio import AsyncIOMotorClient
from passlib.context import CryptContext
from jose import JWTError, jwt
from pydantic import BaseModel

# --- Config ---
API_ID = os.environ.get("API_ID") 
API_HASH = os.environ.get("API_HASH")
BOT_TOKEN = os.environ.get("BOT_TOKEN")
SESSION_STRING = os.environ.get("SESSION_STRING")
CHANNEL_ID_STR = os.environ.get("CHANNEL_ID") 
MONGO_URL = os.environ.get("MONGO_URL")
SECRET_KEY = os.environ.get("SECRET_KEY", "supersecret")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 3000
MAX_FILE_SIZE = 1100 * 1024 * 1024
mongo_client = AsyncIOMotorClient(MONGO_URL, maxPoolSize=50) 
db = mongo_client["fileshare_db"]

# --- Setup ---
app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY)
templates = Jinja2Templates(directory="templates" if os.path.exists("templates") else ".")

# --- Google Auth Config ---
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
db = mongo_client["fileshare_db"]
files_collection = db["files"]
folders_collection = db["folders"]
users_collection = db["users"]

# Auth
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token", auto_error=False)

# Telegram
if SESSION_STRING:
    bot = Client("my_bot", api_id=int(API_ID), api_hash=API_HASH, session_string=SESSION_STRING)
else:
    bot = Client("my_bot", api_id=int(API_ID), api_hash=API_HASH, bot_token=BOT_TOKEN)

# --- Models ---
class CreateFolderRequest(BaseModel):
    name: str
    parent_id: Optional[str] = "root"

class RenameRequest(BaseModel):
    uid: str
    new_name: str
    type: str

class MoveRequest(BaseModel):
    uid: str
    target_parent_id: str
    type: str

class SetPasswordRequest(BaseModel):
    uid: str
    password: Optional[str] = None

class BatchItem(BaseModel):
    uid: str
    type: str  # 'file' or 'folder'

class BatchDeleteRequest(BaseModel):
    items: List[BatchItem]

class BatchMoveRequest(BaseModel):
    items: List[BatchItem]
    target_parent_id: str

class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str

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

# --- Helper Functions ---
# main.py ထဲက delete_recursive function ကို ဒီလိုအစားထိုးပါ
async def delete_recursive(folder_uid: str, owner: str):
    """
    Folder တစ်ခုအောက်ရှိ File များနှင့် Sub-folder များကို အဆင့်ဆင့် လိုက်ဖျက်ပေးမည့် Function
    (Depth-First approach ကိုသုံးထားလို့ ပိုစိတ်ချရပါတယ်)
    """
    # 1. ဒီ Folder အောက်မှာရှိတဲ့ Sub-folder တွေကို အရင်ရှာမယ် (List အနေနဲ့ အရင်ထုတ်မယ်)
    sub_folders = []
    async for sub in folders_collection.find({"parent_id": folder_uid, "owner": owner}):
        sub_folders.append(sub["uid"])

    # 2. Sub-folder တစ်ခုချင်းစီအတွက် Recursive ခေါ်မယ်
    for sub_uid in sub_folders:
        await delete_recursive(sub_uid, owner)

    # 3. ဒီ Folder အောက်က File တွေကို ဖျက်မယ်
    await files_collection.delete_many({"parent_id": folder_uid, "owner": owner})

    # 4. နောက်ဆုံးမှ Folder တွေကို ဖျက်မယ်
    await folders_collection.delete_many({"parent_id": folder_uid, "owner": owner})
    

async def is_descendant(target_uid: str, moving_folder_uid: str, owner: str) -> bool:
    """
    Target Folder သည် Moving Folder ၏ Sub-folder ဖြစ်နေသလား စစ်ဆေးရန်။
    True ပြန်လာပါက Circular Move ဖြစ်သည်။
    """
    current_id = target_uid
    while current_id and current_id != "root":
        # Target ရဲ့ အထက်တစ်နေရာရာမှာ Moving Folder ရှိနေရင် Circular Move မိသွားပြီ
        if current_id == moving_folder_uid:
            return True
        
        # Parent ကို ဆက်ရှာရန် Database ထဲက ဆွဲထုတ်မည်
        current_folder = await folders_collection.find_one({"uid": current_id, "owner": owner})
        if not current_folder:
            break
        current_id = current_folder.get("parent_id")
        
    return False

# --- Startup ---
@app.on_event("startup")
async def startup():
    await bot.start()
    try:
        # Resolve Channel ID
        cid = int(CHANNEL_ID_STR) if CHANNEL_ID_STR.startswith("-100") else CHANNEL_ID_STR
        await bot.get_chat(cid)
        print("✅ Connected to Telegram Channel")
        
        # --- OPTIMIZATION: Create Database Indexes ---
        # User တစ်ယောက်ချင်းစီရဲ့ file တွေကို အမြန်ရှာနိုင်ဖို့
        await files_collection.create_index([("owner", 1), ("parent_id", 1)])
        await folders_collection.create_index([("owner", 1), ("parent_id", 1)])
        
        # UID နဲ့ အမြန်ရှာနိုင်ဖို့
        await files_collection.create_index("uid", unique=True)
        await folders_collection.create_index("uid", unique=True)
        
        print("✅ MongoDB Indexes verified")
    except Exception as e:
        print(f"❌ Telegram/MongoDB Error: {e}")

@app.on_event("shutdown")
async def shutdown(): await bot.stop()

# --- Routes ---

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

# Auth
@app.post("/register")
async def register(full_name: str = Form(...), username: str = Form(...), password: str = Form(...)):
    
    # --- Backend Validation အသစ် ---
    if not username.isalnum() or not username.islower():
        return JSONResponse(status_code=400, content={"error": "Username must contain only lowercase letters and numbers"})
        
    if len(password) < 6:
        return JSONResponse(status_code=400, content={"error": "Password must be at least 6 characters long"})
    # ------------------------------
    
    # Username တူနေတာ ရှိမရှိ စစ်မည်
    if await users_collection.find_one({"username": username}):
        return JSONResponse(status_code=400, content={"error": "Username already taken"})
    
    # Database ထဲသို့ ထည့်သိမ်းမည်
    await users_collection.insert_one({
        "full_name": full_name,
        "username": username, 
        "password": get_password_hash(password),
        "created_at": time.time()
    })
    return {"message": "Success"}

@app.post("/token")
async def login(form_data: OAuth2PasswordRequestForm = Depends()):
    user = await users_collection.find_one({"username": form_data.username})
    if not user or not verify_password(form_data.password, user["password"]):
        raise HTTPException(status_code=400, detail="Incorrect username or password")
    return {"access_token": create_access_token({"sub": user["username"]}), "token_type": "bearer", "username": user["username"]}

# --- Google Login Routes ---

@app.get("/login/google")
async def login_google(request: Request):
    # Render မှာ တင်ထားတဲ့ ဒိုမိန်းအမှန်ကို တိုက်ရိုက် ရေးထည့်တာ ပိုသေချာပါတယ်
    # (Local မှာ စမ်းရင် 'http://localhost:8000/auth/google' လို့ ပြောင်းသုံးပါ)
    redirect_uri = "https://www.myanmarcloud.online/auth/google" 
    
    return await oauth.google.authorize_redirect(request, redirect_uri)

@app.get("/auth/google")
async def auth_google(request: Request):
    try:
        # Google က ပြန်လာတဲ့ Data ကို ဖတ်မယ်
        token = await oauth.google.authorize_access_token(request)
        user_info = token.get('userinfo')
        
        if not user_info:
            raise HTTPException(status_code=400, detail="Google Auth Failed")

        email = user_info.get("email")
        name = user_info.get("name") or email.split("@")[0]

        # DB မှာ User ရှိမရှိ စစ်မယ်
        user = await users_collection.find_one({"username": email})
        
        if not user:
            # မရှိရင် အသစ်ဆောက်မယ် (Password မလိုဘူး Google နဲ့မို့လို့)
            await users_collection.insert_one({
                "username": email,
                "auth_type": "google",
                "created_at": time.time()
            })
        
        # JWT Token ထုတ်ပေးမယ်
        access_token = create_access_token({"sub": email})
        
        # Frontend ကို Token ပြန်ပို့ဖို့ HTML အသေးလေး render လုပ်မယ်
        # ဒါက Professional Technique ပါ (Backend က Token ကို LocalStorage ထဲထည့်ပေးလိုက်တာ)
        html_content = f"""
        <html>
            <head>
                <title>Redirecting...</title>
                <script>
                    localStorage.setItem('token', '{access_token}');
                    localStorage.setItem('username', '{email}');
                    window.location.href = '/';
                </script>
            </head>
            <body>
                <p>Login successful! Redirecting...</p>
            </body>
        </html>
        """
        return HTMLResponse(content=html_content)

    except Exception as e:
        return HTMLResponse(content=f"<p style='color:red'>Auth Error: {str(e)}</p>")

# Upload (Hybrid: Guest & User) - Secure Disk Buffer Streaming
@app.post("/upload")
async def upload_file(file: UploadFile = File(...), token: Optional[str] = Form(None), parent_id: Optional[str] = Form(None)):
    user = await get_current_user(token)
    
    try:
        target_id = int(CHANNEL_ID_STR)
    except ValueError:
        target_id = CHANNEL_ID_STR
        
    file_uid = str(uuid.uuid4())[:8]
    
    # File Size Limit စစ်ဆေးခြင်း
    if file.size and file.size > MAX_FILE_SIZE:
        return JSONResponse(status_code=413, content={"error": f"File too large. Maximum limit is {MAX_FILE_SIZE/1024/1024}MB."})
        
    # ယာယီသိမ်းမည့် ဖိုင်နာမည်
    temp_file_path = f"temp_{file_uid}_{file.filename}"
    
    try:
        # ၁။ File ကို Local Disk ပေါ် အရင်ရေးမည် (Pyrogram မှ Error မတက်စေရန် အသေချာဆုံးနည်း)
        import shutil
        with open(temp_file_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
        
        # ၂။ Telegram ဆီသို့ ပို့မည် (File Path အတိအကျကို သုံးလိုက်ပါသည်)
        msg = await bot.send_document(
            chat_id=target_id, 
            document=temp_file_path,
            caption=f"UID: {file_uid}", 
            force_document=True
        )
        
        # Thumbnail ယူခြင်း
        thumb_id = None
        if getattr(msg, "document", None) and getattr(msg.document, "thumbs", None):
            thumb_id = msg.document.thumbs[0].file_id

        # Thumbnail ယူခြင်း ပြီးသွားတဲ့ နေရာအောက်တွင် ...
        file_data = {
            "uid": file_uid,
            "message_id": msg.id,         # <--- 🌟 🌟 ဒီစာကြောင်း အသစ်တိုးလိုက်ပါ 🌟 🌟
            "file_id": msg.document.file_id,
            "filename": file.filename,
            "size": getattr(msg.document, "file_size", file.size),
            "upload_date": time.time(),
            "owner": user["username"] if user else None,
            "parent_id": parent_id if (user and parent_id != "root") else None,
            "thumb_id": thumb_id
        }
        await files_collection.insert_one(file_data)
        
        return {"status": "success", "download_url": f"/dl/{file_uid}", "filename": file.filename}

    except Exception as e:
        print(f"Upload Error: {e}")
        return JSONResponse(status_code=500, content={"error": str(e)})
        
    finally:
        # ၃။ Telegram ကို ပို့ပြီးသည်နှင့် (သို့မဟုတ် Error တက်သည်နှင့်) Local Temp File ကို ချက်ချင်းဖျက်မည်
        file.file.close()
        if os.path.exists(temp_file_path):
            try:
                os.remove(temp_file_path)
            except Exception:
                pass
        
# Drive API (User Only)
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

@app.get("/api/content")
async def get_content(folder_id: Optional[str] = "root", q: Optional[str] = None, token: str = Depends(oauth2_scheme)):
    user = await get_current_user(token)
    if not user: raise HTTPException(status_code=401)
    
    query = {"owner": user["username"]}

    if q:
        query["filename"] = {"$regex": q, "$options": "i"} 
        # Search လုပ်ရင် Folder ကိုပါ ရှာဖို့ ထည့်ပေးရပါမယ်
        folder_query = {"name": {"$regex": q, "$options": "i"}, "owner": user["username"]}
    else:
        query["parent_id"] = None if folder_id == "root" else folder_id
        folder_query = {"owner": user["username"], "parent_id": query.get("parent_id")}

    folders = []
    # if not q: ကို ဖြုတ်လိုက်ပါမည်
    async for f in folders_collection.find(folder_query).sort("name", 1):
        folders.append({"uid": f["uid"], "name": f["name"], "type": "folder"})
    
    files = []
    async for f in files_collection.find(query).sort("upload_date", -1):
        files.append({
            "uid": f["uid"],
            "name": f["filename"],
            "size": f"{round(f['size']/1024/1024, 2)} MB",
            "type": "file",
            "date": time.strftime('%Y-%m-%d', time.localtime(f['upload_date'])),
            "has_thumb": bool(f.get("thumb_id")),
            "has_password": bool(f.get("share_password")) # ဒီစာကြောင်း အသစ်တိုးလာတာပါ
        })
    return {"folders": folders, "files": files}
    
@app.put("/api/rename")
async def rename_item(req: RenameRequest, token: str = Depends(oauth2_scheme)):
    user = await get_current_user(token)
    if not user: raise HTTPException(status_code=401)
    col = folders_collection if req.type == "folder" else files_collection
    field = "name" if req.type == "folder" else "filename"
    await col.update_one({"uid": req.uid, "owner": user["username"]}, {"$set": {field: req.new_name}})
    return {"message": "Renamed"}

# main.py ထဲက move_item function
@app.put("/api/move")
async def move_item(req: MoveRequest, token: str = Depends(oauth2_scheme)):
    user = await get_current_user(token)
    if not user: raise HTTPException(status_code=401)
    
    # Target Folder ရှိမရှိ နှင့် ကိုယ်ပိုင် ဟုတ်မဟုတ် စစ်ဆေးခြင်း
    if req.target_parent_id != "root":
        target = await folders_collection.find_one({"uid": req.target_parent_id, "owner": user["username"]})
        if not target: 
            raise HTTPException(status_code=404, detail="Destination folder not found or access denied")
        
    # Folder Move လုပ်မည်ဆိုပါက
    if req.type == "folder":
        # ၁။ ကိုယ့် Folder ကို ကိုယ့်ထဲပြန်ထည့်လို့မရအောင် ကာကွယ်ခြင်း
        if req.uid == req.target_parent_id:
            raise HTTPException(status_code=400, detail="Cannot move folder into itself")
            
        # ၂။ Circular Move ဖြစ်မဖြစ် စစ်ဆေးခြင်း (အသစ်ထည့်ထားသောအပိုင်း)
        if req.target_parent_id != "root":
            is_circular = await is_descendant(req.target_parent_id, req.uid, user["username"])
            if is_circular:
                raise HTTPException(status_code=400, detail="Cannot move a folder into its own sub-folder")

    col = folders_collection if req.type == "folder" else files_collection
    
    # Root ဆိုရင် None ပြောင်းပေးရမယ် (Database Schema အရ)
    new_parent = None if req.target_parent_id == "root" else req.target_parent_id
    
    result = await col.update_one(
        {"uid": req.uid, "owner": user["username"]}, 
        {"$set": {"parent_id": new_parent}}
    )
    
    if result.matched_count == 0: # modified အစား matched ကိုသုံးပါ
        raise HTTPException(status_code=404, detail="Item not found")
    return {"message": "Moved successfully"}

@app.delete("/api/delete/{uid}")
async def delete_item(uid: str, type: str, token: str = Depends(oauth2_scheme)):
    user = await get_current_user(token)
    if not user: 
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    result = None # ဒီနေရာမှာ ကြိုကြေညာပေးထားပါ
    
    if type == "folder":
        await delete_recursive(uid, user["username"])
        result = await folders_collection.delete_one({"uid": uid, "owner": user["username"]})
    elif type == "file":
        result = await files_collection.delete_one({"uid": uid, "owner": user["username"]})
    else:
        raise HTTPException(status_code=400, detail="Invalid type")
    
    if result and result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Item not found")
        
    return {"message": "Deleted successfully"}

# --- Password Prompt HTML Helper ---
def get_password_prompt_html(uid: str, action: str, error: str = ""):
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

# --- User Profile API ---
@app.get("/api/user/profile")
async def get_user_profile(token: str = Depends(oauth2_scheme)):
    user = await get_current_user(token)
    if not user: raise HTTPException(status_code=401)
    
    return {
        "username": user["username"],
        "full_name": user.get("full_name", ""),
        "created_at": user.get("created_at", time.time()) # အကောင့်ဟောင်းတွေအတွက် Fallback
    }

@app.put("/api/user/password")
async def update_user_password(req: ChangePasswordRequest, token: str = Depends(oauth2_scheme)):
    user = await get_current_user(token)
    if not user: raise HTTPException(status_code=401)
    
    # Google နဲ့ ဝင်ထားတဲ့ အကောင့်ဆိုရင် Password ပြောင်းလို့မရအောင် တားမည်
    if user.get("auth_type") == "google":
        raise HTTPException(status_code=400, detail="Cannot change password for Google linked accounts")
        
    # Password အဟောင်း မှန်/မမှန် စစ်မည်
    if not verify_password(req.old_password, user["password"]):
        raise HTTPException(status_code=400, detail="Incorrect current password")
        
    # Password အသစ်ကို Hash လုပ်ပြီး Database မှာ Update လုပ်မည်
    hashed_new = get_password_hash(req.new_password)
    await users_collection.update_one(
        {"username": user["username"]},
        {"$set": {"password": hashed_new}}
    )
    return {"message": "Password updated successfully"}
    

# --- File Access Routes (Protected) ---
@app.get("/dl/{uid}")
async def download_file(uid: str, pwd: Optional[str] = None):
    file_data = await files_collection.find_one({"uid": uid})
    if not file_data: raise HTTPException(status_code=404, detail="File not found")
    
    req_pwd = file_data.get("share_password")
    if req_pwd:
        if not pwd: return HTMLResponse(get_password_prompt_html(uid, "dl"))
        if pwd != req_pwd: return HTMLResponse(get_password_prompt_html(uid, "dl", "Incorrect password!"))

    file_size = file_data.get("size", 0)
    
    async def streamer():
        try:
            target_chat = int(CHANNEL_ID_STR) if CHANNEL_ID_STR.startswith("-100") else CHANNEL_ID_STR
            # --- FIX: Message ID ကနေတစ်ဆင့် လုံခြုံစွာ Stream လုပ်မည် ---
            if "message_id" in file_data:
                message = await bot.get_messages(chat_id=target_chat, message_ids=file_data["message_id"])
                async for chunk in bot.stream_media(message): yield chunk
            else:
                async for chunk in bot.stream_media(file_data["file_id"]): yield chunk
        except Exception as e:
            print(f"Download Error: {e}")
            
    headers = {"Content-Disposition": f'attachment; filename="{file_data["filename"]}"'}
    if file_size: headers["Content-Length"] = str(file_size)
    
    return StreamingResponse(streamer(), media_type="application/octet-stream", headers=headers)

@app.get("/view/{uid}")
async def view_file(request: Request, uid: str, pwd: Optional[str] = None):
    file_data = await files_collection.find_one({"uid": uid})
    if not file_data: raise HTTPException(status_code=404, detail="File not found")
    
    req_pwd = file_data.get("share_password")
    if req_pwd:
        if not pwd: return HTMLResponse(get_password_prompt_html(uid, "view"))
        if pwd != req_pwd: return HTMLResponse(get_password_prompt_html(uid, "view", "Incorrect password!"))

    filename = file_data["filename"]
    file_size = file_data.get("size", 0)
    
    mime_type, _ = mimetypes.guess_type(filename)
    if not mime_type: mime_type = "application/octet-stream"
    
    target_chat = int(CHANNEL_ID_STR) if CHANNEL_ID_STR.startswith("-100") else CHANNEL_ID_STR
    
    # --- Range Request Logic for Video Playback ---
    range_header = request.headers.get("Range")
    
    if range_header and file_size > 0:
        byte1, byte2 = 0, None
        match = range_header.replace("bytes=", "").split("-")
        if match[0]: byte1 = int(match[0])
        if match[1]: byte2 = int(match[1])
        else: byte2 = file_size - 1

        length = byte2 - byte1 + 1
        chunk_size = 1048576 # 1MB (Pyrogram Default Chunk Size)
        offset_chunks = byte1 // chunk_size
        limit_chunks = math.ceil(length / chunk_size)
        
        async def range_streamer():
            first_chunk_offset = byte1 % chunk_size
            bytes_to_send = length
            
            try:
                # --- FIX: Message ID ကနေတစ်ဆင့် လုံခြုံစွာ Stream လုပ်မည် ---
                if "message_id" in file_data:
                    media_source = await bot.get_messages(chat_id=target_chat, message_ids=file_data["message_id"])
                else:
                    media_source = file_data["file_id"]
                    
                async for chunk in bot.stream_media(media_source, offset=offset_chunks, limit=limit_chunks):
                    if not chunk: break
                    if first_chunk_offset > 0:
                        chunk = chunk[first_chunk_offset:]
                        first_chunk_offset = 0
                    if len(chunk) > bytes_to_send:
                        chunk = chunk[:bytes_to_send]
                    
                    yield chunk
                    bytes_to_send -= len(chunk)
                    if bytes_to_send <= 0: break
            except Exception as e:
                print(f"Streaming Error: {e}")

        headers = {
            "Content-Range": f"bytes {byte1}-{byte2}/{file_size}",
            "Accept-Ranges": "bytes",
            "Content-Length": str(length),
            "Content-Type": mime_type,
        }
        return StreamingResponse(range_streamer(), status_code=206, headers=headers)
        
    else:
        async def streamer():
            try:
                # --- FIX: Message ID ကနေတစ်ဆင့် လုံခြုံစွာ Stream လုပ်မည် ---
                if "message_id" in file_data:
                    message = await bot.get_messages(chat_id=target_chat, message_ids=file_data["message_id"])
                    async for chunk in bot.stream_media(message): yield chunk
                else:
                    async for chunk in bot.stream_media(file_data["file_id"]): yield chunk
            except Exception as e:
                print(f"Streaming Error: {e}")
                
        headers = {
            "Accept-Ranges": "bytes",
            "Content-Type": mime_type,
            "Content-Disposition": f'inline; filename="{filename}"'
        }
        if file_size: headers["Content-Length"] = str(file_size)
        
        return StreamingResponse(streamer(), headers=headers)

@app.get("/thumb/{uid}")
async def get_thumbnail(uid: str):
    file_data = await files_collection.find_one({"uid": uid})
    if not file_data or not file_data.get("thumb_id"): raise HTTPException(status_code=404)
    async def streamer():
        async for chunk in bot.stream_media(file_data["thumb_id"]): yield chunk
    return StreamingResponse(streamer(), media_type="image/jpeg")

# --- Set Password API ---
@app.put("/api/file/password")
async def set_file_password(req: SetPasswordRequest, token: str = Depends(oauth2_scheme)):
    user = await get_current_user(token)
    if not user: raise HTTPException(status_code=401)
    await files_collection.update_one({"uid": req.uid, "owner": user["username"]}, {"$set": {"share_password": req.password}})
    return {"message": "Password updated"}

# --- Batch Operations Routes (Routes အပိုင်းမှာ ထပ်ဖြည့်ပါ) ---

@app.post("/api/batch/delete")
async def batch_delete(req: BatchDeleteRequest, token: str = Depends(oauth2_scheme)):
    user = await get_current_user(token)
    if not user: raise HTTPException(status_code=401)
    
    deleted_count = 0
    
    for item in req.items:
        try:
            if item.type == "folder":
                await delete_recursive(item.uid, user["username"])
                await folders_collection.delete_one({"uid": item.uid, "owner": user["username"]})
            elif item.type == "file":
                await files_collection.delete_one({"uid": item.uid, "owner": user["username"]})
            deleted_count += 1
        except:
            continue # တချို့ဖျက်မရရင် ကျော်သွားမယ်
            
    return {"message": f"Deleted {deleted_count} items"}

@app.put("/api/batch/move")
async def batch_move(req: BatchMoveRequest, token: str = Depends(oauth2_scheme)):
    user = await get_current_user(token)
    if not user: raise HTTPException(status_code=401)

    # Check Target
    target_parent = None if req.target_parent_id == "root" else req.target_parent_id
    if target_parent:
        target = await folders_collection.find_one({"uid": target_parent, "owner": user["username"]})
        if not target: raise HTTPException(status_code=404, detail="Target folder not found")

    moved_count = 0
    for item in req.items:
        # Folder Move အတွက် စစ်ဆေးချက်များ
        if item.type == "folder":
            # ၁။ ကိုယ့်အထဲကိုယ် ပြန်မထည့်မိအောင် စစ်မယ်
            if item.uid == req.target_parent_id:
                continue
                
            # ၂။ Circular Move ဖြစ်နေရင် ဒီ item ကို Move မလုပ်ဘဲ ကျော်သွားမယ်
            if req.target_parent_id != "root":
                is_circular = await is_descendant(req.target_parent_id, item.uid, user["username"])
                if is_circular:
                    continue # Circular ဖြစ်နေရင် မရွှေ့ဘဲ နောက်တစ်ဖိုင်ကို ဆက်သွားမယ်

        col = folders_collection if item.type == "folder" else files_collection
        res = await col.update_one(
            {"uid": item.uid, "owner": user["username"]},
            {"$set": {"parent_id": target_parent}}
        )
        if res.modified_count > 0: moved_count += 1

    return {"message": f"Moved {moved_count} items"}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
