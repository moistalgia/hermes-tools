# Running the fine-tune

Dataset preparation ends here. Actually running the training is outside the
scope of hermes-tools.

The suggested tool is **[Unsloth](https://github.com/unslothai/unsloth)** — it
runs QLoRA fine-tuning on consumer/workstation GPUs with significantly reduced
VRAM use compared to vanilla HuggingFace training, and has ready-made notebooks
for Qwen, Llama, and other common base models. Point it at the training JSONL
this skill produces.
