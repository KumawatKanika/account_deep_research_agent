import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List
import os
import sys

from dotenv import load_dotenv  
load_dotenv()
# Ensure we can import from the current directory
current_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.append(current_dir)
parent_dir = os.path.dirname(current_dir)
sys.path.append(parent_dir)

from test_main import deep_researcher as graph
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve static files from the src/static directory
# We assume the user wants to reuse the existing frontend
static_dir = os.path.join(parent_dir, "src", "static")
if not os.path.exists(static_dir):
    # Fallback to local static dir if src/static doesn't exist
    static_dir = os.path.join(current_dir, "static")
    if not os.path.exists(static_dir):
        os.makedirs(static_dir)

app.mount("/static", StaticFiles(directory=static_dir), name="static")

class Message(BaseModel):
    role: str
    content: str

class ChatRequest(BaseModel):
    message: str
    history: List[Message] = []

@app.get("/", response_class=HTMLResponse)
async def read_root():
    index_path = os.path.join(static_dir, "index.html")
    if os.path.exists(index_path):
        with open(index_path, "r") as f:
            return f.read()
    return "<h1>Test Agent Server is Running</h1><p>Go to /static/index.html if you created it, or ensure it exists in src/static.</p>"

@app.post("/chat")
async def chat(request: ChatRequest):
    try:
        # Reconstruct history
        messages = []
        for msg in request.history:
            if msg.role == "user":
                messages.append(HumanMessage(content=msg.content))
            elif msg.role == "assistant":
                messages.append(AIMessage(content=msg.content))
            
        # Add new message
        messages.append(HumanMessage(content=request.message))
        
        initial_input = {
            "messages": messages,
            "clarification_loop_count": 0
        }
        
        # Invoke graph
        final_state = await graph.ainvoke(initial_input)
        
        # Get the last message from the agent
        output_messages = final_state.get("messages", [])
        last_message = output_messages[-1]
        
        response_text = last_message.content if last_message else "No response generated."
        
        return {
            "response": response_text,
            "status": final_state.get("status"),
            "buyer_entity": final_state.get("buyer_entity")
        }
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    print("Starting test server at http://localhost:8000")
    uvicorn.run(app, host="127.0.0.1", port=8000)

