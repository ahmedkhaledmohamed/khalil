# Khalil restart recovery flow

This flow shows a foreground write that reaches the shared execution policy, pauses durably, survives a process restart, and continues only after explicit approval.

```mermaid
C4Dynamic
  title Dynamic Diagram - Foreground Write Recovery

  Container(channel, "Messaging Channel", "Platform API", "Carries requests and approval callbacks")
  Component(loop, "Durable Tool Loop", "DurableToolLoopRunner", "Coordinates bounded foreground reasoning")
  Component(policy, "Execution Bus", "ExecutionBus", "Enforces action policy")
  Component(recovery, "Recovery Coordinator", "Startup lifecycle", "Classifies interrupted work")
  Component(action, "Action Handler", "Skill Registry", "Performs the approved side effect")
  ContainerDb(state, "Durable State", "SQLite, WAL", "Stores loop checkpoints")

  Rel(channel, loop, "1. Submit a request")
  Rel(loop, state, "2. Save model and before-action checkpoints", "SQLite")
  Rel(loop, policy, "3. Submit the proposed write")
  Rel(policy, loop, "4. Return waiting-for-approval")
  Rel(loop, state, "5. Persist the approval boundary", "SQLite")
  Rel(recovery, state, "6. Reload the paused run after restart", "SQLite")
  Rel(recovery, channel, "7. Send run-specific Resume and Cancel controls")
  Rel(channel, recovery, "8. Return explicit approval")
  Rel(recovery, state, "9. Move the run back to before-actions", "SQLite")
  Rel(recovery, policy, "10. Replay with approval evidence")
  Rel(policy, action, "11. Execute the approved write")
  Rel(action, loop, "12. Return observable evidence")
  Rel(loop, state, "13. Save after-action and terminal checkpoints", "SQLite")
  Rel(loop, channel, "14. Return the final response")
```
