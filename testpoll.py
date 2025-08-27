import asyncio
from routers.email_injest import poll_imap

if __name__ == "__main__":
    print("Running poll_imap once…")
    try:
        res = asyncio.run(poll_imap(limit=1))
        print("poll_imap finished:", res)
    except Exception as e:
        print("poll_imap error:", e)
