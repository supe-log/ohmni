# dimos_ohmni.autoresearch

Robot-shaped adaptation of [karpathy/autoresearch](https://github.com/karpathy/autoresearch).

## The pattern

karpathy's `program.md` boils down to:

> LOOP FOREVER:
>   1. propose an experimental change
>   2. run experiment with fixed time budget
>   3. read out a metric
>   4. decide: keep | discard | crash
>   5. log to results.tsv

For an LLM training rig the "experiment" is editing `train.py` and re-running. For a robot, each *microloop* targets one **behavior knob** of the running stack. The propose / run / score / decide / log shape is identical.

## Files

```
loop_base.py    — Loop abstract base + LoopResult dataclass
journal.py      — append-only TSV at ~/.ohmni/research/journal.tsv
microloops/
    calibration.py        — tunes wheel-encoder & safety constants
    skill_probe.py        — runs single skills, scores success/fail
    web_research.py       — pulls external knowledge into brain.md
    exploration.py        — tunes brain proposal cadence + revisit bias
orchestrator.py — AutoResearchOrchestrator Module — schedules microloops
```

## Adding a microloop

```python
from dimos_ohmni.autoresearch import Loop

class MyLoop(Loop):
    name = "my_loop"
    budget_s = 30.0

    def propose(self):
        return {"knob": "thing=42", "thing": 42, "notes": "trying 42"}

    def apply(self, proposal):
        # mutate state, return rollback info
        return prev_value

    def run(self, proposal, budget_s):
        time.sleep(budget_s)
        return {"observation": measured}

    def score(self, observations):
        return float(observations["observation"])  # higher = better

    def rollback(self, proposal, prev_value):
        # restore prev_value if desired
        pass
```

Then register it in `orchestrator.py`'s `_register_default_loops`.

## The journal

`~/.ohmni/research/journal.tsv` is the cross-loop ledger. Read it to see what every loop has tried and what worked. Skills:

- `autoresearch_status()` — current weights and last scores per loop
- `autoresearch_recent(n=20)` — last N journal entries
- `autoresearch_run_now(loop_name)` — force one tick

## Safety

Every microloop is **bounded**:

- Time budget per tick (5–90s, declared on the class).
- Knob values are clamped to a `(lo, hi)` range, not unbounded.
- All mutations are *configuration*, not source. The orchestrator never edits `.py` files at runtime.
- Failed ticks call `rollback(proposal, info)` to restore prior state.
- The only "physical" side effect any loop has on the robot is the same skill set the agent has access to (drive small distance, set neck, etc.) — bounded by the SafetyGovernor.

Pause everything by setting `OHMNI_AUTORESEARCH=0` before launching the stack.

## Microloop design notes

### CalibrationLoop
- Knobs: `OHMNI_APOS_PER_M`, `OHMNI_WHEELBASE_M`, `SAFETY_IMMINENT_M`.
- Score: `goal_reach_rate = reached / published` over a 30s window from `/tmp/ohmni_full.log`.
- Effect: writes `~/.ohmni/calibration.env`. Requires a stack restart to actually apply (we don't hot-reload env into a running process).

### SkillProbeLoop
- Picks one skill from a fixed list (`say`, `set_led`, `set_neck_angle`, `drive_forward`, `drive_rotate`, `get_battery`).
- Drives a *small* test action (5cm forward, 2s rotation) and reads the relevant sensor.
- Score: 1.0 if the expected effect is observed, else 0.0.
- Output: per-skill success rate over time, visible in the journal.

### WebResearchLoop
- Reads recent `brain.md` entries, finds questions or unfamiliar nouns, falls back to a fixed topic list.
- Calls `WebResearcher.web_search`, appends top-3 results to brain as `[research]` lines with URL + snippet.
- Score: number of distinct sources injected per cycle.

### ExplorationTuningLoop
- Knobs: `propose_interval_s`, `revisit_bias`, `outward_radius_frac`.
- Score: `unique_cells_visited / minute` (read from `[explore]` entries in `brain.md`).
- Effect: writes `~/.ohmni/exploration.json`. **Hot-reloaded** by `BrainResearcher` on every tick — no restart needed.

## How loops compose

The orchestrator picks one loop per scheduling cycle, weighted by `weight × (1 + last_delta)`. Loops that produced positive deltas recently get more cycles; stagnant ones decay toward `min_weight=0.1`. EMA alpha=0.3 means each tick's signal is 30% of the new weight.

This is a soft scheduler — every loop runs *some* of the time, even if it's been stagnant, so we don't lock in a wrong baseline.

## Running fully free (no paid API keys)

The whole stack runs zero-cost. What each free choice trades off:

### LLM (Agent + autoresearch proposer)
**Paid:** OpenAI GPT-5-mini ($0.15/$0.60 per 1M tokens). Best tool-use accuracy, low latency.

**Free:** [Ollama](https://ollama.com) on Mac silicon. Models that handle tool use:
```bash
brew install ollama
ollama serve &
ollama pull llama3.1:8b   # 8B params, ~5GB, ok at tools
ollama pull qwen2.5:7b    # 7B params, ~4.5GB, better at tools
ollama pull qwen2.5:32b   # 32B, ~20GB, near GPT-4-mini quality
```
Honest gap: Local LLMs are **3–8× slower** and make ~10–20% more wrong tool calls on multi-step tasks. For a robot loop where each tick is 30s anyway, the speed matters less than the accuracy. To wire dimos to Ollama, copy the pattern in `dimos/dimos/robot/unitree/go2/blueprints/agentic/unitree_go2_agentic_ollama.py`.

### TTS (SpeakSkill)
**Paid:** OpenAI TTS (~$15/1M chars).

**Free:** Robot-side Android TTS via `OhmniConnection.say` — already wired, on-device, $0. The agent can call `say("hello")` and the robot speaks. Skip OpenAI TTS unless you specifically need a particular voice.

### VLM (SemanticPin describe_fn)
**Paid:** GPT-5 vision, Gemini Flash.

**Free:** [Moondream](https://huggingface.co/vikhyatk/moondream2) (~3GB) or [Qwen2-VL](https://huggingface.co/Qwen/Qwen2-VL-7B-Instruct) (~10GB). Both run on Mac silicon, no API key, public weights.
```python
from dimos_ohmni.perception import SemanticPin, SemanticPinConfig
SemanticPinConfig(vlm_model="moondream")  # auto-downloads on first call
```
Honest read: Moondream is **genuinely good** for "what's the dominant object/place" — your exact use case. Don't pay for vision unless you need GPT-5-class spatial reasoning.

### Web search
**Paid:** Brave (2k/mo free, then ~$3/1k), SerpAPI (~$50/mo).

**Free, in priority order:**
1. **searxng** — meta-search aggregator, JSON API. Set `SEARXNG_URL=https://searx.be` (or self-host) and `WebResearcher.web_search` uses it automatically. Most robust.
2. **DuckDuckGo HTML scrape** — fallback. Currently used. Works but flaky.
3. **arXiv API** — `WebResearcher.arxiv_search(query)`. Free, structured, papers only.
4. **Wikipedia API** — `WebResearcher.wiki_search(query)`. Free, structured, canonical facts.
5. **GitHub Search API** — `GitHubResearchLoop` uses it. Free; **5000 req/hr with `GITHUB_TOKEN`** vs 10/min anon. Strongly recommend a token (free PAT, no $ cost).

### Web scraping / article extraction
**Paid:** Firecrawl ($50/mo+), Browserless.

**Free:** [trafilatura](https://github.com/adbar/trafilatura) — best free open-source article extractor, already wired into `WebResearcher.read_url`. `pip install trafilatura`.

### Recommended free-tier env

```bash
# All free. The robot will literally never bill you a cent.

# Optional but huge: 5000 GitHub req/hr instead of 10/min
export GITHUB_TOKEN=github_pat_xxx       # free at github.com/settings/tokens

# Optional: searxng instance for cleaner web search
export SEARXNG_URL=https://searx.be      # or your self-hosted instance

# Optional: tighter local resource control
export OHMNI_AUTORESEARCH=1              # default; '0' to pause
export OHMNI_APOS_PER_M=10860            # wheel-encoder calibration
```

### Where paid actually wins

The one place paid is worth the money: **the main LLM driving long agent conversations** (the web chat at :8765, complex multi-step skill chains like "go to the kitchen, count chairs, come back, tell me"). Set `OPENAI_API_KEY` for that flow. Everything else — TTS, VLM, web search, repo mining — is at near-parity for free.

## Reading the journal

```bash
column -ts $'\t' ~/.ohmni/research/journal.tsv | less
```

Most useful queries:

- "Which calibration value gave the best reach rate?"
  ```bash
  awk -F'\t' '$2=="calibration" && $6=="keep" {print $4, $3}' \
    ~/.ohmni/research/journal.tsv | sort -rn | head
  ```
- "What did the web researcher pull yesterday?"
  ```bash
  awk -F'\t' '$2=="web_research" {print $1, $7}' \
    ~/.ohmni/research/journal.tsv | tail
  ```
