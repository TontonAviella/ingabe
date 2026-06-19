"""ACP client adapter — bridges Hermes Agent's streaming output to mundi-app's
WebSocket emit functions.

The ACP Client base class (from `agent-client-protocol` package) defines
hooks the agent can call BACK into the client for: streaming session
updates, requesting permission for a file/terminal op, reading/writing
text files, creating terminals, etc.

Mundi-app is a chat client only — we don't expose filesystem or terminal
to Sage. So most hooks return method_not_found. The one that matters is
`session_update`, which is how streaming agent message chunks reach us.
We map those to `kue_stream_token` on the conversation WebSocket.
"""
from __future__ import annotations

import logging
from typing import Callable, Optional

logger = logging.getLogger(__name__)


# NOTE: this module imports `acp` only inside functions/methods so that
# importing src.services.* doesn't pull in agent-client-protocol on
# environments where it isn't installed (e.g. CI without Hermes deps).
# When MUNDI_USE_HERMES=0, the runtime never instantiates this class.


def build_ingabe_acp_client(
    stream_token: Callable[[str, str], None],
    notify_error: Callable[[str, str], None],
    conversation_id: str,
):
    """Construct an IngabeAcpClient bound to a conversation's WebSocket emit
    functions. Done as a factory so the `acp` import is lazy.

    Args:
        stream_token: async callable (conversation_id, text) → None.
            For each incremental text delta, emit to the WebSocket.
            Typically `src.routes.websocket.kue_stream_token`.
        notify_error: async callable (conversation_id, msg) → None.
            For surface-able errors. Typically `kue_notify_error`.
        conversation_id: mundi-app's conversation id; used as the
            stable identifier in WebSocket emits.

    Returns:
        An instance of IngabeAcpClient ready to pass to
        `acp.connect_to_agent(client, reader, writer)`. The instance
        exposes `accumulated_text: list[str]` — every text chunk seen
        via session_update is appended. After `conn.prompt(...)` returns,
        `"".join(client.accumulated_text)` is the full assistant message
        to persist via chat_completion_messages.
    """
    import acp  # local import — see module docstring

    class IngabeAcpClient(acp.Client):
        """Minimal ACP Client implementation for a chat-only host.

        We deny all filesystem/terminal capabilities because mundi-app
        does not expose any of those to Sage. Tool dispatch back into
        mundi-app happens via the /internal/tool-call HTTP endpoint,
        NOT via ACP client hooks. The ACP Client interface here is
        purely the "deliver streaming output to the user" path.

        The `accumulated_text` list captures every text delta in turn
        order. The factory returns a fresh client per turn, so the list
        starts empty each call and ends as the full assistant message.
        """

        def __init__(self) -> None:
            super().__init__()
            self.accumulated_text: list[str] = []

        async def session_update(self, session_id: str, update) -> None:
            """Called by the agent for every streaming chunk.

            CALLBACK SIGNATURE caught 2026-05-15 via raw-socket probe:
            the SDK calls `client.session_update(session_id, update)`
            with TWO positional args — not `session_update(notification)`
            with a wrapping `notification.update` attribute. The earlier
            (notification-wrapper) signature silently TypeError'd before
            the body ran, swallowed by the SDK's exception handler, with
            zero chunks ever reaching `accumulated_text`. The actual
            base-class signature in acp v0.10.0 is:

                async def session_update(
                    self, session_id: str,
                    update: UserMessageChunk | AgentMessageChunk |
                            AgentThoughtChunk | ToolCallUpdate | ...,
                ) -> None

            For chat streaming, we care about `AgentMessageChunk` and
            the `TextContentBlock` inside its `.content` field. We
            stream to the WebSocket AND accumulate for post-turn
            persistence.

            ACP wire format quirk: for chunk-bearing updates the
            `content` field is a SINGLE block, not a list — wire JSON
            is `{"content": {"text": "...", "type": "text"}}` not
            `{"content": [{"text": "...", "type": "text"}]}`. Handle
            both shapes defensively in case future update types use
            lists.

            REASONING STRIP (PR #53, 2026-05-16):
            We only forward `agent_message_chunk` text. Earlier code
            also surfaced `agent_thought_chunk` text under the
            (wrong) rationale that it kept "parity with the hand-rolled
            path which streams reasoning tokens too." It doesn't —
            the hand-rolled loop strips reasoning fields before sending
            history to the LLM and before persisting (see
            `_ALWAYS_STRIP_FIELDS = {"reasoning", "reasoning_details"}`
            at src/routes/message_routes.py:1190). Streaming thought
            chunks here meant Nemotron's `<think>` text reached the
            WebSocket as user-visible answer ("The user wants to see
            'nyamagabe'. This likely refers to...") AND got persisted
            to `chat_completion_messages`, doubling assistant text
            when the thought happened to mirror the final answer
            ("Hello! How can I assist you today?Hello! How can I
            assist you today?"). Caught 2026-05-16 during the
            MUNDI_USE_HERMES=1 smoke test on prod. Future work can
            route thought chunks to a separate UI affordance
            (collapsible "Sage is thinking…" panel); for now we
            match the hand-rolled path's strip behavior exactly.

            Tool-call updates (`tool_call`, `tool_call_update`) are
            also currently dropped here — PR #55 will route them to
            `kue_notify_tool_call` so the frontend renders pending /
            running / complete status. Until that lands, the gateway's
            tool dispatch is the only thing the user "sees" via the
            persisted assistant message text.
            """
            try:
                update_kind = getattr(update, "session_update", None)
                if update_kind != "agent_message_chunk":
                    # Drop thought chunks, tool-call updates, plan
                    # updates, and anything else that isn't the
                    # assistant's user-facing message stream.
                    return
                content = getattr(update, "content", None)
                if content is None:
                    return
                blocks = content if isinstance(content, list) else [content]
                for block in blocks:
                    text = getattr(block, "text", None)
                    if text:
                        self.accumulated_text.append(text)
                        await stream_token(conversation_id, text)
            except Exception:
                logger.exception(
                    "ACP session_update handler failed for conv=%s",
                    conversation_id,
                )

        async def request_permission(self, request) -> "acp.RequestPermissionResponse":
            """Agent asks for permission to do something privileged.

            Mundi-app's policy: deny everything. Sage operates within
            the partner's RLS-scoped data plane via /internal/tool-call,
            not via ACP-mediated host actions.
            """
            # acp.RequestPermissionResponse with outcome="cancelled" is
            # the standard "client refuses" response.
            return acp.RequestPermissionResponse(
                outcome=acp.RequestPermissionResponse.Outcome(
                    outcome="cancelled"
                )
            )

        async def read_text_file(self, request):
            raise acp.RequestError.method_not_found()

        async def write_text_file(self, request):
            raise acp.RequestError.method_not_found()

        async def create_terminal(self, request):
            raise acp.RequestError.method_not_found()

        async def terminal_output(self, request):
            raise acp.RequestError.method_not_found()

        async def kill_terminal(self, request):
            raise acp.RequestError.method_not_found()

        async def wait_for_terminal_exit(self, request):
            raise acp.RequestError.method_not_found()

        async def release_terminal(self, request):
            raise acp.RequestError.method_not_found()

    return IngabeAcpClient()
