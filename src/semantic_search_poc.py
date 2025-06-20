import os
import sys
import psycopg2
from dotenv import load_dotenv
import numpy as np
import requests

# Usage info
USAGE = """\
Usage: python semantic_search_poc.py "your search query"
"""

# Load environment variables from .env
load_dotenv()

POSTGRES_PARAMS = {
    'host': os.getenv('POSTGRES_HOST', 'localhost'),
    'port': os.getenv('POSTGRES_PORT', 5432),
    'dbname': os.getenv('POSTGRES_DB', 'postgres'),
    'user': os.getenv('POSTGRES_USER', 'postgres'),
    'password': os.getenv('POSTGRES_PASSWORD', '')
}
LLAMA_SERVER_URL = os.getenv('LLAMA_SERVER_URL', 'http://localhost:8000/embed')

# --- Embedding via HTTP llama-server ---
def get_embedding_from_server(text, dim=1024):
    if not text or not text.strip():
        return np.zeros(dim, dtype=np.float32)
    try:
        response = requests.post(
            LLAMA_SERVER_URL,
            #json={"content": text }
            json={"content": text + "<|endoftext|>"}
        )
        response.raise_for_status()
        emb = np.array(response.json()[0]["embedding"][0], dtype=np.float32)
        print("len(emb):", len(emb))
        if emb.shape[0] > dim:
            emb = emb[:dim]
        elif emb.shape[0] < dim:
            emb = np.pad(emb, (0, dim - emb.shape[0]))
        return emb
    except Exception as e:
        print(f"Embedding server error: {e}")
        return np.zeros(dim, dtype=np.float32)

# --- Main semantic search logic ---
def main():
    if len(sys.argv) != 2:
        print(USAGE)
        sys.exit(1)
    query = sys.argv[1]
    print(f"Semantic search for: {query}")
    query_emb = get_embedding_from_server(query, 1024)
    if np.all(query_emb == 0):
        print("Failed to get embedding for query.")
        sys.exit(1)

    # Connect to Postgres
    conn = psycopg2.connect(**POSTGRES_PARAMS)
    cur = conn.cursor()

    # Use pgvector's <=> operator for cosine distance in SQL
    # Only fetch top 200 most similar passages (primary k-NN)
    sql = """
        SELECT pe.id, p.text,
               1 - (pe.embedding <=> %s::vector) AS similarity
        FROM passage_embedding pe
        JOIN passage p ON pe.passage_id = p.id
        ORDER BY pe.embedding <=> %s::vector ASC
        LIMIT 5
    """
    # JOIN document d ON p.document_id = d.id
    emb_str = '[' + ','.join(f'{x:.8f}' for x in query_emb) + ']'
    cur.execute(sql, (emb_str, emb_str))
    rows = cur.fetchall()
    if not rows:
        print("No passages found in the database.")
        sys.exit(0)

    print("\nTop 10 most similar passages (primary k-NN, k=200):")
    for i, (pe_id, text, sim) in enumerate(rows[:10]):
        print(f"\nRank {i+1} | Score: {sim:.4f}")
        print(f"Text: {text}")

    cur.close()
    conn.close()

if __name__ == "__main__":
    main() 