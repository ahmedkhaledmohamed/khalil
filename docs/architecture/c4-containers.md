# Khalil containers

The Python daemon owns message handling and execution coordination. SQLite is the durable source of truth for both foreground loops and multi-step graphs; model providers and Codex remain external runtimes.

```mermaid
C4Container
  title Container Diagram - Khalil

  Person(owner, "Owner", "Requests information and actions")
  System_Ext(channels, "Messaging Channels", "Telegram, Slack, Discord, WhatsApp, and HTTP")
  System_Ext(llmGateway, "Taskforce-compatible LLM Gateway", "Cloud model routing")
  System_Ext(localModels, "Local Model Runtime", "Ollama generation and embeddings")
  System_Ext(codex, "Codex CLI", "Repository-changing coding agent")

  System_Boundary(khalil, "Khalil") {
    Container(daemon, "Assistant Daemon", "Python, asyncio, FastAPI", "Routes messages and coordinates execution")
    Container(mcp, "MCP Server", "Python, stdio", "Exposes Khalil context and tools to IDE sessions")
    ContainerDb(state, "Durable State", "SQLite, WAL", "Stores conversations, checkpoints, graphs, receipts, and signals")
  }

  Rel(owner, channels, "Sends requests and approvals")
  Rel(channels, daemon, "Delivers messages and callbacks", "Platform APIs")
  Rel(daemon, channels, "Sends responses and approval controls", "Platform APIs")
  Rel(daemon, state, "Appends checkpoints and reads recovery state", "SQLite")
  Rel(mcp, state, "Reads indexed context and assistant state", "SQLite")
  Rel(daemon, llmGateway, "Requests reasoning and tool selection", "OpenAI-compatible HTTPS")
  Rel(daemon, localModels, "Requests local inference and embeddings", "HTTP")
  Rel(daemon, codex, "Starts isolated coding sessions", "Subprocess")
```
