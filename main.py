import os
import json
import requests
from fastapi import FastAPI, Depends, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import create_engine, Column, Integer, String, ForeignKey, Float
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from pydantic import BaseModel
from passlib.context import CryptContext
import jwt
from datetime import datetime, timedelta, timezone

# --- CONFIGURATION ---
DATABASE_URL = os.getenv("DATABASE_URL")
GROQ_KEY = os.getenv("GROQ_KEY")
GEMINI_KEY = os.getenv("GEMINI_KEY")
OR_KEY = os.getenv("OPENROUTER_KEY")
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")
SECRET_KEY = os.getenv("SECRET_KEY", "oxbridge_secret")

# Database Setup
engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# --- DATABASE MODELS ---
class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True)
    email = Column(String, unique=True)
    hashed_password = Column(String)
    full_name = Column(String, nullable=True)
    profile_pic = Column(String, nullable=True)
    bio = Column(String, nullable=True)
    progress_score = Column(Float, default=0.0)
    last_learned_topic = Column(String, nullable=True)

class Subject(Base):
    __tablename__ = "subjects"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String)
    level = Column(String)

class Topic(Base):
    __tablename__ = "topics"
    id = Column(Integer, primary_key=True, index=True)
    title = Column(String)
    subject_id = Column(Integer, ForeignKey("subjects.id"))

class Question(Base):
    __tablename__ = "questions"
    id = Column(Integer, primary_key=True, index=True)
    topic_id = Column(Integer, ForeignKey("topics.id"))
    question_text = Column(String)
    option_a = Column(String); option_b = Column(String)
    option_c = Column(String); option_d = Column(String)
    correct_answer = Column(String)

# FIX: checkfirst=True prevents "Duplicate Table" errors
Base.metadata.create_all(bind=engine, checkfirst=True)

# --- PYDANTIC SCHEMAS ---
class UserCreate(BaseModel):
    username: str; email: str; password: str
class SubjectCreate(BaseModel):
    name: str; level: str
class TopicCreate(BaseModel):
    title: str; subject_id: int
class ScoreUpdate(BaseModel):
    username: str; points: float
class ProfileUpdate(BaseModel):
    full_name: str = None; profile_pic: str = None; bio: str = None

# --- HYBRID AI ROUTER ---
def get_ai_response(prompt):
    # 1. Groq
    if GROQ_KEY:
        try:
            res = requests.post("https://api.groq.com/openai/v1/chat/completions", 
                headers={"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json"},
                json={"model": "llama-3.1-8b-instant", "messages": [{"role": "user", "content": prompt}]}, timeout=15)
            if res.status_code == 200: return res.json()['choices'][0]['message']['content']
        except: pass
    # 2. Gemini
    if GEMINI_KEY:
        try:
            res = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_KEY}",
                json={"contents": [{"parts": [{"text": prompt}]}]}, timeout=15)
            if res.status_code == 200: return res.json()["candidates"][0]["content"]["parts"][0]["text"]
        except: pass
    # 3. OpenRouter
    if OR_KEY:
        try:
            res = requests.post("https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {OR_KEY}", "Content-Type": "application/json"},
                json={"model": "meta-llama/llama-3-8b-instruct:free", "messages": [{"role": "user", "content": prompt}]}, timeout=15)
            if res.status_code == 200: return res.json()['choices'][0]['message']['content']
        except: pass
    return "AI services are currently busy."

# --- FASTAPI APP ---
app = FastAPI(title="Ox-Bridge Learning Hub")

# CORS Setup
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def get_db():
    db = SessionLocal()
    try: yield db
    finally: db.close()

# --- AUTH ENDPOINTS (ROBUST) ---
@app.post("/signup")
def signup(user: UserCreate, db = Depends(get_db)):
    if db.query(User).filter(User.username == user.username).first():
        raise HTTPException(400, "Username already taken")
    hashed_pw = pwd_context.hash(user.password)
    new_user = User(username=user.username, email=user.email, hashed_password=hashed_pw)
    db.add(new_user)
    db.commit()
    return {"msg": "User created successfully! Please login."}

@app.post("/login")
def login(username: str, password: str, db = Depends(get_db)):
    user = db.query(User).filter(User.username == username).first()
    if not user or not pwd_context.verify(password, user.hashed_password):
        raise HTTPException(401, "Invalid username or password")
    token = jwt.encode({"sub": user.username, "exp": datetime.now(timezone.utc) + timedelta(minutes=60)}, SECRET_KEY)
    return {"access_token": token, "username": user.username}

# --- PROFILE & PROGRESS ---
@app.post("/profile/update")
def update_profile(username: str, data: ProfileUpdate, db = Depends(get_db)):
    user = db.query(User).filter(User.username == username).first()
    if not user: raise HTTPException(404, "User not found")
    if data.full_name: user.full_name = data.full_name
    if data.profile_pic: user.profile_pic = data.profile_pic
    if data.bio: user.bio = data.bio
    db.commit()
    return {"msg": "Profile updated"}

@app.get("/profile/{username}")
def get_profile(username: str, db = Depends(get_db)):
    user = db.query(User).filter(User.username == username).first()
    if not user: raise HTTPException(404, "User not found")
    return {
        "username": user.username, "full_name": user.full_name,
        "bio": user.bio, "score": user.progress_score,
        "last_topic": user.last_learned_topic
    }

@app.post("/progress/add-score")
def add_score(data: ScoreUpdate, db = Depends(get_db)):
    user = db.query(User).filter(User.username == data.username).first()
    if user:
        user.progress_score += data.points
        db.commit()
        return {"msg": "Score added", "new_total": user.progress_score}
    raise HTTPException(404, "User not found")

@app.get("/leaderboard")
def get_leaderboard(db = Depends(get_db)):
    top_users = db.query(User).order_by(User.progress_score.desc()).limit(10).all()
    return [{"username": u.username, "score": u.progress_score, "rank": i+1} for i, u in enumerate(top_users)]

# --- ADMIN ENDPOINTS ---
@app.post("/admin/add-subject")
def add_subject(data: SubjectCreate, db = Depends(get_db)):
    s = Subject(name=data.name, level=data.level)
    db.add(s); db.commit()
    return {"id": s.id, "msg": "Subject added"}

@app.post("/admin/add-topic")
def add_topic(data: TopicCreate, db = Depends(get_db)):
    t = Topic(title=data.title, subject_id=data.subject_id)
    db.add(t); db.commit()
    return {"id": t.id, "msg": "Topic added"}

# --- AI LEARNING & QUIZ ---
@app.get("/learn/{topic}")
def learn(topic: str, username: str, level: str = "Secondary", subject: str = "General", db = Depends(get_db)):
    lesson = get_ai_response(f"Explain '{topic}' to a {level} {subject} student in 150 words.")
    user = db.query(User).filter(User.username == username).first()
    if user:
        user.last_learned_topic = topic
        db.commit()
    return {"topic": topic, "lesson": lesson}

@app.get("/quiz/{topic}")
def smart_quiz(topic: str, level: str = "Secondary", subject: str = "General"):
    prompt = f"""
    Generate 3 multiple-choice questions about '{topic}' for a {level} student.
    Return ONLY a JSON list like this:
    [{{"question": "...", "options": ["A) ...", "B) ...", "C) ...", "D) ..."], "answer": "A", "time_limit_sec": 30}}]
    """
    raw = get_ai_response(prompt)
    try:
        clean_raw = raw.replace("```json", "").replace("```", "").strip()
        return {"topic": topic, "quiz": json.loads(clean_raw)}
    except:
        return {"topic": topic, "quiz": raw, "error": "Parsing failed"}

# --- TAVILY SEARCH ---
@app.get("/search/web/{query}")
def search_web(query: str):
    if not TAVILY_API_KEY:
        return {"error": "Tavily API Key missing"}
    url = "https://api.tavily.com/search"
    payload = {
        "api_key": TAVILY_API_KEY,
        "query": query + " education tutorial",
        "search_depth": "basic",
        "max_results": 5
    }
    try:
        res = requests.post(url, json=payload, timeout=10)
        if res.status_code == 200:
            results = []
            for item in res.json()["results"]:
                results.append({
                    "title": item["title"],
                    "url": item["url"],
                    "snippet": item["content"][:150] + "..."
                })
            return {"query": query, "results": results}
    except Exception as e:
        return {"error": f"Search failed: {str(e)}"}
    return {"error": "No results found"}

# --- WEBSOCKET CLASSROOM ---
active_connections = {}

@app.websocket("/ws/classroom/{room}")
async def websocket_endpoint(websocket: WebSocket, room: str):
    await websocket.accept()
    if room not in active_connections:
        active_connections[room] = []
    active_connections[room].append(websocket)
    await websocket.send_json({"type": "system", "message": "Connected to Live Classroom!"})
    
    try:
        while True:
            data = await websocket.receive_json()
            msg = data.get("message")
            user = data.get("username", "Student")
            
            for connection in active_connections[room]:
                await connection.send_json({"type": "chat", "username": user, "message": msg})
                
            if msg.startswith("/ai"):
                query = msg.replace("/ai", "").strip()
                response = get_ai_response(query)
                for connection in active_connections[room]:
                    await connection.send_json({"type": "ai", "username": "Tutor Bot", "message": response})
                    
    except WebSocketDisconnect:
        if websocket in active_connections.get(room, []):
            active_connections[room].remove(websocket)
