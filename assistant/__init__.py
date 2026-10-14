"""The OpenGrid Assistant: a deterministic question parser + grounded tools, optionally phrased by an LLM.

    intents.parse(message)          -> Parsed(intent, slots, gpus, confidence)   (no LLM, pure)
    tools.*                         -> card payloads from in-process, read-only services
    engine.respond(message, context, who) -> the POST /v1/assistant response (docs contract §5)
    llm.provider()                  -> an optional ModelProvider, or None (the default)

Nothing in this package writes to the database or calls a mutating service: there are no
mutation tools.
"""
