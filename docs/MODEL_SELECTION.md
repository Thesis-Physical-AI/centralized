# Model Selection: VLM vs. VLA for Robot-Level Intelligence

Status: **evidence summary + recommendation, pending team/supervisor decision (ADR-0003).**

## 1. The question

The proposal names OpenVLA as the reasoning core. The team's current direction is a VLM per robot that picks from a fixed action menu, with deterministic executors underneath. The question is which model type belongs at which level, given the actual robots (PuppyPi quadruped, PX4 drone) and actual compute (RTX 4080 workstation; Raspberry Pi 5 on the PuppyPi).

## 2. What the models actually are (verified from sources)

| Model | Size | Output | Trained embodiments | Reported compute |
|---|---|---|---|---|
| **OpenVLA** [1] | 7B (Llama 2 + DINOv2/SigLIP) | 7-D relative end-effector actions, 256 bins each | Single-arm manipulators from Open X-Embodiment (Bridge, Fractal, Kuka, DROID, …). No legged or aerial data. | 15 GB in bf16 (16.8 GB measured); 10.2 GB int8; 7.0 GB int4. ~6 Hz on an RTX 4090 in bf16. int8 1.2 Hz / int4 3 Hz on an A5000. |
| **SmolVLA** [2] | 450M (~100M action expert, frozen VLM backbone) | Continuous action chunks via flow matching | LeRobot community datasets (low-cost arms, e.g. SO-100) | Designed to train on one GPU and deploy on consumer GPUs or CPU |
| **NaVILA** [3] | VLA for legged navigation | **Mid-level language actions** ("move forward 75 cm"), executed by a separate visual locomotion RL policy | Legged robots | Not stated on abstract page |
| **QUAR-VLA / QUART** [4] | VLA for quadrupeds | Quadruped commands; dataset covers navigation, terrain, whole-body manipulation | Quadrupeds (their own QUARD dataset) | Authors list real-time inference speed and sim-to-real as open problems |
| **CognitiveDrone, UAV-VLA, AerialVLA …** [5][6] | Research VLAs for UAVs | Flight commands / paths | Simulated flight trajectories | Research code; no PX4-ready package found |
| **SmolVLM** [7] | 256M / 500M / 2.2B | Text only | — (it is a VLM, not a VLA) | 0.8 / 1.2 / 4.9 GB GPU RAM at batch 1 |

Sources: [1] Kim et al., OpenVLA, arXiv 2406.09246 (§3.5, Table 2, §5.4). [2] Hugging Face, "SmolVLA" blog and arXiv 2506.01844. [3] NaVILA, arXiv 2412.04453. [4] Ding et al., QUAR-VLA, ECCV 2024, arXiv 2312.14457. [5] CognitiveDrone, arXiv 2503.01378. [6] UAV-VLA, arXiv 2501.05014; AerialVLA arXiv 2603.14363. [7] SmolVLM, arXiv 2504.05299 and model cards.

## 3. What this means for our robots

1. **OpenVLA cannot drive PuppyPi or the drone out of the box.** Its action space is a manipulator end-effector; none of its training data is legged or aerial. Using it would mean collecting demonstrations on our robots and fine-tuning — a separate thesis in itself. The literature review already identifies this ("VLA research primarily targets single-robot tasks", §4).
2. **OpenVLA is also not a mission planner.** It maps (image, instruction) → next low-level action for one robot. It does not decompose missions or allocate across robots. The proposal's phrase "OpenVLA for high-level reasoning, orchestration and mission planning" does not match the model's capabilities; the literature review already softened this to "LLM, VLM or VLA". Using Gemini (an LLM) at the mission level is consistent with the review's taxonomy (SMART-LLM, COHERENT, CoMuRoS all use LLMs there).
3. **The state of the art for legged/aerial "VLAs" is hierarchical**: NaVILA's VLA emits language-level motion commands and a classical/RL locomotion policy executes them. That is architecturally the same as our "VLM selects from a constrained action set → deterministic executor" design. So our direction is not a retreat from VLAs; it is the pattern the legged-VLA literature converged on.
4. **A VLM does not provide robot control.** It produces text. Control comes from the executor (gait controller / Nav2 for PuppyPi, PX4 for the drone). The VLM's value is perception-grounded decisions: *is the door open? is the apple visible? which way to look?* This must be stated plainly in the thesis.
5. **A VLA becomes appropriate if PuppyPi gets an arm and the task is manipulation.** Grasping is exactly what SmolVLA/OpenVLA are trained for. SmolVLA (450M) is the realistic candidate: it is small enough to run on the RTX 4080 workstation and fine-tunable with LeRobot on our own demonstrations. It would sit *inside* one skill (`grab`, `open`) behind the same executor interface, not replace the action-selection layer.

## 4. Where inference should run

| Option | PuppyPi (Raspberry Pi 5, 2–8 GB depending on kit) | Drone | Verdict |
|---|---|---|---|
| Onboard | SmolVLM-256M (0.8 GB) might fit on CPU; latency unmeasured. 2.2B (4.9 GB) will not fit usefully. No GPU. | Depends on the companion computer (unknown). | Measure before committing |
| Companion computer (e.g. Jetson) | Not part of the stock kit | Typical for PX4 drones | Hardware decision |
| **Remote (workstation over Wi-Fi/ROS 2)** | Works with any model size; adds network latency and a dependency on the link | Same | **Recommended for the thesis timeline**, sim and first hardware tests |

Because the action-selection rate is low (one decision per step, seconds apart, not a 50 Hz control loop), remote inference is acceptable. Safety-critical loops (balance, PX4 attitude control, obstacle stop) never depend on the model or the network.

A local SmolVLM latency and output-validity benchmark on the lab workstation is still to be run (tracked as an experiment issue).

## 5. Recommendation

| Level | Recommended model | Status |
|---|---|---|
| Mission reasoning & orchestration (centralized) | LLM — Gemini now; keep the provider interface so an open model can be swapped in for comparison (literature review contribution 1) | Implemented |
| Robot step execution (decentralized — the VLA slot) | Small VLM choosing from a **closed, per-robot action menu** with schema-constrained JSON output, validated by deterministic code; a rule-based selector as the baseline | Proposed |
| Locomotion / flight | Classical: scripted gait or Nav2 for PuppyPi; PX4 for the drone. No learned model. | Gait exists in sim |
| Manipulation (only if an arm is fitted) | VLA candidate: SmolVLA fine-tuned on our demonstrations, wrapped as a skill | Research option |

Thesis framing that keeps the original objective intact: *a VLA belongs on each robot, not in the center. Because no current VLA is both deployable on our edge hardware and trained for quadrupeds or quadrotors, each agent fills the VLA slot with a VLM that selects from a closed action list, executed by classical autonomy; a VLA is evaluated where its embodiment matches (manipulation) and can replace the VLM behind the same interface.* This needs supervisor confirmation (D1 in `DECISIONS.md`).

## 6. Open research questions

1. Does VLM-based action selection beat a rule-based selector on step success rate? (Required baseline — otherwise the VLM's contribution is unproven.)
2. How often does a small VLM produce out-of-menu or wrong actions, and does constrained decoding (JSON schema / grammar) fix the format problem without hurting choice quality?
3. What is the end-to-end step latency budget (camera → decision → executor) that remote inference must meet?
4. Can SmolVLM-256M on a Pi 5 CPU meet that budget, removing the network dependency?
