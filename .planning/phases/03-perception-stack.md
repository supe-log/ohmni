# Phase 3 — Perception (the realistic NVIDIA-Lidar-AI sub-plan)

**Goal:** Better-than-pure-occupancy perception, on a Mac without an NVIDIA GPU.

## Honest read on `NVIDIA-AI-IOT/Lidar_AI_Solution`

That repo is a CUDA + TensorRT toolkit: voxelization kernels, scatter-cumsum, BEVFusion, CenterPoint, PointPillars, etc. Tied to Jetson / RTX. **It will not run on the Ohmni's Android x86 SoC, and it won't run on a Mac without an NVIDIA GPU.** What's portable is the algorithm structure, not the binaries.

## What's actually shippable on this hardware

### a) Already in dimos — use it first
- `dimos.mapping.voxels.VoxelGridMapper` — Open3D voxel hashmap. Already wired into `ohmni_smart`. Real 3D occupancy from the 2D lidar scans (height = 0).
- `dimos.mapping.costmapper.CostMapper` — converts voxel map to `OccupancyGrid` for the planner.
- `dimos.perception.visual_servoing` (Qwen-VL + EdgeTAM) — used by `FollowHuman` skill. Object queries on camera frame, BEV-style steering.

### b) Add — Mac-friendly BEV fusion
- Use `mlx` or CoreML to run a small CenterPoint-style detection head on the BEV slice from the voxel mapper + the camera frame. PointPillars is overkill for a 2D lidar; a 2D-grid CNN classifier ("free / wall / movable obstacle / person") is enough.
- Reference architecture: BEVFusion's late-fusion path (camera features projected to BEV grid, summed with lidar BEV grid, classified). Implement in PyTorch + MPS, swap to MLX if we want lower latency.

### c) Add — semantic SLAM lite
- For each `(x, y)` cell observed, store a class label from VLM ("kitchen counter", "couch") sampled at ~0.5 Hz. Cheap; gives the brain spatial context without needing a real semantic-SLAM stack.

## What we won't do (and why)

- **Don't** try to port `Lidar_AI_Solution` kernels to MLX. The maintenance load isn't worth it and the model accuracy advantage assumes 64-beam lidars, not the A2M8's single-beam 360°.
- **Don't** deploy point-pillars. Single-beam 2D scan at ~50 Hz doesn't fill the input format that point-pillars expects.

## File-level plan

- `dimos-ohmni/src/dimos_ohmni/perception/bev_fusion.py` — new Module. `In[Image]`, `In[OccupancyGrid]`, `Out[OccupancyGrid]` (semantically labeled).
- `dimos-ohmni/src/dimos_ohmni/perception/semantic_pin.py` — new Module. Drops VLM-derived labels onto the world frame at the robot's current pose; logs to `~/ohmni_world.json` for the brain.

## Acceptance

- BEV semantic map renders on the websocket vis with class colors, not just occupancy levels.
- Brain log accumulates ≥10 distinct `("pose", "label")` entries during a 10-minute autonomous run.
