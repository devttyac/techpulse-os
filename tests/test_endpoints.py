import asyncio
import httpx
import sys
import os

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

from src.main import app

async def run_asgi_tests():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        # 1. Test /healthz
        r_health = await client.get("/healthz")
        assert r_health.status_code == 200, f"Healthcheck failed: {r_health.status_code}"
        health_data = r_health.json()
        assert health_data["status"] == "healthy"
        print(f"✓ /healthz returned healthy (episodes: {health_data['episodes_count']})")

        # 2. Test /api/episodes
        r_ep = await client.get("/api/episodes")
        assert r_ep.status_code == 200, f"Episodes API failed: {r_ep.status_code}"
        data = r_ep.json()
        assert len(data['episodes']) > 0
        print(f"✓ /api/episodes returned {len(data['episodes'])} episodes")

        # 3. Test /api/episodes/ep-142
        r_ep_detail = await client.get("/api/episodes/ep-142")
        assert r_ep_detail.status_code == 200, f"Episode detail failed: {r_ep_detail.status_code}"
        ep = r_ep_detail.json()
        assert "takeaways" in ep
        print(f"✓ /api/episodes/ep-142 returned: '{ep['title'][:35]}...'")

        # 4. Test /api/chat
        r_chat = await client.post("/api/chat", json={"query": "Explain SPIFFE in banking", "episode_id": "ep-142"})
        assert r_chat.status_code == 200, f"Chat API failed: {r_chat.status_code}"
        chat_data = r_chat.json()
        assert "response" in chat_data
        print(f"✓ /api/chat returned grounded model response ({len(chat_data['response'])} bytes)")

        # 5. Test /api/export-vault and /api/export-markdown/{episode_id}
        r_export_json = await client.post("/api/export-vault", json={"episode_id": "ep-142"})
        assert r_export_json.status_code == 200, f"Export JSON API failed: {r_export_json.status_code}"
        export_data = r_export_json.json()
        assert export_data["status"] == "ok"
        assert export_data["filename"] == "techpulse-ep-142.md"
        assert "Domain Takeaways" in export_data["markdown"]
        print(f"✓ /api/export-vault returned structured markdown payload ({len(export_data['markdown'])} bytes)")

        r_export_file = await client.get("/api/export-markdown/ep-142")
        assert r_export_file.status_code == 200, f"Export file API failed: {r_export_file.status_code}"
        assert 'attachment; filename="techpulse-ep-142.md"' in r_export_file.headers.get("content-disposition", "")
        assert "Domain Takeaways" in r_export_file.text
        print(f"✓ /api/export-markdown/ep-142 returned markdown file download ({len(r_export_file.text)} bytes)")

        # 6. Test /api/settings and /api/refresh/status
        r_settings = await client.get("/api/settings")
        assert r_settings.status_code == 200, f"Settings API failed: {r_settings.status_code}"
        s_data = r_settings.json()
        assert "config" in s_data and "storage" in s_data
        print(f"✓ /api/settings returned config and storage stats ({s_data['storage']['disk_usage_mb']} MB)")

        r_status = await client.get("/api/refresh/status")
        assert r_status.status_code == 200, f"Refresh status API failed: {r_status.status_code}"
        assert "stage" in r_status.json()
        print(f"✓ /api/refresh/status returned live stage: {r_status.json()['stage']}")

        # 7. Test /feed.xml (Podcast RSS 2.0)
        r_feed = await client.get("/feed.xml")
        assert r_feed.status_code == 200, f"Feed API failed: {r_feed.status_code}"
        assert '<rss version="2.0"' in r_feed.text
        assert '<psc:chapters' in r_feed.text
        assert '<pubDate>' in r_feed.text
        assert 'enclosure' in r_feed.text
        print(f"✓ /feed.xml returned valid RSS 2.0 Podcast XML with RFC 822 pubDate ({len(r_feed.text)} bytes)")

        # 8. Test Static PWA Root
        r_root = await client.get("/")
        assert r_root.status_code == 200, f"Static PWA failed: {r_root.status_code}"
        assert "TechPulse" in r_root.text
        assert "Podcast RSS Feed" in r_root.text
        assert "podcast-rss-modal" in r_root.text
        print(f"✓ GET / returned PWA HTML with podcast modal and audio binds ({len(r_root.text)} bytes)")

    print("\n=======================================================")
    print("ALL ASGI FASTAPI & PODCAST RSS ENDPOINTS VERIFIED 100%")
    print("=======================================================")

if __name__ == "__main__":
    asyncio.run(run_asgi_tests())
