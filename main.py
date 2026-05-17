import os
import json
import random
import hashlib
import requests
from fastapi import FastAPI, Depends, HTTPException, WebSocket, WebSocketDisconnect, Request
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import create_engine, Column, Integer, String, ForeignKey, Float, Boolean, text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from pydantic import BaseModel
from passlib.context import CryptContext
import jwt
from datetime import datetime, timedelta, timezone
from typing import Optional, List

# ============================================================
# CONFIGURATION
# ============================================================
DATABASE_URL       = os.getenv("DATABASE_URL")
GROQ_KEY           = os.getenv("GROQ_KEY")
GEMINI_KEY         = os.getenv("GEMINI_KEY")
OR_KEY             = os.getenv("OPENROUTER_KEY")
HF_KEY             = os.getenv("HUGGINGFACE_KEY")
TAVILY_KEY         = os.getenv("TAVILY_API_KEY")
SECRET_KEY         = os.getenv("SECRET_KEY", "oxbridge_secret_2025")
TOKEN_EXPIRE_HOURS = 72

engine       = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base         = declarative_base()
pwd_context  = CryptContext(schemes=["bcrypt"], deprecated="auto", bcrypt__rounds=12)

# ============================================================
# AI RESPONSE CACHE
# ============================================================
ai_cache: dict = {}

def make_cache_key(prompt: str) -> str:
    return hashlib.md5(prompt.strip().lower().encode()).hexdigest()

def get_cached(prompt: str) -> Optional[str]:
    key = make_cache_key(prompt)
    if key in ai_cache:
        print(f"[CACHE HIT] {key[:8]}...")
        return ai_cache[key]
    return None

def set_cache(prompt: str, response: str):
    key = make_cache_key(prompt)
    ai_cache[key] = response
    if len(ai_cache) > 500:
        del ai_cache[next(iter(ai_cache))]

# ============================================================
# DATABASE MODELS
# ============================================================

class User(Base):
    __tablename__ = "users"
    id                 = Column(Integer, primary_key=True, index=True)
    username           = Column(String, unique=True)
    email              = Column(String, unique=True)
    hashed_password    = Column(String)
    full_name          = Column(String, nullable=True)
    profile_pic        = Column(String, nullable=True)
    bio                = Column(String, nullable=True)
    progress_score     = Column(Float,   default=0.0)
    last_learned_topic = Column(String,  nullable=True)
    study_streak       = Column(Integer, default=0)
    last_study_date    = Column(String,  nullable=True)
    coins              = Column(Integer, default=0)

class Subject(Base):
    __tablename__ = "subjects"
    id    = Column(Integer, primary_key=True, index=True)
    name  = Column(String)
    level = Column(String)

class Topic(Base):
    __tablename__ = "topics"
    id         = Column(Integer, primary_key=True, index=True)
    title      = Column(String)
    subject_id = Column(Integer, ForeignKey("subjects.id"))

# ============================================================
# MANUAL QUESTION MODEL
# Standalone — no topic_id needed.
# Add directly via /admin/add-question or Neon dashboard.
# ============================================================
class ManualQuestion(Base):
    __tablename__ = "manual_questions"
    id             = Column(Integer, primary_key=True, index=True)
    subject        = Column(String,  index=True)   # e.g. "Mathematics"
    level          = Column(String,  index=True)   # e.g. "WAEC", "SSS", "JAMB"
    topic          = Column(String,  nullable=True) # e.g. "Algebra"
    question_text  = Column(String)
    option_a       = Column(String)
    option_b       = Column(String)
    option_c       = Column(String)
    option_d       = Column(String)
    correct_answer = Column(String)               # "A", "B", "C", or "D"
    explanation    = Column(String,  nullable=True)
    source         = Column(String,  nullable=True) # "manual", "WAEC 2022", etc.
    added_by       = Column(String,  nullable=True) # admin username
    created_at     = Column(String,  nullable=True)

class Badge(Base):
    __tablename__ = "badges"
    id              = Column(Integer, primary_key=True, index=True)
    name            = Column(String)
    icon            = Column(String)
    description     = Column(String)
    points_required = Column(Float, default=0.0)

class UserBadge(Base):
    __tablename__ = "user_badges"
    id        = Column(Integer, primary_key=True, index=True)
    user_id   = Column(Integer, ForeignKey("users.id"))
    badge_id  = Column(Integer, ForeignKey("badges.id"))
    earned_at = Column(String)

class QuizResult(Base):
    __tablename__ = "quiz_results"
    id              = Column(Integer, primary_key=True, index=True)
    user_id         = Column(Integer, ForeignKey("users.id"))
    subject         = Column(String)
    level           = Column(String)
    topic           = Column(String)
    score           = Column(Integer)
    total_questions = Column(Integer)
    date_taken      = Column(String)

class PastQuestion(Base):
    __tablename__ = "past_questions"
    id             = Column(Integer, primary_key=True, index=True)
    exam_type      = Column(String)
    year           = Column(Integer)
    subject        = Column(String)
    question_text  = Column(String)
    option_a       = Column(String)
    option_b       = Column(String)
    option_c       = Column(String)
    option_d       = Column(String)
    correct_answer = Column(String)
    explanation    = Column(String, nullable=True)

class DailyChallenge(Base):
    __tablename__ = "daily_challenges"
    id             = Column(Integer, primary_key=True, index=True)
    date           = Column(String, unique=True)
    question_text  = Column(String)
    option_a       = Column(String)
    option_b       = Column(String)
    option_c       = Column(String)
    option_d       = Column(String)
    correct_answer = Column(String)

class DailyChallengeAttempt(Base):
    __tablename__ = "daily_challenge_attempts"
    id           = Column(Integer, primary_key=True, index=True)
    user_id      = Column(Integer, ForeignKey("users.id"))
    challenge_id = Column(Integer, ForeignKey("daily_challenges.id"))
    answered_at  = Column(String)
    was_correct  = Column(Boolean, default=False)

class StudySession(Base):
    __tablename__ = "study_sessions"
    id         = Column(Integer, primary_key=True, index=True)
    user_id    = Column(Integer, ForeignKey("users.id"))
    topic      = Column(String)
    subject    = Column(String)
    level      = Column(String)
    studied_at = Column(String)

class Friendship(Base):
    __tablename__ = "friendships"
    id        = Column(Integer, primary_key=True, index=True)
    user_id   = Column(Integer, ForeignKey("users.id"))
    friend_id = Column(Integer, ForeignKey("users.id"))
    status    = Column(String, default="pending")

class Notification(Base):
    __tablename__ = "notifications"
    id         = Column(Integer, primary_key=True, index=True)
    user_id    = Column(Integer, ForeignKey("users.id"))
    message    = Column(String)
    is_read    = Column(Boolean, default=False)
    created_at = Column(String)

class GameScore(Base):
    __tablename__ = "game_scores"
    id        = Column(Integer, primary_key=True, index=True)
    user_id   = Column(Integer, ForeignKey("users.id"))
    game_type = Column(String)
    score     = Column(Integer)
    played_at = Column(String)

# ============================================================
# CREATE TABLES + MIGRATIONS
# ============================================================
Base.metadata.create_all(bind=engine, checkfirst=True)

def run_migrations():
    migrations = [
        # users
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS full_name VARCHAR;",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS profile_pic VARCHAR;",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS bio VARCHAR;",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS progress_score FLOAT DEFAULT 0.0;",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_learned_topic VARCHAR;",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS study_streak INTEGER DEFAULT 0;",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_study_date VARCHAR;",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS coins INTEGER DEFAULT 0;",
        # manual_questions (new table columns safety)
        "ALTER TABLE manual_questions ADD COLUMN IF NOT EXISTS source VARCHAR;",
        "ALTER TABLE manual_questions ADD COLUMN IF NOT EXISTS added_by VARCHAR;",
        "ALTER TABLE manual_questions ADD COLUMN IF NOT EXISTS created_at VARCHAR;",
        "ALTER TABLE manual_questions ADD COLUMN IF NOT EXISTS explanation VARCHAR;",
        "ALTER TABLE manual_questions ADD COLUMN IF NOT EXISTS topic VARCHAR;",
    ]
    with engine.connect() as conn:
        for sql in migrations:
            try:
                conn.execute(text(sql))
            except Exception as e:
                print(f"[MIGRATION] Skipped: {e}")
        conn.commit()

run_migrations()

def seed_badges():
    db = SessionLocal()
    try:
        if db.query(Badge).count() == 0:
            db.add_all([
                Badge(name="First Step",    icon="👣", description="Created your account",      points_required=0),
                Badge(name="Quiz Starter",  icon="🧠", description="Completed your first quiz", points_required=1),
                Badge(name="Rising Star",   icon="⭐", description="Reached 10 points",         points_required=10),
                Badge(name="Scholar",       icon="📚", description="Reached 50 points",         points_required=50),
                Badge(name="Champion",      icon="🏆", description="Reached 100 points",        points_required=100),
                Badge(name="Legend",        icon="🔥", description="Reached 500 points",        points_required=500),
                Badge(name="Streak Master", icon="💫", description="7-day study streak",        points_required=0),
                Badge(name="Speed Learner", icon="⚡", description="Completed 10 quizzes",      points_required=0),
            ])
            db.commit()
    finally:
        db.close()

seed_badges()

# ============================================================
# HELPERS
# ============================================================
def now_str():   return datetime.now(timezone.utc).isoformat()
def today_str(): return datetime.now(timezone.utc).strftime("%Y-%m-%d")

def hash_password(pw: str) -> str:   return pwd_context.hash(pw)
def verify_password(plain: str, hashed: str) -> bool:
    try:    return pwd_context.verify(plain, hashed)
    except: return False

def create_token(username: str) -> str:
    exp = datetime.now(timezone.utc) + timedelta(hours=TOKEN_EXPIRE_HOURS)
    return jwt.encode({"sub": username, "exp": exp}, SECRET_KEY, algorithm="HS256")

def decode_token(token: str) -> Optional[str]:
    try:    return jwt.decode(token, SECRET_KEY, algorithms=["HS256"]).get("sub")
    except: return None

def check_and_award_badges(user_id: int, db):
    user       = db.query(User).filter(User.id == user_id).first()
    if not user: return []
    earned_ids = {ub.badge_id for ub in db.query(UserBadge).filter(UserBadge.user_id == user_id).all()}
    all_badges = db.query(Badge).all()
    quiz_count = db.query(QuizResult).filter(QuizResult.user_id == user_id).count()
    newly = []
    for b in all_badges:
        if b.id in earned_ids: continue
        award = False
        if b.name == "Rising Star"   and (user.progress_score or 0) >= 10:  award = True
        if b.name == "Scholar"       and (user.progress_score or 0) >= 50:  award = True
        if b.name == "Champion"      and (user.progress_score or 0) >= 100: award = True
        if b.name == "Legend"        and (user.progress_score or 0) >= 500: award = True
        if b.name == "Streak Master" and (user.study_streak   or 0) >= 7:   award = True
        if b.name == "Speed Learner" and quiz_count >= 10:                   award = True
        if award:
            db.add(UserBadge(user_id=user_id, badge_id=b.id, earned_at=now_str()))
            db.add(Notification(user_id=user_id, message=f"🏅 You earned the '{b.name}' badge!", created_at=now_str()))
            newly.append(b.name)
    if newly: db.commit()
    return newly

def update_streak(user: User, db):
    today = today_str()
    if user.last_study_date == today: return
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    user.study_streak    = ((user.study_streak or 0) + 1) if user.last_study_date == yesterday else 1
    user.last_study_date = today
    db.commit()

# ============================================================
# HYBRID AI ROUTER — 4 PROVIDERS + CACHE
# ============================================================
def get_ai_response(prompt: str) -> str:
    cached = get_cached(prompt)
    if cached:
        return cached

    result = None

    # 1. Groq (fastest)
    if GROQ_KEY and not result:
        try:
            res = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json"},
                json={"model": "llama-3.1-8b-instant", "messages": [{"role": "user", "content": prompt}]},
                timeout=15
            )
            if res.status_code == 200:
                result = res.json()['choices'][0]['message']['content']
                print("[AI] Groq ✓")
        except Exception as e: print(f"[AI] Groq ✗: {e}")

    # 2. Gemini
    if GEMINI_KEY and not result:
        try:
            res = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_KEY}",
                json={"contents": [{"parts": [{"text": prompt}]}]},
                timeout=15
            )
            if res.status_code == 200:
                result = res.json()["candidates"][0]["content"]["parts"][0]["text"]
                print("[AI] Gemini ✓")
        except Exception as e: print(f"[AI] Gemini ✗: {e}")

    # 3. OpenRouter
    if OR_KEY and not result:
        try:
            res = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {OR_KEY}", "Content-Type": "application/json"},
                json={"model": "meta-llama/llama-3-8b-instruct:free", "messages": [{"role": "user", "content": prompt}]},
                timeout=15
            )
            if res.status_code == 200:
                result = res.json()['choices'][0]['message']['content']
                print("[AI] OpenRouter ✓")
        except Exception as e: print(f"[AI] OpenRouter ✗: {e}")

    # 4. HuggingFace (fallback)
    if HF_KEY and not result:
        try:
            res = requests.post(
                "https://api-inference.huggingface.co/models/mistralai/Mistral-7B-Instruct-v0.2",
                headers={"Authorization": f"Bearer {HF_KEY}", "Content-Type": "application/json"},
                json={"inputs": f"[INST] {prompt} [/INST]", "parameters": {"max_new_tokens": 600, "temperature": 0.7, "return_full_text": False}},
                timeout=25
            )
            if res.status_code == 200:
                data = res.json()
                if isinstance(data, list) and data:
                    result = data[0].get("generated_text", "").strip()
                if result: print("[AI] HuggingFace ✓")
        except Exception as e: print(f"[AI] HuggingFace ✗: {e}")

    if not result:
        result = "AI services are currently busy. Please try again in a moment."

    if "busy" not in result:
        set_cache(prompt, result)

    return result

# ============================================================
# FASTAPI APP
# ============================================================
app = FastAPI(title="Ox-Bridge Learning Hub API", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def get_db():
    db = SessionLocal()
    try:    yield db
    finally: db.close()

# ============================================================
# PYDANTIC SCHEMAS
# ============================================================
class UserCreate(BaseModel):
    username: str
    email:    str
    password: str

class SubjectCreate(BaseModel):
    name:  str
    level: str

class TopicCreate(BaseModel):
    title:      str
    subject_id: int

class ScoreUpdate(BaseModel):
    username: str
    points:   float

class ProfileUpdate(BaseModel):
    full_name:   Optional[str] = None
    profile_pic: Optional[str] = None
    bio:         Optional[str] = None

class QuizResultCreate(BaseModel):
    username:        str
    subject:         str
    level:           str
    topic:           str
    score:           int
    total_questions: int

class ManualQuestionCreate(BaseModel):
    subject:        str
    level:          str
    topic:          Optional[str] = None
    question_text:  str
    option_a:       str
    option_b:       str
    option_c:       str
    option_d:       str
    correct_answer: str           # "A", "B", "C", or "D"
    explanation:    Optional[str] = None
    source:         Optional[str] = "manual"
    added_by:       Optional[str] = "admin"

class BulkQuestionsCreate(BaseModel):
    questions: List[ManualQuestionCreate]

class PastQuestionCreate(BaseModel):
    exam_type:      str
    year:           int
    subject:        str
    question_text:  str
    option_a:       str
    option_b:       str
    option_c:       str
    option_d:       str
    correct_answer: str
    explanation:    Optional[str] = None

class DailyChallengeSubmit(BaseModel):
    username: str
    answer:   str

class StudyLog(BaseModel):
    username: str
    topic:    str
    subject:  str
    level:    str

class FriendRequest(BaseModel):
    username:        str
    friend_username: str

class StudyPlanRequest(BaseModel):
    username:      str
    level:         str
    subjects:      str
    exam_date:     str
    hours_per_day: int

class AnswerCheckRequest(BaseModel):
    question:       str
    student_answer: str
    correct_answer: str
    subject:        str

class GameScoreSave(BaseModel):
    username:  str
    game_type: str
    score:     int

class TokenValidate(BaseModel):
    token: str

# ============================================================
# ROOT — FIXED: supports HEAD for UptimeRobot
# ============================================================
@app.api_route("/", methods=["GET", "HEAD"])
def root():
    return {
        "app":           "Ox-Bridge Learning Hub",
        "version":       "2.0.0",
        "status":        "running",
        "message":       "Powered by Ox-Bridge Technology 🇳🇬",
        "ai_providers":  ["Groq", "Gemini", "OpenRouter", "HuggingFace"],
        "cache_size":    len(ai_cache),
        "manual_q_info": "Add questions via POST /admin/add-question or Neon dashboard"
    }

# ============================================================
# AUTH
# ============================================================
@app.post("/signup")
def signup(user: UserCreate, db=Depends(get_db)):
    if len(user.username.strip()) < 3:
        raise HTTPException(400, "Username must be at least 3 characters")
    if len(user.password) < 6:
        raise HTTPException(400, "Password must be at least 6 characters")
    if "@" not in user.email:
        raise HTTPException(400, "Invalid email address")
    if db.query(User).filter(User.username == user.username.strip()).first():
        raise HTTPException(400, "Username already taken")
    if db.query(User).filter(User.email == user.email.strip().lower()).first():
        raise HTTPException(400, "Email already registered")

    new_user = User(
        username        = user.username.strip(),
        email           = user.email.strip().lower(),
        hashed_password = hash_password(user.password),
        progress_score  = 0.0,
        study_streak    = 0,
        coins           = 0
    )
    db.add(new_user); db.commit(); db.refresh(new_user)

    # First Step badge
    fs = db.query(Badge).filter(Badge.name == "First Step").first()
    if fs:
        db.add(UserBadge(user_id=new_user.id, badge_id=fs.id, earned_at=now_str()))
        db.add(Notification(user_id=new_user.id, message="👣 Welcome! You earned the 'First Step' badge!", created_at=now_str()))
        db.commit()

    return {"msg": "Account created successfully! Please login.", "username": new_user.username}


@app.post("/login")
def login(username: str, password: str, db=Depends(get_db)):
    username = username.strip()
    password = password.strip()

    if not username or not password:
        raise HTTPException(400, "Username and password are required")

    user = db.query(User).filter(User.username.ilike(username)).first()
    if not user:
        print(f"[LOGIN] User not found: '{username}'")
        raise HTTPException(401, "No account found with that username. Please sign up first.")

    ok = verify_password(password, user.hashed_password)
    print(f"[LOGIN] '{username}' — password match: {ok}")

    if not ok:
        raise HTTPException(401, "Wrong password. Please try again.")

    return {
        "access_token": create_token(user.username),
        "token_type":   "bearer",
        "username":     user.username,
        "score":        user.progress_score or 0,
        "streak":       user.study_streak   or 0,
        "coins":        user.coins          or 0,
        "expires_in":   f"{TOKEN_EXPIRE_HOURS} hours"
    }


@app.post("/validate-token")
def validate_token(data: TokenValidate, db=Depends(get_db)):
    username = decode_token(data.token)
    if not username:
        raise HTTPException(401, "Token expired or invalid. Please login again.")
    user = db.query(User).filter(User.username == username).first()
    if not user:
        raise HTTPException(401, "User not found. Please login again.")
    return {
        "valid":    True,
        "username": user.username,
        "score":    user.progress_score or 0,
        "streak":   user.study_streak   or 0,
        "coins":    user.coins          or 0
    }

# ============================================================
# PROFILE & PROGRESS
# ============================================================
@app.get("/profile/{username}")
def get_profile(username: str, db=Depends(get_db)):
    user = db.query(User).filter(User.username == username).first()
    if not user: raise HTTPException(404, "User not found")
    badges     = db.query(UserBadge).filter(UserBadge.user_id == user.id).all()
    badge_list = []
    for ub in badges:
        b = db.query(Badge).filter(Badge.id == ub.badge_id).first()
        if b: badge_list.append({"name": b.name, "icon": b.icon, "earned_at": ub.earned_at})
    return {
        "username":    user.username, "full_name":   user.full_name,
        "bio":         user.bio,      "profile_pic": user.profile_pic,
        "score":       user.progress_score or 0,
        "streak":      user.study_streak   or 0,
        "coins":       user.coins          or 0,
        "last_topic":  user.last_learned_topic,
        "quiz_count":  db.query(QuizResult).filter(QuizResult.user_id == user.id).count(),
        "badges":      badge_list
    }

@app.post("/profile/update")
def update_profile(username: str, data: ProfileUpdate, db=Depends(get_db)):
    user = db.query(User).filter(User.username == username).first()
    if not user: raise HTTPException(404, "User not found")
    if data.full_name   is not None: user.full_name   = data.full_name
    if data.profile_pic is not None: user.profile_pic = data.profile_pic
    if data.bio         is not None: user.bio         = data.bio
    db.commit()
    return {"msg": "Profile updated successfully"}

@app.post("/progress/add-score")
def add_score(data: ScoreUpdate, db=Depends(get_db)):
    user = db.query(User).filter(User.username == data.username).first()
    if not user: raise HTTPException(404, "User not found")
    user.progress_score = (user.progress_score or 0) + data.points
    user.coins          = (user.coins          or 0) + int(data.points)
    db.commit()
    newly = check_and_award_badges(user.id, db)
    return {"msg": "Score added", "new_total": user.progress_score, "coins": user.coins, "new_badges": newly or []}

@app.get("/leaderboard")
def get_leaderboard(db=Depends(get_db)):
    top = db.query(User).order_by(User.progress_score.desc()).limit(10).all()
    return [{"rank": i+1, "username": u.username, "score": u.progress_score or 0, "streak": u.study_streak or 0, "coins": u.coins or 0} for i, u in enumerate(top)]

# ============================================================
# BADGES
# ============================================================
@app.get("/badges/all")
def get_all_badges(db=Depends(get_db)):
    return [{"id": b.id, "name": b.name, "icon": b.icon, "description": b.description, "points_required": b.points_required} for b in db.query(Badge).all()]

@app.get("/badges/{username}")
def get_user_badges(username: str, db=Depends(get_db)):
    user = db.query(User).filter(User.username == username).first()
    if not user: raise HTTPException(404, "User not found")
    rows = db.query(UserBadge).filter(UserBadge.user_id == user.id).all()
    result = []
    for ub in rows:
        b = db.query(Badge).filter(Badge.id == ub.badge_id).first()
        if b: result.append({"name": b.name, "icon": b.icon, "description": b.description, "earned_at": ub.earned_at})
    return result

# ============================================================
# MANUAL QUESTIONS — ADMIN MANAGEMENT
# No need to touch app.py again — use these endpoints!
# ============================================================

@app.post("/admin/add-question")
def add_question(data: ManualQuestionCreate, db=Depends(get_db)):
    """
    Add a single question manually.
    You can also add directly in Neon dashboard → manual_questions table.
    """
    q = ManualQuestion(
        subject        = data.subject.strip(),
        level          = data.level.strip(),
        topic          = data.topic.strip() if data.topic else None,
        question_text  = data.question_text.strip(),
        option_a       = data.option_a.strip(),
        option_b       = data.option_b.strip(),
        option_c       = data.option_c.strip(),
        option_d       = data.option_d.strip(),
        correct_answer = data.correct_answer.upper().strip(),
        explanation    = data.explanation,
        source         = data.source or "manual",
        added_by       = data.added_by or "admin",
        created_at     = now_str()
    )
    db.add(q); db.commit(); db.refresh(q)
    return {"msg": "Question added successfully", "id": q.id, "subject": q.subject, "level": q.level}


@app.post("/admin/add-questions-bulk")
def add_questions_bulk(data: BulkQuestionsCreate, db=Depends(get_db)):
    """
    Add multiple questions at once.
    Send a JSON list of questions.
    """
    added = []
    for qd in data.questions:
        q = ManualQuestion(
            subject        = qd.subject.strip(),
            level          = qd.level.strip(),
            topic          = qd.topic.strip() if qd.topic else None,
            question_text  = qd.question_text.strip(),
            option_a       = qd.option_a.strip(),
            option_b       = qd.option_b.strip(),
            option_c       = qd.option_c.strip(),
            option_d       = qd.option_d.strip(),
            correct_answer = qd.correct_answer.upper().strip(),
            explanation    = qd.explanation,
            source         = qd.source or "manual",
            added_by       = qd.added_by or "admin",
            created_at     = now_str()
        )
        db.add(q)
        added.append({"subject": q.subject, "level": q.level})
    db.commit()
    return {"msg": f"{len(added)} questions added successfully", "questions": added}


@app.get("/admin/questions")
def list_questions(subject: str = None, level: str = None, limit: int = 50, db=Depends(get_db)):
    """
    View all manual questions. Filter by subject and/or level.
    """
    query = db.query(ManualQuestion)
    if subject: query = query.filter(ManualQuestion.subject.ilike(f"%{subject}%"))
    if level:   query = query.filter(ManualQuestion.level.ilike(f"%{level}%"))
    questions = query.limit(limit).all()
    return {
        "total": query.count(),
        "questions": [
            {"id": q.id, "subject": q.subject, "level": q.level,
             "topic": q.topic, "question_text": q.question_text,
             "correct_answer": q.correct_answer, "source": q.source}
            for q in questions
        ]
    }


@app.delete("/admin/question/{question_id}")
def delete_question(question_id: int, db=Depends(get_db)):
    """
    Delete a question by ID.
    """
    q = db.query(ManualQuestion).filter(ManualQuestion.id == question_id).first()
    if not q: raise HTTPException(404, "Question not found")
    db.delete(q); db.commit()
    return {"msg": f"Question {question_id} deleted"}


@app.get("/admin/questions/count")
def count_questions(db=Depends(get_db)):
    """
    Quick count of all manual questions by subject.
    """
    all_q = db.query(ManualQuestion).all()
    counts = {}
    for q in all_q:
        key = f"{q.subject} ({q.level})"
        counts[key] = counts.get(key, 0) + 1
    return {"total": len(all_q), "by_subject_level": counts}


# ============================================================
# QUIZ — DATABASE FIRST, AI FALLBACK
# ============================================================
@app.get("/quiz/{topic}")
def smart_quiz(topic: str, level: str = "SSS", subject: str = "General", db=Depends(get_db)):
    """
    1. Checks manual_questions database first
    2. If 5+ questions found → serves from database (no AI cost!)
    3. If fewer than 5 → generates with AI (cached for next time)
    """
    # Step 1: Query database for matching questions
    db_questions = db.query(ManualQuestion).filter(
        ManualQuestion.subject.ilike(f"%{subject}%"),
        ManualQuestion.level.ilike(f"%{level}%")
    ).all()

    # Also try matching by topic if provided
    if topic.lower() != subject.lower():
        topic_questions = db.query(ManualQuestion).filter(
            ManualQuestion.topic.ilike(f"%{topic}%")
        ).all()
        # Merge and deduplicate
        all_ids    = {q.id for q in db_questions}
        db_questions = db_questions + [q for q in topic_questions if q.id not in all_ids]

    # Step 2: If we have enough DB questions, use them
    if len(db_questions) >= 5:
        selected = random.sample(db_questions, min(5, len(db_questions)))
        quiz     = []
        for q in selected:
            quiz.append({
                "question":       q.question_text,
                "options":        [f"A) {q.option_a}", f"B) {q.option_b}", f"C) {q.option_c}", f"D) {q.option_d}"],
                "answer":         q.correct_answer,
                "explanation":    q.explanation or "",
                "time_limit_sec": 30,
                "source":         "database"
            })
        print(f"[QUIZ] Served {len(quiz)} questions from database for {subject} {level}")
        return {"topic": topic, "level": level, "subject": subject, "quiz": quiz, "source": "database"}

    # Step 3: Not enough DB questions — use AI (result is cached)
    print(f"[QUIZ] Only {len(db_questions)} DB questions found. Using AI for {subject} {level} - {topic}")
    prompt = f"""Generate 5 multiple-choice questions about '{topic}' for a Nigerian {level} {subject} student.
    Return ONLY a JSON list:
    [{{"question":"...","options":["A) ...","B) ...","C) ...","D) ..."],"answer":"A","explanation":"...","time_limit_sec":30}}]"""
    raw = get_ai_response(prompt)
    try:
        clean = raw.replace("```json", "").replace("```", "").strip()
        quiz  = json.loads(clean)
        for q in quiz: q["source"] = "ai"
        return {"topic": topic, "level": level, "subject": subject, "quiz": quiz, "source": "ai"}
    except:
        return {"topic": topic, "quiz": raw, "error": "Parsing failed", "source": "ai"}


# ============================================================
# QUIZ SCORE TRACKING
# ============================================================
@app.post("/quiz/save-result")
def save_quiz_result(data: QuizResultCreate, db=Depends(get_db)):
    user = db.query(User).filter(User.username == data.username).first()
    if not user: raise HTTPException(404, "User not found")
    db.add(QuizResult(user_id=user.id, subject=data.subject, level=data.level,
        topic=data.topic, score=data.score, total_questions=data.total_questions, date_taken=now_str()))
    points              = data.score * 5
    user.progress_score = (user.progress_score or 0) + points
    user.coins          = (user.coins          or 0) + points
    db.commit()
    newly = check_and_award_badges(user.id, db)
    quiz_count = db.query(QuizResult).filter(QuizResult.user_id == user.id).count()
    if quiz_count == 1:
        qs = db.query(Badge).filter(Badge.name == "Quiz Starter").first()
        if qs and not db.query(UserBadge).filter(UserBadge.user_id == user.id, UserBadge.badge_id == qs.id).first():
            db.add(UserBadge(user_id=user.id, badge_id=qs.id, earned_at=now_str()))
            db.add(Notification(user_id=user.id, message="🧠 You earned the 'Quiz Starter' badge!", created_at=now_str()))
            db.commit()
            if "Quiz Starter" not in newly: newly.append("Quiz Starter")
    return {"msg": "Quiz result saved", "points_earned": points, "new_total": user.progress_score, "new_badges": newly or []}

@app.get("/quiz/history/{username}")
def quiz_history(username: str, db=Depends(get_db)):
    user = db.query(User).filter(User.username == username).first()
    if not user: raise HTTPException(404, "User not found")
    results = db.query(QuizResult).filter(QuizResult.user_id == user.id).order_by(QuizResult.id.desc()).limit(20).all()
    return [{"subject": r.subject, "level": r.level, "topic": r.topic, "score": r.score,
             "total_questions": r.total_questions,
             "percentage": round((r.score/r.total_questions)*100) if r.total_questions else 0,
             "date_taken": r.date_taken} for r in results]

@app.get("/quiz/stats/{username}")
def quiz_stats(username: str, db=Depends(get_db)):
    user = db.query(User).filter(User.username == username).first()
    if not user: raise HTTPException(404, "User not found")
    results = db.query(QuizResult).filter(QuizResult.user_id == user.id).all()
    if not results: return {"total_quizzes": 0, "average_score": 0, "best_subject": None}
    total   = len(results)
    avg     = sum(r.score/r.total_questions*100 for r in results if r.total_questions) / total
    sscores: dict = {}
    for r in results:
        if r.total_questions: sscores.setdefault(r.subject, []).append(r.score/r.total_questions*100)
    best = max(sscores, key=lambda s: sum(sscores[s])/len(sscores[s])) if sscores else None
    return {"total_quizzes": total, "average_score": round(avg, 1), "best_subject": best,
            "subject_scores": {s: round(sum(v)/len(v), 1) for s, v in sscores.items()}}

# ============================================================
# PAST QUESTIONS
# ============================================================
@app.post("/admin/add-past-question")
def add_past_question(data: PastQuestionCreate, db=Depends(get_db)):
    q = PastQuestion(**data.dict()); db.add(q); db.commit()
    return {"msg": "Past question added", "id": q.id}

@app.get("/past-questions/{exam_type}/{subject}")
def get_past_questions(exam_type: str, subject: str, year: int = None, db=Depends(get_db)):
    query = db.query(PastQuestion).filter(PastQuestion.exam_type == exam_type.upper(), PastQuestion.subject == subject)
    if year: query = query.filter(PastQuestion.year == year)
    return [{"id": q.id, "year": q.year, "question_text": q.question_text,
             "option_a": q.option_a, "option_b": q.option_b, "option_c": q.option_c, "option_d": q.option_d,
             "correct_answer": q.correct_answer, "explanation": q.explanation} for q in query.limit(20).all()]

@app.get("/past-questions/random/{subject}")
def random_past_question(subject: str, exam: str = "WAEC", db=Depends(get_db)):
    questions = db.query(PastQuestion).filter(PastQuestion.exam_type == exam.upper(), PastQuestion.subject == subject).all()
    if not questions:
        prompt = f"""Generate 1 {exam} past question style MCQ for {subject} (Nigerian curriculum).
        Return ONLY JSON: {{"question_text":"...","option_a":"...","option_b":"...","option_c":"...","option_d":"...","correct_answer":"A","explanation":"..."}}"""
        raw = get_ai_response(prompt)
        try: return {"source": "ai_generated", "question": json.loads(raw.replace("```json","").replace("```","").strip())}
        except: return {"error": "No questions found"}
    q = random.choice(questions)
    return {"source": "database", "question": {"id": q.id, "year": q.year, "question_text": q.question_text,
            "option_a": q.option_a, "option_b": q.option_b, "option_c": q.option_c, "option_d": q.option_d,
            "correct_answer": q.correct_answer, "explanation": q.explanation}}

# ============================================================
# DAILY CHALLENGE
# ============================================================
@app.get("/daily-challenge")
def get_daily_challenge(db=Depends(get_db)):
    today    = today_str()
    existing = db.query(DailyChallenge).filter(DailyChallenge.date == today).first()
    if not existing:
        subjects = ["Mathematics","English Language","Biology","Physics","Chemistry","Government","Economics"]
        prompt   = f"""Generate 1 multiple-choice question about {random.choice(subjects)} for Nigerian secondary school students.
        Return ONLY JSON: {{"question_text":"...","option_a":"...","option_b":"...","option_c":"...","option_d":"...","correct_answer":"A"}}"""
        raw = get_ai_response(prompt)
        try:
            q        = json.loads(raw.replace("```json","").replace("```","").strip())
            existing = DailyChallenge(date=today, question_text=q["question_text"],
                option_a=q["option_a"], option_b=q["option_b"],
                option_c=q["option_c"], option_d=q["option_d"], correct_answer=q["correct_answer"])
            db.add(existing); db.commit(); db.refresh(existing)
        except: return {"error": "Could not generate daily challenge. Try again shortly."}
    return {"id": existing.id, "date": existing.date, "question_text": existing.question_text,
            "option_a": existing.option_a, "option_b": existing.option_b,
            "option_c": existing.option_c, "option_d": existing.option_d}

@app.post("/daily-challenge/submit")
def submit_daily_challenge(data: DailyChallengeSubmit, db=Depends(get_db)):
    user = db.query(User).filter(User.username == data.username).first()
    if not user: raise HTTPException(404, "User not found")
    challenge = db.query(DailyChallenge).filter(DailyChallenge.date == today_str()).first()
    if not challenge: raise HTTPException(404, "No challenge today")
    already = db.query(DailyChallengeAttempt).filter(
        DailyChallengeAttempt.user_id == user.id, DailyChallengeAttempt.challenge_id == challenge.id).first()
    if already: return {"msg": "Already attempted today!", "already_attempted": True, "correct_answer": challenge.correct_answer}
    is_correct = data.answer.upper().strip() == challenge.correct_answer.upper().strip()
    db.add(DailyChallengeAttempt(user_id=user.id, challenge_id=challenge.id, answered_at=now_str(), was_correct=is_correct))
    if is_correct:
        user.progress_score = (user.progress_score or 0) + 5
        user.coins          = (user.coins          or 0) + 10
        db.add(Notification(user_id=user.id, message="🎉 Daily challenge correct! +5 points, +10 coins", created_at=now_str()))
    db.commit()
    return {"correct": is_correct, "correct_answer": challenge.correct_answer,
            "points_earned": 5 if is_correct else 0, "coins_earned": 10 if is_correct else 0}

# ============================================================
# STUDY HISTORY & STREAK
# ============================================================
@app.post("/study/log")
def log_study_session(data: StudyLog, db=Depends(get_db)):
    user = db.query(User).filter(User.username == data.username).first()
    if not user: raise HTTPException(404, "User not found")
    db.add(StudySession(user_id=user.id, topic=data.topic, subject=data.subject, level=data.level, studied_at=now_str()))
    user.last_learned_topic = data.topic
    update_streak(user, db)
    newly = check_and_award_badges(user.id, db)
    return {"msg": "Study session logged", "streak": user.study_streak, "new_badges": newly or []}

@app.get("/study/history/{username}")
def study_history(username: str, db=Depends(get_db)):
    user = db.query(User).filter(User.username == username).first()
    if not user: raise HTTPException(404, "User not found")
    sessions = db.query(StudySession).filter(StudySession.user_id == user.id).order_by(StudySession.id.desc()).limit(20).all()
    return [{"topic": s.topic, "subject": s.subject, "level": s.level, "studied_at": s.studied_at} for s in sessions]

@app.get("/study/streak/{username}")
def get_streak(username: str, db=Depends(get_db)):
    user = db.query(User).filter(User.username == username).first()
    if not user: raise HTTPException(404, "User not found")
    return {"username": username, "streak": user.study_streak or 0, "last_study_date": user.last_study_date}

# ============================================================
# FRIENDS
# ============================================================
@app.post("/friends/add")
def add_friend(data: FriendRequest, db=Depends(get_db)):
    user   = db.query(User).filter(User.username == data.username).first()
    friend = db.query(User).filter(User.username == data.friend_username).first()
    if not user or not friend: raise HTTPException(404, "User not found")
    if db.query(Friendship).filter(Friendship.user_id == user.id, Friendship.friend_id == friend.id).first():
        return {"msg": "Friend request already sent"}
    db.add(Friendship(user_id=user.id, friend_id=friend.id, status="pending"))
    db.add(Notification(user_id=friend.id, message=f"👋 {user.username} sent you a friend request!", created_at=now_str()))
    db.commit()
    return {"msg": f"Friend request sent to {data.friend_username}"}

@app.post("/friends/accept")
def accept_friend(data: FriendRequest, db=Depends(get_db)):
    user   = db.query(User).filter(User.username == data.username).first()
    friend = db.query(User).filter(User.username == data.friend_username).first()
    if not user or not friend: raise HTTPException(404, "User not found")
    req = db.query(Friendship).filter(Friendship.user_id == friend.id, Friendship.friend_id == user.id).first()
    if not req: raise HTTPException(404, "Friend request not found")
    req.status = "accepted"
    db.add(Friendship(user_id=user.id, friend_id=friend.id, status="accepted"))
    db.add(Notification(user_id=friend.id, message=f"🤝 {user.username} accepted your friend request!", created_at=now_str()))
    db.commit()
    return {"msg": "Friend request accepted"}

@app.get("/friends/{username}")
def get_friends(username: str, db=Depends(get_db)):
    user = db.query(User).filter(User.username == username).first()
    if not user: raise HTTPException(404, "User not found")
    fs = db.query(Friendship).filter(Friendship.user_id == user.id, Friendship.status == "accepted").all()
    result = []
    for f in fs:
        fr = db.query(User).filter(User.id == f.friend_id).first()
        if fr: result.append({"username": fr.username, "score": fr.progress_score or 0, "streak": fr.study_streak or 0})
    return result

# ============================================================
# AI FEATURES
# ============================================================
@app.post("/ai/study-plan")
def ai_study_plan(data: StudyPlanRequest):
    prompt = f"""Create a weekly study plan for a Nigerian {data.level} student.
    Subjects: {data.subjects}. Exam date: {data.exam_date}. Hours/day: {data.hours_per_day}.
    Focus on Nigerian curriculum (WAEC/JAMB). Day-by-day schedule with specific topics."""
    return {"username": data.username, "study_plan": get_ai_response(prompt)}

@app.post("/ai/check-answer")
def check_answer(data: AnswerCheckRequest):
    prompt = f"""A Nigerian student answered a {data.subject} question.
    Question: {data.question}. Student: {data.student_answer}. Correct: {data.correct_answer}.
    Tell them if right or wrong, explain why simply, give memory tip. Be encouraging."""
    return {"is_correct": data.student_answer.strip().upper() == data.correct_answer.strip().upper(),
            "feedback": get_ai_response(prompt)}

# ============================================================
# LEARN
# ============================================================
@app.get("/learn/{topic}")
def learn(topic: str, username: str, level: str = "SSS", subject: str = "General", db=Depends(get_db)):
    prompt = f"Explain '{topic}' to a Nigerian {level} {subject} student in 200 words. Use simple language and Nigerian examples."
    lesson = get_ai_response(prompt)
    user   = db.query(User).filter(User.username == username).first()
    if user: user.last_learned_topic = topic; db.commit()
    return {"topic": topic, "lesson": lesson, "level": level, "subject": subject}

# ============================================================
# KIDS GAMES
# ============================================================
@app.get("/games/word-scramble/{subject}")
def word_scramble(subject: str, level: str = "Primary"):
    prompt = f"""Give 1 educational word for {subject} ({level} level, Nigerian curriculum).
    Return ONLY JSON: {{"word":"...","scrambled":"...","hint":"...","meaning":"..."}}"""
    raw = get_ai_response(prompt)
    try: return json.loads(raw.replace("```json","").replace("```","").strip())
    except: return {"error": "Could not generate word scramble"}

@app.get("/games/spell-challenge/{level}")
def spell_challenge(level: str):
    prompt = f"""Give 1 spelling challenge word for Nigerian {level} student.
    Return ONLY JSON: {{"word":"...","hint":"...","example_sentence":"...","difficulty":"easy/medium/hard"}}"""
    raw = get_ai_response(prompt)
    try: return json.loads(raw.replace("```json","").replace("```","").strip())
    except: return {"error": "Could not generate spelling challenge"}

@app.get("/games/math-challenge/{level}")
def math_challenge(level: str):
    prompt = f"""Generate 1 fun math problem for Nigerian {level} student.
    Return ONLY JSON: {{"question":"...","answer":"...","solution_steps":"...","difficulty":"easy/medium/hard"}}"""
    raw = get_ai_response(prompt)
    try: return json.loads(raw.replace("```json","").replace("```","").strip())
    except: return {"error": "Could not generate math challenge"}

@app.get("/games/treasure-hunt/{level}")
def treasure_hunt(level: str, subject: str = "General"):
    prompt = f"""Create a fun educational treasure hunt clue for Nigerian {level} student about {subject}.
    Return ONLY JSON: {{"clue":"...","question":"...","answer":"...","reward_coins":5,"fun_fact":"..."}}"""
    raw = get_ai_response(prompt)
    try: return json.loads(raw.replace("```json","").replace("```","").strip())
    except: return {"error": "Could not generate treasure hunt"}

@app.post("/games/save-score")
def save_game_score(data: GameScoreSave, db=Depends(get_db)):
    user = db.query(User).filter(User.username == data.username).first()
    if not user: raise HTTPException(404, "User not found")
    db.add(GameScore(user_id=user.id, game_type=data.game_type, score=data.score, played_at=now_str()))
    coins_earned = max(1, data.score // 10)
    user.coins   = (user.coins or 0) + coins_earned
    db.commit()
    return {"msg": "Score saved", "coins_earned": coins_earned, "total_coins": user.coins}

@app.get("/games/leaderboard/{game_type}")
def game_leaderboard(game_type: str, db=Depends(get_db)):
    scores = db.query(GameScore).filter(GameScore.game_type == game_type).order_by(GameScore.score.desc()).limit(10).all()
    result = []
    for i, s in enumerate(scores):
        u = db.query(User).filter(User.id == s.user_id).first()
        if u: result.append({"rank": i+1, "username": u.username, "score": s.score, "played_at": s.played_at})
    return result

# ============================================================
# NOTIFICATIONS
# ============================================================
@app.get("/notifications/{username}")
def get_notifications(username: str, db=Depends(get_db)):
    user = db.query(User).filter(User.username == username).first()
    if not user: raise HTTPException(404, "User not found")
    notifs = db.query(Notification).filter(Notification.user_id == user.id).order_by(Notification.id.desc()).limit(20).all()
    return [{"id": n.id, "message": n.message, "is_read": n.is_read, "created_at": n.created_at} for n in notifs]

@app.post("/notifications/mark-read/{notif_id}")
def mark_read(notif_id: int, db=Depends(get_db)):
    n = db.query(Notification).filter(Notification.id == notif_id).first()
    if not n: raise HTTPException(404, "Not found")
    n.is_read = True; db.commit()
    return {"msg": "Marked as read"}

@app.post("/notifications/mark-all-read/{username}")
def mark_all_read(username: str, db=Depends(get_db)):
    user = db.query(User).filter(User.username == username).first()
    if not user: raise HTTPException(404, "User not found")
    db.query(Notification).filter(Notification.user_id == user.id, Notification.is_read == False).update({"is_read": True})
    db.commit()
    return {"msg": "All notifications marked as read"}

# ============================================================
# ADMIN — SUBJECTS & TOPICS
# ============================================================
@app.post("/admin/add-subject")
def add_subject(data: SubjectCreate, db=Depends(get_db)):
    s = Subject(name=data.name, level=data.level); db.add(s); db.commit()
    return {"id": s.id, "msg": "Subject added"}

@app.post("/admin/add-topic")
def add_topic(data: TopicCreate, db=Depends(get_db)):
    t = Topic(title=data.title, subject_id=data.subject_id); db.add(t); db.commit()
    return {"id": t.id, "msg": "Topic added"}

# ============================================================
# CACHE STATUS
# ============================================================
@app.get("/cache/status")
def cache_status():
    return {"cached_responses": len(ai_cache), "memory_usage": f"~{len(str(ai_cache))//1024}KB"}

@app.delete("/cache/clear")
def clear_cache():
    ai_cache.clear()
    return {"msg": "Cache cleared"}

# ============================================================
# TAVILY SEARCH
# ============================================================
@app.get("/search/web/{query}")
def search_web(query: str):
    if not TAVILY_KEY: return {"error": "Tavily API Key missing"}
    try:
        res = requests.post("https://api.tavily.com/search", json={
            "api_key": TAVILY_KEY, "query": query + " Nigeria education",
            "search_depth": "basic", "max_results": 5}, timeout=10)
        if res.status_code == 200:
            return {"query": query, "results": [
                {"title": r["title"], "url": r["url"], "snippet": r["content"][:150]+"..."}
                for r in res.json()["results"]]}
    except Exception as e: return {"error": f"Search failed: {str(e)}"}
    return {"error": "No results found"}

# ============================================================
# WEBSOCKET LIVE CLASSROOM
# ============================================================
active_connections: dict = {}

@app.websocket("/ws/classroom/{room}")
async def websocket_endpoint(websocket: WebSocket, room: str):
    await websocket.accept()
    if room not in active_connections: active_connections[room] = []
    active_connections[room].append(websocket)
    count = len(active_connections[room])
    await websocket.send_json({"type": "system", "message": f"✅ Connected to '{room}' — {count} student(s) online"})
    for conn in active_connections[room]:
        if conn != websocket: await conn.send_json({"type": "system", "message": "👤 A new student joined"})
    try:
        while True:
            data = await websocket.receive_json()
            msg  = data.get("message", "")
            user = data.get("username", "Student")
            for conn in active_connections[room]:
                await conn.send_json({"type": "chat", "username": user, "message": msg})
            if msg.startswith("/ai"):
                query    = msg.replace("/ai", "").strip()
                response = get_ai_response(f"You are an Ox-Bridge AI tutor for Nigerian students. Answer clearly: {query}")
                for conn in active_connections[room]:
                    await conn.send_json({"type": "ai", "username": "🤖 Ox-Bridge Tutor", "message": response})
    except WebSocketDisconnect:
        if websocket in active_connections.get(room, []): active_connections[room].remove(websocket)
        for conn in active_connections.get(room, []):
            await conn.send_json({"type": "system", "message": "👤 A student left the room"})
