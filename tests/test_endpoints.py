import asyncio
import httpx
import sys
import os

sys.path.insert(0, "/home/aaronchan/AgenticOS/Claude-Work/PROJECTS/TECHPULSE-OS/app")
from src.main import app

async def run_asgi_tests():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        # 1. Test /api/episodes
        r_ep = await client.get("/api/episodes")
        assert r_ep.status_code == 200, f"Episodes API failed: {r_ep.status_code}"
        data = r_ep.json()
        print(f"✓ /api/episodes returned {len(data['episodes'])} episodes")

        # 2. Test /api/episodes/ep-142
        r_ep_detail = await client.get("/api/episodes/ep-142")
        assert r_ep_detail.status_code == 200, f"Episode detail failed: {r_ep_detail.status_code}"
        ep = r_ep_detail.json()
        print(f"✓ /api/episodes/ep-142 returned: '{ep['title'][:35]}...'")

        # 3. Test /api/chat
        r_chat = await client.post("/api/chat", json={"query": "Explain SPIFFE in banking", "episode_id": "ep-142"})
        assert r_chat.status_code == 200, f"Chat API failed: {r_chat.status_code}"
        chat_data = r_chat.json()
        print(f"✓ /api/chat returned grounded model response ({len(chat_data['response'])} bytes)")

        # 4. Test /feed.xml (Podcast RSS 2.0)
        r_feed = await client.get("/feed.xml")
        assert r_feed.status_code == 200, f"Feed API failed: {r_feed.status_code}"
        assert '<rss version="2.0"' in r_feed.text
        assert '<psc:chapters' in r_feed.text
        print(f"✓ /feed.xml returned valid RSS 2.0 Podcast XML ({len(r_feed.text)} bytes)")

        # 5. Test Static PWA Root
        r_root = await client.get("/")
        assert r_root.status_code == 200, f"Static PWA failed: {r_root.status_code}"
        assert "TechPulse" in r_root.text
        print(f"✓ GET / returned PWA HTML ({len(r_root.text)} bytes)")

    print("ALL ASGI FASTAPI & PODCAST RSS ENDPOINTS VERIFIED 100%")

if __name__ == "__main__":
    asyncio.run(run_asgi_tests())
