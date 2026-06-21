from fastapi import FastAPI, File, UploadFile 
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from pypdf import PdfReader

from sqlalchemy import create_engine, Column, Integer, Text
from sqlalchemy.orm import declarative_base, sessionmaker

import re
import random

app = FastAPI() #we use fastapi as our backend to communicate with the frontend

# SQLITE DATABASE LAYER

DATABASE_URL = "sqlite:///./flashcards.db"

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} #we check same thread to false beacuse fastapi operates in a loop
)
#configure session factory to establish scopes for database
SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine
)

Base = declarative_base()

class Flashcard(Base):  #flashcard database column names
    __tablename__ = "flashcards"

    id = Column(Integer, primary_key=True, index=True)
    question = Column(Text)
    answer = Column(Text)
    source_text = Column(Text)
    document_name = Column(Text)

Base.metadata.create_all(bind=engine) #automatically initiate tables upon applcation

# CORS (cross origin resource sharing)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# data validation model 
class InputText(BaseModel):
    text: str

generator = None


#load model on our terminal is the first step

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

# pdf extraction and preprocessing 
def extract_text_from_pdf(file):
    reader = PdfReader(file)

    text = ""
    for page in reader.pages:  #skip corrupted layout pages
        try:
            content = page.extract_text()
            if content:
                text += content + " "
        except:
            continue

    return text


# clean text
def clean_text(text):  #remove noise expressions
    text = re.sub(r'\b\d+\b', ' ', text)
    text = re.sub(r'\s+', ' ', text)
    text = re.sub(r'[^\w\s.,!?()-]', '', text)
    return text.strip()

#extract sentences

def extract_sentences(text):
    text = clean_text(text)
    sentences = re.split(r'(?<=[.!?])\s+', text)

    cleaned = [s.strip() for s in sentences if 8 <= len(s.split()) <= 25]
#sentences shorter than 8 words lack context for nlp and longer than 25 causes hallluciantions
    return cleaned

# sentence section  (heurestic ranking alogirthm)

def select_best_sentences(sentences):

    if not sentences:
        return []

    scored = []

    keywords = [  #keywords chosen to give exam style questions
        # with the important concepts from input
        
     "system", "architecture", "process", "model",
     "function", "role", "cause", "effect", "defined",
     "design", "problem", "solution"
]
    

    for s in sentences:    #add small length penalty bias
        score = sum(1 for k in keywords if k in s.lower())
        score += len(s.split()) * 0.03
        scored.append((score, s))

    scored.sort(reverse=True, key=lambda x: x[0]) #sort down candiadates by descending score weight

    top = sorted(scored, key=lambda x: x[0], reverse=True)[:20] #extract top 20
    #sort candidate sentences in order
    top = [s for _, s in top]

    random.shuffle(top)

    return top
#quality check
def quality_check(question, answer):

    if not question or not answer:
        return False

    if len(question.split()) < 6: #filter out short questions
        return False

    if len(answer.split()) < 3: #filter out useless o incomplete explanationstions
        return False

    bad_words = [  #block context that have textbook facts
        "exam",
        "test",
        "quiz",
        "student",
        "teacher",
        "professor",
        "classroom",
        "lecture"
    ]

    q_lower = question.lower() #elimate non specific,vauge questions
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

    bad_starts = [  #eliminate bad starts that generates questions that dont make sense
        
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
    #rule based answer extractions
def extract_answer(chunk):  
    sentences = chunk.split(". ")  #takes soource chunk text block and splits based on the periods
    sentences = [s.strip() for s in sentences if len(s.split()) > 4] #loops throough split sentences and discards sentences less than 4 words
    return ". ".join(sentences[:2])  # joins two sentences from the chunk w,th a full stop to make an answer

def question_too_similar(question, source): #strictly makes sure that model does not 
                                             #give questions that directly copy context sentence

    q_words = set(question.lower().split())
    s_words = set(source.lower().split())

    overlap = len(q_words & s_words)

    return overlap > len(q_words) * 0.9


# FLASHCARD generation with ai model and prompt engineering

def generate_flashcard(sentence):

    if generator is None:
        return None

    try:   #our final prompt given to flan t5 ,our prompt engineering block

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
            do_sample=False, #deterministic greedy decoding to reduce hallucination anomalies
            
        )[0]["generated_text"]

        question = result.strip()
        if question_too_similar(question, sentence):  #generates actual flashcards questions
                                             #not just sentence or paragraph showing as the question
           return None
        if len(set(question.lower().split()) & set(sentence.lower().split())) < 2:
           return None  #leaves out sentences who are too short to be made into questions

        if len(question.split()) < 4:  #really short questions get filtered out
            return None
        
        if not quality_check(question, sentence): #quality check
          return None

        bad_patterns = [  #we want real Q&A not  anoother question format
            "true or false",
            "which of the following",
            

        ]

        if any(p in question.lower() for p in bad_patterns):
            return None

        if not question.endswith("?"):
            question += "?"

        return {
            "Q": question, #flan t5 gives question
            "A": extract_answer(sentence) #answer is generated from sentence
        }

    except Exception as e:
        print("GEN ERROR:", e)  #error exception
        return None

# duplicates

def is_duplicate(question, used_questions): #this is implemented to check for similarity again
    q = set(question.lower().split())

    for u in used_questions:  
        u_set = set(u.lower().split())

        if len(q & u_set) > 7: # if more than 7 simialr words classify as duplicates
         return True

    return False
# MAIN PIPELINE

def generate_flashcards(text, document_name="Manual Text"):

    db = SessionLocal() #openes database

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
    
    

    
    
