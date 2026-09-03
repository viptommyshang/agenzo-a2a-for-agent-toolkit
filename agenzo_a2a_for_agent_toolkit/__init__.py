"""agenzo-a2a-for-agent-toolkit: an MCP server bridging AI agents to the Agenzo A2A orchestrator.

The server exposes stateful tools (``book`` / ``send_message`` / ``act`` / ``poll`` /
``start_payment`` / ``open_url`` / ``discover`` / ``guide`` / ``configure`` / ``resolve_location`` /
``resolve_pickup_time`` / ``inspect``) that drive the orchestrator's domain-agnostic *card*
protocol. The chat agent reads each returned card and picks the next action, so booking a hotel,
flight or ride happens right here in the conversation.

Because this bridge is primarily a *debugging* tool, the exact A2A JSON-RPC request/response is
also inspectable — but it is NEVER inlined into tool results (that would bloat the chat context):
call ``inspect()`` any time to dump the captured raw traffic, and/or set ``AGENZO_A2A_DEBUG=1`` to
LOG each exchange to stderr (and to ``AGENZO_A2A_LOG_FILE`` when set).
"""

__version__ = "0.1.0"
