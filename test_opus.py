from dotenv import load_dotenv
load_dotenv()
import importlib, tradingagents.crypto.agents
importlib.reload(tradingagents.crypto.agents)
from tradingagents.crypto import agents as ag

print("JUDGE_MODEL resolves to:", ag._resolve_model("9router", "judge"))
try:
    result = ag._call_llm(
        "You are an expert.",
        "Reply with exactly: OPUS TEST OK",
        model=ag.JUDGE_MODEL,
        max_tokens=30,
    )
    print("Response:", result)
except Exception as e:
    print(f"Error: {type(e).__name__}: {e}")
