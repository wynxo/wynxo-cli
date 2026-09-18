# Models, context and local observations

Wynxo is Ollama-first and does not assume one model is best for every machine.

## /model

The model table reports what Ollama exposes when available:

- installed model size
- tool support
- thinking support
- native context length
- current GPU placement from the running-model endpoint
- locally observed generation speed and time to first token

Observations are stored locally in the Wynxo data directory and are bounded.
They are evidence about this machine, not a benchmark and not a ranking.

Wynxo never silently switches the selected model.

## Context and GPU placement

A very large context window can move part of a model or its KV cache to CPU and
make a locally hosted model feel dramatically slower. /doctor and /model expose
the placement information Ollama reports.

When Wynxo has enough information to estimate a smaller useful context window,
/doctor prints an explicit command such as:

~~~text
/context window 32768
~~~

It remains your choice whether to change it.

## Thinking and effort

Effort is an agent scheduling policy, not a model name. Thinking display is
also separate from whether a selected model supports reasoning. Use /stats and
/context for runtime detail, and Ctrl-O to reveal or hide available reasoning
while a coding turn is running.

## Health history

Wynxo keeps only a small recent sample per model. The store is intentionally
descriptive: token rate, first-token latency and tool failures. It is never
used to silently reroute a request.
