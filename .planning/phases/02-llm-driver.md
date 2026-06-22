# Phase 2 — LLM at the Steering Wheel (`ohmni_agentic` live)

**Goal:** A natural-language operator session — talk to the robot, it reasons over its sensors and skills, and acts. The blueprint is wired; just needs deps + an API key.

## Install LLM extras

```bash
source .venv/bin/activate
pip install "langchain==1.2.3" langgraph langchain-openai
```

`agent.py` imports `from langgraph.graph.state import CompiledStateGraph` and `from langchain_core.tools import StructuredTool`, so both packages are required even if we use a non-OpenAI provider.

## Provider auth

Set one of:
- `OPENAI_API_KEY=...` (default path; cheapest is `gpt-5-mini` for tool-use; `gpt-5` for spatial reasoning)
- `ANTHROPIC_API_KEY=...` + swap `dimos.agents.agent` to use Claude (requires a small adapter)
- Local Ollama (`ollama pull llama3.1:70b`); use `unitree_go2_agentic_ollama.py` as the template

Stored in `~/.zshrc` or `.env`, NOT in any planning file.

## Person-follow needs Qwen-VL + EdgeTAM

`person_follow_skill` does VL detection ("find the person matching: blue shirt") then visual servoing. Extra deps:

```bash
pip install transformers accelerate sentencepiece
# EdgeTAM weights: download once and cache
```

Mac MPS works for both at reduced precision. Slow but real.

## Run

```bash
.venv/bin/python -m dimos.cli run ohmni-agentic --robot-ip 192.168.1.194
```

Web UI on `http://localhost:8765` for chat input; agent output streams to console.

## Acceptance

- Type "drive forward 1 meter then turn around" — robot does it.
- Type "what room are you in?" — robot grabs camera frame, VLM describes scene, replies.
- Type "follow me" — person_follow tracks the visible person at 1.5 m.
- Type "tag this spot as 'kitchen'" — SpatialMemory stores the current pose.
- Later: "go to kitchen" — robot navigates back via the planner.

## Cost / latency notes

- Single tool call ≈ 1.5–4 s round trip on `gpt-5-mini` with prompt cache hits (cache the system prompt + skill schemas; everything else short).
- Frame-by-frame VLM calls are wasteful; gate `observe` calls on agent-explicit requests, not every loop tick.
