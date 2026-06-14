from fastapi import FastAPI, File, UploadFile 
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from pypdf import PdfReader

from sqlalchemy import create_engine, Column, Integer, Text
from sqlalchemy.orm import declarative_base, sessionmaker

import re
import random

app = FastAPI()

# SQLITE DATABASE

DATABASE_URL = "sqlite:///./flashcards.db"

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False}
)

SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine
)

Base = declarative_base()

class Flashcard(Base):
    __tablename__ = "flashcards"

    id = Column(Integer, primary_key=True, index=True)
    question = Column(Text)
    answer = Column(Text)
    source_text = Column(Text)
    document_name = Column(Text)

Base.metadata.create_all(bind=engine)

# CORS

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Iinput model

class InputText(BaseModel):
    text: str

generator = None


#load model

@app.on_event("startup")
def load_model():
    global generator

    try:
        from transformers import pipeline

        print("Loading FLAN-T5 model...")

        generator = pipeline(
            "text2text-generation",
            model="google/flan-t5-base",
            tokenizer="google/flan-t5-base"
        )

        print("Model loaded successfully!")

    except Exception as e:
        print("MODEL LOAD ERROR:", e)
        generator = None

# pdf extraction
def extract_text_from_pdf(file):
    reader = PdfReader(file)

    text = ""
    for page in reader.pages:
        try:
            content = page.extract_text()
            if content:
                text += content + " "
        except:
            continue

    return text


# clean text
def clean_text(text):
    text = re.sub(r'\b\d+\b', ' ', text)
    text = re.sub(r'\s+', ' ', text)
    text = re.sub(r'[^\w\s.,!?()-]', '', text)
    return text.strip()

#extract sentences

def extract_sentences(text):
    text = clean_text(text)
    sentences = re.split(r'(?<=[.!?])\s+', text)

    cleaned = [s.strip() for s in sentences if 8 <= len(s.split()) <= 25]

    return cleaned

# sentence seection

def select_best_sentences(sentences):

    if not sentences:
        return []

    scored = []

    keywords = [
        
     "system", "architecture", "process", "model",
     "function", "role", "cause", "effect", "defined",
     "design", "problem", "solution"
]
    

    for s in sentences:
        score = sum(1 for k in keywords if k in s.lower())
        score += len(s.split()) * 0.03
        scored.append((score, s))

    scored.sort(reverse=True, key=lambda x: x[0])

    top = sorted(scored, key=lambda x: x[0], reverse=True)[:20]
    top = [s for _, s in top]

    random.shuffle(top)

    return top
#quality check
def quality_check(question, answer):

    if not question or not answer:
        return False

    if len(question.split()) < 6:
        return False

    if len(answer.split()) < 3:
        return False

    bad_words = [
        "exam",
        "test",
        "quiz",
        "student",
        "teacher",
        "professor",
        "classroom",
        "lecture"
    ]

    q_lower = question.lower()
    vague_phrases = [
    "the two models",
    "this model",
    "of this",
    "between the two",
    "these components",
    "this system",
    "the system",
    "they",
    "them"
    ]

    for phrase in vague_phrases:
     if phrase in q_lower:
        return False

    if any(word in q_lower for word in bad_words):
        return False

    bad_starts = [
        
        "what is the purpose",
        "what is the purpose of this",
        "what is the main idea",
        "what is the goal",
        "what is the title",
        "what is the key characteristic",
        "what are the key characteristics",
        "what are the main features",
        "what are the primary reasons",
        "what are the main reasons",
        "what is essential",
        "what is required",
        "what is particularly important",
        "what is one of the most",
        "what is the most important",
        "what is the main feature",
        "what is the name of",
        "what can be done",
        "what is especially important"
    ]

    if any(q_lower.startswith(x) for x in bad_starts):
        return False

    return True
def extract_answer(chunk):
    sentences = chunk.split(". ")
    sentences = [s.strip() for s in sentences if len(s.split()) > 4]
    return ". ".join(sentences[:2])  # HARD LIMIT
def question_too_similar(question, source):

    q_words = set(question.lower().split())
    s_words = set(source.lower().split())

    overlap = len(q_words & s_words)

    return overlap > len(q_words) * 0.9


# FLASHCARD generation

def generate_flashcard(sentence):

    if generator is None:
        return None

    try:

        prompt = f"""

Generate ONE high-quality study question from the text.

RULES:
- Question must focus on ONE concept only.
- No pronouns such as "this", "it", "they", or "these".
- Explicitly name the concept being discussed.
- Prefer definition, explanation, function, role, process, cause, or purpose questions.
- Do NOT generate multiple questions.
- Output ONLY the question.


TEXT:
{sentence}

QUESTION:
"""


        result = generator(
            prompt,
            max_length=80,
            do_sample=False,
            
        )[0]["generated_text"]

        question = result.strip()
        if question_too_similar(question, sentence):
           return None
        if len(set(question.lower().split()) & set(sentence.lower().split())) < 2:
           return None

        if len(question.split()) < 4:
            return None
        
        if not quality_check(question, sentence):
          return None

        bad_patterns = [
            "true or false",
            "which of the following",
            

        ]

        if any(p in question.lower() for p in bad_patterns):
            return None

        if not question.endswith("?"):
            question += "?"

        return {
            "Q": question,
            "A": extract_answer(sentence)
        }

    except Exception as e:
        print("GEN ERROR:", e)
        return None

# duplicates

def is_duplicate(question, used_questions):
    q = set(question.lower().split())

    for u in used_questions:
        u_set = set(u.lower().split())

        if len(q & u_set) > 7: #similar words
            return True

    return False
# MAIN PIPELINE

def generate_flashcards(text, document_name="Manual Text"):

    db = SessionLocal()

    sentences = extract_sentences(text)

    if not sentences:
        return []

    selected = select_best_sentences(sentences)

    flashcards = []
    used_questions = set()

    for s in selected:

        card = generate_flashcard(s)   #one card per sentence

        if not card:
         continue

         #duplicate check
        if is_duplicate(card["Q"], used_questions):
          continue

        flashcards.append(card)
        used_questions.add(card["Q"])

        db.add(Flashcard(
            question=card["Q"],
            answer=card["A"],
            source_text=text[:1000],
            document_name=document_name
       ))

    db.commit()
    db.close()

    return flashcards

# TEXT ENDPOINT


@app.post("/generate")
def generate(data: InputText):

    return {
        "flashcards": generate_flashcards(data.text)
    }

# PDF ENDPOINT

@app.post("/upload-pdf")
async def upload_pdf(file: UploadFile = File(...)):

    try:
        text = extract_text_from_pdf(file.file)

        if not text.strip():
            return {"error": "No readable text found in PDF"}

        flashcards = generate_flashcards(text, file.filename)

        return {"flashcards": flashcards}

    except Exception as e:
        return {"error": str(e)}


# VIEW SAVED FLASHCARDS

@app.get("/flashcards")
def get_flashcards():

    db = SessionLocal()

    cards = db.query(Flashcard).all()

    results = []

    for c in cards:
        results.append({
            "id": c.id,
            "Q": c.question,
            "A": c.answer,
            "document": c.document_name
        })

    db.close()

    return results

#delete individual flashcard
@app.delete("/flashcards/{card_id}")
def delete_flashcard(card_id: int):

    db = SessionLocal()

    card = db.query(Flashcard).filter(
        Flashcard.id == card_id
    ).first()

    if not card:
        db.close()
        return {"error": "Flashcard not found"}

    db.delete(card)
    db.commit()
    db.close()

    return {"message": "Flashcard deleted"}

# CLEAR DATABASE


@app.delete("/flashcards")
def clear_flashcards():

    db = SessionLocal()
    db.query(Flashcard).delete()
    db.commit()
    db.close()

    return {"message": "All flashcards deleted"}


# HEALTH check
@app.get("/health")
def health():
    return {"status": "ok"}
    
    
