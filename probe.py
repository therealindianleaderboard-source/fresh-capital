"""Throwaway: is it the model's request quota, or is grounding just unavailable free?"""
import os
from google import genai
c = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
M = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")
for label, kw in [("plain", {}), ("grounded", {"tools": [{"type": "google_search"}]})]:
    try:
        r = c.interactions.create(model=M, input="Reply with the single word OK.", **kw)
        print(f"{label}: OK -> {r.output_text[:60]!r}")
    except Exception as e:
        print(f"{label}: {type(e).__name__}: {str(e)[:150]}")
