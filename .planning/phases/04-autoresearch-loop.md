# Phase 4 — Self-Improvement Loop (the autoresearch analog)

**Goal:** The robot proposes its own experiments, runs them, scores them, and writes what it learned to a brain file. Same pattern as karpathy/autoresearch — for a robot.

## What karpathy/autoresearch actually is

A single-process loop on top of an LLM:
1. **Read** prior notes (the "brain")
2. **Propose** a small, falsifiable experiment
3. **Run** it (write code, execute, capture output)
4. **Score** it (did it advance the goal?)
5. **Append** finding to the brain
6. Goto 1

Platform forks (`miolini/autoresearch-macos`, `trevin-creator/autoresearch-mlx`, `jsegov/autoresearch-win-rtx`, `andyluo7/autoresearch`) are mostly ports of the same loop with different model backends. The Karpathy gist `442a6bf...` is the seminal "let the model write its own research code and iterate" pattern.

## Robot-shaped translation

The brain isn't research notes; it's a **diary of what the robot tried with its body** and what happened. Same pattern, robot verbs.

```
~/.ohmni/brain.md       (append-only markdown log)
~/.ohmni/world.json     (semantic spatial memory; phase 3 writes here)
~/.ohmni/skills.md      (record of which skill calls work / fail / wedge)
```

## New module: `BrainResearcher`

```python
class BrainResearcher(Module):
    """Autonomous experiment loop for the Ohmni.

    Inputs:  In[Image], In[PointCloud2], In[battery], In[odom]
    Outputs: Out[goal_request]  -- routed to planner like the explorer
             Out[BaseMessage]   -- chat output to the agent's web UI

    Loop (every N minutes when idle):
        1. Read recent brain entries.
        2. Propose: "I haven't been to (x, y) in 24h" or
                    "Last time I tried <skill> at <pose>, it failed.
                     Try with <variation>."
                    or "I see a new object class I don't have a label for."
        3. Execute via the agent (call NavigateTo, observe, ...).
        4. Score: did the action complete? did the human override?
        5. Append a brain entry with timestamp, intent, outcome.
    """
```

Slots into `ohmni_agentic` next to the frontier explorer. The explorer covers spatial coverage; this covers *behavioral* coverage.

## Online research surface

- `web_search(query: str, max_results: int = 5)` → results dict. Brave Search API or DuckDuckGo HTML.
- `read_url(url: str)` → markdown. `firecrawl` or `playwright + readability`.
- `read_paper(arxiv_id: str)` → markdown. ArXiv API.

These get exposed as `@skill`s on a `WebResearcher(Module)`. The brain can include "I should look up how to handle <X>" → BrainResearcher emits a tool call → WebResearcher fetches → result lands in brain → next iteration uses it.

## Microloops within microloops

Karpathy's pattern is one big loop. For a robot we want nested cadences:

| loop                | period       | what                                                       |
|---------------------|--------------|------------------------------------------------------------|
| safety              | 50 ms        | clamp cmd_vel against lidar (Phase 5)                      |
| reactive            | 200 ms       | obstacle bounce, look-around, head tracking                |
| navigation          | 1 s          | replan path                                                |
| skill               | per-call     | execute one named action end-to-end                        |
| brain               | 1–10 min     | reflect + propose + score                                  |
| research            | 30–60 min    | online lookup, brain consolidation, hypothesis generation  |
| sleep               | nightly      | full brain compaction; agent rewrites prior week to a digest |

`BrainResearcher` runs only the brain / research / sleep cadences. The lower ones are existing modules.

## Memory model

- **Episodic** (chronological brain.md, append-only). Cheap to write, expensive to query.
- **Semantic** (world.json: pose → labels). Indexed.
- **Procedural** (skills.md: skill → success-rate, last-failure-mode). Updates on each skill call.

Compaction during nightly sleep summarizes a 24-hour episodic window into 5–10 high-signal lines and merges into the relevant semantic / procedural files.

## Acceptance

- After 24 hours of supervised run, brain.md contains ≥50 entries, ≥80% of them with verifiable claims (a pose, an outcome, a measured value).
- After 7 days, the robot can answer "where do you charge?" and "what did you try yesterday?" from memory without the human re-telling it.
- `skills.md` reflects measured success rates that match observed behavior (i.e. `dock` should show high success once it's working).

## What it does *not* do

- It does not modify its own source code (no autonomous self-rewrite). All code changes still need human review.
- It does not push to remote services without explicit user approval — brain stays local.
- It does not trigger long-running motion experiments without battery > 40%.
