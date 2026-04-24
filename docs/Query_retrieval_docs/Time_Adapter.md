# Time Alignment Adapter — `time_adapter.py`

## 1. Goal

`time_adapter.py` is the single location where a semantic `TimeWindow` from the LLM (e.g. `"yesterday"`, `"past_week"`, `"past_month"`) is compiled into physically-executable time predicates that each data source can actually apply. It eliminates per-handler date arithmetic scattered across the codebase and guarantees that every retriever — Gold (Qdrant) and Silver (DuckDB) — applies the same window for the same source type.

**Core problem solved:** different sources have fundamentally different temporal granularities. A naive uniform `(start_ts, end_ts)` filter causes:
- `"yesterday"` against a MONTHLY source (GPR) → 0 rows (correct answer is the current month's row)
- `"yesterday"` on a Monday against a DAILY source (Options) → 0 rows (last trading day was Friday)
- SEC event-level sources returning 0 because legacy payloads only carry ISO-8601 strings, not Unix epoch integers

The adapter pre-compiles per-source widening so every downstream retriever receives semantically correct, physically valid windows without branching logic.

---

## 2. Architecture

```
TimeWindow (semantic, from LLM)  +  anchor date (from SilverSQLTool._get_anchor_date)
         │
         ▼
 compile_all(time_window, anchor, label)
         │
         ├─ compile_predicate(time_window, GOLD_NEWS,      anchor)
         ├─ compile_predicate(time_window, GOLD_SEC,       anchor)
         ├─ compile_predicate(time_window, GOLD_GPR,       anchor)
         ├─ compile_predicate(time_window, SILVER_OPTIONS, anchor)
         ├─ compile_predicate(time_window, SILVER_MACRO,   anchor)
         └─ compile_predicate(time_window, SILVER_GPR,     anchor)
                │
                ▼  per source:
         SOURCE_SPECS[source]  →  granularity + min_lookback_days
                │
                ▼
         Apply widening policy
         │  EVENT   → no widening; use raw window_days
         │  DAILY   → business-day safe: if label in {"today","yesterday"}, step back to prev_business_day
         │             apply min_lookback_days floor (3d) for weekend gaps
         │  MONTHLY → widen to first-of-month (GPR); apply 35-day floor
                │
                ▼
         TimePredicate(source, label, start_date, end_date,
                       start_epoch_s, end_epoch_s, window_days,
                       widened, widen_reason, time_keys, key_units)
                │
                ▼
         Dict[SourceTimeKey, TimePredicate]  →  MasterRetriever
         (also serialised to state["time_range"]["source_predicates"])
```

---

## 3. Code Strategy & Workflow

### 3.1 Source Registry (`SOURCE_SPECS`)

Every data source is described by a `SourceTimeSpec` dataclass:

| Source Key | Granularity | Physical Time Keys | Key Units | Min Lookback |
|:---|:---|:---|:---|:---|
| `gold.news` | EVENT | `unified_timestamp`, `publish_timestamp` | `epoch_s`, `epoch_s` | 1 day |
| `gold.sec` | EVENT | `unified_timestamp`, `filed_at`, `transaction_date` | `epoch_s`, `iso_datetime`, `iso_date` | 3 days |
| `gold.gpr` | MONTHLY | `unified_timestamp`, `publish_timestamp` | `epoch_s`, `epoch_s` | 35 days |
| `silver.options` | DAILY | `snapshot_date` | `iso_date` | 3 days |
| `silver.macro` | DAILY | `observation_date`, `retrieval_date` | `iso_date`, `iso_date` | 3 days |
| `silver.gpr` | MONTHLY | `date`, `month` | `ts_ns`, `ts_ns` | 35 days |

### 3.2 Widening Policy by Granularity

```
EVENT granularity
  → use raw window_days from semantic TimeWindow
  → min floor = max(min_lookback_days, window_days)
  → [SEC special] if window_days < 3 → widen to 3d (event_min_3d)

DAILY granularity
  → if label in {"today", "yesterday"}:
       end    = anchor (or previous_business_day(anchor) if anchor is not a business day)
       today      -> start = end
       yesterday  -> start = previous_business_day(end)
       reason = "daily_today_busday (...)" / "daily_yesterday_busday (...)"
  → else: use raw window_days with min floor = 3d

MONTHLY granularity
  → if window_days < 35:
       start = first_of_month(anchor)
       reason = "monthly_widen_to_<N>d"  # N is computed from anchor day
  → this guarantees monthly rows are query-visible for short semantic windows
```

### 3.3 `TimePredicate` — the Compiled Artefact

`TimePredicate` is a frozen dataclass. Once compiled, it is never re-derived. Retrievers consume it directly:

- **Qdrant Range filter:** use `start_epoch_s` / `end_epoch_s` for numeric `Range` payload filter
- **DuckDB WHERE clause:** use `start_date` / `end_date` as ISO strings in `BETWEEN` filter

### 3.4 Serialisation & Round-Trip

`TimePredicate.to_dict()` produces a JSON-safe dict stored in `state["time_range"]["source_predicates"]`. `TimePredicate.from_dict()` / `predicates_from_serialised()` reconstruct exact live objects — this is the mechanism that allows the Checker's rescue path to reuse the exact same window as the initial retrieval, eliminating the 3d/2d drift documented in `docs/test/2026-04-22/router_e2e_deep_analysis.md`.

---

## 4. Output Data Schema

### `TimePredicate` Fields

| Field | Type | Description |
|:---|:---|:---|
| `source` | `SourceTimeKey` | Logical source identifier |
| `label` | `str` | Human-readable semantic label (e.g. `"yesterday"`) |
| `granularity` | `TimeGranularity` | `event`, `daily`, or `monthly` |
| `start_date` | `date` | Physical start of the window |
| `end_date` | `date` | Physical end of the window (anchor) |
| `start_epoch_s` | `int` | Unix seconds (UTC midnight of start_date) |
| `end_epoch_s` | `int` | Unix seconds (23:59:59 UTC of end_date, end-inclusive) |
| `window_days` | `int` | `(end_date − start_date).days` |
| `widened` | `bool` | True if the semantic window was widened for source alignment |
| `widen_reason` | `str \| None` | E.g. `"daily_yesterday_busday (...)"`, `"monthly_widen_to_22d (...)"`, `"event_min_3d (...)"` |
| `time_keys` | `tuple[str, ...]` | Physical key names to build filters on (copied from `SourceTimeSpec`) |
| `key_units` | `tuple[str, ...]` | Physical type for each key (`epoch_s`, `iso_date`, `ts_ns`, …) |

### Serialised Form in `state["time_range"]`

```json
{
  "time_window_label": "yesterday",
  "window_days": 1,
  "anchor_date": "2026-04-22",
  "start_date": "2026-04-21",
  "end_date": "2026-04-22",
  "is_default_window_applied": false,
  "source_predicates": {
    "gold.sec": {
      "source": "gold.sec",
      "label": "yesterday",
      "granularity": "event",
      "start_date": "2026-04-20",
      "end_date": "2026-04-23",
      "start_epoch_s": 1745107200,
      "end_epoch_s": 1745452799,
      "window_days": 3,
      "widened": true,
      "widen_reason": "event_min_3d",
      "time_keys": ["unified_timestamp", "filed_at", "transaction_date"],
      "key_units": ["epoch_s", "iso_datetime", "iso_date"]
    },
    "silver.options": {
      "source": "silver.options",
      "label": "yesterday",
      "granularity": "daily",
      "start_date": "2026-04-22",
      "end_date": "2026-04-22",
      "window_days": 1,
      "widened": true,
      "widen_reason": "daily_yesterday_busday",
      "time_keys": ["snapshot_date"],
      "key_units": ["iso_date"]
    },
    "silver.gpr": {
      "source": "silver.gpr",
      "label": "yesterday",
      "granularity": "monthly",
      "start_date": "2026-04-01",
      "end_date": "2026-04-22",
      "window_days": 22,
      "widened": true,
      "widen_reason": "monthly_widen_to_22d"
    }
  }
}
```

### Audit Log Lines (emitted by `MasterRetriever`)

```
🕒 [TimeAdapter] gold.sec:    widened → 2026-04-20..2026-04-23 (3d)  | reason=event_min_3d
🕒 [TimeAdapter] silver.options: widened → 2026-04-22..2026-04-23 (1d) | reason=daily_yesterday_busday
🕒 [TimeAdapter] silver.gpr: widened → 2026-04-01..2026-04-23 (22d)  | reason=monthly_widen_to_22d (base=2d < min=35d)
```

---

## 5. How to Test

### Compile a single predicate

```python
from datetime import date
from Scripts.retrieval.time_adapter import compile_predicate, SourceTimeKey
from Scripts.retrieval.schema import TimeWindow

pred = compile_predicate(TimeWindow.YESTERDAY, SourceTimeKey.SILVER_OPTIONS, anchor=date(2026, 4, 23))
print(pred.start_date, pred.end_date, pred.widened, pred.widen_reason)
# → 2026-04-22  2026-04-22  True  daily_yesterday_busday
```

### Compile full predicate set

```python
from datetime import date
from Scripts.retrieval.time_adapter import compile_all, SourceTimeKey
from Scripts.retrieval.schema import TimeWindow

preds = compile_all(TimeWindow.PAST_WEEK, date(2026, 4, 23))
for key, p in preds.items():
    print(f"{key.value}: {p.start_date} → {p.end_date} | widened={p.widened}")
```

### Serialisation round-trip

```python
from Scripts.retrieval.time_adapter import predicates_from_serialised, compile_all
from datetime import date
from Scripts.retrieval.schema import TimeWindow

preds = compile_all(TimeWindow.YESTERDAY, date(2026, 4, 23))
serialised = {k.value: v.to_dict() for k, v in preds.items()}
restored = predicates_from_serialised(serialised)
assert list(preds.keys()) == list(restored.keys())
```

### Monday / weekend edge-case test

```python
from datetime import date
from Scripts.retrieval.time_adapter import compile_predicate, SourceTimeKey
from Scripts.retrieval.schema import TimeWindow

# Monday anchor — yesterday should resolve to Friday
pred = compile_predicate(TimeWindow.YESTERDAY, SourceTimeKey.SILVER_OPTIONS, anchor=date(2026, 4, 27))
assert pred.start_date == date(2026, 4, 24)  # Friday
```

---

## 6. Dependencies

| Library | Purpose |
|:---|:---|
| `Scripts/core/trading_calendar.py` | `is_business_day`, `previous_business_day` — dependency-free business-day arithmetic |
| `Scripts/retrieval/schema.py` | `TimeWindow`, `TIME_WINDOW_DAYS`, `time_window_to_days` |

No external pip packages required. The module is stateless and has zero database/network dependencies, making it independently unit-testable.

```bash
# Run from repository root
python -c "
from datetime import date
from Scripts.retrieval.time_adapter import compile_all
from Scripts.retrieval.schema import TimeWindow
preds = compile_all(TimeWindow.YESTERDAY, date.today())
for k, p in preds.items():
    print(k.value, p.start_date, p.end_date, p.widen_reason)
"
```
