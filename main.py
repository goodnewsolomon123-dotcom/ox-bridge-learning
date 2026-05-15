
import os
from fastapi import FastAPI
import requests

# --- CONFIG ---
DATABASE_URL = os.getenv("DATABASE_URL")
GROQ_KEY = os.getenv("GROQ_KEY")

app = FastAPI(title="Ox-Bridge Learning Hub")

@app.get("/")
def read_root():
    # This endpoint helps us debug if the server is running
    return {
        "status": "Running", 
        "db_connected": "Yes" if DATABASE_URL else "No (Missing Env Var)",
        "groq_key_present": "Yes" if GROQ_KEY else "No (Missing Env Var)"
    }

@app.get("/learn/{topic}")
def learn(topic: str):
    if not GROQ_KEY:
        return {"error": "Groq Key is missing in Render Environment Variables"}
        
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": "llama-3.1-8b-instant", 
        "messages": [{"role": "user", "content": f"Explain {topic} simply."}]
    }
    try:
        res = requests.post(url, headers=headers, json=payload, timeout=10)
        if res.status_code == 200:
            return {"topic": topic, "lesson": res.json()['choices'][0]['message']['content']}
        else:
            return {"error": f"Groq API Error: {res.status_code}", "details": res.text}
    except Exception as e:
        return {"error": str(e)}
