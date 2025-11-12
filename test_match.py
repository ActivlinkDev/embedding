from pymongo import MongoClient
from dotenv import load_dotenv
import os
import argparse
import json
from typing import Optional, List

# embed helper and similarity util
from utils.common import embed_query, cosine_similarity

# Load environment from .env and read MONGO_URI
load_dotenv()
MONGO_URI = os.getenv("MONGO_URI")

if not MONGO_URI:
    raise RuntimeError("MONGO_URI not set in environment or .env")


def run_vector_search(query: Optional[str] = None, *, query_vector: Optional[List[float]] = None, index: str = "vector_index", num_candidates: int = 100, limit: int = 3):
    """Run a $vectorSearch on the Category collection.

    Either provide `query` (text) which will be embedded via embed_query(), or
    provide `query_vector` (a list of floats) to run the search directly.
    """
    client = MongoClient(MONGO_URI)
    coll = client['Activlink']['Category']

    if query_vector is None:
        if query is None:
            raise ValueError("Either query (text) or query_vector must be provided")
        # embed the query text
        qvec = embed_query(query)
        try:
            query_vec = list(qvec)
        except Exception:
            raise RuntimeError("embed_query returned an unexpected value")
    else:
        # ensure it's a list of floats
        try:
            query_vec = [float(x) for x in query_vector]
        except Exception:
            raise RuntimeError("query_vector must be an iterable of numbers")

    stage = {
        "$vectorSearch": {
            "index": index,
            "path": "embedding",
            "queryVector": query_vec,
            "numCandidates": num_candidates,
            "limit": limit,
        }
    }

    results = []
    for doc in coll.aggregate([stage]):
        results.append(doc)

    # Build a compact summary: include the document (without its heavy embedding)
    # and the category field when available. Also compute or extract a score for
    # each hit and identify the best match.
    summaries = []
    best = None
    best_score = float('-inf')

    for doc in results:
        safe = dict(doc)
        # Extract score if present in common keys
        score = None
        for k in ('score', 'searchScore', 'vectorSearchScore', 'scoreValue', '_score'):
            if k in safe:
                try:
                    score = float(safe[k])
                except Exception:
                    score = None
                break

        # If no score was provided by the server, try computing cosine similarity
        # between the query vector and the stored embedding in the document.
        if score is None and 'embedding' in safe:
            try:
                score = float(cosine_similarity(query_vec, safe['embedding']))
            except Exception:
                score = None

        # Remove heavy embedding payload for output
        if isinstance(safe.get('embedding'), (list, tuple)):
            safe.pop('embedding', None)

        category_val = safe.get('category') or safe.get('Category') or safe.get('category_name')

        summaries.append({'category': category_val, 'score': score, 'document': safe})

        if score is not None and score > best_score:
            best_score = score
            best = summaries[-1]

    # Print compact summaries
    for s in summaries:
        print(json.dumps(s, default=str, indent=2))

    # Print best-match summary
    if best is not None:
        print('\nBest match:')
        print(json.dumps(best, default=str, indent=2))
    else:
        print('\nNo score available for matches; unable to determine best match.')

    return results


def parse_vector_text(s: str) -> List[float]:
    """Parse a vector given as JSON array or comma-separated numbers."""
    s = s.strip()
    if s.startswith("["):
        return list(json.loads(s))
    if "," in s:
        return [float(x) for x in s.split(",") if x.strip()]
    # single value
    return [float(s)]


if __name__ == '__main__':
    p = argparse.ArgumentParser(description="Run a MongoDB vectorSearch for a text query")
    p.add_argument('--query', '-q', type=str, help='Text query to embed and search')
    p.add_argument('--vector', '-v', type=str, help='JSON array or comma-separated vector to search with (bypasses embedding)')
    p.add_argument('--index', type=str, default=os.getenv('VECTOR_INDEX', 'vector_index'), help='Vector index name')
    p.add_argument('--numCandidates', type=int, default=int(os.getenv('VECTOR_NUM_CANDIDATES', '100')))
    p.add_argument('--limit', type=int, default=int(os.getenv('VECTOR_LIMIT', '3')))
    args = p.parse_args()

    try:
        if args.vector:
            vec = parse_vector_text(args.vector)
            run_vector_search(query_vector=vec, index=args.index, num_candidates=args.numCandidates, limit=args.limit)
        else:
            if not args.query:
                raise SystemExit("Provide --query text or --vector numeric vector")
            run_vector_search(args.query, index=args.index, num_candidates=args.numCandidates, limit=args.limit)
    except Exception as e:
        print('Error running vector search:', e)
        raise

