from dotenv import load_dotenv
load_dotenv()
# Force re-import (clear cached client)
import importlib, tradingagents.crypto.agents
importlib.reload(tradingagents.crypto.agents)
from tradingagents.crypto import agents as ag

result = ag._call_llm("You are a helpful test bot.", "Reply with exactly: KIRO TEST OK", max_tokens=30)
print(f"Provider: {ag._provider}")
print(f"Response: {result}")
