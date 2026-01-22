# 🌈 CephFS Storage Tiering System Architecture

> **A Dual-Mode, Automated Tiering Solution for High-Performance Storage**
> *Optimizing costs and performance using eBPF tracking and Intelligent Policy Engines.*

---

## 🏗️ System Overview

This system manages data placement across three storage tiers by analyzing file access patterns. It features a unique **Dual-Mode** capability, allowing administrators to switch between *Frequency-Based* and *Time-Based* optimization strategies on the fly.

### 🎨 High-Level Architecture

```mermaid
graph TD
    %% Nodes
    User(("👤 User / App"))
    Kernel["🐧 Linux Kernel<br>(CephFS Module)"]
    Tracker["️🕵️ eBPF Tracker<br>(BCC / Python)"]
    DB[("🗄️ PostgreSQL<br>(Metadata & Logs)")]
    Policy["🧠 Policy Engine<br>(Decision Maker)"]
    Worker["👷 Migration Worker<br>(Physical Mover)"]
    
    %% Storage Tiers
    subgraph Storage["💾 Storage Tiers"]
        NVMe["🚀 Tier 1: NVMe<br>(Hot Data)"]
        SSD["⚡ Tier 2: SSD<br>(Warm Data)"]
        HDD["📦 Tier 3: HDD<br>(Cold Data)"]
    end

    %% Connections
    User == "Reads/Writes" ==> Kernel
    Kernel -.-> |"kprobe events"| Tracker
    Tracker --> |"Batch Insert"| DB
    Policy <--> |"SQL Functions"| DB
    Worker --> |"Polls Tasks"| DB
    Worker ==> |"Moves Data"| Storage
    
    %% Styling
    classDef user fill:#ff9a9e,stroke:#333,stroke-width:2px;
    classDef kernel fill:#a18cd1,stroke:#333,stroke-width:2px,color:white;
    classDef component fill:#fad0c4,stroke:#333,stroke-width:2px;
    classDef db fill:#84fab0,stroke:#333,stroke-width:2px;
    classDef storage fill:#fccb90,stroke:#333,stroke-width:2px;
    
    class User user;
    class Kernel kernel;
    class Tracker,Policy,Worker component;
    class DB db;
    class NVMe,SSD,HDD storage;
```

---

## 🧠 Dual-Mode Intelligence

The system's core differentiator is its ability to switch "brains" based on workload needs.

```mermaid
graph LR
    subgraph Modes["🎛️ Active Mode Configuration"]
        direction TB
        
        ModeA["🅰️ Frequency Mode<br>(Score-Based)"]
        ModeB["🅱️ Time Mode<br>(Recency-Based)"]
        
        Switch{"🔄 Switcher"} --> ModeA
        Switch --> ModeB
    end

    subgraph LogicA["Logic A: Popularity"]
        MetricA["Metric: Access Count"]
        Formula["Formula: Score = 0.9 × Freq"]
        RuleA1["Promotion: Score ≥ 9"]
        RuleA2["Demotion: Score < 4.5"]
    end

    subgraph LogicB["Logic B: Freshness"]
        MetricB["Metric: Last Access Time"]
        RuleB1["Promotion: Accessed < 3m ago"]
        RuleB2["Demotion: Idle > 3m (Warm) / 6m (Cold)"]
    end

    ModeA -.-> LogicA
    ModeB -.-> LogicB

    %% Styling
    classDef mode fill:#ffecd2,stroke:#ffb347,stroke-width:2px;
    classDef logic fill:#d4fc79,stroke:#96e6a1,stroke-width:2px;
    
    class ModeA,ModeB,Switch mode;
    class MetricA,Formula,RuleA1,RuleA2,MetricB,RuleB1,RuleB2 logic;
```

### 1. Frequency Mode (Score-Based)
*   **Philosophy**: "Keep heavily used files fast, even if they haven't been touched in a few minutes."
*   **Formula**: `Score = 0.90 * Access_Frequency`
*   **Use Case**: Databases, Shared Libraries, Hot Datasets.

### 2. Time Mode (Recency-Based)
*   **Philosophy**: "Keep recently touched files fast. Move everything else to cold storage quickly."
*   **Logic**: 3-minute and 6-minute idle thresholds.
*   **Use Case**: Log processing, Temporary workspaces, Backups.

---

## 🔄 Data Lifecycle & Flow

The journey of a file access event from the Kernel to the Database.

```mermaid
sequenceDiagram
    autonumber
    participant App as 👤 Application
    participant Kern as 🐧 Kernel (CephFS)
    participant BPF as 🕵️ eBPF (BCC)
    participant Py as 🐍 Tracker Service
    participant DBs as 🗄️ PostgreSQL

    App->>Kern: read() / write()
    Kern->>BPF: kprobe: ceph_read_iter
    BPF->>BPF: Filter UID 0 & Hidden Files
    BPF->>BPF: Dedup (1s window)
    BPF->>Py: Perf Buffer Event
    loop Every 1 Second
        Py->>DBs: Batch INSERT (file_access_log)
    end
    
    rect rgb(240, 248, 255)
        note right of Py: Every 60 Seconds
        Py->>DBs: Call aggregate_access_log()
        DBs->>DBs: Move Log -> Metadata
        DBs->>DBs: Calculate Scores
        DBs->>DBs: Evaluate Policy
    end
```

---

## 🛠️ Detailed Component Architecture

### 1. eBPF Tracker (`f:\cephse\tracking service`)
*   **Technology**: BCC (BPF Compiler Collection) hooks into `ceph_read_iter` and `ceph_write_iter`.
*   **Performance**: Uses BPF maps for in-kernel deduplication to minimize overhead.
*   **Output**: Stream of access events to PostgreSQL `file_access_log`.

### 2. Database Layer (`PostgreSQL`)
*   **Hot Table**: `file_access_log` - Append-only, high write throughput.
*   **Cold Table**: `file_metadata` - Stores the state, score, and tier of every file.
*   **Logic**: All tiering logic resides in **Stored Procedures** (`aggregate_access_log`, `apply_tiering_policies`). This ensures data locality and high performance.

### 3. Migration Worker (`f:\cephse\migration engine`)
*   **Parallelism**: 5 concurrent worker threads.
*   **Mechanism**: Server-side object copy (using `librados`/`libcephfs`). Data does not pass through the client.
*   **Safety**: Uses **Shadow Files** for atomic pool switching.
    1.  Create `file.txt.__tiering__` in target pool.
    2.  Copy data.
    3.  `mv file.txt.__tiering__ file.txt` (Atomic Rename).

### 4. Policy Engine (`f:\cephse\policy engine`)
*   **Role**: The conductor. It wakes up every 60s to trigger the DB functions.
*   **Switching**: Dynamically chooses which SQL function to call based on the mode set by `switch_tiering.sh`.

---

## 📊 Tiering Logic Visualization

How a file moves through the tiers in **Frequency Mode**.

```mermaid
stateDiagram-v2
    state "Tier 1: NVMe (Hot)" as Hot
    state "Tier 2: SSD (Warm)" as Warm
    state "Tier 3: HDD (Cold)" as Cold

    [*] --> Hot: New File
    
    Hot --> Warm: Score < 9.0
    Warm --> Hot: Score ≥ 9.0
    
    Warm --> Cold: Score < 4.5
    Cold --> Hot: Score > 0 (Any Access)
    
    note right of Hot
        High Performance
        Cost: $$$
    end note
    
    note right of Cold
        High Capacity
        Cost: $
    end note
```

---

## 📂 Project Structure Map

```mermaid
graph TD
    Root["📁 f:\cephse"]
    
    subgraph Services
        Track["📂 tracking service<br>(eBPF + Python)"]
        Pol["📂 policy engine<br>(Orchestrator)"]
        Mig["📂 migration engine<br>(Worker)"]
    end
    
    subgraph Config
        Tech["📄 TECHNICAL_PRESENTATION.md<br>(SQL Definitions)"]
        Arch["📄 ARCHITECTURE.md<br>(Docs)"]
        Switch["📜 switch_tiering.sh<br>(Mode Toggle)"]
    end
    
    Root --> Track
    Root --> Pol
    Root --> Mig
    Root --> Tech
    Root --> Arch
    Root --> Switch
    
    style Root fill:#f9f,stroke:#333,stroke-width:2px
```
