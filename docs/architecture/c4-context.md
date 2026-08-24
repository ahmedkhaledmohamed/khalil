# Khalil system context

Khalil is a personal assistant that accepts messages, gathers context, executes approved actions, delegates repository work to Codex, and preserves execution state across restarts.

```mermaid
C4Context
  title System Context - Khalil

  Person(owner, "Owner", "Requests information and actions")
  System(khalil, "Khalil", "Personal assistant with durable loops and execution graphs")

  System_Ext(channels, "Messaging Channels", "Telegram, Slack, Discord, WhatsApp, and HTTP")
  System_Ext(llmGateway, "Taskforce-compatible LLM Gateway", "Routes cloud model requests")
  System_Ext(localModels, "Local Model Runtime", "Provides Ollama generation and embeddings")
  System_Ext(personalServices, "Personal Services", "Calendar, email, notes, media, and device APIs")
  System_Ext(codex, "Codex CLI", "Performs repository-changing coding tasks")
  System_Ext(github, "GitHub", "Stores branches, pull requests, and review history")

  Rel(owner, khalil, "Requests information and approved actions", "Messaging channels")
  Rel(owner, channels, "Sends messages and approval callbacks")
  Rel(channels, khalil, "Delivers messages and callbacks", "Platform APIs")
  Rel(khalil, channels, "Returns answers and approval prompts", "Platform APIs")
  Rel(khalil, llmGateway, "Requests reasoning and tool selection", "OpenAI-compatible HTTPS")
  Rel(khalil, localModels, "Requests local inference and embeddings", "HTTP")
  Rel(khalil, personalServices, "Reads context and executes approved actions", "Service APIs")
  Rel(khalil, codex, "Delegates isolated coding work", "Subprocess")
  Rel(codex, github, "Pushes reviewed feature branches", "Git/HTTPS")
  Rel(khalil, github, "Reads status and opens approved pull requests", "GitHub API")
```
