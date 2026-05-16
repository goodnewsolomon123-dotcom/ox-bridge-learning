import os
import requests
from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import create_engine, Column, Integer, String, ForeignKey, Float
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from pydantic import BaseModel
from passlib.context import CryptContext
import jwt
from datetime import datetime, timedelta, timezone

# --- CONFIG ---
DATABASE_URL = os.getenv("DATABASE_URL")
GROQ_KEY = os.getenv("GROQ_KEY")
GEMINI_KEY = os.getenv("GEMINI_KEY")
OR_KEY = os.getenv("OPENROUTER_KEY")
SECRET_KEY = os.getenv("SECRET_KEY", "oxbridge_secret")

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# --- MODELS ---
class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True)
    email = Column(String, unique=True)
    hashed_password = Column(String)
    full_name = Column(String, nullable=True)
    progress_score = Column(Float, default=0.0)

class Subject(Base):
    __tablename__ = "subjects"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String)
    level = Column(String) # e.g., "WASSCE", "NECO"

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

Base.metadata.create_all(bind=engine)

# --- SCHEMAS ---
class UserCreate(BaseModel):
    username: str; email: str; password: str
class SubjectCreate(BaseModel):
    name: str; level: str
class TopicCreate(BaseModel):
    title: str; subject_id: int
class QuestionCreate(BaseModel):
    topic_id: int; question_text: str; option_a: str; option_b: str
    option_c: str; option_d: str; correct_answer: str
class ScoreUpdate(BaseModel):
    username: str; points: float

# --- HYBRID AI ROUTER ---
def get_ai_response(prompt):
    if GROQ_KEY:
        try:
            res = requests.post("https://api.groq.com/openai/v1/chat/completions", 
                headers={"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json"},
                json={"model": "llama-3.1-8b-instant", "messages": [{"role": "user", "content": prompt}]}, timeout=10)
            if res.status_code == 200: return res.json()['choices'][0]['message']['content']
        except: pass
    if GEMINI_KEY:
        try:
            res = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_KEY}",
                json={"contents": [{"parts": [{"text": prompt}]}]}, timeout=10)
            if res.status_code == 200: return res.json()["candidates"][0]["content"]["parts"][0]["text"]
        except: pass
    return "AI services busy."

# --- APP ---
app = FastAPI(title="Ox-Bridge Learning Hub")

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

@app.get("/")
def root():
    return {"status": "Live", "cors_enabled": True}

# --- AUTH ---
@app.post("/signup")
def signup(user: UserCreate, db = Depends(get_db)):
    if db.query(User).filter(User.username == user.username).first():
        raise HTTPException(400, "Username taken")
    db.add(User(username=user.username, email=user.email, hashed_password=pwd_context.hash(user.password)))
    db.commit()
    return {"msg": "User created"}

@app.post("/login")
def login(username: str, password: str, db = Depends(get_db)):
    user = db.query(User).filter(User.username == username).first()
    if not user or not pwd_context.verify(password, user.hashed_password):
        raise HTTPException(401, "Invalid credentials")
    token = jwt.encode({"sub": user.username, "exp": datetime.now(timezone.utc) + timedelta(minutes=60)}, SECRET_KEY)
    return {"access_token": token, "username": user.username}

# --- ADMIN ENDPOINTS (RESTORED) ---
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

@app.post("/admin/add-question")
def add_question(data: QuestionCreate, db = Depends(get_db)):
    q = Question(**data.model_dump())
    db.add(q); db.commit()
    return {"msg": "Question saved"}

# --- USER FEATURES ---
@app.post("/progress/add-score")
def add_score(data: ScoreUpdate, db = Depends(get_db)):
    user = db.query(User).filter(User.username == data.username).first()
    if user:
        user.progress_score += data.points
        db.commit()
        return {"msg": "Score added", "new_total": user.progress_score}
    raise HTTPException(404, "User not found")

@app.get("/learn/{topic}")
def learn(topic: str, level: str = "Secondary", subject: str = "General"):
    lesson = get_ai_response(f"Explain '{topic}' to a {level} {subject} student in 150 words.")
    return {"topic": topic, "lesson": lesson}

@app.get("/quiz/{topic}")
def smart_quiz(topic: str, level: str = "Secondary", subject: str = "General"):
    prompt = f"Generate 3 multiple-choice questions about '{topic}' for a {level} student. Return ONLY a JSON list."
    raw = get_ai_response(prompt)
    return {"topic": topic, "quiz": raw}
