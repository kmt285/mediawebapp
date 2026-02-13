import os
import time
import uuid
import uvicorn
from typing import Optional, List
from datetime import datetime, timedelta

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
MONGO_URL = os.environ.get("MONGO_URL")
SECRET_KEY = os.environ.get("SECRET_KEY", "supersecret")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 3000

# --- Setup ---
app = FastAPI()
templates = Jinja2Templates(directory="templates" if os.path.exists("templates") else ".")

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
class RenameRequest(BaseModel):
    uid: str
    new_name: str
    type: str

class CreateFolderRequest(BaseModel):
    name: str
    parent_id: Optional[str] = None

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

# --- Startup ---
@app.on_event("startup")
async def startup():
    await bot.start()
    try:
        # Resolve Channel ID
        cid = int(CHANNEL_ID_STR) if CHANNEL_ID_STR.startswith("-100") else CHANNEL_ID_STR
        await bot.get_chat(cid)
        print("✅ Connected to Telegram Channel")
    except Exception as e:
        print(f"❌ Telegram Error: {e}")

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

# Upload (Hybrid: Guest & User)
@app.post("/upload")
async def upload_file(file: UploadFile = File(...), token: Optional[str] = Form(None), parent_id: Optional[str] = Form(None)):
    user = await get_current_user(token)
    
    # Telegram Upload
    target_id = int(CHANNEL_ID_STR) if CHANNEL_ID_STR.startswith("-100") else CHANNEL_ID_STR
    file_uid = str(uuid.uuid4())[:8]
    file_loc = f"temp_{file.filename}"
    
    with open(file_loc, "wb") as f: f.write(await file.read())
    
    try:
        msg = await bot.send_document(target_id, file_loc, caption=f"UID: {file_uid}", force_document=True)
        if os.path.exists(file_loc): os.remove(file_loc)

        file_data = {
            "uid": file_uid,
            "file_id": msg.document.file_id,
            "filename": file.filename,
            "size": msg.document.file_size,
            "upload_date": time.time(),
            "owner": user["username"] if user else None, # Guest = None
            "parent_id": parent_id if (user and parent_id != "root") else None
        }
        await files_collection.insert_one(file_data)
        
        return {"status": "success", "download_url": f"/dl/{file_uid}", "filename": file.filename}

    except Exception as e:
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
async def get_content(folder_id: Optional[str] = "root", token: str = Depends(oauth2_scheme)):
    user = await get_current_user(token)
    if not user: raise HTTPException(status_code=401)
    
    # Root ဆိုရင် parent_id က None ဖြစ်ရမယ်
    query_id = None if folder_id == "root" else folder_id
    query = {"owner": user["username"], "parent_id": query_id}
    
    folders = [{"uid": f["uid"], "name": f["name"], "type": "folder"} 
               async for f in folders_collection.find(query).sort("name", 1)]
    
    files = []
    async for f in files_collection.find(query).sort("upload_date", -1):
        files.append({
            "uid": f["uid"],
            "name": f["filename"],
            "size": f"{round(f['size']/1024/1024, 2)} MB",
            "type": "file",
            "date": time.strftime('%Y-%m-%d', time.localtime(f['upload_date']))
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

@app.delete("/api/delete/{uid}")
async def delete_item(uid: str, type: str, token: str = Depends(oauth2_scheme)):
    user = await get_current_user(token)
    if not user: raise HTTPException(status_code=401)
    if type == "folder":
        # Check if empty (Prevent deleting non-empty folders for safety)
        if (await files_collection.count_documents({"parent_id": uid}) > 0 or 
            await folders_collection.count_documents({"parent_id": uid}) > 0):
             return JSONResponse(status_code=400, content={"error": "Folder not empty"})
        await folders_collection.delete_one({"uid": uid, "owner": user["username"]})
    else:
        await files_collection.delete_one({"uid": uid, "owner": user["username"]})
    return {"message": "Deleted"}

@app.get("/dl/{uid}")
async def download_file(uid: str):
    file_data = await files_collection.find_one({"uid": uid})
    if not file_data: raise HTTPException(status_code=404, detail="File not found")
    
    async def streamer():
        async for chunk in bot.stream_media(file_data["file_id"]): yield chunk
            
    return StreamingResponse(streamer(), media_type="application/octet-stream", headers={"Content-Disposition": f'attachment; filename="{file_data["filename"]}"'})

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
