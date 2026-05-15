
import os
from fastapi import FastAPI, Depends, HTTPException
from sqlalchemy import create_engine, Column, Integer, String
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from pydantic import BaseModel
import requests

# --- CONFIG (Using Environment Variables) ---
DATABASE_URL = os.getenv("DATABASE_URL")
GROQ_KEY = os.getenv("GROQ_KEY")

if not DATABASE_URL or not GROQ_KEY:
    raise Exception("Missing Environment Variables! Check Render Settings.")

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
app = FastAPI(title="Ox-Bridge Learning Hub")

def get_db():
    db = SessionLocal()
    try: yield db
    finally: db.close()

@app.get("/")
def read_root():
    return {"message": "Ox-Bridge Learning Hub API is Live!"}

@app.get("/learn/{topic}")
def learn(topic: str):
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": "llama-3.1-8b-instant", 
        "messages": [{"role": "user", "content": f"Explain {topic} simply to a student."}]
    }
    try:
        res = requests.post(url, headers=headers, json=payload, timeout=10)
        if res.status_code == 200:
            return {"topic": topic, "lesson": res.json()['choices'][0]['message']['content']}
    except:
        return {"error": "AI service busy"}
    
    return {"error": "Failed to get lesson"}
