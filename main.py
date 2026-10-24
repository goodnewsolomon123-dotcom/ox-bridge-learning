import os
import re
import json
import random
import hashlib
import hmac
import math
import base64
import requests
import httpx
import smtplib
from email.mime.text import MIMEText
try:
    from pywebpush import webpush, WebPushException
    PYWEBPUSH_AVAILABLE = True
except ImportError:
    PYWEBPUSH_AVAILABLE = False
    print("[PUSH] pywebpush not installed yet - push notification endpoints will return an error until 'pip install pywebpush' is added to requirements.txt")
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
DATABASE_URL       = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL is not set. Add it as an environment variable in "
        "Render before deploying - it must no longer be hardcoded in this "
        "file. (If you're reading this error, that's actually good news: "
        "it means the old hardcoded database password has been removed.)"
    )
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

# Push notification reminders (Web Push / VAPID) - see push-notifications-spec.md.
# These are blank until Eno generates a VAPID keypair and sets them on Render;
# the push endpoints below check for this and return a clear error instead of
# crashing if they're missing.
VAPID_PUBLIC_KEY    = os.getenv("VAPID_PUBLIC_KEY")
VAPID_PRIVATE_KEY   = os.getenv("VAPID_PRIVATE_KEY")
VAPID_CLAIMS_EMAIL  = os.getenv("VAPID_CLAIMS_EMAIL", "support@oxbridge.ng")
CRON_SECRET_KEY     = os.getenv("CRON_SECRET_KEY")

# Password reset emails - sent via a free Gmail account, not a paid service.
# GMAIL_APP_PASSWORD is NOT your normal Gmail password - it's a separate
# 16-character "App Password" you generate once in your Google Account
# security settings, specifically for apps like this to send through.
GMAIL_ADDRESS      = os.getenv("GMAIL_ADDRESS")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")

def send_email(to_email: str, subject: str, body: str) -> bool:
    """Sends a plain-text email via Gmail's SMTP server. Returns True/False instead of raising, so a failed send never crashes the request that triggered it."""
    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
        print("[EMAIL] Not configured - GMAIL_ADDRESS/GMAIL_APP_PASSWORD missing")
        return False
    try:
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = GMAIL_ADDRESS
        msg["To"] = to_email
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_ADDRESS, [to_email], msg.as_string())
        return True
    except Exception as e:
        print(f"[EMAIL ERROR] {e}")
        return False
MAX_FREE_VISION_PER_DAY = 2
TOKEN_EXPIRE_HOURS = 24 * 30  # 30 days - was 72 hours (3 days), which was logging people out too often

# ------------------------------------------------------------
# Master switch for the new free-tier limits (AI Tutor 2/day, Theory
# Lesson once, WAEC/NECO Theory once, Past Questions once). All of that
# gating logic is fully built and ready, but stays OFF until this is
# flipped to True - there's no upgrade UI live yet, so blocking access
# right now would just strand free users with no way to pay.
#
# NOT affected by this flag (these stay active either way):
#   - the quiz coin-farming fix (that's a bug fix, not a paywall)
#   - AI-generated theory/WAEC-theory caching (pure cost saving, no
#     effect on what any user is allowed to do)
# ------------------------------------------------------------
ENFORCE_FREE_TIER_LIMITS = False

# Shared pool of motivational/education quotes - used both for variety in
# daily push reminders below, and available via GET /quotes for the
# frontend's login popup if it ever wants server-driven quotes instead of
# (or alongside) its own local list.
MOTIVATIONAL_QUOTES = [
    "Success is the sum of small efforts repeated day in and day out.",
    "The expert in anything was once a beginner.",
    "Don't watch the clock; do what it does - keep going.",
    "Discipline is choosing between what you want now and what you want most.",
    "Small daily improvements lead to stunning results over time.",
    "You don't have to be great to start, but you have to start to be great.",
    "Every exam you pass today is a door you open for tomorrow.",
    "The pain of studying is temporary; the regret of not trying lasts longer.",
]

# ------------------------------------------------------------
# Shared identity for the AI Tutor (used by /ai/chat and /ai/chat/vision -
# the same endpoint powers both the Ox-Bridge Learning Hub and AKSU Smart
# Hub/Ox-Bridge Campus frontends). Without this, the underlying model
# provider can bleed through its own training and misidentify itself
# (e.g. claiming to be built by OpenAI) when a student asks who it is -
# this makes the correct identity and basic platform knowledge explicit
# instead of leaving it to chance.
# ------------------------------------------------------------
TUTOR_IDENTITY_INSTRUCTION = """
You are the Ox-Bridge AI Tutor, built by Ox-Bridge Technology. If asked who
made you, who owns you, or which company or AI is behind you, say you were
built by Ox-Bridge Technology - never say OpenAI, Google, Anthropic, Meta,
or any other AI lab, and never name or discuss which underlying AI
providers or models power your responses behind the scenes; that is an
internal detail, not something to share with users.

You are part of the Ox-Bridge learning platform, which includes:
- Ox-Bridge Learning Hub: AI-powered lessons, theory breakdowns, practice
  quizzes, and past questions for Nigerian students from Nursery through
  university level, with focused exam prep for WAEC, NECO, and JAMB.
- AKSU Smart Hub (also called Ox-Bridge Campus): built specifically for
  Akwa Ibom State University students, covering GST (General Studies)
  courses, past questions, and mock tests.

If a student asks what this app is, what you can do, or how to use it,
explain simply and naturally: they can ask you to explain a topic, get
help with a WAEC/NECO/JAMB or course question, or just ask academic
questions in plain language - and the app itself also has dedicated
sections for theory lessons, practice quizzes, and past questions if they
want structured practice instead of chatting. Keep this brief and
conversational, not like reciting a feature list, unless they ask for
more detail.

This app is not on the Google Play Store or Apple App Store yet - it's a
web app that installs straight from the browser (a PWA), which still puts
a real icon on their home screen and opens full-screen like a normal app.
If a student asks how to install it, download it, or says they can't find
it on Play Store, walk them through it for their device:
- Android (Chrome): tap the three-dot menu in the top right, then tap
  "Install app" or "Add to Home screen".
- iPhone/iPad (Safari): tap the Share icon (the square with an arrow
  pointing up), scroll down, then tap "Add to Home Screen".
- Desktop (Chrome/Edge): look for an install icon (a small screen with a
  down arrow) at the right side of the address bar, and click it.
After installing, it opens from their home screen like any other app, and
still works the same way. Only explain this when asked - do not bring it
up unprompted.
"""

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
    # These columns already exist in the live database (added via the
    # ALTER TABLE migrations below) but were never declared here, so any
    # direct Python attribute access (user.is_premium, etc.) crashed with
    # AttributeError - the existing Payment module worked around this by
    # using raw SQL instead. Declaring them here fixes that for real,
    # instead of adding another raw-SQL workaround on top.
    is_premium               = Column(Boolean, default=False)
    premium_expires          = Column(String,  nullable=True)
    premium_plan             = Column(String,  nullable=True)
    ai_requests_today        = Column(Integer, default=0)
    ai_requests_date         = Column(String,  nullable=True)
    vision_requests_today    = Column(Integer, default=0)
    vision_requests_date     = Column(String,  nullable=True)
    used_free_theory_lesson  = Column(Boolean, default=False)
    used_free_waec_theory    = Column(Boolean, default=False)
    used_free_past_questions = Column(Boolean, default=False)

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
    cached_explanation = Column(String, nullable=True)  # AI-generated lesson, cached after the first generation

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

class PasswordResetToken(Base):
    __tablename__ = "password_reset_tokens"
    id         = Column(Integer, primary_key=True, index=True)
    username   = Column(String, index=True)
    code       = Column(String)
    created_at = Column(String)
    expires_at = Column(String)
    used       = Column(Boolean, default=False)

class PushSubscription(Base):
    __tablename__ = "push_subscriptions"
    id         = Column(Integer, primary_key=True, index=True)
    username   = Column(String, index=True)
    endpoint   = Column(String, unique=True)
    p256dh_key = Column(String)
    auth_key   = Column(String)
    created_at = Column(String, nullable=True)

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
    image_url      = Column(String, nullable=True)  # diagram/image for questions that need one (hosted URL or base64)

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
    cached_explanation = Column(String, nullable=True)  # AI-generated explanation, cached after the first generation

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
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS vision_requests_today INTEGER DEFAULT 0;",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS vision_requests_date VARCHAR;",
        # Free-tier "once for life" gates - same pattern as the existing
        # GST-mock-test-once-for-life check in the Campus module.
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS used_free_theory_lesson BOOLEAN DEFAULT FALSE;",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS used_free_waec_theory BOOLEAN DEFAULT FALSE;",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS used_free_past_questions BOOLEAN DEFAULT FALSE;",
        # Caches so an AI-generated explanation is produced once total and
        # served to every student who opens that same topic/question after -
        # not regenerated (and re-billed to the AI provider) per viewer.
        "ALTER TABLE topics ADD COLUMN IF NOT EXISTS cached_explanation VARCHAR;",
        "ALTER TABLE waec_theory_questions ADD COLUMN IF NOT EXISTS cached_explanation VARCHAR;",
        # Objective past questions can now carry an image (diagrams, graphs, figures).
        "ALTER TABLE past_questions ADD COLUMN IF NOT EXISTS image_url VARCHAR;",
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

def is_premium_active(is_premium: bool, premium_expires: Optional[str]) -> bool:
    """
    A user can have is_premium=True in the database but still be expired -
    this is the single source of truth for 'is premium right now', so every
    paywall check (theory lessons, WAEC theory, past questions, AI chat)
    agrees with what /premium/status already reports, instead of each
    endpoint re-deriving it slightly differently.
    """
    if not is_premium:
        return False
    if not premium_expires:
        return True  # no expiry set - treat as active (matches existing lenient behavior elsewhere)
    try:
        expiry_date = datetime.strptime(premium_expires, "%Y-%m-%d")
        return (expiry_date - datetime.now()).days >= 0
    except Exception:
        return True  # unparsable expiry - fail open rather than lock out a paying user over a data glitch

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


def fetch_search_context(query: str):
    """
    Runs a Tavily search and returns (context_text, sources).
    context_text is compact text fed into the AI's prompt as before.
    sources is a list of {"title":..., "url":...} dicts pulled from the
    same results, kept separate so the caller can send real clickable
    links back to the frontend (Grok-style) instead of only a paraphrased
    summary the user has no way to verify.
    """
    if not TAVILY_KEY:
        return "", []
    try:
        res = requests.post("https://api.tavily.com/search", json={
            "api_key": TAVILY_KEY, "query": query + " Nigeria university",
            "search_depth": "basic", "max_results": 4,
            "include_answer": True
        }, timeout=10)
        if res.status_code != 200:
            return "", []
        data = res.json()
        parts = []
        sources = []
        if data.get("answer"):
            parts.append(f"Quick answer: {data['answer']}")
        for r in data.get("results", [])[:4]:
            title = r.get("title", "")
            url = r.get("url", "")
            content = (r.get("content", "") or "")[:200]
            parts.append(f"- {title}: {content}")
            if url:
                sources.append({"title": title or url, "url": url})
        return "\n".join(parts), sources
    except Exception as e:
        print(f"[TAVILY ERROR] {e}")
        return "", []


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
        return dedupe_repeated_response(result)

    result = _vision_via_openrouter(prompt, image_url)
    if result:
        return dedupe_repeated_response(result)

    return "I couldn't analyze that image right now. Please try again in a moment."


def _try_groq(prompt: str, errors: list) -> Optional[str]:
    if not GROQ_KEY: return None
    try:
        res = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json"},
            json={"model": "openai/gpt-oss-120b", "messages": [{"role": "user", "content": prompt}]},
            timeout=15
        )
        if res.status_code == 200:
            print("[AI] Groq [OK]")
            return res.json()['choices'][0]['message']['content']
        err = f"Groq HTTP {res.status_code}: {res.text[:100]}"
        errors.append(err); print(f"[AI] {err}")
    except Exception as e:
        errors.append(f"Groq: {str(e)}")
    return None


def _try_deepseek(prompt: str, errors: list) -> Optional[str]:
    if not DEEPSEEK_KEY: return None
    try:
        res = requests.post(
            "https://api.deepseek.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {DEEPSEEK_KEY}", "Content-Type": "application/json"},
            json={"model": "deepseek-v4-flash", "messages": [{"role": "user", "content": prompt}]},
            timeout=15
        )
        if res.status_code == 200:
            print("[AI] DeepSeek [OK]")
            return res.json()['choices'][0]['message']['content']
        err = f"DeepSeek HTTP {res.status_code}: {res.text[:100]}"
        errors.append(err); print(f"[AI] {err}")
    except Exception as e:
        errors.append(f"DeepSeek: {str(e)}")
    return None


def _try_mistral(prompt: str, errors: list) -> Optional[str]:
    if not MISTRAL_KEY: return None
    try:
        res = requests.post(
            "https://api.mistral.ai/v1/chat/completions",
            headers={"Authorization": f"Bearer {MISTRAL_KEY}", "Content-Type": "application/json"},
            json={"model": "open-mistral-nemo", "messages": [{"role": "user", "content": prompt}]},
            timeout=15
        )
        if res.status_code == 200:
            print("[AI] Mistral [OK]")
            return res.json()['choices'][0]['message']['content']
        err = f"Mistral HTTP {res.status_code}: {res.text[:100]}"
        errors.append(err); print(f"[AI] {err}")
    except Exception as e:
        errors.append(f"Mistral: {str(e)}")
    return None


def _try_cohere(prompt: str, errors: list) -> Optional[str]:
    if not COHERE_KEY: return None
    try:
        res = requests.post(
            "https://api.cohere.ai/v1/chat",
            headers={"Authorization": f"Bearer {COHERE_KEY}", "Content-Type": "application/json"},
            json={"model": "command-a-03-2025", "message": prompt},
            timeout=15
        )
        if res.status_code == 200:
            print("[AI] Cohere [OK]")
            return res.json()['text']
        err = f"Cohere HTTP {res.status_code}: {res.text[:100]}"
        errors.append(err); print(f"[AI] {err}")
    except Exception as e:
        errors.append(f"Cohere: {str(e)}")
    return None


def _try_gemini(prompt: str, errors: list) -> Optional[str]:
    if not GEMINI_KEY: return None
    try:
        res = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_KEY}",
            json={"contents": [{"parts": [{"text": prompt}]}]},
            timeout=15
        )
        if res.status_code == 200:
            print("[AI] Gemini [OK]")
            return res.json()["candidates"][0]["content"]["parts"][0]["text"]
        err = f"Gemini HTTP {res.status_code}: {res.text[:100]}"
        errors.append(err); print(f"[AI] {err}")
    except Exception as e:
        errors.append(f"Gemini: {str(e)}")
    return None


def _try_openrouter(prompt: str, errors: list) -> Optional[str]:
    if not OR_KEY: return None
    # "openrouter/free" is OpenRouter's own auto-router - it always picks
    # whatever free model is currently live, which is more durable than
    # pinning specific model IDs (the free lineup rotates constantly;
    # deepseek/deepseek-v4-flash:free and google/gemini-2.5-flash both
    # stopped being free partway through 2026, which is why they were
    # silently failing here). The named models below are just extra
    # backup layers, verified free as of August 2026.
    free_models = ["openrouter/free", "meta-llama/llama-4-maverick:free", "meta-llama/llama-3.3-70b-instruct:free", "qwen/qwen3-coder:free", "google/gemma-3-12b-it:free", "openai/gpt-oss-20b:free"]
    for model in free_models:
        try:
            res = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {OR_KEY}", "Content-Type": "application/json"},
                json={"model": model, "messages": [{"role": "user", "content": prompt}]},
                timeout=15
            )
            if res.status_code == 200:
                print(f"[AI] OpenRouter ({model}) [OK]")
                return res.json()['choices'][0]['message']['content']
            errors.append(f"OpenRouter {model}: HTTP {res.status_code}")
        except Exception as e:
            errors.append(f"OpenRouter {model}: {str(e)}")
    return None


def _try_huggingface(prompt: str, errors: list) -> Optional[str]:
    # FIXED: old api-inference.huggingface.co endpoint was fully retired
    # (HTTP 410 Gone) - replaced by router.huggingface.co, which uses
    # OpenAI-style chat completions instead of the old raw text-gen format.
    if not HF_KEY: return None
    models = ["meta-llama/Llama-3.1-8B-Instruct:novita", "openai/gpt-oss-120b", "Qwen/Qwen2.5-7B-Instruct-1M", "meta-llama/Llama-3.3-70B-Instruct"]
    for model in models:
        try:
            res = requests.post(
                "https://router.huggingface.co/v1/chat/completions",
                headers={"Authorization": f"Bearer {HF_KEY}", "Content-Type": "application/json"},
                json={"model": model, "messages": [{"role": "user", "content": prompt}], "max_tokens": 500},
                timeout=15
            )
            if res.status_code == 200:
                text = res.json()["choices"][0]["message"]["content"].strip()
                if text:
                    print(f"[AI] HuggingFace ({model}) [OK]")
                    return text
            else:
                errors.append(f"HuggingFace {model}: HTTP {res.status_code}")
        except Exception as e:
            errors.append(f"HuggingFace: {str(e)}")
    return None


def dedupe_repeated_response(text: str) -> str:
    """
    Some smaller/weaker fallback AI models occasionally fall into a
    repetition loop and output the exact same block of text twice in a
    row. This detects that specific pattern - the whole response split in
    half being identical - and keeps only the first copy. Doesn't touch
    normal responses at all, only exact whole-response duplication.
    """
    stripped = text.strip()
    length = len(stripped)
    if length < 40:
        return text
    midpoint = length // 2
    first_half = stripped[:midpoint].strip()
    second_half = stripped[midpoint:].strip()
    # allow a small tolerance for an odd-length split landing mid-character
    if first_half and (first_half == second_half or first_half in second_half[:len(first_half)+5]):
        return first_half
    return text


def get_ai_response(prompt: str) -> str:
    cached = get_cached(prompt)
    if cached:
        print(f"[AI] Cache hit for: {prompt[:50]}...")
        return cached

    errors = []

    # NOTE: previously this reordered providers (Gemini first) for
    # math-heavy prompts, on the theory that Gemini/Groq/DeepSeek follow
    # the $ notation instruction more reliably. Reverted after real-world
    # testing showed math formatting actually got WORSE with Gemini
    # prioritized first, not better - so back to the original fixed order,
    # which was reportedly working better. The math-formatting reinforcement
    # in the prompts themselves (not provider order) is the part that helped.
    provider_order = [_try_groq, _try_deepseek, _try_mistral, _try_cohere, _try_gemini, _try_openrouter, _try_huggingface]

    result = None
    for provider_fn in provider_order:
        result = provider_fn(prompt, errors)
        if result:
            result = dedupe_repeated_response(result)
            break

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

def verify_cron(x_cron_key: str = Header(None)):
    """
    Guards /push/send-reminders the same way verify_admin guards /admin/* -
    requires an X-Cron-Key header matching CRON_SECRET_KEY, so the daily
    scheduled job (Render Cron or an external cron service) can call it,
    but a random visitor hitting the URL can't spam every user with pushes.
    """
    if not CRON_SECRET_KEY:
        raise HTTPException(status_code=500, detail="Cron access is not configured on the server")
    if not x_cron_key or x_cron_key != CRON_SECRET_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing cron key")
    return True

# ============================================================
# LIGHTWEIGHT ACTIVITY LOGGER (for admin monitoring)
# Logs a small set of meaningful actions. Fails silently so it
# can never break an existing request.
# ============================================================
LOGGED_PATH_PREFIXES = {
    "/login": "login", "/signup": "signup", "/ai/chat": "ai_chat",
    "/quiz/save-result": "quiz_submit", "/theory/": "theory_view",
    "/games/save-score": "game_played", "/games/finish": "game_played", "/search/web/": "web_search",
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

class ForgotPasswordSchema(BaseModel):
    username: str

class ResetPasswordSchema(BaseModel):
    username:     str
    code:         str
    new_password: str

class PushSubscriptionKeys(BaseModel):
    p256dh: str
    auth:   str

class PushSubscribeSchema(BaseModel):
    username:     str
    endpoint:     str
    keys:         PushSubscriptionKeys

class PushUnsubscribeSchema(BaseModel):
    username: str

class PushTestSchema(BaseModel):
    username: str

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
    image_url:      Optional[str] = None   # optional diagram/image (hosted URL or base64)

class PastQuestionImageAttach(BaseModel):
    image_url: Optional[str] = None   # send null/empty to remove the image

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
    top = db.query(User).order_by(User.progress_score.desc()).limit(20).all()
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

    # Free tier: 2 AI Tutor messages/day. Reuses the same daily-reset
    # columns/limit already built for the Campus app's /ai/check-limit -
    # enforced directly here so it actually takes effect without needing
    # any frontend changes to call a separate check endpoint first.
    # Usage is tracked either way; the block itself only fires once
    # ENFORCE_FREE_TIER_LIMITS is turned on (see top of file).
    premium_ok = is_premium_active(user.is_premium, user.premium_expires)
    if not premium_ok:
        today = today_str()
        if user.ai_requests_date != today:
            user.ai_requests_today = 0
            user.ai_requests_date = today
        if ENFORCE_FREE_TIER_LIMITS and (user.ai_requests_today or 0) >= 2:
            db.commit()  # persist the reset-to-0 above even though this particular request is blocked
            raise HTTPException(status_code=402, detail="You've used your 2 free AI Tutor messages for today. Upgrade for unlimited access, or come back tomorrow.")

    history = db.query(AIChatHistory).filter(
        AIChatHistory.user_id == user.id
    ).order_by(AIChatHistory.id.desc()).limit(6).all()
    history.reverse()

    context = ""
    for h in history:
        role     = "Student" if h.role == "user" else "Tutor"
        context += f"{role}: {h.message}\n"

    search_context, search_sources = fetch_search_context(data.message) if needs_web_search(data.message) else ("", [])
    search_block = f"""
    Current web search results (this is real, up to date information - use it
    to answer anything about recent events, dates, deadlines, or current facts.
    Don't mention that you searched, just answer naturally as if you knew it):
    {search_context}
    """ if search_context else ""

    datetime_block = f"The current date and time (from the student's own device) is: {data.client_datetime}." if data.client_datetime else ""
    subject_block = SUBJECT_INSTRUCTIONS.get(subject_lane_for_course(data.subject), "")

    prompt = f"""{TUTOR_IDENTITY_INSTRUCTION}
    You are tutoring a Nigerian student (Level: {data.level}, Subject: {data.subject}).
    Explain things simply, use Nigerian examples, and encourage students.
    Whenever you write ANY mathematical expression, chemical formula, chemical
    equation, or physics equation - always wrap it in single dollar signs for
    inline use, e.g. $x^2 + y^2 = z^2$, or double dollar signs $$...$$ for a
    standalone equation on its own line. This applies to every subject, not
    just Mathematics:
    - Chemistry formulas and balanced equations: $2H_2 + O_2 \rightarrow 2H_2O$,
      $Na^+ + Cl^- \rightarrow NaCl$, $CaCO_3 \rightarrow CaO + CO_2$
    - Physics equations: $F = ma$, $E = mc^2$, $v = u + at$, $\frac{1}{2}mv^2$
    - Any subscript or superscript (like H2O or x squared) must use this
      notation: write $H_2O$ not H2O, write $x^2$ not x^2 or x**2.
    Never write any of this in plain text, with ASCII symbols like ^ or _, or by
    spelling out the equation in words - always use the $ notation instead, for
    every single formula or equation, in every subject.
    Do this silently - never mention the word "LaTeX", never explain that you're
    using a notation or formatting system, never say things like "I'll write this
    in LaTeX" or "using LaTeX format". Just write the formula wrapped in $ signs
    as if that's simply how you naturally write math, chemistry, and physics.
    {subject_block}
    {datetime_block}
    {search_block}
    Previous conversation:
    {context}

    Student: {data.message}

    Reminder: wrap every math, chemistry, or physics expression in $ signs.

    Tutor:"""

    response = get_ai_response(prompt)

    if not premium_ok:
        user.ai_requests_today = (user.ai_requests_today or 0) + 1

    db.add(AIChatHistory(user_id=user.id, role="user",      message=data.message, created_at=now_str()))
    db.add(AIChatHistory(user_id=user.id, role="assistant", message=response,      created_at=now_str()))
    db.commit()

    return {"response": response, "username": data.username, "timestamp": now_str(), "sources": search_sources}

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

    prompt = f"""{TUTOR_IDENTITY_INSTRUCTION}
    You are tutoring a Nigerian student (Level: {data.level}, Subject: {data.subject}).
    The student has shared an image or document and asked a question about it.
    Look at it carefully and give a clear, helpful, encouraging explanation.
    Explain things simply and use Nigerian examples where relevant.
    Whenever you write ANY mathematical expression, chemical formula, chemical
    equation, or physics equation, always wrap it in $...$ for inline math or
    $$...$$ for a standalone equation - this applies to every subject, not just
    Mathematics. For example: chemistry equations like $2H_2 + O_2 \rightarrow 2H_2O$,
    physics equations like $F = ma$ or $E = mc^2$, and any subscript/superscript
    like $H_2O$ or $x^2$ - never write these in plain text or with ASCII symbols
    like ^ or _ instead. Do this silently, without ever mentioning the word
    "LaTeX" or explaining the notation.

    Student's question about the image: {data.message}

    Reminder: wrap every math, chemistry, or physics expression in $ signs."""

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

    # Coin-farming fix: a coin payout only happens the FIRST time this exact
    # subject+level+topic quiz is completed. Retaking the same quiz for
    # practice still earns XP (a "bonus"), but doesn't pay out coins again
    # every time - previously this paid full coins on every single retake.
    is_first_attempt = not db.query(QuizResult).filter(
        QuizResult.user_id == user.id,
        QuizResult.subject == data.subject,
        QuizResult.level == data.level,
        QuizResult.topic == data.topic,
    ).first()

    db.add(QuizResult(user_id=user.id, subject=data.subject, level=data.level,
        topic=data.topic, score=data.score, total_questions=data.total_questions, date_taken=now_str()))
    points              = data.score * 5
    coins_earned        = points if is_first_attempt else 0
    user.progress_score = (user.progress_score or 0) + points
    user.coins          = (user.coins          or 0) + coins_earned
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
    return {"msg": "Quiz result saved", "points_earned": points, "coins_earned": coins_earned,
            "is_first_attempt": is_first_attempt, "new_total": user.progress_score, "new_badges": newly or []}

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
    return {"msg": "Past question added", "id": q.id, "has_image": bool(q.image_url)}

@app.post("/admin/past-question/{question_id}/image")
def attach_past_question_image(question_id: int, data: PastQuestionImageAttach, db=Depends(get_db), admin_ok: bool = Depends(verify_admin)):
    """Admin attaches (or removes) an image on an existing objective past question."""
    q = db.query(PastQuestion).filter(PastQuestion.id == question_id).first()
    if not q: raise HTTPException(404, "Past question not found")
    q.image_url = data.image_url or None
    db.commit()
    return {"msg": "Past question image updated", "id": q.id, "has_image": bool(q.image_url)}

@app.get("/past-questions/{exam_type}/{subject}")
def get_past_questions(exam_type: str, subject: str, year: int = None, username: Optional[str] = None, db=Depends(get_db)):
    """Free tier: 1 past-questions practice session total, for life -
    same 'once for life' pattern as the Campus GST mock test. No AI
    caching needed here since this content is static, DB-sourced."""
    user = None
    if username:
        user = db.query(User).filter(User.username == username).first()
        if ENFORCE_FREE_TIER_LIMITS and user and not is_premium_active(user.is_premium, user.premium_expires) and user.used_free_past_questions:
            raise HTTPException(
                status_code=402,
                detail="You've used your free past questions practice. Upgrade to unlock unlimited practice.",
            )

    query = db.query(PastQuestion).filter(
        PastQuestion.exam_type.ilike(f"%{exam_type}%"),
        PastQuestion.subject.ilike(f"%{subject}%")
    )
    if year: query = query.filter(PastQuestion.year == year)
    results = query.limit(20).all()

    if results and user and not is_premium_active(user.is_premium, user.premium_expires):
        user.used_free_past_questions = True
        db.commit()

    return [{"id": q.id, "year": q.year, "question_text": q.question_text,
             "option_a": q.option_a, "option_b": q.option_b,
             "option_c": q.option_c, "option_d": q.option_d,
             "correct_answer": q.correct_answer, "explanation": q.explanation,
             "image_url": q.image_url, "has_image": bool(q.image_url)}
            for q in results]

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
        "correct_answer": q.correct_answer, "explanation": q.explanation,
        "image_url": q.image_url, "has_image": bool(q.image_url)}}

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
def save_game_score(data: GameScoreSave, authorization: Optional[str] = Header(None), db=Depends(get_db)):
    # Temporary guard for games not yet moved to /games/start + /games/finish.
    # 1) caller must be logged in AS the username they are saving for
    token_user = None
    if authorization and authorization.lower().startswith("bearer "):
        token_user = decode_token(authorization[7:].strip())
    if not token_user or token_user != data.username:
        raise HTTPException(401, "Please login again.")
    # 2) sane score range
    if data.score < 0 or data.score > 600:
        raise HTTPException(400, "Invalid score")
    user = db.query(User).filter(User.username == data.username).first()
    if not user: raise HTTPException(404, "User not found")
    # 3) a real round takes longer than a few seconds
    last = db.query(GameScore).filter(GameScore.user_id == user.id).order_by(GameScore.id.desc()).first()
    if last and last.played_at:
        try:
            gap = (datetime.now(timezone.utc) - datetime.fromisoformat(last.played_at)).total_seconds()
            if gap < 8: raise HTTPException(429, "Slow down a little!")
        except HTTPException: raise
        except Exception: pass
    db.add(GameScore(user_id=user.id, game_type=data.game_type, score=data.score, played_at=now_str()))
    coins_earned = max(1, data.score // 10)
    user.coins   = (user.coins or 0) + coins_earned
    db.commit()
    return {"msg": "Score saved", "coins_earned": coins_earned, "total_coins": user.coins}

# ============================================================
# FUN & BRAIN CHALLENGE - server-controlled games
# Tables (Neon): game_items (question bank) + game_sessions (one row per round)
# Games here: trivia, science, word, sentence, puzzle.  Math, Number
# Patterns, Memory Match and Flashcards stay in the frontend.
# Questions come from: your admin panel, or the AI endpoint (auto top-up).
#
# "puzzle" (Word Puzzles) covers 6 sub-types - spelling, odd-one-out,
# synonym, antonym, cipher code, anagram - but every one of them renders
# as the same multiple-choice shape trivia already uses, just tagged with
# a "type" field for variety and admin filtering. Payload shape:
# {"type": "spell"|"odd"|"syn"|"ant"|"code"|"anagram", "q": "...",
#  "a": "correct answer", "w": ["wrong1","wrong2","wrong3"], "why": "..."}
# ============================================================
import time as _time
import uuid as _uuid
import threading as _threading
from fastapi import BackgroundTasks
from sqlalchemy import Text, UniqueConstraint, func as sa_func
from sqlalchemy.exc import IntegrityError

GAME_LEVELS = ("Junior", "Senior")

GAME_CFG = {
    "trivia":   {"n": 10, "tiers": [0, 0, 0, 1, 1, 1, 1, 2, 2, 2], "secs": [15, 18, 20], "save": "trivia",
                 "tries": 1, "base": 10, "tier_bonus": 5, "speed_bonus": 5, "streak_bonus": 5},
    "science":  {"n": 10, "tiers": [0, 0, 0, 1, 1, 1, 1, 2, 2, 2], "secs": [14, 16, 18], "save": "science_sort",
                 "tries": 1, "base": 10, "tier_bonus": 5, "speed_bonus": 5, "streak_bonus": 5},
    "word":     {"n": 10, "tiers": [0, 0, 0, 1, 1, 1, 1, 2, 2, 2], "secs": [25, 28, 32], "save": "word_builder",
                 "tries": 3, "base": 10, "tier_bonus": 5, "speed_bonus": 5, "streak_bonus": 5},
    # Duolingo style: no timer, 3 hearts, wrong ones come back at the end, bonus for a perfect lesson
    "sentence": {"n": 10, "tiers": [0, 0, 0, 1, 1, 1, 1, 2, 2, 2], "secs": None, "save": "sentence_builder",
                 "tries": 1, "base": 10, "tier_bonus": 5, "speed_bonus": 0, "streak_bonus": 5,
                 "hearts": 3, "requeue": True, "perfect_bonus": 20},
    "puzzle":   {"n": 10, "tiers": [0, 0, 0, 1, 1, 1, 1, 2, 2, 2], "secs": [18, 22, 26], "save": "word_puzzle",
                 "tries": 1, "base": 10, "tier_bonus": 5, "speed_bonus": 5, "streak_bonus": 5},
}

GAME_PUZZLE_TYPES = ("spell", "odd", "syn", "ant", "code", "anagram")

GAME_DAILY_COIN_CAP = 500
GAME_POOL_TARGET    = 40      # keep at least this many active questions per tier (per game + level)
GAME_BATCH          = 15      # questions requested from the AI per call (5 easy, 5 medium, 5 hard)
GAME_SENT_DECOYS    = [["big", "went", "are", "very", "not", "our", "many", "also"],
                       ["whom", "had", "nor", "were", "although", "their", "would", "shall"]]
GAME_PROPER = re.compile(r"^(I|I'm|I'll|Nigeria|Nigerian|Monday|Saturday|Lagos|Abuja)(?![a-z])")


class GameItem(Base):
    __tablename__ = "game_items"
    __table_args__ = (UniqueConstraint("game", "level", "qhash", name="uq_game_items_qhash"),)
    id         = Column(Integer, primary_key=True, index=True)
    game       = Column(String, index=True)       # trivia | science | word | sentence
    level      = Column(String, index=True)       # Junior | Senior
    tier       = Column(Integer, default=0)       # 0 easy, 1 medium, 2 hard
    payload    = Column(Text)                     # JSON text, shape depends on the game
    qhash      = Column(String)                   # fingerprint of the question text (blocks duplicates)
    source     = Column(String, default="admin")  # admin | ai | seed
    active     = Column(Boolean, default=True)
    created_at = Column(String, nullable=True)


class GameSession(Base):
    __tablename__ = "game_sessions"
    id         = Column(String, primary_key=True)
    user_id    = Column(Integer, ForeignKey("users.id"), index=True)
    game       = Column(String)
    level      = Column(String)
    item_ids   = Column(Text)                     # JSON list = the question queue
    answers    = Column(Text, default="{}")       # JSON dict: position -> result
    state      = Column(Text, default="{}")       # JSON dict: tries, hearts, requeued, n0, over
    points     = Column(Integer, default=0)
    correct    = Column(Integer, default=0)
    streak     = Column(Integer, default=0)
    started_ts = Column(Float)
    last_ts    = Column(Float)
    finished   = Column(Boolean, default=False)
    coins      = Column(Integer, default=0)
    day        = Column(String, nullable=True)
    result     = Column(Text, nullable=True)


try:
    GameItem.__table__.create(bind=engine, checkfirst=True)
    GameSession.__table__.create(bind=engine, checkfirst=True)
    print("[GAMES] game_items + game_sessions ready [OK]")
except Exception as _e:
    print(f"[GAMES TABLE ERROR] {_e}")


# ---------- small helpers ----------
def _gm_norm(s):
    return re.sub(r"\s+", " ", str(s or "").strip()).lower()

def _gm_norm_sentence(s):
    return _gm_norm(re.sub(r"[.,;:!?]", "", str(s or "")))

def _gm_hash(game, p):
    game = _gm_kind(game)
    key = {"trivia": p.get("q"), "science": f"{p.get('topic', '')}|{p.get('item', '')}",
           "word": p.get("w"), "sentence": p.get("s"),
           "puzzle": f"{p.get('q')}|{p.get('a')}"}.get(game, "")
    return hashlib.md5(_gm_norm(key).encode("utf-8")).hexdigest()

def _gm_key_text(game, p):
    game = _gm_kind(game)
    return {"trivia": p.get("q"), "science": p.get("item"), "word": p.get("w"), "sentence": p.get("s"),
            "puzzle": p.get("q")}.get(game) or ""

def _gm_clip(v, n):
    return str(v or "").strip()[:n]


# ---------- validators: clean one item or return None ----------
def _gm_validate(game, p):
    game = _gm_kind(game)
    if not isinstance(p, dict):
        return None
    try:
        if game == "trivia":
            q, a = _gm_clip(p.get("q"), 240), _gm_clip(p.get("a"), 90)
            w = p.get("w")
            if not q or not a or not isinstance(w, list):
                return None
            wrongs = [_gm_clip(x, 90) for x in w if _gm_clip(x, 90)]
            wrongs = [x for x in dict.fromkeys(wrongs) if _gm_norm(x) != _gm_norm(a)]
            if len(wrongs) < 2:
                return None
            out = {"q": q, "a": a, "w": wrongs[:3]}
        elif game == "science":
            topic, item, a = _gm_clip(p.get("topic"), 60), _gm_clip(p.get("item"), 50), _gm_clip(p.get("a"), 50)
            cats = p.get("cats")
            if not topic or not item or not a or not isinstance(cats, list):
                return None
            cats = list(dict.fromkeys([_gm_clip(c, 40) for c in cats if _gm_clip(c, 40)]))
            if a not in cats:
                cats.append(a)
            if len(cats) < 2 or len(cats) > 5:
                return None
            out = {"topic": topic, "cats": cats, "item": item, "a": a}
        elif game == "word":
            w, hint = _gm_clip(p.get("w"), 20), _gm_clip(p.get("hint"), 140)
            if not re.fullmatch(r"[A-Za-z]{3,16}", w) or not hint:
                return None
            out = {"w": w, "hint": hint}
        elif game == "sentence":
            s = _gm_clip(p.get("s"), 160)
            words = s.rstrip(".?!").split()
            if len(words) < 3 or len(words) > 18 or s[-1:] not in ".?!":
                return None
            out = {"s": s}
            hint = _gm_clip(p.get("hint"), 160)
            if hint:
                out["hint"] = hint
            d = p.get("d")
            if isinstance(d, list):
                low = {re.sub(r"[,;:.?!]", "", x).lower() for x in words}
                dec = [x for x in dict.fromkeys(_gm_clip(x, 15).lower() for x in d)
                       if re.fullmatch(r"[a-z']{2,15}", x) and x not in low]
                if dec:
                    out["d"] = dec[:3]
            fobj = p.get("f")
            if isinstance(fobj, dict) and isinstance(fobj.get("o"), list):
                fw = _gm_clip(fobj.get("w"), 25)
                m_ = re.search(r"\b" + re.escape(fw) + r"\b", s, flags=re.I) if fw else None
                if m_:
                    fo = [x for x in dict.fromkeys(_gm_clip(x, 25) for x in fobj["o"]) if x and _gm_norm(x) != _gm_norm(fw)]
                    if len(fo) >= 2:
                        out["f"] = {"w": m_.group(0), "o": fo[:3]}
        elif game == "puzzle":
            ptype = _gm_clip(p.get("type"), 20).lower()
            q, a = _gm_clip(p.get("q"), 240), _gm_clip(p.get("a"), 90)
            w = p.get("w")
            if ptype not in GAME_PUZZLE_TYPES or not q or not a or not isinstance(w, list):
                return None
            wrongs = [_gm_clip(x, 90) for x in w if _gm_clip(x, 90)]
            wrongs = [x for x in dict.fromkeys(wrongs) if _gm_norm(x) != _gm_norm(a)]
            if len(wrongs) < 2:
                return None
            out = {"type": ptype, "q": q, "a": a, "w": wrongs[:3]}
        else:
            return None
        if p.get("why"):
            out["why"] = _gm_clip(p.get("why"), 240)
        return out
    except Exception:
        return None


# ---------- saving items (skips duplicates) ----------
def _gm_save_items(db, game, level, entries, source):
    """entries = [(tier, payload_dict)]. Returns (added, skipped)."""
    existing = {h for (h,) in db.query(GameItem.qhash).filter(GameItem.game == game, GameItem.level == level).all()}
    added = skipped = 0
    rows = []
    for tier, raw in entries:
        clean = _gm_validate(game, raw)
        if not clean or level not in GAME_LEVELS or tier not in (0, 1, 2):
            skipped += 1
            continue
        h = _gm_hash(game, clean)
        if h in existing:
            skipped += 1
            continue
        existing.add(h)
        rows.append(GameItem(game=game, level=level, tier=tier, payload=json.dumps(clean, ensure_ascii=False),
                             qhash=h, source=source, active=True, created_at=now_str()))
    try:
        db.add_all(rows)
        db.commit()
        added = len(rows)
    except IntegrityError:          # another worker saved the same question a moment ago
        db.rollback()
        for r in rows:
            try:
                db.add(GameItem(game=r.game, level=r.level, tier=r.tier, payload=r.payload, qhash=r.qhash,
                                source=r.source, active=True, created_at=r.created_at))
                db.commit()
                added += 1
            except IntegrityError:
                db.rollback()
                skipped += 1
    return added, skipped


# ---------- AI generation ----------
_GM_GEN_LOCK = _threading.Lock()
_GM_GEN_STATE = {}   # (game, level) -> {"running": bool, "next_ok": float}

def _gm_prompt(game, level, recent):
    topic = (GAME_CFG.get(game) or {}).get("topic")
    game = _gm_kind(game)
    aud = "primary school pupils aged 8 to 12 in Nigeria" if level == "Junior" else \
          "secondary school students aged 13 to 18 in Nigeria (WAEC/JAMB level)"
    mix = (f"Create {GAME_BATCH} items: {GAME_BATCH // 3} easy (tier 0), {GAME_BATCH // 3} medium (tier 1) and "
           f"{GAME_BATCH - 2 * (GAME_BATCH // 3)} hard (tier 2) for {aud}. ")
    avoid = ("Do NOT repeat or closely resemble any of these: " + " | ".join(recent) + ". ") if recent else ""
    nonce = f"Variation code {_uuid.uuid4().hex[:8]}. "
    tail = "Return ONLY a JSON array with no markdown and no commentary. Every fact must be accurate and well established. "
    if game == "trivia":
        spec = ('Multiple-choice general-knowledge questions (Nigeria, Africa, science, geography, history, English, maths). '
                'One clearly correct answer and 3 plausible but clearly wrong options. No trick or opinion questions. '
                'Item format: {"tier":0,"q":"question?","a":"correct answer","w":["wrong1","wrong2","wrong3"]}')
    elif game == "science":
        spec = ('Sorting questions for science. Each item picks a topic that has 2 to 4 categories, one thing to sort and the '
                'category it belongs to (e.g. topic "States of matter", cats ["Solid","Liquid","Gas"], item "Ice", a "Solid"). '
                'Use many different topics. Item format: {"tier":0,"topic":"...","cats":["...","..."],"item":"...","a":"one of cats"}')
    elif game == "word":
        spec = ('Spelling words that students should know (4 to 14 letters, letters only, correctly spelt, no names). '
                'Harder tiers use longer or trickier words. hint = a short, clear meaning that does NOT contain the word. '
                'Item format: {"tier":0,"w":"school","hint":"A place where students learn"}')
    elif game == "sentence":
        spec = ('Natural English sentences for a word-ordering game. Easy: 4 to 7 words, medium: 8 to 11 words, hard: 12 to 16 words. '
                'Start with a capital letter, end with . ? or !. Use everyday Nigerian school and home life, in clear standard English. '
                'd = 2 extra wrong words that do not appear in the sentence. '
                'f = a fill-in-the-blank object for ONE word of the sentence: "w" is that exact word as written in the sentence, '
                '"o" is 3 wrong options of the same kind of word (for example wrong verb forms or wrong prepositions) that clearly '
                'make the sentence incorrect. Leave out "hint" for most items; only about 1 in 5 items may add "hint" with the '
                'sentence in simple Nigerian Pidgin. '
                'Item format: {"tier":0,"s":"The boy is reading a book.","d":["went","very"],'
                '"f":{"w":"reading","o":["read","reads","readed"]}}')
    else:
        spec = ('Word-puzzle questions, mixing these 6 types evenly across the batch - vary "type" between them: '
                '(1) "spell": q asks to choose the correctly spelt word, a = the correct spelling, w = 2-3 plausible '
                'misspellings of the SAME word. '
                '(2) "odd": q asks which word is the odd one out among a group that share a category, a = the odd '
                'word, w = 2-3 words that genuinely belong together (the group it does not fit). '
                '(3) "syn": q asks for the word closest in meaning to a given word, a = the correct synonym, w = '
                '2-3 unrelated or opposite-meaning words. '
                '(4) "ant": q asks for the opposite of a given word, a = the correct antonym, w = 2-3 words that are '
                'not opposites. '
                '(5) "code": q presents a simple letter-shift cipher example then asks how a second word would be '
                'written in the same code, a = the correctly encoded word, w = 2-3 incorrectly encoded versions. '
                '(6) "anagram": q shows scrambled letters of a real word and asks what word they spell, a = the '
                'correct word, w = 2-3 other real words of similar length. '
                'Keep q under 200 characters. Item format: '
                '{"tier":0,"type":"syn","q":"Choose the word closest in meaning to HAPPY:","a":"joyful",'
                '"w":["angry","tired","confused"]}')
    if topic:
        spec = ('Multiple-choice questions ONLY about this topic: ' + str(topic) + '. One clearly correct answer and 3 plausible '
                'but clearly wrong options. No trick or opinion questions. '
                'Item format: {"tier":0,"q":"question?","a":"correct answer","w":["wrong1","wrong2","wrong3"]}')
    return f"You write learning-game content. {spec} {mix}{avoid}{nonce}{tail}"

def _gm_parse_ai(raw):
    if not raw or "AI services unavailable" in raw[:60]:
        return []
    t = raw.replace("```json", "").replace("```", "").strip()
    a, b = t.find("["), t.rfind("]")
    if a < 0 or b <= a:
        return []
    try:
        arr = json.loads(t[a:b + 1])
    except Exception:
        return []
    out = []
    for it in arr if isinstance(arr, list) else []:
        if isinstance(it, dict):
            try:
                tier = int(it.get("tier", 0))
            except Exception:
                tier = 0
            out.append((tier if tier in (0, 1, 2) else 1, it))
    return out

def _gm_generate(game, level):
    """Ask the AI for a batch and save it. Safe to call from a background task. Returns (added, skipped)."""
    key = (game, level)
    now = _time.time()
    with _GM_GEN_LOCK:
        st = _GM_GEN_STATE.setdefault(key, {"running": False, "next_ok": 0})
        if st["running"] or now < st["next_ok"]:
            return 0, 0
        st["running"] = True
    db = SessionLocal()
    added = skipped = 0
    try:
        recent_rows = (db.query(GameItem).filter(GameItem.game == game, GameItem.level == level)
                         .order_by(GameItem.id.desc()).limit(25).all())
        recent = []
        for r in recent_rows:
            try:
                recent.append(_gm_clip(_gm_key_text(game, json.loads(r.payload)), 70))
            except Exception:
                pass
        raw = get_ai_response(_gm_prompt(game, level, recent))
        entries = _gm_parse_ai(raw)
        if entries:
            added, skipped = _gm_save_items(db, game, level, entries, "ai")
            print(f"[GAMES] AI added {added} {game}/{level} (skipped {skipped})")
        with _GM_GEN_LOCK:
            _GM_GEN_STATE[key]["next_ok"] = _time.time() + (30 if added else 300)   # back off if the AI failed
    except Exception as e:
        db.rollback()
        print(f"[GAMES AI ERROR] {game}/{level}: {e}")
        with _GM_GEN_LOCK:
            _GM_GEN_STATE[key]["next_ok"] = _time.time() + 300
    finally:
        db.close()
        with _GM_GEN_LOCK:
            _GM_GEN_STATE[key]["running"] = False
    return added, skipped

def _gm_topup(game, level):
    """Background: if any tier is running low, ask the AI for more."""
    db = SessionLocal()
    try:
        rows = (db.query(GameItem.tier, sa_func.count(GameItem.id))
                  .filter(GameItem.game == game, GameItem.level == level, GameItem.active.is_(True))
                  .group_by(GameItem.tier).all())
        have = {t: c for t, c in rows}
        low = any(have.get(t, 0) < GAME_POOL_TARGET for t in (0, 1, 2))
    except Exception as e:
        print(f"[GAMES TOPUP ERROR] {e}")
        low = False
    finally:
        db.close()
    if low:
        _gm_generate(game, level)


# ---------- picking questions ----------
def _gm_pick(db, user_id, game, level, tiers):
    seen = set()
    for (raw,) in (db.query(GameSession.item_ids)
                     .filter(GameSession.user_id == user_id, GameSession.game == game)
                     .order_by(GameSession.started_ts.desc()).limit(6).all()):
        try:
            seen.update(json.loads(raw or "[]"))
        except Exception:
            pass
    base = db.query(GameItem).filter(GameItem.game == game, GameItem.level == level, GameItem.active.is_(True))
    chosen, ids = [], set()
    for t in sorted(set(tiers)):
        q = base.filter(GameItem.tier == t)
        if seen:
            q = q.filter(~GameItem.id.in_(list(seen)))
        for r in q.order_by(sa_func.random()).limit(tiers.count(t)).all():
            chosen.append(r)
            ids.add(r.id)
    missing = len(tiers) - len(chosen)
    for avoid_seen in (True, False):          # fill shortfall: unseen first, then anything
        if missing <= 0:
            break
        q = base
        if ids:
            q = q.filter(~GameItem.id.in_(list(ids)))
        if avoid_seen and seen:
            q = q.filter(~GameItem.id.in_(list(seen)))
        for r in q.order_by(sa_func.random()).limit(missing).all():
            chosen.append(r)
            ids.add(r.id)
        missing = len(tiers) - len(chosen)
    chosen.sort(key=lambda r: r.tier or 0)
    return chosen


# ---------- what the player is allowed to see ----------
def _gm_public(game, item, p):
    cfg_ = GAME_CFG[game]
    game = _gm_kind(game)
    tier = item.tier if item.tier in (0, 1, 2) else 0
    secs = cfg_["secs"][tier] if cfg_["secs"] else None
    base = {"tier": tier, "secs": secs}
    if game == "trivia":
        opts = [p["a"]] + list(p["w"])[:3]
        random.shuffle(opts)
        base.update({"kind": "choice", "q": p["q"], "options": opts})
    elif game == "science":
        others = [c for c in p["cats"] if c != p["a"]]
        random.shuffle(others)
        opts = [p["a"]] + others[:3]
        random.shuffle(opts)
        base.update({"kind": "choice", "q": f"Which group does {p['item']} belong to?", "options": opts})
    elif game == "word":
        letters = list(p["w"].upper())
        shuffled, guard = letters[:], 0
        while guard < 20:
            random.shuffle(shuffled)
            if shuffled != letters or len(set(letters)) == 1:
                break
            guard += 1
        # the browser reads the word aloud (listen + spell), so it needs the text
        base.update({"kind": "word", "hint": p["hint"], "letters": shuffled, "len": len(letters), "speak": p["w"]})
    elif game == "sentence":
        s = p["s"]
        end = s[-1] if s[-1] in ".?!" else "."
        words = s.rstrip(".?!").split()
        if words and not GAME_PROPER.match(words[0]):
            words[0] = words[0][:1].lower() + words[0][1:]
        low = {re.sub(r"[,;:]", "", w).lower() for w in words}
        pool = p.get("d") or [d for d in GAME_SENT_DECOYS[0 if item.level == "Junior" else 1] if d not in low]
        random.shuffle(pool)
        decoys = [d for d in pool if d not in low][: (1 if tier == 0 else 2)]
        tiles = [{"id": i, "w": w} for i, w in enumerate(words + decoys)]
        random.shuffle(tiles)
        mode = "build" if (p.get("hint") and random.random() < 0.5) else "listen"
        base.update({"kind": "sentence", "tiles": tiles, "end": end, "mode": mode, "hint": p.get("hint") if mode == "build" else None,
                     "speak": s if mode == "listen" else None})
    elif game == "puzzle":
        opts = [p["a"]] + list(p["w"])[:3]
        random.shuffle(opts)
        base.update({"kind": "choice", "type": p.get("type"), "q": p["q"], "options": opts})
    return base

def _gm_answer_text(game, p):
    game = _gm_kind(game)
    return {"trivia": p.get("a"), "science": p.get("a"), "word": p.get("w"), "sentence": p.get("s"),
            "puzzle": p.get("a")}.get(game, "")

def _gm_check(game, p, choice):
    game = _gm_kind(game)
    if choice is None:
        return False
    if game == "sentence":
        return _gm_norm_sentence(choice) == _gm_norm_sentence(p.get("s"))
    return _gm_norm(choice) == _gm_norm(_gm_answer_text(game, p))

def _gm_timed_out(secs, elapsed_raw, is_first, choice):
    if choice is None:
        return True
    if not secs:
        return False
    return max(0.0, float(elapsed_raw) - (1.0 if is_first else 3.0)) > secs + 4.0

def _gm_points(cfg, tier, secs, elapsed_raw, is_first, streak):
    pts = cfg["base"] + tier * cfg["tier_bonus"]
    if secs and cfg.get("speed_bonus"):
        elapsed = max(0.0, float(elapsed_raw) - (1.0 if is_first else 3.0))
        if (secs - elapsed) / float(secs) > 0.5:
            pts += cfg["speed_bonus"]
    if streak >= 3:
        pts += cfg["streak_bonus"]
    return pts


# ---------- request bodies + login ----------
class GameStartIn(BaseModel):
    game: str
    level: str = "Junior"
    username: Optional[str] = None     # only used by the activity logger

class GameAnswerIn(BaseModel):
    session_id: str
    idx: int
    choice: Optional[str] = None       # None = ran out of time
    username: Optional[str] = None

class GameFinishIn(BaseModel):
    session_id: str
    username: Optional[str] = None

class GameAdminItem(BaseModel):
    level: str
    tier: int = 0
    payload: dict

class GameAdminItems(BaseModel):
    game: str
    items: List[GameAdminItem]

class GameAdminGen(BaseModel):
    game: str
    level: str = "Junior"
    rounds: int = 1

class GameAdminActive(BaseModel):
    active: bool

def _game_user(authorization: Optional[str] = Header(None), db=Depends(get_db)):
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Please login to play.")
    username = decode_token(authorization[7:].strip())
    if not username:
        raise HTTPException(401, "Session expired. Please login again.")
    user = db.query(User).filter(User.username == username).first()
    if not user:
        raise HTTPException(401, "User not found. Please login again.")
    return user


# ---------- player endpoints ----------
@app.post("/games/start")
def games_start(data: GameStartIn, background_tasks: BackgroundTasks, user=Depends(_game_user), db=Depends(get_db)):
    cfg = _gm_cfg_for(data.game)
    if not cfg or cfg.get("lesson_only"):
        raise HTTPException(404, "Unknown game")
    level = data.level if data.level in GAME_LEVELS else "Junior"
    chosen = _gm_pick(db, user.id, data.game, level, cfg["tiers"])
    if not chosen:
        _gm_generate(data.game, level)                 # empty pool: make the first batch now (player waits once)
        chosen = _gm_pick(db, user.id, data.game, level, cfg["tiers"])
        if not chosen:
            raise HTTPException(503, "No questions yet. Please try again in a moment.")
    background_tasks.add_task(_gm_topup, data.game, level)
    now = _time.time()
    sid = _uuid.uuid4().hex
    state = {"tries": {}, "n0": len(chosen), "requeued": [], "over": False}
    if cfg.get("hearts"):
        state["hearts"] = cfg["hearts"]
    db.add(GameSession(id=sid, user_id=user.id, game=data.game, level=level,
                       item_ids=json.dumps([i.id for i in chosen]), answers="{}", state=json.dumps(state),
                       points=0, correct=0, streak=0, started_ts=now, last_ts=now, finished=False, coins=0))
    db.commit()
    questions = []
    for idx, it in enumerate(chosen):
        q = _gm_public(data.game, it, json.loads(it.payload))
        q["idx"] = idx
        questions.append(q)
    return {"session_id": sid, "game": data.game, "level": level, "n": len(questions),
            "hearts": cfg.get("hearts"), "questions": questions}


@app.post("/games/answer")
def games_answer(data: GameAnswerIn, user=Depends(_game_user), db=Depends(get_db)):
    s = (db.query(GameSession).filter(GameSession.id == data.session_id, GameSession.user_id == user.id)
           .with_for_update().first())
    if not s:
        raise HTTPException(404, "Round not found")
    if s.finished:
        raise HTTPException(400, "This round is already finished")
    cfg = _gm_cfg_for(s.game)
    if not cfg:
        raise HTTPException(404, "Unknown game")
    ids = json.loads(s.item_ids)
    answers = json.loads(s.answers or "{}")
    state = json.loads(s.state or "{}")
    key = str(data.idx)
    if key in answers:                                  # repeated request (network retry): same result, no double points
        return answers[key]
    if state.get("over"):
        raise HTTPException(400, "This round is over")
    if data.idx < 0 or data.idx >= len(ids) or data.idx != len(answers):
        raise HTTPException(400, "Answer the questions in order")
    if s.game == "maths":
        return _math_answer(db, s, user, cfg, ids, answers, state, data)
    item = db.query(GameItem).filter(GameItem.id == ids[data.idx]).first()
    if not item:
        raise HTTPException(404, "Question not found")
    p = json.loads(item.payload)
    tier = item.tier if item.tier in (0, 1, 2) else 0
    secs = cfg["secs"][tier] if cfg["secs"] else None
    now = _time.time()
    elapsed_raw = now - (s.last_ts or now)
    is_first = data.idx == 0
    timed_out = _gm_timed_out(secs, elapsed_raw, is_first, data.choice)
    ex = (state.get("ex") or {}).get(key)
    if ex and ex.get("t") == "fill":
        ok = (not timed_out) and data.choice is not None and _gm_norm(data.choice) == _gm_norm(ex.get("a"))
    else:
        ok = (not timed_out) and _gm_check(s.game, p, data.choice)

    tries = int((state.get("tries") or {}).get(key, 0))
    if not ok and not timed_out and tries + 1 < cfg["tries"]:      # e.g. Word Builder allows 3 spellings
        state.setdefault("tries", {})[key] = tries + 1
        s.state = json.dumps(state)
        db.commit()
        return {"idx": data.idx, "final": False, "correct": False, "tries_left": cfg["tries"] - tries - 1}

    streak = (s.streak or 0) + 1 if ok else 0
    points = _gm_points(cfg, tier, secs, elapsed_raw, is_first, streak) if ok else 0
    hearts, out, requeue_idx = state.get("hearts"), False, None
    if not ok and cfg.get("hearts"):
        hearts = max(0, int(hearts if hearts is not None else cfg["hearts"]) - 1)
        state["hearts"] = hearts
        if hearts == 0:
            state["over"] = True
            out = True
    if not ok and cfg.get("requeue") and not out and item.id not in (state.get("requeued") or []):
        ids.append(item.id)
        state.setdefault("requeued", []).append(item.id)
        requeue_idx = len(ids) - 1
        s.item_ids = json.dumps(ids)
        if (state.get("ex") or {}).get(key):
            state["ex"][str(requeue_idx)] = dict(state["ex"][key])
    ans_text = _gm_answer_text(s.game, p)
    why = p.get("why") or (f"{p.get('item')} belongs to {p.get('a')}." if s.game == "science" else None)
    res = {"idx": data.idx, "final": True, "correct": bool(ok), "timed_out": bool(timed_out),
           "correct_answer": ans_text, "why": why, "points": points, "streak": streak,
           "total_points": int(s.points or 0) + points, "hearts": hearts, "out_of_hearts": out,
           "requeue_idx": requeue_idx}
    answers[key] = res
    s.answers = json.dumps(answers, ensure_ascii=False)
    s.state = json.dumps(state)
    s.points = res["total_points"]
    s.correct = int(s.correct or 0) + (1 if ok else 0)
    s.streak = streak
    s.last_ts = now
    db.commit()
    return res


@app.post("/games/finish")
def games_finish(data: GameFinishIn, user=Depends(_game_user), db=Depends(get_db)):
    s = (db.query(GameSession).filter(GameSession.id == data.session_id, GameSession.user_id == user.id)
           .with_for_update().first())
    if not s:
        raise HTTPException(404, "Round not found")
    if s.finished and s.result:
        return json.loads(s.result)
    cfg = _gm_cfg_for(s.game)
    if not cfg:
        raise HTTPException(404, "Unknown game")
    ids = json.loads(s.item_ids)
    answers = json.loads(s.answers or "{}")
    state = json.loads(s.state or "{}")
    if len(answers) < len(ids) and not state.get("over"):
        raise HTTPException(400, "Finish all the questions first")
    n0 = int(state.get("n0") or len(ids))
    first_try = sum(1 for k, a in answers.items() if int(k) < n0 and a.get("correct"))
    perfect = (len(answers) >= n0 and all(a.get("correct") for a in answers.values()) and not state.get("over"))
    pts = int(s.points or 0)
    bonus = cfg.get("perfect_bonus", 0) if perfect else 0
    pts += bonus
    want = max(1, pts // 10) if pts > 0 else 0
    today = today_str()
    coins = want
    if want:
        earned = (db.query(sa_func.coalesce(sa_func.sum(GameSession.coins), 0))
                    .filter(GameSession.user_id == user.id, GameSession.day == today,
                            GameSession.finished.is_(True)).scalar()) or 0
        coins = max(0, min(want, GAME_DAILY_COIN_CAP - int(earned)))
    user.coins = int(user.coins or 0) + coins
    if pts > 0:
        db.add(GameScore(user_id=user.id, game_type=cfg["save"], score=pts, played_at=now_str()))
    s.points = pts
    s.finished = True
    s.coins = coins
    s.day = today
    result = {"points": pts, "correct": first_try, "total": n0, "coins_earned": coins,
              "total_coins": int(user.coins or 0), "coin_cap_hit": coins < want,
              "perfect": perfect, "perfect_bonus": bonus, "out_of_hearts": bool(state.get("over")),
              "hearts": state.get("hearts")}
    result = _lesson_finish(db, user, s, state, result)
    s.result = json.dumps(result)
    db.commit()
    return result


# ---------- admin endpoints (X-Admin-Key) ----------
@app.post("/admin/games/items")
def admin_games_add(data: GameAdminItems, db=Depends(get_db), _ok=Depends(verify_admin)):
    _cfg = _gm_cfg_for(data.game)
    if not _cfg or _cfg.get("lesson_only"):
        raise HTTPException(400, "Unknown game")
    by_level = {}
    for it in data.items:
        by_level.setdefault(it.level, []).append((it.tier, it.payload))
    added = skipped = 0
    for lvl, entries in by_level.items():
        a, s_ = _gm_save_items(db, data.game, lvl, entries, "admin")
        added += a
        skipped += s_
    return {"added": added, "skipped": skipped}

@app.get("/admin/games/items")
def admin_games_list(game: str = "", level: str = "", source: str = "", active: str = "",
                     limit: int = 50, offset: int = 0, db=Depends(get_db), _ok=Depends(verify_admin)):
    q = db.query(GameItem)
    if game:   q = q.filter(GameItem.game == game)
    if level:  q = q.filter(GameItem.level == level)
    if source: q = q.filter(GameItem.source == source)
    if active in ("true", "false"): q = q.filter(GameItem.active.is_(active == "true"))
    total = q.count()
    rows = q.order_by(GameItem.id.desc()).offset(max(0, offset)).limit(min(max(1, limit), 200)).all()
    items = []
    for r in rows:
        try:
            pl = json.loads(r.payload)
        except Exception:
            pl = {}
        items.append({"id": r.id, "game": r.game, "level": r.level, "tier": r.tier, "source": r.source,
                      "active": bool(r.active), "created_at": r.created_at, "payload": pl})
    return {"total": total, "items": items}

@app.post("/admin/games/items/{item_id}/active")
def admin_games_set_active(item_id: int, data: GameAdminActive, db=Depends(get_db), _ok=Depends(verify_admin)):
    r = db.query(GameItem).filter(GameItem.id == item_id).first()
    if not r:
        raise HTTPException(404, "Item not found")
    r.active = bool(data.active)
    db.commit()
    return {"id": item_id, "active": r.active}

@app.delete("/admin/games/items/{item_id}")
def admin_games_delete(item_id: int, db=Depends(get_db), _ok=Depends(verify_admin)):
    r = db.query(GameItem).filter(GameItem.id == item_id).first()
    if not r:
        raise HTTPException(404, "Item not found")
    db.delete(r)
    db.commit()
    return {"deleted": item_id}

@app.get("/admin/games/stats")
def admin_games_stats(db=Depends(get_db), _ok=Depends(verify_admin)):
    rows = (db.query(GameItem.game, GameItem.level, GameItem.tier, GameItem.source, sa_func.count(GameItem.id))
              .filter(GameItem.active.is_(True))
              .group_by(GameItem.game, GameItem.level, GameItem.tier, GameItem.source).all())
    return [{"game": g, "level": l, "tier": t, "source": s_, "count": c} for g, l, t, s_, c in rows]

@app.post("/admin/games/generate")
def admin_games_generate(data: GameAdminGen, _ok=Depends(verify_admin)):
    """Fill the pool with AI questions right now (each round = one batch of about 15). Run this before launch."""
    _cfg = _gm_cfg_for(data.game)
    if not _cfg or _cfg.get("lesson_only") or data.level not in GAME_LEVELS:
        raise HTTPException(400, "Unknown game or level")
    total_added = total_skipped = 0
    for _ in range(max(1, min(data.rounds, 5))):
        with _GM_GEN_LOCK:
            _GM_GEN_STATE.setdefault((data.game, data.level), {"running": False, "next_ok": 0})["next_ok"] = 0
        a, s_ = _gm_generate(data.game, data.level)
        total_added += a
        total_skipped += s_
    return {"added": total_added, "skipped": total_skipped}

print("[GAMES] Routes ready: /games/start /games/answer /games/finish + /admin/games/* [OK]")

# ============================================================
# LESSON PATH (Duolingo style) + DATA-DRIVEN GAMES
#  - game_defs      : add a brand-new multiple-choice game from the admin panel (no code)
#  - game_progress  : which lesson each user has unlocked on each path
#  - game_streaks   : daily streak, streak freezes and daily XP goal
#  Tracks: "sentence" (Build the Sentence) and "maths" (Maths Path)
# ============================================================
GAME_CFG["maths"] = {"n": 8, "tiers": [], "secs": None, "save": "maths", "tries": 1, "base": 10, "tier_bonus": 5,
                     "speed_bonus": 0, "streak_bonus": 5, "hearts": 3, "requeue": True, "perfect_bonus": 20,
                     "lesson_only": True}
RESERVED_SLUGS = {"trivia", "science", "word", "sentence", "puzzle", "maths", "mathpath", "math", "memory",
                  "pattern", "flash", "hunt", "crossword"}
FREEZE_COST = 50
MAX_FREEZES = 2


class GameDef(Base):
    __tablename__ = "game_defs"
    slug       = Column(String, primary_key=True)
    name       = Column(String)
    icon       = Column(String)
    blurb      = Column(String)
    grad       = Column(String)
    topic      = Column(Text)
    enabled    = Column(Boolean, default=True)
    created_at = Column(String, nullable=True)


class GameProgress(Base):
    __tablename__ = "game_progress"
    __table_args__ = (UniqueConstraint("user_id", "track", "level", name="uq_game_progress"),)
    id      = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), index=True)
    track   = Column(String)
    level   = Column(String)
    done    = Column(Integer, default=0)


class GameStreak(Base):
    __tablename__ = "game_streaks"
    user_id  = Column(Integer, ForeignKey("users.id"), primary_key=True)
    current  = Column(Integer, default=0)
    best     = Column(Integer, default=0)
    last_day = Column(String, nullable=True)
    freezes  = Column(Integer, default=1)
    xp_today = Column(Integer, default=0)
    goal     = Column(Integer, default=20)
    total_xp = Column(Integer, default=0)


for _tbl in (GameDef, GameProgress, GameStreak):
    try:
        _tbl.__table__.create(bind=engine, checkfirst=True)
    except Exception as _e:
        print(f"[LESSONS TABLE ERROR] {_tbl.__tablename__}: {_e}")


# ---------- data-driven games ----------
_GM_DEFS_TS = {"t": 0.0}

def _gm_load_defs(force=False):
    """Copy the enabled rows of game_defs into GAME_CFG (as multiple-choice games)."""
    now = _time.time()
    if not force and now - _GM_DEFS_TS["t"] < 20:
        return
    _GM_DEFS_TS["t"] = now
    db = None
    try:
        db = SessionLocal()
        live = set()
        for r in db.query(GameDef).all():
            if not r.enabled:
                continue
            if r.slug in GAME_CFG and not GAME_CFG[r.slug].get("dynamic"):
                continue                                   # never override a built-in game
            c = dict(GAME_CFG["trivia"])
            c.update({"dynamic": True, "save": r.slug, "topic": r.topic or r.name, "name": r.name,
                      "icon": r.icon or "🎯", "blurb": r.blurb or "", "grad": r.grad or "#6366f1,#818cf8"})
            GAME_CFG[r.slug] = c
            live.add(r.slug)
        for slug in [k for k, v in GAME_CFG.items() if v.get("dynamic") and k not in live]:
            del GAME_CFG[slug]
    except Exception as e:
        print(f"[GAMES DEFS ERROR] {e}")
    finally:
        if db is not None:
            db.close()

def _gm_cfg_for(game):
    cfg = GAME_CFG.get(game)
    if cfg is None:
        _gm_load_defs()
        cfg = GAME_CFG.get(game)
    return cfg

def _gm_kind(game):
    """Games created in the admin panel behave exactly like trivia (question + 1 right + 3 wrong)."""
    return "trivia" if (GAME_CFG.get(game) or {}).get("dynamic") else game

_gm_load_defs(force=True)


class GameDefIn(BaseModel):
    slug: str
    name: str
    icon: str = "🎯"
    blurb: str = ""
    grad: str = "#6366f1,#818cf8"
    topic: str = ""
    enabled: bool = True

@app.get("/games/catalog")
def games_catalog():
    _gm_load_defs()
    return [{"id": s, "name": c["name"], "ic": c["icon"], "d": c["blurb"], "g": c["grad"]}
            for s, c in GAME_CFG.items() if c.get("dynamic")]

@app.get("/admin/games/defs")
def admin_games_defs(db=Depends(get_db), _ok=Depends(verify_admin)):
    return [{"slug": r.slug, "name": r.name, "icon": r.icon, "blurb": r.blurb, "grad": r.grad,
             "topic": r.topic, "enabled": bool(r.enabled)} for r in db.query(GameDef).order_by(GameDef.created_at).all()]

@app.post("/admin/games/defs")
def admin_games_def_save(data: GameDefIn, db=Depends(get_db), _ok=Depends(verify_admin)):
    slug = (data.slug or "").strip().lower()
    if not re.fullmatch(r"[a-z][a-z0-9_]{2,20}", slug) or slug in RESERVED_SLUGS:
        raise HTTPException(400, "Game id must be 3-21 letters/numbers/underscore, start with a letter, and not be a built-in name")
    name = _gm_clip(data.name, 40)
    if not name:
        raise HTTPException(400, "Game name is required")
    r = db.query(GameDef).filter(GameDef.slug == slug).first()
    if not r:
        r = GameDef(slug=slug, created_at=now_str())
        db.add(r)
    r.name, r.icon, r.blurb = name, _gm_clip(data.icon, 8) or "🎯", _gm_clip(data.blurb, 80)
    r.grad = _gm_clip(data.grad, 40) or "#6366f1,#818cf8"
    r.topic, r.enabled = _gm_clip(data.topic, 400), bool(data.enabled)
    db.commit()
    _gm_load_defs(force=True)
    return {"slug": slug, "saved": True}

@app.post("/admin/games/defs/{slug}/enabled")
def admin_games_def_enable(slug: str, data: GameAdminActive, db=Depends(get_db), _ok=Depends(verify_admin)):
    r = db.query(GameDef).filter(GameDef.slug == slug).first()
    if not r:
        raise HTTPException(404, "Game not found")
    r.enabled = bool(data.active)
    db.commit()
    _gm_load_defs(force=True)
    return {"slug": slug, "enabled": r.enabled}


# ---------- Duolingo-style streak + daily goal ----------
def _day(s):
    return datetime.strptime(s, "%Y-%m-%d").date()

def _streak_row(db, user_id):
    row = db.query(GameStreak).filter(GameStreak.user_id == user_id).first()
    if not row:
        row = GameStreak(user_id=user_id, current=0, best=0, last_day=None, freezes=1, xp_today=0, goal=20, total_xp=0)
        db.add(row)
        db.flush()
    return row

def _streak_view(row):
    today = today_str()
    cur, xp_today, at_risk = int(row.current or 0), int(row.xp_today or 0), False
    if row.last_day:
        gap = (_day(today) - _day(row.last_day)).days
        if gap == 0:
            pass
        elif gap == 1 or (gap == 2 and int(row.freezes or 0) > 0):
            at_risk, xp_today = cur > 0, 0          # still alive: do a lesson today (a freeze covers a single missed day)
        else:
            cur, xp_today = 0, 0                    # streak lost
    else:
        cur, xp_today = 0, 0
    return {"current": cur, "best": max(int(row.best or 0), cur), "freezes": int(row.freezes or 0),
            "xp_today": xp_today, "goal": int(row.goal or 20), "at_risk": at_risk,
            "done_today": row.last_day == today}

def _streak_add(row, xp):
    """Call when a lesson is completed. Returns the XP the user already had today."""
    today = today_str()
    before = int(row.xp_today or 0) if row.last_day == today else 0
    if row.last_day != today:
        gap = (_day(today) - _day(row.last_day)).days if row.last_day else None
        if gap == 1:
            row.current = int(row.current or 0) + 1
        elif gap == 2 and int(row.freezes or 0) > 0:
            row.freezes = int(row.freezes) - 1
            row.current = int(row.current or 0) + 1
        else:
            row.current = 1
        row.last_day = today
        row.xp_today = 0
    row.xp_today = int(row.xp_today or 0) + int(xp)
    row.total_xp = int(row.total_xp or 0) + int(xp)
    row.best = max(int(row.best or 0), int(row.current or 0))
    return before


# ---------- maths problems made by the server ----------
_SUP = str.maketrans("0123456789-", "⁰¹²³⁴⁵⁶⁷⁸⁹⁻")
_SUB = str.maketrans("0123456789", "₀₁₂₃₄₅₆₇₈₉")
def _sup(n): return str(n).translate(_SUP)
def _sub(n): return str(n).translate(_SUB)
def _ri(a, b): return random.randint(a, b)

def _mg_j_add():
    a, b = _ri(12, 99), _ri(12, 99)
    return f"{a} + {b} = ?", a + b, [], f"{a} + {b} = {a + b}."
def _mg_j_sub():
    a, b = _ri(40, 150), _ri(11, 39)
    return f"{a} − {b} = ?", a - b, [], f"{a} − {b} = {a - b}."
def _mg_j_mul():
    a, b = _ri(3, 12), _ri(3, 12)
    return f"{a} × {b} = ?", a * b, [], f"{a} × {b} = {a * b}."
def _mg_j_div():
    b, c = _ri(3, 12), _ri(3, 12)
    return f"{b * c} ÷ {b} = ?", c, [], f"{b} × {c} = {b * c}, so {b * c} ÷ {b} = {c}."
def _mg_j_pct_easy():
    p = random.choice([10, 50])
    x = _ri(2, 15) * 10 if p == 10 else _ri(2, 30) * 2
    return f"What is {p}% of {x}?", x * p // 100, [], f"{p}% of {x} = {x * p // 100}."
def _mg_j_frac():
    f = random.choice([[1, 2], [1, 4], [3, 4], [2, 5], [1, 5], [2, 3]])
    k = _ri(2, 12)
    x = f[1] * k
    return f"What is {f[0]}/{f[1]} of {x}?", f[0] * k, [], f"{x} ÷ {f[1]} = {k}, then × {f[0]} = {f[0] * k}."
def _mg_j_pct():
    p = random.choice([25, 20, 75])
    u = 5 if p == 20 else 4
    x = u * _ri(3, 25)
    return f"What is {p}% of {x}?", x * p // 100, [], f"{p}% of {x} = {x * p // 100}."
def _mg_j_order1():
    a, b, c = _ri(2, 9), _ri(2, 9), _ri(2, 9)
    return f"{a} + {b} × {c} = ?", a + b * c, [(a + b) * c], f"Multiply first: {b} × {c} = {b * c}, then add {a} = {a + b * c}."
def _mg_j_order2():
    a, b, c = _ri(2, 9), _ri(2, 9), _ri(2, 5)
    return f"({a} + {b}) × {c} = ?", (a + b) * c, [a + b * c], f"Brackets first: {a} + {b} = {a + b}, then × {c} = {(a + b) * c}."
def _mg_j_area():
    L, W = _ri(4, 15), _ri(3, 12)
    return f"A rectangle is {L} cm long and {W} cm wide. What is its area in cm²?", L * W, [2 * (L + W)], f"Area = {L} × {W} = {L * W} cm²."
def _mg_j_perim():
    L, W = _ri(4, 15), _ri(3, 12)
    return f"A rectangle is {L} cm long and {W} cm wide. What is its perimeter in cm?", 2 * (L + W), [L * W], f"Perimeter = 2 × ({L} + {W}) = {2 * (L + W)} cm."
def _mg_j_eq1():
    x, a = _ri(3, 20), _ri(4, 30)
    return f"Solve for x: x + {a} = {x + a}", x, [], f"x = {x + a} − {a} = {x}."
def _mg_j_lcm():
    p = random.choice([[4, 6], [6, 8], [8, 12], [9, 12], [10, 15], [12, 18], [6, 9]])
    g = math.gcd(p[0], p[1])
    return f"What is the LCM of {p[0]} and {p[1]}?", p[0] * p[1] // g, [g, p[0] * p[1]], f"The smallest number both {p[0]} and {p[1]} divide into is {p[0] * p[1] // g}."
def _mg_j_hcf():
    p = random.choice([[12, 18], [8, 12], [18, 24], [20, 30], [16, 24], [15, 45]])
    g = math.gcd(p[0], p[1])
    return f"What is the HCF of {p[0]} and {p[1]}?", g, [p[0] * p[1] // g], f"The biggest number that divides both {p[0]} and {p[1]} is {g}."
def _mg_j_eq2():
    x, a, c = _ri(2, 12), _ri(2, 5), _ri(1, 9)
    return f"Solve for x: {a}x + {c} = {a * x + c}", x, [], f"{a}x = {a * x + c} − {c} = {a * x}, so x = {x}."
def _mg_j_price():
    P, r = _ri(2, 20) * 100, random.choice([10, 20, 25, 50])
    return f"A price of ₦{P} goes up by {r}%. What is the new price (₦)?", P + P * r // 100, [P * r // 100], f"Increase = {P * r // 100}, so new price = ₦{P + P * r // 100}."
def _mg_j_mean():
    m = _ri(5, 30)
    x, y = _ri(1, m - 1), _ri(1, m - 1)
    nums = [m - x, m + x, m - y, m + y]
    random.shuffle(nums)
    return f"Find the mean of {', '.join(map(str, nums))}.", m, [], f"The numbers add up to {4 * m}, and {4 * m} ÷ 4 = {m}."
def _mg_j_tri():
    b, h = _ri(4, 20), _ri(3, 16)
    if (b * h) % 2:
        b += 1
    return f"A triangle has base {b} cm and height {h} cm. What is its area in cm²?", b * h // 2, [b * h], f"Area = ½ × {b} × {h} = {b * h // 2} cm²."
def _mg_j_sqdiff():
    a = _ri(5, 12)
    b = _ri(2, a - 1)
    return f"What is {a}² − {b}²?", a * a - b * b, [(a - b) * (a - b)], f"{a * a} − {b * b} = {a * a - b * b}."

def _mg_s_eq1():
    x, a, b = _ri(2, 12), _ri(2, 9), _ri(1, 15)
    return f"Solve for x: {a}x − {b} = {a * x - b}", x, [], f"{a}x = {a * x - b} + {b} = {a * x}, so x = {x}."
def _mg_s_pow():
    b = random.choice([2, 3, 4, 5])
    e = _ri(4, 9) if b == 2 else _ri(3, 5) if b == 3 else _ri(2, 4)
    return f"Evaluate {b}{_sup(e)}.", b ** e, [], f"{b} multiplied by itself {e} times is {b ** e}."
def _mg_s_sqrt():
    r = _ri(6, 20)
    return f"Evaluate √{r * r}.", r, [], f"{r} × {r} = {r * r}, so √{r * r} = {r}."
def _mg_s_si():
    P, R, T = _ri(2, 20) * 100, random.choice([5, 10, 4, 8]), _ri(2, 5)
    return f"Find the simple interest on ₦{P} at {R}% per annum for {T} years (₦).", P * R * T // 100, [P + P * R * T // 100], f"I = P × R × T ÷ 100 = {P} × {R} × {T} ÷ 100 = ₦{P * R * T // 100}."
def _mg_s_pct():
    p, x = random.choice([15, 30, 40, 60, 80]), 20 * _ri(2, 15)
    return f"What is {p}% of {x}?", x * p // 100, [], f"{p}% of {x} = {x * p // 100}."
def _mg_s_quad():
    r1 = _ri(1, 9)
    r2 = _ri(r1 + 1, 10)
    return f"Solve x² − {r1 + r2}x + {r1 * r2} = 0. What is the larger root?", r2, [r1, r1 + r2], f"It factorises as (x − {r1})(x − {r2}) = 0, so the roots are {r1} and {r2}."
def _mg_s_log():
    if random.random() < .5:
        k = _ri(2, 8)
        return f"Evaluate log{_sub(2)} {2 ** k}.", k, [], f"2{_sup(k)} = {2 ** k}, so log{_sub(2)} {2 ** k} = {k}."
    k = _ri(2, 5)
    return f"Evaluate log{_sub(10)} {10 ** k}.", k, [], f"10{_sup(k)} = {10 ** k}, so log{_sub(10)} {10 ** k} = {k}."
def _mg_s_sim():
    x = _ri(3, 15)
    y = _ri(1, x - 1)
    return f"If x + y = {x + y} and x − y = {x - y}, find x.", x, [y], f"Add the equations: 2x = {2 * x}, so x = {x}."
def _mg_s_median():
    nums = random.sample(range(2, 41), 5)
    srt = sorted(nums)
    random.shuffle(nums)
    return f"Find the median of {', '.join(map(str, nums))}.", srt[2], [], f"In order: {', '.join(map(str, srt))}. The middle one is {srt[2]}."
def _mg_s_grad():
    m, x1, dx, y1 = random.choice([2, 3, 4, 5, -2, -3]), _ri(0, 5), _ri(1, 4), _ri(-3, 8)
    return f"Find the gradient of the line through ({x1}, {y1}) and ({x1 + dx}, {y1 + m * dx}).", m, [], f"Gradient = change in y ÷ change in x = {m * dx} ÷ {dx} = {m}."
def _mg_s_ap_n():
    a, d, n = _ri(2, 9), _ri(2, 7), _ri(8, 15)
    return f"The first term of an AP is {a} and the common difference is {d}. Find the {n}th term.", a + (n - 1) * d, [a + n * d], f"Tₙ = a + (n − 1)d = {a} + {n - 1} × {d} = {a + (n - 1) * d}."
def _mg_s_ap_sum():
    n, a, d = _ri(6, 12), _ri(2, 9), _ri(2, 6)
    return f"Find the sum of the first {n} terms of an AP with first term {a} and common difference {d}.", n * (2 * a + (n - 1) * d) // 2, [], f"Sₙ = n/2 × (2a + (n − 1)d) = {n * (2 * a + (n - 1) * d) // 2}."
def _mg_s_gp():
    a, r, n = _ri(2, 5), _ri(2, 3), _ri(4, 6)
    return f"The first term of a GP is {a} and the common ratio is {r}. Find the {n}th term.", a * r ** (n - 1), [a * r ** n], f"Tₙ = a × r{_sup(n - 1)} = {a} × {r ** (n - 1)} = {a * r ** (n - 1)}."
def _mg_s_deriv():
    a, n, x = _ri(2, 6), _ri(2, 4), _ri(1, 4)
    return f"If y = {a}x{_sup(n)}, find dy/dx when x = {x}.", a * n * x ** (n - 1), [], f"dy/dx = {a * n}x{_sup(n - 1)}; at x = {x} that is {a * n * x ** (n - 1)}."
def _mg_s_comp():
    P, T = _ri(1, 5) * 10000, random.choice([2, 3])
    return f"Find the amount (₦) when ₦{P} is invested for {T} years at 10% compound interest per annum.", int(round(P * 1.1 ** T)), [int(P + P * 0.1 * T)], f"A = P × 1.1{_sup(T)} = ₦{int(round(P * 1.1 ** T))}."
def _mg_s_comb():
    n, k = _ri(5, 9), _ri(2, 3)
    return f"In how many ways can {k} students be chosen from {n}?", math.comb(n, k), [], f"{n}C{k} = {math.comb(n, k)}."
def _mg_s_disc():
    a, b, c = _ri(1, 3), random.choice([-6, -5, -4, -3, 3, 4, 5, 6]), random.choice([-4, -3, -2, 2, 3, 4, 5])
    return f"Find the discriminant of {a}x² {'−' if b < 0 else '+'} {abs(b)}x {'−' if c < 0 else '+'} {abs(c)} = 0.", b * b - 4 * a * c, [], f"b² − 4ac = {b * b} − {4 * a * c} = {b * b - 4 * a * c}."

MATH_GENS = {k[4:]: v for k, v in list(globals().items()) if k.startswith("_mg_")}

def _math_options(ans, alt):
    s = [ans]
    for x in alt or []:
        if isinstance(x, int) and x != ans and x not in s:
            s.append(x)
    step = 10 if abs(ans) > 200 else 1
    near = [ans + d * step for d in (1, 2, 3, 5, 10, -1, -2, -3, -5, -10)]
    random.shuffle(near)
    for x in near:
        if len(s) < 4 and x not in s and (ans < 0 or x >= 0):
            s.append(x)
    g = ans
    while len(s) < 4:
        g += 7
        if g not in s:
            s.append(g)
    s = s[:4]
    random.shuffle(s)
    return [str(x) for x in s]

def _math_num(v):
    t = str(v if v is not None else "").strip().replace("−", "-").replace(",", "").replace(" ", "")
    try:
        return float(t)
    except Exception:
        return None

def _math_check(ans, choice):
    v = _math_num(choice)
    return v is not None and abs(v - float(ans)) < 1e-6


# ---------- the lesson paths ----------
SENTENCE_UNITS = [("Basics", "🌱", [0] * 8), ("Everyday Sentences", "🏠", [0] * 5 + [1] * 3),
                  ("School Life", "🎒", [0] * 3 + [1] * 5), ("Questions & Answers", "❓", [1] * 6 + [2] * 2),
                  ("Longer Sentences", "📏", [1] * 3 + [2] * 5), ("Challenge", "🏆", [2] * 8)]
MATH_UNITS = {
    "Junior": [("Adding & Subtracting", "➕", ["j_add", "j_sub"]), ("Times Tables", "✖️", ["j_mul", "j_div"]),
               ("Fractions & Percent", "🍕", ["j_pct_easy", "j_frac", "j_pct"]),
               ("Order of Operations", "🔢", ["j_order1", "j_order2", "j_sqdiff"]),
               ("Shapes", "📐", ["j_area", "j_perim", "j_tri"]), ("Simple Equations", "⚖️", ["j_eq1", "j_eq2"]),
               ("Number Power", "🚀", ["j_lcm", "j_hcf", "j_mean", "j_price"])],
    "Senior": [("Equations", "⚖️", ["s_eq1", "s_sim"]), ("Powers & Roots", "🔢", ["s_pow", "s_sqrt", "s_log"]),
               ("Money", "💰", ["s_si", "s_pct", "s_comp"]), ("Statistics", "📊", ["s_median", "j_mean"]),
               ("Lines & Quadratics", "📈", ["s_grad", "s_quad", "s_disc"]),
               ("Sequences", "🔁", ["s_ap_n", "s_ap_sum", "s_gp"]), ("Calculus & Counting", "🧮", ["s_deriv", "s_comb"])],
}
LESSONS_PER_UNIT = 4
LESSON_TRACKS = {"sentence": "Build the Sentence", "maths": "Maths Path"}
UNIT_COLORS = ["#58cc02", "#1cb0f6", "#ce82ff", "#ff9600", "#ff4b4b", "#00cd9c", "#2b70c9"]

def _track_units(track, level):
    if track == "sentence":
        return [(t, i, v) for t, i, v in SENTENCE_UNITS]
    return [(t, i, v) for t, i, v in MATH_UNITS.get(level, MATH_UNITS["Junior"])]

def _lesson_locate(track, level, idx):
    """Global lesson number -> (unit index, lesson index inside the unit) or None."""
    units = _track_units(track, level)
    if idx < 0 or idx >= len(units) * LESSONS_PER_UNIT:
        return None
    return idx // LESSONS_PER_UNIT, idx % LESSONS_PER_UNIT

def _progress_row(db, user_id, track, level):
    row = (db.query(GameProgress).filter(GameProgress.user_id == user_id, GameProgress.track == track,
                                         GameProgress.level == level).first())
    if not row:
        row = GameProgress(user_id=user_id, track=track, level=level, done=0)
        db.add(row)
        db.flush()
    return row

SENT_PATTERN = ["arrange", "fill", "listen", "arrange", "fill", "arrange", "listen", "fill"]
MATH_PATTERN = ["choice", "choice", "type", "choice", "type", "choice", "choice", "type"]

def _sentence_tiles(item, p):
    s = p["s"]
    end = s[-1] if s[-1] in ".?!" else "."
    words = s.rstrip(".?!").split()
    if words and not GAME_PROPER.match(words[0]):
        words[0] = words[0][:1].lower() + words[0][1:]
    low = {re.sub(r"[,;:]", "", w).lower() for w in words}
    tier = item.tier if item.tier in (0, 1, 2) else 0
    pool = list(p.get("d") or [d for d in GAME_SENT_DECOYS[0 if item.level == "Junior" else 1] if d not in low])
    random.shuffle(pool)
    decoys = [d for d in pool if d not in low][: (1 if tier == 0 else 2)]
    tiles = [{"id": i, "w": w} for i, w in enumerate(words + decoys)]
    random.shuffle(tiles)
    return tiles, end

def _lesson_sentence_public(item, p, ex_type):
    tier = item.tier if item.tier in (0, 1, 2) else 0
    base = {"tier": tier, "secs": None}
    if ex_type == "fill":
        f = p["f"]
        blanked = re.sub(r"\b" + re.escape(f["w"]) + r"\b", "_____", p["s"], count=1, flags=re.I)
        opts = [f["w"]] + list(f["o"])[:3]
        random.shuffle(opts)
        base.update({"kind": "choice", "mode": "fill", "prompt": "Complete the sentence", "q": blanked, "options": opts})
        return base
    tiles, end = _sentence_tiles(item, p)
    base.update({"kind": "sentence", "mode": ex_type, "tiles": tiles, "end": end,
                 "hint": p.get("hint") if ex_type == "arrange" else None,
                 "speak": p["s"] if ex_type == "listen" else None})
    return base


# ---------- lesson endpoints ----------
class LessonStartIn(BaseModel):
    track: str
    level: str = "Junior"
    idx: int = 0
    voice: bool = True
    username: Optional[str] = None

class LessonGoalIn(BaseModel):
    goal: int

@app.get("/lessons/path")
def lessons_path(track: str = "sentence", level: str = "Junior", user=Depends(_game_user), db=Depends(get_db)):
    if track not in LESSON_TRACKS:
        raise HTTPException(404, "Unknown track")
    level = level if level in GAME_LEVELS else "Junior"
    units = _track_units(track, level)
    total = len(units) * LESSONS_PER_UNIT
    done = int(_progress_row(db, user.id, track, level).done or 0)
    row = _streak_row(db, user.id)
    db.commit()
    out = []
    for u, (title, icon, _x) in enumerate(units):
        lessons = []
        for k in range(LESSONS_PER_UNIT):
            g = u * LESSONS_PER_UNIT + k
            lessons.append({"idx": g, "state": "done" if g < done else ("current" if g == done else "locked")})
        out.append({"n": u + 1, "title": title, "icon": icon, "color": UNIT_COLORS[u % len(UNIT_COLORS)], "lessons": lessons})
    return {"track": track, "title": LESSON_TRACKS[track], "level": level, "done": done, "total": total,
            "units": out, "streak": _streak_view(row), "coins": int(user.coins or 0), "freeze_cost": FREEZE_COST}

@app.post("/lessons/start")
def lessons_start(data: LessonStartIn, background_tasks: BackgroundTasks, user=Depends(_game_user), db=Depends(get_db)):
    if data.track not in LESSON_TRACKS:
        raise HTTPException(404, "Unknown track")
    level = data.level if data.level in GAME_LEVELS else "Junior"
    loc = _lesson_locate(data.track, level, data.idx)
    if not loc:
        raise HTTPException(400, "That lesson does not exist")
    done = int(_progress_row(db, user.id, data.track, level).done or 0)
    if data.idx > done:
        raise HTTPException(403, "Finish the earlier lessons first")
    u, _k = loc
    title, icon, spec = _track_units(data.track, level)[u]
    game = "sentence" if data.track == "sentence" else "maths"
    cfg = GAME_CFG[game]
    state = {"tries": {}, "requeued": [], "over": False, "hearts": cfg["hearts"],
             "lesson": {"track": data.track, "level": level, "idx": data.idx}}
    questions, ids = [], []
    if data.track == "sentence":
        chosen = _gm_pick(db, user.id, "sentence", level, list(spec))
        if not chosen:
            _gm_generate("sentence", level)
            chosen = _gm_pick(db, user.id, "sentence", level, list(spec))
            if not chosen:
                raise HTTPException(503, "No sentences yet. Please try again in a moment.")
        background_tasks.add_task(_gm_topup, "sentence", level)
        state["ex"] = {}
        for i, it in enumerate(chosen):
            p = json.loads(it.payload)
            t = SENT_PATTERN[i % len(SENT_PATTERN)]
            if t == "listen" and not data.voice:
                t = "arrange"
            if t == "fill" and not p.get("f"):
                t = "arrange"
            q = _lesson_sentence_public(it, p, t)
            q["idx"] = i
            questions.append(q)
            ids.append(it.id)
            state["ex"][str(i)] = {"t": t, "a": p["f"]["w"] if t == "fill" else None}
    else:
        tier = 0 if u < 2 else (1 if u < 5 else 2)
        used, state["m"] = set(), {}
        for i in range(cfg["n"]):
            q_text, ans, alt, why = None, None, None, None
            for _ in range(12):
                q_text, ans, alt, why = MATH_GENS[random.choice(spec)]()
                if q_text not in used:
                    break
            used.add(q_text)
            kind = MATH_PATTERN[i % len(MATH_PATTERN)]
            q = {"idx": i, "tier": tier, "secs": None, "kind": kind, "mode": "math", "q": q_text}
            if kind == "choice":
                q["options"] = _math_options(ans, alt)
            questions.append(q)
            ids.append(i)
            state["m"][str(i)] = {"a": ans, "q": q_text, "why": why, "tier": tier, "o": i}
    state["n0"] = len(questions)
    now = _time.time()
    sid = _uuid.uuid4().hex
    db.add(GameSession(id=sid, user_id=user.id, game=game, level=level, item_ids=json.dumps(ids), answers="{}",
                       state=json.dumps(state, ensure_ascii=False), points=0, correct=0, streak=0, started_ts=now,
                       last_ts=now, finished=False, coins=0))
    db.commit()
    return {"session_id": sid, "game": game, "level": level, "n": len(questions), "hearts": cfg["hearts"],
            "lesson": {"track": data.track, "idx": data.idx, "unit": u + 1, "unit_title": title, "icon": icon},
            "questions": questions}

@app.post("/lessons/freeze")
def lessons_freeze(user=Depends(_game_user), db=Depends(get_db)):
    row = _streak_row(db, user.id)
    if int(row.freezes or 0) >= MAX_FREEZES:
        raise HTTPException(400, f"You can hold at most {MAX_FREEZES} streak freezes.")
    if int(user.coins or 0) < FREEZE_COST:
        raise HTTPException(400, f"You need {FREEZE_COST} coins for a streak freeze.")
    user.coins = int(user.coins or 0) - FREEZE_COST
    row.freezes = int(row.freezes or 0) + 1
    db.commit()
    return {"freezes": int(row.freezes), "coins": int(user.coins)}

@app.post("/lessons/goal")
def lessons_goal(data: LessonGoalIn, user=Depends(_game_user), db=Depends(get_db)):
    if data.goal not in (10, 20, 30, 50):
        raise HTTPException(400, "Choose 10, 20, 30 or 50 XP")
    row = _streak_row(db, user.id)
    row.goal = data.goal
    db.commit()
    return {"goal": row.goal}

def _lesson_finish(db, user, s, state, result):
    """Called by /games/finish: unlocks the next lesson, updates the streak and daily goal."""
    lesson = state.get("lesson")
    if not lesson:
        return result
    completed = not state.get("over")
    track, level, idx = lesson["track"], lesson["level"], int(lesson["idx"])
    row = _progress_row(db, user.id, track, level)
    total = len(_track_units(track, level)) * LESSONS_PER_UNIT
    unlocked = False
    if completed and idx >= int(row.done or 0):
        row.done = idx + 1
        unlocked = True
    srow = _streak_row(db, user.id)
    before = 0
    if completed:
        before = _streak_add(srow, int(result.get("points") or 0))
    view = _streak_view(srow)
    result["lesson"] = {"track": track, "idx": idx, "completed": completed, "unlocked_next": unlocked and idx + 1 < total,
                        "path_done": int(row.done or 0) >= total}
    result["streak"] = view
    result["daily"] = {"xp_today": view["xp_today"], "goal": view["goal"],
                       "reached_now": completed and before < view["goal"] <= view["xp_today"]}
    return result

def _math_answer(db, s, user, cfg, ids, answers, state, data):
    key = str(data.idx)
    m = (state.get("m") or {}).get(key)
    if not m:
        raise HTTPException(404, "Question not found")
    ok = _math_check(m["a"], data.choice)
    streak = (s.streak or 0) + 1 if ok else 0
    tier = int(m.get("tier", 0))
    points = _gm_points(cfg, tier, None, 0, False, streak) if ok else 0
    hearts, out, requeue_idx = state.get("hearts"), False, None
    if not ok:
        hearts = max(0, int(hearts if hearts is not None else cfg["hearts"]) - 1)
        state["hearts"] = hearts
        if hearts == 0:
            state["over"] = True
            out = True
    orig = m.get("o", data.idx)
    if not ok and not out and orig not in (state.get("requeued") or []):
        requeue_idx = len(ids)
        ids.append(orig)
        state.setdefault("requeued", []).append(orig)
        state["m"][str(requeue_idx)] = dict(m)
        s.item_ids = json.dumps(ids)
    ans = m["a"]
    res = {"idx": data.idx, "final": True, "correct": bool(ok), "timed_out": False,
           "correct_answer": str(ans), "why": m.get("why"), "points": points, "streak": streak,
           "total_points": int(s.points or 0) + points, "hearts": hearts, "out_of_hearts": out,
           "requeue_idx": requeue_idx}
    answers[key] = res
    s.answers = json.dumps(answers, ensure_ascii=False)
    s.state = json.dumps(state, ensure_ascii=False)
    s.points = res["total_points"]
    s.correct = int(s.correct or 0) + (1 if ok else 0)
    s.streak = streak
    s.last_ts = _time.time()
    db.commit()
    return res

print("[LESSONS] Lesson paths, streaks and data-driven games ready [OK]")


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
    frontend to render alongside the AI explanation.

    Free tier: 1 theory lesson total, for life (any topic) - same "once for
    life" pattern as the Campus GST mock test. The AI explanation itself is
    generated once per topic and cached on the Topic row, so the 2nd, 100th,
    or 10,000th student to open this same topic gets the saved version
    instantly instead of triggering another AI call."""
    topic = db.query(Topic).filter(Topic.id == topic_id).first()
    if not topic: raise HTTPException(404, "Topic not found")
    subject = db.query(Subject).filter(Subject.id == topic.subject_id).first()
    subject_name = subject.name if subject else "General"
    level         = subject.level if subject else "SSS"

    user = None
    if username:
        user = db.query(User).filter(User.username == username).first()
        if ENFORCE_FREE_TIER_LIMITS and user and not is_premium_active(user.is_premium, user.premium_expires) and user.used_free_theory_lesson:
            raise HTTPException(
                status_code=402,
                detail="You've used your free theory lesson. Upgrade to unlock unlimited lessons.",
            )

    if topic.cached_explanation:
        explanation = topic.cached_explanation
    else:
        if topic.admin_notes:
            prompt = f"""You are Ox-Bridge AI Tutor for Nigerian {level} students studying {subject_name}.
            Topic: '{topic.title}'.
            The teacher has provided this specific question/note to build the lesson around:
            \"{topic.admin_notes}\"
            Write a clear theory breakdown: (1) simple explanation of the concept,
            (2) how it relates to the teacher's note above, (3) one worked example,
            (4) a short summary. Use Nigerian WAEC/JAMB/NECO exam style.
            Whenever you write ANY mathematical expression, chemical formula, chemical
            equation, or physics equation, always wrap it in $...$ for inline math or
            $$...$$ for a standalone equation - this applies to every subject, not
            just Mathematics. For example: chemistry equations like
            $2H_2 + O_2 \rightarrow 2H_2O$, physics equations like $F = ma$ or
            $E = mc^2$, and any subscript/superscript like $H_2O$ or $x^2$ - never
            write these in plain text or with ASCII symbols like ^ or _ instead.
            Do this silently, without ever mentioning the word "LaTeX" or explaining
            the notation. Reminder: wrap every math, chemistry, or physics
            expression in $ signs, even a simple one.
            Only use a markdown table when you are genuinely comparing several
            items side by side across shared columns (e.g. comparing properties
            of several elements, contrasting two theories). For a normal
            explanation, worked example, or summary, write it as plain
            paragraphs, numbered steps, or a bullet list instead - do not force
            single explanations or single examples into a table just because
            it looks structured."""
        else:
            prompt = f"""You are Ox-Bridge AI Tutor for Nigerian {level} students studying {subject_name}.
            Topic: '{topic.title}'.
            Write a clear theory breakdown: (1) simple explanation of the concept,
            (2) one worked example, (3) a short summary a student can revise from.
            Use Nigerian WAEC/JAMB/NECO exam style.
            Whenever you write ANY mathematical expression, chemical formula, chemical
            equation, or physics equation, always wrap it in $...$ for inline math or
            $$...$$ for a standalone equation - this applies to every subject, not
            just Mathematics. For example: chemistry equations like
            $2H_2 + O_2 \rightarrow 2H_2O$, physics equations like $F = ma$ or
            $E = mc^2$, and any subscript/superscript like $H_2O$ or $x^2$ - never
            write these in plain text or with ASCII symbols like ^ or _ instead.
            Do this silently, without ever mentioning the word "LaTeX" or explaining
            the notation. Reminder: wrap every math, chemistry, or physics
            expression in $ signs, even a simple one.
            Only use a markdown table when you are genuinely comparing several
            items side by side across shared columns (e.g. comparing properties
            of several elements, contrasting two theories). For a normal
            explanation, worked example, or summary, write it as plain
            paragraphs, numbered steps, or a bullet list instead - do not force
            single explanations or single examples into a table just because
            it looks structured."""

        explanation = get_ai_response(prompt)
        topic.cached_explanation = explanation

    if user:
        user.last_learned_topic = topic.title
        if not is_premium_active(user.is_premium, user.premium_expires):
            user.used_free_theory_lesson = True
        db.commit()
    else:
        db.commit()  # persist the cached_explanation write even with no logged-in user

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
         {"model": "openai/gpt-oss-120b", "messages": [{"role": "user", "content": "Say OK"}]}),
        ("deepseek", DEEPSEEK_KEY, "https://api.deepseek.com/v1/chat/completions",
         {"model": "deepseek-v4-flash", "messages": [{"role": "user", "content": "Say OK"}]}),
        ("mistral", MISTRAL_KEY, "https://api.mistral.ai/v1/chat/completions",
         {"model": "open-mistral-nemo", "messages": [{"role": "user", "content": "Say OK"}]}),
        ("cohere", COHERE_KEY, "https://api.cohere.ai/v1/chat",
         {"model": "command-a-03-2025", "message": "Say OK"}),
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
                "https://router.huggingface.co/v1/chat/completions",
                headers={"Authorization": f"Bearer {HF_KEY}", "Content-Type": "application/json"},
                json={"model": "meta-llama/Llama-3.1-8B-Instruct:novita", "messages": [{"role": "user", "content": "Say OK"}]},
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
    """AI generates a detailed explanation for a theory question.

    Free tier: 1 WAEC/NECO theory explanation total, for life. The AI
    explanation is generated once per question and cached, so every
    student after the first gets the saved version instead of another
    AI call for the same question."""
    q = db.query(WaecTheoryQuestion).filter(WaecTheoryQuestion.id == question_id).first()
    if not q:
        raise HTTPException(status_code=404, detail="Question not found")

    user = None
    if username:
        user = db.query(User).filter(User.username == username).first()
        if ENFORCE_FREE_TIER_LIMITS and user and not is_premium_active(user.is_premium, user.premium_expires) and user.used_free_waec_theory:
            raise HTTPException(
                status_code=402,
                detail="You've used your free WAEC/NECO theory explanation. Upgrade to unlock unlimited explanations.",
            )

    if q.cached_explanation:
        explanation = q.cached_explanation
    else:
        model_answer_line = ("Model Answer provided:\n" + q.model_answer) if q.model_answer else "No model answer provided - generate a comprehensive answer."
        prompt = f"""You are an expert Ox-Bridge AI Tutor for Nigerian {q.exam_type} students.

{MATH_NOTATION_INSTRUCTION}

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

Write in simple, clear English suitable for Nigerian SS3 students preparing for {q.exam_type}.

Only use a markdown table when genuinely comparing several items side by
side across shared columns. For the explanation, model answer, and
revision tip, write plain paragraphs, numbered steps, or a bullet list
instead - do not force this into a table just because it looks structured."""

        explanation = get_ai_response(prompt)
        q.cached_explanation = explanation

    if user and not is_premium_active(user.is_premium, user.premium_expires):
        user.used_free_waec_theory = True
    db.commit()

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

            # Heartbeat from the frontend (sent every 25s to keep Render's
            # connection alive) - reply with pong and skip broadcasting it
            # as a chat message. Previously unhandled, this silently spammed
            # every room with a blank "Student:" message every 25 seconds.
            if data.get("type") == "ping":
                await websocket.send_json({"type": "pong"})
                continue

            try:
                msg  = data.get("message", "")
                user = data.get("username", "Student")
                for conn in active_connections[room]:
                    await conn.send_json({"type": "chat", "username": user, "message": msg})
                if msg.startswith("/ai"):
                    query    = msg.replace("/ai", "").strip()
                    response = get_ai_response(f"You are Ox-Bridge AI Tutor for Nigerian students. Answer clearly: {query}")
                    for conn in active_connections[room]:
                        await conn.send_json({"type": "ai", "username": " Ox-Bridge Tutor", "message": response})
            except Exception as e:
                # A single bad message or a failed AI call should never kill
                # the whole connection - previously any error here (not just
                # a real disconnect) fell through uncaught and dropped the
                # student's connection, which looked identical to a genuine
                # disconnect on the frontend.
                print(f"[CLASSROOM WS ERROR] room={room}: {e}")
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
    # These already exist in the live database via the migrations below but
    # were never declared here - every existing campus endpoint reads them
    # with raw SQL instead, so this gap was never actually triggered, but
    # closing it now (same class of bug found and fixed on User/Topic/
    # WaecTheoryQuestion) prevents it from silently biting future code.
    is_premium            = Column(Boolean, default=False)
    premium_expires       = Column(String,  nullable=True)
    premium_plan          = Column(String,  nullable=True)
    ai_requests_today     = Column(Integer, default=0)
    ai_requests_date      = Column(String,  nullable=True)
    email                 = Column(String,  nullable=True)
    profile_pic           = Column(String,  nullable=True)
    vision_requests_today = Column(Integer, default=0)
    vision_requests_date  = Column(String,  nullable=True)


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
        "ALTER TABLE campus_users ADD COLUMN IF NOT EXISTS vision_requests_today INTEGER DEFAULT 0;",
        "ALTER TABLE campus_users ADD COLUMN IF NOT EXISTS vision_requests_date VARCHAR;",
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


@app.get("/ai/check-vision-limit/{username}")
def ai_check_vision_limit(username: str, app_type: str = Query(...), db=Depends(get_db)):
    """
    Image/file analysis stays capped even in free-launch mode, since
    this is the one feature with a real per-use cost (Gemini/OpenRouter
    vision calls) behind it, unlike everything else which is free to run.
    Premium AI plan still bypasses this, for whenever payment is re-enabled.
    """
    table = premium_table_for(app_type)
    row = db.execute(
        text(f"SELECT is_premium, premium_plan, vision_requests_today, vision_requests_date FROM {table} WHERE username = :username"),
        {"username": username},
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="User not found")

    is_premium, premium_plan, requests_today, requests_date = row
    today = today_str()
    if requests_date != today:
        requests_today = 0

    vision_unlocked = is_premium and (app_type != "campus" or (premium_plan and "ai" in premium_plan))
    limit = 999999 if vision_unlocked else MAX_FREE_VISION_PER_DAY
    can_send = (requests_today or 0) < limit

    return {"can_send": can_send, "requests_today": requests_today or 0, "limit": limit}


@app.post("/ai/increment-vision-request")
def ai_increment_vision_request(payload: AiRequestCheckSchema, db=Depends(get_db)):
    table = premium_table_for(payload.app_type)
    today = today_str()

    row = db.execute(
        text(f"SELECT vision_requests_today, vision_requests_date FROM {table} WHERE username = :username"),
        {"username": payload.username},
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="User not found")

    requests_today, requests_date = row
    new_count = 1 if requests_date != today else (requests_today or 0) + 1

    db.execute(
        text(f"""
            UPDATE {table}
            SET vision_requests_today = :count, vision_requests_date = :today
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
    # level now matches loosely (like department already did) - a student's
    # profile stores level as a bare "200", but data entered separately
    # (e.g. via Colab) may have used "200L" or "200 Level" etc, which an
    # exact match would silently reject with zero results.
    rows = db.query(CampusCourseQuestion.course_code, CampusCourseQuestion.course_title).filter(
        CampusCourseQuestion.department.ilike(f"%{department.strip()}%"),
        CampusCourseQuestion.level.ilike(f"%{level.strip()}%"),
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


MAX_IMAGE_SIZE_MB = 8
MAX_FILE_SIZE_MB = 20

@app.post("/upload/image")
async def upload_image(file: UploadFile = File(...)):
    if not CLOUDINARY_SDK_AVAILABLE:
        raise HTTPException(status_code=500, detail="Image upload is not available - the cloudinary package is not installed on the server")
    if not CLOUDINARY_CONFIGURED:
        raise HTTPException(status_code=500, detail="Image upload is not configured - CLOUDINARY_URL is missing")

    contents = await file.read()
    size_mb = len(contents) / (1024 * 1024)
    if size_mb > MAX_IMAGE_SIZE_MB:
        raise HTTPException(status_code=413, detail=f"Image is too large ({size_mb:.1f}MB). Max allowed is {MAX_IMAGE_SIZE_MB}MB.")
    await file.seek(0)

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
    msgs = _filter_expired(msgs, conversation_id, db)
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
    msgs = _filter_expired(msgs, conversation_id, db)
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

    contents = await file.read()
    size_mb = len(contents) / (1024 * 1024)
    if size_mb > MAX_FILE_SIZE_MB:
        raise HTTPException(status_code=413, detail=f"File is too large ({size_mb:.1f}MB). Max allowed is {MAX_FILE_SIZE_MB}MB.")
    await file.seek(0)

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


# ============================================================
# POST-UTME PAST QUESTIONS MODULE
# Deliberately a SEPARATE table from CampusCourseQuestion, even
# though the shape is similar. Post-UTME candidates aren't
# necessarily registered students with a department/level yet
# (they're applying, not admitted), and mixing this into the same
# table as real department course questions risks Post-UTME
# content accidentally surfacing inside a current student's
# actual course practice test (or vice versa) if department/level
# tags ever overlap or get mistagged. Full isolation avoids that
# entirely.
# ============================================================

class PostUtmeQuestion(Base):
    __tablename__ = "postutme_questions"
    id             = Column(Integer, primary_key=True, index=True)
    subject        = Column(String, index=True)   # e.g. "English Language", "Use of English"
    year           = Column(Integer, nullable=True, index=True)
    question_text  = Column(String)
    option_a       = Column(String)
    option_b       = Column(String)
    option_c       = Column(String)
    option_d       = Column(String)
    correct_answer = Column(String)
    explanation    = Column(String, nullable=True)
    university     = Column(String, default="AKSU")

class PostUtmeResult(Base):
    __tablename__ = "postutme_results"
    id          = Column(Integer, primary_key=True, index=True)
    username    = Column(String, index=True)
    subject     = Column(String)
    score       = Column(Integer)
    total       = Column(Integer)
    percentage  = Column(Float)
    time_taken  = Column(Integer, nullable=True)
    taken_at    = Column(String)


try:
    Base.metadata.create_all(bind=engine, checkfirst=True)
    print("[DB] Post-UTME tables created/verified [OK]")
except Exception as e:
    print(f"[DB ERROR - postutme create_all] {e}")


# ------------------------------------------------------------
# ADMIN: add Post-UTME questions
# ------------------------------------------------------------
class PostUtmeQuestionCreate(BaseModel):
    subject: str
    year: Optional[int] = None
    question_text: str
    option_a: str
    option_b: str
    option_c: str
    option_d: str
    correct_answer: str
    explanation: Optional[str] = None
    university: Optional[str] = "AKSU"

class BulkPostUtmeQuestionsCreate(BaseModel):
    questions: List[PostUtmeQuestionCreate]


@app.post("/admin/campus/add-postutme-question")
def admin_add_postutme_question(data: PostUtmeQuestionCreate, db=Depends(get_db), admin_ok: bool = Depends(verify_admin)):
    q = PostUtmeQuestion(
        subject=data.subject.strip(), year=data.year,
        question_text=data.question_text.strip(),
        option_a=data.option_a.strip(), option_b=data.option_b.strip(),
        option_c=data.option_c.strip(), option_d=data.option_d.strip(),
        correct_answer=data.correct_answer.upper().strip(),
        explanation=data.explanation, university=data.university or "AKSU",
    )
    db.add(q); db.commit(); db.refresh(q)
    return {"msg": "Question added", "id": q.id, "subject": q.subject}


@app.post("/admin/campus/add-postutme-questions-bulk")
def admin_add_postutme_questions_bulk(data: BulkPostUtmeQuestionsCreate, db=Depends(get_db), admin_ok: bool = Depends(verify_admin)):
    added = []
    for qd in data.questions:
        q = PostUtmeQuestion(
            subject=qd.subject.strip(), year=qd.year,
            question_text=qd.question_text.strip(),
            option_a=qd.option_a.strip(), option_b=qd.option_b.strip(),
            option_c=qd.option_c.strip(), option_d=qd.option_d.strip(),
            correct_answer=qd.correct_answer.upper().strip(),
            explanation=qd.explanation, university=qd.university or "AKSU",
        )
        db.add(q); added.append({"subject": q.subject})
    db.commit()
    return {"msg": f"{len(added)} questions added", "questions": added}


# ------------------------------------------------------------
# STUDENT-FACING: browse subjects, take a mock test, submit
# ------------------------------------------------------------
@app.get("/campus/postutme/subjects")
def campus_postutme_subjects(db=Depends(get_db)):
    rows = db.query(PostUtmeQuestion.subject).distinct().all()
    if not rows:
        return {"subjects": ["Use of English", "Current Affairs", "General Knowledge"]}
    return {"subjects": [r[0] for r in rows]}


@app.get("/campus/postutme/questions/{subject}")
def campus_postutme_questions(subject: str, username: Optional[str] = Query(None), db=Depends(get_db)):
    # Same free-tier model as General Course Test / department courses:
    # 1 free test for life, unlimited if premium_plan contains "gst"
    # (the same "Mock Test Premium" tier covers all three features).
    if username:
        row = db.execute(
            text("SELECT is_premium, premium_plan FROM campus_users WHERE username = :username"),
            {"username": username},
        ).fetchone()
        if row:
            is_premium, premium_plan = row
            unlocked = is_premium and premium_plan and "gst" in premium_plan
            if not unlocked:
                attempts_total = db.query(PostUtmeResult).filter(
                    PostUtmeResult.username == username,
                ).count()
                if attempts_total >= 1:
                    raise HTTPException(
                        status_code=402,
                        detail="You've used your free Post-UTME practice test. Upgrade to unlock unlimited practice.",
                    )

    questions = db.query(PostUtmeQuestion).filter(PostUtmeQuestion.subject == subject).all()
    if not questions:
        raise HTTPException(status_code=404, detail="No questions found for this subject")

    random.shuffle(questions)
    selected = questions[:30]

    return [
        {
            "id": q.id, "subject": q.subject,
            "question_text": q.question_text,
            "option_a": q.option_a, "option_b": q.option_b,
            "option_c": q.option_c, "option_d": q.option_d,
        }
        for q in selected
    ]


class PostUtmeSubmitSchema(BaseModel):
    username: str
    subject: str
    answers: dict
    time_taken: Optional[int] = None


@app.post("/campus/postutme/submit")
def campus_postutme_submit(payload: PostUtmeSubmitSchema, db=Depends(get_db)):
    question_ids = [int(qid) for qid in payload.answers.keys()]
    questions = db.query(PostUtmeQuestion).filter(PostUtmeQuestion.id.in_(question_ids)).all()
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
            "question_id": q.id, "question_text": q.question_text,
            "chosen_answer": chosen, "correct_answer": q.correct_answer,
            "is_correct": is_correct, "explanation": q.explanation,
        })

    percentage = round((score / total) * 100, 2) if total > 0 else 0.0

    result = PostUtmeResult(
        username=payload.username, subject=payload.subject,
        score=score, total=total, percentage=percentage,
        time_taken=payload.time_taken, taken_at=now_str(),
    )
    db.add(result)
    db.commit()

    return {"success": True, "score": score, "total": total, "percentage": percentage, "results": breakdown}


# ============================================================
# DIAGNOSTIC: check exactly what department/level values exist
# in campus_course_questions vs campus_users, to catch mismatches
# like "200" vs "200L" vs "200 Level" at a glance.
# ============================================================
@app.get("/admin/campus/diagnose-course-data")
def admin_diagnose_course_data(db=Depends(get_db), admin_ok: bool = Depends(verify_admin)):
    question_combos = db.execute(
        text("SELECT DISTINCT department, level FROM campus_course_questions ORDER BY department, level")
    ).fetchall()
    user_combos = db.execute(
        text("SELECT DISTINCT department, level FROM campus_users ORDER BY department, level")
    ).fetchall()
    return {
        "department_level_combos_in_questions": [{"department": r[0], "level": r[1]} for r in question_combos],
        "department_level_combos_in_users": [{"department": r[0], "level": r[1]} for r in user_combos],
    }


# ============================================================
# DISAPPEARING MESSAGES (per-conversation setting, like WhatsApp)
# Since there's no background job scheduler in this app, expiry
# is enforced "lazily" at read time - expired messages are simply
# excluded from history results rather than deleted immediately.
# Functionally identical to the student, and avoids needing a
# separate worker process.
# ============================================================

class ConversationSettings(Base):
    __tablename__ = "conversation_settings"
    conversation_id     = Column(String, primary_key=True, index=True)
    disappearing_seconds = Column(Integer, nullable=True)   # null/0 = off
    set_by              = Column(String, nullable=True)
    updated_at          = Column(String, nullable=True)

try:
    Base.metadata.create_all(bind=engine, checkfirst=True)
    print("[DB] Conversation settings table created/verified [OK]")
except Exception as e:
    print(f"[DB ERROR - conversation settings create_all] {e}")


class DisappearingSettingSchema(BaseModel):
    username: str
    duration_seconds: int   # 0 = off, else e.g. 86400 (24h), 604800 (7 days), 7776000 (90 days)


@app.post("/community/conversations/{conversation_id}/disappearing")
def set_disappearing_messages(conversation_id: str, payload: DisappearingSettingSchema, db=Depends(get_db)):
    row = db.query(ConversationSettings).filter(ConversationSettings.conversation_id == conversation_id).first()
    if row:
        row.disappearing_seconds = payload.duration_seconds or None
        row.set_by = payload.username
        row.updated_at = now_str()
    else:
        db.add(ConversationSettings(
            conversation_id=conversation_id,
            disappearing_seconds=payload.duration_seconds or None,
            set_by=payload.username, updated_at=now_str(),
        ))
    db.commit()
    return {"success": True, "disappearing_seconds": payload.duration_seconds or None}


@app.get("/community/conversations/{conversation_id}/settings")
def get_conversation_settings(conversation_id: str, db=Depends(get_db)):
    row = db.query(ConversationSettings).filter(ConversationSettings.conversation_id == conversation_id).first()
    return {"disappearing_seconds": row.disappearing_seconds if row else None}


def _filter_expired(messages, conversation_id: str, db):
    """Drops messages older than the conversation's disappearing-messages window, if one is set."""
    settings = db.query(ConversationSettings).filter(ConversationSettings.conversation_id == conversation_id).first()
    if not settings or not settings.disappearing_seconds:
        return messages
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=settings.disappearing_seconds)
    kept = []
    for m in messages:
        try:
            created = datetime.fromisoformat(m.created_at.replace("Z", "+00:00"))
            if created >= cutoff:
                kept.append(m)
        except Exception:
            kept.append(m)   # if the timestamp can't be parsed, don't accidentally hide it
    return kept


# ============================================================
# OX-BRIDGEGPT - general-purpose assistant (coding, reasoning,
# research, writing), separate from the exam-tutor AI above.
# Supports real multi-conversation history (unlike the tutor's
# single continuous thread), automatic image generation via
# Pollinations (genuinely free, no API key), and reuses the
# existing vision + web search + 7-provider text pipeline.
# ============================================================

class GptConversation(Base):
    __tablename__ = "gpt_conversations"
    id         = Column(Integer, primary_key=True, index=True)
    username   = Column(String, index=True)
    title      = Column(String, default="New Chat")
    created_at = Column(String)
    updated_at = Column(String, index=True)

class GptMessage(Base):
    __tablename__ = "gpt_messages"
    id              = Column(Integer, primary_key=True, index=True)
    conversation_id = Column(Integer, ForeignKey("gpt_conversations.id"), index=True)
    role            = Column(String)              # "user" or "assistant"
    message_text    = Column(String, nullable=True)
    image_url       = Column(String, nullable=True)
    message_type    = Column(String, default="text")   # text / image
    created_at      = Column(String)

try:
    Base.metadata.create_all(bind=engine, checkfirst=True)
    print("[DB] OX-BridgeGPT tables created/verified [OK]")
except Exception as e:
    print(f"[DB ERROR - gpt create_all] {e}")


def wants_image_generation(message: str) -> bool:
    """Heuristic for detecting an image-generation request, separate from
    needs_web_search - things like 'draw me', 'generate an image of'."""
    triggers = [
        "generate an image", "generate a picture", "draw me", "draw a",
        "create an image", "create a picture", "make an image", "make a picture",
        "paint me", "paint a", "picture of", "image of", "illustration of",
        "design a logo", "generate a logo"
    ]
    msg_lower = message.lower()
    return any(t in msg_lower for t in triggers)


def generate_pollinations_image_url(prompt: str) -> str:
    """
    Builds a Pollinations image URL - no API key needed, no server-side
    fetch required, the URL itself IS the image (an <img src> pointed at
    this URL will render it directly). Anonymous tier is rate-limited to
    roughly 1 request/15s across all users of this app; registering a
    free Pollinations account later removes that limit and the watermark
    if this becomes a bottleneck.
    """
    encoded = requests.utils.quote(prompt)
    seed = random.randint(1, 999999)
    return f"https://image.pollinations.ai/prompt/{encoded}?width=768&height=768&nologo=true&seed={seed}"


GPT_IDENTITY_INSTRUCTION = """
You are OX-BridgeGPT, a general-purpose AI assistant built by OX-Bridge
Technology - you help with coding, reasoning, research, writing, analysis,
and general knowledge questions, not just academic tutoring.
If asked who made you, who owns you, or which company or AI is behind you,
say you were built by OX-Bridge Technology. Do not mention, name, or discuss
which underlying AI providers, models, or companies power your responses
behind the scenes - that is an internal implementation detail, not something
to share with users.

This app is not on the Google Play Store or Apple App Store yet - it's a
web app that installs straight from the browser (a PWA), which still puts
a real icon on the home screen and opens full-screen like a normal app.
If asked how to install it, download it, or why it isn't on Play Store,
walk them through it for their device:
- Android (Chrome): tap the three-dot menu in the top right, then tap
  "Install app" or "Add to Home screen".
- iPhone/iPad (Safari): tap the Share icon (the square with an arrow
  pointing up), scroll down, then tap "Add to Home Screen".
- Desktop (Chrome/Edge): look for an install icon (a small screen with a
  down arrow) at the right side of the address bar, and click it.
Only explain this when asked - do not bring it up unprompted.
"""

MATH_NOTATION_INSTRUCTION = """
Whenever you write ANY mathematical expression, chemical formula, chemical
equation, or physics equation, always wrap it in $...$ for inline math or
$$...$$ for a standalone equation - this applies to every subject. For
example: chemistry equations like $2H_2 + O_2 \\rightarrow 2H_2O$, physics
equations like $F = ma$ or $E = mc^2$, and any subscript/superscript like
$H_2O$ or $x^2$ - never write these in plain text or with ASCII symbols like
^ or _ instead. Do this silently, without ever mentioning the word "LaTeX"
or explaining the notation.
"""

def subject_lane_for_course(course_subject: str) -> str:
    """
    Same idea as detect_query_subject, but for endpoints that already know
    the subject explicitly (the student picked it from a dropdown) rather
    than having to guess from free-text - more reliable than keyword
    matching when this information is already available.
    """
    s = (course_subject or "").lower()
    if any(k in s for k in ["computer", "ict", "data processing", "programming"]):
        return "code"
    if any(k in s for k in ["math", "physics", "chemistry"]):
        return "math"
    return "general"

def detect_query_subject(message: str) -> str:
    """
    Classifies a message into a subject lane so a sharper, domain-specific
    instruction can be injected into the prompt. This deliberately does NOT
    change which AI provider answers (see the note on get_ai_response above -
    reordering providers by subject was tried and made things worse); this
    only changes what we ask the provider to do, which is the lever that
    actually helped last time.
    """
    msg = message.lower()
    code_triggers = [
        "code", "function", "python", "javascript", "java", "html", "css",
        "sql", "bug", "debug", "algorithm", "compile", "syntax error",
        "def", "class", "variable", "array", "for loop", "while loop",
        "api", "regex", "json", "```", "c++", "c#", "typescript", "react",
        "node.js", "endpoint", "database query"
    ]
    math_triggers = [
        "solve", "equation", "calculate", "derivative", "integral", "matrix",
        "algebra", "geometry", "trigonometry", "theorem", "prove", "proof",
        "factorize", "factorise", "simplify", "quadratic", "probability",
        "statistics", "vector", "logarithm", "differentiate", "integrate"
    ]
    # Word-boundary matching, not plain substring - a bare "in" check would
    # match "api" inside "capital" or "css" inside "process", for example.
    # Symbol-only tokens (```, c++, c#) have no word characters for \b to
    # anchor on, so those fall back to a plain substring check instead.
    def matches(triggers):
        for t in triggers:
            if re.search(r'[a-z]', t):
                if re.search(r'\b' + re.escape(t) + r'\b', msg):
                    return True
            elif t in msg:
                return True
        return False
    if matches(code_triggers):
        return "code"
    if matches(math_triggers):
        return "math"
    return "general"

SUBJECT_INSTRUCTIONS = {
    "code": """
You're answering a coding question. Write clean, correct, well-commented
code. Always specify the language on your code fences (e.g. ```python).
Briefly explain your approach before the code, then walk through what it
does afterward. Point out edge cases or gotchas if relevant. Prefer working,
runnable code over pseudocode unless pseudocode is specifically asked for.
""",
    "math": """
You're answering a math question. Show your work step-by-step instead of
jumping straight to the answer - state the method you're using, work
through each step, and clearly state the final answer at the end. Double
check your arithmetic before presenting the final result.
""",
    "general": """
Answer clearly and directly. Structure longer answers with short paragraphs
or bullet points where that aids readability. Stay accurate, and note when
you're uncertain about something instead of guessing confidently.
""",
}


class GptConversationCreateSchema(BaseModel):
    username: str

class GptChatSchema(BaseModel):
    conversation_id: int
    username: str
    message: str
    image_url: Optional[str] = None
    client_datetime: Optional[str] = None


@app.post("/gpt/conversations/create")
def gpt_create_conversation(payload: GptConversationCreateSchema, db=Depends(get_db)):
    convo = GptConversation(
        username=payload.username, title="New Chat",
        created_at=now_str(), updated_at=now_str(),
    )
    db.add(convo); db.commit(); db.refresh(convo)
    return {"conversation_id": convo.id, "title": convo.title}


@app.get("/gpt/conversations/{username}")
def gpt_list_conversations(username: str, db=Depends(get_db)):
    convos = db.query(GptConversation).filter(GptConversation.username == username) \
        .order_by(GptConversation.updated_at.desc()).all()
    return [{"id": c.id, "title": c.title, "updated_at": c.updated_at} for c in convos]


@app.get("/gpt/conversations/{conversation_id}/messages")
def gpt_get_messages(conversation_id: int, db=Depends(get_db)):
    msgs = db.query(GptMessage).filter(GptMessage.conversation_id == conversation_id) \
        .order_by(GptMessage.id.asc()).all()
    return [
        {"role": m.role, "message_text": m.message_text, "image_url": m.image_url,
         "message_type": m.message_type, "created_at": m.created_at}
        for m in msgs
    ]


@app.delete("/gpt/conversations/{conversation_id}")
def gpt_delete_conversation(conversation_id: int, db=Depends(get_db)):
    db.query(GptMessage).filter(GptMessage.conversation_id == conversation_id).delete()
    db.query(GptConversation).filter(GptConversation.id == conversation_id).delete()
    db.commit()
    return {"success": True}


@app.post("/gpt/chat")
def gpt_chat(data: GptChatSchema, db=Depends(get_db)):
    convo = db.query(GptConversation).filter(GptConversation.id == data.conversation_id).first()
    if not convo:
        raise HTTPException(status_code=404, detail="Conversation not found")

    db.add(GptMessage(
        conversation_id=data.conversation_id, role="user",
        message_text=data.message, message_type="text", created_at=now_str(),
    ))

    # Auto-title the conversation from the first real message
    if convo.title == "New Chat":
        convo.title = (data.message[:40] + "...") if len(data.message) > 40 else data.message

    # Image generation branch
    if wants_image_generation(data.message) and not data.image_url:
        image_url = generate_pollinations_image_url(data.message)
        response_text = "Here's what I generated for you:"
        db.add(GptMessage(
            conversation_id=data.conversation_id, role="assistant",
            message_text=response_text, image_url=image_url,
            message_type="image", created_at=now_str(),
        ))
        convo.updated_at = now_str()
        db.commit()
        return {"response": response_text, "message_type": "image", "image_url": image_url,
                "conversation_id": data.conversation_id, "title": convo.title}

    search_sources = []

    # Vision branch (user attached an image to analyze)
    if data.image_url:
        prompt = f"""{GPT_IDENTITY_INSTRUCTION}
        {MATH_NOTATION_INSTRUCTION}
        The user shared an image or document and asked: {data.message}
        Look at it carefully and give a clear, helpful explanation.
        Reminder: wrap every math, chemistry, or physics expression in $ signs."""
        response_text = get_ai_vision_response(prompt, data.image_url)
    else:
        # Load recent history for context
        history = db.query(GptMessage).filter(GptMessage.conversation_id == data.conversation_id) \
            .order_by(GptMessage.id.desc()).limit(10).all()
        context = ""
        for h in reversed(history):
            role = "User" if h.role == "user" else "Assistant"
            context += f"{role}: {h.message_text}\n"

        search_context, search_sources = fetch_search_context(data.message) if needs_web_search(data.message) else ("", [])
        search_block = f"""
        Current web search results (real, up to date - use this for anything
        about recent events, dates, or current facts. Don't mention that you
        searched, just answer naturally):
        {search_context}
        """ if search_context else ""

        datetime_block = f"The current date and time is: {data.client_datetime}." if data.client_datetime else ""
        subject_block = SUBJECT_INSTRUCTIONS.get(detect_query_subject(data.message), "")

        prompt = f"""{GPT_IDENTITY_INSTRUCTION}
        {MATH_NOTATION_INSTRUCTION}
        {subject_block}
        {datetime_block}
        {search_block}
        Recent conversation:
        {context}

        User: {data.message}

        Reminder: wrap every math, chemistry, or physics expression in $ signs.

        Assistant:"""
        response_text = get_ai_response(prompt)

    db.add(GptMessage(
        conversation_id=data.conversation_id, role="assistant",
        message_text=response_text, message_type="text", created_at=now_str(),
    ))
    convo.updated_at = now_str()
    db.commit()

    return {"response": response_text, "message_type": "text",
            "conversation_id": data.conversation_id, "title": convo.title,
            "sources": search_sources}

# ============================================================
# PUSH NOTIFICATIONS — DAILY STREAK REMINDERS
# See push-notifications-spec.md for the full design doc. Needs
# VAPID_PUBLIC_KEY / VAPID_PRIVATE_KEY / VAPID_CLAIMS_EMAIL / CRON_SECRET_KEY
# set as environment variables before these actually work end to end -
# until then, the endpoints return a clear 500 instead of crashing.
# ============================================================

def _require_push_configured():
    if not PYWEBPUSH_AVAILABLE:
        raise HTTPException(status_code=500, detail="pywebpush is not installed on the server yet - add it to requirements.txt and redeploy.")
    if not VAPID_PUBLIC_KEY or not VAPID_PRIVATE_KEY:
        raise HTTPException(status_code=500, detail="Push notifications are not configured yet - VAPID_PUBLIC_KEY/VAPID_PRIVATE_KEY are missing.")

def send_push_to_user(db, username: str, title: str, body: str, url: str = "./index.html"):
    """
    Sends a push notification to every device a user has subscribed on
    (they could have enabled it on more than one phone/browser). Returns
    (sent_count, failed_count). Automatically removes subscriptions the
    push service reports as gone (expired, uninstalled, permission revoked)
    so they don't pile up and get retried forever.
    """
    subs = db.query(PushSubscription).filter(PushSubscription.username == username).all()
    sent, failed = 0, 0
    for sub in subs:
        try:
            webpush(
                subscription_info={
                    "endpoint": sub.endpoint,
                    "keys": {"p256dh": sub.p256dh_key, "auth": sub.auth_key}
                },
                data=json.dumps({"title": title, "body": body, "url": url}),
                vapid_private_key=VAPID_PRIVATE_KEY,
                vapid_claims={"sub": f"mailto:{VAPID_CLAIMS_EMAIL}"}
            )
            sent += 1
        except WebPushException as e:
            status = e.response.status_code if e.response is not None else None
            if status in (404, 410):
                # Subscription is gone (uninstalled, permission revoked, expired) - clean it up.
                db.delete(sub)
                db.commit()
            else:
                print(f"[PUSH ERROR] {username}: {e}")
            failed += 1
        except Exception as e:
            print(f"[PUSH ERROR] {username}: {e}")
            failed += 1
    return sent, failed

@app.post("/auth/forgot-password")
def forgot_password(data: ForgotPasswordSchema, db=Depends(get_db)):
    """
    Sends a 6-digit reset code to the email on file for this username.
    Always returns the same success message whether or not the username
    exists or has an email - this stops someone from using this endpoint
    to probe which usernames are registered.
    """
    generic_response = {"msg": "If that account exists and has an email on file, a reset code has been sent."}

    user = db.query(User).filter(User.username == data.username).first()
    if not user or not user.email:
        return generic_response

    code = f"{random.randint(0, 999999):06d}"
    now = datetime.now(timezone.utc)
    db.add(PasswordResetToken(
        username=data.username, code=code,
        created_at=now.isoformat(),
        expires_at=(now + timedelta(minutes=15)).isoformat(),
        used=False
    ))
    db.commit()

    send_email(
        user.email,
        "Your Ox-Bridge password reset code",
        f"Your password reset code is: {code}\n\nThis code expires in 15 minutes. If you didn't request this, you can safely ignore this email."
    )
    return generic_response

@app.post("/auth/reset-password")
def reset_password(data: ResetPasswordSchema, db=Depends(get_db)):
    """Verifies the 6-digit code and, if valid and unused and not expired, sets the new password."""
    user = db.query(User).filter(User.username == data.username).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    token = db.query(PasswordResetToken).filter(
        PasswordResetToken.username == data.username,
        PasswordResetToken.code == data.code,
        PasswordResetToken.used == False
    ).order_by(PasswordResetToken.id.desc()).first()

    if not token:
        raise HTTPException(status_code=400, detail="Invalid or already-used code")

    if datetime.now(timezone.utc) > datetime.fromisoformat(token.expires_at):
        raise HTTPException(status_code=400, detail="This code has expired - request a new one")

    if len(data.new_password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")

    user.hashed_password = hash_password(data.new_password)
    token.used = True
    db.commit()
    return {"msg": "Password reset successfully - you can now log in with your new password"}

@app.get("/quotes")
def get_quotes():
    """Returns the shared motivational quote pool - optional for the frontend to use."""
    return {"quotes": MOTIVATIONAL_QUOTES}

@app.get("/push/vapid-public-key")
def get_vapid_public_key():
    """Frontend calls this before subscribing - pushManager.subscribe() needs the public key."""
    if not VAPID_PUBLIC_KEY:
        raise HTTPException(status_code=500, detail="Push notifications are not configured yet.")
    return {"publicKey": VAPID_PUBLIC_KEY}

@app.post("/push/subscribe")
def push_subscribe(data: PushSubscribeSchema, db=Depends(get_db)):
    """
    Saves (or updates) a user's push subscription. A user can have more than
    one - e.g. they enabled reminders on both their phone and laptop - so
    this upserts by endpoint, not by username alone.
    """
    existing = db.query(PushSubscription).filter(PushSubscription.endpoint == data.endpoint).first()
    if existing:
        existing.username   = data.username
        existing.p256dh_key = data.keys.p256dh
        existing.auth_key   = data.keys.auth
    else:
        db.add(PushSubscription(
            username=data.username, endpoint=data.endpoint,
            p256dh_key=data.keys.p256dh, auth_key=data.keys.auth,
            created_at=now_str()
        ))
    db.commit()
    return {"msg": "Subscribed to reminders"}

@app.post("/push/unsubscribe")
def push_unsubscribe(data: PushUnsubscribeSchema, db=Depends(get_db)):
    """Removes all of a user's saved subscriptions - call this when they turn reminders off in settings."""
    db.query(PushSubscription).filter(PushSubscription.username == data.username).delete()
    db.commit()
    return {"msg": "Unsubscribed from reminders"}

@app.post("/push/test")
def push_test(data: PushTestSchema, db=Depends(get_db)):
    """
    Lets a logged-in user send themselves one test push, to confirm it
    actually arrives before trusting the daily cron job. No cron-key
    needed - they're only ever sending it to their own username.
    """
    _require_push_configured()
    user = db.query(User).filter(User.username == data.username).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    sent, failed = send_push_to_user(
        db, data.username,
        title="Test notification",
        body="If you can see this, push notifications are working correctly!",
    )
    if sent == 0:
        raise HTTPException(status_code=404, detail="No active push subscription found for this user - enable reminders first.")
    return {"msg": "Test push sent", "sent": sent, "failed": failed}

@app.post("/push/send-reminders", dependencies=[Depends(verify_cron)])
def push_send_reminders(db=Depends(get_db)):
    """
    Meant to be called once a day by a scheduled job (Render Cron or an
    external cron service), protected by the X-Cron-Key header. Sends a
    streak reminder to every user who hasn't studied today and has an
    active push subscription. Users with no subscription are silently
    skipped - they just haven't enabled reminders.
    """
    _require_push_configured()
    today = today_str()
    inactive_users = db.query(User).filter(User.last_study_date != today).all()

    total_sent, total_failed, users_notified = 0, 0, 0
    for user in inactive_users:
        has_sub = db.query(PushSubscription).filter(PushSubscription.username == user.username).first()
        if not has_sub:
            continue
        streak = user.study_streak or 0
        quote = random.choice(MOTIVATIONAL_QUOTES)
        if streak > 0:
            body = f"You're on a {streak}-day streak - study today to keep it alive. \"{quote}\""
        else:
            body = f"Jump back in for a quick lesson or quiz today. \"{quote}\""
        sent, failed = send_push_to_user(db, user.username, title="Don't lose your streak! 🔥", body=body)
        total_sent += sent
        total_failed += failed
        if sent > 0:
            users_notified += 1

    return {
        "msg": "Reminder run complete",
        "inactive_users_checked": len(inactive_users),
        "users_notified": users_notified,
        "pushes_sent": total_sent,
        "pushes_failed": total_failed,
    }
