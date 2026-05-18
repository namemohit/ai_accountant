import os
import json
import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv
from google import generativeai as genai

load_dotenv()

# Configure Gemini API
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "AIzaSyCuVgfmx3oaja0O4Mr3jMb8wP7Ikpe9BXs")
genai.configure(api_key=GEMINI_API_KEY)

# Supabase Connection String
DB_URL = os.getenv("DB_URL", "postgresql://postgres.vxnflumpectzqdamjqsc:yantr_ai_labs@aws-1-ap-south-1.pooler.supabase.com:5432/postgres")

def get_embedding(text: str):
    try:
        result = genai.embed_content(
            model="models/gemini-embedding-2",
            content=text,
            task_type="retrieval_document"
        )
        return result['embedding']
    except Exception as e:
        print(f"Error generating embedding: {e}")
        return None

def backfill():
    print("[BACKFILL] Connecting to Supabase database...")
    conn = psycopg2.connect(DB_URL)
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    update_cursor = conn.cursor()
    
    try:
        cursor.execute("SELECT id, type, data, embedding FROM knowledge_base WHERE embedding IS NULL OR data->>'company_name' IS NULL")
        rows = cursor.fetchall()
        
        print(f"[BACKFILL] Found {len(rows)} rows needing embedding or company_name ring-fencing backfill.")
        if not rows:
            print("[BACKFILL] No backfill needed. All records are already fully vectorized and company ring-fenced!")
            return

        updated_count = 0
        for r in rows:
            row_id = r['id']
            data = r['data']
            existing_emb = r.get('embedding')
            if isinstance(data, str):
                data = json.loads(data)
                
            field = data.get("field", "")
            original = data.get("original", "")
            corrected = data.get("corrected", "")
            party_name = data.get("party_name", "Unknown")
            company_name = data.get("company_name", "Acme Corp")
            
            data["company_name"] = company_name
            
            if field == "ledger_group_mapping":
                desc = f"Ledger {original} belongs to group {corrected} for company {company_name}"
            else:
                desc = f"For {party_name}: The {field} should be '{corrected}' (NOT '{original}')"
                
            emb = existing_emb
            if not emb:
                print(f"-> Generating embedding for ID {row_id}: {desc}")
                emb = get_embedding(desc)
                
            if emb:
                emb_str = f"[{','.join(map(str, emb))}]" if isinstance(emb, list) else emb
                update_cursor.execute("""
                UPDATE knowledge_base 
                SET embedding = %s, data = %s 
                WHERE id = %s
                """, (emb_str, json.dumps(data), row_id))
                conn.commit()
                updated_count += 1
                print(f"   [SUCCESS] Row {row_id} vectorized and company ring-fenced.")
            else:
                print(f"   [ERROR] Failed to generate embedding for Row {row_id}")

        print(f"\n[BACKFILL COMPLETE] Successfully backfilled {updated_count} out of {len(rows)} legacy records.")
        
    except Exception as e:
        print(f"[BACKFILL ERROR] {e}")
    finally:
        cursor.close()
        update_cursor.close()
        conn.close()

if __name__ == "__main__":
    backfill()
