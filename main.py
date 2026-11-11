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
TOKEN_EXPIRE_HOURS = 24 * 60  # 60 days (was 30, before that 72 hours) - users stay logged in; /validate-token also hands active users a fresh token (see v3 additions)

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
                json={"model": model, "messages": [{"role": "user", "content": prompt}], "max_tokens": 2500},
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
            username = username_from_body or request.query_params.get("username") or _v3_bearer_username(request)
            db = SessionLocal()
            db.add(ActivityLog(username=username, action=action, path=path, created_at=now_str()))
            db.commit()
            db.close()
        else:
            # v3: features that were never logged before (WAEC/NECO theory, past
            # questions, quizzes, lessons...). Separate, fail-silent helper.
            _v3_log_extra_activity(request, response, username_from_body)
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
    _v3_block_if_suspended("hub", user.username)

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
    _v3_block_if_suspended("hub", user.username, 401)
    resp = {
        "valid":    True,
        "username": user.username,
        "score":    user.progress_score or 0,
        "streak":   user.study_streak   or 0,
        "coins":    user.coins          or 0
    }
    # v3: sliding 60-day session - when under 30 days remain, hand back a fresh
    # token (extra field; the frontend should save it). Never breaks validation.
    try:
        fresh = _v3_maybe_refresh_token(data.token, user.username)
        if fresh:
            resp["new_token"] = fresh
    except Exception as e:
        print(f"[TOKEN REFRESH ERROR] {e}")
    return resp

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
def update_profile(username: str, data: ProfileUpdate, db=Depends(get_db), authorization: Optional[str] = Header(None)):
    tok_user = decode_token(authorization[7:].strip()) if authorization and authorization.lower().startswith("bearer ") else None
    if not tok_user or tok_user.lower() != (username or "").lower():
        raise HTTPException(403, "You can only change your own profile.")
    if data.profile_pic and not re.match(r"^https?://", data.profile_pic.strip()):
        raise HTTPException(400, "The profile photo must be a web link starting with https://")
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
def get_past_questions(exam_type: str, subject: str, year: int = None, limit: int = 20, shuffle: bool = True,
                       username: Optional[str] = None, db=Depends(get_db)):
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
    # Students choose how many questions they want (WAEC/NECO papers have 50-60). Default 20, maximum 100.
    limit = max(1, min(int(limit or 20), 100))
    if shuffle:
        query = query.order_by(sa_func.random())      # a different mix each time instead of always the first ones
    results = query.limit(limit).all()

    if results and user and not is_premium_active(user.is_premium, user.premium_expires):
        user.used_free_past_questions = True
        db.commit()

    return [{"id": q.id, "year": q.year, "question_text": q.question_text,
             "option_a": q.option_a, "option_b": q.option_b,
             "option_c": q.option_c, "option_d": q.option_d,
             "correct_answer": q.correct_answer, "explanation": q.explanation,
             "image_url": q.image_url, "has_image": bool(q.image_url)}
            for q in results]

@app.get("/past-questions/years/{exam_type}/{subject}")
def past_question_years(exam_type: str, subject: str, db=Depends(get_db)):
    """Which years have questions for this exam and subject, and how many each (newest first)."""
    rows = (db.query(PastQuestion.year, sa_func.count(PastQuestion.id))
              .filter(PastQuestion.exam_type.ilike(f"%{exam_type}%"), PastQuestion.subject.ilike(f"%{subject}%"))
              .group_by(PastQuestion.year).order_by(PastQuestion.year.desc()).all())
    return {"years": [{"year": y, "count": int(c)} for y, c in rows if y],
            "total": sum(int(c) for _, c in rows)}

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
    _v3_block_if_suspended("hub", user.username)
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

    if topic.cached_explanation and _v3_lesson_is_current(db, topic.id):
        explanation = topic.cached_explanation
    else:
        # v3: a complete, structured lesson for the topic (see _v3_theory_lesson_prompt)
        prompt = _v3_theory_lesson_prompt(level, subject_name, topic.title, topic.admin_notes)
        explanation = get_ai_response(prompt)
        if _v3_lesson_ok(explanation):
            topic.cached_explanation = explanation
            _v3_mark_lesson_version(db, topic.id)
        elif topic.cached_explanation:
            explanation = topic.cached_explanation      # AI is down right now: show the saved lesson, not an error

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
    _v3_block_if_suspended("campus", user.username)

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
    _v3_block_if_restricted("campus", payload.username)
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
    img_gone = (m.image_url == _V3_EXPIRED)
    return {
        "id": m.id, "sender_username": m.sender_username, "message_text": m.message_text,
        "image_url": None if img_gone else m.image_url, "file_url": m.file_url, "file_name": m.file_name,
        "voice_duration": m.voice_duration, "message_type": m.message_type,
        "is_edited": m.is_edited, "is_deleted": m.is_deleted, "created_at": m.created_at,
        "image_expired": img_gone,
        "image_expires_at": _v3_media_expiry(m.created_at, bool(m.image_url) and not img_gone),
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
    _v3_block_if_restricted("campus", payload.from_username)
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


# ############################################################
# ############################################################
# OX-BRIDGE v3 ADDITIONS
# Everything below is NEW. Nothing above was rewritten - the only
# existing lines touched are: TOKEN_EXPIRE_HOURS (60 days), the end of
# /validate-token (extra "new_token" field) and 3 lines of the activity
# middleware (Bearer-token username + a call to the new extra logger).
#
# Contents
#   1. Sliding 60-day login helpers
#   2. Activity logging for features that were never logged + `detail` column
#   3. Admin monitoring: user list, who-used, daily usage, trend, prune
#   4. Admin -> user messages (directions, greetings, Christmas, ...)
#   5. Live class, WhatsApp style (REST-first + resilient WebSocket)
#   6. Admin-only blog (loading page)
# ############################################################
# ############################################################
import asyncio as _asyncio
from starlette.concurrency import run_in_threadpool as _run_in_threadpool
from fastapi.responses import JSONResponse as _JSONResponse
from typing import Any as _Any
from sqlalchemy import literal_column as _literal_column, distinct as _sa_distinct, bindparam as _bindparam
from sqlalchemy.exc import IntegrityError as _V3IntegrityError


# ------------------------------------------------------------
# 1. SLIDING 60-DAY LOGIN
# ------------------------------------------------------------
def _v3_bearer_username(request) -> Optional[str]:
    """Username from an 'Authorization: Bearer <token>' header, or None. Never raises."""
    try:
        auth = request.headers.get("authorization") or ""
        if auth.lower().startswith("bearer "):
            return decode_token(auth[7:].strip())
    except Exception:
        pass
    return None


def _v3_maybe_refresh_token(token: str, username: str) -> Optional[str]:
    """If the token has less than 30 days left, return a brand-new 60-day token."""
    payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
    exp = payload.get("exp")
    if not exp:
        return None
    remaining = float(exp) - datetime.now(timezone.utc).timestamp()
    if remaining < 30 * 24 * 3600:
        return create_token(username)
    return None


# ------------------------------------------------------------
# 2. ACTIVITY LOGGING FOR FEATURES THAT WERE NEVER LOGGED
# ------------------------------------------------------------
# First match wins. These only run for paths the ORIGINAL logger ignores.
_V3_LOG_RULES = [
    ("/waec-theory/questions", "waec_theory"),
    ("/waec-theory/explain",   "waec_theory"),
    ("/past-questions/",       "past_questions"),
    ("/gpt/chat",              "gpt_chat"),
    ("/quiz/",                 "quiz_start"),
    ("/daily-challenge/submit", "daily_challenge_submit"),
    ("/study/log",             "study_logged"),
    ("/lessons/start",         "lesson_started"),
    ("/games/start",           "game_started"),
    ("/ai/study-plan",         "ai_study_plan"),
    ("/ai/check-answer",       "ai_check_answer"),
    ("/learn/",                "learn_topic"),
]
_V3_LOG_SKIP = ("/quiz/history", "/quiz/stats")
_V3_DETAIL_PARAMS = ("subject", "exam", "exam_type", "year", "level", "topic", "game", "track", "type", "category")

# Friendly feature -> activity actions (used by the admin "who used what" screens)
_V3_FEATURES = {
    "waec_theory":     ["waec_theory"],
    "past_questions":  ["past_questions"],
    "quiz":            ["quiz_start", "quiz_submit"],
    "theory_lessons":  ["theory_view", "learn_topic"],
    "ai_tutor":        ["ai_chat", "ai_study_plan", "ai_check_answer", "gpt_chat"],
    "games":           ["game_started", "game_played"],
    "lessons":         ["lesson_started"],
    "daily_challenge": ["daily_challenge_submit"],
    "study_log":       ["study_logged"],
    "live_class":      ["liveclass_message"],
    "web_search":      ["web_search"],
    "login":           ["login", "signup"],
    "payments":        ["payment_initiated", "payment_verified"],
}


def _v3_write_activity(username, action, path, detail):
    """Insert one activity row (with `detail` when that column exists). Never raises."""
    db = SessionLocal()
    try:
        try:
            db.execute(
                text("INSERT INTO activity_logs (username, action, path, created_at, detail) "
                     "VALUES (:u, :a, :p, :c, :d)"),
                {"u": username, "a": action, "p": path, "c": now_str(), "d": detail},
            )
            db.commit()
        except Exception:
            # `detail` column not there (yet) - fall back to the normal insert so logging never stops
            db.rollback()
            db.add(ActivityLog(username=username, action=action, path=path, created_at=now_str()))
            db.commit()
    except Exception as e:
        print(f"[V3 ACTIVITY LOG ERROR] {e}")
    finally:
        db.close()


def _v3_log_extra_activity(request, response, username_from_body=None):
    """Called by the activity middleware for paths it does not already log."""
    try:
        method = request.method
        if method not in ("GET", "POST"):
            return
        if getattr(response, "status_code", 200) >= 400:
            return  # don't count failed / forbidden requests as "usage"
        path = request.url.path
        action, tail = None, ""
        if path.startswith("/liveclass/"):
            if method == "POST" and path.endswith("/send"):
                action, tail = "liveclass_message", path[len("/liveclass/"):-len("/send")]
        else:
            if any(path.startswith(x) for x in _V3_LOG_SKIP):
                return
            for prefix, act in _V3_LOG_RULES:
                if path.startswith(prefix):
                    action, tail = act, path[len(prefix):]
                    break
        if not action:
            return
        detail = _v3_special_detail(action, path, request)
        if detail is None:
            parts = []
            if tail.strip("/"):
                parts.append(tail.strip("/")[:80])
            for k in _V3_DETAIL_PARAMS:
                v = request.query_params.get(k)
                if v:
                    parts.append(f"{k}={str(v)[:40]}")
            detail = (" | ".join(parts))[:200] or None
        username = username_from_body or request.query_params.get("username") or _v3_bearer_username(request)
        _v3_write_activity(username, action, path, detail)
    except Exception as e:
        print(f"[V3 EXTRA LOG ERROR] {e}")


def _v3_since(days: int) -> str:
    days = max(1, min(int(days or 7), 365))
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


# ------------------------------------------------------------
# NEW TABLES (all additive) + migrations
# ------------------------------------------------------------
class AdminMessage(Base):
    __tablename__ = "admin_messages"
    id              = Column(Integer, primary_key=True, index=True)
    title           = Column(String)
    body            = Column(Text)
    kind            = Column(String, default="announcement")   # direction/announcement/greeting/christmas/new_year/exam_tip/other
    target_username = Column(String, nullable=True, index=True)  # NULL = everyone
    pinned          = Column(Boolean, default=False)
    send_push       = Column(Boolean, default=False)
    push_sent       = Column(Boolean, default=False)
    scheduled_at    = Column(String, nullable=True)   # UTC ISO; NULL = visible immediately
    expires_at      = Column(String, nullable=True)
    created_by      = Column(String, default="admin")
    created_at      = Column(String, index=True)


class AdminMessageRead(Base):
    __tablename__ = "admin_message_reads"
    __table_args__ = (UniqueConstraint("message_id", "username", name="uq_admin_message_read"),)
    id         = Column(Integer, primary_key=True, index=True)
    message_id = Column(Integer, ForeignKey("admin_messages.id"), index=True)
    username   = Column(String, index=True)
    read_at    = Column(String)


class LiveClassMessage(Base):
    __tablename__ = "liveclass_messages"
    id              = Column(Integer, primary_key=True, index=True)
    room            = Column(String, index=True)
    sender_username = Column(String, index=True)
    message_text    = Column(Text, nullable=True)
    image_url       = Column(String, nullable=True)
    file_url        = Column(String, nullable=True)
    file_name       = Column(String, nullable=True)
    message_type    = Column(String, default="text")   # text / image / file / ai / admin
    reply_to_id     = Column(Integer, nullable=True)
    is_edited       = Column(Boolean, default=False)
    edited_at       = Column(String, nullable=True)
    is_deleted      = Column(Boolean, default=False)
    created_at      = Column(String, index=True)


class BlogPost(Base):
    __tablename__ = "blog_posts"
    id              = Column(Integer, primary_key=True, index=True)
    title           = Column(String)
    excerpt         = Column(String, nullable=True)
    body            = Column(Text)
    cover_image_url = Column(String, nullable=True)
    category        = Column(String, default="education", index=True)  # top_students/education/announcement/news/tip
    is_pinned       = Column(Boolean, default=False)
    is_published    = Column(Boolean, default=True)
    extra_json      = Column(Text, nullable=True)      # e.g. list of top-student cards
    views           = Column(Integer, default=0)
    author          = Column(String, default="OX-Bridge Team")
    created_at      = Column(String, index=True)
    updated_at      = Column(String, nullable=True)


try:
    Base.metadata.create_all(bind=engine, checkfirst=True)
    print("[DB] v3 tables (admin messages, live class, blog) created/verified [OK]")
except Exception as e:
    print(f"[DB ERROR - v3 create_all] {e}")


def _v3_run_migrations():
    """One statement per transaction so a single failure can never poison the rest."""
    statements = [
        "ALTER TABLE activity_logs ADD COLUMN IF NOT EXISTS detail VARCHAR;",
        "ALTER TABLE topics ADD COLUMN IF NOT EXISTS lesson_version INTEGER;",
        "CREATE INDEX IF NOT EXISTS ix_activity_logs_action_created ON activity_logs (action, created_at);",
        "CREATE INDEX IF NOT EXISTS ix_activity_logs_lower_username ON activity_logs (LOWER(username));",
    ]
    for sql in statements:
        try:
            with engine.begin() as conn:
                conn.execute(text(sql))
        except Exception as e:
            print(f"[V3 MIGRATION SKIPPED] {sql[:60]}... -> {e}")
    print("[DB] v3 migrations complete [OK]")

_v3_run_migrations()


# ------------------------------------------------------------
# 3. ADMIN MONITORING (new endpoints - the old three are untouched)
# ------------------------------------------------------------
_V3_DAY_COL = _literal_column("substr(activity_logs.created_at, 1, 10)")


@app.get("/admin/monitor/features")
def v3_monitor_features(admin_ok: bool = Depends(verify_admin)):
    """The feature names you can pass to /admin/monitor/who-used?feature=..."""
    return {"features": _V3_FEATURES}


@app.get("/admin/users")
def v3_admin_users(search: str = "", page: int = 1, limit: int = 50, sort: str = "recent",
                   db=Depends(get_db), admin_ok: bool = Depends(verify_admin)):
    """
    Every OX-Bridge user with last-active time and total actions.
    sort: recent (last active) | newest (latest signup) | username | score | coins | streak | activity
    """
    page = max(1, page)
    limit = min(max(1, limit), 200)

    stats = db.query(
        sa_func.lower(ActivityLog.username).label("uname"),
        sa_func.max(ActivityLog.created_at).label("last_active"),
        sa_func.min(ActivityLog.created_at).label("first_seen"),
        sa_func.count(ActivityLog.id).label("total_actions"),
    ).filter(ActivityLog.username.isnot(None)).group_by(sa_func.lower(ActivityLog.username)).subquery()

    cond = None
    if search and search.strip():
        like = f"%{search.strip()}%"
        cond = (User.username.ilike(like)) | (User.full_name.ilike(like)) | (User.email.ilike(like))

    total_q = db.query(sa_func.count(User.id))
    if cond is not None:
        total_q = total_q.filter(cond)
    total = total_q.scalar() or 0

    q = db.query(User, stats.c.last_active, stats.c.first_seen, stats.c.total_actions) \
          .outerjoin(stats, stats.c.uname == sa_func.lower(User.username))
    if cond is not None:
        q = q.filter(cond)

    orders = {
        "recent":   [stats.c.last_active.desc().nullslast()],
        "newest":   [User.id.desc()],
        "username": [User.username.asc()],
        "score":    [User.progress_score.desc().nullslast()],
        "coins":    [User.coins.desc().nullslast()],
        "streak":   [User.study_streak.desc().nullslast()],
        "activity": [stats.c.total_actions.desc().nullslast()],
    }
    q = q.order_by(*orders.get(sort, orders["recent"]), User.id.desc())
    rows = q.offset((page - 1) * limit).limit(limit).all()

    restr = _v3_restrictions_for("hub", [u.username for u, _l, _f, _t in rows])
    users = []
    for u, last_active, first_seen, total_actions in rows:
        users.append({
            "restriction": restr.get((u.username or "").lower()),
            "id": u.id, "username": u.username, "full_name": u.full_name, "email": u.email,
            "is_premium": is_premium_active(bool(u.is_premium), u.premium_expires),
            "premium_plan": u.premium_plan, "premium_expires": u.premium_expires,
            "coins": u.coins or 0, "score": u.progress_score or 0, "streak": u.study_streak or 0,
            "last_study_date": u.last_study_date,
            "last_active": last_active, "first_seen": first_seen,
            "total_actions": int(total_actions or 0),
        })
    return {"total": total, "page": page, "limit": limit, "sort": sort, "users": users}


@app.get("/admin/monitor/who-used")
def v3_monitor_who_used(feature: str = "", action: str = "", days: int = 7, limit: int = 100,
                        db=Depends(get_db), admin_ok: bool = Depends(verify_admin)):
    """Who used a feature (WAEC/NECO theory, past questions, quiz, AI...) in the last N days."""
    actions = []
    if feature:
        if feature not in _V3_FEATURES:
            raise HTTPException(400, f"Unknown feature. Choose one of: {', '.join(_V3_FEATURES.keys())}")
        actions = list(_V3_FEATURES[feature])
    if action:
        actions = [action]
    if not actions:
        raise HTTPException(400, "Pass feature=... (see /admin/monitor/features) or action=...")
    limit = min(max(1, limit), 500)
    since = _v3_since(days)
    base = [ActivityLog.action.in_(actions), ActivityLog.created_at >= since]

    rows = db.query(
        sa_func.min(ActivityLog.username), sa_func.count(ActivityLog.id), sa_func.max(ActivityLog.created_at)
    ).filter(*base, ActivityLog.username.isnot(None)) \
     .group_by(sa_func.lower(ActivityLog.username)) \
     .order_by(sa_func.count(ActivityLog.id).desc()).limit(limit).all()

    unique_users = db.query(sa_func.count(_sa_distinct(sa_func.lower(ActivityLog.username)))) \
        .filter(*base, ActivityLog.username.isnot(None)).scalar() or 0
    total_uses = db.query(sa_func.count(ActivityLog.id)).filter(*base).scalar() or 0
    anonymous = db.query(sa_func.count(ActivityLog.id)).filter(*base, ActivityLog.username.is_(None)).scalar() or 0

    top_details = []
    try:
        stmt = text(
            "SELECT detail, COUNT(*) AS n FROM activity_logs "
            "WHERE action IN :acts AND created_at >= :since AND detail IS NOT NULL "
            "GROUP BY detail ORDER BY n DESC LIMIT 20"
        ).bindparams(_bindparam("acts", expanding=True))
        top_details = [{"detail": r[0], "count": int(r[1])}
                       for r in db.execute(stmt, {"acts": actions, "since": since}).fetchall()]
    except Exception:
        db.rollback()   # detail column not available - skip silently

    return {
        "feature": feature or None, "actions": actions, "days": max(1, min(int(days or 7), 365)),
        "unique_users": int(unique_users), "total_uses": int(total_uses), "anonymous_uses": int(anonymous),
        "users": [{"username": r[0], "uses": int(r[1]), "last_used": r[2]} for r in rows],
        "top_details": top_details,   # e.g. which exam/subject/topic is used most
    }


@app.get("/admin/monitor/daily")
def v3_monitor_daily(date: str = "", db=Depends(get_db), admin_ok: bool = Depends(verify_admin)):
    """Platform-wide usage for one day (default today, UTC): per-activity totals + most active users."""
    day = (date or "").strip() or today_str()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", day):
        raise HTTPException(400, "date must look like 2026-12-25")
    like = f"{day}%"
    by_action = db.query(ActivityLog.action, sa_func.count(ActivityLog.id)) \
        .filter(ActivityLog.created_at.like(like)).group_by(ActivityLog.action) \
        .order_by(sa_func.count(ActivityLog.id).desc()).all()
    active = db.query(sa_func.count(_sa_distinct(sa_func.lower(ActivityLog.username)))) \
        .filter(ActivityLog.created_at.like(like), ActivityLog.username.isnot(None)).scalar() or 0
    top = db.query(sa_func.min(ActivityLog.username), sa_func.count(ActivityLog.id)) \
        .filter(ActivityLog.created_at.like(like), ActivityLog.username.isnot(None)) \
        .group_by(sa_func.lower(ActivityLog.username)) \
        .order_by(sa_func.count(ActivityLog.id).desc()).limit(10).all()
    return {
        "date": day, "active_users": int(active),
        "total_actions": int(sum(n for _, n in by_action)),
        "by_action": {a: int(n) for a, n in by_action},
        "top_users": [{"username": u, "actions": int(n)} for u, n in top],
    }


@app.get("/admin/monitor/trend")
def v3_monitor_trend(days: int = 14, db=Depends(get_db), admin_ok: bool = Depends(verify_admin)):
    """Per-day active users + total actions for the last N days (for a chart)."""
    since = _v3_since(days)
    rows = db.query(
        _V3_DAY_COL.label("d"),
        sa_func.count(ActivityLog.id),
        sa_func.count(_sa_distinct(sa_func.lower(ActivityLog.username))),
    ).filter(ActivityLog.created_at >= since).group_by(_V3_DAY_COL).order_by(_V3_DAY_COL).all()
    return {"days": [{"date": r[0], "total_actions": int(r[1]), "active_users": int(r[2])} for r in rows]}


@app.get("/admin/monitor/daily/{username}")
def v3_monitor_user_daily(username: str, days: int = 30, db=Depends(get_db), admin_ok: bool = Depends(verify_admin)):
    """One user's usage per day, broken down by activity."""
    uname = (username or "").strip().lower()
    user = db.query(User).filter(sa_func.lower(User.username) == uname).first()
    if not user:
        raise HTTPException(404, "User not found")
    since = _v3_since(days)
    rows = db.query(_V3_DAY_COL.label("d"), ActivityLog.action, sa_func.count(ActivityLog.id)) \
        .filter(sa_func.lower(ActivityLog.username) == uname, ActivityLog.created_at >= since) \
        .group_by(_V3_DAY_COL, ActivityLog.action).order_by(_V3_DAY_COL.desc()).all()
    per_day: dict = {}
    for d, action, n in rows:
        entry = per_day.setdefault(d, {"date": d, "total": 0, "actions": {}})
        entry["actions"][action] = int(n)
        entry["total"] += int(n)
    last_active = db.query(sa_func.max(ActivityLog.created_at)) \
        .filter(sa_func.lower(ActivityLog.username) == uname).scalar()
    return {
        "username": user.username, "full_name": user.full_name,
        "coins": user.coins or 0, "score": user.progress_score or 0, "streak": user.study_streak or 0,
        "last_active": last_active,
        "days": sorted(per_day.values(), key=lambda x: x["date"], reverse=True),
    }


@app.get("/admin/monitor/activity/detailed")
def v3_monitor_activity_detailed(username: str = "", action: str = "", limit: int = 100,
                                 db=Depends(get_db), admin_ok: bool = Depends(verify_admin)):
    """Same as /admin/monitor/activity but each row also has `detail` (exam/subject/topic...)."""
    limit = min(max(1, limit), 500)
    sql = "SELECT id, username, action, path, detail, created_at FROM activity_logs WHERE 1=1"
    params: dict = {"lim": limit}
    if username:
        sql += " AND LOWER(username) = :u"
        params["u"] = username.strip().lower()
    if action:
        sql += " AND action = :a"
        params["a"] = action
    sql += " ORDER BY id DESC LIMIT :lim"
    try:
        rows = db.execute(text(sql), params).fetchall()
        return [{"id": r[0], "username": r[1], "action": r[2], "path": r[3], "detail": r[4], "created_at": r[5]}
                for r in rows]
    except Exception:
        db.rollback()
        q = db.query(ActivityLog)
        if username:
            q = q.filter(sa_func.lower(ActivityLog.username) == username.strip().lower())
        if action:
            q = q.filter(ActivityLog.action == action)
        logs = q.order_by(ActivityLog.id.desc()).limit(limit).all()
        return [{"id": l.id, "username": l.username, "action": l.action, "path": l.path,
                 "detail": None, "created_at": l.created_at} for l in logs]


@app.post("/admin/monitor/prune")
def v3_monitor_prune(days: int = 180, db=Depends(get_db), admin_ok: bool = Depends(verify_admin)):
    """Delete activity logs older than N days (minimum 30) so the Neon database doesn't fill up."""
    days = max(30, int(days or 180))
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    deleted = db.query(ActivityLog).filter(ActivityLog.created_at < cutoff).delete(synchronize_session=False)
    db.commit()
    return {"success": True, "deleted": int(deleted or 0), "older_than_days": days}


# ------------------------------------------------------------
# 4. ADMIN -> USER MESSAGES (directions, greetings, Christmas, ...)
# Separate from the old `notifications` table and from the campus
# notify system. Works for one user or everyone, can be scheduled,
# supports {name}, and can also send a push notification.
# ------------------------------------------------------------
_V3_MSG_KINDS = {"direction", "announcement", "greeting", "christmas", "new_year", "exam_tip", "other"}


class V3AdminMessageIn(BaseModel):
    title: str
    body: str
    kind: str = "announcement"
    username: Optional[str] = None        # omit/null = everyone
    pinned: bool = False
    send_push: bool = False
    scheduled_at: Optional[str] = None    # ISO time; no timezone given = Nigeria time (UTC+1)
    expires_at: Optional[str] = None


def _v3_parse_iso(value) -> Optional[str]:
    """Any ISO date/time -> UTC ISO string. Naive times are treated as Nigeria time (UTC+1)."""
    if not value or not str(value).strip():
        return None
    v = str(value).strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(v)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone(timedelta(hours=1)))
    return dt.astimezone(timezone.utc).isoformat()


def _v3_first_name(user) -> str:
    name = (getattr(user, "full_name", None) or "").strip()
    return name.split()[0] if name else user.username


def _v3_render(txt: str, user) -> str:
    return (txt or "").replace("{name}", _v3_first_name(user)).replace("{username}", user.username)


def _v3_msg_visible(username: str):
    """SQL condition: messages this user may see right now (addressed to them or everyone, due, not expired)."""
    now = now_str()
    return (
        ((AdminMessage.target_username.is_(None)) | (sa_func.lower(AdminMessage.target_username) == username.lower()))
        & ((AdminMessage.scheduled_at.is_(None)) | (AdminMessage.scheduled_at <= now))
        & ((AdminMessage.expires_at.is_(None)) | (AdminMessage.expires_at > now))
    )


def _v3_push_ready() -> bool:
    return bool(PYWEBPUSH_AVAILABLE and VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY)


def _v3_push_message_job(message_id: int):
    """Background job: push a message to its recipients. Marks it sent FIRST so it can never double-send."""
    if not _v3_push_ready():
        return
    db = SessionLocal()
    try:
        msg = db.query(AdminMessage).filter(AdminMessage.id == message_id).first()
        if not msg or msg.push_sent:
            return
        msg.push_sent = True
        db.commit()
        if msg.expires_at and msg.expires_at <= now_str():
            return
        if msg.target_username:
            names = [msg.target_username]
        else:
            names = [r[0] for r in db.query(PushSubscription.username).distinct().all() if r[0]]
        for uname in names[:5000]:
            try:
                u = db.query(User).filter(User.username == uname).first()
                if not u:
                    continue
                send_push_to_user(db, u.username, _v3_render(msg.title, u)[:80], _v3_render(msg.body, u)[:160])
            except Exception as e:
                print(f"[V3 PUSH ERROR] {uname}: {e}")
    except Exception as e:
        print(f"[V3 PUSH JOB ERROR] message {message_id}: {e}")
    finally:
        db.close()


def _v3_message_templates():
    quote = random.choice(MOTIVATIONAL_QUOTES) if MOTIVATIONAL_QUOTES else "Keep learning, keep growing."
    return [
        {"id": "christmas_wish", "kind": "christmas", "suggested_date": "12-25",
         "title": "Merry Christmas from OX-Bridge 🎄",
         "body": "Dear {name}, Merry Christmas! Thank you for learning with OX-Bridge this year. Rest well, celebrate with your loved ones, and come back ready to shine. 🎁"},
        {"id": "christmas_quote", "kind": "christmas", "suggested_date": "12-24",
         "title": "A Christmas thought for you 🎅",
         "body": "Hi {name}, education is the gift that keeps on giving. Enjoy the season, share the joy, and keep learning!"},
        {"id": "new_year", "kind": "new_year", "suggested_date": "01-01",
         "title": "Happy New Year, {name}! 🎉",
         "body": "New year, new goals! Set a daily study target and let's make this your best year yet. We're proud to learn with you."},
        {"id": "easter", "kind": "greeting", "suggested_date": "Easter",
         "title": "Happy Easter 🐣",
         "body": "Dear {name}, wishing you a blessed Easter with family and friends. Rest well and come back strong!"},
        {"id": "eid", "kind": "greeting", "suggested_date": "Eid",
         "title": "Eid Mubarak 🌙",
         "body": "Dear {name}, Eid Mubarak! Wishing you and your family joy, peace and blessings today."},
        {"id": "independence_day", "kind": "greeting", "suggested_date": "10-01",
         "title": "Happy Independence Day 🇳🇬",
         "body": "Happy Independence Day, {name}! Today we celebrate Nigeria, and the students who will build its future. Keep learning!"},
        {"id": "childrens_day", "kind": "greeting", "suggested_date": "05-27",
         "title": "Happy Children's Day 🎈",
         "body": "Happy Children's Day, {name}! Dream big and keep learning. The future belongs to you."},
        {"id": "teachers_day", "kind": "greeting", "suggested_date": "10-05",
         "title": "Happy Teachers' Day 🍎",
         "body": "Today we say thank you to every teacher who guides us. Thank a teacher today, {name}!"},
        {"id": "exam_season", "kind": "exam_tip", "suggested_date": "before exams",
         "title": "Exam season is close 📚",
         "body": "{name}, WAEC/NECO/JAMB season is near. Practise Past Questions and WAEC/NECO Theory daily, and revise your weak topics. You've got this!"},
        {"id": "how_to_use", "kind": "direction", "suggested_date": "anytime",
         "title": "Quick tip from OX-Bridge",
         "body": "Hi {name}, open Past Questions and WAEC/NECO Theory every day, then test yourself with a quiz. Small steps every day beat last-minute reading."},
        {"id": "quote_of_the_day", "kind": "greeting", "suggested_date": "daily",
         "title": "Quote of the day ✨",
         "body": "{name}, " + str(quote)},
    ]


@app.get("/admin/messages/templates")
def v3_admin_message_templates(admin_ok: bool = Depends(verify_admin)):
    """Ready-made greetings (Christmas, New Year, exams...). Edit them freely, then send."""
    return {"templates": _v3_message_templates()}


@app.post("/admin/messages/send")
def v3_admin_send_message(payload: V3AdminMessageIn, background_tasks: BackgroundTasks,
                          db=Depends(get_db), admin_ok: bool = Depends(verify_admin)):
    title = (payload.title or "").strip()
    body = (payload.body or "").strip()
    if not title or not body:
        raise HTTPException(400, "Title and message are required")
    if len(title) > 120 or len(body) > 2000:
        raise HTTPException(400, "Title max 120 characters, message max 2000 characters")

    target = None
    if payload.username and payload.username.strip():
        u = db.query(User).filter(sa_func.lower(User.username) == payload.username.strip().lower()).first()
        if not u:
            raise HTTPException(404, "That username does not exist")
        target = u.username

    try:
        sched = _v3_parse_iso(payload.scheduled_at)
        exp = _v3_parse_iso(payload.expires_at)
    except ValueError:
        raise HTTPException(400, "Dates must look like 2026-12-25T08:00:00 (Nigeria time unless you add a timezone)")

    now = now_str()
    future = bool(sched and sched > now)
    if exp and exp <= now:
        raise HTTPException(400, "expires_at is already in the past")

    kind = payload.kind if payload.kind in _V3_MSG_KINDS else "other"
    msg = AdminMessage(
        title=title, body=body, kind=kind, target_username=target,
        pinned=bool(payload.pinned), send_push=bool(payload.send_push), push_sent=False,
        scheduled_at=sched if future else None, expires_at=exp, created_by="admin", created_at=now,
    )
    db.add(msg)
    db.commit()
    db.refresh(msg)

    push_status = "off"
    if payload.send_push:
        if not _v3_push_ready():
            push_status = "not_configured"      # message is still saved and visible in-app
        elif future:
            push_status = "scheduled"           # sent automatically when the time arrives
        else:
            background_tasks.add_task(_v3_push_message_job, msg.id)
            push_status = "queued"
    return {
        "success": True, "id": msg.id, "target": target or "everyone",
        "delivered_now": not future, "scheduled_at": msg.scheduled_at, "push": push_status,
    }


@app.get("/admin/messages")
def v3_admin_list_messages(limit: int = 50, db=Depends(get_db), admin_ok: bool = Depends(verify_admin)):
    limit = min(max(1, limit), 200)
    msgs = db.query(AdminMessage).order_by(AdminMessage.id.desc()).limit(limit).all()
    ids = [m.id for m in msgs]
    reads = {}
    if ids:
        reads = {mid: n for mid, n in db.query(AdminMessageRead.message_id, sa_func.count(AdminMessageRead.id))
                 .filter(AdminMessageRead.message_id.in_(ids)).group_by(AdminMessageRead.message_id).all()}
    total_users = db.query(sa_func.count(User.id)).scalar() or 0
    return [{
        "id": m.id, "title": m.title, "body": m.body, "kind": m.kind,
        "target": m.target_username or "everyone", "pinned": bool(m.pinned),
        "send_push": bool(m.send_push), "push_sent": bool(m.push_sent),
        "scheduled_at": m.scheduled_at, "expires_at": m.expires_at, "created_at": m.created_at,
        "read_count": int(reads.get(m.id, 0)),
        "audience": 1 if m.target_username else int(total_users),
    } for m in msgs]


@app.delete("/admin/messages/{message_id}")
def v3_admin_delete_message(message_id: int, db=Depends(get_db), admin_ok: bool = Depends(verify_admin)):
    msg = db.query(AdminMessage).filter(AdminMessage.id == message_id).first()
    if not msg:
        raise HTTPException(404, "Message not found")
    db.query(AdminMessageRead).filter(AdminMessageRead.message_id == message_id).delete(synchronize_session=False)
    db.delete(msg)
    db.commit()
    return {"success": True}


@app.post("/admin/messages/process-scheduled", dependencies=[Depends(verify_cron)])
def v3_process_scheduled_messages(background_tasks: BackgroundTasks, db=Depends(get_db)):
    """Optional cron hook (X-Cron-Key). Scheduled pushes are ALSO picked up automatically whenever any user opens their inbox."""
    due = db.query(AdminMessage.id).filter(
        AdminMessage.send_push.is_(True), AdminMessage.push_sent.is_(False),
        AdminMessage.scheduled_at.isnot(None), AdminMessage.scheduled_at <= now_str(),
    ).limit(20).all()
    for (mid,) in due:
        background_tasks.add_task(_v3_push_message_job, mid)
    return {"queued": len(due), "push_configured": _v3_push_ready()}


# ---------- user side (Bearer token) ----------
@app.get("/messages/inbox")
def v3_inbox(background_tasks: BackgroundTasks, limit: int = 50,
             user=Depends(_game_user), db=Depends(get_db)):
    limit = min(max(1, limit), 200)
    vis = _v3_msg_visible(user.username)
    rows = db.query(AdminMessage).filter(vis).order_by(AdminMessage.id.desc()).limit(limit).all()
    ids = [m.id for m in rows]
    read_ids = set()
    if ids:
        read_ids = {r[0] for r in db.query(AdminMessageRead.message_id).filter(
            AdminMessageRead.username == user.username, AdminMessageRead.message_id.in_(ids)).all()}
    total_visible = db.query(sa_func.count(AdminMessage.id)).filter(vis).scalar() or 0
    total_read = db.query(sa_func.count(AdminMessageRead.id)) \
        .join(AdminMessage, AdminMessage.id == AdminMessageRead.message_id) \
        .filter(AdminMessageRead.username == user.username, vis).scalar() or 0

    # Scheduled pushes that are now due get sent the first time anyone opens the app after the time.
    try:
        due = db.query(AdminMessage.id).filter(
            AdminMessage.send_push.is_(True), AdminMessage.push_sent.is_(False),
            AdminMessage.scheduled_at.isnot(None), AdminMessage.scheduled_at <= now_str(),
        ).limit(3).all()
        for (mid,) in due:
            background_tasks.add_task(_v3_push_message_job, mid)
    except Exception:
        db.rollback()

    items = [{
        "id": m.id, "title": _v3_render(m.title, user), "body": _v3_render(m.body, user), "kind": m.kind,
        "pinned": bool(m.pinned), "is_read": m.id in read_ids,
        "created_at": m.scheduled_at or m.created_at, "from": "OX-Bridge Team",
    } for m in rows]
    items.sort(key=lambda x: x["created_at"] or "", reverse=True)
    return {"unread_count": max(0, int(total_visible) - int(total_read)), "messages": items}


@app.post("/messages/{message_id}/read")
def v3_inbox_mark_read(message_id: int, user=Depends(_game_user), db=Depends(get_db)):
    msg = db.query(AdminMessage).filter(AdminMessage.id == message_id, _v3_msg_visible(user.username)).first()
    if not msg:
        raise HTTPException(404, "Message not found")
    exists = db.query(AdminMessageRead.id).filter(
        AdminMessageRead.message_id == message_id, AdminMessageRead.username == user.username).first()
    if not exists:
        try:
            db.add(AdminMessageRead(message_id=message_id, username=user.username, read_at=now_str()))
            db.commit()
        except _V3IntegrityError:
            db.rollback()
    return {"success": True}


@app.post("/messages/read-all")
def v3_inbox_mark_all_read(user=Depends(_game_user), db=Depends(get_db)):
    vis = _v3_msg_visible(user.username)
    ids = [r[0] for r in db.query(AdminMessage.id).filter(vis).order_by(AdminMessage.id.desc()).limit(500).all()]
    if not ids:
        return {"success": True, "marked": 0}
    already = {r[0] for r in db.query(AdminMessageRead.message_id).filter(
        AdminMessageRead.username == user.username, AdminMessageRead.message_id.in_(ids)).all()}
    marked = 0
    for mid in ids:
        if mid not in already:
            db.add(AdminMessageRead(message_id=mid, username=user.username, read_at=now_str()))
            marked += 1
    try:
        db.commit()
    except _V3IntegrityError:
        db.rollback()
    return {"success": True, "marked": marked}


# ------------------------------------------------------------
# 5. LIVE CLASS - WHATSAPP STYLE
# Same proven pattern as the campus /community chat: REST saves the
# message FIRST, then pushes it to everyone connected over WebSocket.
# If the socket drops, the app keeps working by polling
# GET /liveclass/{room}/messages?after_id=<last id>.
# The old /ws/classroom/{room} route is left exactly as it was.
#
# Why the old one showed "disconnected": it called the (slow, blocking)
# AI function directly inside the async socket handler, which froze every
# connected student's socket for seconds; it also never removed dead
# sockets, so one broken connection could crash the join/leave broadcast.
# ------------------------------------------------------------
_lc_conns: dict = {}      # room -> {websocket: username}
_lc_seen: dict = {}       # room -> {username: last_seen_unix}  (people using REST/polling count as online)
_lc_recent: dict = {}     # username -> [unix times of recent messages] (flood control)
_lc_ai_last: dict = {}    # username -> unix time of last /ai question
_lc_bg_tasks: set = set()
_LC_HEARTBEAT_SEC = 20
_LC_MAX_TEXT = 2000


def _lc_clean_room(room: str) -> str:
    r = (room or "").strip()
    if not r or len(r) > 80 or any(ord(c) < 32 for c in r):
        raise HTTPException(400, "Invalid room name")
    return r


def _lc_touch(room: str, username: str):
    _lc_seen.setdefault(room, {})[username] = _time.time()


def _lc_online_users(room: str) -> list:
    now = _time.time()
    users = set(_lc_conns.get(room, {}).values())
    seen = _lc_seen.get(room, {})
    for u, ts in list(seen.items()):
        if now - ts < 45:
            users.add(u)
        elif now - ts > 600:
            seen.pop(u, None)
    return sorted(users)


def _lc_drop(room: str, ws):
    d = _lc_conns.get(room)
    if d is not None:
        d.pop(ws, None)
        if not d:
            _lc_conns.pop(room, None)


async def _lc_broadcast(room: str, payload: dict, skip=None):
    """Send to everyone in the room. A dead/slow socket is dropped - it can never break the others."""
    conns = [ws for ws in list(_lc_conns.get(room, {}).keys()) if ws is not skip]
    if not conns:
        return

    async def _one(ws):
        try:
            await _asyncio.wait_for(ws.send_json(payload), timeout=5)
        except Exception:
            _lc_drop(room, ws)

    await _asyncio.gather(*[_one(ws) for ws in conns], return_exceptions=True)


def _lc_rate_ok(username: str) -> bool:
    now = _time.time()
    recent = [t for t in _lc_recent.get(username, []) if now - t < 10]
    if len(recent) >= 8:
        _lc_recent[username] = recent
        return False
    recent.append(now)
    _lc_recent[username] = recent
    return True


def _lc_reply_map(db, rows) -> dict:
    ids = {m.reply_to_id for m in rows if m.reply_to_id}
    if not ids:
        return {}
    out = {}
    for r in db.query(LiveClassMessage).filter(LiveClassMessage.id.in_(ids)).all():
        if r.is_deleted:
            snippet = "This message was deleted"
        elif r.message_text:
            snippet = r.message_text[:80]
        elif r.image_url:
            snippet = "📷 Photo"
        else:
            snippet = "📎 File"
        out[r.id] = {"id": r.id, "sender_username": r.sender_username, "text": snippet}
    return out


def _lc_serialize(m, reply_map=None) -> dict:
    if m.is_deleted:
        return {
            "type": "message", "id": m.id, "room": m.room, "sender_username": m.sender_username,
            "message_text": "This message was deleted", "image_url": None, "file_url": None, "file_name": None,
            "message_type": "deleted", "reply_to_id": None, "reply_to": None,
            "is_edited": False, "is_deleted": True, "created_at": m.created_at,
        }
    img_gone = (m.image_url == _V3_EXPIRED)
    file_gone = (m.file_url == _V3_EXPIRED)
    return {
        "type": "message", "id": m.id, "room": m.room, "sender_username": m.sender_username,
        "message_text": m.message_text, "image_url": None if img_gone else m.image_url,
        "file_url": None if file_gone else m.file_url, "file_name": m.file_name,
        "message_type": m.message_type, "reply_to_id": m.reply_to_id,
        "reply_to": (reply_map or {}).get(m.reply_to_id) if m.reply_to_id else None,
        "is_edited": bool(m.is_edited), "is_deleted": False, "created_at": m.created_at,
        "image_expired": img_gone, "file_expired": file_gone,
        "image_expires_at": _v3_media_expiry(m.created_at, bool(m.image_url) and not img_gone),
    }


def _lc_save_system_message(room: str, sender: str, text_value: str, mtype: str,
                            reply_to_id=None, image_url=None) -> dict:
    """Save an AI/admin message and return its payload. Runs in a worker thread."""
    db = SessionLocal()
    try:
        m = LiveClassMessage(room=room, sender_username=sender, message_text=(text_value or "")[:4000] or None,
                             image_url=image_url, message_type=mtype, reply_to_id=reply_to_id, created_at=now_str())
        db.add(m)
        db.commit()
        db.refresh(m)
        return _lc_serialize(m, _lc_reply_map(db, [m]))
    finally:
        db.close()


def _lc_send_sync(username: str, room: str, text_value, image_url, file_url, file_name, reply_to_id):
    """Validate + save a student's message. Returns (payload, ai_query_or_None). Runs in a worker thread."""
    _lc_check_access(room, username)       # personal chats: only the two people in them
    _v3_block_if_restricted("hub", username)
    text_value = (text_value or "").strip()
    if not text_value and not image_url and not file_url:
        raise HTTPException(400, "Message must have text, an image, or a file")
    if len(text_value) > _LC_MAX_TEXT:
        raise HTTPException(400, f"Message too long (max {_LC_MAX_TEXT} characters)")
    if not _lc_rate_ok(username):
        raise HTTPException(429, "You are sending messages too fast. Slow down a little.")

    ai_query = None
    if text_value.lower().startswith("/ai"):
        ai_query = text_value[3:].strip()
        if not ai_query:
            raise HTTPException(400, "Type your question after /ai, e.g. /ai explain photosynthesis")
        now = _time.time()
        if now - _lc_ai_last.get(username, 0) < 8:
            raise HTTPException(429, "Wait a few seconds before asking the AI again.")
        _lc_ai_last[username] = now

    mtype = "image" if image_url else ("file" if file_url else "text")
    db = SessionLocal()
    try:
        if reply_to_id:
            ok = db.query(LiveClassMessage.id).filter(
                LiveClassMessage.id == reply_to_id, LiveClassMessage.room == room).first()
            if not ok:
                reply_to_id = None
        m = LiveClassMessage(room=room, sender_username=username, message_text=text_value or None,
                             image_url=image_url, file_url=file_url, file_name=file_name, message_type=mtype,
                             reply_to_id=reply_to_id or None, created_at=now_str())
        db.add(m)
        db.commit()
        db.refresh(m)
        return _lc_serialize(m, _lc_reply_map(db, [m])), ai_query
    finally:
        db.close()


async def _lc_ai_job(room: str, query: str, reply_to_id: int):
    """The AI answer is produced OFF the event loop, so it can never freeze anyone's connection."""
    try:
        prompt = f"You are Ox-Bridge AI Tutor for Nigerian students. Answer clearly: {query}"
        answer = await _run_in_threadpool(get_ai_response, prompt)
        payload = await _run_in_threadpool(_lc_save_system_message, room, "Ox-Bridge Tutor", answer, "ai", reply_to_id, None)
        await _lc_broadcast(room, payload)
    except Exception as e:
        print(f"[LIVECLASS AI ERROR] room={room}: {e}")


class V3LcSend(BaseModel):
    message_text: Optional[str] = None
    image_url: Optional[str] = None
    file_url: Optional[str] = None
    file_name: Optional[str] = None
    reply_to_id: Optional[int] = None
    notify_user: Optional[str] = None      # personal chats: who to send a phone notification to


class V3LcEdit(BaseModel):
    message_text: str


class V3LcAdminSend(BaseModel):
    message_text: Optional[str] = None
    image_url: Optional[str] = None


@app.post("/liveclass/{room}/send")
async def v3_lc_send(room: str, payload: V3LcSend, background_tasks: BackgroundTasks, user=Depends(_game_user)):
    room = _lc_clean_room(room)
    out, ai_query = await _run_in_threadpool(
        _lc_send_sync, user.username, room, payload.message_text,
        payload.image_url, payload.file_url, payload.file_name, payload.reply_to_id)
    _lc_touch(room, user.username)
    await _lc_broadcast(room, out)
    if ai_query:
        background_tasks.add_task(_lc_ai_job, room, ai_query, out["id"])
    if room.startswith("dm__") and payload.notify_user:
        snippet = out.get("message_text") or ("📷 Photo" if out.get("image_url") else "📎 File")
        background_tasks.add_task(_v3_dm_push_job, room, user.username, payload.notify_user.strip(), snippet)
    _v3_maybe_expire(background_tasks)
    return {"success": True, "message": out}


@app.get("/liveclass/{room}/messages")
def v3_lc_history(room: str, background_tasks: BackgroundTasks, after_id: int = 0, before_id: int = 0, limit: int = 50,
                  user=Depends(_game_user), db=Depends(get_db)):
    """
    No params      -> latest messages (oldest first).
    after_id=N     -> only messages newer than N (use this to poll when the socket is down).
    before_id=N    -> older messages (scroll up).
    """
    room = _lc_clean_room(room)
    _lc_check_access(room, user.username)
    limit = min(max(1, limit), 200)
    _lc_touch(room, user.username)
    _v3_maybe_expire(background_tasks)
    q = db.query(LiveClassMessage).filter(LiveClassMessage.room == room)
    if after_id and after_id > 0:
        rows = q.filter(LiveClassMessage.id > after_id).order_by(LiveClassMessage.id.asc()).limit(limit).all()
    elif before_id and before_id > 0:
        rows = q.filter(LiveClassMessage.id < before_id).order_by(LiveClassMessage.id.desc()).limit(limit).all()
        rows.reverse()
    else:
        rows = q.order_by(LiveClassMessage.id.desc()).limit(limit).all()
        rows.reverse()
    rmap = _lc_reply_map(db, rows)
    online = _lc_online_users(room)
    return {"room": room, "messages": [_lc_serialize(m, rmap) for m in rows],
            "online_count": len(online), "server_time": now_str()}


@app.get("/liveclass/{room}/online")
def v3_lc_online(room: str, user=Depends(_game_user)):
    room = _lc_clean_room(room)
    _lc_check_access(room, user.username)
    _lc_touch(room, user.username)
    users = _lc_online_users(room)
    return {"room": room, "count": len(users), "users": users}


@app.post("/liveclass/messages/{message_id}/edit")
async def v3_lc_edit(message_id: int, payload: V3LcEdit, user=Depends(_game_user), db=Depends(get_db)):
    m = db.query(LiveClassMessage).filter(LiveClassMessage.id == message_id).first()
    if not m:
        raise HTTPException(404, "Message not found")
    if m.sender_username != user.username:
        raise HTTPException(403, "You can only edit your own messages")
    if m.is_deleted or m.message_type != "text":
        raise HTTPException(400, "Only text messages can be edited")
    new_text = (payload.message_text or "").strip()
    if not new_text or len(new_text) > _LC_MAX_TEXT:
        raise HTTPException(400, f"Message must be 1-{_LC_MAX_TEXT} characters")
    m.message_text = new_text
    m.is_edited = True
    m.edited_at = now_str()
    db.commit()
    out = _lc_serialize(m, _lc_reply_map(db, [m]))
    out["type"] = "edit"
    await _lc_broadcast(m.room, out)
    return {"success": True, "message": out}


@app.post("/liveclass/messages/{message_id}/delete")
async def v3_lc_delete(message_id: int, user=Depends(_game_user), db=Depends(get_db)):
    m = db.query(LiveClassMessage).filter(LiveClassMessage.id == message_id).first()
    if not m:
        raise HTTPException(404, "Message not found")
    if m.sender_username != user.username:
        raise HTTPException(403, "You can only delete your own messages")
    m.is_deleted = True
    db.commit()
    await _lc_broadcast(m.room, {"type": "delete", "id": m.id, "room": m.room})
    return {"success": True}


# ---------- admin ----------
@app.post("/admin/liveclass/{room}/send")
async def v3_lc_admin_send(room: str, payload: V3LcAdminSend, admin_ok: bool = Depends(verify_admin)):
    """Teacher/admin announcement inside a live class room."""
    room = _lc_clean_room(room)
    txt = (payload.message_text or "").strip()
    if not txt and not payload.image_url:
        raise HTTPException(400, "Message must have text or an image")
    out = await _run_in_threadpool(_lc_save_system_message, room, "OX-Bridge Admin", txt, "admin", None, payload.image_url)
    await _lc_broadcast(room, out)
    return {"success": True, "message": out}


@app.delete("/admin/liveclass/messages/{message_id}")
async def v3_lc_admin_delete(message_id: int, db=Depends(get_db), admin_ok: bool = Depends(verify_admin)):
    m = db.query(LiveClassMessage).filter(LiveClassMessage.id == message_id).first()
    if not m:
        raise HTTPException(404, "Message not found")
    m.is_deleted = True
    db.commit()
    await _lc_broadcast(m.room, {"type": "delete", "id": m.id, "room": m.room})
    return {"success": True}


@app.get("/admin/liveclass/rooms")
def v3_lc_admin_rooms(db=Depends(get_db), admin_ok: bool = Depends(verify_admin)):
    """Every live-class room: message count, last activity, and how many students are online right now."""
    rows = db.query(LiveClassMessage.room, sa_func.count(LiveClassMessage.id), sa_func.max(LiveClassMessage.created_at)) \
        .group_by(LiveClassMessage.room).order_by(sa_func.max(LiveClassMessage.created_at).desc()).limit(200).all()
    known = set()
    out = []
    for room, n, last in rows:
        known.add(room)
        out.append({"room": room, "messages": int(n), "last_message_at": last, "online_now": len(_lc_online_users(room))})
    for room in list(_lc_conns.keys()):
        if room not in known:
            out.append({"room": room, "messages": 0, "last_message_at": None, "online_now": len(_lc_online_users(room))})
    return {"rooms": out}


# ---------- WebSocket ----------
def _v3_user_exists(username: str) -> bool:
    db = SessionLocal()
    try:
        return db.query(User.id).filter(User.username == username).first() is not None
    finally:
        db.close()


async def _lc_heartbeat(ws):
    """Server-driven keep-alive so Render / mobile networks don't silently drop an idle socket."""
    try:
        while True:
            await _asyncio.sleep(_LC_HEARTBEAT_SEC)
            await ws.send_json({"type": "ping"})
    except Exception:
        return


@app.websocket("/ws/liveclass/{room}")
async def v3_liveclass_ws(websocket: WebSocket, room: str, token: Optional[str] = Query(None)):
    """
    Connect with  wss://<host>/ws/liveclass/<room>?token=<login token>
    Server -> client events: ready, ping, pong, message, edit, delete, typing, presence, error
    Client -> server: {"type":"ping"} | {"type":"typing"} | {"type":"message","message_text":"..."}
    (messages can also be sent with POST /liveclass/{room}/send - that is the main, most reliable way)
    """
    await websocket.accept()
    username = decode_token(token) if token else None
    r = (room or "").strip()
    if not username or not r or len(r) > 80:
        try:
            await websocket.close(code=4401)
        except Exception:
            pass
        return
    try:
        if not await _run_in_threadpool(_v3_user_exists, username):
            await websocket.close(code=4401)
            return
    except Exception:
        try:
            await websocket.close(code=1011)
        except Exception:
            pass
        return

    room = r
    try:
        _lc_check_access(room, username)
    except HTTPException:
        try:
            await websocket.close(code=4403)     # private chat of two other students
        except Exception:
            pass
        return
    _r = _v3_restriction("hub", username)
    if _r and _r["status"] == "suspended":
        try:
            await websocket.close(code=4403)
        except Exception:
            pass
        return
    _lc_conns.setdefault(room, {})[websocket] = username
    _lc_touch(room, username)
    hb = None
    try:
        online = _lc_online_users(room)
        await websocket.send_json({"type": "ready", "room": room, "username": username,
                                   "online_count": len(online), "heartbeat_sec": _LC_HEARTBEAT_SEC})
        await _lc_broadcast(room, {"type": "presence", "event": "join", "username": username,
                                   "online_count": len(online)}, skip=websocket)
        hb = _asyncio.create_task(_lc_heartbeat(websocket))
        while True:
            raw = await websocket.receive_text()
            try:
                data = json.loads(raw)
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            kind = data.get("type")
            _lc_touch(room, username)
            if kind == "ping":
                await websocket.send_json({"type": "pong"})
            elif kind == "typing":
                await _lc_broadcast(room, {"type": "typing", "room": room, "username": username}, skip=websocket)
            elif kind == "message":
                try:
                    out, ai_query = await _run_in_threadpool(
                        _lc_send_sync, username, room, data.get("message_text"), data.get("image_url"),
                        data.get("file_url"), data.get("file_name"), data.get("reply_to_id"))
                    await _lc_broadcast(room, out)
                    if ai_query:
                        task = _asyncio.create_task(_lc_ai_job(room, ai_query, out["id"]))
                        _lc_bg_tasks.add(task)
                        task.add_done_callback(_lc_bg_tasks.discard)
                except HTTPException as he:
                    await websocket.send_json({"type": "error", "detail": he.detail})
    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"[LIVECLASS WS ERROR] room={room} user={username}: {e}")
    finally:
        if hb:
            hb.cancel()
        _lc_drop(room, websocket)
        try:
            await _lc_broadcast(room, {"type": "presence", "event": "leave", "username": username,
                                       "online_count": len(_lc_online_users(room))})
        except Exception:
            pass


# ------------------------------------------------------------
# 6. ADMIN-ONLY BLOG (for the loading page)
# Anyone can READ published posts (no login needed, so it works on the
# loading page). Only the admin (X-Admin-Key) can create/edit/delete.
# Images: upload with the existing POST /upload/image, then save the URL.
# ------------------------------------------------------------
_V3_BLOG_CATEGORIES = {"top_students", "education", "announcement", "news", "tip"}


class V3BlogIn(BaseModel):
    title: str
    body: str
    excerpt: Optional[str] = None
    cover_image_url: Optional[str] = None
    category: str = "education"
    is_pinned: bool = False
    is_published: bool = True
    extra: Optional[_Any] = None       # e.g. [{"name": "Ada", "score": 98, "photo": "https://..."}]


class V3BlogUpdate(BaseModel):
    title: Optional[str] = None
    body: Optional[str] = None
    excerpt: Optional[str] = None
    cover_image_url: Optional[str] = None
    category: Optional[str] = None
    is_pinned: Optional[bool] = None
    is_published: Optional[bool] = None
    extra: Optional[_Any] = None


class V3BlogToggle(BaseModel):
    value: Optional[bool] = None       # omit to flip the current value


def _v3_blog_out(p, full: bool = False) -> dict:
    extra = None
    if p.extra_json:
        try:
            extra = json.loads(p.extra_json)
        except Exception:
            extra = None
    excerpt = p.excerpt or ((p.body or "").strip().replace("\n", " ")[:160])
    out = {
        "id": p.id, "title": p.title, "excerpt": excerpt, "cover_image_url": p.cover_image_url,
        "category": p.category, "is_pinned": bool(p.is_pinned), "is_published": bool(p.is_published),
        "author": p.author, "views": p.views or 0, "extra": extra,
        "created_at": p.created_at, "updated_at": p.updated_at,
    }
    if full:
        out["body"] = p.body
    return out


def _v3_payload_dict(model, **kw) -> dict:
    fn = getattr(model, "model_dump", None) or model.dict
    return fn(**kw)


def _v3_clean_blog_fields(d: dict) -> dict:
    if "title" in d:
        d["title"] = (d["title"] or "").strip()
        if not d["title"] or len(d["title"]) > 200:
            raise HTTPException(400, "Title is required (max 200 characters)")
    if "body" in d:
        d["body"] = (d["body"] or "").strip()
        if not d["body"]:
            raise HTTPException(400, "Post body is required")
        if len(d["body"]) > 50000:
            raise HTTPException(400, "Post body is too long (max 50,000 characters)")
    if "excerpt" in d and d["excerpt"] is not None:
        d["excerpt"] = d["excerpt"].strip()[:300] or None
    if "category" in d and d["category"] not in _V3_BLOG_CATEGORIES:
        raise HTTPException(400, f"category must be one of: {', '.join(sorted(_V3_BLOG_CATEGORIES))}")
    return d


# ---------- public (no login) ----------
@app.get("/blog/posts")
def v3_blog_list(category: str = "", page: int = 1, limit: int = 10, db=Depends(get_db)):
    page = max(1, page)
    limit = min(max(1, limit), 50)
    q = db.query(BlogPost).filter(BlogPost.is_published.is_(True))
    if category:
        if category not in _V3_BLOG_CATEGORIES:
            raise HTTPException(400, f"category must be one of: {', '.join(sorted(_V3_BLOG_CATEGORIES))}")
        q = q.filter(BlogPost.category == category)
    total = q.count()
    rows = q.order_by(BlogPost.is_pinned.desc(), BlogPost.id.desc()).offset((page - 1) * limit).limit(limit).all()
    content = {"posts": [_v3_blog_out(p) for p in rows], "page": page, "limit": limit,
               "total": total, "has_more": page * limit < total}
    return _JSONResponse(content=content, headers={"Cache-Control": "public, max-age=60"})


@app.get("/blog/posts/{post_id}")
def v3_blog_get(post_id: int, db=Depends(get_db)):
    p = db.query(BlogPost).filter(BlogPost.id == post_id, BlogPost.is_published.is_(True)).first()
    if not p:
        raise HTTPException(404, "Post not found")
    try:
        db.query(BlogPost).filter(BlogPost.id == post_id).update(
            {BlogPost.views: sa_func.coalesce(BlogPost.views, 0) + 1}, synchronize_session=False)
        db.commit()
        db.refresh(p)
    except Exception:
        db.rollback()
    return _v3_blog_out(p, full=True)


# ---------- admin ----------
@app.get("/admin/blog/posts")
def v3_admin_blog_list(category: str = "", status: str = "all", page: int = 1, limit: int = 20,
                       db=Depends(get_db), admin_ok: bool = Depends(verify_admin)):
    """status: all | published | draft"""
    page = max(1, page)
    limit = min(max(1, limit), 100)
    q = db.query(BlogPost)
    if category:
        q = q.filter(BlogPost.category == category)
    if status == "published":
        q = q.filter(BlogPost.is_published.is_(True))
    elif status == "draft":
        q = q.filter(BlogPost.is_published.is_(False))
    total = q.count()
    rows = q.order_by(BlogPost.is_pinned.desc(), BlogPost.id.desc()).offset((page - 1) * limit).limit(limit).all()
    return {"posts": [_v3_blog_out(p, full=True) for p in rows], "page": page, "limit": limit,
            "total": total, "has_more": page * limit < total}


@app.post("/admin/blog/posts")
def v3_admin_blog_create(payload: V3BlogIn, db=Depends(get_db), admin_ok: bool = Depends(verify_admin)):
    d = _v3_clean_blog_fields(_v3_payload_dict(payload))
    extra = d.pop("extra", None)
    now = now_str()
    p = BlogPost(
        title=d["title"], body=d["body"], excerpt=d.get("excerpt"), cover_image_url=d.get("cover_image_url"),
        category=d.get("category", "education"), is_pinned=bool(d.get("is_pinned")),
        is_published=bool(d.get("is_published", True)),
        extra_json=json.dumps(extra) if extra is not None else None,
        views=0, author="OX-Bridge Team", created_at=now, updated_at=now,
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    return {"success": True, "post": _v3_blog_out(p, full=True)}


@app.put("/admin/blog/posts/{post_id}")
def v3_admin_blog_update(post_id: int, payload: V3BlogUpdate, db=Depends(get_db), admin_ok: bool = Depends(verify_admin)):
    p = db.query(BlogPost).filter(BlogPost.id == post_id).first()
    if not p:
        raise HTTPException(404, "Post not found")
    d = _v3_clean_blog_fields(_v3_payload_dict(payload, exclude_unset=True))
    if "extra" in d:
        extra = d.pop("extra")
        p.extra_json = json.dumps(extra) if extra is not None else None
    for field in ("title", "body", "excerpt", "cover_image_url", "category", "is_pinned", "is_published"):
        if field in d and (d[field] is not None or field in ("excerpt", "cover_image_url")):
            setattr(p, field, d[field])
    p.updated_at = now_str()
    db.commit()
    db.refresh(p)
    return {"success": True, "post": _v3_blog_out(p, full=True)}


@app.delete("/admin/blog/posts/{post_id}")
def v3_admin_blog_delete(post_id: int, db=Depends(get_db), admin_ok: bool = Depends(verify_admin)):
    p = db.query(BlogPost).filter(BlogPost.id == post_id).first()
    if not p:
        raise HTTPException(404, "Post not found")
    db.delete(p)
    db.commit()
    return {"success": True}


@app.post("/admin/blog/posts/{post_id}/pin")
def v3_admin_blog_pin(post_id: int, payload: V3BlogToggle, db=Depends(get_db), admin_ok: bool = Depends(verify_admin)):
    p = db.query(BlogPost).filter(BlogPost.id == post_id).first()
    if not p:
        raise HTTPException(404, "Post not found")
    p.is_pinned = (not bool(p.is_pinned)) if payload.value is None else bool(payload.value)
    p.updated_at = now_str()
    db.commit()
    return {"success": True, "is_pinned": bool(p.is_pinned)}


@app.post("/admin/blog/posts/{post_id}/publish")
def v3_admin_blog_publish(post_id: int, payload: V3BlogToggle, db=Depends(get_db), admin_ok: bool = Depends(verify_admin)):
    p = db.query(BlogPost).filter(BlogPost.id == post_id).first()
    if not p:
        raise HTTPException(404, "Post not found")
    p.is_published = (not bool(p.is_published)) if payload.value is None else bool(payload.value)
    p.updated_at = now_str()
    db.commit()
    return {"success": True, "is_published": bool(p.is_published)}


@app.get("/admin/blog/top-students-suggestions")
def v3_admin_blog_top_students(limit: int = 10, db=Depends(get_db), admin_ok: bool = Depends(verify_admin)):
    """Top students by score, shaped so you can paste `suggested_extra` into a top_students post."""
    limit = min(max(1, limit), 50)
    top = db.query(User).filter(User.progress_score.isnot(None)) \
        .order_by(User.progress_score.desc()).limit(limit).all()
    return {
        "students": [{"username": u.username, "full_name": u.full_name, "profile_pic": u.profile_pic,
                      "score": u.progress_score or 0, "streak": u.study_streak or 0, "coins": u.coins or 0}
                     for u in top],
        "suggested_extra": [{"name": u.full_name or u.username, "score": u.progress_score or 0,
                             "photo": u.profile_pic} for u in top],
    }


# ------------------------------------------------------------
# 7. ADMIN DASHBOARD EXTRAS
# ------------------------------------------------------------
class V3HubNotify(BaseModel):
    username: Optional[str] = None      # omit/null = every student
    message: str


@app.get("/admin/hub/users")
def v3_admin_hub_users(limit: int = 500, db=Depends(get_db), admin_ok: bool = Depends(verify_admin)):
    """Plain list of Learning Hub students (the admin dashboard's Users tab calls this)."""
    limit = min(max(1, limit), 2000)
    rows = db.query(User).order_by(User.id.desc()).limit(limit).all()
    return [{
        "username": u.username, "full_name": u.full_name, "email": u.email,
        "score": u.progress_score or 0, "streak": u.study_streak or 0,
        "last_study_date": u.last_study_date,
        "is_premium": is_premium_active(bool(u.is_premium), u.premium_expires),
    } for u in rows]


@app.post("/admin/hub/notify")
def v3_admin_hub_notify(payload: V3HubNotify, db=Depends(get_db), admin_ok: bool = Depends(verify_admin)):
    """Quick message to one Learning Hub student or everyone (shows in the in-app inbox)."""
    message = (payload.message or "").strip()
    if not message:
        raise HTTPException(400, "Write a message first")
    if len(message) > 2000:
        raise HTTPException(400, "Message is too long (max 2000 characters)")
    target = None
    if payload.username and payload.username.strip():
        u = db.query(User).filter(sa_func.lower(User.username) == payload.username.strip().lower()).first()
        if not u:
            raise HTTPException(404, "That username does not exist")
        target = u.username
    msg = AdminMessage(title="Message from OX-Bridge", body=message, kind="announcement",
                       target_username=target, pinned=False, send_push=False, push_sent=False,
                       created_by="admin", created_at=now_str())
    db.add(msg)
    db.commit()
    sent_to = 1 if target else int(db.query(sa_func.count(User.id)).scalar() or 0)
    return {"success": True, "id": msg.id, "sent_to": sent_to, "broadcast": target is None}


@app.get("/admin/liveclass/{room}/messages")
def v3_lc_admin_messages(room: str, limit: int = 60, db=Depends(get_db), admin_ok: bool = Depends(verify_admin)):
    """Moderation view: latest messages in a room (with ids, so you can delete one)."""
    room = _lc_clean_room(room)
    limit = min(max(1, limit), 300)
    rows = db.query(LiveClassMessage).filter(LiveClassMessage.room == room) \
        .order_by(LiveClassMessage.id.desc()).limit(limit).all()
    rows.reverse()
    rmap = _lc_reply_map(db, rows)
    out = []
    for m in rows:
        d = _lc_serialize(m, rmap)
        d["was_deleted"] = bool(m.is_deleted)
        out.append(d)
    return {"room": room, "messages": out}



# ------------------------------------------------------------
# 8. PRIVATE CHATS + FINDING STUDENTS
# Personal chats use rooms named dm__<one>__<other> (built by the app).
# Groups are rooms named grp-<name>-<code>; the code is the invite.
# ------------------------------------------------------------
_us_hits: dict = {}     # username -> unix times of recent searches


def _lc_slug(s) -> str:
    """Same as the app's slug(): lowercase, anything not a-z/0-9 becomes '-'."""
    return re.sub(r"[^a-z0-9]+", "-", str(s or "").lower()).strip("-")


def _lc_b36(n: int) -> str:
    if n == 0:
        return "0"
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    while n:
        n, r = divmod(n, 36)
        out = digits[r] + out
    return out


def _lc_js_hash(s) -> int:
    """Same as the app's hash() (31-multiplier, 32-bit)."""
    h = 0
    for ch in str(s or ""):
        h = (h * 31 + ord(ch)) & 0xFFFFFFFF
    return h


def _lc_dm_parts(room: str):
    parts = room.split("__")
    if len(parts) == 3 and parts[0] == "dm" and parts[1] and parts[2]:
        return parts[1], parts[2]
    return None


def _lc_hashed_dm_ok(me: str, h1: str, h2: str) -> bool:
    """Very long usernames make the app shorten a personal-chat room name to two hashes; verify it is really mine."""
    db = SessionLocal()
    try:
        if len(me) >= 38:
            names = [u for (u,) in db.query(User.username).all()]
        else:
            names = [u for (u,) in db.query(User.username).filter(sa_func.length(User.username) >= 38).all()]
        for n in names:
            c = _lc_slug(n)
            if not c or c == me:
                continue
            x = sorted([me, c])
            if len("dm__" + x[0] + "__" + x[1]) <= 80:
                continue
            if _lc_b36(_lc_js_hash(x[0] + "|" + x[1])) == h1 and _lc_b36(_lc_js_hash(x[1] + "|" + x[0])) == h2:
                return True
        return False
    finally:
        db.close()


def _lc_check_access(room: str, username: str):
    """Personal chats (dm__...) can only be opened by the two students in them. Everything else is unchanged."""
    if not room.startswith("dm__"):
        return
    parts = _lc_dm_parts(room)
    if parts is None:
        raise HTTPException(403, "This chat is not available")
    me = _lc_slug(username)
    if me in parts:
        return
    a, b = parts
    if re.fullmatch(r"[0-9a-z]{1,7}", a) and re.fullmatch(r"[0-9a-z]{1,7}", b):
        try:
            if _lc_hashed_dm_ok(me, a, b):
                return
        except Exception as e:
            print(f"[LIVECLASS DM CHECK ERROR] {e}")
    raise HTTPException(403, "This is a private chat between two other students")


@app.get("/users/search")
def v3_users_search(q: str = "", limit: int = 20, user=Depends(_game_user), db=Depends(get_db)):
    """Find any student by username or name (min 2 letters). Only safe public details are returned."""
    q = (q or "").strip()
    if len(q) < 2:
        return {"users": []}
    now = _time.time()
    hits = [t for t in _us_hits.get(user.username, []) if now - t < 60]
    if len(hits) >= 40:
        raise HTTPException(429, "Too many searches. Wait a moment.")
    hits.append(now)
    _us_hits[user.username] = hits

    limit = min(max(1, limit), 30)
    safe = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    like = f"%{safe}%"
    rows = db.query(User).filter(
        (User.username.ilike(like, escape="\\")) | (User.full_name.ilike(like, escape="\\")),
        User.id != user.id,
    ).limit(100).all()
    ql = q.lower()

    def rank(u):
        name = (u.username or "").lower()
        full = (u.full_name or "").lower()
        if name == ql:
            return 0
        if name.startswith(ql) or full.startswith(ql):
            return 1
        return 2

    rows.sort(key=lambda u: (rank(u), (u.username or "").lower()))
    return {"users": [{
        "username": u.username, "full_name": u.full_name, "profile_pic": getattr(u, "profile_pic", None),
        "score": u.progress_score or 0, "streak": u.study_streak or 0,
    } for u in rows[:limit]]}


@app.get("/liveclass/my-chats")
def v3_lc_my_chats(limit: int = 50, user=Depends(_game_user), db=Depends(get_db)):
    """
    Every personal chat and group/room this student is part of, newest first, with the last message.
    Lets the app show a message someone sent you (even from a stranger) and keeps your chat list on a new phone.
    """
    limit = min(max(1, limit), 100)
    me = _lc_slug(user.username)
    dm_like = LiveClassMessage.room.like("dm\\_\\_%", escape="\\")
    cand = [r for (r,) in db.query(LiveClassMessage.room)
            .filter(dm_like, LiveClassMessage.room.like(f"%{me}%")).distinct().limit(300).all()]
    dm_rooms = []
    for r in cand:
        parts = _lc_dm_parts(r)
        if parts and me in parts:
            dm_rooms.append(r)
    posted = [r for (r,) in db.query(LiveClassMessage.room)
              .filter(LiveClassMessage.sender_username == user.username).distinct().limit(300).all()]
    rooms = list(dict.fromkeys(dm_rooms + posted))
    if not rooms:
        return {"chats": []}

    last_ids = [i for (i,) in db.query(sa_func.max(LiveClassMessage.id))
                .filter(LiveClassMessage.room.in_(rooms)).group_by(LiveClassMessage.room)
                .order_by(sa_func.max(LiveClassMessage.id).desc()).limit(limit).all()]
    last_msgs = db.query(LiveClassMessage).filter(LiveClassMessage.id.in_(last_ids)).all() if last_ids else []
    last_msgs.sort(key=lambda m: m.id, reverse=True)

    dm_in_result = [m.room for m in last_msgs if m.room.startswith("dm__")]
    peers = {}
    if dm_in_result:
        for room, sender in db.query(LiveClassMessage.room, LiveClassMessage.sender_username) \
                .filter(LiveClassMessage.room.in_(dm_in_result), LiveClassMessage.sender_username != user.username).distinct().all():
            peers.setdefault(room, sender)

    chats = []
    for m in last_msgs:
        if m.is_deleted:
            snippet = "This message was deleted"
        elif m.message_text:
            snippet = m.message_text[:80]
        elif m.image_url:
            snippet = "📷 Photo"
        else:
            snippet = "📎 File"
        kind = "dm" if m.room.startswith("dm__") else ("group" if m.room.startswith("grp-") else "room")
        chats.append({
            "room": m.room, "type": kind, "peer": peers.get(m.room) if kind == "dm" else None,
            "last": {"id": m.id, "sender_username": m.sender_username, "text": snippet,
                     "message_type": m.message_type, "created_at": m.created_at},
        })
    return {"chats": chats}


# ------------------------------------------------------------
# 9. FULL THEORY LESSONS + WAEC/NECO THEORY ADMIN VIEW
# ------------------------------------------------------------
# Every topic's lesson is written once by the AI and saved on the topic. Raise
# THEORY_LESSON_VERSION whenever the lesson prompt below is improved: each topic
# then rebuilds its lesson (once) the next time a student opens it.
THEORY_LESSON_VERSION = 3

_V3_SUBJECT_GUIDES = [
    (("further math", "mathematics", "maths", "statistics", "arithmetic", "quantitative"),
     "the definition and the rules/formulas (written clearly), the method step by step, how to recognise which method to use, "
     "two or three worked examples that go from easy to exam level, special cases, and the usual calculation traps."),
    (("physics",),
     "the definition and principle/law, every formula with the meaning and unit of each symbol, the types/cases, "
     "how it works (step by step), applications in daily life, how a diagram or experiment for it is set up and labelled (described in words), "
     "and a short numerical example."),
    (("chemistry",),
     "the definition, the particles/types/methods involved, physical and chemical properties, how it is carried out or prepared (step by step), "
     "relevant equations, laboratory/industrial/everyday uses, tests or identification, safety points, and how a diagram of the set-up is labelled (described in words)."),
    (("biology", "agricultural", "agric", "health", "physical and health", "basic science", "integrated science", "science", "computer", "ict", "technology"),
     "the definition, the structure and the function of every part (parts and what each does), types/classification, "
     "processes explained step by step, differences and similarities where the syllabus compares things, importance and everyday examples, "
     "and the labelled parts of the usual diagram (described in words)."),
    (("english", "literature", "yoruba", "igbo", "hausa", "french", "language", "grammar", "comprehension"),
     "the definition, the rules with clear examples, the types, common errors students make and how to correct them, "
     "how it is tested in exams, and short model sentences or answers."),
    (("government", "civic", "economics", "commerce", "accounting", "history", "geography", "crs", "irs", "christian", "islamic",
      "social", "business", "marketing", "office", "insurance", "citizenship", "security", "culture"),
     "the meaning, the features/characteristics, the types or stages, the causes and effects (or advantages and disadvantages) where they apply, "
     "functions, Nigerian examples with names and dates where relevant, and the key points examiners reward in essay answers."),
]


def _v3_subject_guide(subject_name: str) -> str:
    name = (subject_name or "").lower()
    for keys, guide in _V3_SUBJECT_GUIDES:
        if any(k in name for k in keys):
            return guide
    return ("the definition, all the main points, types or parts, how it works or is done step by step, "
            "importance and everyday Nigerian examples, and the points examiners usually ask about.")


def _v3_theory_lesson_prompt(level, subject_name, title, admin_notes=None) -> str:
    notes = ""
    if admin_notes and str(admin_notes).strip():
        notes = ('The teacher gave this specific question/note. Make sure the lesson fully covers it: "'
                 + str(admin_notes).strip() + '"\n')
    return f"""You are Ox-Bridge AI Tutor for Nigerian {level} students studying {subject_name}.
Topic: '{title}'.
{notes}
GOAL: write the COMPLETE lesson on this topic, so a student who reads it once knows everything the {level} syllabus (WAEC/NECO/JAMB where relevant) expects about it and does not need another source. A thin overview is a failure. Padding, repeated points, long introductions and filler are also a failure: every sentence must teach something. Length: about 600 to 1000 words (shorter only if the topic is very narrow, never skimpy).

WHAT THE LESSON MUST COVER for this subject (a checklist of content, not an order; include a definition or meaning only when this topic really has one): {_v3_subject_guide(subject_name)}

OPENING: first decide what kind of topic this is (silently, never mention this step) and open the lesson in the way that fits it. Do NOT open with a 'Meaning' section by default, and never write a thin one-line definition just to fill a section:
- A term or concept that has a clear definition (for example osmosis, inflation, a noun): open with ## Definition, in one or two simple sentences, then say why it matters.
- A process, system or how-something-works topic (for example photosynthesis, the digestive system, how a bill becomes law): open with ## Overview, a plain explanation of what happens and why.
- A broad chapter made of many parts (for example the Nigerian Constitution, Organic Chemistry): open with ## Introduction that maps out the main parts the lesson will cover.
- A skill, method or calculation (for example solving quadratic equations, summary writing, writing a formal letter): open with ## The Idea that explains what it is used for and the rule or format to follow.
- A historical event, period or person (for example the amalgamation of Nigeria): open with ## Background that sets the scene.
- A hard or abstract topic: explain it deeply and simply first, with an everyday Nigerian comparison, before the key points.
Name the opening heading to suit the topic.

LAYOUT (after the opening, use these markdown headings in this order, skip one only if it truly does not apply, and rename a heading when a better name fits the topic):
## Key points to know
The full content of the topic as short numbered points or bullets, each with a one-line explanation. Do not leave out any commonly examined sub-point, type, method, property, part, law or step.
## Uses and importance
Real uses and everyday Nigerian examples.
## Worked example
One or two examples. For topics without calculations, use a real-life example or an exam-style answer.
## Common mistakes and exam tips
What students get wrong and what examiners look for.
## Quick summary
5 to 8 bullet points a student can revise from in one minute.
## Test yourself
3 short questions, each with the answer written right below it.

STYLE: simple clear English a {level} student can follow, short sentences, no emojis, no greeting and no closing remark, and never mention these instructions.
Whenever you write ANY mathematical expression, chemical formula, chemical
equation, or physics equation, always wrap it in $...$ for inline math or
$$...$$ for a standalone equation - this applies to every subject, not
just Mathematics. For example: chemistry equations like
$2H_2 + O_2 \\rightarrow 2H_2O$, physics equations like $F = ma$ or
$E = mc^2$, and any subscript/superscript like $H_2O$ or $x^2$ - never
write these in plain text or with ASCII symbols like ^ or _ instead.
Do this silently, without ever mentioning the word "LaTeX" or explaining
the notation. Reminder: wrap every math, chemistry, or physics
expression in $ signs, even a simple one.
Only use a markdown table when you are genuinely comparing several
items side by side across shared columns (e.g. comparing properties
of several elements, contrasting two theories, plant cell vs animal cell). For a normal
explanation, worked example, or summary, write it as plain
paragraphs, numbered steps, or a bullet list instead - do not force
single explanations or single examples into a table just because
it looks structured."""


def _v3_lesson_ok(text_value) -> bool:
    """False for an AI failure message, so a broken answer is never saved as the lesson."""
    t = (text_value or "").strip()
    if len(t) < 200:
        return False
    low = t[:200].lower()
    return not ("ai services unavailable" in low or "all api keys missing" in low)


def _v3_lesson_is_current(db, topic_id: int) -> bool:
    """True when the saved lesson was written with the current prompt. If we cannot tell, keep it (old behaviour)."""
    try:
        with db.begin_nested():
            row = db.execute(text("SELECT lesson_version FROM topics WHERE id = :i"), {"i": topic_id}).first()
        if row is None:
            return True
        return (row[0] or 0) >= THEORY_LESSON_VERSION
    except Exception as e:
        print(f"[V3 LESSON VERSION READ SKIPPED] {e}")
        return True


def _v3_mark_lesson_version(db, topic_id: int):
    try:
        with db.begin_nested():
            db.execute(text("UPDATE topics SET lesson_version = :v WHERE id = :i"),
                       {"v": THEORY_LESSON_VERSION, "i": topic_id})
    except Exception as e:
        print(f"[V3 LESSON VERSION WRITE SKIPPED] {e}")


class V3TheoryRegen(BaseModel):
    topic_id: Optional[int] = None
    subject_id: Optional[int] = None
    everything: bool = False


@app.get("/admin/theory/status")
def v3_theory_status(db=Depends(get_db), admin_ok: bool = Depends(verify_admin)):
    """How many topics have a saved lesson, and how many of those use the newest full-lesson prompt."""
    try:
        row = db.execute(text(
            "SELECT COUNT(*), "
            "SUM(CASE WHEN cached_explanation IS NOT NULL THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN cached_explanation IS NOT NULL AND COALESCE(lesson_version, 0) >= :v THEN 1 ELSE 0 END) "
            "FROM topics"), {"v": THEORY_LESSON_VERSION}).first()
    except Exception as e:
        db.rollback()
        raise HTTPException(500, f"Lesson version column is not ready yet: {e}")
    total, saved, current = int(row[0] or 0), int(row[1] or 0), int(row[2] or 0)
    return {"version": THEORY_LESSON_VERSION, "topics": total, "with_saved_lesson": saved,
            "full_lessons": current, "will_rebuild_when_opened": max(0, saved - current)}


@app.post("/admin/theory/regenerate")
def v3_theory_regenerate(payload: V3TheoryRegen, db=Depends(get_db), admin_ok: bool = Depends(verify_admin)):
    """Mark saved lessons as out of date. Each one is rebuilt (once) the next time a student opens that topic."""
    if payload.topic_id:
        sql, params = "UPDATE topics SET lesson_version = 0 WHERE id = :i", {"i": payload.topic_id}
    elif payload.subject_id:
        sql, params = "UPDATE topics SET lesson_version = 0 WHERE subject_id = :i", {"i": payload.subject_id}
    elif payload.everything:
        sql, params = "UPDATE topics SET lesson_version = 0", {}
    else:
        raise HTTPException(400, "Choose topic_id, subject_id or everything")
    try:
        res = db.execute(text(sql), params)
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(500, f"Could not refresh lessons: {e}")
    return {"success": True, "marked": int(res.rowcount or 0)}


# ---------- usage details for the activity log (what exam / subject was opened) ----------
def _v3_special_detail(action: str, path: str, request):
    """Readable detail like 'WAEC · Physics · 2019'. Returns None to fall back to the generic detail."""
    try:
        qp = request.query_params
        if action == "waec_theory":
            if path.startswith("/waec-theory/explain/"):
                qid = path.rsplit("/", 1)[-1]
                if qid.isdigit():
                    db = SessionLocal()
                    try:
                        q = db.query(WaecTheoryQuestion).filter(WaecTheoryQuestion.id == int(qid)).first()
                        if q:
                            return f"{q.exam_type} · {q.subject} · {q.year}"[:200]
                    finally:
                        db.close()
                return None
            subject = qp.get("subject")
            if subject:
                bits = [(qp.get("exam_type") or "WAEC").upper(), subject, qp.get("year")]
                return " · ".join(str(b) for b in bits if b)[:200]
        if action == "past_questions":
            tail = path[len("/past-questions/"):].strip("/").split("/")
            if len(tail) >= 2 and tail[0] and tail[1]:
                first = "Random" if tail[0].lower() == "random" else tail[0].upper()
                return f"{first} · {tail[1]}"[:200]
    except Exception:
        pass
    return None


# ---------- WAEC/NECO theory: admin view ----------
class V3WaecTheoryUpdate(BaseModel):
    exam_type: Optional[str] = None
    year: Optional[int] = None
    subject: Optional[str] = None
    topic: Optional[str] = None
    question_text: Optional[str] = None
    model_answer: Optional[str] = None
    image_url: Optional[str] = None
    marks: Optional[int] = None


@app.get("/admin/waec-theory/usage")
def v3_waec_theory_usage(days: int = 1, db=Depends(get_db), admin_ok: bool = Depends(verify_admin)):
    """Who opened WAEC/NECO theory questions in the last N days, and which exam/subject each one used."""
    since = _v3_since(days)
    rows = []
    try:
        rows = db.execute(text(
            "SELECT username, detail, created_at FROM activity_logs "
            "WHERE action = 'waec_theory' AND created_at >= :s ORDER BY id DESC LIMIT 5000"), {"s": since}).fetchall()
    except Exception:
        db.rollback()
        logs = db.query(ActivityLog).filter(ActivityLog.action == "waec_theory", ActivityLog.created_at >= since) \
            .order_by(ActivityLog.id.desc()).limit(5000).all()
        rows = [(l.username, None, l.created_at) for l in logs]

    def label(detail):
        if not detail:
            return "(subject not recorded)"
        parts = [p.strip() for p in detail.split("·")]
        return " · ".join(parts[:2]) if len(parts) >= 2 else detail

    by_subject: dict = {}
    by_user: dict = {}
    anonymous = 0
    for username, detail, created in rows:
        lab = label(detail)
        s = by_subject.setdefault(lab, {"uses": 0, "users": set()})
        s["uses"] += 1
        if not username:
            anonymous += 1
            continue
        s["users"].add(username.lower())
        u = by_user.setdefault(username.lower(), {"username": username, "uses": 0, "last_used": created, "subjects": set()})
        u["uses"] += 1
        u["subjects"].add(lab)
        if created and (not u["last_used"] or created > u["last_used"]):
            u["last_used"] = created
    return {
        "days": max(1, min(int(days or 1), 365)),
        "total_uses": len(rows), "unique_users": len(by_user), "anonymous_uses": anonymous,
        "subjects": sorted([{"subject": k, "uses": v["uses"], "users": len(v["users"])} for k, v in by_subject.items()],
                           key=lambda x: x["uses"], reverse=True),
        "users": sorted([{"username": v["username"], "uses": v["uses"], "last_used": v["last_used"],
                          "subjects": sorted(v["subjects"])} for v in by_user.values()],
                        key=lambda x: x["uses"], reverse=True)[:200],
    }


@app.get("/admin/waec-theory/{question_id}")
def v3_waec_theory_get(question_id: int, db=Depends(get_db), admin_ok: bool = Depends(verify_admin)):
    q = db.query(WaecTheoryQuestion).filter(WaecTheoryQuestion.id == question_id).first()
    if not q:
        raise HTTPException(404, "Question not found")
    return {"id": q.id, "exam_type": q.exam_type, "year": q.year, "subject": q.subject, "topic": q.topic,
            "question_text": q.question_text, "model_answer": q.model_answer, "image_url": q.image_url,
            "marks": q.marks, "created_at": q.created_at, "has_ai_explanation": bool(q.cached_explanation)}


@app.put("/admin/waec-theory/{question_id}")
def v3_waec_theory_update(question_id: int, payload: V3WaecTheoryUpdate, db=Depends(get_db),
                          admin_ok: bool = Depends(verify_admin)):
    q = db.query(WaecTheoryQuestion).filter(WaecTheoryQuestion.id == question_id).first()
    if not q:
        raise HTTPException(404, "Question not found")
    d = _v3_payload_dict(payload, exclude_unset=True)
    if "exam_type" in d and d["exam_type"]:
        d["exam_type"] = d["exam_type"].strip().upper()
        if d["exam_type"] not in ("WAEC", "NECO"):
            raise HTTPException(400, "Exam must be WAEC or NECO")
    if "question_text" in d and not (d["question_text"] or "").strip():
        raise HTTPException(400, "The question text cannot be empty")
    if "subject" in d and not (d["subject"] or "").strip():
        raise HTTPException(400, "The subject cannot be empty")
    changed_content = False
    for field in ("exam_type", "year", "subject", "topic", "question_text", "model_answer", "image_url", "marks"):
        if field in d:
            val = d[field]
            if isinstance(val, str):
                val = val.strip() or None
            if field in ("exam_type", "year", "subject", "question_text") and val is None:
                continue
            if getattr(q, field) != val:
                setattr(q, field, val)
                if field != "marks":
                    changed_content = True
    if changed_content:
        q.cached_explanation = None      # the saved AI explanation belonged to the old wording
    db.commit()
    return {"success": True, "id": q.id}


# ------------------------------------------------------------
# 10. ACCOUNT CONTROL (mute / suspend / delete), PROFILE PICTURE,
#     30-DAY PHOTO EXPIRY, DM PUSH NOTIFICATIONS
# ------------------------------------------------------------
from sqlalchemy import or_ as _sa_or

MEDIA_EXPIRE_DAYS = 30          # chat and feed photos are removed from Cloudinary after this many days
_V3_EXPIRED = "expired"         # stored in image_url / file_url once the file is gone
_v3_restr_cache: dict = {}      # (account, lowercase username) -> (checked_at, status, reason)
_v3_expire_state = {"last": 0.0, "running": False}
_v3_dm_push_last: dict = {}     # (room, peer) -> unix time of the last push


class UserRestriction(Base):
    __tablename__ = "user_restrictions"
    __table_args__ = (UniqueConstraint("account", "username", name="uq_user_restriction"),)
    id         = Column(Integer, primary_key=True, index=True)
    account    = Column(String, index=True)          # hub | campus
    username   = Column(String, index=True)          # stored in lowercase
    status     = Column(String)                      # muted | suspended
    reason     = Column(String, nullable=True)
    until      = Column(String, nullable=True)       # UTC ISO; empty = until an admin lifts it
    created_by = Column(String, default="admin")
    created_at = Column(String)


def _v3_restriction(account: str, username: str):
    """Returns {'status','reason'} when this account is muted/suspended right now, else None. Cached for 20 s."""
    key = (account, (username or "").strip().lower())
    if not key[1]:
        return None
    now = _time.time()
    hit = _v3_restr_cache.get(key)
    if hit and now - hit[0] < 20:
        return {"status": hit[1], "reason": hit[2]} if hit[1] else None
    status = reason = None
    db = SessionLocal()
    try:
        r = db.query(UserRestriction).filter(UserRestriction.account == account,
                                             UserRestriction.username == key[1]).first()
        if r and (not r.until or r.until > now_str()):
            status, reason = r.status, r.reason
    except Exception as e:
        print(f"[V3 RESTRICTION CHECK ERROR] {e}")
    finally:
        db.close()
    _v3_restr_cache[key] = (now, status, reason)
    return {"status": status, "reason": reason} if status else None


def _v3_restrictions_for(account: str, usernames: list) -> dict:
    names = [(u or "").lower() for u in usernames if u]
    if not names:
        return {}
    db = SessionLocal()
    try:
        rows = db.query(UserRestriction).filter(UserRestriction.account == account,
                                                UserRestriction.username.in_(names)).all()
        now = now_str()
        return {r.username: {"status": r.status, "reason": r.reason, "until": r.until}
                for r in rows if not r.until or r.until > now}
    except Exception:
        return {}
    finally:
        db.close()


def _v3_block_if_suspended(account: str, username: str, code: int = 403):
    r = _v3_restriction(account, username)
    if r and r["status"] == "suspended":
        why = f" Reason: {r['reason']}" if r.get("reason") else ""
        raise HTTPException(code, "Your account has been suspended by the admin." + why)


def _v3_block_if_restricted(account: str, username: str):
    """Muted or suspended people cannot post or send messages."""
    r = _v3_restriction(account, username)
    if r:
        why = f" Reason: {r['reason']}" if r.get("reason") else ""
        word = "suspended" if r["status"] == "suspended" else "muted"
        raise HTTPException(403, f"Your account has been {word} by the admin, so you cannot post or send messages.{why}")


# ---------- admin: restrict / lift / delete ----------
class V3Restrict(BaseModel):
    account: str = "hub"                 # hub | campus
    username: str
    status: str = "muted"                # muted | suspended
    reason: Optional[str] = None
    days: Optional[int] = None           # empty = until an admin lifts it


class V3AccountRef(BaseModel):
    account: str = "hub"
    username: str


class V3DeleteAccount(BaseModel):
    account: str = "hub"
    username: str
    confirm: str                         # type the username again to confirm


def _v3_find_account(db, account: str, username: str):
    """Returns the exact stored username, or None when it does not exist."""
    u = (username or "").strip().lower()
    if account == "hub":
        row = db.query(User).filter(sa_func.lower(User.username) == u).first()
        return row.username if row else None
    if account == "campus":
        row = db.execute(text("SELECT username FROM campus_users WHERE LOWER(username) = :u"), {"u": u}).first()
        return row[0] if row else None
    raise HTTPException(400, "account must be hub or campus")


@app.post("/admin/accounts/restrict")
def v3_admin_restrict(payload: V3Restrict, db=Depends(get_db), admin_ok: bool = Depends(verify_admin)):
    if payload.status not in ("muted", "suspended"):
        raise HTTPException(400, "status must be muted or suspended")
    exact = _v3_find_account(db, payload.account, payload.username)
    if not exact:
        raise HTTPException(404, "That username does not exist")
    until = None
    if payload.days:
        until = (datetime.now(timezone.utc) + timedelta(days=max(1, min(int(payload.days), 3650)))).isoformat()
    key = exact.lower()
    r = db.query(UserRestriction).filter(UserRestriction.account == payload.account, UserRestriction.username == key).first()
    reason = (payload.reason or "").strip()[:200] or None
    if r:
        r.status, r.reason, r.until, r.created_at = payload.status, reason, until, now_str()
    else:
        db.add(UserRestriction(account=payload.account, username=key, status=payload.status, reason=reason,
                               until=until, created_by="admin", created_at=now_str()))
    db.commit()
    _v3_restr_cache.pop((payload.account, key), None)
    return {"success": True, "username": exact, "status": payload.status, "until": until}


@app.post("/admin/accounts/unrestrict")
def v3_admin_unrestrict(payload: V3AccountRef, db=Depends(get_db), admin_ok: bool = Depends(verify_admin)):
    key = (payload.username or "").strip().lower()
    n = db.query(UserRestriction).filter(UserRestriction.account == payload.account, UserRestriction.username == key) \
        .delete(synchronize_session=False)
    db.commit()
    _v3_restr_cache.pop((payload.account, key), None)
    return {"success": True, "lifted": int(n or 0)}


@app.get("/admin/accounts/restrictions")
def v3_admin_restrictions(account: str = "", db=Depends(get_db), admin_ok: bool = Depends(verify_admin)):
    q = db.query(UserRestriction)
    if account:
        q = q.filter(UserRestriction.account == account)
    now = now_str()
    return {"restrictions": [{"account": r.account, "username": r.username, "status": r.status, "reason": r.reason,
                              "until": r.until, "created_at": r.created_at,
                              "active": (not r.until or r.until > now)}
                             for r in q.order_by(UserRestriction.id.desc()).limit(500).all()]}


@app.post("/admin/accounts/delete")
def v3_admin_delete_account(payload: V3DeleteAccount, db=Depends(get_db), admin_ok: bool = Depends(verify_admin)):
    """Permanently remove a student (and their progress). The username must be typed again as confirmation."""
    exact = _v3_find_account(db, payload.account, payload.username)
    if not exact:
        raise HTTPException(404, "That username does not exist")
    if (payload.confirm or "").strip().lower() != exact.lower():
        raise HTTPException(400, "Confirmation does not match the username. Nothing was deleted.")
    low = exact.lower()
    try:
        if payload.account == "hub":
            user = db.query(User).filter(User.username == exact).first()
            uid = user.id
            # every table that points at users.id (progress, friends, games, badges ...)
            for table in reversed(Base.metadata.sorted_tables):
                if table.name == "users":
                    continue
                conds = [col == uid for col in table.columns for fk in col.foreign_keys
                         if fk.column.table.name == "users" and fk.column.name == "id"]
                if conds:
                    db.execute(table.delete().where(_sa_or(*conds)))
            # rows that are linked by username instead
            for model, col in ((ActivityLog, ActivityLog.username), (AdminMessageRead, AdminMessageRead.username),
                               (PushSubscription, PushSubscription.username)):
                try:
                    with db.begin_nested():
                        db.query(model).filter(sa_func.lower(col) == low).delete(synchronize_session=False)
                except Exception as e:
                    print(f"[V3 DELETE CLEANUP SKIPPED] {model.__name__}: {e}")
            db.query(AdminMessage).filter(sa_func.lower(AdminMessage.target_username) == low) \
                .delete(synchronize_session=False)
            db.query(LiveClassMessage).filter(sa_func.lower(LiveClassMessage.sender_username) == low) \
                .update({LiveClassMessage.is_deleted: True}, synchronize_session=False)
            db.query(FeedPost).filter(sa_func.lower(FeedPost.author) == low) \
                .update({FeedPost.is_deleted: True}, synchronize_session=False)
            db.query(FeedReaction).filter(sa_func.lower(FeedReaction.username) == low).delete(synchronize_session=False)
            db.query(FeedVote).filter(sa_func.lower(FeedVote.username) == low).delete(synchronize_session=False)
            db.query(BattlePlayer).filter(sa_func.lower(BattlePlayer.username) == low).delete(synchronize_session=False)
            db.delete(user)
        else:
            db.query(CommunityGroupMember).filter(sa_func.lower(CommunityGroupMember.username) == low) \
                .delete(synchronize_session=False)
            db.query(CommunityMessage).filter(sa_func.lower(CommunityMessage.sender_username) == low) \
                .update({CommunityMessage.is_deleted: True}, synchronize_session=False)
            db.execute(text("DELETE FROM campus_users WHERE LOWER(username) = :u"), {"u": low})
        db.query(UserRestriction).filter(UserRestriction.account == payload.account, UserRestriction.username == low) \
            .delete(synchronize_session=False)
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(500, f"Could not delete this account (nothing was changed): {e}")
    _v3_restr_cache.pop((payload.account, low), None)
    return {"success": True, "deleted": exact, "account": payload.account}


# ---------- profile picture (Learning Hub) ----------
class V3ProfilePic(BaseModel):
    image_url: str


def _v3_valid_pic_url(url: str) -> str:
    url = (url or "").strip()
    if len(url) > 500 or not re.match(r"^https://res\.cloudinary\.com/", url):
        raise HTTPException(400, "Upload the photo first (the app does this for you), then save it.")
    return url


@app.post("/me/profile-pic")
def v3_set_profile_pic(payload: V3ProfilePic, background_tasks: BackgroundTasks,
                       user=Depends(_game_user), db=Depends(get_db)):
    """Set the logged-in student's profile photo to an image already uploaded with POST /upload/image."""
    url = _v3_valid_pic_url(payload.image_url)
    old = user.profile_pic
    user.profile_pic = url
    db.commit()
    if old and old != url:
        background_tasks.add_task(_v3_cloudinary_destroy, old)     # free the old photo's space
    return {"success": True, "profile_pic": url}


@app.post("/me/profile-pic/remove")
def v3_remove_profile_pic(background_tasks: BackgroundTasks, user=Depends(_game_user), db=Depends(get_db)):
    old = user.profile_pic
    user.profile_pic = None
    db.commit()
    if old:
        background_tasks.add_task(_v3_cloudinary_destroy, old)
    return {"success": True, "profile_pic": None}


# ---------- 30-day photo expiry ----------
_V3_CLD_RE = re.compile(r"^https?://res\.cloudinary\.com/([^/]+)/(image|video|raw)/upload/(?:v\d+/)?(.+)$")


def _v3_cloudinary_destroy(url) -> bool:
    """True = the file is gone (deleted, already missing, or not ours). False = could not delete now, try again later."""
    if not url or not str(url).startswith("http"):
        return True
    m = _V3_CLD_RE.match(str(url))
    if not m:
        return True                                  # not a Cloudinary file: nothing for us to free
    if not (CLOUDINARY_SDK_AVAILABLE and CLOUDINARY_CONFIGURED):
        return False
    try:
        mine = cloudinary.config().cloud_name
        if mine and m.group(1) != mine:
            return True                              # belongs to another Cloudinary account: never touch it
        rtype, pid = m.group(2), m.group(3)
        if rtype in ("image", "video"):
            pid = re.sub(r"\.[A-Za-z0-9]{2,5}$", "", pid)
        res = cloudinary.uploader.destroy(pid, resource_type=rtype, invalidate=True)
        return (res or {}).get("result") in ("ok", "not found")
    except Exception as e:
        print(f"[V3 CLOUDINARY DESTROY ERROR] {e}")
        return False


def _v3_media_expiry(created_at, has_media: bool):
    """When this message's photo will disappear (ISO), or None."""
    if not has_media or not created_at:
        return None
    try:
        return (datetime.fromisoformat(str(created_at)) + timedelta(days=MEDIA_EXPIRE_DAYS)).isoformat()
    except Exception:
        return None


def _v3_expire_media_sync(days: int = MEDIA_EXPIRE_DAYS, limit: int = 100) -> dict:
    """Remove photos older than `days` from Cloudinary and mark them expired. Safe to run again and again."""
    days = max(7, int(days or MEDIA_EXPIRE_DAYS))
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    stats = {"live_class": 0, "campus": 0, "feed": 0, "failed": 0, "more_waiting": False}
    db = SessionLocal()
    try:
        for label, model in (("live_class", LiveClassMessage), ("campus", CommunityMessage), ("feed", FeedPost)):
            rows = db.query(model).filter(model.created_at < cutoff, model.image_url.isnot(None),
                                          model.image_url != _V3_EXPIRED).limit(limit).all()
            if len(rows) >= limit:
                stats["more_waiting"] = True
            for r in rows:
                if _v3_cloudinary_destroy(r.image_url):
                    r.image_url = _V3_EXPIRED
                    stats[label] += 1
                else:
                    stats["failed"] += 1
            db.commit()
        # stories vanish from the app after 2 hours, so their photos are freed after a day
        try:
            day = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
            for r in db.query(FeedPost).filter(FeedPost.post_type == "story", FeedPost.created_at < day,
                                               FeedPost.image_url.isnot(None), FeedPost.image_url != _V3_EXPIRED).limit(limit).all():
                if _v3_cloudinary_destroy(r.image_url):
                    r.image_url = _V3_EXPIRED
                    stats["feed"] += 1
            db.commit()
        except Exception:
            db.rollback()
        # old finished quiz battles are small, but there is no reason to keep them forever
        try:
            old = _time.time() - 30 * 86400
            codes = [c for (c,) in db.query(BattleRoom.code).filter(BattleRoom.created_ts < old).limit(500).all()]
            if codes:
                db.query(BattlePlayer).filter(BattlePlayer.code.in_(codes)).delete(synchronize_session=False)
                db.query(BattleRoom).filter(BattleRoom.code.in_(codes)).delete(synchronize_session=False)
                db.commit()
        except Exception:
            db.rollback()
    except Exception as e:
        db.rollback()
        print(f"[V3 MEDIA EXPIRE ERROR] {e}")
    finally:
        db.close()
    return stats


def _v3_expire_job():
    """Background run: a few rounds so a backlog clears, then stop."""
    if _v3_expire_state["running"]:
        return
    _v3_expire_state["running"] = True
    try:
        for _ in range(5):
            s = _v3_expire_media_sync()
            if not s.get("more_waiting"):
                break
    finally:
        _v3_expire_state["running"] = False


def _v3_maybe_expire(background_tasks):
    """Cheap, automatic: at most one clean-up run every 6 hours while the app is being used."""
    now = _time.time()
    if not _v3_expire_state["running"] and now - _v3_expire_state["last"] > 6 * 3600:
        _v3_expire_state["last"] = now
        background_tasks.add_task(_v3_expire_job)


@app.post("/admin/media/expire")
def v3_admin_media_expire(days: int = MEDIA_EXPIRE_DAYS, limit: int = 100, admin_ok: bool = Depends(verify_admin)):
    """Run the photo clean-up now (admin button). Run again while it says more_waiting."""
    return _v3_expire_media_sync(days, min(max(10, limit), 300))


@app.post("/admin/media/expire-cron", dependencies=[Depends(verify_cron)])
def v3_media_expire_cron(background_tasks: BackgroundTasks):
    background_tasks.add_task(_v3_expire_job)
    return {"started": True}


@app.get("/admin/media/status")
def v3_admin_media_status(days: int = MEDIA_EXPIRE_DAYS, db=Depends(get_db), admin_ok: bool = Depends(verify_admin)):
    days = max(7, int(days or MEDIA_EXPIRE_DAYS))
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    out = {"expire_after_days": days}
    for label, model in (("live_class", LiveClassMessage), ("campus", CommunityMessage), ("feed", FeedPost)):
        live = db.query(sa_func.count(model.id)).filter(model.image_url.isnot(None), model.image_url != _V3_EXPIRED).scalar() or 0
        due = db.query(sa_func.count(model.id)).filter(model.image_url.isnot(None), model.image_url != _V3_EXPIRED,
                                                       model.created_at < cutoff).scalar() or 0
        gone = db.query(sa_func.count(model.id)).filter(model.image_url == _V3_EXPIRED).scalar() or 0
        out[label] = {"photos_stored": int(live), "due_to_expire": int(due), "already_expired": int(gone)}
    out["cloudinary_ready"] = bool(CLOUDINARY_SDK_AVAILABLE and CLOUDINARY_CONFIGURED)
    return out


# ---------- phone notification for a personal chat message ----------
def _v3_dm_push_job(room: str, sender: str, peer: str, snippet: str):
    """Push 'sender: message' to the other person when they are not looking at the chat. At most one per minute per chat."""
    try:
        if not _v3_push_ready():
            return
        parts = _lc_dm_parts(room)
        ps, pp = _lc_slug(sender), _lc_slug(peer)
        if not parts or ps == pp or ps not in parts or pp not in parts:
            return                                    # the named person is not in this chat: ignore
        if peer.lower() in {u.lower() for u in _lc_online_users(room)}:
            return                                    # they are in the chat right now
        key = (room, peer.lower())
        now = _time.time()
        if now - _v3_dm_push_last.get(key, 0) < 60:
            return
        _v3_dm_push_last[key] = now
        db = SessionLocal()
        try:
            u = db.query(User).filter(sa_func.lower(User.username) == peer.lower()).first()
            if not u:
                return
            r = _v3_restriction("hub", u.username)
            if r and r["status"] == "suspended":
                return
            send_push_to_user(db, u.username, f"💬 {sender}", (snippet or "New message")[:100])
        finally:
            db.close()
    except Exception as e:
        print(f"[V3 DM PUSH ERROR] {e}")


# ------------------------------------------------------------
# 11. FEED: study notes, tips, questions, polls, mini quizzes and flashcards
# Text and photos only. Photos disappear after MEDIA_EXPIRE_DAYS (see section 10).
# room = a live class room/group, or empty for the open feed everybody sees.
# ------------------------------------------------------------
_FEED_TYPES = ("post", "note", "tip", "question", "poll", "quiz", "flashcards", "story")
_FEED_REACTIONS = ("like", "love", "idea", "clap", "fire")
_feed_recent: dict = {}     # username -> unix times of recent posts


class FeedPost(Base):
    __tablename__ = "feed_posts"
    id         = Column(Integer, primary_key=True, index=True)
    room       = Column(String, nullable=True, index=True)
    author     = Column(String, index=True)
    post_type  = Column(String, default="note")
    title      = Column(String, nullable=True)
    text       = Column(Text, nullable=True)
    image_url  = Column(String, nullable=True)
    data_json  = Column(Text, nullable=True)          # poll/quiz options, flashcards ...
    created_at = Column(String, index=True)
    is_deleted = Column(Boolean, default=False)


class FeedReaction(Base):
    __tablename__ = "feed_reactions"
    __table_args__ = (UniqueConstraint("post_id", "username", name="uq_feed_reaction"),)
    id         = Column(Integer, primary_key=True, index=True)
    post_id    = Column(Integer, ForeignKey("feed_posts.id"), index=True)
    username   = Column(String, index=True)
    kind       = Column(String)
    created_at = Column(String)


class FeedVote(Base):
    __tablename__ = "feed_votes"
    __table_args__ = (UniqueConstraint("post_id", "username", name="uq_feed_vote"),)
    id         = Column(Integer, primary_key=True, index=True)
    post_id    = Column(Integer, ForeignKey("feed_posts.id"), index=True)
    username   = Column(String, index=True)
    choice     = Column(Integer)
    created_at = Column(String)


class V3FeedPostIn(BaseModel):
    post_type: str = "note"
    room: Optional[str] = None
    title: Optional[str] = None
    text: Optional[str] = None
    image_url: Optional[str] = None
    data: Optional[_Any] = None
    closes_in_hours: Optional[int] = None      # polls only


class V3FeedReact(BaseModel):
    kind: Optional[str] = None                 # empty = remove my reaction


class V3FeedVoteIn(BaseModel):
    choice: int


class V3FeedShare(BaseModel):
    room: str


def _feed_clean(payload) -> tuple:
    """Validate a post. Returns (title, text, data_dict_or_None)."""
    ptype = payload.post_type
    if ptype not in _FEED_TYPES:
        raise HTTPException(400, "post_type must be one of: " + ", ".join(_FEED_TYPES))
    title = (payload.title or "").strip()[:120] or None
    body = (payload.text or "").strip()
    if len(body) > 3000:
        raise HTTPException(400, "The post is too long (max 3000 characters)")
    data = payload.data if isinstance(payload.data, dict) else {}
    out = None
    if ptype in ("post", "note", "tip", "question"):
        if not body and not payload.image_url:
            raise HTTPException(400, "Write something or add a photo")
    elif ptype == "story":
        if not body and not payload.image_url:
            raise HTTPException(400, "Write something or add a photo")
        if len(body) > 200:
            raise HTTPException(400, "A story can be up to 200 characters")
    elif ptype in ("poll", "quiz"):
        if not body:
            raise HTTPException(400, "Write the question")
        if len(body) > 400:
            raise HTTPException(400, "Keep the question under 400 characters")
        raw = data.get("options") if isinstance(data.get("options"), list) else []
        options = [str(o).strip()[:100] for o in raw if str(o).strip()]
        top = 6 if ptype == "poll" else 5
        if len(options) < 2 or len(options) > top:
            raise HTTPException(400, f"Give between 2 and {top} options")
        out = {"options": options}
        if ptype == "quiz":
            try:
                correct = int(data.get("correct"))
            except Exception:
                raise HTTPException(400, "Choose the correct option")
            if correct < 0 or correct >= len(options):
                raise HTTPException(400, "Choose the correct option")
            out["correct"] = correct
            out["explanation"] = str(data.get("explanation") or "").strip()[:500]
        elif payload.closes_in_hours:
            hours = max(1, min(int(payload.closes_in_hours), 24 * 14))
            out["closes_at"] = (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()
    elif ptype == "flashcards":
        if not title:
            raise HTTPException(400, "Give the flashcard set a title")
        cards = []
        for c in (data.get("cards") if isinstance(data.get("cards"), list) else []):
            if isinstance(c, dict) and str(c.get("front") or "").strip() and str(c.get("back") or "").strip():
                cards.append({"front": str(c["front"]).strip()[:200], "back": str(c["back"]).strip()[:300]})
        if len(cards) < 2 or len(cards) > 30:
            raise HTTPException(400, "A flashcard set needs between 2 and 30 cards")
        out = {"cards": cards}
    return title, body or None, out


def _feed_image_ok(url):
    if not url:
        return None
    url = str(url).strip()
    if len(url) > 500 or not url.startswith("https://"):
        raise HTTPException(400, "Photos must be uploaded with the app first")
    return url


def _feed_serialize(db, posts, viewer: str, admin: bool = False) -> list:
    if not posts:
        return []
    ids = [p.id for p in posts]
    low = viewer.lower() if viewer else ""
    reactions: dict = {}
    for pid, kind, n in db.query(FeedReaction.post_id, FeedReaction.kind, sa_func.count(FeedReaction.id)) \
            .filter(FeedReaction.post_id.in_(ids)).group_by(FeedReaction.post_id, FeedReaction.kind).all():
        reactions.setdefault(pid, {})[kind] = int(n)
    mine_react = {pid: kind for pid, kind in db.query(FeedReaction.post_id, FeedReaction.kind)
                  .filter(FeedReaction.post_id.in_(ids), sa_func.lower(FeedReaction.username) == low).all()} if low else {}
    votes: dict = {}
    for pid, choice, n in db.query(FeedVote.post_id, FeedVote.choice, sa_func.count(FeedVote.id)) \
            .filter(FeedVote.post_id.in_(ids)).group_by(FeedVote.post_id, FeedVote.choice).all():
        votes.setdefault(pid, {})[int(choice)] = int(n)
    mine_vote = {pid: int(c) for pid, c in db.query(FeedVote.post_id, FeedVote.choice)
                 .filter(FeedVote.post_id.in_(ids), sa_func.lower(FeedVote.username) == low).all()} if low else {}
    comments = {pid: int(n) for pid, n in db.query(FeedComment.post_id, sa_func.count(FeedComment.id))
                .filter(FeedComment.post_id.in_(ids), FeedComment.is_deleted.is_(False)).group_by(FeedComment.post_id).all()}
    names = [f"post:{i}" for i in ids]
    shares = {n: int(c) for n, c in db.query(LiveClassMessage.file_name, sa_func.count(LiveClassMessage.id))
              .filter(LiveClassMessage.message_type == "post", LiveClassMessage.file_name.in_(names))
              .group_by(LiveClassMessage.file_name).all()}
    pics = {u.lower(): p for u, p in db.query(User.username, User.profile_pic)
            .filter(sa_func.lower(User.username).in_(list({p.author.lower() for p in posts}))).all()}
    now = now_str()
    out = []
    for p in posts:
        try:
            data = json.loads(p.data_json) if p.data_json else None
        except Exception:
            data = None
        counts = reactions.get(p.id, {})
        img_gone = p.image_url == _V3_EXPIRED
        item = {
            "id": p.id, "room": p.room, "author": p.author, "author_pic": pics.get((p.author or "").lower()),
            "post_type": p.post_type, "title": p.title, "text": p.text,
            "image_url": None if img_gone else p.image_url, "image_expired": img_gone,
            "image_expires_at": _v3_media_expiry(p.created_at, bool(p.image_url) and not img_gone),
            "created_at": p.created_at, "mine": (p.author or "").lower() == low,
            "reactions": counts, "reaction_total": sum(counts.values()), "my_reaction": mine_react.get(p.id),
            "shares": shares.get(f"post:{p.id}", 0), "comments": comments.get(p.id, 0),
        }
        if p.post_type in ("poll", "quiz") and data:
            options = data.get("options", [])
            tally = votes.get(p.id, {})
            my = mine_vote.get(p.id)
            item["options"] = options
            item["my_vote"] = my
            if p.post_type == "poll":
                item["closed"] = bool(data.get("closes_at") and data["closes_at"] <= now)
                item["closes_at"] = data.get("closes_at")
                item["votes"] = [tally.get(i, 0) for i in range(len(options))]
                item["vote_total"] = sum(item["votes"])
            else:  # quiz: the answer is only shown after you have answered (or to the author / admin)
                item["answers_total"] = sum(tally.values())
                if my is not None or item["mine"] or admin:
                    item["correct"] = data.get("correct")
                    item["explanation"] = data.get("explanation") or None
                    item["votes"] = [tally.get(i, 0) for i in range(len(options))]
        elif p.post_type == "flashcards" and data:
            item["cards"] = data.get("cards", [])
        out.append(item)
    return out


def _feed_room_ok(room, username):
    room = (room or "").strip() or None
    if room:
        room = _lc_clean_room(room)
        if room.startswith("dm__"):
            raise HTTPException(400, "Posts cannot be made inside a private chat")
    return room


@app.post("/feed/posts")
def v3_feed_create(payload: V3FeedPostIn, background_tasks: BackgroundTasks,
                   user=Depends(_game_user), db=Depends(get_db)):
    _v3_block_if_restricted("hub", user.username)
    room = _feed_room_ok(payload.room, user.username)
    if payload.post_type == "story":
        room = None                       # stories belong to everyone, not to one class
    title, body, data = _feed_clean(payload)
    image = _feed_image_ok(payload.image_url)
    now = _time.time()
    recent = [t for t in _feed_recent.get(user.username, []) if now - t < 600]
    if len(recent) >= 5:
        raise HTTPException(429, "You are posting too fast. Please wait a few minutes.")
    recent.append(now)
    _feed_recent[user.username] = recent
    p = FeedPost(room=room, author=user.username, post_type=payload.post_type, title=title, text=body,
                 image_url=image, data_json=json.dumps(data) if data is not None else None,
                 created_at=now_str(), is_deleted=False)
    db.add(p)
    db.commit()
    db.refresh(p)
    _v3_maybe_expire(background_tasks)
    return {"success": True, "post": _feed_serialize(db, [p], user.username)[0]}


@app.get("/feed/posts")
def v3_feed_list(background_tasks: BackgroundTasks, room: str = "", before_id: int = 0, limit: int = 20,
                 post_type: str = "", scope: str = "", search: str = Query("", alias="q"),
                 user=Depends(_game_user), db=Depends(get_db)):
    """room = a class/group name, or leave empty for the open feed.
    scope: mine (my posts everywhere) | friends (posts by my friends) | empty (everyone).
    q: words or a #hashtag to look for."""
    limit = min(max(1, limit), 50)
    room = _feed_room_ok(room, user.username)
    q = db.query(FeedPost).filter(FeedPost.is_deleted.is_(False), FeedPost.post_type != "story")
    if scope == "mine":
        q = q.filter(sa_func.lower(FeedPost.author) == user.username.lower())
    else:
        q = q.filter(FeedPost.room == room) if room else q.filter(FeedPost.room.is_(None))
        if scope == "friends":
            fr = [u for (u,) in db.query(User.username).join(Friendship, Friendship.friend_id == User.id)
                  .filter(Friendship.user_id == user.id, Friendship.status == "accepted").all()]
            q = q.filter(sa_func.lower(FeedPost.author).in_([f.lower() for f in fr] or [""]))
    if post_type in _FEED_TYPES and post_type != "story":
        q = q.filter(FeedPost.post_type == post_type)
    term = (search or "").strip()[:60]
    if term:
        safe = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like = f"%{safe}%"
        q = q.filter((FeedPost.text.ilike(like, escape="\\")) | (FeedPost.title.ilike(like, escape="\\")))
    if before_id and before_id > 0:
        q = q.filter(FeedPost.id < before_id)
    rows = q.order_by(FeedPost.id.desc()).limit(limit + 1).all()
    more = len(rows) > limit
    rows = rows[:limit]
    _v3_maybe_expire(background_tasks)
    return {"posts": _feed_serialize(db, rows, user.username), "has_more": more}


@app.get("/feed/posts/{post_id}")
def v3_feed_get(post_id: int, user=Depends(_game_user), db=Depends(get_db)):
    p = db.query(FeedPost).filter(FeedPost.id == post_id, FeedPost.is_deleted.is_(False)).first()
    if not p:
        raise HTTPException(404, "This post is no longer available")
    return _feed_serialize(db, [p], user.username)[0]


def _feed_get_live(db, post_id):
    p = db.query(FeedPost).filter(FeedPost.id == post_id, FeedPost.is_deleted.is_(False)).first()
    if not p:
        raise HTTPException(404, "This post is no longer available")
    return p


@app.post("/feed/posts/{post_id}/react")
def v3_feed_react(post_id: int, payload: V3FeedReact, user=Depends(_game_user), db=Depends(get_db)):
    p = _feed_get_live(db, post_id)
    kind = (payload.kind or "").strip().lower() or None
    if kind and kind not in _FEED_REACTIONS:
        raise HTTPException(400, "Reaction must be one of: " + ", ".join(_FEED_REACTIONS))
    r = db.query(FeedReaction).filter(FeedReaction.post_id == p.id, FeedReaction.username == user.username).first()
    if kind is None:
        if r:
            db.delete(r)
    elif r:
        r.kind = kind
    else:
        db.add(FeedReaction(post_id=p.id, username=user.username, kind=kind, created_at=now_str()))
    try:
        db.commit()
    except _V3IntegrityError:
        db.rollback()
    item = _feed_serialize(db, [p], user.username)[0]
    return {"success": True, "reactions": item["reactions"], "reaction_total": item["reaction_total"],
            "my_reaction": item["my_reaction"]}


@app.post("/feed/posts/{post_id}/vote")
def v3_feed_vote(post_id: int, payload: V3FeedVoteIn, user=Depends(_game_user), db=Depends(get_db)):
    """Vote in a poll (you can change your vote until it closes) or answer a quiz (one try)."""
    _v3_block_if_restricted("hub", user.username)
    p = _feed_get_live(db, post_id)
    if p.post_type not in ("poll", "quiz") or not p.data_json:
        raise HTTPException(400, "This post has nothing to vote on")
    data = json.loads(p.data_json)
    options = data.get("options", [])
    if payload.choice < 0 or payload.choice >= len(options):
        raise HTTPException(400, "That option does not exist")
    if p.post_type == "poll" and data.get("closes_at") and data["closes_at"] <= now_str():
        raise HTTPException(400, "This poll is closed")
    v = db.query(FeedVote).filter(FeedVote.post_id == p.id, FeedVote.username == user.username).first()
    if v and p.post_type == "quiz":
        raise HTTPException(400, "You have already answered this quiz")
    if v:
        v.choice = payload.choice
    else:
        db.add(FeedVote(post_id=p.id, username=user.username, choice=payload.choice, created_at=now_str()))
    try:
        db.commit()
    except _V3IntegrityError:
        db.rollback()
        raise HTTPException(400, "You have already answered this one")
    item = _feed_serialize(db, [p], user.username)[0]
    out = {"success": True, "my_vote": payload.choice, "votes": item.get("votes")}
    if p.post_type == "quiz":
        out.update({"correct_index": data.get("correct"), "is_correct": payload.choice == data.get("correct"),
                    "explanation": item.get("explanation")})
    return out


@app.post("/feed/posts/{post_id}/share")
async def v3_feed_share(post_id: int, payload: V3FeedShare, user=Depends(_game_user), db=Depends(get_db)):
    """Share a post into a chat (class, group or a personal chat you are in) as a post card."""
    _v3_block_if_restricted("hub", user.username)
    p = _feed_get_live(db, post_id)
    room = _lc_clean_room(payload.room)
    _lc_check_access(room, user.username)
    if not _lc_rate_ok(user.username):
        raise HTTPException(429, "You are sending messages too fast. Slow down a little.")
    snippet = (p.title or p.text or "Shared post")[:140]
    m = LiveClassMessage(room=room, sender_username=user.username, message_text=snippet, message_type="post",
                         file_name=f"post:{p.id}", created_at=now_str())
    db.add(m)
    db.commit()
    db.refresh(m)
    out = _lc_serialize(m, {})
    await _lc_broadcast(room, out)
    return {"success": True, "message": out}


@app.delete("/feed/posts/{post_id}")
def v3_feed_delete(post_id: int, background_tasks: BackgroundTasks, user=Depends(_game_user), db=Depends(get_db)):
    p = db.query(FeedPost).filter(FeedPost.id == post_id).first()
    if not p or p.is_deleted:
        raise HTTPException(404, "Post not found")
    if (p.author or "").lower() != user.username.lower():
        raise HTTPException(403, "You can only delete your own posts")
    p.is_deleted = True
    img = p.image_url
    db.commit()
    if img and img != _V3_EXPIRED:
        background_tasks.add_task(_v3_cloudinary_destroy, img)
    return {"success": True}


@app.get("/admin/feed/posts")
def v3_admin_feed_list(room: str = "", limit: int = 50, db=Depends(get_db), admin_ok: bool = Depends(verify_admin)):
    limit = min(max(1, limit), 200)
    q = db.query(FeedPost).filter(FeedPost.is_deleted.is_(False))
    if room == "__open__":
        q = q.filter(FeedPost.room.is_(None))
    elif room:
        q = q.filter(FeedPost.room == room)
    rows = q.order_by(FeedPost.id.desc()).limit(limit).all()
    return {"posts": _feed_serialize(db, rows, "", admin=True)}


@app.delete("/admin/feed/posts/{post_id}")
def v3_admin_feed_delete(post_id: int, background_tasks: BackgroundTasks, db=Depends(get_db),
                         admin_ok: bool = Depends(verify_admin)):
    p = db.query(FeedPost).filter(FeedPost.id == post_id).first()
    if not p:
        raise HTTPException(404, "Post not found")
    p.is_deleted = True
    img = p.image_url
    db.commit()
    if img and img != _V3_EXPIRED:
        background_tasks.add_task(_v3_cloudinary_destroy, img)
    return {"success": True}


# ------------------------------------------------------------
# 12. QUIZ BATTLES (2 or more players, same questions, same time)
# Built on the questions of the existing games (Trivia, Science Sort, Word Puzzles
# and any game made in the admin panel). The server keeps the clock, hides the
# answers until each question is over, scores speed fairly and pays the coins.
# ------------------------------------------------------------
_BT_LEAD = 4.0                  # seconds of countdown before question 1
_BT_GAP = 3.0                   # seconds the right answer is shown between questions
_BT_KINDS = ("trivia", "science", "puzzle")
BATTLE_DAILY_COIN_CAP = 150
_BT_CODE_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


class BattleRoom(Base):
    __tablename__ = "battle_rooms"
    code        = Column(String, primary_key=True)
    host        = Column(String, index=True)
    game        = Column(String)
    level       = Column(String, default="Junior")
    n           = Column(Integer, default=10)
    secs        = Column(Integer, default=15)
    max_players = Column(Integer, default=10)
    is_public   = Column(Boolean, default=False)
    status      = Column(String, default="lobby")      # lobby / playing / finished / cancelled
    item_ids    = Column(Text, default="[]")
    started_ts  = Column(Float, nullable=True)
    created_ts  = Column(Float, index=True)
    rewarded    = Column(Boolean, default=False)


class BattlePlayer(Base):
    __tablename__ = "battle_players"
    __table_args__ = (UniqueConstraint("code", "username", name="uq_battle_player"),)
    id        = Column(Integer, primary_key=True, index=True)
    code      = Column(String, index=True)
    username  = Column(String, index=True)
    joined_ts = Column(Float)
    score     = Column(Integer, default=0)
    correct   = Column(Integer, default=0)
    answers   = Column(Text, default="{}")
    coins     = Column(Integer, default=0)
    coin_day  = Column(String, nullable=True)


class V3BattleCreate(BaseModel):
    game: str = "trivia"
    level: str = "Junior"
    questions: int = 10
    secs: int = 15
    max_players: int = 10
    public: bool = False
    invite: List[str] = []


class V3BattleJoin(BaseModel):
    code: str


class V3BattleAnswer(BaseModel):
    idx: int
    choice_index: Optional[int] = None


# ---------- the clock (pure functions, easy to test) ----------
def _bt_slot(secs: int) -> float:
    return secs + _BT_GAP


def _bt_q_start(started_ts: float, secs: int, i: int) -> float:
    return started_ts + _BT_LEAD + i * _bt_slot(secs)


def _bt_phase(started_ts: float, secs: int, n: int, now: float) -> dict:
    """Where the battle is right now: countdown / question / reveal / finished."""
    t = now - started_ts
    if t < _BT_LEAD:
        return {"phase": "countdown", "idx": 0, "ends_in": round(_BT_LEAD - t, 2)}
    slot = _bt_slot(secs)
    i = int((t - _BT_LEAD) // slot)
    if i >= n:
        return {"phase": "finished", "idx": n - 1, "ends_in": 0}
    within = (t - _BT_LEAD) - i * slot
    if within < secs:
        return {"phase": "question", "idx": i, "ends_in": round(secs - within, 2)}
    return {"phase": "reveal", "idx": i, "ends_in": round(slot - within, 2)}


def _bt_points(tier: int, secs: int, used: float) -> int:
    """100 for a right answer + 10 per difficulty level + up to 50 for speed."""
    remaining = max(0.0, secs - max(0.0, used))
    return 100 + 10 * int(tier or 0) + int(round(50 * remaining / float(secs)))


def _bt_question(room, i: int, item):
    """(question text, options, right option text, tier) with the SAME option order for every player."""
    p = json.loads(item.payload)
    kind = _gm_kind(room.game)
    rng = random.Random(f"{room.code}:{i}")
    if kind in ("trivia", "puzzle"):
        q, opts = p["q"], [p["a"]] + list(p["w"])[:3]
    elif kind == "science":
        q = f"Which group does {p['item']} belong to?"
        others = [c for c in p["cats"] if c != p["a"]]
        rng.shuffle(others)
        opts = [p["a"]] + others[:3]
    else:
        return None
    rng.shuffle(opts)
    return q, opts, _gm_answer_text(room.game, p), (item.tier if item.tier in (0, 1, 2) else 0), p


def _bt_items(db, room):
    ids = json.loads(room.item_ids or "[]")
    by_id = {it.id: it for it in db.query(GameItem).filter(GameItem.id.in_(ids)).all()} if ids else {}
    return [by_id.get(i) for i in ids]


def _bt_new_code(db) -> str:
    for _ in range(20):
        code = "".join(random.choice(_BT_CODE_CHARS) for _ in range(6))
        if not db.query(BattleRoom.code).filter(BattleRoom.code == code).first():
            return code
    raise HTTPException(503, "Could not make a battle code. Try again.")


def _bt_get(db, code: str, lock: bool = False):
    q = db.query(BattleRoom).filter(BattleRoom.code == (code or "").strip().upper())
    room = (q.with_for_update().first() if lock else q.first())
    if not room:
        raise HTTPException(404, "No battle with that code")
    return room


def _bt_players(db, code: str):
    return db.query(BattlePlayer).filter(BattlePlayer.code == code).order_by(BattlePlayer.joined_ts.asc()).all()


def _bt_finish(db, room, players):
    """Close the battle once and pay the coins. Only call with the room row locked."""
    if room.status == "finished":
        return
    room.status = "finished"
    if room.rewarded:
        return
    room.rewarded = True
    if len(players) < 2:
        return
    today = today_str()
    top = max((p.score for p in players), default=0)
    for p in players:
        coins = 0
        if p.correct >= 1:
            coins = 5 + 2 * p.correct
            if p.score == top and top > 0:
                coins += 20
        used = db.query(sa_func.coalesce(sa_func.sum(BattlePlayer.coins), 0)) \
            .filter(BattlePlayer.username == p.username, BattlePlayer.coin_day == today, BattlePlayer.id != p.id).scalar() or 0
        coins = max(0, min(coins, BATTLE_DAILY_COIN_CAP - int(used)))
        p.coins, p.coin_day = coins, today
        if coins:
            u = db.query(User).filter(User.username == p.username).first()
            if u:
                u.coins = (u.coins or 0) + coins


def _bt_state(db, room, viewer: str, now: float) -> dict:
    players = _bt_players(db, room.code)
    mine = next((p for p in players if p.username.lower() == viewer.lower()), None)
    if room.status == "lobby" and now - (room.created_ts or now) > 1800:
        room.status = "cancelled"
        db.commit()
    state = {
        "code": room.code, "game": room.game, "level": room.level, "n": room.n, "secs": room.secs,
        "status": room.status, "host": room.host, "is_host": room.host.lower() == viewer.lower(),
        "is_public": bool(room.is_public), "max_players": room.max_players, "server_time": now,
        "joined": mine is not None,
        "players": [], "phase": room.status,
    }
    ranked = sorted(players, key=lambda p: (-p.score, -p.correct, p.joined_ts))
    for r, p in enumerate(ranked, start=1):
        answers = json.loads(p.answers or "{}")
        state["players"].append({"username": p.username, "score": p.score, "correct": p.correct, "answered": len(answers),
                                 "rank": r, "me": p.username.lower() == viewer.lower()})
    if room.status != "playing" and room.status != "finished":
        return state
    items = _bt_items(db, room)
    if room.status == "playing":
        ph = _bt_phase(room.started_ts, room.secs, room.n, now)
        if ph["phase"] == "finished":
            locked = _bt_get(db, room.code, lock=True)
            _bt_finish(db, locked, _bt_players(db, room.code))
            db.commit()
            room = locked
            players = _bt_players(db, room.code)
            ranked = sorted(players, key=lambda p: (-p.score, -p.correct, p.joined_ts))
            state["status"] = "finished"
        else:
            state.update(ph)
    if state["status"] == "finished":
        state["phase"] = "finished"
        top = ranked[0].score if ranked else 0
        state["final"] = {
            "ranking": [{"rank": r, "username": p.username, "score": p.score, "correct": p.correct, "coins": p.coins or 0}
                        for r, p in enumerate(ranked, start=1)],
            "winners": [p.username for p in ranked if p.score == top and top > 0],
            "my_coins": (mine.coins or 0) if mine else 0,
        }
        state["players"] = [{"username": p.username, "score": p.score, "correct": p.correct, "answered": len(json.loads(p.answers or "{}")),
                             "rank": r, "me": p.username.lower() == viewer.lower()} for r, p in enumerate(ranked, start=1)]
        return state
    idx = state.get("idx", 0)
    item = items[idx] if 0 <= idx < len(items) else None
    built = _bt_question(room, idx, item) if item else None
    my_answers = json.loads(mine.answers or "{}") if mine else {}
    if state["phase"] == "question" and built:
        q, opts, _correct, tier, p = built
        state["question"] = {"idx": idx, "q": q, "options": opts, "tier": tier, "type": p.get("type")}
        state["answered_this"] = str(idx) in my_answers
    elif state["phase"] == "reveal" and built:
        q, opts, correct, tier, _p = built
        results = {}
        for pl in players:
            a = json.loads(pl.answers or "{}").get(str(idx))
            results[pl.username] = {"choice": a.get("c") if a else None, "ok": bool(a and a.get("ok")),
                                    "points": a.get("pts", 0) if a else 0}
        state["reveal"] = {"idx": idx, "q": q, "options": opts, "correct": correct,
                           "correct_index": opts.index(correct) if correct in opts else None, "results": results}
    return state


@app.post("/battle/create")
def v3_battle_create(payload: V3BattleCreate, background_tasks: BackgroundTasks,
                     user=Depends(_game_user), db=Depends(get_db)):
    game = (payload.game or "").strip()
    cfg = _gm_cfg_for(game)
    if not cfg or cfg.get("lesson_only") or _gm_kind(game) not in _BT_KINDS:
        raise HTTPException(400, "That game cannot be played as a battle. Try Trivia, Science Sort or Word Puzzles.")
    level = payload.level if payload.level in GAME_LEVELS else "Junior"
    n = max(5, min(int(payload.questions or 10), 20))
    secs = max(8, min(int(payload.secs or 15), 40))
    maxp = max(2, min(int(payload.max_players or 10), 20))
    open_one = db.query(BattleRoom).filter(BattleRoom.host == user.username, BattleRoom.status.in_(["lobby", "playing"]),
                                           BattleRoom.created_ts > _time.time() - 3600).first()
    if open_one:
        raise HTTPException(409, f"You already have a battle open (code {open_one.code}). Finish or cancel it first.")
    tiers = ([0] * int(round(n * 0.3)) + [1] * int(round(n * 0.4)))
    tiers += [2] * (n - len(tiers))
    chosen = _gm_pick(db, user.id, game, level, tiers)
    if len(chosen) < 5:
        _gm_generate(game, level)
        chosen = _gm_pick(db, user.id, game, level, tiers)
    if len(chosen) < 5:
        raise HTTPException(503, "Not enough questions yet. Please try again in a moment.")
    background_tasks.add_task(_gm_topup, game, level)
    now = _time.time()
    room = BattleRoom(code=_bt_new_code(db), host=user.username, game=game, level=level, n=len(chosen), secs=secs,
                      max_players=maxp, is_public=bool(payload.public), status="lobby",
                      item_ids=json.dumps([c.id for c in chosen]), created_ts=now)
    db.add(room)
    db.add(BattlePlayer(code=room.code, username=user.username, joined_ts=now, answers="{}"))
    db.commit()
    invited = 0
    for name in [x for x in (payload.invite or []) if x][:10]:
        u = db.query(User).filter(sa_func.lower(User.username) == name.strip().lower()).first()
        if not u or u.username.lower() == user.username.lower():
            continue
        msg = AdminMessage(title=f"⚔️ Quiz battle from {user.username}",
                           body=f"{user.username} challenged you to a quiz battle. Open Quiz Battles and join with the code {room.code}.",
                           kind="other", target_username=u.username, pinned=False, send_push=True, push_sent=False,
                           created_by="battle", created_at=now_str())
        db.add(msg)
        db.commit()
        db.refresh(msg)
        background_tasks.add_task(_v3_push_message_job, msg.id)
        invited += 1
    state = _bt_state(db, room, user.username, now)
    state["invited"] = invited
    return state


@app.post("/battle/join")
def v3_battle_join(payload: V3BattleJoin, user=Depends(_game_user), db=Depends(get_db)):
    room = _bt_get(db, payload.code, lock=True)
    now = _time.time()
    mine = db.query(BattlePlayer).filter(BattlePlayer.code == room.code, BattlePlayer.username == user.username).first()
    if not mine:
        if room.status != "lobby" or now - (room.created_ts or now) > 1800:
            raise HTTPException(400, "This battle has already started or is closed")
        if db.query(sa_func.count(BattlePlayer.id)).filter(BattlePlayer.code == room.code).scalar() >= room.max_players:
            raise HTTPException(400, "This battle is full")
        db.add(BattlePlayer(code=room.code, username=user.username, joined_ts=now, answers="{}"))
        try:
            db.commit()
        except _V3IntegrityError:
            db.rollback()
    else:
        db.commit()
    return _bt_state(db, _bt_get(db, room.code), user.username, now)


@app.post("/battle/{code}/start")
def v3_battle_start(code: str, user=Depends(_game_user), db=Depends(get_db)):
    room = _bt_get(db, code, lock=True)
    if room.host.lower() != user.username.lower():
        raise HTTPException(403, "Only the person who created the battle can start it")
    if room.status != "lobby":
        raise HTTPException(400, "This battle has already started or is closed")
    if db.query(sa_func.count(BattlePlayer.id)).filter(BattlePlayer.code == room.code).scalar() < 2:
        raise HTTPException(400, "You need at least 2 players to start")
    room.status = "playing"
    room.started_ts = _time.time()
    db.commit()
    return _bt_state(db, _bt_get(db, room.code), user.username, _time.time())


@app.post("/battle/{code}/leave")
def v3_battle_leave(code: str, user=Depends(_game_user), db=Depends(get_db)):
    room = _bt_get(db, code, lock=True)
    if room.status == "lobby":
        if room.host.lower() == user.username.lower():
            room.status = "cancelled"
        else:
            db.query(BattlePlayer).filter(BattlePlayer.code == room.code, BattlePlayer.username == user.username) \
                .delete(synchronize_session=False)
    db.commit()
    return {"success": True}


@app.post("/battle/{code}/answer")
def v3_battle_answer(code: str, payload: V3BattleAnswer, user=Depends(_game_user), db=Depends(get_db)):
    now = _time.time()
    room = _bt_get(db, code)
    if room.status != "playing":
        raise HTTPException(400, "This battle is not running")
    pl = db.query(BattlePlayer).filter(BattlePlayer.code == room.code, BattlePlayer.username == user.username) \
        .with_for_update().first()
    if not pl:
        raise HTTPException(403, "You are not in this battle")
    ph = _bt_phase(room.started_ts, room.secs, room.n, now)
    i = payload.idx
    if i < 0 or i >= room.n:
        raise HTTPException(400, "No such question")
    start_i = _bt_q_start(room.started_ts, room.secs, i)
    if now < start_i - 0.5 or now > start_i + room.secs + 1.5:
        raise HTTPException(400, "Time is up for this question")
    answers = json.loads(pl.answers or "{}")
    if str(i) in answers:
        a = answers[str(i)]
        return {"idx": i, "correct": bool(a.get("ok")), "points": a.get("pts", 0), "score": pl.score}
    items = _bt_items(db, room)
    item = items[i] if i < len(items) else None
    built = _bt_question(room, i, item) if item else None
    if not built:
        raise HTTPException(404, "Question not found")
    _q, opts, _correct, tier, p = built
    ci = payload.choice_index
    ok = False
    if ci is not None and 0 <= ci < len(opts):
        ok = bool(_gm_check(room.game, p, opts[ci]))
    pts = _bt_points(tier, room.secs, now - start_i) if ok else 0
    answers[str(i)] = {"c": ci, "ok": ok, "pts": pts, "t": round(now - start_i, 2)}
    pl.answers = json.dumps(answers)
    pl.score = (pl.score or 0) + pts
    pl.correct = (pl.correct or 0) + (1 if ok else 0)
    db.commit()
    return {"idx": i, "correct": ok, "points": pts, "score": pl.score}


@app.get("/battle/{code}")
def v3_battle_state(code: str, user=Depends(_game_user), db=Depends(get_db)):
    room = _bt_get(db, code)
    state = _bt_state(db, room, user.username, _time.time())
    if not state["joined"]:
        # outsiders only see the lobby card so they can decide to join
        card = {k: state[k] for k in ("code", "game", "level", "n", "secs", "status", "host", "max_players",
                                      "joined", "server_time")}
        card["players"] = [{"username": p["username"]} for p in state["players"]]
        return card
    return state


@app.get("/battles/open")
def v3_battles_open(game: str = "", db=Depends(get_db), user=Depends(_game_user)):
    """Public battles waiting for players (so anyone can find a game)."""
    now = _time.time()
    q = db.query(BattleRoom).filter(BattleRoom.status == "lobby", BattleRoom.is_public.is_(True),
                                    BattleRoom.created_ts > now - 1800)
    if game:
        q = q.filter(BattleRoom.game == game)
    out = []
    for r in q.order_by(BattleRoom.created_ts.desc()).limit(20).all():
        n = db.query(sa_func.count(BattlePlayer.id)).filter(BattlePlayer.code == r.code).scalar() or 0
        if n < r.max_players:
            out.append({"code": r.code, "game": r.game, "level": r.level, "questions": r.n, "secs": r.secs,
                        "host": r.host, "players": int(n), "max_players": r.max_players})
    return {"battles": out}


@app.get("/battles/mine")
def v3_battles_mine(user=Depends(_game_user), db=Depends(get_db)):
    rows = db.query(BattleRoom, BattlePlayer).join(BattlePlayer, BattlePlayer.code == BattleRoom.code) \
        .filter(BattlePlayer.username == user.username).order_by(BattleRoom.created_ts.desc()).limit(20).all()
    return {"battles": [{"code": r.code, "game": r.game, "status": r.status, "host": r.host, "my_score": p.score,
                         "my_coins": p.coins or 0, "created_ts": r.created_ts} for r, p in rows]}


@app.get("/admin/battles")
def v3_admin_battles(limit: int = 50, db=Depends(get_db), admin_ok: bool = Depends(verify_admin)):
    rows = db.query(BattleRoom).order_by(BattleRoom.created_ts.desc()).limit(min(max(1, limit), 200)).all()
    return {"battles": [{"code": r.code, "game": r.game, "host": r.host, "status": r.status, "questions": r.n,
                         "players": db.query(sa_func.count(BattlePlayer.id)).filter(BattlePlayer.code == r.code).scalar() or 0,
                         "created_ts": r.created_ts} for r in rows]}


@app.delete("/admin/battles/{code}")
def v3_admin_battle_delete(code: str, db=Depends(get_db), admin_ok: bool = Depends(verify_admin)):
    room = _bt_get(db, code)
    db.query(BattlePlayer).filter(BattlePlayer.code == room.code).delete(synchronize_session=False)
    db.delete(room)
    db.commit()
    return {"success": True}


try:
    Base.metadata.create_all(bind=engine, checkfirst=True)
    print("[DB] v3 tables (restrictions, feed, battles) created/verified [OK]")
except Exception as e:
    print(f"[DB ERROR - v3 create_all #2] {e}")


# ------------------------------------------------------------
# 13. REALCHAT LIVE EXTRAS: comments, stories (2 hours), reports, my stats
# ------------------------------------------------------------
class FeedComment(Base):
    __tablename__ = "feed_comments"
    id         = Column(Integer, primary_key=True, index=True)
    post_id    = Column(Integer, ForeignKey("feed_posts.id"), index=True)
    author     = Column(String, index=True)
    text       = Column(Text)
    created_at = Column(String)
    is_deleted = Column(Boolean, default=False)


class FeedReport(Base):
    __tablename__ = "feed_reports"
    __table_args__ = (UniqueConstraint("post_id", "reporter", name="uq_feed_report"),)
    id         = Column(Integer, primary_key=True, index=True)
    post_id    = Column(Integer, ForeignKey("feed_posts.id"), index=True)
    reporter   = Column(String, index=True)
    reason     = Column(String)
    created_at = Column(String)


class V3CommentIn(BaseModel):
    text: str


class V3ReportIn(BaseModel):
    reason: str = "Inappropriate content"


STORY_HOURS = 2


@app.get("/feed/posts/{post_id}/comments")
def v3_feed_comments(post_id: int, limit: int = 100, user=Depends(_game_user), db=Depends(get_db)):
    _feed_get_live(db, post_id)
    rows = db.query(FeedComment).filter(FeedComment.post_id == post_id, FeedComment.is_deleted.is_(False)) \
        .order_by(FeedComment.id.asc()).limit(min(max(1, limit), 300)).all()
    pics = {u.lower(): p for u, p in db.query(User.username, User.profile_pic)
            .filter(sa_func.lower(User.username).in_(list({r.author.lower() for r in rows}) or [""])).all()}
    return {"comments": [{"id": c.id, "author": c.author, "author_pic": pics.get((c.author or "").lower()),
                          "text": c.text, "created_at": c.created_at,
                          "mine": (c.author or "").lower() == user.username.lower()} for c in rows]}


@app.post("/feed/posts/{post_id}/comments")
def v3_feed_comment_add(post_id: int, payload: V3CommentIn, user=Depends(_game_user), db=Depends(get_db)):
    _v3_block_if_restricted("hub", user.username)
    _feed_get_live(db, post_id)
    text_value = (payload.text or "").strip()
    if not text_value:
        raise HTTPException(400, "Write a comment first")
    if len(text_value) > 500:
        raise HTTPException(400, "Comments can be up to 500 characters")
    if not _lc_rate_ok(user.username):
        raise HTTPException(429, "You are commenting too fast. Slow down a little.")
    c = FeedComment(post_id=post_id, author=user.username, text=text_value, created_at=now_str(), is_deleted=False)
    db.add(c)
    db.commit()
    db.refresh(c)
    total = db.query(sa_func.count(FeedComment.id)).filter(FeedComment.post_id == post_id,
                                                           FeedComment.is_deleted.is_(False)).scalar() or 0
    return {"success": True, "total": int(total),
            "comment": {"id": c.id, "author": c.author, "text": c.text, "created_at": c.created_at, "mine": True}}


@app.delete("/feed/comments/{comment_id}")
def v3_feed_comment_delete(comment_id: int, user=Depends(_game_user), db=Depends(get_db)):
    c = db.query(FeedComment).filter(FeedComment.id == comment_id).first()
    if not c or c.is_deleted:
        raise HTTPException(404, "Comment not found")
    post = db.query(FeedPost).filter(FeedPost.id == c.post_id).first()
    me = user.username.lower()
    if (c.author or "").lower() != me and not (post and (post.author or "").lower() == me):
        raise HTTPException(403, "You can only delete your own comments")
    c.is_deleted = True
    db.commit()
    return {"success": True}


@app.post("/feed/posts/{post_id}/report")
def v3_feed_report(post_id: int, payload: V3ReportIn, user=Depends(_game_user), db=Depends(get_db)):
    p = _feed_get_live(db, post_id)
    if (p.author or "").lower() == user.username.lower():
        raise HTTPException(400, "You cannot report your own post")
    reason = (payload.reason or "Inappropriate content").strip()[:120]
    if not db.query(FeedReport.id).filter(FeedReport.post_id == post_id, FeedReport.reporter == user.username).first():
        db.add(FeedReport(post_id=post_id, reporter=user.username, reason=reason, created_at=now_str()))
        try:
            db.commit()
        except _V3IntegrityError:
            db.rollback()
    return {"success": True}


@app.get("/admin/feed/reports")
def v3_admin_feed_reports(db=Depends(get_db), admin_ok: bool = Depends(verify_admin)):
    """Posts that people reported, most reported first."""
    rows = db.query(FeedReport.post_id, sa_func.count(FeedReport.id), sa_func.max(FeedReport.created_at)) \
        .group_by(FeedReport.post_id).order_by(sa_func.count(FeedReport.id).desc()).limit(100).all()
    out = []
    for pid, n, last in rows:
        p = db.query(FeedPost).filter(FeedPost.id == pid).first()
        if not p or p.is_deleted:
            continue
        reasons = [r for (r,) in db.query(FeedReport.reason).filter(FeedReport.post_id == pid).limit(10).all()]
        out.append({"post_id": pid, "reports": int(n), "last_report": last, "author": p.author, "type": p.post_type,
                    "text": (p.text or p.title or "")[:200], "has_image": bool(p.image_url), "reasons": reasons})
    return {"reports": out}


@app.delete("/admin/feed/reports/{post_id}")
def v3_admin_feed_reports_clear(post_id: int, db=Depends(get_db), admin_ok: bool = Depends(verify_admin)):
    db.query(FeedReport).filter(FeedReport.post_id == post_id).delete(synchronize_session=False)
    db.commit()
    return {"success": True}


@app.get("/feed/stories")
def v3_feed_stories(user=Depends(_game_user), db=Depends(get_db)):
    """Short status posts (text and/or photo). They disappear from the app after 2 hours."""
    since = (datetime.now(timezone.utc) - timedelta(hours=STORY_HOURS)).isoformat()
    rows = db.query(FeedPost).filter(FeedPost.post_type == "story", FeedPost.is_deleted.is_(False),
                                     FeedPost.created_at >= since).order_by(FeedPost.id.asc()).limit(300).all()
    pics = {u.lower(): p for u, p in db.query(User.username, User.profile_pic)
            .filter(sa_func.lower(User.username).in_(list({r.author.lower() for r in rows}) or [""])).all()}
    groups: dict = {}
    for r in rows:
        g = groups.setdefault((r.author or "").lower(), {
            "author": r.author, "author_pic": pics.get((r.author or "").lower()),
            "mine": (r.author or "").lower() == user.username.lower(), "stories": []})
        gone = r.image_url == _V3_EXPIRED
        try:
            ends = (datetime.fromisoformat(r.created_at) + timedelta(hours=STORY_HOURS)).isoformat()
        except Exception:
            ends = None
        g["stories"].append({"id": r.id, "text": r.text, "image_url": None if gone else r.image_url,
                             "created_at": r.created_at, "expires_at": ends})
    people = sorted(groups.values(), key=lambda g: (not g["mine"], -(g["stories"][-1]["id"])))
    return {"people": people, "hours": STORY_HOURS}


@app.get("/feed/me/stats")
def v3_feed_my_stats(user=Depends(_game_user), db=Depends(get_db)):
    posts = db.query(sa_func.count(FeedPost.id)).filter(sa_func.lower(FeedPost.author) == user.username.lower(),
                                                        FeedPost.is_deleted.is_(False),
                                                        FeedPost.post_type != "story").scalar() or 0
    friends = db.query(sa_func.count(Friendship.id)).filter(Friendship.user_id == user.id,
                                                            Friendship.status == "accepted").scalar() or 0
    return {"posts": int(posts), "friends": int(friends), "score": user.progress_score or 0,
            "streak": user.study_streak or 0}


try:
    Base.metadata.create_all(bind=engine, checkfirst=True)
    print("[DB] v3 tables (comments, reports) created/verified [OK]")
except Exception as e:
    print(f"[DB ERROR - v3 create_all #3] {e}")

print("[V3] OX-Bridge v3 additions loaded [OK]")


# ============================================================
# TEXT-TO-SPEECH: natural voices for every student
# ------------------------------------------------------------
# * Works with Azure Speech (has Nigerian English voices), Google Cloud TTS, or ElevenLabs.
#   Pick one with TTS_PROVIDER (or leave empty: the first one that has a key is used).
# * Every clip is cached (Cloudinary + a database row), so a lesson that many students listen to
#   is paid for ONCE. Cached plays cost no characters.
# * Per-student daily limit and an overall monthly cap keep you inside the free quota.
# * If anything is not set up or a limit is reached, the app simply falls back to the phone's voice.
#
# Environment variables (set them on Render):
#   TTS_PROVIDER            azure | google | elevenlabs        (optional)
#   AZURE_SPEECH_KEY, AZURE_SPEECH_REGION                      (for azure)
#   GOOGLE_TTS_API_KEY                                         (for google)
#   ELEVENLABS_API_KEY                                         (for elevenlabs)
#   TTS_AZURE_VOICE   default en-NG-EzinneNeural   (Nigerian English, female; en-NG-AbeoNeural = male)
#   TTS_GOOGLE_VOICE  default en-GB-Wavenet-A
#   TTS_ELEVEN_VOICE  default 21m00Tcm4TlvDq8ikWAM,  TTS_ELEVEN_MODEL default eleven_flash_v2_5
#   TTS_DAILY_CHARS_PER_USER  default 6000      TTS_MONTHLY_CHAR_CAP  default 400000
# ============================================================
import io as _tts_io
from xml.sax.saxutils import escape as _tts_xml_escape

TTS_PROVIDER        = (os.getenv("TTS_PROVIDER") or "").strip().lower()
AZURE_SPEECH_KEY    = os.getenv("AZURE_SPEECH_KEY")
AZURE_SPEECH_REGION = (os.getenv("AZURE_SPEECH_REGION") or "").strip()
GOOGLE_TTS_API_KEY  = os.getenv("GOOGLE_TTS_API_KEY")
ELEVENLABS_API_KEY  = os.getenv("ELEVENLABS_API_KEY")
TTS_AZURE_VOICE     = os.getenv("TTS_AZURE_VOICE", "en-NG-EzinneNeural")
TTS_GOOGLE_VOICE    = os.getenv("TTS_GOOGLE_VOICE", "en-GB-Wavenet-A")
TTS_ELEVEN_VOICE    = os.getenv("TTS_ELEVEN_VOICE", "21m00Tcm4TlvDq8ikWAM")
TTS_ELEVEN_MODEL    = os.getenv("TTS_ELEVEN_MODEL", "eleven_flash_v2_5")
TTS_DAILY_CHARS_PER_USER = int(os.getenv("TTS_DAILY_CHARS_PER_USER", "6000"))
TTS_MONTHLY_CHAR_CAP     = int(os.getenv("TTS_MONTHLY_CHAR_CAP", "400000"))
TTS_MAX_CHARS_PER_REQUEST = 1200


class TtsAudio(Base):
    __tablename__ = "tts_audio"
    id         = Column(Integer, primary_key=True, index=True)
    key        = Column(String, unique=True, index=True)     # sha256(provider|voice|clean text)
    url        = Column(String)
    public_id  = Column(String, nullable=True)
    provider   = Column(String, nullable=True)
    chars      = Column(Integer, default=0)
    plays      = Column(Integer, default=0)
    created_at = Column(String, index=True)
    last_used  = Column(String, nullable=True)


class TtsUsage(Base):
    __tablename__ = "tts_usage"
    __table_args__ = (UniqueConstraint("username", "day", name="uq_tts_usage_user_day"),)
    id       = Column(Integer, primary_key=True, index=True)
    username = Column(String, index=True)
    day      = Column(String, index=True)       # YYYY-MM-DD (UTC)
    chars    = Column(Integer, default=0)
    calls    = Column(Integer, default=0)


class TtsIn(BaseModel):
    text: str


# ---------- turn answers (markdown + LaTeX) into plain, speakable text ----------
_TTS_EMOJI = re.compile("[\U0001F300-\U0001FAFF\u2600-\u27BF\uFE0F\u200d]")
# rules that must run from the inside out (so nested braces like \frac{-b \pm \sqrt{x}}{2a} work)
_TTS_MATH_STRUCT = [
    (r"\^\{?\\circ\}?|\\degree", " degrees "),
    (r"\\sqrt\s*\[(\d+)\]\s*\{([^{}]*)\}", r" root \1 of \2 "),
    (r"\\sqrt\s*\{([^{}]*)\}", r" square root of \1 "),
    (r"\\text\{([^{}]*)\}", r" \1 "),
    (r"\\d?frac\s*\{([^{}]*)\}\s*\{([^{}]*)\}", r" \1 over \2 "),
    (r"\^\{?2\}?", " squared "), (r"\^\{?3\}?", " cubed "),
    (r"\^\{([^{}]*)\}", r" to the power \1 "), (r"_\{([^{}]*)\}", r" \1 "),
]
_TTS_MATH_RULES = [
    (r"\^(\w)", r" to the power \1 "), (r"_(\w)", r" \1 "),
    (r"\\times|\\cdot", " times "), (r"\\div", " divided by "), (r"\\pm", " plus or minus "),
    (r"\\leq?(?![a-z])", " less than or equal to "), (r"\\geq?(?![a-z])", " greater than or equal to "),
    (r"\\neq", " not equal to "), (r"\\approx", " approximately "),
    (r"\\rightleftharpoons|\\leftrightarrow|<=>|<->", " is in equilibrium with "),
    (r"\\rightarrow|\\to(?![a-z])|\\longrightarrow|\\Rightarrow|-{1,2}>|=>", " gives "),
    (r"\\Delta", " delta "), (r"\\pi(?![a-z])", " pi "), (r"\\theta", " theta "), (r"\\alpha", " alpha "),
    (r"\\beta", " beta "), (r"\\gamma", " gamma "), (r"\\lambda", " lambda "), (r"\\mu(?![a-z])", " mu "),
    (r"\\sigma", " sigma "), (r"\\omega", " omega "), (r"\\infty", " infinity "),
    (r"\\[,;:! ]", " "), (r"\\[a-zA-Z]+", " "), (r"[{}]", " "),
    (r"=", " equals "), (r"\+", " plus "), (r"(\d)\s*-\s*(\d)", r"\1 minus \2"), (r"-", " minus "), (r"/", " over "),
]


def _tts_math_speech(t: str) -> str:
    t = re.sub(r"\\ce\{([^{}]*)\}", r"\1", t or "")
    for _ in range(5):                                   # work from the innermost braces outwards
        before = t
        for pat, rep in _TTS_MATH_STRUCT:
            t = re.sub(pat, rep, t)
        if t == before:
            break
    for pat, rep in _TTS_MATH_RULES:
        t = re.sub(pat, rep, t)
    return re.sub(r"\s+", " ", t).strip()


def _tts_clean(text: str) -> str:
    t = str(text or "")
    t = re.sub(r"```[\s\S]*?```", " ", t)                                   # code is not read aloud

    def _m(mo):
        return " " + _tts_math_speech(mo.group(1)) + " "
    t = re.sub(r"\$\$([\s\S]+?)\$\$", _m, t)
    t = re.sub(r"\\\[([\s\S]+?)\\\]", _m, t)
    t = re.sub(r"\\\(([\s\S]+?)\\\)", _m, t)
    t = re.sub(r"\\begin\{(?:align\*?|aligned|equation\*?|gather\*?|cases|array|matrix|pmatrix|bmatrix)\}([\s\S]*?)\\end\{[^}]*\}", _m, t)
    t = re.sub(r"\$(?!\s)((?:\\.|[^$\n\\])*?(?:[^\s$\\]|\\\S))\$(?!\d)", _m, t)
    t = re.sub(r"\\ce\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", lambda mo: " " + _tts_math_speech(mo.group(0)) + " ", t)
    t = re.sub(r"\\d?frac(?:\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}){2}|\\sqrt(?:\[[^\]\n]*\])?\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}",
               lambda mo: " " + _tts_math_speech(mo.group(0)) + " ", t)
    t = re.sub(r"`([^`\n]+)`", r"\1", t)
    t = re.sub(r"\[([^\]\n]+)\]\((?:https?://)[^\s)]+\)", r"\1", t)
    t = re.sub(r"https?://\S+", " link ", t)
    t = re.sub(r"^\s*\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)+\|?\s*$", " ", t, flags=re.M)   # table separator rows
    t = re.sub(r"\s*\|\s*", ", ", t)                                           # table cells become pauses
    t = re.sub(r"^\s{0,3}#{1,6}\s*", "", t, flags=re.M)
    t = re.sub(r"^\s*[-*+\u2022]\s+", "", t, flags=re.M)
    t = re.sub(r"^\s*>\s?", "", t, flags=re.M)
    t = re.sub(r"<br\s*/?>", ". ", t, flags=re.I)
    t = re.sub(r"</?(?:sub|sup)>", "", t, flags=re.I)
    t = re.sub(r"[*_~#>]+", " ", t)
    t = _TTS_EMOJI.sub("", t)
    t = re.sub(r"\\[a-zA-Z]+", " ", t)
    t = re.sub(r"\s*\n+\s*", ". ", t)
    t = re.sub(r"([.!?:;,])\s*(?:[.,]\s*)+", r"\1 ", t)
    return re.sub(r"\s+", " ", t).strip()


# ---------- providers ----------
def _tts_provider():
    """(provider, voice) for the provider that is configured, or (None, None)."""
    avail = {
        "azure": bool(AZURE_SPEECH_KEY and AZURE_SPEECH_REGION),
        "google": bool(GOOGLE_TTS_API_KEY),
        "elevenlabs": bool(ELEVENLABS_API_KEY),
    }
    voices = {"azure": TTS_AZURE_VOICE, "google": TTS_GOOGLE_VOICE, "elevenlabs": TTS_ELEVEN_VOICE}
    if TTS_PROVIDER in avail and avail[TTS_PROVIDER]:
        return TTS_PROVIDER, voices[TTS_PROVIDER]
    for p in ("azure", "google", "elevenlabs"):
        if avail[p]:
            return p, voices[p]
    return None, None


def _tts_synth(provider: str, voice: str, text: str) -> bytes:
    try:
        if provider == "azure":
            lang = "-".join(voice.split("-")[:2]) or "en-NG"
            ssml = ("<speak version='1.0' xml:lang='%s' xmlns='http://www.w3.org/2001/10/synthesis'>"
                    "<voice name='%s'><prosody rate='-4%%'>%s</prosody></voice></speak>") % (lang, voice, _tts_xml_escape(text))
            r = requests.post("https://%s.tts.speech.microsoft.com/cognitiveservices/v1" % AZURE_SPEECH_REGION,
                              headers={"Ocp-Apim-Subscription-Key": AZURE_SPEECH_KEY, "Content-Type": "application/ssml+xml",
                                       "X-Microsoft-OutputFormat": "audio-24khz-48kbitrate-mono-mp3", "User-Agent": "oxbridge-hub"},
                              data=ssml.encode("utf-8"), timeout=30)
            if r.status_code != 200:
                raise RuntimeError("azure %s %s" % (r.status_code, r.text[:160]))
            return r.content
        if provider == "google":
            lang = "-".join(voice.split("-")[:2]) or "en-GB"
            r = requests.post("https://texttospeech.googleapis.com/v1/text:synthesize", params={"key": GOOGLE_TTS_API_KEY},
                              json={"input": {"text": text}, "voice": {"languageCode": lang, "name": voice},
                                    "audioConfig": {"audioEncoding": "MP3", "speakingRate": 0.97}}, timeout=30)
            if r.status_code != 200:
                raise RuntimeError("google %s %s" % (r.status_code, r.text[:160]))
            return base64.b64decode(r.json().get("audioContent", ""))
        if provider == "elevenlabs":
            r = requests.post("https://api.elevenlabs.io/v1/text-to-speech/%s" % voice, params={"output_format": "mp3_44100_64"},
                              headers={"xi-api-key": ELEVENLABS_API_KEY, "Content-Type": "application/json", "Accept": "audio/mpeg"},
                              json={"text": text, "model_id": TTS_ELEVEN_MODEL}, timeout=40)
            if r.status_code != 200:
                raise RuntimeError("elevenlabs %s %s" % (r.status_code, r.text[:160]))
            return r.content
    except HTTPException:
        raise
    except Exception as e:
        print("[TTS] synth failed:", e)
        raise HTTPException(502, "The voice service is busy right now. Using the phone voice instead.")
    raise HTTPException(503, "Voice service is not set up yet.")


def _tts_store(audio: bytes, key: str):
    """Keep the clip on Cloudinary so it can be replayed free. Returns (url, public_id) or (None, None)."""
    if not CLOUDINARY_CONFIGURED:
        return None, None
    try:
        res = cloudinary.uploader.upload(_tts_io.BytesIO(audio), resource_type="video", folder="oxbridge-tts",
                                         public_id=key[:48], overwrite=True)
        return res.get("secure_url"), res.get("public_id")
    except Exception as e:
        print("[TTS] Cloudinary store failed:", e)
        return None, None


def _tts_day():   return datetime.now(timezone.utc).strftime("%Y-%m-%d")
def _tts_month(): return datetime.now(timezone.utc).strftime("%Y-%m")


def _tts_used_today(db, username: str) -> int:
    row = db.query(TtsUsage).filter(TtsUsage.username == username, TtsUsage.day == _tts_day()).first()
    return int(row.chars or 0) if row else 0


def _tts_used_month(db) -> int:
    return int(db.query(sa_func.coalesce(sa_func.sum(TtsUsage.chars), 0)).filter(TtsUsage.day.like(_tts_month() + "%")).scalar() or 0)


def _tts_user(authorization: Optional[str] = Header(None), db=Depends(get_db)):
    """Logged-in student. A muted student can still listen to answers, so no suspension check here."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Please login to use the voice.")
    username = decode_token(authorization[7:].strip())
    if not username:
        raise HTTPException(401, "Session expired. Please login again.")
    user = db.query(User).filter(User.username == username).first()
    if not user:
        raise HTTPException(401, "User not found. Please login again.")
    return user


# ---------- endpoints ----------
@app.get("/tts/status")
def tts_status(user=Depends(_tts_user), db=Depends(get_db)):
    prov, voice = _tts_provider()
    used = _tts_used_today(db, user.username)
    return {"enabled": bool(prov), "provider": prov, "voice": voice, "daily_limit": TTS_DAILY_CHARS_PER_USER,
            "used_today": used, "left_today": max(0, TTS_DAILY_CHARS_PER_USER - used)}


@app.post("/tts")
def tts_speak(data: TtsIn, user=Depends(_tts_user), db=Depends(get_db)):
    """Give back natural-sounding audio for a piece of text (send up to ~1200 characters at a time)."""
    prov, voice = _tts_provider()
    if not prov:
        raise HTTPException(503, "Voice service is not set up yet.")
    clean = _tts_clean(data.text)
    if len(clean) < 2:
        raise HTTPException(400, "Nothing to read.")
    if len(clean) > TTS_MAX_CHARS_PER_REQUEST:
        raise HTTPException(400, "That text is too long. Please send it in smaller pieces.")
    key = hashlib.sha256(("%s|%s|%s" % (prov, voice, clean)).encode("utf-8")).hexdigest()

    # already made before? replay it for free
    hit = db.query(TtsAudio).filter(TtsAudio.key == key).first()
    if hit and hit.url:
        hit.plays = (hit.plays or 0) + 1
        hit.last_used = now_str()
        db.commit()
        used = _tts_used_today(db, user.username)
        return {"success": True, "url": hit.url, "cached": True, "chars": len(clean), "left_today": max(0, TTS_DAILY_CHARS_PER_USER - used)}

    used = _tts_used_today(db, user.username)
    if used + len(clean) > TTS_DAILY_CHARS_PER_USER:
        raise HTTPException(429, "You have used today's voice reading limit. Using the phone voice instead.")
    if _tts_used_month(db) + len(clean) > TTS_MONTHLY_CHAR_CAP:
        raise HTTPException(429, "The voice service has reached its monthly limit. Using the phone voice instead.")

    audio = _tts_synth(prov, voice, clean)
    if not audio:
        raise HTTPException(502, "The voice service returned no audio. Using the phone voice instead.")

    # count the characters
    day = _tts_day()
    row = db.query(TtsUsage).filter(TtsUsage.username == user.username, TtsUsage.day == day).first()
    if not row:
        try:
            row = TtsUsage(username=user.username, day=day, chars=0, calls=0)
            db.add(row)
            db.commit()
        except IntegrityError:
            db.rollback()          # another request created it a moment ago
            row = db.query(TtsUsage).filter(TtsUsage.username == user.username, TtsUsage.day == day).first()
    row.chars = (row.chars or 0) + len(clean)
    row.calls = (row.calls or 0) + 1
    db.commit()

    url, pid = _tts_store(audio, key)
    if url:
        try:
            db.add(TtsAudio(key=key, url=url, public_id=pid, provider=prov, chars=len(clean), plays=1, created_at=now_str(), last_used=now_str()))
            db.commit()
        except IntegrityError:
            db.rollback()          # someone saved the same clip a moment ago: fine
        return {"success": True, "url": url, "cached": False, "chars": len(clean),
                "left_today": max(0, TTS_DAILY_CHARS_PER_USER - (row.chars or 0))}
    return {"success": True, "audio": base64.b64encode(audio).decode("ascii"), "cached": False, "chars": len(clean),
            "left_today": max(0, TTS_DAILY_CHARS_PER_USER - (row.chars or 0))}


@app.get("/admin/tts/stats")
def admin_tts_stats(db=Depends(get_db), _ok=Depends(verify_admin)):
    prov, voice = _tts_provider()
    day, month = _tts_day(), _tts_month()
    today = int(db.query(sa_func.coalesce(sa_func.sum(TtsUsage.chars), 0)).filter(TtsUsage.day == day).scalar() or 0)
    users_today = int(db.query(sa_func.count(TtsUsage.id)).filter(TtsUsage.day == day).scalar() or 0)
    clips = int(db.query(sa_func.count(TtsAudio.id)).scalar() or 0)
    replays = int(db.query(sa_func.coalesce(sa_func.sum(TtsAudio.plays), 0)).scalar() or 0)
    saved = int(db.query(sa_func.coalesce(sa_func.sum(TtsAudio.chars * (TtsAudio.plays - 1)), 0)).scalar() or 0)
    return {"enabled": bool(prov), "provider": prov, "voice": voice, "cloudinary_ready": bool(CLOUDINARY_CONFIGURED),
            "month_chars": _tts_used_month(db), "month_cap": TTS_MONTHLY_CHAR_CAP, "today_chars": today, "users_today": users_today,
            "daily_limit_per_user": TTS_DAILY_CHARS_PER_USER, "saved_clips": clips, "total_plays": replays,
            "characters_saved_by_cache": max(0, saved)}


@app.post("/admin/tts/purge")
def admin_tts_purge(days: int = 90, db=Depends(get_db), _ok=Depends(verify_admin)):
    """Delete saved clips not used for `days` days (frees Cloudinary space)."""
    days = max(7, min(int(days), 3650))
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = db.query(TtsAudio).filter(sa_func.coalesce(TtsAudio.last_used, TtsAudio.created_at) < cutoff).limit(500).all()
    removed = 0
    for r in rows:
        if r.public_id and CLOUDINARY_CONFIGURED:
            try:
                cloudinary.uploader.destroy(r.public_id, resource_type="video")
            except Exception as e:
                print("[TTS] purge destroy failed:", e)
        db.delete(r)
        removed += 1
    db.commit()
    return {"removed": removed, "more_waiting": len(rows) == 500}


try:
    Base.metadata.create_all(bind=engine, checkfirst=True)
    print("[DB] text-to-speech tables created/verified [OK]")
except Exception as e:
    print(f"[DB ERROR - tts create_all] {e}")

print("[TTS] text-to-speech section loaded [OK]")
