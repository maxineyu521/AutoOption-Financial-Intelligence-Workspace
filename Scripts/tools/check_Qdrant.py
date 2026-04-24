import sys
from pathlib import Path

# dynamically resolve path issues
project_root = str(Path(__file__).resolve().parents[2])
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from Scripts.vector_store.connection import get_qdrant_client

def run_health_check():
    client = get_qdrant_client()
    collection_name = "financial_rag_gold"
    
    print("="*50)
    print("🏥 Qdrant Database Health Check")
    print("="*50)
    
    # 1. check if the collection exists and its count
    try:
        count_result = client.count(collection_name=collection_name)
        print(f"✅ Collection found: '{collection_name}'")
        print(f"📊 Total Documents in DB: {count_result.count}")
    except Exception as e:
        print(f"❌ Collection missing or error: {e}")
        return

    # 2. sample one latest SEC document to verify Payload and Hybrid Vectors
    print("\n🔍 Sampling 1 SEC document to verify Payload and Hybrid Vectors...")
    scroll_results, _ = client.scroll(
        collection_name=collection_name,
        scroll_filter={"must": [{"key": "source_type", "match": {"value": "sec"}}]},
        limit=1,
        with_payload=True,
        with_vectors=True # force pull the vectors to see what's inside
    )
    
    if scroll_results:
        doc = scroll_results[0]
        print(f"\n[Document ID]: {doc.id}")
        print(f"[Vector Types Found]: {list(doc.vector.keys())}") # should print out ['dense', 'sparse']
        print(f"[Text Preview]: {doc.payload.get('text')[:100]}...")
        print(f"[Ticker]: {doc.payload.get('ticker')}")
        print(f"[Unified Timestamp]: {doc.payload.get('unified_timestamp')}")
        print(f"[Has Bronze Evidence?]: {doc.payload.get('has_bronze_evidence')}")
    else:
        print("⚠️ No SEC documents found.")

if __name__ == "__main__":
    run_health_check()