#!/usr/bin/env python3
"""
Ensure the MongoDB collection 'history' exists in the 'sefaria' database.

"""
import os
from pymongo import MongoClient, errors


def main() -> None:
    host = os.environ.get("MONGO_HOST", "127.0.0.1")
    port = int(os.environ.get("MONGO_PORT", "27017"))
    db_name = os.environ.get("MONGO_DB_NAME", "sefaria")

    client = MongoClient(host=host, port=port)
    db = client[db_name]
    try:
        db.create_collection("history")
        print("Created empty 'history' collection.")
    except errors.CollectionInvalid:
        print("'history' collection already exists.")


if __name__ == "__main__":
    main()
