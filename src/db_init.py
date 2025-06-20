import os
import psycopg2
from psycopg2 import sql
from dotenv import load_dotenv

# Load environment variables from .env
# Required: POSTGRES_HOST, POSTGRES_PORT, POSTGRES_DB, POSTGRES_USER, POSTGRES_PASSWORD
load_dotenv()

def main():
    print("Connecting to Postgres...")
    conn = psycopg2.connect(
        host=os.getenv('POSTGRES_HOST', 'localhost'),
        port=os.getenv('POSTGRES_PORT', 5432),
        dbname=os.getenv('POSTGRES_DB', 'postgres'),
        user=os.getenv('POSTGRES_USER', 'postgres'),
        password=os.getenv('POSTGRES_PASSWORD', '')
    )
    conn.autocommit = True
    cur = conn.cursor()

    print("Enabling pgvector extension...")
    cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")

    print("Dropping existing tables (if any)...")
    drop_order = [
        'document_party', 'contact_embedding', 'contact_alias', 'contact',
        'sentence_embedding', 'sentence',
        'passage_embedding', 'passage',
        'document_embedding', 'attachment', 'document', 'source'
    ]
    for table in drop_order:
        cur.execute(sql.SQL("DROP TABLE IF EXISTS {} CASCADE;").format(sql.Identifier(table)))

    print("Creating tables...")
    # 1. Source
    cur.execute("""
    CREATE TABLE source (
        id          BIGSERIAL PRIMARY KEY,
        name        TEXT UNIQUE,
        kind        TEXT     NOT NULL,
        details     JSONB
    );
    """)
    # 2. Contact
    cur.execute("""
    CREATE TABLE contact (
        id            BIGSERIAL PRIMARY KEY,
        canonical     TEXT,
        notes         TEXT,
        first_seen    TIMESTAMPTZ,
        last_seen     TIMESTAMPTZ,
        trust_level   SMALLINT DEFAULT 0,
        meta          JSONB
    );
    """)
    # 3. Contact Alias
    cur.execute("""
    CREATE TABLE contact_alias (
        id            BIGSERIAL PRIMARY KEY,
        contact_id    BIGINT REFERENCES contact(id) ON DELETE CASCADE,
        kind          TEXT NOT NULL,
        value         TEXT NOT NULL,
        is_primary    BOOLEAN DEFAULT FALSE,
        UNIQUE(kind, value)
    );
    """)
    # 4. Contact Embedding
    cur.execute("""
    CREATE TABLE contact_embedding (
        id            BIGSERIAL PRIMARY KEY,
        alias_id      BIGINT REFERENCES contact_alias(id) ON DELETE CASCADE,
        model_id      TEXT NOT NULL,
        embedding     VECTOR(1024) NOT NULL
    );
    """)
    # 5. Document
    cur.execute("""
    CREATE TABLE document (
        id              BIGSERIAL PRIMARY KEY,
        source_id       BIGINT  REFERENCES source(id) ON DELETE CASCADE,
        ext_id          TEXT    NOT NULL,
        thread_id       TEXT,
        sent_ts         TIMESTAMPTZ NOT NULL,
        author_contact_id BIGINT REFERENCES contact(id),
        subject         TEXT,
        raw_body        TEXT,
        clean_body      TEXT,
        metadata        JSONB,
        UNIQUE(source_id, ext_id)
    );
    """)
    # 6. Attachment
    cur.execute("""
    CREATE TABLE attachment (
        id              BIGSERIAL PRIMARY KEY,
        document_id     BIGINT REFERENCES document(id) ON DELETE CASCADE,
        filename        TEXT,
        mime_type       TEXT,
        file_path       TEXT,
        meta            JSONB
    );
    """)
    # 7. Passage
    cur.execute("""
    CREATE TABLE passage (
        id              BIGSERIAL PRIMARY KEY,
        document_id     BIGINT REFERENCES document(id) ON DELETE CASCADE,
        level           CHAR(1)  NOT NULL CHECK(level IN ('P','S')),
        start_tok       INT     NOT NULL,
        end_tok         INT     NOT NULL,
        text            TEXT    NOT NULL
    );
    """)
    # 8. Passage Embedding
    cur.execute("""
    CREATE TABLE passage_embedding (
        id              BIGSERIAL PRIMARY KEY,
        passage_id      BIGINT REFERENCES passage(id) ON DELETE CASCADE,
        model_id        TEXT    NOT NULL,
        embedding       VECTOR(1024) NOT NULL
    );
    """)
    # 9. Document Embedding
    cur.execute("""
    CREATE TABLE document_embedding (
        id              BIGSERIAL PRIMARY KEY,
        document_id     BIGINT REFERENCES document(id) ON DELETE CASCADE,
        model_id        TEXT    NOT NULL,
        embedding       VECTOR(1024) NOT NULL
    );
    """)
    # 10. Sentence
    cur.execute("""
    CREATE TABLE sentence (
        id              BIGSERIAL PRIMARY KEY,
        passage_id      BIGINT REFERENCES passage(id) ON DELETE CASCADE,
        start_tok       INT     NOT NULL,
        end_tok         INT     NOT NULL,
        text            TEXT    NOT NULL
    );
    """)
    # 11. Sentence Embedding
    cur.execute("""
    CREATE TABLE sentence_embedding (
        id              BIGSERIAL PRIMARY KEY,
        sentence_id     BIGINT REFERENCES sentence(id) ON DELETE CASCADE,
        model_id        TEXT    NOT NULL,
        embedding       VECTOR(1024) NOT NULL
    );
    """)
    # 12. Document Party
    cur.execute("""
    CREATE TABLE document_party (
        id            BIGSERIAL PRIMARY KEY,
        document_id   BIGINT REFERENCES document(id) ON DELETE CASCADE,
        contact_id    BIGINT REFERENCES contact(id),
        alias_id      BIGINT REFERENCES contact_alias(id),
        role          TEXT NOT NULL,
        display_order SMALLINT
    );
    """)

    print("Creating indexes...")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_passage_embedding_vector ON passage_embedding USING hnsw (embedding vector_cosine_ops);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_contact_embedding_vector ON contact_embedding USING hnsw (embedding vector_cosine_ops);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_document_sent_ts ON document(sent_ts);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_document_thread_id ON document(thread_id);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_document_author ON document(author_contact_id);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_document_party_contact ON document_party(contact_id);")

    print("Database initialized successfully.")
    cur.close()
    conn.close()

if __name__ == "__main__":
    main() 