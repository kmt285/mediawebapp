import os
import time
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
CHANNEL_ID_STR = os.environ.get("CHANNEL_ID") 
CHANNEL_INVITE_LINK = os.environ.get("CHANNEL_INVITE_LINK")
MONGO_URL = os.environ.get("MONGO_URL")
SECRET_KEY = os.environ.get("SECRET_KEY", "supersecret")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 3000

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
mongo_client = AsyncIOMotorClient(MONGO_URL)
db = mongo_client["fileshare_db"]
files_collection = db["files"]
folders_collection = db["folders"]
users_collection = db["users"]

# Auth
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token", auto_error=False)

# Telegram
bot = Client("my_bot", api_id=int(API_ID), api_hash=API_HASH, bot_token=BOT_TOKEN, in_memory=True)

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

async def get_current_admin(token: str = Depends(oauth2_scheme)):
    user = await get_current_user(token)
    if not user: raise HTTPException(status_code=401)
    
    # Database ထဲမှာ role='admin' ဖြစ်မှ ခွင့်ပြုမည်
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin privileges required")
    return user

# --- Helper Functions ---
# (ရှိပြီးသား function တွေရဲ့ အောက်မှာ ဒီ function ကို ထပ်ထည့်ပါ)

async def delete_recursive(folder_uid: str, owner: str):
    """
    Folder တစ်ခုအောက်ရှိ File များနှင့် Sub-folder များကို အဆင့်ဆင့် လိုက်ဖျက်ပေးမည့် Function
    """
    # 1. ဒီ Folder အောက်မှာရှိတဲ့ File တွေကို အရင်ဖျက်မယ်
    await files_collection.delete_many({"parent_id": folder_uid, "owner": owner})

    # 2. ဒီ Folder အောက်မှာရှိတဲ့ Sub-folder တွေကို ရှာမယ်
    async for sub_folder in folders_collection.find({"parent_id": folder_uid, "owner": owner}):
        # 3. တွေ့တဲ့ Sub-folder တစ်ခုချင်းစီအတွက် ဒီ Function ကိုပြန်ခေါ်မယ် (Recursion)
        await delete_recursive(sub_folder["uid"], owner)

    # 4. အထဲကအရာတွေ ရှင်းသွားပြီဆိုမှ Sub-folder တွေကို ဖျက်မယ်
    await folders_collection.delete_many({"parent_id": folder_uid, "owner": owner})

def get_target_chat_id(chat_id_str: str):
    """
    ID string ကို စစ်ဆေးပြီး Integer (သို့) Username string ပြန်ထုတ်ပေးမည့် function
    """
    if not chat_id_str:
        return None
    
    chat_id_str = chat_id_str.strip().replace('"', '').replace("'", "")
    
    # ဂဏန်းသက်သက်ပဲဆိုရင် (ဥပမာ -100xxx သို့မဟုတ် 100xxx) Integer ပြောင်းမယ်
    try:
        if chat_id_str.startswith("-100"):
            return int(chat_id_str)
        # တကယ်လို့ User က -100 မထည့်ဘဲ ဂဏန်းချည်းပဲထည့်ရင် -100 ထည့်ပေါင်းပေးမယ်
        if chat_id_str.isdigit() or (chat_id_str.startswith("-") and chat_id_str[1:].isdigit()):
             # private channel id အများစုက ဂဏန်း 13 လုံးကျော်တယ်၊ ဒါဆို -100 တပ်ပေးမယ်
            if len(chat_id_str) > 10 and not chat_id_str.startswith("-100"):
                 return int(f"-100{chat_id_str}")
            return int(chat_id_str)
    except ValueError:
        pass
        
    # ဂဏန်းမဟုတ်ရင် Username (@channel) အနေနဲ့ပဲ ပြန်ပေးမယ်
    return chat_id_str

#startup
@app.on_event("startup")
async def startup():
    print("🚀 Starting up...")
    await bot.start()
    
    found_channel = False
    target_id = None
    
    # Env ထဲက ID ကို ဂဏန်းပြောင်းယူမယ်
    try:
        if CHANNEL_ID_STR.startswith("-100"):
            target_id = int(CHANNEL_ID_STR)
        else:
            target_id = int(f"-100{CHANNEL_ID_STR}") if not CHANNEL_ID_STR.startswith("-") else int(CHANNEL_ID_STR)
    except:
        print("⚠️ ID format check needed")

    print(f"🔍 Looking for Channel ID: {target_id}")

    try:
        # Bot ရောက်နေသမျှ Group/Channel အကုန်လုံးကို လိုက်စစ်မယ် (ဒါက အဓိက key ပါ)
        async for dialog in bot.get_dialogs():
            print(f"👀 Found Chat: {dialog.chat.title} | ID: {dialog.chat.id}")
            
            # ID တူရင် (သို့) Channel ဖြစ်ရင် Cache ထဲ မှတ်ခိုင်းမယ်
            if dialog.chat.id == target_id:
                found_channel = True
                print("✅ Match found! Cache updated.")
                break
        
        # Loop ပတ်ပြီးမှ တကယ်လှမ်းချိတ်မယ်
        if found_channel:
            chat_info = await bot.get_chat(target_id)
            print(f"🎉 Successfully Connected to: {chat_info.title}")
        else:
            # ID မတူရင်တောင် Admin ဖြစ်နေရင် ID အမှန်ကို Log မှာ ပြပေးလိမ့်မယ်
            print("⚠️ Target ID not found in dialogs. Please check the 'Found Chat' logs above.")
            # ID အမှန်ကို ရှာပြီး get_chat ပြန်စမ်းမယ်
            await bot.get_chat(target_id)

    except Exception as e:
        print(f"❌ Connection Error: {e}")

@app.on_event("shutdown")
async def shutdown(): await bot.stop()

# --- Routes ---

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

# Auth
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

# --- Google Login Routes ---

@app.get("/login/google")
async def login_google(request: Request):
    # Render မှာ တင်ထားတဲ့ ဒိုမိန်းအမှန်ကို တိုက်ရိုက် ရေးထည့်တာ ပိုသေချာပါတယ်
    # (Local မှာ စမ်းရင် 'http://localhost:8000/auth/google' လို့ ပြောင်းသုံးပါ)
    redirect_uri = "https://mediawebapp.onrender.com/auth/google" 
    
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

# Upload (Hybrid: Guest & User)
@app.post("/upload")
async def upload_file(file: UploadFile = File(...), token: Optional[str] = Form(None), parent_id: Optional[str] = Form(None)):
    user = await get_current_user(token)
    
    # Telegram Upload
    target_id = get_target_chat_id(CHANNEL_ID_STR)
    file_uid = str(uuid.uuid4())[:8]
    file_loc = f"temp_{file.filename}"
    
    # aiofiles ဖြင့် သိမ်းခြင်း (Non-blocking)
    async with aiofiles.open(file_loc, "wb") as f:
        while content := await file.read(1024 * 1024):
            await f.write(content)
    
    try:
        msg = await bot.send_document(target_id, file_loc, caption=f"UID: {file_uid}", force_document=True)
        if os.path.exists(file_loc): os.remove(file_loc)

        # Thumbnail ရှိ/မရှိ စစ်ဆေးပြီး ရှိရင် ယူမယ်
        thumb_id = None
        if getattr(msg, "document", None) and getattr(msg.document, "thumbs", None):
            thumb_id = msg.document.thumbs[0].file_id

        file_data = {
            "uid": file_uid,
            "file_id": msg.document.file_id,
            "filename": file.filename,
            "size": msg.document.file_size,
            "upload_date": time.time(),
            "owner": user["username"] if user else None,
            "parent_id": parent_id if (user and parent_id != "root") else None,
            "thumb_id": thumb_id # Thumbnail ID ကို Database မှာ သိမ်းမယ်
        }
        await files_collection.insert_one(file_data)
        
        return {"status": "success", "download_url": f"/dl/{file_uid}", "filename": file.filename}

    except Exception as e:
        # ဒီ except block ပျောက်သွားလို့ Error တက်တာပါ
        if os.path.exists(file_loc): os.remove(file_loc)
        return JSONResponse(status_code=500, content={"error": str(e)})

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
    else:
        query["parent_id"] = None if folder_id == "root" else folder_id

    folders = []
    if not q:
        folder_query = {"owner": user["username"], "parent_id": query.get("parent_id")}
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

@app.put("/api/move")
async def move_item(req: MoveRequest, token: str = Depends(oauth2_scheme)):
    user = await get_current_user(token)
    if not user: raise HTTPException(status_code=401)
    
    # Target Folder ရှိမရှိ စစ်ဆေးခြင်း (Root မဟုတ်ရင်)
    if req.target_parent_id != "root":
        target = await folders_collection.find_one({"uid": req.target_parent_id, "owner": user["username"]})
        if not target: raise HTTPException(status_code=404, detail="Target folder not found")
        
    # ကိုယ့် Folder ကို ကိုယ့်ထဲပြန်ထည့်လို့မရအောင် ကာကွယ်ခြင်း
    if req.type == "folder" and req.uid == req.target_parent_id:
        raise HTTPException(status_code=400, detail="Cannot move folder into itself")

    col = folders_collection if req.type == "folder" else files_collection
    
    # Parent ID ကို Update လုပ်ခြင်း (နေရာရွှေ့ခြင်း)
    new_parent = None if req.target_parent_id == "root" else req.target_parent_id
    
    result = await col.update_one(
        {"uid": req.uid, "owner": user["username"]}, 
        {"$set": {"parent_id": new_parent}}
    )
    
    if result.modified_count == 0:
        raise HTTPException(status_code=400, detail="Move failed")

    return {"message": "Moved successfully"}

@app.delete("/api/delete/{uid}")
async def delete_item(uid: str, type: str, token: str = Depends(oauth2_scheme)):
    user = await get_current_user(token)
    if not user: 
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    if type == "folder":
        # အသစ်ထည့်လိုက်တဲ့ Recursive Function ကို အရင်ခေါ်မယ်
        # ဒါက Folder ထဲက အရာအားလုံးကို ရှင်းပေးလိမ့်မယ်
        await delete_recursive(uid, user["username"])
        
        # ပြီးမှ မိခင် Folder ကြီးကို ဖျက်မယ်
        result = await folders_collection.delete_one({"uid": uid, "owner": user["username"]})

    elif type == "file":
        # File ဆိုရင်တော့ ပုံမှန်အတိုင်း တစ်ခုတည်း ဖျက်မယ်
        result = await files_collection.delete_one({"uid": uid, "owner": user["username"]})
    
    # ဖျက်စရာမတွေ့ရင် Error ပြမယ်
    if result.deleted_count == 0:
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

# --- File Access Routes (Protected) ---
@app.get("/dl/{uid}")
async def download_file(uid: str, pwd: Optional[str] = None):
    file_data = await files_collection.find_one({"uid": uid})
    if not file_data: raise HTTPException(status_code=404, detail="File not found")
    
    # Password စစ်ဆေးခြင်း
    req_pwd = file_data.get("share_password")
    if req_pwd:
        if not pwd: return HTMLResponse(get_password_prompt_html(uid, "dl"))
        if pwd != req_pwd: return HTMLResponse(get_password_prompt_html(uid, "dl", "Incorrect password!"))

    async def streamer():
        async for chunk in bot.stream_media(file_data["file_id"]): yield chunk
    return StreamingResponse(streamer(), media_type="application/octet-stream", headers={"Content-Disposition": f'attachment; filename="{file_data["filename"]}"'})

@app.get("/view/{uid}")
async def view_file(uid: str, pwd: Optional[str] = None):
    file_data = await files_collection.find_one({"uid": uid})
    if not file_data: raise HTTPException(status_code=404, detail="File not found")
    
    # Password စစ်ဆေးခြင်း
    req_pwd = file_data.get("share_password")
    if req_pwd:
        if not pwd: return HTMLResponse(get_password_prompt_html(uid, "view"))
        if pwd != req_pwd: return HTMLResponse(get_password_prompt_html(uid, "view", "Incorrect password!"))

    mime_type, _ = mimetypes.guess_type(file_data["filename"])
    if not mime_type: mime_type = "application/octet-stream"
    
    async def streamer():
        async for chunk in bot.stream_media(file_data["file_id"]): yield chunk
    return StreamingResponse(streamer(), media_type=mime_type, headers={"Content-Disposition": f'inline; filename="{file_data["filename"]}"'})

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

# --- ADMIN ROUTES ---

@app.get("/api/admin/stats")
async def get_admin_stats(admin: dict = Depends(get_current_admin)):
    # 1. Total Files & Users
    total_files = await files_collection.count_documents({})
    total_users = await users_collection.count_documents({})
    
    # 2. Total Storage Used (Aggregation)
    pipeline = [{"$group": {"_id": None, "total_size": {"$sum": "$size"}}}]
    cursor = files_collection.aggregate(pipeline)
    result = await cursor.to_list(length=1)
    total_bytes = result[0]["total_size"] if result else 0
    
    # 3. Recent Uploads (Last 5)
    recent_files = []
    async for f in files_collection.find().sort("upload_date", -1).limit(5):
        recent_files.append({
            "name": f["filename"],
            "owner": f.get("owner", "Guest"),
            "size": f"{round(f['size']/(1024*1024), 2)} MB",
            "date": time.strftime('%Y-%m-%d', time.localtime(f['upload_date']))
        })

    return {
        "total_users": total_users,
        "total_files": total_files,
        "total_storage": f"{round(total_bytes/(1024*1024*1024), 2)} GB",
        "recent_files": recent_files
    }

@app.get("/api/admin/users")
async def get_all_users(admin: dict = Depends(get_current_admin)):
    users = []
    async for u in users_collection.find():
        users.append({
            "username": u["username"],
            "role": u.get("role", "user"),
            "joined": time.strftime('%Y-%m-%d', time.localtime(u.get("created_at", time.time())))
        })
    return users

@app.delete("/api/admin/ban/{username}")
async def ban_user(username: str, admin: dict = Depends(get_current_admin)):
    if username == admin["username"]:
        raise HTTPException(status_code=400, detail="Cannot ban yourself")
    
    # User ကိုဖျက်မည် (သို့မဟုတ် field တစ်ခုထည့်ပြီး lock လုပ်နိုင်သည်)
    await users_collection.delete_one({"username": username})
    # User ပိုင်တဲ့ ဖိုင်တွေကိုပါ ဖျက်ချင်ရင် ဒီမှာ ထပ်ရေးနိုင်ပါတယ်
    return {"message": f"User {username} has been banned/deleted"}

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
