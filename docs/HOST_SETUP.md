# Host setup (Python stack)

How to set up the host machine (Mac/Linux, Python 3.10+) so the dimos blueprints
can run. Do this **after** you've confirmed the basic robot connection works
(see [`CONNECTING_TO_A_NEW_ROBOT.md`](CONNECTING_TO_A_NEW_ROBOT.md)).

The runtime is: **dimos** (the robotics framework) + **dimos-ohmni** (this repo's
adapter that teaches dimos how to drive an Ohmni).

---

## 1. `dimos` is not committed — clone it yourself

`dimos/` is a vendored clone of [`dimensionalOS/dimos`](https://github.com/dimensionalOS/dimos)
(Apache 2.0) and is **gitignored** here — it's ~31 GB of source plus runtime
data and does not belong in this repo. Recreate it at the exact pinned commit and
apply the one local patch this project depends on:

```bash
# from the repo root
git clone https://github.com/dimensionalOS/dimos.git
cd dimos
git checkout a035fb315d35bba511dfc6156dc21827e70dbc94
git apply ../patches/dimos-robot-all_blueprints.patch
cd ..
```

The patch (`patches/dimos-robot-all_blueprints.patch`) registers the five Ohmni
blueprints and the `ohmni-connection` module in dimos's `all_blueprints.py` so
dimos can discover them. Without it, dimos won't know the Ohmni blueprints exist.

> If upstream `dimos` has moved on and the patch no longer applies cleanly, the
> change is tiny (8 lines) — open `patches/dimos-robot-all_blueprints.patch` and
> add those entries to `dimos/dimos/robot/all_blueprints.py` by hand.

---

## 2. Create the virtualenv and install

```bash
python3.12 -m venv .venv          # 3.10+ works; 3.12 is what this was built on
source .venv/bin/activate

pip install -e ./dimos
pip install -e ./dimos-ohmni
pip install langchain-core        # hard dependency of dimos core
```

For the full LLM-driven stack (`run_ohmni_full.py`) you also need the agent stack
dimos pins:

```bash
pip install "langchain==1.0.3" "langgraph==1.0.4" "langchain-openai==1.0.2"
export OPENAI_API_KEY=sk-...       # or another provider configured in dimos.agents
```

---

## 3. Run

```bash
export ANDROID_ADB_SERVER_PORT=6037   # the run scripts also set this themselves
python run_ohmni.py                   # ohmni-smart
python run_ohmni_full.py              # ohmni-full (needs OPENAI_API_KEY)
```

Web UI at <http://localhost:8765>; websocket vis at `ws://localhost:7779`.

---

## 4. Optional environment knobs

| Variable | Default | Effect |
|---|---|---|
| `ANDROID_ADB_SERVER_PORT` | `6037` (set by scripts) | Avoids the poisoned default adb server (5037). |
| `OPENAI_API_KEY` | — | Required for the Agent + SpeakSkill in `run_ohmni_full.py`. |
| `OHMNI_AUTO_EXPLORE` | `1` | Auto-trigger frontier exploration ~30 s after boot. |
| `OHMNI_AUTORESEARCH` | `1` | Set `0` to pause the self-improvement microloops. |
| `OHMNI_ENABLE_AGENT` | — | Gates the LLM driver / PersonFollow skills. |
| `OHMNI_APOS_PER_M` | `10860` | Wheel-encoder counts per metre (odometry calibration). |
| `OHMNI_WHEELBASE_M` | `0.30` | Distance between wheels, metres. |

Runtime state is written under `~/.ohmni/` (`brain.md`, `world.json`,
`research/journal.tsv`, calibration env files) — outside the repo.
</content>
