# Khalil — System Architecture

## High-Level Overview

```mermaid
graph TB
    subgraph Channels["Channel Layer"]
        TG[Telegram Bot]
        WA[WhatsApp]
        SL[Slack Bot]
        DC[Discord Bot]
        API[FastAPI REST]
    end

    subgraph Core["Core Pipeline"]
        MCtx[MessageContext]
        ID[Intent Detection]
        TD[Tool Dispatch]
        LLM[LLM Engine]
    end

    subgraph Skills["Skill & Action System"]
        SR[Skill Registry]
        TC[Tool Catalog]
        Actions["73 Action Modules"]
    end

    subgraph Intelligence["Intelligence Layer"]
        AL[Agent Loop]
        Learn[Learning Engine]
        Auto[Autonomy Controller]
        Guard[Guardian]
    end

    subgraph SelfImprovement["Self-Improvement"]
        Evo[Evolution Engine]
        Heal[Self-Healing]
        Ext[Self-Extension]
    end

    subgraph Infra["Infrastructure"]
        DB[(SQLite + Vec)]
        Sched[APScheduler]
        OAuth[OAuth Tokens]
        MR[Model Router]
    end

    TG & WA & SL & DC & API --> MCtx
    MCtx --> ID
    ID -->|fast-path regex| SR
    ID -->|fallback| LLM
    SR --> TD
    TD --> Actions
    LLM -->|tool-use| TC
    TC --> SR
    Actions --> MCtx

    AL -->|sense/think/act| Actions
    AL --> Auto
    Learn -->|signals| Evo
    Heal --> Evo
    Ext --> Evo
    Evo -->|PRs| Actions
    Guard --> TD
    Auto --> TD

    Actions --> DB
    Learn --> DB
    Sched --> AL
    MR --> LLM
```

## Message Processing Pipeline

```mermaid
sequenceDiagram
    participant U as User
    participant Ch as Channel
    participant Ctx as MessageContext
    participant ID as Intent Detection
    participant SR as Skill Registry
    participant LLM as Claude/Ollama
    participant TC as Tool Catalog
    participant Act as Action Handler
    participant DB as SQLite

    U->>Ch: Send message
    Ch->>Ctx: Wrap in MessageContext
    Ctx->>ID: detect_intent(text)

    alt Fast Path — Regex Match
        ID->>SR: match_intent(text)
        SR-->>ID: (action_type, skill)
        ID->>Act: handler(action, intent, ctx)
    else Conversational — Greeting/Chat
        ID->>LLM: ask_llm_stream(query, context)
        LLM-->>Ctx: Streamed response
    else Complex — Tool-Use Loop
        ID->>TC: generate_tool_schemas(registry)
        TC-->>LLM: tools + messages
        loop Max 5 iterations (tool_choice=none after iter 2)
            LLM->>LLM: Select tool + action
            LLM->>Act: _execute_tool_call(tool_call)
            Act-->>LLM: Tool result
        end
        LLM-->>Ctx: Final response
    end

    Act->>DB: save_message()
    Act->>DB: record_signal() [learning]
    Ctx->>Ch: reply(text)
    Ch->>U: Display response
```

## Skill & Action System

```mermaid
graph LR
    subgraph Discovery["Startup Discovery"]
        Scan["Scan actions/*.py<br/>for SKILL dicts"]
        ExtScan["Scan extensions/*.json<br/>for manifests"]
    end

    subgraph Registry["Skill Registry"]
        Match["match_intent(text)<br/>Regex patterns"]
        Handler["get_handler(action_type)<br/>→ callable"]
        Context["get_context_for_intent(text)<br/>→ selective LLM context"]
    end

    subgraph Catalog["Tool Catalog"]
        Filter["_INCLUDE_SKILLS<br/>~16 curated skills"]
        Schema["generate_tool_schemas()<br/>OpenAI format"]
    end

    subgraph ActionModules["Action Modules (73)"]
        direction TB
        A1["calendar, gmail, reminders"]
        A2["shell, terminal, tmux_control"]
        A3["spotify, weather, web"]
        A4["machine, dev_tools, gui_automation"]
        A5["github_api, slack_reader, drive"]
        A6["workflows, pomodoro, synthesis"]
        A7["apple_reminders, apple_notes, apple_health"]
        A8["extend, guardian, healing"]
    end

    Scan --> Registry
    ExtScan --> Registry
    Registry --> Catalog
    Match --> Handler
    Handler --> ActionModules
    Filter --> Schema
    Schema -->|"~120 action types<br/>across ~16 tools"| LLM["LLM Tool-Use"]
```

## Agent Loop — Sense/Think/Act Cycle

```mermaid
graph TB
    subgraph Sense["SENSE (every 5 min)"]
        S1[System Health<br/>disk, battery, apps]
        S2[Skill Sensors<br/>calendar, email, dev_state]
        S3[Evolution Sensor<br/>pending signals count]
    end

    subgraph Think["THINK"]
        O1[System Health Alerts<br/>disk > 90%]
        O2[Follow-up Nudges<br/>overdue items]
        O3[Routine Drift<br/>deviation from patterns]
        O4[Time-Aware Nudges<br/>end-of-day sweep]
        O5[Evolution Readiness<br/>signals ≥ 5 or 6h elapsed]
        O6[Cross-Domain Synthesis<br/>compound stress detection]
    end

    subgraph Filter["FILTER"]
        QH[Quiet Hours<br/>11pm–7am]
        LP[Learned Preferences<br/>suppressed skills]
        GT[Good Timing<br/>activity patterns]
    end

    subgraph Act["ACT"]
        AA[Autonomy Check<br/>needs_approval?]
        Exec[Execute Action]
        Alert[Send Alert Only]
    end

    subgraph Report["REPORT"]
        Batch[Batch Notification<br/>acted + alerted]
    end

    S1 & S2 & S3 --> Think
    O1 & O2 & O3 & O4 & O5 & O6 --> Filter
    QH & LP & GT --> Act
    AA -->|approved| Exec
    AA -->|needs approval| Alert
    Exec & Alert --> Report
```

## Self-Improvement Architecture

```mermaid
graph TB
    subgraph Signals["Signal Sources"]
        IM[Incoming Messages<br/>post_interaction_check]
        AF[Action Failures<br/>record_signal]
        UC[User Corrections<br/>learning engine]
        SM[Search Misses<br/>learning engine]
        SR2[Slow Responses<br/>latency > 5s]
    end

    subgraph Evolution["Evolution Engine (4x/day)"]
        Gather["GATHER<br/>aggregate all signal sources"]
        Rank["RANK<br/>impact × feasibility"]
        Execute["EXECUTE<br/>top 2 candidates"]
        Verify["VERIFY<br/>did PRs merge? signals improve?"]
    end

    subgraph Healing["Self-Healing Pipeline"]
        DetectFail["detect_recurring_failures()<br/>fingerprint + threshold"]
        Diagnose["build_diagnosis()<br/>map to source file"]
        GenPatch["generate_healing_patch()<br/>Claude generates fix"]
    end

    subgraph Extension["Self-Extension Pipeline"]
        DetectGap["detect_capability_gap()<br/>regex on LLM response"]
        ClassGap["classify_gap()<br/>LLM: CAP_GAP vs KNOWLEDGE"]
        GenCode["generate_extension_code()<br/>Claude Opus generates module"]
    end

    subgraph Safety["Safety Gates"]
        GR[Guardian Review<br/>review_code_patch]
        Auto2[Autonomy Controller<br/>WRITE approval]
        RL[Rate Limits<br/>2 PRs/cycle, 1 gen/hour]
    end

    subgraph Output["Output"]
        PR[GitHub PR<br/>branch + commit + push]
        Notify[User Notification<br/>via Telegram]
    end

    IM & AF & UC & SM & SR2 --> Gather
    Gather --> Rank
    Rank --> Execute

    Execute -->|"category: fix"| Healing
    Execute -->|"category: extend"| Extension

    DetectFail --> Diagnose --> GenPatch
    DetectGap --> ClassGap --> GenCode

    GenPatch & GenCode --> Safety
    GR & Auto2 & RL --> PR
    PR --> Notify
    Notify --> Verify
    Verify -->|"next cycle"| Gather
```

## LLM & Model Layer

```mermaid
graph LR
    subgraph Router["Model Router"]
        Classify["classify_complexity(query)"]
        Fast["FAST<br/>greetings, status<br/>< 20 chars"]
        Std["STANDARD<br/>default tier"]
        Complex["COMPLEX<br/>code gen, healing<br/>> 500 chars"]
    end

    subgraph Providers["Provider Stack"]
        TF["Taskforce Proxy<br/>hendrix-genai.spotify.net"]
        Claude["Claude Opus/Sonnet"]
        GPT["GPT-5.2 (fallback)"]
        Gemini["Gemini 2.5 Pro (fallback)"]
        Ollama["Ollama Local<br/>qwen3:14b"]
    end

    subgraph Privacy["Privacy Routing"]
        Sens["Sensitive Pattern Match"]
        Local["Force Local (Ollama)"]
    end

    subgraph CircuitBreaker["Resilience"]
        CB["Circuit Breaker<br/>per provider"]
        Fallback["Provider Fallback<br/>Claude → GPT → Gemini → Ollama"]
    end

    Classify --> Fast & Std & Complex
    Fast & Std --> TF
    Complex --> TF
    TF --> Claude & GPT & Gemini
    Sens --> Local --> Ollama
    CB --> Fallback
```

## Autonomy & Safety

```mermaid
graph TB
    subgraph Levels["Autonomy Levels"]
        SUP["SUPERVISED<br/>Ask for everything"]
        GUI["GUIDED<br/>Auto-approve READ<br/>Ask WRITE/DANGEROUS"]
        AUT["AUTONOMOUS<br/>Auto-approve all<br/>except HARD_GUARDRAILS"]
    end

    subgraph Classification["Action Classification"]
        R["READ<br/>search, status, list"]
        W["WRITE<br/>send email, create reminder<br/>shell write, evolution cycle"]
        D["DANGEROUS<br/>send money, delete data<br/>shell dangerous"]
    end

    subgraph Gates["Safety Gates"]
        RL2["Rate Limiter<br/>per action type"]
        Guard2["Guardian<br/>Claude Haiku review"]
        Audit["Audit Log<br/>DB + JSONL trail"]
        HG["Hard Guardrails<br/>never auto-approved"]
    end

    R --> RL2
    W --> Guard2 --> RL2
    D --> HG --> Guard2

    RL2 --> Audit
```

## Data Layer

```mermaid
erDiagram
    documents {
        int id PK
        text source
        text category
        text title
        text content
        blob embedding
    }

    interaction_signals {
        int id PK
        text signal_type
        text context_json
        text value
        text action_hint
        datetime created_at
    }

    insights {
        int id PK
        text category
        text summary
        text evidence_json
        text recommendation
        text status
        datetime created_at
    }

    learned_preferences {
        int id PK
        text key
        text value
        int source_insight_id FK
        real confidence
    }

    conversations {
        int id PK
        int chat_id
        text role
        text content
        datetime timestamp
    }

    memories {
        int id PK
        text memory_type
        text content
        blob embedding
    }

    evolution_candidates {
        int id PK
        text source
        text category
        text summary
        text evidence_json
        real impact_score
        real feasibility_score
        real priority
        text status
        int failure_count
    }

    reminders {
        int id PK
        text text
        datetime due_at
        text status
        datetime fired_at
    }

    audit_log {
        int id PK
        text action_type
        text description
        text result
        text autonomy_level
        datetime created_at
    }

    interaction_signals ||--o{ insights : "triggers"
    insights ||--o{ learned_preferences : "derives"
    interaction_signals ||--o{ evolution_candidates : "feeds"
    conversations ||--o{ memories : "extracts"
```

## Scheduled Jobs

```mermaid
gantt
    title Daily Schedule (Recurring Jobs)
    dateFormat HH:mm
    axisFormat %H:%M

    section Evolution
    Evolution Cycle     :03:00, 30min
    Evolution Cycle     :09:00, 30min
    Evolution Cycle     :15:00, 30min
    Evolution Cycle     :21:00, 30min

    section Agent Loop
    Sense/Think/Act     :00:00, 1440min

    section Digests
    Morning Brief       :08:00, 15min
    Financial Check     :12:00, 10min

    section Weekly (Monday)
    Career Alert        :18:00, 10min
    Weekly Summary      :18:30, 15min

    section Weekly (Friday)
    Friday Reflection   :17:00, 15min

    section Continuous
    Reminder Check      :00:00, 1440min
    Dev State Poll      :00:00, 1440min
    Email Sync          :00:00, 1440min
    Meeting Prep/Follow :00:00, 1440min
```

## External Integrations

```mermaid
graph TB
    K((Khalil))

    subgraph Google["Google APIs"]
        Gmail["Gmail<br/>read, compose, labels"]
        GCal["Calendar<br/>events, scheduling"]
        GDrive["Drive<br/>docs, search"]
        GTasks["Tasks"]
        GContacts["Contacts"]
        YouTube["YouTube"]
    end

    subgraph Apple["Apple Native"]
        AR["Reminders"]
        AN["Notes"]
        AH["HealthKit"]
        AM["Music"]
        HK["HomeKit"]
    end

    subgraph Dev["Developer Tools"]
        GH["GitHub<br/>PRs, issues, API"]
        CC["Claude Code<br/>via TTY"]
        Cursor["Cursor IDE"]
        DO["DigitalOcean"]
        ASC["App Store Connect"]
    end

    subgraph Comms["Communication"]
        TG2["Telegram"]
        Slack2["Slack"]
        Discord2["Discord"]
        WA2["WhatsApp"]
        iMsg["iMessage"]
    end

    subgraph Media["Media & Knowledge"]
        Spot["Spotify"]
        RW["Readwise"]
        Notion2["Notion"]
        Obsidian["Obsidian"]
        Anki2["Anki"]
        Web2["Web Search"]
    end

    subgraph Local["Local Services"]
        Ollama2["Ollama<br/>qwen3:14b"]
        HA["Home Assistant"]
        Shell2["Shell<br/>macOS commands"]
        GUI2["GUI Automation<br/>keyboard, mouse"]
    end

    K --- Google
    K --- Apple
    K --- Dev
    K --- Comms
    K --- Media
    K --- Local
```
