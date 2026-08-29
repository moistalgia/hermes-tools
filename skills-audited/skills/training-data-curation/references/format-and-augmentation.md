# Format template and augmentation

## The ShareGPT/ChatML message template

Each kept record becomes one training example with three messages:

```json
{
  "messages": [
    {
      "role": "system",
      "content": "You are Hermes, a home agent. You act by calling MCP tools."
    },
    {
      "role": "user",
      "content": "<the user request that led to this tool call>"
    },
    {
      "role": "assistant",
      "content": "<reasoning if present>\n\n<the tool call and its result, in the format the model should learn to produce>"
    }
  ]
}
```

The `assistant` content should contain the reasoning trace (if any) followed by
the structured call in whatever format the target model is being trained to
produce. If you want the fine-tuned model to emit reasoning at inference time,
keep the reasoning in the assistant message. If you want pure structured
output, strip it.

## Rejection sampling for synthetic variations

To grow a small real dataset:

1. Take 10–20 curated real examples.
2. Ask a strong model to generate N synthetic variations: new rooms, new
   brightness levels, new time-of-day contexts, new user phrasings.
3. For each synthetic candidate, verify the final tool call is valid:
   - The tool name exists in the server's tool list.
   - All required arguments are present.
   - Argument values are in range (e.g. `position_pct` is 0–100).
4. Only keep candidates that pass validation. Discard the rest.

This keeps bad reasoning chains out of the dataset without manual review of
every line.
