# Khalil execution components

Foreground conversations use a bounded durable loop. Planned, workflow, scheduled, and proactive work use durable dependency graphs. Both paths cross the same execution-policy boundary before an action handler runs.

```mermaid
C4Component
  title Component Diagram - Durable Execution

  Container_Ext(channels, "Messaging Channels", "Platform APIs", "Messages and approval callbacks")
  ContainerDb(state, "Durable State", "SQLite, WAL", "Loop checkpoints, graph nodes, and idempotency receipts")
  Container_Ext(models, "Model Providers", "Compatible APIs", "Reasoning and tool selection")

  Container_Boundary(daemon, "Assistant Daemon") {
    Component(router, "Message Router", "Python", "Selects direct, loop, or graph execution")
    Component(toolLoop, "Durable Tool Loop", "DurableToolLoopRunner", "Bounds and checkpoints foreground reasoning")
    Component(graphRunner, "Execution Graph Runner", "ExecutionGraphRunner", "Claims and checkpoints dependency nodes")
    Component(policy, "Execution Bus", "ExecutionBus", "Applies approval, rate-limit, and audit policy")
    Component(actions, "Action Handlers", "Skill Registry", "Read and write integrations")
    Component(recovery, "Recovery Coordinator", "Startup lifecycle", "Classifies interrupted work after restart")
  }

  Rel(channels, router, "Delivers messages and callbacks", "Platform APIs")
  Rel(router, toolLoop, "Starts foreground reasoning")
  Rel(router, graphRunner, "Starts planned or background work")
  Rel(toolLoop, models, "Requests the next tool or final answer", "Compatible API")
  Rel(toolLoop, state, "Appends loop checkpoints", "SQLite")
  Rel(graphRunner, state, "Claims nodes and stores receipts", "SQLite")
  Rel(toolLoop, policy, "Submits foreground tool calls")
  Rel(graphRunner, policy, "Submits graph node actions")
  Rel(policy, actions, "Invokes allowed handlers")
  Rel(recovery, state, "Scans non-terminal runs", "SQLite")
  Rel(recovery, toolLoop, "Resumes safe loops or requests approval")
  Rel(recovery, graphRunner, "Reclaims safe nodes and blocks ambiguous writes")
  Rel(policy, channels, "Requests approval for protected actions")
```
