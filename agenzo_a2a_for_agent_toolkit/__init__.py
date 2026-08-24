"""agenzo-a2a-for-agent-toolkit: an MCP server bridging AI agents to the Agenzo A2A orchestrator.

The server exposes stateful tools (``book`` / ``send_message`` / ``act`` / ``poll`` /
``start_payment`` / ``open_url`` / ``discover`` / ``guide`` / ``configure``) that drive the
orchestrator's domain-agnostic *card* protocol. The chat agent reads each returned card and picks
the next action, so booking a hotel or a flight happens right here in the conversation.
"""

__version__ = "0.1.0"
