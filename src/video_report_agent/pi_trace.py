"""Compact persisted RPC events; the live Pi consumer still sees every event."""


def compact_event(event):
    kind = event.get("type")
    if kind in {"message_update", "tool_execution_update"}:
        return None
    result = dict(event)
    if kind in {"message_start", "message_end"}:
        message = event.get("message", {})
        # Tool output is already recorded by tool_execution_end.
        if message.get("role") == "toolResult":
            return None
        message = dict(message)
        if kind == "message_start":
            message.pop("content", None)
        elif isinstance(message.get("content"), list):
            content = []
            for part in message["content"]:
                if (
                    part.get("type") == "toolCall"
                    and message.get("stopReason") not in {"error", "aborted"}
                ):
                    # Full arguments live in tool_execution_start, once.
                    part = {key: part[key] for key in ("type", "id", "name") if key in part}
                elif part.get("type") == "thinking":
                    part = {"type": "thinking", "characters": len(part.get("thinking", ""))}
                content.append(part)
            message["content"] = content
        result["message"] = message
    elif kind == "turn_end":
        result.pop("message", None)
        result.pop("toolResults", None)
    elif kind == "agent_end":
        result.pop("messages", None)
    return result
