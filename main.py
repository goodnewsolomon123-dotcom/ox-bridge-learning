
from fastapi import FastAPI, Depends, HTTPException
from sqlalchemy import create_engine, Column, Integer, String
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from pydantic import BaseModel
import requests
import os # Import os module to access environment variables

# --- CONFIG ---
# DATABASE_URL should also ideally come from an environment variable for deployment
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://neondb_owner:npg_UElyr9BSK5OH@ep-bold-hall-aq15g941-pooler.c-8.us-east-1.aws.neon.tech/neondb?sslmode=require&channel_binding=require")
GROQ_KEY = os.environ.get("GROQ_KEY") # Load Groq key from environment variable

# --- DB SETUP ---
engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True)
    email = Column(String, unique=True)
    hashed_password = Column(String)

Base.metadata.create_all(bind=engine)

# --- APP ---
app = FastAPI()

def get_db():
    db = SessionLocal()
    try: yield db
    finally: db.close()

@app.get("/")
def read_root():
    return {"message": "Ox-Bridge Learning Hub API is Live!"}

@app.get("/learn/{topic}")
def learn(topic: str):
    # Simple AI call
    if not GROQ_KEY:
        return {"error": "GROQ_KEY not set in environment variables"}
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json"}
    payload = {"model": "llama-3.1-8b-instant", "messages": [{"role": "user", "content": f"Explain {topic} simply."}]}
    res = requests.post(url, headers=headers, json=payload)
    if res.status_code == 200:
        return {"topic": topic, "lesson": res.json()['choices'][0]['message']['content']}
    return {"error": "AI busy or API key invalid"}
