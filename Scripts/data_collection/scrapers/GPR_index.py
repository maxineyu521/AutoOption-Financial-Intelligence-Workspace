import os
import time
import uuid
import requests
import pandas as pd
from datetime import datetime
import io
import warnings

warnings.filterwarnings('ignore')

# ==========================================
# 0. Dynamic Path & Directory Setup
# ==========================================
current_date = datetime.now().strftime("%Y-%m-%d")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, "..", "..", ".."))

# Bronze: optional dated raw exports. Gold: dated corpus per run.
# Silver: single canonical folder — one Parquet replaces the full 2020+ series (no date subfolder).
csv_FOLDER = os.path.join(PROJECT_ROOT, "Data", "1_Bronze_Raw", "GPR_index", current_date)
SILVER_GPR_DIR = os.path.join(PROJECT_ROOT, "Data", "2_Silver_Processed", "GPR_index")
Json_FOLDER = os.path.join(PROJECT_ROOT, "Data", "3_Gold_Semantic", "GPR_index", current_date)

os.makedirs(SILVER_GPR_DIR, exist_ok=True)
os.makedirs(Json_FOLDER, exist_ok=True)
os.makedirs(csv_FOLDER, exist_ok=True)

# Log setting: Saved in logs/YYYY-MM-DD/
LOG_FOLDER = os.path.join(PROJECT_ROOT, "logs", current_date)
os.makedirs(LOG_FOLDER, exist_ok=True)
LOG_FILE = os.path.join(LOG_FOLDER, "gpr_downloader.log")

def log_print(message):
    print(message)
    with open(LOG_FILE, 'a', encoding='utf-8') as f:
        f.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} - {message}\n")

log_print("System Startup: Robust GPR Downloader Initialized.")

# ==========================================
# 1. Download & Parse GPR Data (With Fault Tolerance)
# ==========================================
GPR_EXCEL_URL = "https://www.matteoiacoviello.com/gpr_files/data_gpr_export.xls"

def fetch_and_process_gpr(max_retries=3):
    """Fetches data with a robust retry mechanism for network drops."""
    for attempt in range(1, max_retries + 1):
        log_print(f"Attempt {attempt}/{max_retries}: Downloading GPR data...")
        try:
            response = requests.get(GPR_EXCEL_URL, timeout=30)
            response.raise_for_status()
            
            # Read binary content into Pandas
            df = pd.read_excel(io.BytesIO(response.content))
            
            # Clean and standardize column names
            df.columns = df.columns.str.strip().str.lower()
            
            if 'month' not in df.columns or 'gpr' not in df.columns:
                log_print("WARNING: Target columns not found. Excel structure might have changed.")
                return None
                
            # Drop empty rows
            df = df.dropna(subset=['gpr'])
            
            # Convert '1985M01' to standard datetime format
            df['date'] = pd.to_datetime(df['month'].astype(str).str.replace('M', '-'), errors='coerce')
            df = df.dropna(subset=['date']).sort_values('date').reset_index(drop=True)
            
            log_print(f"Success: Loaded {len(df)} historical records.")
            return df

        except Exception as e:
            log_print(f"Network/Parsing Error on attempt {attempt}: {e}")
            if attempt < max_retries:
                log_print("Retrying in 10 seconds...")
                time.sleep(10)
            else:
                log_print("CRITICAL: Failed to download GPR data after maximum retries. Aborting to protect existing data.")
                return None

# ==========================================
# 2. Enrich Metadata (Deltas & Moving Averages)
# ==========================================
def enrich_gpr_data(df):
    log_print("Calculating MoM, YoY, and Historical Trends...")
    
    # CRITICAL: Calculate moving averages BEFORE truncating to 2020. 
    # This ensures January 2020 has accurate historical context from late 2019.
    
    # Month-over-Month
    df['gpr_mom_pct'] = df['gpr'].pct_change(periods=1) * 100
    
    # Year-over-Year
    df['gpr_yoy_pct'] = df['gpr'].pct_change(periods=12) * 100
    
    # Moving averages
    df['gpr_3m_ma'] = df['gpr'].rolling(window=3).mean()
    
    # Historical percentile
    df['gpr_percentile'] = df['gpr'].rank(pct=True) * 100

    return df.round(2)

# ==========================================
# 3. Natural Language Generation & Qdrant Formatting
# ==========================================
def generate_rag_markdown(row):
    """Converts a row of data into a clean, RAG-friendly paragraph."""
    date_str = row['date'].strftime("%B %Y")
    gpr = row['gpr']
    mom = row['gpr_mom_pct']
    yoy = row['gpr_yoy_pct']
    percentile = row['gpr_percentile']
    
    # Handle NaN values elegantly (prevents 'nan' from leaking into text)
    ma_3_text = f"{row['gpr_3m_ma']:.2f}" if pd.notna(row['gpr_3m_ma']) else "Insufficient historical data"
    
    # Dynamic narrative based on momentum
    if pd.isna(mom):
        trend_narrative = "Data initialization phase."
    elif mom > 10:
        trend_narrative = f"This represents a **significant escalation**, surging by {mom:.2f}% compared to the previous month."
    elif mom < -10:
        trend_narrative = f"This indicates a **cooling off** of geopolitical tensions, dropping by {abs(mom):.2f}% month-over-month."
    else:
        trend_narrative = f"The index changed by {mom:.2f}% month-over-month, showing relative stability."

    yoy_narrative = f"Compared to the same period last year, the index shifted by {yoy:.2f}%." if pd.notna(yoy) else ""
    
    md_text = f"""### Geopolitical Risk (GPR) Index Update: {date_str}

**Date:** {date_str}
**GPR Score:** {gpr}

**Summary:**
In {date_str}, the global Geopolitical Risk (GPR) Index stood at **{gpr}**. {trend_narrative} {yoy_narrative}

**Historical Context:**
This reading sits at the **{percentile:.1f}th percentile** of all historically recorded geopolitical risk levels. A 3-month moving average of {ma_3_text} suggests the current medium-term trend.

**Impact on Precious Metals:**
Historically, spikes in the GPR index correlate with safe-haven asset accumulation, driving up the implied volatility and spot prices of Gold (XAU) and Silver (XAG).
"""
    return md_text

# ==========================================
# 4. Save Logic (Idempotent File Generation)
# ==========================================
def save_data(df):
    import json
    
    # 1. Truncate time window: post-2020 for RAG, Parquet, and downstream consumers
    recent_df = df[df['date'] >= '2020-01-01'].copy()
    log_print(f"Truncated dataset to post-2020. Generating embeddings for {len(recent_df)} months.")

    # Silver: one file, full post-2020 history — no dated subfolder (each run overwrites the canonical snapshot).
    parquet_path = os.path.join(SILVER_GPR_DIR, "gpr_monthly_enriched.parquet")
    try:
        recent_df.to_parquet(parquet_path, index=False, engine="pyarrow")
        log_print(f"SUCCESS: Silver Parquet saved to {parquet_path}")
    except ImportError:
        log_print(
            "WARNING: pyarrow is required for Parquet export. Install with: pip install pyarrow"
        )
    except Exception as e:
        log_print(f"WARNING: Parquet write failed: {e}")

    md_path = os.path.join(Json_FOLDER, "gpr_narrative_corpus.md")
    qdrant_jsonl_path = os.path.join(Json_FOLDER, "qdrant_gpr_input.jsonl")
    
    # Open in 'w' mode to Overwrite safely
    with open(md_path, 'w', encoding='utf-8') as f_md, open(qdrant_jsonl_path, 'w', encoding='utf-8') as f_jsonl:
        
        for idx, row in recent_df.iterrows():
            year = row['date'].year
            month = row['date'].month
            date_str = row['date'].strftime("%Y-%m-%d")
            
            # Generate RAG Narrative
            md_text = generate_rag_markdown(row)
            f_md.write(md_text + "\n\n---\n\n")
            
            # Generate Deterministic ID for Qdrant (Guarantees Idempotent Upserts)
            # e.g., GPR_2024_04 will always generate the exact same UUID
            doc_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"GPR_{year}_{month}"))
            
            # Prepare structured Qdrant Payload
            qdrant_record = {
                "id": doc_id,
                "text": md_text,
                "metadata": {
                    "topic": "macro_geopolitics_risk",
                    "title": f"GPR Index Update {year}-{month:02d}",
                    "publish_date": date_str,
                    "publish_timestamp": int(row['date'].timestamp()),
                    "gpr_score": row['gpr'],
                    "gpr_percentile": row['gpr_percentile']
                }
            }
            f_jsonl.write(json.dumps(qdrant_record, ensure_ascii=False) + '\n')
            
    log_print(f"SUCCESS: Saved NLP narrative to {md_path}")
    log_print(f"SUCCESS: Saved Qdrant payload to {qdrant_jsonl_path}")

if __name__ == "__main__":
    log_print("Starting GPR Index pipeline...")
    
    # Step 1: Download safely
    raw_df = fetch_and_process_gpr()
    
    if raw_df is not None:
        # Step 2: Calculate metrics on ALL historical data
        enriched_df = enrich_gpr_data(raw_df)
        
        # Step 3: Truncate and Overwrite files ONLY if everything above succeeded
        save_data(enriched_df)
        
        # Optional: Save a CSV for quick human viewing
        csv_path = os.path.join(csv_FOLDER, "gpr_preview.csv")
        enriched_df.tail(24).to_csv(csv_path, index=False) 
        
        log_print("Pipeline completed successfully! Files are ready for Vector DB Upsert.")
    else:
        log_print("Pipeline aborted due to upstream errors. Existing data remains untouched.")