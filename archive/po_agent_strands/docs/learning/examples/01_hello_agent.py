"""Lesson 1 — the agent loop.

An Agent is model + system prompt + tools + conversation. Calling it runs the
loop (model -> tools -> model ... -> answer) and returns an AgentResult.

    uv run docs/learning/examples/01_hello_agent.py [--live]
"""

from _common import banner, model

from strands import Agent
from tests.fakes import text

banner("01 hello agent")

agent = Agent(
    model=model([[text("Hi! I'm a PM assistant.")], [text("You asked me to say hello in one sentence.")]]),
    system_prompt="You are a concise project-management assistant.",
    callback_handler=None,  # default would stream tokens to stdout
)

result = agent("Say hello in one sentence.")
print("stop_reason:", result.stop_reason)        # "end_turn" — the model finished
print("answer     :", str(result).strip())       # str(result) is the final text

# The conversation is plain data you can inspect (and share — see lesson 4).
result = agent("What did I just ask you?")
print("answer 2   :", str(result).strip())
print("\nagent.messages (Bedrock Converse shape):")
for m in agent.messages:
    print(f"  {m['role']:9} {m['content']}")
