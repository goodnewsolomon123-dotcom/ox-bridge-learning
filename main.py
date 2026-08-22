import os
import json
import random
import hashlib
import hmac
import math
import requests
import httpx
from fastapi import FastAPI, Depends, HTTPException, WebSocket, WebSocketDisconnect, Query, Request, File, UploadFile, Header
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
# CONFIGURATION - 7 AI PROVIDERS
# ============================================================
DATABASE_URL       = os.getenv("DATABASE_URL", "postgresql://neondb_owner:npg_UElyr9BSK5OH@ep-bold-hall-aq15g941-pooler.c-8.us-east-1.aws.neon.tech/neondb?sslmode=require&channel_binding=require")
GROQ_KEY           = os.getenv("GROQ_KEY")
GEMINI_KEY         = os.getenv("GEMINI_KEY")
OR_KEY             = os.getenv("OPENROUTER_KEY")
HF_KEY             = os.getenv("HUGGINGFACE_KEY")
COHERE_KEY         = os.getenv("COHERE_KEY")          
MISTRAL_KEY        = os.getenv("MISTRAL_KEY")         
DEEPSEEK_KEY       = os.getenv("DEEPSEEK_KEY")        
TAVILY_KEY         = os.getenv("TAVILY_API_KEY")
SECRET_KEY         = os.getenv("SECRET_KEY", "oxbridge_secret_2025")
ADMIN_API_KEY      = os.getenv("ADMIN_API_KEY")
TOKEN_EXPIRE_HOURS = 72

# ============================================================
# DATABASE ENGINE - FIXED FOR NEON
# ============================================================
engine = create_engine(
    DATABASE_URL,
    pool_pre_ping      = True,
    pool_recycle       = 300,
    pool_size          = 5,
    max_overflow       = 10,
    connect_args       = {
        "connect_timeout":    10,
        "sslmode":            "require",
        "keepalives":         1,
        "keepalives_idle":    30,
        "keepalives_interval":10,
        "keepalives_count":   5,
    }
)

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
        print(f"[CACHE HIT] {key[:8]}")
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
    id           = Column(Integer, primary_key=True, index=True)
    title        = Column(String)
    subject_id   = Column(Integer, ForeignKey("subjects.id"))
    image_url    = Column(String, nullable=True)   # admin-uploaded diagram/image for this topic
    admin_notes  = Column(String, nullable=True)   # admin's own short question/explanation (optional override/addition)
    created_at   = Column(String, nullable=True)

class ManualQuestion(Base):
    __tablename__ = "manual_questions"
    id             = Column(Integer, primary_key=True, index=True)
    subject        = Column(String,  index=True)
    level          = Column(String,  index=True)
    topic          = Column(String,  nullable=True)
    question_text  = Column(String)
    option_a       = Column(String)
    option_b       = Column(String)
    option_c       = Column(String)
    option_d       = Column(String)
    correct_answer = Column(String)
    explanation    = Column(String,  nullable=True)
    source         = Column(String,  nullable=True)
    added_by       = Column(String,  nullable=True)
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

class WaecTheoryQuestion(Base):
    __tablename__ = "waec_theory_questions"
    id            = Column(Integer, primary_key=True, index=True)
    exam_type     = Column(String, index=True)   # WAEC or NECO
    year          = Column(Integer, index=True)
    subject       = Column(String,  index=True)
    topic         = Column(String,  nullable=True)
    question_text = Column(String)
    model_answer  = Column(String,  nullable=True)
    image_url     = Column(String,  nullable=True)  # base64 diagram
    marks         = Column(Integer, nullable=True, default=10)
    created_at    = Column(String,  nullable=True)

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

class AIChatHistory(Base):
    __tablename__ = "ai_chat_history"
    id         = Column(Integer, primary_key=True, index=True)
    user_id    = Column(Integer, ForeignKey("users.id"))
    role       = Column(String)
    message    = Column(String)
    created_at = Column(String)

class ActivityLog(Base):
    __tablename__ = "activity_logs"
    id         = Column(Integer, primary_key=True, index=True)
    username   = Column(String, nullable=True, index=True)
    action     = Column(String)          # e.g. "login", "ai_chat", "quiz_submit", "theory_view"
    path       = Column(String, nullable=True)
    created_at = Column(String, index=True)

# ============================================================
# CREATE TABLES + MIGRATIONS
# ============================================================
try:
    Base.metadata.create_all(bind=engine, checkfirst=True)
    print("[DB] Tables created/verified [OK]")
except Exception as e:
    print(f"[DB ERROR] {e}")

def run_migrations():
    migrations = [
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS full_name VARCHAR;",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS profile_pic VARCHAR;",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS bio VARCHAR;",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS progress_score FLOAT DEFAULT 0.0;",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_learned_topic VARCHAR;",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS study_streak INTEGER DEFAULT 0;",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_study_date VARCHAR;",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS coins INTEGER DEFAULT 0;",
        "ALTER TABLE manual_questions ADD COLUMN IF NOT EXISTS source VARCHAR;",
        "ALTER TABLE manual_questions ADD COLUMN IF NOT EXISTS added_by VARCHAR;",
        "ALTER TABLE manual_questions ADD COLUMN IF NOT EXISTS created_at VARCHAR;",
        "ALTER TABLE manual_questions ADD COLUMN IF NOT EXISTS explanation VARCHAR;",
        "ALTER TABLE manual_questions ADD COLUMN IF NOT EXISTS topic VARCHAR;",
        "ALTER TABLE topics ADD COLUMN IF NOT EXISTS image_url VARCHAR;",
        "ALTER TABLE topics ADD COLUMN IF NOT EXISTS admin_notes VARCHAR;",
        "ALTER TABLE topics ADD COLUMN IF NOT EXISTS created_at VARCHAR;",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_premium BOOLEAN DEFAULT FALSE;",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS premium_expires VARCHAR;",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS premium_plan VARCHAR;",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS ai_requests_today INTEGER DEFAULT 0;",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS ai_requests_date VARCHAR;",
    ]
    try:
        with engine.connect() as conn:
            for sql in migrations:
                try:    conn.execute(text(sql))
                except: pass
            conn.commit()
        print("[DB] Migrations complete [OK]")
    except Exception as e:
        print(f"[DB MIGRATION ERROR] {e}")

run_migrations()

def seed_badges():
    try:
        db = SessionLocal()
        if db.query(Badge).count() == 0:
            db.add_all([
                Badge(name="First Step",    icon="", description="Created your account",      points_required=0),
                Badge(name="Quiz Starter",  icon="", description="Completed your first quiz", points_required=1),
                Badge(name="Rising Star",   icon="", description="Reached 10 points",         points_required=10),
                Badge(name="Scholar",       icon="", description="Reached 50 points",         points_required=50),
                Badge(name="Champion",      icon="", description="Reached 100 points",        points_required=100),
                Badge(name="Legend",        icon="", description="Reached 500 points",        points_required=500),
                Badge(name="Streak Master", icon="", description="7-day study streak",        points_required=0),
                Badge(name="Speed Learner", icon="", description="Completed 10 quizzes",      points_required=0),
            ])
            db.commit()
            print("[DB] Badges seeded [OK]")
        db.close()
    except Exception as e:
        print(f"[SEED ERROR] {e}")

seed_badges()

# ============================================================
# HELPERS
# ============================================================
def now_str():   return datetime.now(timezone.utc).isoformat()
def today_str(): return datetime.now(timezone.utc).strftime("%Y-%m-%d")

def hash_password(pw: str) -> str: return pwd_context.hash(pw)

def verify_password(plain: str, hashed: str) -> bool:
    try:    return pwd_context.verify(plain, hashed)
    except Exception as e:
        print(f"[PWD VERIFY ERROR] {e}")
        return False

def create_token(username: str) -> str:
    exp = datetime.now(timezone.utc) + timedelta(hours=TOKEN_EXPIRE_HOURS)
    return jwt.encode({"sub": username, "exp": exp}, SECRET_KEY, algorithm="HS256")

def decode_token(token: str) -> Optional[str]:
    try:    return jwt.decode(token, SECRET_KEY, algorithms=["HS256"]).get("sub")
    except: return None

def check_and_award_badges(user_id: int, db):
    try:
        user       = db.query(User).filter(User.id == user_id).first()
        if not user: return []
        earned_ids = {ub.badge_id for ub in db.query(UserBadge).filter(UserBadge.user_id == user_id).all()}
        quiz_count = db.query(QuizResult).filter(QuizResult.user_id == user_id).count()
        newly = []
        for b in db.query(Badge).all():
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
                db.add(Notification(user_id=user_id, message=f"[BADGE] You earned the '{b.name}' badge!", created_at=now_str()))
                newly.append(b.name)
        if newly: db.commit()
        return newly
    except Exception as e:
        print(f"[BADGE ERROR] {e}")
        return []

def update_streak(user: User, db):
    today = today_str()
    if user.last_study_date == today: return
    yesterday            = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    user.study_streak    = ((user.study_streak or 0) + 1) if user.last_study_date == yesterday else 1
    user.last_study_date = today
    db.commit()

# ============================================================
# HYBRID AI ROUTER - 7 PROVIDERS + CACHE + DETAILED LOGGING
# ============================================================
def needs_web_search(message: str) -> bool:
    """
    Heuristic check for whether a student's question likely needs
    current/real-time information the AI's training data wouldn't have
    (e.g. anything from this year, recent news, deadlines, admission
    lists). Kept as a simple keyword check rather than a second AI call
    so it stays fast and doesn't add extra provider cost per message.
    """
    msg_lower = message.lower()

    # Pure date/time/day questions are answered directly from
    # client_datetime (sent by the browser) - no search needed, saves
    # a Tavily call for something that doesn't require the web at all.
    pure_datetime_patterns = [
        "what is today", "what's today", "today's date", "what day is",
        "what date is", "what time is", "current time", "current date"
    ]
    if any(p in msg_lower for p in pure_datetime_patterns):
        return False

    triggers = [
        "current", "latest", "recent", "recently", "this year", "this month",
        "this week", "today", "now", "upcoming", "new ", "news", "update",
        "202", "when is", "when does", "deadline", "schedule", "announcement",
        "resumption", "admission list", "jamb cut off", "cut-off", "cutoff",
        "result", "release date", "still exist", "still active", "happening",
        "who is the", "vice chancellor", "vc of", "registrar"
    ]
    msg_lower = message.lower()
    return any(t in msg_lower for t in triggers)


def fetch_search_context(query: str) -> str:
    """Runs a Tavily search and formats results as compact context to feed the AI prompt."""
    if not TAVILY_KEY:
        return ""
    try:
        res = requests.post("https://api.tavily.com/search", json={
            "api_key": TAVILY_KEY, "query": query + " Nigeria university",
            "search_depth": "basic", "max_results": 4,
            "include_answer": True
        }, timeout=10)
        if res.status_code != 200:
            return ""
        data = res.json()
        parts = []
        if data.get("answer"):
            parts.append(f"Quick answer: {data['answer']}")
        for r in data.get("results", [])[:4]:
            title = r.get("title", "")
            content = (r.get("content", "") or "")[:200]
            parts.append(f"- {title}: {content}")
        return "\n".join(parts)
    except Exception as e:
        print(f"[TAVILY ERROR] {e}")
        return ""


def get_ai_response(prompt: str) -> str:
    cached = get_cached(prompt)
    if cached: 
        print(f"[AI] Cache hit for: {prompt[:50]}...")
        return cached

    result = None
    errors = []

    # 1. Groq
    if GROQ_KEY and not result:
        try:
            res = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json"},
                json={"model": "llama-3.3-70b-versatile", "messages": [{"role": "user", "content": prompt}]},
                timeout=15
            )
            if res.status_code == 200:
                result = res.json()['choices'][0]['message']['content']
                print("[AI] Groq [OK]")
            else:
                err = f"Groq HTTP {res.status_code}: {res.text[:100]}"
                errors.append(err)
                print(f"[AI] {err}")
        except Exception as e: 
            errors.append(f"Groq: {str(e)}")

    # 2. DeepSeek
    if DEEPSEEK_KEY and not result:
        try:
            res = requests.post(
                "https://api.deepseek.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {DEEPSEEK_KEY}", "Content-Type": "application/json"},
                json={"model": "deepseek-chat", "messages": [{"role": "user", "content": prompt}]},
                timeout=15
            )
            if res.status_code == 200:
                result = res.json()['choices'][0]['message']['content']
                print("[AI] DeepSeek [OK]")
            else:
                err = f"DeepSeek HTTP {res.status_code}: {res.text[:100]}"
                errors.append(err)
                print(f"[AI] {err}")
        except Exception as e: 
            errors.append(f"DeepSeek: {str(e)}")

    # 3. Mistral AI
    if MISTRAL_KEY and not result:
        try:
            res = requests.post(
                "https://api.mistral.ai/v1/chat/completions",
                headers={"Authorization": f"Bearer {MISTRAL_KEY}", "Content-Type": "application/json"},
                json={"model": "mistral-tiny", "messages": [{"role": "user", "content": prompt}]},
                timeout=15
            )
            if res.status_code == 200:
                result = res.json()['choices'][0]['message']['content']
                print("[AI] Mistral [OK]")
            else:
                err = f"Mistral HTTP {res.status_code}: {res.text[:100]}"
                errors.append(err)
                print(f"[AI] {err}")
        except Exception as e: 
            errors.append(f"Mistral: {str(e)}")

    # 4. Cohere (FIXED: Payload parameter updated to use an active model 'command-r')
    if COHERE_KEY and not result:
        try:
            res = requests.post(
                "https://api.cohere.ai/v1/chat",
                headers={"Authorization": f"Bearer {COHERE_KEY}", "Content-Type": "application/json"},
                json={"model": "command-r", "message": prompt},
                timeout=15
            )
            if res.status_code == 200:
                result = res.json()['text']
                print("[AI] Cohere [OK]")
            else:
                err = f"Cohere HTTP {res.status_code}: {res.text[:100]}"
                errors.append(err)
                print(f"[AI] {err}")
        except Exception as e: 
            errors.append(f"Cohere: {str(e)}")

    # 5. Gemini
    if GEMINI_KEY and not result:
        try:
            res = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_KEY}",
                json={"contents": [{"parts": [{"text": prompt}]}]},
                timeout=15
            )
            if res.status_code == 200:
                result = res.json()["candidates"][0]["content"]["parts"][0]["text"]
                print("[AI] Gemini [OK]")
            else:
                err = f"Gemini HTTP {res.status_code}: {res.text[:100]}"
                errors.append(err)
                print(f"[AI] {err}")
        except Exception as e: 
            errors.append(f"Gemini: {str(e)}")

    # 6. OpenRouter (FIXED: Using real, working active free IDs to avoid 404)
    if OR_KEY and not result:
        free_models = ["openrouter/free", "deepseek/deepseek-v4-flash:free", "google/gemini-2.5-flash"]

        for model in free_models:
            try:
                res = requests.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers={"Authorization": f"Bearer {OR_KEY}", "Content-Type": "application/json"},
                    json={"model": model, "messages": [{"role": "user", "content": prompt}]},
                    timeout=15
                )
                if res.status_code == 200:
                    result = res.json()['choices'][0]['message']['content']
                    print(f"[AI] OpenRouter ({model}) [OK]")
                    break
                else:
                    errors.append(f"OpenRouter {model}: HTTP {res.status_code}")
            except Exception as e:
                errors.append(f"OpenRouter {model}: {str(e)}")

    # 7. HuggingFace (FIXED: Improved configuration headers and fallback structure)
    if HF_KEY and not result:
        models = ["meta-llama/Llama-3-8B-Instruct"]
        for model in models:
            try:
                res = requests.post(
                    f"https://api-inference.huggingface.co/models/{model}",
                    headers={"Authorization": f"Bearer {HF_KEY}", "Content-Type": "application/json", "Connection": "close"},
                    json={"inputs": prompt, "parameters": {"max_new_tokens": 500}},
                    timeout=15
                )
                if res.status_code == 200:
                    data = res.json()
                    if isinstance(data, list) and data:
                        result = data[0].get("generated_text", "").replace(prompt, "").strip()
                    elif isinstance(data, dict) and "generated_text" in data:
                        result = data["generated_text"].strip()
                    if result:
                        print(f"[AI] HuggingFace ({model}) [OK]")
                        break
            except Exception as e:
                errors.append(f"HuggingFace: {str(e)}")

    if not result:
        error_summary = " | ".join(errors[-3:]) if errors else "All API keys missing"
        print(f"[AI] ALL PROVIDERS FAILED: {error_summary}")
        return f"AI services unavailable. Debug: {error_summary}"

    if "unavailable" not in result:
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

def verify_admin(x_admin_key: str = Header(None)):
    """
    Guards every /admin/* endpoint. Requires an X-Admin-Key header
    matching ADMIN_API_KEY (set in Render's environment variables).
    Without this, anyone who discovers an admin URL could add fake
    questions, read every user's activity, or message students as
    if they were the school.
    """
    if not ADMIN_API_KEY:
        raise HTTPException(status_code=500, detail="Admin access is not configured on the server")
    if not x_admin_key or x_admin_key != ADMIN_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing admin key")
    return True

# ============================================================
# LIGHTWEIGHT ACTIVITY LOGGER (for admin monitoring)
# Logs a small set of meaningful actions. Fails silently so it
# can never break an existing request.
# ============================================================
LOGGED_PATH_PREFIXES = {
    "/login": "login", "/signup": "signup", "/ai/chat": "ai_chat",
    "/quiz/save-result": "quiz_submit", "/theory/": "theory_view",
    "/games/save-score": "game_played", "/search/web/": "web_search",
    "/campus/login": "campus_login", "/campus/register": "campus_register",
    "/campus/gst/submit": "campus_gst_submit", "/campus/courses/submit": "campus_course_submit",
    "/payment/initialize": "payment_initiated", "/payment/verify": "payment_verified",
    "/campus/feedback": "campus_feedback", "/campus/cgpa/save": "cgpa_saved",
    "/upload/image": "image_uploaded",
}

@app.middleware("http")
async def log_activity_middleware(request, call_next):
    username_from_body = None
    try:
        if request.method == "POST":
            body = await request.body()
            if body:
                parsed = json.loads(body)
                username_from_body = parsed.get("username")
    except Exception:
        pass
    response = await call_next(request)
    try:
        path = request.url.path
        action = next((v for k, v in LOGGED_PATH_PREFIXES.items() if path.startswith(k)), None)
        if action:
            username = username_from_body or request.query_params.get("username")
            db = SessionLocal()
            db.add(ActivityLog(username=username, action=action, path=path, created_at=now_str()))
            db.commit()
            db.close()
    except Exception as e:
        print(f"[ACTIVITY LOG ERROR] {e}")
    return response

# ============================================================
# PYDANTIC SCHEMAS
# ============================================================
class UserCreate(BaseModel):
    username: str
    email:    str
    password: str

class LoginData(BaseModel):
    username: str
    password: str

class TokenValidate(BaseModel):
    token: str

class ProfileUpdate(BaseModel):
    full_name:   Optional[str] = None
    profile_pic: Optional[str] = None
    bio:         Optional[str] = None

class ScoreUpdate(BaseModel):
    username: str
    points:   float

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
    correct_answer: str
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

class WaecTheoryCreate(BaseModel):
    exam_type:     str              # WAEC or NECO
    year:          int
    subject:       str
    topic:         Optional[str] = None
    question_text: str
    model_answer:  Optional[str] = None
    image_url:     Optional[str] = None   # base64 image string
    marks:         Optional[int] = 10

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

class SubjectCreate(BaseModel):
    name:  str
    level: str

class TopicCreate(BaseModel):
    title:      str
    subject_id: int

class TopicAttach(BaseModel):
    image_url:   Optional[str] = None
    admin_notes: Optional[str] = None

class AIChatMessage(BaseModel):
    username: str
    message:  str
    subject:  Optional[str] = "General"
    level:    Optional[str] = "SSS"
    client_datetime: Optional[str] = None  # e.g. "Wednesday, 20 August 2026, 14:32" - from the student's own device clock

class AIChatHistoryClear(BaseModel):
    username: str

# ============================================================
# ROOT
# ============================================================
@app.api_route("/", methods=["GET", "HEAD"])
def root():
    return {
        "app":         "Ox-Bridge Learning Hub",
        "version":     "2.0.0",
        "status":      "running [OK]",
        "powered_by":  "Ox-Bridge Technology ",
        "ai_engines":  ["Groq", "DeepSeek", "Mistral", "Cohere", "Gemini", "OpenRouter", "HuggingFace"],
        "cache_size":  len(ai_cache)
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

    fs = db.query(Badge).filter(Badge.name == "First Step").first()
    if fs:
        db.add(UserBadge(user_id=new_user.id, badge_id=fs.id, earned_at=now_str()))
        db.add(Notification(
            user_id    = new_user.id,
            message    = " Welcome! You earned the 'First Step' badge!",
            created_at = now_str()
        ))
        db.commit()

    return {"msg": "Account created successfully! Please login.", "username": new_user.username}

@app.post("/login")
def login(data: LoginData, db=Depends(get_db)):
    username = data.username.strip()
    password = data.password.strip()

    print(f"[LOGIN] Attempt for username: '{username}'")

    if not username or not password:
        raise HTTPException(400, "Username and password are required")

    user = db.query(User).filter(User.username.ilike(username)).first()

    if not user:
        print(f"[LOGIN] [FAIL] User not found: '{username}'")
        raise HTTPException(401, "No account found with that username. Please sign up first.")

    ok = verify_password(password, user.hashed_password)
    print(f"[LOGIN] '{username}' - password match: {ok}")

    if not ok:
        raise HTTPException(401, "Wrong password. Please try again.")

    print(f"[LOGIN] [OK] Success for '{username}'")
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
        raise HTTPException(401, "Token expired. Please login again.")
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
    badges = []
    for ub in db.query(UserBadge).filter(UserBadge.user_id == user.id).all():
        b = db.query(Badge).filter(Badge.id == ub.badge_id).first()
        if b: badges.append({"name": b.name, "icon": b.icon, "earned_at": ub.earned_at})
    return {
        "username":    user.username,
        "full_name":   user.full_name,
        "bio":         user.bio,
        "profile_pic": user.profile_pic,
        "score":       user.progress_score or 0,
        "streak":      user.study_streak or 0,
        "coins":       user.coins          or 0,
        "last_topic":  user.last_learned_topic,
        "quiz_count":  db.query(QuizResult).filter(QuizResult.user_id == user.id).count(),
        "badges":      badges
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
    return [{"rank": i+1, "username": u.username, "score": u.progress_score or 0,
             "streak": u.study_streak or 0, "coins": u.coins or 0} for i, u in enumerate(top)]

# ============================================================
# BADGES
# ============================================================
@app.get("/badges/all")
def get_all_badges(db=Depends(get_db)):
    return [{"id": b.id, "name": b.name, "icon": b.icon,
             "description": b.description, "points_required": b.points_required}
            for b in db.query(Badge).all()]

@app.get("/badges/{username}")
def get_user_badges(username: str, db=Depends(get_db)):
    user = db.query(User).filter(User.username == username).first()
    if not user: raise HTTPException(404, "User not found")
    result = []
    for ub in db.query(UserBadge).filter(UserBadge.user_id == user.id).all():
        b = db.query(Badge).filter(Badge.id == ub.badge_id).first()
        if b: result.append({"name": b.name, "icon": b.icon, "description": b.description, "earned_at": ub.earned_at})
    return result

# ============================================================
# AI CHAT SPACE
# ============================================================
@app.post("/ai/chat")
def ai_chat(data: AIChatMessage, db=Depends(get_db)):
    user = db.query(User).filter(User.username == data.username).first()
    if not user: raise HTTPException(404, "User not found")

    history = db.query(AIChatHistory).filter(
        AIChatHistory.user_id == user.id
    ).order_by(AIChatHistory.id.desc()).limit(6).all()
    history.reverse()

    context = ""
    for h in history:
        role     = "Student" if h.role == "user" else "Tutor"
        context += f"{role}: {h.message}\n"

    search_context = fetch_search_context(data.message) if needs_web_search(data.message) else ""
    search_block = f"""
    Current web search results (this is real, up to date information - use it
    to answer anything about recent events, dates, deadlines, or current facts.
    Don't mention that you searched, just answer naturally as if you knew it):
    {search_context}
    """ if search_context else ""

    datetime_block = f"The current date and time (from the student's own device) is: {data.client_datetime}." if data.client_datetime else ""

    prompt = f"""You are Ox-Bridge AI Tutor - a friendly, knowledgeable tutor for Nigerian students
    (Level: {data.level}, Subject: {data.subject}).
    Explain things simply, use Nigerian examples, and encourage students.
    You are built by Ox-Bridge Technology.
    {datetime_block}
    {search_block}
    Previous conversation:
    {context}

    Student: {data.message}

    Tutor:"""

    response = get_ai_response(prompt)

    db.add(AIChatHistory(user_id=user.id, role="user",      message=data.message, created_at=now_str()))
    db.add(AIChatHistory(user_id=user.id, role="assistant", message=response,      created_at=now_str()))
    db.commit()

    return {"response": response, "username": data.username, "timestamp": now_str()}

@app.get("/ai/chat/history/{username}")
def get_chat_history(username: str, limit: int = 20, db=Depends(get_db)):
    user = db.query(User).filter(User.username == username).first()
    if not user: raise HTTPException(404, "User not found")
    history = db.query(AIChatHistory).filter(
        AIChatHistory.user_id == user.id
    ).order_by(AIChatHistory.id.desc()).limit(limit).all()
    history.reverse()
    return [{"role": h.role, "message": h.message, "created_at": h.created_at} for h in history]

@app.post("/ai/chat/clear")
def clear_chat_history(data: AIChatHistoryClear, db=Depends(get_db)):
    user = db.query(User).filter(User.username == data.username).first()
    if not user: raise HTTPException(404, "User not found")
    db.query(AIChatHistory).filter(AIChatHistory.user_id == user.id).delete()
    db.commit()
    return {"msg": "Chat history cleared"}

# ============================================================
# MANUAL QUESTIONS - ADMIN
# ============================================================
@app.post("/admin/add-question")
def add_question(data: ManualQuestionCreate, db=Depends(get_db), admin_ok: bool = Depends(verify_admin)):
    q = ManualQuestion(
        subject=data.subject.strip(), level=data.level.strip(),
        topic=data.topic.strip() if data.topic else None,
        question_text=data.question_text.strip(),
        option_a=data.option_a.strip(), option_b=data.option_b.strip(),
        option_c=data.option_c.strip(), option_d=data.option_d.strip(),
        correct_answer=data.correct_answer.upper().strip(),
        explanation=data.explanation, source=data.source or "manual",
        added_by=data.added_by or "admin", created_at=now_str()
    )
    db.add(q); db.commit(); db.refresh(q)
    return {"msg": "Question added", "id": q.id, "subject": q.subject, "level": q.level}

@app.post("/admin/add-questions-bulk")
def add_questions_bulk(data: BulkQuestionsCreate, db=Depends(get_db), admin_ok: bool = Depends(verify_admin)):
    added = []
    for qd in data.questions:
        q = ManualQuestion(
            subject=qd.subject.strip(), level=qd.level.strip(),
            topic=qd.topic.strip() if qd.topic else None,
            question_text=qd.question_text.strip(),
            option_a=qd.option_a.strip(), option_b=qd.option_b.strip(),
            option_c=qd.option_c.strip(), option_d=qd.option_d.strip(),
            correct_answer=qd.correct_answer.upper().strip(),
            explanation=qd.explanation, source=qd.source or "manual",
            added_by=qd.added_by or "admin", created_at=now_str()
        )
        db.add(q); added.append({"subject": q.subject, "level": q.level})
    db.commit()
    return {"msg": f"{len(added)} questions added", "questions": added}

@app.get("/admin/questions")
def list_questions(subject: str = None, level: str = None, limit: int = 50, db=Depends(get_db), admin_ok: bool = Depends(verify_admin)):
    query = db.query(ManualQuestion)
    if subject: query = query.filter(ManualQuestion.subject.ilike(f"%{subject}%"))
    if level:   query = query.filter(ManualQuestion.level.ilike(f"%{level}%"))
    questions = query.limit(limit).all()
    return {"total": query.count(), "questions": [
        {"id": q.id, "subject": q.subject, "level": q.level, "topic": q.topic,
         "question_text": q.question_text, "correct_answer": q.correct_answer, "source": q.source}
        for q in questions]}

@app.delete("/admin/question/{question_id}")
def delete_question(question_id: int, db=Depends(get_db), admin_ok: bool = Depends(verify_admin)):
    q = db.query(ManualQuestion).filter(ManualQuestion.id == question_id).first()
    if not q: raise HTTPException(404, "Question not found")
    db.delete(q); db.commit()
    return {"msg": f"Question {question_id} deleted"}

@app.get("/admin/questions/count")
def count_questions(db=Depends(get_db), admin_ok: bool = Depends(verify_admin)):
    all_q = db.query(ManualQuestion).all()
    counts = {}
    for q in all_q:
        key = f"{q.subject} ({q.level})"
        counts[key] = counts.get(key, 0) + 1
    return {"total": len(all_q), "by_subject_level": counts}

# ============================================================
# QUIZ - DATABASE FIRST, AI FALLBACK
# ============================================================
@app.get("/quiz/{topic}")
def smart_quiz(topic: str, level: str = "SSS", subject: str = "General", db=Depends(get_db)):
    db_qs = db.query(ManualQuestion).filter(
        ManualQuestion.subject.ilike(f"%{subject}%"),
        ManualQuestion.level.ilike(f"%{level}%")
    ).all()

    if topic.lower() != subject.lower():
        topic_qs  = db.query(ManualQuestion).filter(ManualQuestion.topic.ilike(f"%{topic}%")).all()
        existing  = {q.id for q in db_qs}
        db_qs     = db_qs + [q for q in topic_qs if q.id not in existing]

    if len(db_qs) >= 5:
        selected = random.sample(db_qs, min(5, len(db_qs)))
        quiz = [{"question": q.question_text,
                 "options":  [f"A) {q.option_a}", f"B) {q.option_b}", f"C) {q.option_c}", f"D) {q.option_d}"],
                 "answer": q.correct_answer, "explanation": q.explanation or "",
                 "time_limit_sec": 30, "source": "database"} for q in selected]
        print(f"[QUIZ] {len(quiz)} from DB for {subject} {level}")
        return {"topic": topic, "level": level, "subject": subject, "quiz": quiz, "source": "database"}

    print(f"[QUIZ] Using AI for {subject} {level} - {topic}")
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
        return {"topic": topic, "quiz": [], "error": "Could not generate quiz. Please try again.", "source": "ai"}

# ============================================================
# MANUAL QUIZ BY TOPIC
# ============================================================
@app.get("/quiz/manual/{topic}")
def get_manual_quiz(topic: str, db=Depends(get_db)):
    questions = db.query(ManualQuestion).filter(
        ManualQuestion.topic.ilike(f"%{topic}%")
    ).all()
    
    if not questions:
        return {"error": "No manual questions found for this topic yet. Add them via /admin/add-question"}
    
    quiz_list = []
    for q in questions:
        quiz_list.append({
            "id": q.id,
            "question": q.question_text,
            "options": [f"A) {q.option_a}", f"B) {q.option_b}", f"C) {q.option_c}", f"D) {q.option_d}"],
            "answer": q.correct_answer,
            "explanation": q.explanation,
            "subject": q.subject,
            "level": q.level
        })
        
    return {
        "topic": topic,
        "quiz": quiz_list,
        "total": len(quiz_list),
        "source": "manual_database"
    }

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
    newly      = check_and_award_badges(user.id, db)
    quiz_count = db.query(QuizResult).filter(QuizResult.user_id == user.id).count()
    if quiz_count == 1:
        qs = db.query(Badge).filter(Badge.name == "Quiz Starter").first()
        if qs and not db.query(UserBadge).filter(UserBadge.user_id == user.id, UserBadge.badge_id == qs.id).first():
            db.add(UserBadge(user_id=user.id, badge_id=qs.id, earned_at=now_str()))
            db.add(Notification(user_id=user.id, message=" You earned the 'Quiz Starter' badge!", created_at=now_str()))
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
def add_past_question(data: PastQuestionCreate, db=Depends(get_db), admin_ok: bool = Depends(verify_admin)):
    q = PastQuestion(**data.dict()); db.add(q); db.commit()
    return {"msg": "Past question added", "id": q.id}

@app.get("/past-questions/{exam_type}/{subject}")
def get_past_questions(exam_type: str, subject: str, year: int = None, db=Depends(get_db)):
    query = db.query(PastQuestion).filter(
        PastQuestion.exam_type.ilike(f"%{exam_type}%"),
        PastQuestion.subject.ilike(f"%{subject}%")
    )
    if year: query = query.filter(PastQuestion.year == year)
    return [{"id": q.id, "year": q.year, "question_text": q.question_text,
             "option_a": q.option_a, "option_b": q.option_b,
             "option_c": q.option_c, "option_d": q.option_d,
             "correct_answer": q.correct_answer, "explanation": q.explanation}
            for q in query.limit(20).all()]

@app.get("/past-questions/random/{subject}")
def random_past_question(subject: str, exam: str = "WAEC", db=Depends(get_db)):
    questions = db.query(PastQuestion).filter(
        PastQuestion.exam_type.ilike(f"%{exam}%"),
        PastQuestion.subject.ilike(f"%{subject}%")
    ).all()
    if not questions:
        return {"source": "database", "question": None}
    q = random.choice(questions)
    return {"source": "database", "question": {
        "id": q.id, "year": q.year, "question_text": q.question_text,
        "option_a": q.option_a, "option_b": q.option_b,
        "option_c": q.option_c, "option_d": q.option_d,
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
        prompt   = f"Generate 1 multiple-choice question about {random.choice(subjects)} for Nigerian secondary school students. Return ONLY JSON: {{\x22question_text\x22:\x22...\x22,\x22option_a\x22:\x22...\x22,\x22option_b\x22:\x22...\x22,\x22option_c\x22:\x22...\x22,\x22option_d\x22:\x22...\x22,\x22correct_answer\x22:\x22A\x22}}"
        raw = get_ai_response(prompt)
        try:
            q        = json.loads(raw.replace("```json","").replace("```","").strip())
            existing = DailyChallenge(date=today, question_text=q["question_text"],
                option_a=q["option_a"], option_b=q["option_b"],
                option_c=q["option_c"], option_d=q["option_d"], correct_answer=q["correct_answer"])
            db.add(existing); db.commit(); db.refresh(existing)
        except:
            return {"error": "Could not generate daily challenge. Try again shortly."}
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
        DailyChallengeAttempt.user_id == user.id,
        DailyChallengeAttempt.challenge_id == challenge.id).first()
    if already:
        return {"msg": "Already attempted!", "already_attempted": True, "correct_answer": challenge.correct_answer}
    is_correct = data.answer.upper().strip() == challenge.correct_answer.upper().strip()
    db.add(DailyChallengeAttempt(user_id=user.id, challenge_id=challenge.id, answered_at=now_str(), was_correct=is_correct))
    if is_correct:
        user.progress_score = (user.progress_score or 0) + 5
        user.coins          = (user.coins          or 0) + 10
        db.add(Notification(user_id=user.id, message=" Daily challenge correct! +5 points, +10 coins", created_at=now_str()))
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
    db.add(Notification(user_id=friend.id, message=f" {user.username} sent you a friend request!", created_at=now_str()))
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
    db.add(Notification(user_id=friend.id, message=f" {user.username} accepted your friend request!", created_at=now_str()))
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
@app.get("/learn/{topic}")
def learn(topic: str, username: str, level: str = "SSS", subject: str = "General", db=Depends(get_db)):
    prompt = f"Explain '{topic}' to a Nigerian {level} {subject} student in 200 words. Use simple language and Nigerian examples."
    lesson = get_ai_response(prompt)
    user   = db.query(User).filter(User.username == username).first()
    if user: user.last_learned_topic = topic; db.commit()
    return {"topic": topic, "lesson": lesson, "level": level, "subject": subject}

@app.post("/ai/study-plan")
def ai_study_plan(data: StudyPlanRequest):
    prompt = f"""Create a weekly study plan for a Nigerian {data.level} student.
    Subjects: {data.subjects}. Exam: {data.exam_date}. Hours/day: {data.hours_per_day}.
    Nigerian curriculum (WAEC/JAMB). Day-by-day with specific topics."""
    return {"username": data.username, "study_plan": get_ai_response(prompt)}

@app.post("/ai/check-answer")
def check_answer(data: AnswerCheckRequest):
    prompt = f"""Nigerian student answered a {data.subject} question.
    Q: {data.question}. Their answer: {data.student_answer}. Correct: {data.correct_answer}.
    Tell if right/wrong, explain why simply, give memory tip. Be encouraging."""
    return {"is_correct": data.student_answer.strip().upper() == data.correct_answer.strip().upper(),
            "feedback": get_ai_response(prompt)}

# ============================================================
# KIDS GAMES
# ============================================================
@app.get("/games/word-scramble/{subject}")
def word_scramble(subject: str, level: str = "Primary"):
    raw = get_ai_response(f"1 educational word for {subject} ({level}, Nigerian curriculum). ONLY JSON: {{\x22word\x22:\x22...\x22,\x22scrambled\x22:\x22...\x22,\x22hint\x22:\x22...\x22,\x22meaning\x22:\x22...\x22}}")
    try: return json.loads(raw.replace("```json","").replace("```","").strip())
    except: return {"error": "Could not generate"}

@app.get("/games/spell-challenge/{level}")
def spell_challenge(level: str):
    raw = get_ai_response(f"1 spelling word for Nigerian {level} student. ONLY JSON: {{\x22word\x22:\x22...\x22,\x22hint\x22:\x22...\x22,\x22example_sentence\x22:\x22...\x22,\x22difficulty\x22:\x22easy/medium/hard\x22}}")
    try: return json.loads(raw.replace("```json","").replace("```","").strip())
    except: return {"error": "Could not generate"}

@app.get("/games/math-challenge/{level}")
def math_challenge(level: str):
    raw = get_ai_response(f"1 math problem for Nigerian {level} student. ONLY JSON: {{\x22question\x22:\x22...\x22,\x22answer\x22:\x22...\x22,\x22solution_steps\x22:\x22...\x22,\x22difficulty\x22:\x22easy/medium/hard\x22}}")
    try: return json.loads(raw.replace("```json","").replace("```","").strip())
    except: return {"error": "Could not generate"}

@app.get("/games/treasure-hunt/{level}")
def treasure_hunt(level: str, subject: str = "General"):
    raw = get_ai_response(f"Educational treasure hunt for Nigerian {level} student about {subject}. ONLY JSON: {{\x22clue\x22:\x22...\x22,\x22question\x22:\x22...\x22,\x22answer\x22:\x22...\x22,\x22reward_coins\x22:5,\x22fun_fact\x22:\x22...\x22}}")
    try: return json.loads(raw.replace("```json","").replace("```","").strip())
    except: return {"error": "Could not generate"}

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
# ADMIN - SUBJECTS & TOPICS
# ============================================================
@app.post("/admin/add-subject")
def add_subject(data: SubjectCreate, db=Depends(get_db), admin_ok: bool = Depends(verify_admin)):
    s = Subject(name=data.name, level=data.level); db.add(s); db.commit()
    return {"id": s.id, "msg": "Subject added"}

@app.post("/admin/add-topic")
def add_topic(data: TopicCreate, db=Depends(get_db), admin_ok: bool = Depends(verify_admin)):
    t = Topic(title=data.title, subject_id=data.subject_id, created_at=now_str()); db.add(t); db.commit()
    return {"id": t.id, "msg": "Topic added"}

@app.delete("/admin/subject/{subject_id}")
def delete_subject(subject_id: int, db=Depends(get_db), admin_ok: bool = Depends(verify_admin)):
    s = db.query(Subject).filter(Subject.id == subject_id).first()
    if not s: raise HTTPException(404, "Subject not found")
    db.query(Topic).filter(Topic.subject_id == subject_id).delete()
    db.delete(s); db.commit()
    return {"msg": "Subject and its topics deleted"}

@app.delete("/admin/topic/{topic_id}")
def delete_topic(topic_id: int, db=Depends(get_db), admin_ok: bool = Depends(verify_admin)):
    t = db.query(Topic).filter(Topic.id == topic_id).first()
    if not t: raise HTTPException(404, "Topic not found")
    db.delete(t); db.commit()
    return {"msg": "Topic deleted"}

@app.post("/admin/topic/{topic_id}/attach")
def attach_topic_media(topic_id: int, data: TopicAttach, db=Depends(get_db), admin_ok: bool = Depends(verify_admin)):
    """Admin attaches an image (e.g. base64 or hosted URL) and/or a short
    custom note/question to a theory topic. Both fields are optional -
    send only what you want to set."""
    t = db.query(Topic).filter(Topic.id == topic_id).first()
    if not t: raise HTTPException(404, "Topic not found")
    if data.image_url   is not None: t.image_url   = data.image_url
    if data.admin_notes is not None: t.admin_notes = data.admin_notes
    db.commit()
    return {"msg": "Topic updated", "topic_id": t.id, "has_image": bool(t.image_url)}

# ============================================================
# THEORY - SUBJECTS, TOPICS, AI-GENERATED BREAKDOWN
# ============================================================
@app.get("/subjects")
def list_subjects(level: str = None, db=Depends(get_db)):
    query = db.query(Subject)
    if level: query = query.filter(Subject.level.ilike(f"%{level}%"))
    subjects = query.all()
    return [{"id": s.id, "name": s.name, "level": s.level} for s in subjects]

@app.get("/subjects/{subject_id}/topics")
def list_topics_for_subject(subject_id: int, db=Depends(get_db)):
    subject = db.query(Subject).filter(Subject.id == subject_id).first()
    if not subject: raise HTTPException(404, "Subject not found")
    topics = db.query(Topic).filter(Topic.subject_id == subject_id).all()
    return {
        "subject": {"id": subject.id, "name": subject.name, "level": subject.level},
        "topics": [
            {"id": t.id, "title": t.title, "has_image": bool(t.image_url)}
            for t in topics
        ]
    }

@app.get("/theory/{topic_id}")
def get_theory_breakdown(topic_id: int, username: str = None, db=Depends(get_db)):
    """Generates the theory breakdown for a topic. If the admin attached a
    custom note/question, it's woven into the prompt so the AI explains
    around it. If an image is attached, its URL is returned for the
    frontend to render alongside the AI explanation."""
    topic = db.query(Topic).filter(Topic.id == topic_id).first()
    if not topic: raise HTTPException(404, "Topic not found")
    subject = db.query(Subject).filter(Subject.id == topic.subject_id).first()
    subject_name = subject.name if subject else "General"
    level         = subject.level if subject else "SSS"

    if topic.admin_notes:
        prompt = f"""You are Ox-Bridge AI Tutor for Nigerian {level} students studying {subject_name}.
        Topic: '{topic.title}'.
        The teacher has provided this specific question/note to build the lesson around:
        \"{topic.admin_notes}\"
        Write a clear theory breakdown: (1) simple explanation of the concept,
        (2) how it relates to the teacher's note above, (3) one worked example,
        (4) a short summary. Use Nigerian WAEC/JAMB/NECO exam style."""
    else:
        prompt = f"""You are Ox-Bridge AI Tutor for Nigerian {level} students studying {subject_name}.
        Topic: '{topic.title}'.
        Write a clear theory breakdown: (1) simple explanation of the concept,
        (2) one worked example, (3) a short summary a student can revise from.
        Use Nigerian WAEC/JAMB/NECO exam style."""

    explanation = get_ai_response(prompt)

    if username:
        user = db.query(User).filter(User.username == username).first()
        if user: user.last_learned_topic = topic.title; db.commit()

    return {
        "topic_id":   topic.id,
        "title":      topic.title,
        "subject":    subject_name,
        "level":      level,
        "explanation": explanation,
        "image_url":  topic.image_url,
        "has_image":  bool(topic.image_url)
    }

# ============================================================
# ADMIN - MONITORING
# ============================================================
@app.get("/admin/monitor/overview")
def monitor_overview(db=Depends(get_db), admin_ok: bool = Depends(verify_admin)):
    today = today_str()
    total_users     = db.query(User).count()
    ai_calls_today  = db.query(ActivityLog).filter(
        ActivityLog.action == "ai_chat", ActivityLog.created_at.like(f"{today}%")
    ).count()
    active_today = db.query(ActivityLog.username).filter(
        ActivityLog.created_at.like(f"{today}%"), ActivityLog.username.isnot(None)
    ).distinct().count()
    total_logs = db.query(ActivityLog).count()
    return {
        "total_users":        total_users,
        "active_users_today": active_today,
        "ai_calls_today":     ai_calls_today,
        "total_activity_logs": total_logs,
        "cache_size":         len(ai_cache)
    }

@app.get("/admin/monitor/activity")
def monitor_activity(username: str = None, action: str = None, limit: int = 50, db=Depends(get_db), admin_ok: bool = Depends(verify_admin)):
    query = db.query(ActivityLog)
    if username: query = query.filter(ActivityLog.username == username)
    if action:   query = query.filter(ActivityLog.action == action)
    logs = query.order_by(ActivityLog.id.desc()).limit(limit).all()
    return [{"id": l.id, "username": l.username, "action": l.action,
              "path": l.path, "created_at": l.created_at} for l in logs]

@app.get("/admin/monitor/usage/{username}")
def monitor_user_usage(username: str, db=Depends(get_db), admin_ok: bool = Depends(verify_admin)):
    user = db.query(User).filter(User.username == username).first()
    if not user: raise HTTPException(404, "User not found")
    logs = db.query(ActivityLog).filter(ActivityLog.username == username).all()
    by_action = {}
    for l in logs:
        by_action[l.action] = by_action.get(l.action, 0) + 1
    return {
        "username":        username,
        "total_actions":   len(logs),
        "breakdown":       by_action,
        "quiz_count":      db.query(QuizResult).filter(QuizResult.user_id == user.id).count(),
        "ai_messages":     db.query(AIChatHistory).filter(AIChatHistory.user_id == user.id, AIChatHistory.role == "user").count(),
        "coins":           user.coins or 0,
        "score":           user.progress_score or 0
    }

# ============================================================
# CACHE
# ============================================================
@app.get("/cache/status")
def cache_status():
    return {"cached_responses": len(ai_cache), "memory_usage": f"~{len(str(ai_cache))//1024}KB"}

@app.delete("/cache/clear")
def clear_cache():
    ai_cache.clear()
    return {"msg": "Cache cleared"}

# ============================================================
# DEBUG - AI PROVIDER STATUS (7 PROVIDERS)
# ============================================================
@app.get("/debug/ai")
def debug_ai():
    results = {}
    
    providers = [
        ("groq", GROQ_KEY, "https://api.groq.com/openai/v1/chat/completions", 
         {"model": "llama-3.3-70b-versatile", "messages": [{"role": "user", "content": "Say OK"}]}),
        ("deepseek", DEEPSEEK_KEY, "https://api.deepseek.com/v1/chat/completions",
         {"model": "deepseek-chat", "messages": [{"role": "user", "content": "Say OK"}]}),
        ("mistral", MISTRAL_KEY, "https://api.mistral.ai/v1/chat/completions",
         {"model": "mistral-tiny", "messages": [{"role": "user", "content": "Say OK"}]}),
        ("cohere", COHERE_KEY, "https://api.cohere.ai/v1/chat",
         {"model": "command-r", "message": "Say OK"}),
        ("gemini", GEMINI_KEY, f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_KEY}",
         {"contents": [{"parts": [{"text": "Say OK"}]}]}),
    ]
    
    for name, key, url, payload in providers:
        if not key:
            results[name] = "[WARN] Key not set"
            continue
        try:
            headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
            if name == "gemini":
                headers = {"Content-Type": "application/json"}
            
            r = requests.post(url, headers=headers, json=payload, timeout=10)
            
            if r.status_code == 200:
                results[name] = "[OK] Working"
            elif r.status_code == 429:
                results[name] = "[WAIT] Rate limited"
            else:
                results[name] = f"[FAIL] HTTP {r.status_code}: {r.text[:100]}"
        except Exception as e:
            results[name] = f"[FAIL] Exception: {str(e)[:100]}"
    
    # Test OpenRouter
    if OR_KEY:
        try:
            r = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {OR_KEY}", "Content-Type": "application/json"},
                json={"model": "openrouter/free", "messages": [{"role": "user", "content": "Say OK"}]},
                timeout=10
            )
            if r.status_code == 200:
                results["openrouter"] = "[OK] Working"
            else:
                results["openrouter"] = f"[FAIL] HTTP {r.status_code}: {r.text[:100]}"
        except Exception as e:
            results["openrouter"] = f"[FAIL] Exception: {str(e)[:100]}"
    else:
        results["openrouter"] = "[WARN] Key not set"
    
    # Test HuggingFace
    if HF_KEY:
        try:
            r = requests.post(
                "https://api-inference.huggingface.co/models/meta-llama/Llama-3-8B-Instruct",
                headers={"Authorization": f"Bearer {HF_KEY}", "Content-Type": "application/json"},
                json={"inputs": "Say OK"},
                timeout=15
            )
            if r.status_code == 200:
                results["huggingface"] = "[OK] Working"
            else:
                results["huggingface"] = f"[FAIL] HTTP {r.status_code}"
        except Exception as e:
            results["huggingface"] = f"[FAIL] Exception: {str(e)[:100]}"
    else:
        results["huggingface"] = "[WARN] Key not set"
    
    working = sum(1 for v in results.values() if "[OK]" in str(v))
    
    return {
        "ai_status": results,
        "cache_size": len(ai_cache),
        "working_providers": working,
        "total_providers": 7,
        "recommendation": "Get free keys: DeepSeek (deepseek.com), Mistral (console.mistral.ai), Cohere (cohere.com)"
    }

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
# WAEC / NECO THEORY PAST QUESTIONS
# ============================================================

@app.post("/admin/add-waec-theory")
def add_waec_theory(data: WaecTheoryCreate, db=Depends(get_db), admin_ok: bool = Depends(verify_admin)):
    """Admin adds a WAEC/NECO theory past question"""
    try:
        q = WaecTheoryQuestion(
            exam_type     = data.exam_type.upper(),
            year          = data.year,
            subject       = data.subject,
            topic         = data.topic,
            question_text = data.question_text,
            model_answer  = data.model_answer,
            image_url     = data.image_url,
            marks         = data.marks or 10,
            created_at    = now_str()
        )
        db.add(q); db.commit(); db.refresh(q)
        return {"success": True, "id": q.id, "message": f"Theory question added for {data.subject} {data.exam_type} {data.year}"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/waec-theory/subjects")
def get_waec_theory_subjects(db=Depends(get_db)):
    """Get all subjects that have WAEC/NECO theory questions"""
    try:
        rows = db.execute(
            text("""
                SELECT DISTINCT subject, exam_type, COUNT(*) as total
                FROM waec_theory_questions
                GROUP BY subject, exam_type
                ORDER BY subject, exam_type
            """)
        ).fetchall()
        result = {}
        for row in rows:
            subj = row[0]
            if subj not in result:
                result[subj] = {"subject": subj, "exams": [], "total": 0}
            result[subj]["exams"].append({"exam_type": row[1], "count": row[2]})
            result[subj]["total"] += row[2]
        return list(result.values())
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/waec-theory/questions")
def get_waec_theory_questions(
    subject:   str = Query(...),
    exam_type: str = Query("WAEC"),
    year:      Optional[int] = Query(None),
    db=Depends(get_db)
):
    """Get theory questions filtered by subject, exam type and optionally year"""
    try:
        query = db.query(WaecTheoryQuestion).filter(
            WaecTheoryQuestion.subject   == subject,
            WaecTheoryQuestion.exam_type == exam_type.upper()
        )
        if year:
            query = query.filter(WaecTheoryQuestion.year == year)
        questions = query.order_by(WaecTheoryQuestion.year.desc()).all()
        return [
            {
                "id":            q.id,
                "exam_type":     q.exam_type,
                "year":          q.year,
                "subject":       q.subject,
                "topic":         q.topic,
                "question_text": q.question_text,
                "model_answer":  q.model_answer,
                "has_image":     bool(q.image_url),
                "image_url":     q.image_url,
                "marks":         q.marks
            }
            for q in questions
        ]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/waec-theory/years")
def get_waec_theory_years(
    subject:   str = Query(...),
    exam_type: str = Query("WAEC"),
    db=Depends(get_db)
):
    """Get all available years for a subject"""
    try:
        rows = db.query(WaecTheoryQuestion.year).filter(
            WaecTheoryQuestion.subject   == subject,
            WaecTheoryQuestion.exam_type == exam_type.upper()
        ).distinct().order_by(WaecTheoryQuestion.year.desc()).all()
        return {"years": [r[0] for r in rows]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/waec-theory/explain/{question_id}")
def explain_waec_theory(question_id: int, username: Optional[str] = None, db=Depends(get_db)):
    """AI generates a detailed explanation for a theory question"""
    q = db.query(WaecTheoryQuestion).filter(WaecTheoryQuestion.id == question_id).first()
    if not q:
        raise HTTPException(status_code=404, detail="Question not found")
    model_answer_line = ("Model Answer provided:\n" + q.model_answer) if q.model_answer else "No model answer provided - generate a comprehensive answer."
    prompt = f"""You are an expert Ox-Bridge AI Tutor for Nigerian {q.exam_type} students.

Subject: {q.subject}
Year: {q.year}
{"Topic: " + q.topic if q.topic else ""}
Marks: {q.marks} marks

Theory Question:
{q.question_text}

{model_answer_line}

Please provide:
1. A clear, step-by-step explanation of the question
2. The key points that earn marks in {q.exam_type} marking scheme
3. A well-structured model answer a student should write
4. Common mistakes students make on this question
5. A quick revision tip

Write in simple, clear English suitable for Nigerian SS3 students preparing for {q.exam_type}."""

    explanation = get_ai_response(prompt)
    return {
        "id":            q.id,
        "subject":       q.subject,
        "exam_type":     q.exam_type,
        "year":          q.year,
        "topic":         q.topic,
        "question_text": q.question_text,
        "model_answer":  q.model_answer,
        "image_url":     q.image_url,
        "marks":         q.marks,
        "explanation":   explanation
    }

@app.delete("/admin/waec-theory/{question_id}")
def delete_waec_theory(question_id: int, db=Depends(get_db), admin_ok: bool = Depends(verify_admin)):
    """Admin deletes a theory question"""
    q = db.query(WaecTheoryQuestion).filter(WaecTheoryQuestion.id == question_id).first()
    if not q:
        raise HTTPException(status_code=404, detail="Question not found")
    db.delete(q); db.commit()
    return {"success": True, "message": f"Theory question #{question_id} deleted"}

@app.get("/admin/waec-theory/list")
def list_waec_theory_admin(
    subject:   Optional[str] = Query(None),
    exam_type: Optional[str] = Query(None),
    db=Depends(get_db),
    admin_ok: bool = Depends(verify_admin)
):
    """Admin view all theory questions"""
    try:
        query = db.query(WaecTheoryQuestion)
        if subject:   query = query.filter(WaecTheoryQuestion.subject == subject)
        if exam_type: query = query.filter(WaecTheoryQuestion.exam_type == exam_type.upper())
        questions = query.order_by(WaecTheoryQuestion.year.desc()).all()
        return {
            "total": len(questions),
            "questions": [
                {
                    "id":            q.id,
                    "exam_type":     q.exam_type,
                    "year":          q.year,
                    "subject":       q.subject,
                    "topic":         q.topic,
                    "question_text": q.question_text[:80]+"..." if q.question_text and len(q.question_text)>80 else q.question_text,
                    "has_image":     bool(q.image_url),
                    "has_answer":    bool(q.model_answer),
                    "marks":         q.marks
                }
                for q in questions
            ]
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

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
    await websocket.send_json({"type": "system", "message": f"[OK] Connected to '{room}' - {count} student(s) online"})
    for conn in active_connections[room]:
        if conn != websocket:
            await conn.send_json({"type": "system", "message": " A new student joined"})
    try:
        while True:
            data = await websocket.receive_json()
            msg  = data.get("message", "")
            user = data.get("username", "Student")
            for conn in active_connections[room]:
                await conn.send_json({"type": "chat", "username": user, "message": msg})
            if msg.startswith("/ai"):
                query    = msg.replace("/ai", "").strip()
                response = get_ai_response(f"You are Ox-Bridge AI Tutor for Nigerian students. Answer clearly: {query}")
                for conn in active_connections[room]:
                    await conn.send_json({"type": "ai", "username": " Ox-Bridge Tutor", "message": response})
    except WebSocketDisconnect:
        if websocket in active_connections.get(room, []): active_connections[room].remove(websocket)
        for conn in active_connections.get(room, []):
            await conn.send_json({"type": "system", "message": " A student left the room"})


# ============================================================
# OX-BRIDGE CAMPUS MODULE - NEW ADDITIONS ONLY
# Everything below is new. Nothing above this line was touched.
# ============================================================

# ------------------------------------------------------------
# NEW SQLALCHEMY MODELS
# ------------------------------------------------------------
class CampusUser(Base):
    __tablename__ = "campus_users"
    id             = Column(Integer, primary_key=True, index=True)
    username       = Column(String, unique=True, index=True)
    full_name      = Column(String)
    matric_number  = Column(String, unique=True, index=True)
    department     = Column(String)
    level          = Column(String)
    password_hash  = Column(String)
    created_at     = Column(String, nullable=True)


class GSTQuestion(Base):
    __tablename__ = "gst_questions"
    id             = Column(Integer, primary_key=True, index=True)
    course_code    = Column(String, index=True)
    course_title   = Column(String)
    question_text  = Column(String)
    option_a       = Column(String)
    option_b       = Column(String)
    option_c       = Column(String)
    option_d       = Column(String)
    correct_answer = Column(String)
    explanation    = Column(String, nullable=True)
    university     = Column(String, default="AKSU")


class GSTResult(Base):
    __tablename__ = "gst_results"
    id          = Column(Integer, primary_key=True, index=True)
    username    = Column(String, index=True)
    course_code = Column(String)
    score       = Column(Integer)
    total       = Column(Integer)
    percentage  = Column(Float)
    time_taken  = Column(Integer, nullable=True)
    taken_at    = Column(String)


class CampusFeedback(Base):
    __tablename__ = "campus_feedback"
    id         = Column(Integer, primary_key=True, index=True)
    username   = Column(String)
    rating     = Column(Integer)
    message    = Column(String, nullable=True)
    created_at = Column(String)


# NOTE: Base.metadata.create_all(bind=engine, checkfirst=True) already runs
# earlier in the file with checkfirst=True, so these new tables will be
# created automatically on next startup. No need to call it again.


# ------------------------------------------------------------
# NEW PYDANTIC SCHEMAS
# ------------------------------------------------------------
class CampusRegisterSchema(BaseModel):
    username: str
    full_name: str
    email: str
    matric_number: str
    department: str
    level: str
    password: str


class CampusLoginSchema(BaseModel):
    username: str
    password: str


class GSTSubmitSchema(BaseModel):
    username: str
    course_code: str
    answers: dict          # { "1": "A", "2": "C", ... } question_id -> chosen option
    time_taken: Optional[int] = None


class CampusFeedbackSchema(BaseModel):
    username: str
    rating: int
    message: Optional[str] = None


# ------------------------------------------------------------
# NEW HELPER (local to this module - sha256 hashing, kept
# separate from the existing bcrypt-based password helpers so
# nothing existing is touched)
# ------------------------------------------------------------
def campus_hash_password(pw: str) -> str:
    return hashlib.sha256(pw.encode()).hexdigest()


def campus_verify_password(plain: str, hashed: str) -> bool:
    return campus_hash_password(plain) == hashed


# ------------------------------------------------------------
# NEW ENDPOINTS - OX-BRIDGE CAMPUS
# ------------------------------------------------------------

@app.post("/campus/register")
def campus_register(payload: CampusRegisterSchema, db=Depends(get_db)):
    existing = db.query(CampusUser).filter(
        (CampusUser.username == payload.username) |
        (CampusUser.matric_number == payload.matric_number)
    ).first()
    if existing:
        raise HTTPException(status_code=400, detail="Username or matric number already registered")

    new_user = CampusUser(
        username=payload.username,
        full_name=payload.full_name,
        matric_number=payload.matric_number,
        department=payload.department,
        level=payload.level,
        password_hash=campus_hash_password(payload.password),
        created_at=now_str(),
    )
    db.add(new_user)
    db.commit()
    db.refresh(new_user)

    # email isn't on the CampusUser ORM class (added via a separate migration),
    # so it's saved with a plain SQL update right after the ORM insert above.
    db.execute(
        text("UPDATE campus_users SET email = :email WHERE username = :username"),
        {"email": payload.email, "username": new_user.username},
    )
    db.commit()

    return {"success": True, "message": "Registration successful", "username": new_user.username}


@app.post("/campus/login")
def campus_login(payload: CampusLoginSchema, db=Depends(get_db)):
    user = db.query(CampusUser).filter(CampusUser.username == payload.username).first()
    if not user or not campus_verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid username or password")

    email_row = db.execute(
        text("SELECT email, profile_pic FROM campus_users WHERE username = :username"),
        {"username": user.username},
    ).fetchone()
    email = email_row[0] if email_row else None
    profile_pic = email_row[1] if email_row else None

    return {
        "success": True,
        "username": user.username,
        "full_name": user.full_name,
        "email": email,
        "profile_pic": profile_pic,
        "department": user.department,
        "level": user.level,
        "matric_number": user.matric_number,
    }


@app.get("/campus/gst/questions/{course_code}")
def campus_gst_questions(course_code: str, username: Optional[str] = Query(None), db=Depends(get_db)):
    # Free tier: 1 mock test total, for life. GST premium (plan containing "gst") is unlimited.
    # username is optional so this endpoint stays backward compatible with any caller
    # that doesn't send it (in which case no gating is applied).
    if username:
        row = db.execute(
            text("SELECT is_premium, premium_plan FROM campus_users WHERE username = :username"),
            {"username": username},
        ).fetchone()
        if row:
            is_premium, premium_plan = row
            gst_unlocked = is_premium and premium_plan and "gst" in premium_plan
            if not gst_unlocked:
                attempts_total = db.query(GSTResult).filter(
                    GSTResult.username == username,
                ).count()
                if attempts_total >= 1:
                    raise HTTPException(
                        status_code=402,
                        detail="You've used your free mock test. Upgrade to unlock unlimited General Course Tests.",
                    )

    questions = db.query(GSTQuestion).filter(GSTQuestion.course_code == course_code).all()
    if not questions:
        raise HTTPException(status_code=404, detail="No questions found for this course code")

    random.shuffle(questions)
    selected = questions[:30]

    return [
        {
            "id": q.id,
            "course_code": q.course_code,
            "question_text": q.question_text,
            "option_a": q.option_a,
            "option_b": q.option_b,
            "option_c": q.option_c,
            "option_d": q.option_d,
        }
        for q in selected
    ]


@app.post("/campus/gst/submit")
def campus_gst_submit(payload: GSTSubmitSchema, db=Depends(get_db)):
    question_ids = [int(qid) for qid in payload.answers.keys()]
    questions = db.query(GSTQuestion).filter(GSTQuestion.id.in_(question_ids)).all()
    if not questions:
        raise HTTPException(status_code=404, detail="No matching questions found")

    score = 0
    total = len(questions)
    breakdown = []

    for q in questions:
        chosen = payload.answers.get(str(q.id))
        is_correct = bool(chosen) and chosen.strip().upper() == (q.correct_answer or "").strip().upper()
        if is_correct:
            score += 1
        breakdown.append({
            "question_id": q.id,
            "question_text": q.question_text,
            "chosen_answer": chosen,
            "correct_answer": q.correct_answer,
            "is_correct": is_correct,
            "explanation": q.explanation,
        })

    percentage = round((score / total) * 100, 2) if total > 0 else 0.0

    result = GSTResult(
        username=payload.username,
        course_code=payload.course_code,
        score=score,
        total=total,
        percentage=percentage,
        time_taken=payload.time_taken,
        taken_at=now_str(),
    )
    db.add(result)
    db.commit()

    return {
        "success": True,
        "score": score,
        "total": total,
        "percentage": percentage,
        "results": breakdown,
    }


@app.get("/campus/profile/{username}")
def campus_profile(username: str, db=Depends(get_db)):
    results = db.query(GSTResult).filter(GSTResult.username == username).order_by(GSTResult.taken_at.desc()).all()

    if not results:
        return {
            "username": username,
            "total_tests_taken": 0,
            "highest_percentage": 0,
            "average_percentage": 0,
            "last_test": None,
        }

    total_tests = len(results)
    highest = max(r.percentage for r in results)
    average = round(sum(r.percentage for r in results) / total_tests, 2)
    last = results[0]

    return {
        "username": username,
        "total_tests_taken": total_tests,
        "highest_percentage": highest,
        "average_percentage": average,
        "last_test": {
            "course_code": last.course_code,
            "score": last.score,
            "total": last.total,
            "percentage": last.percentage,
            "taken_at": last.taken_at,
        },
    }


@app.post("/campus/feedback")
def campus_feedback(payload: CampusFeedbackSchema, db=Depends(get_db)):
    fb = CampusFeedback(
        username=payload.username,
        rating=payload.rating,
        message=payload.message,
        created_at=now_str(),
    )
    db.add(fb)
    db.commit()
    return {"success": True, "message": "Feedback received"}


@app.get("/campus/gst/courses")
def campus_gst_courses(db=Depends(get_db)):
    rows = db.query(GSTQuestion.course_code, GSTQuestion.course_title).distinct().all()

    if not rows:
        return {
            "courses": [
                {"course_code": "GST111", "course_title": "Communication in English I"},
                {"course_code": "GST112", "course_title": "Nigerian Peoples and Culture"},
                {"course_code": "GST113", "course_title": "Communication in English II"},
                {"course_code": "GST122", "course_title": "Logic, Philosophy and Human Existence"},
                {"course_code": "GST123", "course_title": "Peace and Conflict Resolution"},
            ]
        }

    return {"courses": [{"course_code": c, "course_title": t} for c, t in rows]}


# ============================================================
# PREMIUM + PAYSTACK PAYMENTS MODULE - NEW ADDITIONS ONLY
# Covers both OX-Bridge Learning Hub ("oxbridge" -> users table)
# and AKSU Smart Hub ("campus" -> campus_users table).
# Nothing above this line was touched.
# ============================================================

PAYSTACK_SECRET_KEY = os.environ.get("PAYSTACK_SECRET_KEY")
PAYSTACK_BASE_URL = "https://api.paystack.co"

# ------------------------------------------------------------
# FIX: campus_users / gst_questions / gst_results / campus_feedback
# were defined AFTER the original Base.metadata.create_all() call
# earlier in this file, so they were never actually created in the
# database. This second call (safe, checkfirst=True) picks up any
# model class defined anywhere in the file, including the Campus
# module above and everything in this module.
# ------------------------------------------------------------
try:
    Base.metadata.create_all(bind=engine, checkfirst=True)
    print("[DB] Campus + Premium tables created/verified [OK]")
except Exception as e:
    print(f"[DB ERROR - campus/premium create_all] {e}")

# ------------------------------------------------------------
# campus_users premium columns - must run AFTER the create_all
# fix above, since campus_users did not exist any earlier in
# the file's execution order.
# ------------------------------------------------------------
def run_campus_premium_migrations():
    migrations = [
        "ALTER TABLE campus_users ADD COLUMN IF NOT EXISTS is_premium BOOLEAN DEFAULT FALSE;",
        "ALTER TABLE campus_users ADD COLUMN IF NOT EXISTS premium_expires VARCHAR;",
        "ALTER TABLE campus_users ADD COLUMN IF NOT EXISTS premium_plan VARCHAR;",
        "ALTER TABLE campus_users ADD COLUMN IF NOT EXISTS ai_requests_today INTEGER DEFAULT 0;",
        "ALTER TABLE campus_users ADD COLUMN IF NOT EXISTS ai_requests_date VARCHAR;",
        "ALTER TABLE campus_users ADD COLUMN IF NOT EXISTS email VARCHAR;",
        "ALTER TABLE campus_users ADD COLUMN IF NOT EXISTS profile_pic VARCHAR;",
    ]
    try:
        with engine.connect() as conn:
            for sql in migrations:
                try:    conn.execute(text(sql))
                except: pass
            conn.commit()
        print("[DB] Campus premium migrations complete [OK]")
    except Exception as e:
        print(f"[DB MIGRATION ERROR - campus premium] {e}")

run_campus_premium_migrations()


# ------------------------------------------------------------
# NEW PYDANTIC SCHEMAS
# ------------------------------------------------------------
class PaymentInitSchema(BaseModel):
    username: str
    email: str
    amount: int          # amount in NAIRA (e.g. 1500) - converted to kobo before calling Paystack
    plan: str             # "monthly" | "biweekly" | "weekly"
    app_type: str          # "oxbridge" | "campus"
    callback_url: str


class PaymentVerifySchema(BaseModel):
    reference: str
    username: str
    app_type: str


class AiRequestCheckSchema(BaseModel):
    username: str
    app_type: str


# ------------------------------------------------------------
# NEW HELPERS (local to this module)
# ------------------------------------------------------------
PLAN_DURATIONS = {
    "monthly": 30,
    "biweekly": 14,
    "weekly": 7,
}

def premium_table_for(app_type: str) -> str:
    if app_type == "oxbridge":
        return "users"
    if app_type == "campus":
        return "campus_users"
    raise HTTPException(status_code=400, detail="Invalid app_type. Must be 'oxbridge' or 'campus'")


def calculate_expiry(plan: str) -> str:
    days = PLAN_DURATIONS.get(plan, 30)  # unrecognized plan -> default 30 days
    return (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y-%m-%d")


def add_paystack_fee(target_naira: float) -> int:
    """
    Grosses up a price so that after Paystack deducts its fee, the
    business still receives the full target amount. Based on Paystack's
    2026 Nigeria local-card pricing: 1.5% + N100 flat, with the N100
    waived for transactions <= N2,500, and total fee capped at N2,000.
    Returns a whole naira amount (rounded up) to charge the customer.
    """
    PCT = 0.015
    FLAT = 100
    WAIVER_THRESHOLD = 2500
    FEE_CAP = 2000

    # Try without the flat fee first (covers all current plan prices)
    charge_no_flat = target_naira / (1 - PCT)
    if charge_no_flat <= WAIVER_THRESHOLD:
        return math.ceil(charge_no_flat)

    # Flat fee applies once grossed-up charge crosses the waiver line
    charge_with_flat = (target_naira + FLAT) / (1 - PCT)
    fee = charge_with_flat - target_naira
    if fee > FEE_CAP:
        return math.ceil(target_naira + FEE_CAP)
    return math.ceil(charge_with_flat)


def apply_premium_update(db, app_type: str, username: str, plan: str, expiry: str):
    table = premium_table_for(app_type)
    db.execute(
        text(f"""
            UPDATE {table}
            SET is_premium = TRUE,
                premium_expires = :expiry,
                premium_plan = :plan
            WHERE username = :username
        """),
        {"expiry": expiry, "plan": plan, "username": username},
    )
    db.commit()


# ------------------------------------------------------------
# NEW ENDPOINTS - PAYMENTS
# ------------------------------------------------------------

@app.post("/payment/initialize")
def payment_initialize(payload: PaymentInitSchema):
    if not PAYSTACK_SECRET_KEY:
        raise HTTPException(status_code=500, detail="Payment provider is not configured")

    amount_kobo = add_paystack_fee(payload.amount) * 100  # gross up so Paystack's cut doesn't eat into your revenue

    try:
        res = httpx.post(
            f"{PAYSTACK_BASE_URL}/transaction/initialize",
            headers={
                "Authorization": f"Bearer {PAYSTACK_SECRET_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "email": payload.email,
                "amount": amount_kobo,
                "callback_url": payload.callback_url,
                "metadata": {
                    "username": payload.username,
                    "plan": payload.plan,
                    "app_type": payload.app_type,
                },
            },
            timeout=15,
        )
        data = res.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Could not reach payment provider: {str(e)}")

    if not data.get("status"):
        raise HTTPException(status_code=400, detail=data.get("message", "Could not initialize payment"))

    return {
        "authorization_url": data["data"]["authorization_url"],
        "reference": data["data"]["reference"],
        "status": data["status"],
    }


@app.post("/payment/verify")
def payment_verify(payload: PaymentVerifySchema, db=Depends(get_db)):
    if not PAYSTACK_SECRET_KEY:
        raise HTTPException(status_code=500, detail="Payment provider is not configured")

    try:
        res = httpx.get(
            f"{PAYSTACK_BASE_URL}/transaction/verify/{payload.reference}",
            headers={"Authorization": f"Bearer {PAYSTACK_SECRET_KEY}"},
            timeout=15,
        )
        data = res.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Could not reach payment provider: {str(e)}")

    tx = data.get("data", {})
    if tx.get("status") != "success":
        raise HTTPException(status_code=400, detail="Payment not successful")

    metadata = tx.get("metadata", {}) or {}
    plan = metadata.get("plan", "monthly")
    app_type = payload.app_type or metadata.get("app_type")

    expiry = calculate_expiry(plan)
    apply_premium_update(db, app_type, payload.username, plan, expiry)

    return {"success": True, "message": "Premium activated", "premium_expires": expiry, "premium_plan": plan}


@app.post("/payment/webhook")
async def payment_webhook(request: Request, db=Depends(get_db)):
    if not PAYSTACK_SECRET_KEY:
        raise HTTPException(status_code=500, detail="Payment provider is not configured")

    raw_body = await request.body()
    signature = request.headers.get("x-paystack-signature", "")

    computed_signature = hmac.new(
        PAYSTACK_SECRET_KEY.encode("utf-8"),
        raw_body,
        hashlib.sha512,
    ).hexdigest()

    if not hmac.compare_digest(computed_signature, signature):
        raise HTTPException(status_code=400, detail="Invalid webhook signature")

    payload = json.loads(raw_body)

    if payload.get("event") == "charge.success":
        tx = payload.get("data", {})
        metadata = tx.get("metadata", {}) or {}
        username = metadata.get("username")
        plan = metadata.get("plan", "monthly")
        app_type = metadata.get("app_type")

        if username and app_type:
            expiry = calculate_expiry(plan)
            try:
                apply_premium_update(db, app_type, username, plan, expiry)
            except Exception as e:
                print(f"[WEBHOOK UPDATE ERROR] {e}")

    return {"status": "ok"}


@app.get("/premium/status/{username}")
def premium_status(username: str, db=Depends(get_db)):
    row = db.execute(
        text("SELECT is_premium, premium_expires, premium_plan FROM users WHERE username = :username"),
        {"username": username},
    ).fetchone()
    app_type = "oxbridge"

    if not row:
        row = db.execute(
            text("SELECT is_premium, premium_expires, premium_plan FROM campus_users WHERE username = :username"),
            {"username": username},
        ).fetchone()
        app_type = "campus"

    if not row:
        raise HTTPException(status_code=404, detail="User not found")

    is_premium, premium_expires, premium_plan = row

    days_remaining = 0
    if premium_expires:
        try:
            expiry_date = datetime.strptime(premium_expires, "%Y-%m-%d")
            days_remaining = max((expiry_date - datetime.now()).days, 0)
        except Exception:
            days_remaining = 0

    if days_remaining <= 0:
        is_premium = False

    return {
        "is_premium": bool(is_premium),
        "premium_expires": premium_expires,
        "premium_plan": premium_plan,
        "days_remaining": days_remaining,
        "app_type": app_type,
    }


@app.get("/ai/check-limit/{username}")
def ai_check_limit(username: str, app_type: str = Query(...), db=Depends(get_db)):
    table = premium_table_for(app_type)

    row = db.execute(
        text(f"SELECT is_premium, premium_plan, ai_requests_today, ai_requests_date FROM {table} WHERE username = :username"),
        {"username": username},
    ).fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="User not found")

    is_premium, premium_plan, requests_today, requests_date = row
    today = today_str()

    if requests_date != today:
        requests_today = 0

    # For the campus app, a user may have paid for only the GST Mock plan
    # (not AI). Only a plan containing "ai" (or the oxbridge app, which has
    # a single unified premium tier) unlocks premium AI Assistant access.
    ai_unlocked = is_premium and (app_type != "campus" or (premium_plan and "ai" in premium_plan))

    # Premium is generous but not literally unlimited, to protect against
    # runaway AI provider costs from a single account. This cap is enforced
    # server-side only - the frontend does not advertise a specific number,
    # so it doesn't read as an invitation to max it out.
    PREMIUM_DAILY_CAP = 150

    limit = PREMIUM_DAILY_CAP if ai_unlocked else 2
    can_send = requests_today < limit

    return {
        "can_send": can_send,
        "requests_today": requests_today or 0,
        "limit": limit,
        "is_premium": bool(ai_unlocked),
    }


@app.post("/ai/increment-request")
def ai_increment_request(payload: AiRequestCheckSchema, db=Depends(get_db)):
    table = premium_table_for(payload.app_type)
    today = today_str()

    row = db.execute(
        text(f"SELECT ai_requests_today, ai_requests_date FROM {table} WHERE username = :username"),
        {"username": payload.username},
    ).fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="User not found")

    requests_today, requests_date = row
    new_count = 1 if requests_date != today else (requests_today or 0) + 1

    db.execute(
        text(f"""
            UPDATE {table}
            SET ai_requests_today = :count, ai_requests_date = :today
            WHERE username = :username
        """),
        {"count": new_count, "today": today, "username": payload.username},
    )
    db.commit()

    return {"success": True, "requests_today": new_count}


# ============================================================
# DEPARTMENT / COURSE-SPECIFIC PAST QUESTIONS MODULE
# Separate from GSTQuestion (general courses everyone takes) -
# this is for courses specific to a student's own department,
# e.g. CSC201 for Computer Science, MED301 for Medicine.
# Kept as its own table so it never mixes with OX-Bridge
# Learning Hub's WAEC/JAMB question bank (ManualQuestion /
# PastQuestion), which uses a different subject+level shape.
# ============================================================

class CampusCourseQuestion(Base):
    __tablename__ = "campus_course_questions"
    id             = Column(Integer, primary_key=True, index=True)
    department     = Column(String, index=True)   # e.g. "Computer Science"
    level          = Column(String, index=True)   # e.g. "200"
    course_code    = Column(String, index=True)   # e.g. "CSC201"
    course_title   = Column(String)                # e.g. "Data Structures"
    question_text  = Column(String)
    option_a       = Column(String)
    option_b       = Column(String)
    option_c       = Column(String)
    option_d       = Column(String)
    correct_answer = Column(String)
    explanation    = Column(String, nullable=True)
    university     = Column(String, default="AKSU")


class CourseTestResult(Base):
    __tablename__ = "course_test_results"
    id          = Column(Integer, primary_key=True, index=True)
    username    = Column(String, index=True)
    course_code = Column(String)
    score       = Column(Integer)
    total       = Column(Integer)
    percentage  = Column(Float)
    time_taken  = Column(Integer, nullable=True)
    taken_at    = Column(String)


try:
    Base.metadata.create_all(bind=engine, checkfirst=True)
    print("[DB] Course-specific question tables created/verified [OK]")
except Exception as e:
    print(f"[DB ERROR - course questions create_all] {e}")


# ------------------------------------------------------------
# NEW PYDANTIC SCHEMAS
# ------------------------------------------------------------
class CourseSubmitSchema(BaseModel):
    username: str
    course_code: str
    answers: dict
    time_taken: Optional[int] = None


# ------------------------------------------------------------
# NEW ENDPOINTS
# ------------------------------------------------------------

@app.get("/campus/courses/available")
def campus_courses_available(department: str = Query(...), level: str = Query(...), db=Depends(get_db)):
    rows = db.query(CampusCourseQuestion.course_code, CampusCourseQuestion.course_title).filter(
        CampusCourseQuestion.department.ilike(f"%{department}%"),
        CampusCourseQuestion.level == level,
    ).distinct().all()

    return {"courses": [{"course_code": c, "course_title": t} for c, t in rows]}


@app.get("/campus/courses/questions/{course_code}")
def campus_courses_questions(course_code: str, username: Optional[str] = Query(None), db=Depends(get_db)):
    # Same free-tier model as General Course Test: 1 free test for life,
    # unlimited if the user's premium_plan contains "gst" (the existing
    # GST Mock Premium plan covers both General Course Test and this
    # department-specific test feature - one test-taking premium tier
    # rather than splitting pricing further. Revisit if you'd rather
    # price this separately.)
    if username:
        row = db.execute(
            text("SELECT is_premium, premium_plan FROM campus_users WHERE username = :username"),
            {"username": username},
        ).fetchone()
        if row:
            is_premium, premium_plan = row
            unlocked = is_premium and premium_plan and "gst" in premium_plan
            if not unlocked:
                attempts_total = db.query(CourseTestResult).filter(
                    CourseTestResult.username == username,
                ).count()
                if attempts_total >= 1:
                    raise HTTPException(
                        status_code=402,
                        detail="You've used your free course test. Upgrade to unlock unlimited practice tests.",
                    )

    questions = db.query(CampusCourseQuestion).filter(CampusCourseQuestion.course_code == course_code).all()
    if not questions:
        raise HTTPException(status_code=404, detail="No questions found for this course code")

    random.shuffle(questions)
    selected = questions[:30]

    return [
        {
            "id": q.id,
            "course_code": q.course_code,
            "question_text": q.question_text,
            "option_a": q.option_a,
            "option_b": q.option_b,
            "option_c": q.option_c,
            "option_d": q.option_d,
        }
        for q in selected
    ]


@app.post("/campus/courses/submit")
def campus_courses_submit(payload: CourseSubmitSchema, db=Depends(get_db)):
    question_ids = [int(qid) for qid in payload.answers.keys()]
    questions = db.query(CampusCourseQuestion).filter(CampusCourseQuestion.id.in_(question_ids)).all()
    if not questions:
        raise HTTPException(status_code=404, detail="No matching questions found")

    score = 0
    total = len(questions)
    breakdown = []

    for q in questions:
        chosen = payload.answers.get(str(q.id))
        is_correct = bool(chosen) and chosen.strip().upper() == (q.correct_answer or "").strip().upper()
        if is_correct:
            score += 1
        breakdown.append({
            "question_id": q.id,
            "question_text": q.question_text,
            "chosen_answer": chosen,
            "correct_answer": q.correct_answer,
            "is_correct": is_correct,
            "explanation": q.explanation,
        })

    percentage = round((score / total) * 100, 2) if total > 0 else 0.0

    result = CourseTestResult(
        username=payload.username,
        course_code=payload.course_code,
        score=score,
        total=total,
        percentage=percentage,
        time_taken=payload.time_taken,
        taken_at=now_str(),
    )
    db.add(result)
    db.commit()

    return {
        "success": True,
        "score": score,
        "total": total,
        "percentage": percentage,
        "results": breakdown,
    }


# ============================================================
# CLOUDINARY IMAGE UPLOAD MODULE
# Generic image upload endpoint - reusable for profile pictures,
# topic diagrams, GST/course question images, etc. Reads config
# from the CLOUDINARY_URL environment variable automatically
# (format: cloudinary://<api_key>:<api_secret>@<cloud_name>),
# so no cloudinary.config() call is needed on this end.
# If the cloudinary package isn't installed yet, or the env var
# isn't set, this endpoint fails gracefully instead of crashing
# the whole app on startup.
# ============================================================

try:
    import cloudinary
    import cloudinary.uploader
    CLOUDINARY_SDK_AVAILABLE = True
except ImportError:
    CLOUDINARY_SDK_AVAILABLE = False

CLOUDINARY_CONFIGURED = CLOUDINARY_SDK_AVAILABLE and bool(os.environ.get("CLOUDINARY_URL"))


@app.post("/upload/image")
async def upload_image(file: UploadFile = File(...)):
    if not CLOUDINARY_SDK_AVAILABLE:
        raise HTTPException(status_code=500, detail="Image upload is not available - the cloudinary package is not installed on the server")
    if not CLOUDINARY_CONFIGURED:
        raise HTTPException(status_code=500, detail="Image upload is not configured - CLOUDINARY_URL is missing")

    try:
        result = cloudinary.uploader.upload(file.file)
        return {
            "success": True,
            "url": result.get("secure_url"),
            "public_id": result.get("public_id"),
        }
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Image upload failed: {str(e)}")


class ProfilePictureSchema(BaseModel):
    username: str
    image_url: str


@app.post("/campus/profile/picture")
def campus_update_profile_picture(payload: ProfilePictureSchema, db=Depends(get_db)):
    result = db.execute(
        text("UPDATE campus_users SET profile_pic = :url WHERE username = :username"),
        {"url": payload.image_url, "username": payload.username},
    )
    db.commit()
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="User not found")
    return {"success": True, "profile_pic": payload.image_url}


# ============================================================
# CGPA CALCULATOR MODULE
# Uses the standard Nigerian university 5-point grading scale
# (A=5, B=4, C=3, D=2, E=1, F=0). Students enter a letter grade
# per course rather than a raw score, since score-to-grade
# boundaries vary by course/lecturer - the letter grade itself
# is the actual input that matters for CGPA math.
# ============================================================

class CGPARecord(Base):
    __tablename__ = "cgpa_records"
    id           = Column(Integer, primary_key=True, index=True)
    username     = Column(String, index=True)
    session      = Column(String, nullable=True)   # e.g. "2025/2026"
    level        = Column(String, nullable=True)   # e.g. "200"
    semester     = Column(String, nullable=True)   # e.g. "First" / "Second"
    gpa          = Column(Float)
    total_units  = Column(Integer)
    quality_points = Column(Float)
    created_at   = Column(String)


try:
    Base.metadata.create_all(bind=engine, checkfirst=True)
    print("[DB] CGPA table created/verified [OK]")
except Exception as e:
    print(f"[DB ERROR - cgpa create_all] {e}")


class CGPASaveSchema(BaseModel):
    username: str
    session: Optional[str] = None
    level: Optional[str] = None
    semester: Optional[str] = None
    gpa: float
    total_units: int
    quality_points: float


@app.post("/campus/cgpa/save")
def campus_cgpa_save(payload: CGPASaveSchema, db=Depends(get_db)):
    record = CGPARecord(
        username=payload.username,
        session=payload.session,
        level=payload.level,
        semester=payload.semester,
        gpa=payload.gpa,
        total_units=payload.total_units,
        quality_points=payload.quality_points,
        created_at=now_str(),
    )
    db.add(record)
    db.commit()

    all_records = db.query(CGPARecord).filter(CGPARecord.username == payload.username).all()
    total_units_all = sum(r.total_units for r in all_records)
    total_points_all = sum(r.quality_points for r in all_records)
    cumulative_cgpa = round(total_points_all / total_units_all, 2) if total_units_all > 0 else 0.0

    return {"success": True, "cumulative_cgpa": cumulative_cgpa, "semesters_recorded": len(all_records)}


@app.get("/campus/cgpa/history/{username}")
def campus_cgpa_history(username: str, db=Depends(get_db)):
    records = db.query(CGPARecord).filter(CGPARecord.username == username).order_by(CGPARecord.created_at.asc()).all()

    total_units_all = sum(r.total_units for r in records)
    total_points_all = sum(r.quality_points for r in records)
    cumulative_cgpa = round(total_points_all / total_units_all, 2) if total_units_all > 0 else 0.0

    return {
        "cumulative_cgpa": cumulative_cgpa,
        "semesters": [
            {
                "id": r.id,
                "session": r.session,
                "level": r.level,
                "semester": r.semester,
                "gpa": r.gpa,
                "total_units": r.total_units,
                "created_at": r.created_at,
            }
            for r in records
        ],
    }


# ============================================================
# ADMIN UPLOAD ENDPOINTS - DEPARTMENT COURSES + GST QUESTIONS
# Mirrors the existing /admin/add-question(s-bulk) pattern used
# for ManualQuestion, applied to the two campus question tables
# that never had a way to add data: CampusCourseQuestion and
# GSTQuestion. Without these, both tables can only ever be
# populated by hand-editing the database directly.
# ============================================================

class CampusCourseQuestionCreate(BaseModel):
    department:     str
    level:          str
    course_code:    str
    course_title:   str
    question_text:  str
    option_a:       str
    option_b:       str
    option_c:       str
    option_d:       str
    correct_answer: str
    explanation:    Optional[str] = None
    university:     Optional[str] = "AKSU"

class BulkCampusCourseQuestionsCreate(BaseModel):
    questions: List[CampusCourseQuestionCreate]


class GSTQuestionCreate(BaseModel):
    course_code:    str
    course_title:   str
    question_text:  str
    option_a:       str
    option_b:       str
    option_c:       str
    option_d:       str
    correct_answer: str
    explanation:    Optional[str] = None
    university:     Optional[str] = "AKSU"

class BulkGSTQuestionsCreate(BaseModel):
    questions: List[GSTQuestionCreate]


@app.post("/admin/campus/add-course-question")
def admin_add_course_question(data: CampusCourseQuestionCreate, db=Depends(get_db), admin_ok: bool = Depends(verify_admin)):
    q = CampusCourseQuestion(
        department=data.department.strip(), level=data.level.strip(),
        course_code=data.course_code.strip().upper(), course_title=data.course_title.strip(),
        question_text=data.question_text.strip(),
        option_a=data.option_a.strip(), option_b=data.option_b.strip(),
        option_c=data.option_c.strip(), option_d=data.option_d.strip(),
        correct_answer=data.correct_answer.upper().strip(),
        explanation=data.explanation, university=data.university or "AKSU",
    )
    db.add(q); db.commit(); db.refresh(q)
    return {"msg": "Question added", "id": q.id, "course_code": q.course_code}


@app.post("/admin/campus/add-course-questions-bulk")
def admin_add_course_questions_bulk(data: BulkCampusCourseQuestionsCreate, db=Depends(get_db), admin_ok: bool = Depends(verify_admin)):
    added = []
    for qd in data.questions:
        q = CampusCourseQuestion(
            department=qd.department.strip(), level=qd.level.strip(),
            course_code=qd.course_code.strip().upper(), course_title=qd.course_title.strip(),
            question_text=qd.question_text.strip(),
            option_a=qd.option_a.strip(), option_b=qd.option_b.strip(),
            option_c=qd.option_c.strip(), option_d=qd.option_d.strip(),
            correct_answer=qd.correct_answer.upper().strip(),
            explanation=qd.explanation, university=qd.university or "AKSU",
        )
        db.add(q); added.append({"course_code": q.course_code, "department": q.department})
    db.commit()
    return {"msg": f"{len(added)} questions added", "questions": added}


@app.post("/admin/campus/add-gst-question")
def admin_add_gst_question(data: GSTQuestionCreate, db=Depends(get_db), admin_ok: bool = Depends(verify_admin)):
    q = GSTQuestion(
        course_code=data.course_code.strip().upper(), course_title=data.course_title.strip(),
        question_text=data.question_text.strip(),
        option_a=data.option_a.strip(), option_b=data.option_b.strip(),
        option_c=data.option_c.strip(), option_d=data.option_d.strip(),
        correct_answer=data.correct_answer.upper().strip(),
        explanation=data.explanation, university=data.university or "AKSU",
    )
    db.add(q); db.commit(); db.refresh(q)
    return {"msg": "Question added", "id": q.id, "course_code": q.course_code}


@app.post("/admin/campus/add-gst-questions-bulk")
def admin_add_gst_questions_bulk(data: BulkGSTQuestionsCreate, db=Depends(get_db), admin_ok: bool = Depends(verify_admin)):
    added = []
    for qd in data.questions:
        q = GSTQuestion(
            course_code=qd.course_code.strip().upper(), course_title=qd.course_title.strip(),
            question_text=qd.question_text.strip(),
            option_a=qd.option_a.strip(), option_b=qd.option_b.strip(),
            option_c=qd.option_c.strip(), option_d=qd.option_d.strip(),
            correct_answer=qd.correct_answer.upper().strip(),
            explanation=qd.explanation, university=qd.university or "AKSU",
        )
        db.add(q); added.append({"course_code": q.course_code})
    db.commit()
    return {"msg": f"{len(added)} questions added", "questions": added}


# ============================================================
# CAMPUS ADMIN MONITORING - mirrors the existing /admin/monitor/*
# pattern built for OX-Bridge Hub, applied to campus_users +
# ActivityLog (now that campus paths are also logged above).
# All routes require the same X-Admin-Key header as every other
# /admin/* endpoint.
# ============================================================

@app.get("/admin/campus/monitor/overview")
def campus_monitor_overview(db=Depends(get_db), admin_ok: bool = Depends(verify_admin)):
    today = today_str()
    total_users = db.query(CampusUser).count()
    premium_users = db.execute(
        text("SELECT COUNT(*) FROM campus_users WHERE is_premium = TRUE")
    ).scalar()
    active_today = db.query(ActivityLog.username).filter(
        ActivityLog.created_at.like(f"{today}%"),
        ActivityLog.action.like("campus_%") | ActivityLog.action.in_(["ai_chat", "payment_initiated", "payment_verified"]),
        ActivityLog.username.isnot(None),
    ).distinct().count()
    signups_today = db.query(ActivityLog).filter(
        ActivityLog.action == "campus_register", ActivityLog.created_at.like(f"{today}%")
    ).count()
    tests_today = db.query(GSTResult).filter(GSTResult.taken_at.like(f"{today}%")).count() \
        + db.query(CourseTestResult).filter(CourseTestResult.taken_at.like(f"{today}%")).count()

    return {
        "total_users": total_users,
        "premium_users": premium_users or 0,
        "active_users_today": active_today,
        "signups_today": signups_today,
        "mock_tests_today": tests_today,
    }


@app.get("/admin/campus/monitor/activity")
def campus_monitor_activity(username: str = None, action: str = None, limit: int = 50, db=Depends(get_db), admin_ok: bool = Depends(verify_admin)):
    query = db.query(ActivityLog)
    if username: query = query.filter(ActivityLog.username == username)
    if action:   query = query.filter(ActivityLog.action == action)
    logs = query.order_by(ActivityLog.id.desc()).limit(limit).all()
    return [{"id": l.id, "username": l.username, "action": l.action,
              "path": l.path, "created_at": l.created_at} for l in logs]


@app.get("/admin/campus/monitor/usage/{username}")
def campus_monitor_user_usage(username: str, db=Depends(get_db), admin_ok: bool = Depends(verify_admin)):
    user = db.query(CampusUser).filter(CampusUser.username == username).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    logs = db.query(ActivityLog).filter(ActivityLog.username == username).all()
    by_action = {}
    for l in logs:
        by_action[l.action] = by_action.get(l.action, 0) + 1

    premium_row = db.execute(
        text("SELECT is_premium, premium_plan, premium_expires FROM campus_users WHERE username = :u"),
        {"u": username},
    ).fetchone()

    return {
        "username": username,
        "full_name": user.full_name,
        "department": user.department,
        "level": user.level,
        "total_actions": len(logs),
        "breakdown": by_action,
        "is_premium": bool(premium_row[0]) if premium_row else False,
        "premium_plan": premium_row[1] if premium_row else None,
        "premium_expires": premium_row[2] if premium_row else None,
        "gst_tests_taken": db.query(GSTResult).filter(GSTResult.username == username).count(),
        "course_tests_taken": db.query(CourseTestResult).filter(CourseTestResult.username == username).count(),
    }


@app.get("/admin/campus/users")
def campus_list_users(limit: int = 100, db=Depends(get_db), admin_ok: bool = Depends(verify_admin)):
    """Full roster for the admin dashboard - who's signed up, department, premium status."""
    rows = db.execute(
        text("""SELECT username, full_name, email, department, level, matric_number,
                        is_premium, premium_plan, created_at
                 FROM campus_users ORDER BY id DESC LIMIT :limit"""),
        {"limit": limit},
    ).fetchall()
    return [
        {
            "username": r[0], "full_name": r[1], "email": r[2], "department": r[3],
            "level": r[4], "matric_number": r[5], "is_premium": bool(r[6]),
            "premium_plan": r[7], "created_at": r[8],
        }
        for r in rows
    ]


# ============================================================
# ADMIN <-> STUDENT MESSAGING (in-app notifications/announcements)
# Lets an admin message one specific student or broadcast to all
# campus students - shows up inside the app, not via email/SMS.
# ============================================================

class CampusNotification(Base):
    __tablename__ = "campus_notifications"
    id         = Column(Integer, primary_key=True, index=True)
    username   = Column(String, index=True, nullable=True)  # null = broadcast to everyone
    message    = Column(String)
    from_admin = Column(Boolean, default=True)
    is_read    = Column(Boolean, default=False)
    created_at = Column(String)

try:
    Base.metadata.create_all(bind=engine, checkfirst=True)
    print("[DB] Campus notifications table created/verified [OK]")
except Exception as e:
    print(f"[DB ERROR - campus notifications create_all] {e}")


class AdminNotifySchema(BaseModel):
    username: Optional[str] = None  # omit or leave null to broadcast to every student
    message: str


@app.post("/admin/campus/notify")
def admin_campus_notify(payload: AdminNotifySchema, db=Depends(get_db), admin_ok: bool = Depends(verify_admin)):
    note = CampusNotification(
        username=payload.username,  # None = broadcast
        message=payload.message,
        from_admin=True,
        is_read=False,
        created_at=now_str(),
    )
    db.add(note)
    db.commit()
    return {"success": True, "broadcast": payload.username is None}


@app.get("/campus/notifications/{username}")
def campus_get_notifications(username: str, db=Depends(get_db)):
    """Student-facing: fetch messages sent directly to them plus any broadcasts."""
    notes = db.query(CampusNotification).filter(
        (CampusNotification.username == username) | (CampusNotification.username.is_(None))
    ).order_by(CampusNotification.id.desc()).limit(50).all()

    return [
        {"id": n.id, "message": n.message, "is_read": n.is_read, "created_at": n.created_at}
        for n in notes
    ]


@app.post("/campus/notifications/{notification_id}/read")
def campus_mark_notification_read(notification_id: int, db=Depends(get_db)):
    note = db.query(CampusNotification).filter(CampusNotification.id == notification_id).first()
    if not note:
        raise HTTPException(status_code=404, detail="Notification not found")
    note.is_read = True
    db.commit()
    return {"success": True}


# ============================================================
# STUDENT COMMUNITY - WHATSAPP-STYLE MESSAGING MODULE
# Group chats (including auto department groups) + direct 1:1
# messages, image/file sharing via Cloudinary, read receipts,
# and online/last-seen presence. All messages are persisted -
# the existing /ws/classroom/{room} socket only broadcast live
# and never saved anything, so this is a separate, proper system.
#
# Architecture: REST endpoints are the source of truth (every
# send saves to the database first), then push the new message
# out over WebSocket to anyone currently connected to that
# conversation. This is the same pattern real chat apps use -
# it means message history is never lost even if nobody was
# online to receive it live.
# ============================================================

class CommunityGroup(Base):
    __tablename__ = "community_groups"
    id           = Column(Integer, primary_key=True, index=True)
    name         = Column(String)
    description  = Column(String, nullable=True)
    department   = Column(String, nullable=True)   # null = open to every student
    icon_url     = Column(String, nullable=True)
    created_by   = Column(String, nullable=True)
    is_official  = Column(Boolean, default=False)   # true for admin-created department groups
    created_at   = Column(String)

class CommunityGroupMember(Base):
    __tablename__ = "community_group_members"
    id        = Column(Integer, primary_key=True, index=True)
    group_id  = Column(Integer, ForeignKey("community_groups.id"))
    username  = Column(String, index=True)
    role      = Column(String, default="member")   # member / admin
    joined_at = Column(String)

class CommunityMessage(Base):
    __tablename__ = "community_messages"
    id               = Column(Integer, primary_key=True, index=True)
    conversation_id  = Column(String, index=True)   # "group:<id>" or "dm:<userA>:<userB>" (usernames sorted alphabetically)
    sender_username  = Column(String, index=True)
    message_text     = Column(String, nullable=True)
    image_url        = Column(String, nullable=True)
    file_url         = Column(String, nullable=True)
    file_name        = Column(String, nullable=True)
    message_type     = Column(String, default="text")   # text / image / file / voice
    voice_duration   = Column(Integer, nullable=True)    # seconds, only for message_type="voice"
    is_edited        = Column(Boolean, default=False)
    edited_at        = Column(String, nullable=True)
    is_deleted       = Column(Boolean, default=False)
    created_at       = Column(String, index=True)

class MessageReadReceipt(Base):
    __tablename__ = "message_read_receipts"
    id         = Column(Integer, primary_key=True, index=True)
    message_id = Column(Integer, ForeignKey("community_messages.id"))
    username   = Column(String, index=True)
    read_at    = Column(String)

class UserPresence(Base):
    __tablename__ = "user_presence"
    username   = Column(String, primary_key=True, index=True)
    is_online  = Column(Boolean, default=False)
    last_seen  = Column(String, nullable=True)


try:
    Base.metadata.create_all(bind=engine, checkfirst=True)
    print("[DB] Community messaging tables created/verified [OK]")
except Exception as e:
    print(f"[DB ERROR - community create_all] {e}")

def run_community_migrations():
    migrations = [
        "ALTER TABLE community_messages ADD COLUMN IF NOT EXISTS voice_duration INTEGER;",
        "ALTER TABLE community_messages ADD COLUMN IF NOT EXISTS is_edited BOOLEAN DEFAULT FALSE;",
        "ALTER TABLE community_messages ADD COLUMN IF NOT EXISTS edited_at VARCHAR;",
        "ALTER TABLE community_messages ADD COLUMN IF NOT EXISTS is_deleted BOOLEAN DEFAULT FALSE;",
    ]
    try:
        with engine.connect() as conn:
            for sql in migrations:
                try:    conn.execute(text(sql))
                except: pass
            conn.commit()
        print("[DB] Community messaging migrations complete [OK]")
    except Exception as e:
        print(f"[DB MIGRATION ERROR - community] {e}")

run_community_migrations()


def dm_conversation_id(user_a: str, user_b: str) -> str:
    """Deterministic conversation id for a DM thread regardless of who sends first."""
    a, b = sorted([user_a, user_b])
    return f"dm:{a}:{b}"


# ------------------------------------------------------------
# Live push - separate connection registry from the old
# /ws/classroom/{room} socket, keyed by conversation_id so it
# works for both group chats and DMs the same way.
# ------------------------------------------------------------
community_connections: dict = {}

async def broadcast_to_conversation(conversation_id: str, payload: dict):
    for conn in community_connections.get(conversation_id, []):
        try:
            await conn.send_json(payload)
        except Exception:
            pass


@app.websocket("/ws/community/{conversation_id}")
async def community_websocket(websocket: WebSocket, conversation_id: str):
    await websocket.accept()
    if conversation_id not in community_connections:
        community_connections[conversation_id] = []
    community_connections[conversation_id].append(websocket)
    try:
        while True:
            # Only used for ephemeral events (typing indicators) - actual
            # messages go through the REST send endpoints below so they
            # always get persisted, even if nobody is connected to see them live.
            data = await websocket.receive_json()
            if data.get("type") == "typing":
                for conn in community_connections[conversation_id]:
                    if conn != websocket:
                        await conn.send_json({"type": "typing", "username": data.get("username")})
    except WebSocketDisconnect:
        if websocket in community_connections.get(conversation_id, []):
            community_connections[conversation_id].remove(websocket)


# ------------------------------------------------------------
# SCHEMAS
# ------------------------------------------------------------
class GroupCreateSchema(BaseModel):
    name: str
    description: Optional[str] = None
    department: Optional[str] = None
    icon_url: Optional[str] = None
    created_by: str

class GroupMemberActionSchema(BaseModel):
    username: str

class SendGroupMessageSchema(BaseModel):
    username: str
    message_text: Optional[str] = None
    image_url: Optional[str] = None
    file_url: Optional[str] = None
    file_name: Optional[str] = None
    is_voice: Optional[bool] = False
    voice_duration: Optional[int] = None   # seconds

class SendDMSchema(BaseModel):
    from_username: str
    to_username: str
    message_text: Optional[str] = None
    image_url: Optional[str] = None
    file_url: Optional[str] = None
    file_name: Optional[str] = None
    is_voice: Optional[bool] = False
    voice_duration: Optional[int] = None   # seconds

class ReadReceiptSchema(BaseModel):
    username: str

class PresencePingSchema(BaseModel):
    username: str


# ------------------------------------------------------------
# GROUPS
# ------------------------------------------------------------
@app.post("/community/groups/create")
def community_create_group(payload: GroupCreateSchema, db=Depends(get_db)):
    group = CommunityGroup(
        name=payload.name, description=payload.description, department=payload.department,
        icon_url=payload.icon_url, created_by=payload.created_by, is_official=False,
        created_at=now_str(),
    )
    db.add(group); db.commit(); db.refresh(group)
    db.add(CommunityGroupMember(group_id=group.id, username=payload.created_by, role="admin", joined_at=now_str()))
    db.commit()
    return {"success": True, "group_id": group.id}


@app.get("/community/groups")
def community_list_groups(department: str = Query(None), username: str = Query(None), db=Depends(get_db)):
    query = db.query(CommunityGroup)
    if department:
        query = query.filter((CommunityGroup.department == department) | (CommunityGroup.department.is_(None)))
    groups = query.order_by(CommunityGroup.is_official.desc(), CommunityGroup.id.desc()).all()

    joined_ids = set()
    if username:
        joined_ids = {m.group_id for m in db.query(CommunityGroupMember).filter(CommunityGroupMember.username == username).all()}

    result = []
    for g in groups:
        member_count = db.query(CommunityGroupMember).filter(CommunityGroupMember.group_id == g.id).count()
        result.append({
            "id": g.id, "name": g.name, "description": g.description, "department": g.department,
            "icon_url": g.icon_url, "is_official": g.is_official, "member_count": member_count,
            "joined": g.id in joined_ids,
        })
    return result


@app.post("/community/groups/{group_id}/join")
def community_join_group(group_id: int, payload: GroupMemberActionSchema, db=Depends(get_db)):
    existing = db.query(CommunityGroupMember).filter(
        CommunityGroupMember.group_id == group_id, CommunityGroupMember.username == payload.username
    ).first()
    if existing:
        return {"success": True, "already_joined": True}
    db.add(CommunityGroupMember(group_id=group_id, username=payload.username, role="member", joined_at=now_str()))
    db.commit()
    return {"success": True}


@app.post("/community/groups/{group_id}/leave")
def community_leave_group(group_id: int, payload: GroupMemberActionSchema, db=Depends(get_db)):
    member = db.query(CommunityGroupMember).filter(
        CommunityGroupMember.group_id == group_id, CommunityGroupMember.username == payload.username
    ).first()
    if member:
        db.delete(member); db.commit()
    return {"success": True}


@app.get("/community/groups/{group_id}/members")
def community_group_members(group_id: int, db=Depends(get_db)):
    members = db.query(CommunityGroupMember).filter(CommunityGroupMember.group_id == group_id).all()
    return [{"username": m.username, "role": m.role, "joined_at": m.joined_at} for m in members]


# ------------------------------------------------------------
# GROUP MESSAGES
# ------------------------------------------------------------
@app.post("/community/groups/{group_id}/send")
async def community_send_group_message(group_id: int, payload: SendGroupMessageSchema, db=Depends(get_db)):
    if not payload.message_text and not payload.image_url and not payload.file_url:
        raise HTTPException(status_code=400, detail="Message must have text, an image, or a file")

    if payload.is_voice and payload.file_url:
        msg_type = "voice"
    elif payload.image_url:
        msg_type = "image"
    elif payload.file_url:
        msg_type = "file"
    else:
        msg_type = "text"
    conversation_id = f"group:{group_id}"

    msg = CommunityMessage(
        conversation_id=conversation_id, sender_username=payload.username,
        message_text=payload.message_text, image_url=payload.image_url,
        file_url=payload.file_url, file_name=payload.file_name,
        voice_duration=payload.voice_duration if msg_type == "voice" else None,
        message_type=msg_type, created_at=now_str(),
    )
    db.add(msg); db.commit(); db.refresh(msg)

    out = {
        "type": "message", "id": msg.id, "conversation_id": conversation_id,
        "sender_username": msg.sender_username, "message_text": msg.message_text,
        "image_url": msg.image_url, "file_url": msg.file_url, "file_name": msg.file_name,
        "voice_duration": msg.voice_duration, "message_type": msg.message_type,
        "is_edited": False, "is_deleted": False, "created_at": msg.created_at,
    }
    await broadcast_to_conversation(conversation_id, out)
    return {"success": True, "message": out}


def _serialize_message(m):
    if m.is_deleted:
        return {
            "id": m.id, "sender_username": m.sender_username, "message_text": "This message was deleted",
            "image_url": None, "file_url": None, "file_name": None, "voice_duration": None,
            "message_type": "deleted", "is_edited": False, "is_deleted": True, "created_at": m.created_at,
        }
    return {
        "id": m.id, "sender_username": m.sender_username, "message_text": m.message_text,
        "image_url": m.image_url, "file_url": m.file_url, "file_name": m.file_name,
        "voice_duration": m.voice_duration, "message_type": m.message_type,
        "is_edited": m.is_edited, "is_deleted": m.is_deleted, "created_at": m.created_at,
    }


@app.get("/community/groups/{group_id}/messages")
def community_group_history(group_id: int, before_id: int = Query(None), limit: int = Query(50), db=Depends(get_db)):
    conversation_id = f"group:{group_id}"
    query = db.query(CommunityMessage).filter(CommunityMessage.conversation_id == conversation_id)
    if before_id:
        query = query.filter(CommunityMessage.id < before_id)
    msgs = query.order_by(CommunityMessage.id.desc()).limit(limit).all()
    return [_serialize_message(m) for m in reversed(msgs)]


# ------------------------------------------------------------
# DIRECT MESSAGES
# ------------------------------------------------------------
@app.post("/community/dm/send")
async def community_send_dm(payload: SendDMSchema, db=Depends(get_db)):
    if not payload.message_text and not payload.image_url and not payload.file_url:
        raise HTTPException(status_code=400, detail="Message must have text, an image, or a file")

    if payload.is_voice and payload.file_url:
        msg_type = "voice"
    elif payload.image_url:
        msg_type = "image"
    elif payload.file_url:
        msg_type = "file"
    else:
        msg_type = "text"
    conversation_id = dm_conversation_id(payload.from_username, payload.to_username)

    msg = CommunityMessage(
        conversation_id=conversation_id, sender_username=payload.from_username,
        message_text=payload.message_text, image_url=payload.image_url,
        file_url=payload.file_url, file_name=payload.file_name,
        voice_duration=payload.voice_duration if msg_type == "voice" else None,
        message_type=msg_type, created_at=now_str(),
    )
    db.add(msg); db.commit(); db.refresh(msg)

    out = {
        "type": "message", "id": msg.id, "conversation_id": conversation_id,
        "sender_username": msg.sender_username, "message_text": msg.message_text,
        "image_url": msg.image_url, "file_url": msg.file_url, "file_name": msg.file_name,
        "voice_duration": msg.voice_duration, "message_type": msg.message_type,
        "is_edited": False, "is_deleted": False, "created_at": msg.created_at,
    }
    await broadcast_to_conversation(conversation_id, out)
    return {"success": True, "message": out}


@app.get("/community/dm/{username}/{other_username}/messages")
def community_dm_history(username: str, other_username: str, before_id: int = Query(None), limit: int = Query(50), db=Depends(get_db)):
    conversation_id = dm_conversation_id(username, other_username)
    query = db.query(CommunityMessage).filter(CommunityMessage.conversation_id == conversation_id)
    if before_id:
        query = query.filter(CommunityMessage.id < before_id)
    msgs = query.order_by(CommunityMessage.id.desc()).limit(limit).all()
    return [_serialize_message(m) for m in reversed(msgs)]


@app.get("/community/dm/{username}/conversations")
def community_dm_conversations(username: str, db=Depends(get_db)):
    """List of people this student has DM'd, with a preview of the last message - like WhatsApp's chat list."""
    rows = db.execute(
        text("""
            SELECT conversation_id, sender_username, message_text, message_type, created_at
            FROM community_messages
            WHERE conversation_id LIKE :prefix
            ORDER BY id DESC
        """),
        {"prefix": f"dm:%{username}%"},
    ).fetchall()

    seen = {}
    for r in rows:
        conv_id = r[0]
        parts = conv_id.split(":")
        if len(parts) != 3 or username not in (parts[1], parts[2]):
            continue
        other = parts[2] if parts[1] == username else parts[1]
        if other not in seen:
            preview = r[2] if r[3] == "text" else f"[{r[3]}]"
            seen[other] = {"with_username": other, "last_message": preview, "last_sender": r[1], "last_at": r[4]}
    return list(seen.values())


# ------------------------------------------------------------
# READ RECEIPTS
# ------------------------------------------------------------
@app.post("/community/messages/{message_id}/read")
def community_mark_read(message_id: int, payload: ReadReceiptSchema, db=Depends(get_db)):
    existing = db.query(MessageReadReceipt).filter(
        MessageReadReceipt.message_id == message_id, MessageReadReceipt.username == payload.username
    ).first()
    if existing:
        return {"success": True, "already_read": True}
    db.add(MessageReadReceipt(message_id=message_id, username=payload.username, read_at=now_str()))
    db.commit()
    return {"success": True}


@app.get("/community/messages/{message_id}/read-by")
def community_read_by(message_id: int, db=Depends(get_db)):
    receipts = db.query(MessageReadReceipt).filter(MessageReadReceipt.message_id == message_id).all()
    return [{"username": r.username, "read_at": r.read_at} for r in receipts]


# ------------------------------------------------------------
# EDIT / DELETE MESSAGES
# Only the original sender can edit or delete their own message.
# Delete is a soft-delete - the row stays (so message order and
# read receipts stay intact) but content is cleared and replaced
# with a placeholder in every response, matching how WhatsApp
# shows "This message was deleted" instead of removing it outright.
# ------------------------------------------------------------
class EditMessageSchema(BaseModel):
    username: str
    new_text: str

class DeleteMessageSchema(BaseModel):
    username: str


@app.post("/community/messages/{message_id}/edit")
async def community_edit_message(message_id: int, payload: EditMessageSchema, db=Depends(get_db)):
    msg = db.query(CommunityMessage).filter(CommunityMessage.id == message_id).first()
    if not msg:
        raise HTTPException(status_code=404, detail="Message not found")
    if msg.sender_username != payload.username:
        raise HTTPException(status_code=403, detail="You can only edit your own messages")
    if msg.is_deleted:
        raise HTTPException(status_code=400, detail="Can't edit a deleted message")
    if msg.message_type != "text":
        raise HTTPException(status_code=400, detail="Only text messages can be edited")

    msg.message_text = payload.new_text
    msg.is_edited = True
    msg.edited_at = now_str()
    db.commit()

    out = {"type": "edit", "id": msg.id, "message_text": msg.message_text, "edited_at": msg.edited_at}
    await broadcast_to_conversation(msg.conversation_id, out)
    return {"success": True, "message": _serialize_message(msg)}


@app.post("/community/messages/{message_id}/delete")
async def community_delete_message(message_id: int, payload: DeleteMessageSchema, db=Depends(get_db)):
    msg = db.query(CommunityMessage).filter(CommunityMessage.id == message_id).first()
    if not msg:
        raise HTTPException(status_code=404, detail="Message not found")
    if msg.sender_username != payload.username:
        raise HTTPException(status_code=403, detail="You can only delete your own messages")

    msg.is_deleted = True
    db.commit()

    out = {"type": "delete", "id": msg.id}
    await broadcast_to_conversation(msg.conversation_id, out)
    return {"success": True}


# ------------------------------------------------------------
# PRESENCE (online / last seen)
# ------------------------------------------------------------
@app.post("/community/presence/ping")
def community_presence_ping(payload: PresencePingSchema, db=Depends(get_db)):
    """Call this every ~30s from the frontend while the app is open to keep a student marked online."""
    row = db.query(UserPresence).filter(UserPresence.username == payload.username).first()
    if row:
        row.is_online = True
        row.last_seen = now_str()
    else:
        db.add(UserPresence(username=payload.username, is_online=True, last_seen=now_str()))
    db.commit()
    return {"success": True}


@app.get("/community/presence/{username}")
def community_presence_get(username: str, db=Depends(get_db)):
    row = db.query(UserPresence).filter(UserPresence.username == username).first()
    if not row:
        return {"username": username, "is_online": False, "last_seen": None}

    # Treat as offline if the last ping was more than 90 seconds ago
    is_online = row.is_online
    if row.last_seen:
        try:
            last_dt = datetime.fromisoformat(row.last_seen.replace("Z", "+00:00"))
            if (datetime.now(timezone.utc) - last_dt).total_seconds() > 90:
                is_online = False
        except Exception:
            pass

    return {"username": username, "is_online": is_online, "last_seen": row.last_seen}


# ------------------------------------------------------------
# GENERIC FILE UPLOAD (documents, PDFs, etc - not just images)
# Reuses the same Cloudinary config as /upload/image, but with
# resource_type="auto" so Cloudinary accepts any file type.
# ------------------------------------------------------------
@app.post("/upload/file")
async def upload_file(file: UploadFile = File(...)):
    if not CLOUDINARY_SDK_AVAILABLE:
        raise HTTPException(status_code=500, detail="File upload is not available - the cloudinary package is not installed on the server")
    if not CLOUDINARY_CONFIGURED:
        raise HTTPException(status_code=500, detail="File upload is not configured - CLOUDINARY_URL is missing")

    try:
        result = cloudinary.uploader.upload(file.file, resource_type="auto")
        return {
            "success": True,
            "url": result.get("secure_url"),
            "file_name": file.filename,
            "public_id": result.get("public_id"),
        }
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"File upload failed: {str(e)}")


# ------------------------------------------------------------
# ADMIN: create official department groups in bulk
# ------------------------------------------------------------
class OfficialGroupCreateSchema(BaseModel):
    name: str
    description: Optional[str] = None
    department: Optional[str] = None   # null = open campus-wide group

@app.post("/admin/community/create-official-group")
def admin_create_official_group(payload: OfficialGroupCreateSchema, db=Depends(get_db), admin_ok: bool = Depends(verify_admin)):
    group = CommunityGroup(
        name=payload.name, description=payload.description, department=payload.department,
        created_by="admin", is_official=True, created_at=now_str(),
    )
    db.add(group); db.commit(); db.refresh(group)
    return {"success": True, "group_id": group.id}
