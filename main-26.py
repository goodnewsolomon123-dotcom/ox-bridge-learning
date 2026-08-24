import os
import json
import random
import hashlib
import hmac
import math
import base64
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


def _vision_via_gemini(prompt: str, image_b64: str, content_type: str):
    """Returns the response text, or None if Gemini fails (to allow fallback)."""
    if not GEMINI_KEY:
        return None
    try:
        res = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_KEY}",
            json={
                "contents": [{
                    "parts": [
                        {"text": prompt},
                        {"inline_data": {"mime_type": content_type, "data": image_b64}}
                    ]
                }]
            },
            timeout=30
        )
        if res.status_code == 200:
            print("[AI VISION] Gemini [OK]")
            return res.json()["candidates"][0]["content"]["parts"][0]["text"]
        print(f"[AI VISION] Gemini failed: HTTP {res.status_code}: {res.text[:150]}")
        return None
    except Exception as e:
        print(f"[AI VISION] Gemini error: {e}")
        return None


def _vision_via_openrouter(prompt: str, image_url: str):
    """
    Fallback if Gemini is down or its quota is exhausted. Uses OpenRouter's
    own free auto-router ("openrouter/free") rather than a hardcoded model
    name - OpenRouter's free model catalog changes often (models get
    delisted with no notice, same issue that broke the Groq integration),
    so letting their router pick a currently-live vision-capable model is
    more durable than pinning one ourselves.
    """
    if not OR_KEY:
        return None
    try:
        res = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {OR_KEY}", "Content-Type": "application/json"},
            json={
                "model": "openrouter/free",
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": image_url}}
                    ]
                }]
            },
            timeout=30
        )
        if res.status_code == 200:
            print("[AI VISION] OpenRouter fallback [OK]")
            return res.json()["choices"][0]["message"]["content"]
        print(f"[AI VISION] OpenRouter fallback failed: HTTP {res.status_code}: {res.text[:150]}")
        return None
    except Exception as e:
        print(f"[AI VISION] OpenRouter fallback error: {e}")
        return None


def get_ai_vision_response(prompt: str, image_url: str) -> str:
    """
    Analyzes an image (or PDF) alongside a text prompt. Tries Gemini
    first (best quality, generous free tier), then falls back to
    OpenRouter's free vision router if Gemini is unavailable. Other
    doc types (docx, pptx) aren't supported here, only images and PDFs.
    """
    try:
        img_res = requests.get(image_url, timeout=15)
        if img_res.status_code != 200:
            return "I couldn't load that image to analyze it. Please try uploading it again."
        image_b64 = base64.b64encode(img_res.content).decode("utf-8")
        content_type = img_res.headers.get("Content-Type", "image/jpeg")
    except Exception as e:
        print(f"[AI VISION ERROR] {e}")
        return "Something went wrong while analyzing that image."

    result = _vision_via_gemini(prompt, image_b64, content_type)
    if result:
        return result

    result = _vision_via_openrouter(prompt, image_url)
    if result:
        return result

    return "I couldn't analyze that image right now. Please try again in a moment."


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
                json={"model": "openai/gpt-oss-120b", "messages": [{"role": "user", "content": prompt}]},
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

class AIVisionChatSchema(BaseModel):
    username: str
    message: str
    image_url: str
    subject: Optional[str] = "General"
    level: Optional[str] = "SSS"


@app.post("/ai/chat/vision")
def ai_chat_vision(data: AIVisionChatSchema, db=Depends(get_db)):
    """
    Lets a student send an image or PDF (e.g. a photo of an assignment,
    a diagram, a scanned page) along with a question, and get a real
    answer about what's in it - not just a text-only response.
    """
    user = db.query(User).filter(User.username == data.username).first()
    if not user: raise HTTPException(404, "User not found")

    prompt = f"""You are Ox-Bridge AI Tutor - a friendly, knowledgeable tutor for Nigerian students
    (Level: {data.level}, Subject: {data.subject}).
    The student has shared an image or document and asked a question about it.
    Look at it carefully and give a clear, helpful, encouraging explanation.
    Explain things simply and use Nigerian examples where relevant.

    Student's question about the image: {data.message}"""

    response = get_ai_vision_response(prompt, data.image_url)

    db.add(AIChatHistory(user_id=user.id, role="user",      message=f"[Image] {data.message}", created_at=now_str()))
    db.add(AIChatHistory(user_id=user.id, role="assistant", message=response,                   created_at=now_str()))
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
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         