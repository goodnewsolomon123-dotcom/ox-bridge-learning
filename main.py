import os
import json
import random
import requests
from fastapi import FastAPI, Depends, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import create_engine, Column, Integer, String, ForeignKey, Float, Boolean, DateTime, text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from pydantic import BaseModel
from passlib.context import CryptContext
import jwt
from datetime import datetime, timedelta, timezone
from typing import Optional

# ============================================================
# CONFIGURATION
# ============================================================
DATABASE_URL  = os.getenv("DATABASE_URL")
GROQ_KEY      = os.getenv("GROQ_KEY")
GEMINI_KEY    = os.getenv("GEMINI_KEY")
OR_KEY        = os.getenv("OPENROUTER_KEY")
TAVILY_KEY    = os.getenv("TAVILY_API_KEY")
SECRET_KEY    = os.getenv("SECRET_KEY", "oxbridge_secret")

engine       = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base         = declarative_base()
pwd_context  = CryptContext(schemes=["bcrypt"], deprecated="auto")


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
    progress_score     = Column(Float,  default=0.0)
    last_learned_topic = Column(String, nullable=True)
    study_streak       = Column(Integer, default=0)
    last_study_date    = Column(String,  nullable=True)   # stored as YYYY-MM-DD
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

class Question(Base):
    __tablename__ = "questions"
    id             = Column(Integer, primary_key=True, index=True)
    topic_id       = Column(Integer, ForeignKey("topics.id"))
    question_text  = Column(String)
    option_a       = Column(String)
    option_b       = Column(String)
    option_c       = Column(String)
    option_d       = Column(String)
    correct_answer = Column(String)

# --- NEW MODELS ---

class Badge(Base):
    __tablename__ = "badges"
    id          = Column(Integer, primary_key=True, index=True)
    name        = Column(String)
    icon        = Column(String)          # emoji or icon name
    description = Column(String)
    points_required = Column(Float, default=0.0)

class UserBadge(Base):
    __tablename__ = "user_badges"
    id        = Column(Integer, primary_key=True, index=True)
    user_id   = Column(Integer, ForeignKey("users.id"))
    badge_id  = Column(Integer, ForeignKey("badges.id"))
    earned_at = Column(String)            # stored as ISO datetime string

class QuizResult(Base):
    __tablename__ = "quiz_results"
    id              = Column(Integer, primary_key=True, index=True)
    user_id         = Column(Integer, ForeignKey("users.id"))
    subject         = Column(String)
    level           = Column(String)
    topic           = Column(String)
    score           = Column(Integer)
    total_questions = Column(Integer)
    date_taken      = Column(String)      # ISO datetime string

class PastQuestion(Base):
    __tablename__ = "past_questions"
    id             = Column(Integer, primary_key=True, index=True)
    exam_type      = Column(String)       # WAEC or JAMB
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
    id            = Column(Integer, primary_key=True, index=True)
    date          = Column(String, unique=True)   # YYYY-MM-DD
    question_text = Column(String)
    option_a      = Column(String)
    option_b      = Column(String)
    option_c      = Column(String)
    option_d      = Column(String)
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
    studied_at = Column(String)           # ISO datetime string

class Friendship(Base):
    __tablename__ = "friendships"
    id        = Column(Integer, primary_key=True, index=True)
    user_id   = Column(Integer, ForeignKey("users.id"))
    friend_id = Column(Integer, ForeignKey("users.id"))
    status    = Column(String, default="pending")  # pending | accepted

class Notification(Base):
    __tablename__ = "notifications"
    id         = Column(Integer, primary_key=True, index=True)
    user_id    = Column(Integer, ForeignKey("users.id"))
    message    = Column(String)
    is_read    = Column(Boolean, default=False)
    created_at = Column(String)

class GameScore(Base):
    __tablename__ = "game_scores"
    id         = Column(Integer, primary_key=True, index=True)
    user_id    = Column(Integer, ForeignKey("users.id"))
    game_type  = Column(String)           # word_scramble | spell | math
    score      = Column(Integer)
    played_at  = Column(String)


# ============================================================
# CREATE TABLES + MIGRATIONS
# ============================================================

Base.metadata.create_all(bind=engine, checkfirst=True)

def run_migrations():
    migrations = [
        # users — new columns
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS full_name VARCHAR;",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS profile_pic VARCHAR;",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS bio VARCHAR;",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS progress_score FLOAT DEFAULT 0.0;",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_learned_topic VARCHAR;",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS study_streak INTEGER DEFAULT 0;",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_study_date VARCHAR;",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS coins INTEGER DEFAULT 0;",
    ]
    with engine.connect() as conn:
        for sql in migrations:
            conn.execute(text(sql))
        conn.commit()

run_migrations()

# Seed default badges once
def seed_badges():
    db = SessionLocal()
    try:
        if db.query(Badge).count() == 0:
            default_badges = [
                Badge(name="First Step",     icon="👣", description="Completed your first login",        points_required=0),
                Badge(name="Quiz Starter",   icon="🧠", description="Completed your first quiz",         points_required=1),
                Badge(name="Rising Star",    icon="⭐", description="Reached 10 points",                 points_required=10),
                Badge(name="Scholar",        icon="📚", description="Reached 50 points",                 points_required=50),
                Badge(name="Champion",       icon="🏆", description="Reached 100 points",                points_required=100),
                Badge(name="Legend",         icon="🔥", description="Reached 500 points",                points_required=500),
                Badge(name="Streak Master",  icon="💫", description="7-day study streak",                points_required=0),
                Badge(name="Speed Learner",  icon="⚡", description="Completed 10 quizzes",              points_required=0),
            ]
            db.add_all(default_badges)
            db.commit()
    finally:
        db.close()

seed_badges()


# ============================================================
# HELPER FUNCTIONS
# ============================================================

def now_str():
    return datetime.now(timezone.utc).isoformat()

def today_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")

def check_and_award_badges(user_id: int, db):
    """Auto-award badges based on user score and activity."""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return
    earned_ids = {ub.badge_id for ub in db.query(UserBadge).filter(UserBadge.user_id == user_id).all()}
    all_badges  = db.query(Badge).all()
    newly_earned = []
    quiz_count   = db.query(QuizResult).filter(QuizResult.user_id == user_id).count()

    for badge in all_badges:
        if badge.id in earned_ids:
            continue
        award = False
        if badge.name == "Rising Star"   and user.progress_score >= 10:   award = True
        if badge.name == "Scholar"       and user.progress_score >= 50:   award = True
        if badge.name == "Champion"      and user.progress_score >= 100:  award = True
        if badge.name == "Legend"        and user.progress_score >= 500:  award = True
        if badge.name == "Streak Master" and user.study_streak  >= 7:     award = True
        if badge.name == "Speed Learner" and quiz_count         >= 10:    award = True
        if award:
            db.add(UserBadge(user_id=user_id, badge_id=badge.id, earned_at=now_str()))
            newly_earned.append(badge.name)
            db.add(Notification(
                user_id    = user_id,
                message    = f"🏅 You earned the '{badge.name}' badge!",
                created_at = now_str()
            ))
    if newly_earned:
        db.commit()
    return newly_earned

def update_streak(user: User, db):
    """Update daily study streak."""
    today = today_str()
    if user.last_study_date == today:
        return
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    if user.last_study_date == yesterday:
        user.study_streak = (user.study_streak or 0) + 1
    else:
        user.study_streak = 1
    user.last_study_date = today
    db.commit()


# ============================================================
# HYBRID AI ROUTER
# ============================================================

def get_ai_response(prompt: str) -> str:
    # 1. Groq
    if GROQ_KEY:
        try:
            res = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json"},
                json={"model": "llama-3.1-8b-instant", "messages": [{"role": "user", "content": prompt}]},
                timeout=15
            )
            if res.status_code == 200:
                return res.json()['choices'][0]['message']['content']
        except: pass
    # 2. Gemini
    if GEMINI_KEY:
        try:
            res = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_KEY}",
                json={"contents": [{"parts": [{"text": prompt}]}]},
                timeout=15
            )
            if res.status_code == 200:
                return res.json()["candidates"][0]["content"]["parts"][0]["text"]
        except: pass
    # 3. OpenRouter
    if OR_KEY:
        try:
            res = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {OR_KEY}", "Content-Type": "application/json"},
                json={"model": "meta-llama/llama-3-8b-instruct:free", "messages": [{"role": "user", "content": prompt}]},
                timeout=15
            )
            if res.status_code == 200:
                return res.json()['choices'][0]['message']['content']
        except: pass
    return "AI services are currently busy. Please try again shortly."


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
    try:
        yield db
    finally:
        db.close()


# ============================================================
# PYDANTIC SCHEMAS
# ============================================================

class UserCreate(BaseModel):
    username: str
    email: str
    password: str

class SubjectCreate(BaseModel):
    name: str
    level: str

class TopicCreate(BaseModel):
    title: str
    subject_id: int

class ScoreUpdate(BaseModel):
    username: str
    points: float

class ProfileUpdate(BaseModel):
    full_name: Optional[str]    = None
    profile_pic: Optional[str]  = None
    bio: Optional[str]          = None

class QuizResultCreate(BaseModel):
    username: str
    subject: str
    level: str
    topic: str
    score: int
    total_questions: int

class PastQuestionCreate(BaseModel):
    exam_type: str
    year: int
    subject: str
    question_text: str
    option_a: str
    option_b: str
    option_c: str
    option_d: str
    correct_answer: str
    explanation: Optional[str] = None

class DailyChallengeSubmit(BaseModel):
    username: str
    answer: str

class StudyLog(BaseModel):
    username: str
    topic: str
    subject: str
    level: str

class FriendRequest(BaseModel):
    username: str
    friend_username: str

class StudyPlanRequest(BaseModel):
    username: str
    level: str
    subjects: str          # comma-separated
    exam_date: str         # e.g. "2025-11-15"
    hours_per_day: int

class AnswerCheckRequest(BaseModel):
    question: str
    student_answer: str
    correct_answer: str
    subject: str

class GameScoreSave(BaseModel):
    username: str
    game_type: str
    score: int


# ============================================================
# ROOT
# ============================================================

@app.get("/")
def root():
    return {
        "app":     "Ox-Bridge Learning Hub",
        "version": "2.0.0",
        "status":  "running",
        "message": "Welcome to Ox-Bridge API — Powering Nigerian Education with AI 🇳🇬"
    }


# ============================================================
# AUTH
# ============================================================

@app.post("/signup")
def signup(user: UserCreate, db=Depends(get_db)):
    if db.query(User).filter(User.username == user.username).first():
        raise HTTPException(400, "Username already taken")
    if db.query(User).filter(User.email == user.email).first():
        raise HTTPException(400, "Email already registered")
    new_user = User(
        username        = user.username,
        email           = user.email,
        hashed_password = pwd_context.hash(user.password)
    )
    db.add(new_user)
    db.commit()
    db.refresh(new_user)
    # Award First Step badge
    first_step = db.query(Badge).filter(Badge.name == "First Step").first()
    if first_step:
        db.add(UserBadge(user_id=new_user.id, badge_id=first_step.id, earned_at=now_str()))
        db.add(Notification(user_id=new_user.id, message="👣 Welcome! You earned the 'First Step' badge!", created_at=now_str()))
        db.commit()
    return {"msg": "Account created successfully! Please login."}

@app.post("/login")
def login(username: str, password: str, db=Depends(get_db)):
    user = db.query(User).filter(User.username == username).first()
    if not user or not pwd_context.verify(password, user.hashed_password):
        raise HTTPException(401, "Invalid username or password")
    token = jwt.encode(
        {"sub": user.username, "exp": datetime.now(timezone.utc) + timedelta(hours=24)},
        SECRET_KEY
    )
    return {
        "access_token": token,
        "username":     user.username,
        "score":        user.progress_score,
        "streak":       user.study_streak,
        "coins":        user.coins
    }


# ============================================================
# PROFILE & PROGRESS
# ============================================================

@app.get("/profile/{username}")
def get_profile(username: str, db=Depends(get_db)):
    user = db.query(User).filter(User.username == username).first()
    if not user:
        raise HTTPException(404, "User not found")
    badges     = db.query(UserBadge).filter(UserBadge.user_id == user.id).all()
    badge_list = []
    for ub in badges:
        b = db.query(Badge).filter(Badge.id == ub.badge_id).first()
        if b:
            badge_list.append({"name": b.name, "icon": b.icon, "earned_at": ub.earned_at})
    quiz_count = db.query(QuizResult).filter(QuizResult.user_id == user.id).count()
    return {
        "username":    user.username,
        "full_name":   user.full_name,
        "bio":         user.bio,
        "profile_pic": user.profile_pic,
        "score":       user.progress_score,
        "streak":      user.study_streak,
        "coins":       user.coins,
        "last_topic":  user.last_learned_topic,
        "quiz_count":  quiz_count,
        "badges":      badge_list
    }

@app.post("/profile/update")
def update_profile(username: str, data: ProfileUpdate, db=Depends(get_db)):
    user = db.query(User).filter(User.username == username).first()
    if not user:
        raise HTTPException(404, "User not found")
    if data.full_name:   user.full_name   = data.full_name
    if data.profile_pic: user.profile_pic = data.profile_pic
    if data.bio:         user.bio         = data.bio
    db.commit()
    return {"msg": "Profile updated successfully"}

@app.post("/progress/add-score")
def add_score(data: ScoreUpdate, db=Depends(get_db)):
    user = db.query(User).filter(User.username == data.username).first()
    if not user:
        raise HTTPException(404, "User not found")
    user.progress_score = (user.progress_score or 0) + data.points
    user.coins          = (user.coins or 0) + int(data.points)
    db.commit()
    newly_earned = check_and_award_badges(user.id, db)
    return {
        "msg":          "Score added",
        "new_total":    user.progress_score,
        "coins":        user.coins,
        "new_badges":   newly_earned or []
    }

@app.get("/leaderboard")
def get_leaderboard(db=Depends(get_db)):
    top = db.query(User).order_by(User.progress_score.desc()).limit(10).all()
    return [
        {
            "rank":     i + 1,
            "username": u.username,
            "score":    u.progress_score,
            "streak":   u.study_streak,
            "coins":    u.coins
        }
        for i, u in enumerate(top)
    ]


# ============================================================
# BADGES
# ============================================================

@app.get("/badges/all")
def get_all_badges(db=Depends(get_db)):
    return [
        {"id": b.id, "name": b.name, "icon": b.icon,
         "description": b.description, "points_required": b.points_required}
        for b in db.query(Badge).all()
    ]

@app.get("/badges/{username}")
def get_user_badges(username: str, db=Depends(get_db)):
    user = db.query(User).filter(User.username == username).first()
    if not user:
        raise HTTPException(404, "User not found")
    rows = db.query(UserBadge).filter(UserBadge.user_id == user.id).all()
    result = []
    for ub in rows:
        b = db.query(Badge).filter(Badge.id == ub.badge_id).first()
        if b:
            result.append({"name": b.name, "icon": b.icon,
                           "description": b.description, "earned_at": ub.earned_at})
    return result


# ============================================================
# QUIZ SCORE TRACKING
# ============================================================

@app.post("/quiz/save-result")
def save_quiz_result(data: QuizResultCreate, db=Depends(get_db)):
    user = db.query(User).filter(User.username == data.username).first()
    if not user:
        raise HTTPException(404, "User not found")
    result = QuizResult(
        user_id         = user.id,
        subject         = data.subject,
        level           = data.level,
        topic           = data.topic,
        score           = data.score,
        total_questions = data.total_questions,
        date_taken      = now_str()
    )
    db.add(result)
    # Add points: each correct answer = 5 points
    points_earned = data.score * 5
    user.progress_score = (user.progress_score or 0) + points_earned
    user.coins          = (user.coins or 0) + points_earned
    db.commit()
    newly_earned = check_and_award_badges(user.id, db)
    # Award Quiz Starter badge on first quiz
    quiz_count = db.query(QuizResult).filter(QuizResult.user_id == user.id).count()
    if quiz_count == 1:
        qs = db.query(Badge).filter(Badge.name == "Quiz Starter").first()
        if qs:
            already = db.query(UserBadge).filter(
                UserBadge.user_id == user.id,
                UserBadge.badge_id == qs.id
            ).first()
            if not already:
                db.add(UserBadge(user_id=user.id, badge_id=qs.id, earned_at=now_str()))
                db.add(Notification(user_id=user.id,
                    message="🧠 You earned the 'Quiz Starter' badge!", created_at=now_str()))
                db.commit()
    return {
        "msg":          "Quiz result saved",
        "points_earned": points_earned,
        "new_total":    user.progress_score,
        "new_badges":   newly_earned or []
    }

@app.get("/quiz/history/{username}")
def quiz_history(username: str, db=Depends(get_db)):
    user = db.query(User).filter(User.username == username).first()
    if not user:
        raise HTTPException(404, "User not found")
    results = db.query(QuizResult).filter(
        QuizResult.user_id == user.id
    ).order_by(QuizResult.id.desc()).limit(20).all()
    return [
        {
            "subject":         r.subject,
            "level":           r.level,
            "topic":           r.topic,
            "score":           r.score,
            "total_questions": r.total_questions,
            "percentage":      round((r.score / r.total_questions) * 100) if r.total_questions else 0,
            "date_taken":      r.date_taken
        }
        for r in results
    ]

@app.get("/quiz/stats/{username}")
def quiz_stats(username: str, db=Depends(get_db)):
    user = db.query(User).filter(User.username == username).first()
    if not user:
        raise HTTPException(404, "User not found")
    results = db.query(QuizResult).filter(QuizResult.user_id == user.id).all()
    if not results:
        return {"total_quizzes": 0, "average_score": 0, "best_subject": None}
    total      = len(results)
    avg        = sum(r.score / r.total_questions * 100 for r in results if r.total_questions) / total
    subject_scores: dict = {}
    for r in results:
        if r.subject not in subject_scores:
            subject_scores[r.subject] = []
        if r.total_questions:
            subject_scores[r.subject].append(r.score / r.total_questions * 100)
    best_subject = max(subject_scores, key=lambda s: sum(subject_scores[s]) / len(subject_scores[s]))
    return {
        "total_quizzes":  total,
        "average_score":  round(avg, 1),
        "best_subject":   best_subject,
        "subject_scores": {s: round(sum(v) / len(v), 1) for s, v in subject_scores.items()}
    }


# ============================================================
# PAST QUESTIONS BANK
# ============================================================

@app.post("/admin/add-past-question")
def add_past_question(data: PastQuestionCreate, db=Depends(get_db)):
    q = PastQuestion(**data.dict())
    db.add(q)
    db.commit()
    return {"msg": "Past question added", "id": q.id}

@app.get("/past-questions/{exam_type}/{subject}")
def get_past_questions(exam_type: str, subject: str, year: int = None, db=Depends(get_db)):
    query = db.query(PastQuestion).filter(
        PastQuestion.exam_type == exam_type.upper(),
        PastQuestion.subject   == subject
    )
    if year:
        query = query.filter(PastQuestion.year == year)
    questions = query.limit(20).all()
    return [
        {
            "id":             q.id,
            "year":           q.year,
            "question_text":  q.question_text,
            "option_a":       q.option_a,
            "option_b":       q.option_b,
            "option_c":       q.option_c,
            "option_d":       q.option_d,
            "correct_answer": q.correct_answer,
            "explanation":    q.explanation
        }
        for q in questions
    ]

@app.get("/past-questions/random/{subject}")
def random_past_question(subject: str, exam: str = "WAEC", db=Depends(get_db)):
    questions = db.query(PastQuestion).filter(
        PastQuestion.exam_type == exam.upper(),
        PastQuestion.subject   == subject
    ).all()
    if not questions:
        # AI generates a past-question-style question as fallback
        prompt = f"""Generate 1 {exam} past question for {subject}.
        Return ONLY JSON: {{"question_text":"...","option_a":"...","option_b":"...","option_c":"...","option_d":"...","correct_answer":"A","explanation":"..."}}"""
        raw = get_ai_response(prompt)
        try:
            clean = raw.replace("```json","").replace("```","").strip()
            return {"source": "ai_generated", "question": json.loads(clean)}
        except:
            return {"error": "No questions found for this subject"}
    q = random.choice(questions)
    return {
        "source":   "database",
        "question": {
            "id":             q.id,
            "year":           q.year,
            "question_text":  q.question_text,
            "option_a":       q.option_a,
            "option_b":       q.option_b,
            "option_c":       q.option_c,
            "option_d":       q.option_d,
            "correct_answer": q.correct_answer,
            "explanation":    q.explanation
        }
    }


# ============================================================
# DAILY CHALLENGE
# ============================================================

@app.get("/daily-challenge")
def get_daily_challenge(db=Depends(get_db)):
    today    = today_str()
    existing = db.query(DailyChallenge).filter(DailyChallenge.date == today).first()
    if not existing:
        # Generate new challenge via AI
        subjects = ["Mathematics", "English Language", "Biology", "Physics", "Chemistry",
                    "Government", "Economics", "Geography", "Literature"]
        subject  = random.choice(subjects)
        prompt   = f"""Generate 1 challenging multiple-choice question about {subject} 
        suitable for Nigerian secondary school students.
        Return ONLY JSON: {{"question_text":"...","option_a":"...","option_b":"...","option_c":"...","option_d":"...","correct_answer":"A"}}"""
        raw = get_ai_response(prompt)
        try:
            clean = raw.replace("```json","").replace("```","").strip()
            q     = json.loads(clean)
            existing = DailyChallenge(
                date           = today,
                question_text  = q["question_text"],
                option_a       = q["option_a"],
                option_b       = q["option_b"],
                option_c       = q["option_c"],
                option_d       = q["option_d"],
                correct_answer = q["correct_answer"]
            )
            db.add(existing)
            db.commit()
            db.refresh(existing)
        except:
            return {"error": "Could not generate daily challenge. Try again shortly."}
    return {
        "id":            existing.id,
        "date":          existing.date,
        "question_text": existing.question_text,
        "option_a":      existing.option_a,
        "option_b":      existing.option_b,
        "option_c":      existing.option_c,
        "option_d":      existing.option_d
        # correct_answer intentionally hidden from GET
    }

@app.post("/daily-challenge/submit")
def submit_daily_challenge(data: DailyChallengeSubmit, db=Depends(get_db)):
    user = db.query(User).filter(User.username == data.username).first()
    if not user:
        raise HTTPException(404, "User not found")
    today     = today_str()
    challenge = db.query(DailyChallenge).filter(DailyChallenge.date == today).first()
    if not challenge:
        raise HTTPException(404, "No daily challenge found for today")
    # Check if already attempted
    already = db.query(DailyChallengeAttempt).filter(
        DailyChallengeAttempt.user_id      == user.id,
        DailyChallengeAttempt.challenge_id == challenge.id
    ).first()
    if already:
        return {"msg": "You already attempted today's challenge", "already_attempted": True}
    is_correct = data.answer.upper() == challenge.correct_answer.upper()
    db.add(DailyChallengeAttempt(
        user_id      = user.id,
        challenge_id = challenge.id,
        answered_at  = now_str(),
        was_correct  = is_correct
    ))
    if is_correct:
        user.progress_score = (user.progress_score or 0) + 5
        user.coins          = (user.coins or 0) + 10
        db.add(Notification(user_id=user.id,
            message="🎉 Daily challenge correct! +5 points, +10 coins", created_at=now_str()))
    db.commit()
    return {
        "correct":        is_correct,
        "correct_answer": challenge.correct_answer,
        "points_earned":  5 if is_correct else 0,
        "coins_earned":   10 if is_correct else 0
    }


# ============================================================
# STUDY HISTORY & STREAK
# ============================================================

@app.post("/study/log")
def log_study_session(data: StudyLog, db=Depends(get_db)):
    user = db.query(User).filter(User.username == data.username).first()
    if not user:
        raise HTTPException(404, "User not found")
    db.add(StudySession(
        user_id    = user.id,
        topic      = data.topic,
        subject    = data.subject,
        level      = data.level,
        studied_at = now_str()
    ))
    user.last_learned_topic = data.topic
    update_streak(user, db)
    newly_earned = check_and_award_badges(user.id, db)
    return {
        "msg":        "Study session logged",
        "streak":     user.study_streak,
        "new_badges": newly_earned or []
    }

@app.get("/study/history/{username}")
def study_history(username: str, db=Depends(get_db)):
    user = db.query(User).filter(User.username == username).first()
    if not user:
        raise HTTPException(404, "User not found")
    sessions = db.query(StudySession).filter(
        StudySession.user_id == user.id
    ).order_by(StudySession.id.desc()).limit(20).all()
    return [
        {"topic": s.topic, "subject": s.subject, "level": s.level, "studied_at": s.studied_at}
        for s in sessions
    ]

@app.get("/study/streak/{username}")
def get_streak(username: str, db=Depends(get_db)):
    user = db.query(User).filter(User.username == username).first()
    if not user:
        raise HTTPException(404, "User not found")
    return {"username": username, "streak": user.study_streak, "last_study_date": user.last_study_date}


# ============================================================
# FRIENDS
# ============================================================

@app.post("/friends/add")
def add_friend(data: FriendRequest, db=Depends(get_db)):
    user   = db.query(User).filter(User.username == data.username).first()
    friend = db.query(User).filter(User.username == data.friend_username).first()
    if not user or not friend:
        raise HTTPException(404, "User not found")
    existing = db.query(Friendship).filter(
        Friendship.user_id == user.id,
        Friendship.friend_id == friend.id
    ).first()
    if existing:
        return {"msg": "Friend request already sent"}
    db.add(Friendship(user_id=user.id, friend_id=friend.id, status="pending"))
    db.add(Notification(user_id=friend.id,
        message=f"👋 {user.username} sent you a friend request!", created_at=now_str()))
    db.commit()
    return {"msg": f"Friend request sent to {data.friend_username}"}

@app.post("/friends/accept")
def accept_friend(data: FriendRequest, db=Depends(get_db)):
    user   = db.query(User).filter(User.username == data.username).first()
    friend = db.query(User).filter(User.username == data.friend_username).first()
    if not user or not friend:
        raise HTTPException(404, "User not found")
    req = db.query(Friendship).filter(
        Friendship.user_id   == friend.id,
        Friendship.friend_id == user.id
    ).first()
    if not req:
        raise HTTPException(404, "Friend request not found")
    req.status = "accepted"
    db.add(Friendship(user_id=user.id, friend_id=friend.id, status="accepted"))
    db.add(Notification(user_id=friend.id,
        message=f"🤝 {user.username} accepted your friend request!", created_at=now_str()))
    db.commit()
    return {"msg": "Friend request accepted"}

@app.get("/friends/{username}")
def get_friends(username: str, db=Depends(get_db)):
    user = db.query(User).filter(User.username == username).first()
    if not user:
        raise HTTPException(404, "User not found")
    friendships = db.query(Friendship).filter(
        Friendship.user_id == user.id,
        Friendship.status  == "accepted"
    ).all()
    result = []
    for f in friendships:
        friend = db.query(User).filter(User.id == f.friend_id).first()
        if friend:
            result.append({
                "username": friend.username,
                "score":    friend.progress_score,
                "streak":   friend.study_streak
            })
    return result


# ============================================================
# AI ADVANCED FEATURES
# ============================================================

@app.post("/ai/study-plan")
def ai_study_plan(data: StudyPlanRequest):
    prompt = f"""Create a detailed weekly study plan for a Nigerian {data.level} student.
    Subjects: {data.subjects}
    Exam date: {data.exam_date}
    Available study hours per day: {data.hours_per_day}
    
    Focus on Nigerian curriculum (WAEC/JAMB style).
    Format as a clear day-by-day schedule with specific topics to cover each day.
    Keep it motivating and practical."""
    plan = get_ai_response(prompt)
    return {"username": data.username, "study_plan": plan}

@app.post("/ai/check-answer")
def check_answer(data: AnswerCheckRequest):
    prompt = f"""A Nigerian student answered a {data.subject} question.
    Question: {data.question}
    Student's answer: {data.student_answer}
    Correct answer: {data.correct_answer}
    
    1. Tell them if they are right or wrong
    2. Explain why the correct answer is right in simple terms
    3. Give a helpful tip to remember this concept
    Keep your response friendly and encouraging."""
    feedback = get_ai_response(prompt)
    is_correct = data.student_answer.strip().upper() == data.correct_answer.strip().upper()
    return {"is_correct": is_correct, "feedback": feedback}

@app.get("/ai/explain-wrong/{topic}")
def explain_wrong_answer(topic: str, wrong_answer: str, correct_answer: str, subject: str = "General"):
    prompt = f"""A student got a question about '{topic}' in {subject} wrong.
    They answered: {wrong_answer}
    The correct answer was: {correct_answer}
    Explain in simple Nigerian student-friendly language why their answer was wrong
    and help them understand the concept better. Be encouraging."""
    explanation = get_ai_response(prompt)
    return {"topic": topic, "explanation": explanation}


# ============================================================
# AI LEARNING & QUIZ (original + enhanced)
# ============================================================

@app.get("/learn/{topic}")
def learn(topic: str, username: str, level: str = "Secondary", subject: str = "General", db=Depends(get_db)):
    prompt = f"Explain '{topic}' to a Nigerian {level} {subject} student in 200 words. Use simple language and Nigerian examples where possible."
    lesson = get_ai_response(prompt)
    user   = db.query(User).filter(User.username == username).first()
    if user:
        user.last_learned_topic = topic
        db.commit()
    return {"topic": topic, "lesson": lesson, "level": level, "subject": subject}

@app.get("/quiz/{topic}")
def smart_quiz(topic: str, level: str = "Secondary", subject: str = "General"):
    prompt = f"""Generate 5 multiple-choice questions about '{topic}' for a Nigerian {level} {subject} student.
    Return ONLY a JSON list:
    [{{"question":"...","options":["A) ...","B) ...","C) ...","D) ..."],"answer":"A","explanation":"...","time_limit_sec":30}}]"""
    raw = get_ai_response(prompt)
    try:
        clean = raw.replace("```json","").replace("```","").strip()
        return {"topic": topic, "level": level, "subject": subject, "quiz": json.loads(clean)}
    except:
        return {"topic": topic, "quiz": raw, "error": "Parsing failed — raw AI response returned"}


# ============================================================
# KIDS GAMES
# ============================================================

@app.get("/games/word-scramble/{subject}")
def word_scramble(subject: str, level: str = "Primary"):
    prompt = f"""Give me 1 educational word related to {subject} for a Nigerian {level} student.
    Return ONLY JSON: {{"word":"...","scrambled":"...","hint":"...","meaning":"..."}}
    The scrambled field must be the word letters randomly rearranged."""
    raw = get_ai_response(prompt)
    try:
        clean = raw.replace("```json","").replace("```","").strip()
        return json.loads(clean)
    except:
        return {"error": "Could not generate word scramble"}

@app.get("/games/spell-challenge/{level}")
def spell_challenge(level: str):
    prompt = f"""Give me 1 spelling challenge word for a Nigerian {level} student.
    Return ONLY JSON: {{"word":"...","hint":"...","example_sentence":"...","difficulty":"easy/medium/hard"}}"""
    raw = get_ai_response(prompt)
    try:
        clean = raw.replace("```json","").replace("```","").strip()
        return json.loads(clean)
    except:
        return {"error": "Could not generate spelling challenge"}

@app.get("/games/math-challenge/{level}")
def math_challenge(level: str):
    prompt = f"""Generate 1 fun math problem for a Nigerian {level} student.
    Return ONLY JSON: {{"question":"...","answer":"...","solution_steps":"...","difficulty":"easy/medium/hard"}}"""
    raw = get_ai_response(prompt)
    try:
        clean = raw.replace("```json","").replace("```","").strip()
        return json.loads(clean)
    except:
        return {"error": "Could not generate math challenge"}

@app.get("/games/treasure-hunt/{level}")
def treasure_hunt(level: str, subject: str = "General"):
    prompt = f"""Create a fun educational treasure hunt clue for a Nigerian {level} student about {subject}.
    Return ONLY JSON: {{"clue":"...","question":"...","answer":"...","reward_coins":5,"fun_fact":"..."}}"""
    raw = get_ai_response(prompt)
    try:
        clean = raw.replace("```json","").replace("```","").strip()
        return json.loads(clean)
    except:
        return {"error": "Could not generate treasure hunt"}

@app.post("/games/save-score")
def save_game_score(data: GameScoreSave, db=Depends(get_db)):
    user = db.query(User).filter(User.username == data.username).first()
    if not user:
        raise HTTPException(404, "User not found")
    db.add(GameScore(
        user_id   = user.id,
        game_type = data.game_type,
        score     = data.score,
        played_at = now_str()
    ))
    # Award coins for playing
    coins_earned = max(1, data.score // 10)
    user.coins   = (user.coins or 0) + coins_earned
    db.commit()
    return {"msg": "Score saved", "coins_earned": coins_earned, "total_coins": user.coins}

@app.get("/games/leaderboard/{game_type}")
def game_leaderboard(game_type: str, db=Depends(get_db)):
    scores = db.query(GameScore).filter(GameScore.game_type == game_type).order_by(
        GameScore.score.desc()
    ).limit(10).all()
    result = []
    for i, s in enumerate(scores):
        u = db.query(User).filter(User.id == s.user_id).first()
        if u:
            result.append({
                "rank":      i + 1,
                "username":  u.username,
                "score":     s.score,
                "played_at": s.played_at
            })
    return result


# ============================================================
# NOTIFICATIONS
# ============================================================

@app.get("/notifications/{username}")
def get_notifications(username: str, db=Depends(get_db)):
    user = db.query(User).filter(User.username == username).first()
    if not user:
        raise HTTPException(404, "User not found")
    notifs = db.query(Notification).filter(
        Notification.user_id == user.id
    ).order_by(Notification.id.desc()).limit(20).all()
    return [
        {"id": n.id, "message": n.message, "is_read": n.is_read, "created_at": n.created_at}
        for n in notifs
    ]

@app.post("/notifications/mark-read/{notif_id}")
def mark_notification_read(notif_id: int, db=Depends(get_db)):
    n = db.query(Notification).filter(Notification.id == notif_id).first()
    if not n:
        raise HTTPException(404, "Notification not found")
    n.is_read = True
    db.commit()
    return {"msg": "Marked as read"}

@app.post("/notifications/mark-all-read/{username}")
def mark_all_read(username: str, db=Depends(get_db)):
    user = db.query(User).filter(User.username == username).first()
    if not user:
        raise HTTPException(404, "User not found")
    db.query(Notification).filter(
        Notification.user_id == user.id,
        Notification.is_read == False
    ).update({"is_read": True})
    db.commit()
    return {"msg": "All notifications marked as read"}


# ============================================================
# ADMIN
# ============================================================

@app.post("/admin/add-subject")
def add_subject(data: SubjectCreate, db=Depends(get_db)):
    s = Subject(name=data.name, level=data.level)
    db.add(s)
    db.commit()
    return {"id": s.id, "msg": "Subject added"}

@app.post("/admin/add-topic")
def add_topic(data: TopicCreate, db=Depends(get_db)):
    t = Topic(title=data.title, subject_id=data.subject_id)
    db.add(t)
    db.commit()
    return {"id": t.id, "msg": "Topic added"}


# ============================================================
# TAVILY WEB SEARCH
# ============================================================

@app.get("/search/web/{query}")
def search_web(query: str):
    if not TAVILY_KEY:
        return {"error": "Tavily API Key missing"}
    try:
        res = requests.post("https://api.tavily.com/search", json={
            "api_key":      TAVILY_KEY,
            "query":        query + " Nigeria education",
            "search_depth": "basic",
            "max_results":  5
        }, timeout=10)
        if res.status_code == 200:
            return {
                "query": query,
                "results": [
                    {"title": r["title"], "url": r["url"], "snippet": r["content"][:150] + "..."}
                    for r in res.json()["results"]
                ]
            }
    except Exception as e:
        return {"error": f"Search failed: {str(e)}"}
    return {"error": "No results found"}


# ============================================================
# WEBSOCKET LIVE CLASSROOM
# ============================================================

active_connections: dict = {}

@app.websocket("/ws/classroom/{room}")
async def websocket_endpoint(websocket: WebSocket, room: str):
    await websocket.accept()
    if room not in active_connections:
        active_connections[room] = []
    active_connections[room].append(websocket)
    room_count = len(active_connections[room])
    await websocket.send_json({
        "type":    "system",
        "message": f"✅ Connected to classroom '{room}' — {room_count} student(s) online"
    })
    # Broadcast join event
    for conn in active_connections[room]:
        if conn != websocket:
            await conn.send_json({"type": "system", "message": f"👤 A new student joined the room"})
    try:
        while True:
            data = await websocket.receive_json()
            msg  = data.get("message", "")
            user = data.get("username", "Student")
            # Broadcast message to all in room
            for conn in active_connections[room]:
                await conn.send_json({"type": "chat", "username": user, "message": msg})
            # AI tutor command
            if msg.startswith("/ai"):
                query    = msg.replace("/ai", "").strip()
                response = get_ai_response(
                    f"You are a Nigerian school AI tutor. Answer this student's question clearly: {query}"
                )
                for conn in active_connections[room]:
                    await conn.send_json({"type": "ai", "username": "🤖 Tutor Bot", "message": response})
    except WebSocketDisconnect:
        if websocket in active_connections.get(room, []):
            active_connections[room].remove(websocket)
        for conn in active_connections.get(room, []):
            await conn.send_json({"type": "system", "message": "👤 A student left the room"})
