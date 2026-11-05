"""Per-call event bus: policy/tool callbacks -> the browser that owns the call.

Each live session gets its own queue, keyed by the ADK session id the callbacks
see on `tool_context.session.id`.

Publishing to a call with no subscriber (e.g. inside `adk web`) is a no-op.
"""
import asyncio

_queues: dict[str, asyncio.Queue] = {}


def subscribe(call_id: str) -> asyncio.Queue:
    q = _queues[call_id] = asyncio.Queue()
    return q


def unsubscribe(call_id: str) -> None:
    _queues.pop(call_id, None)


def publish(call_id: str, event: dict) -> None:
    q = _queues.get(call_id)
    if q is not None:
        q.put_nowait(event)
