import os
import re
import json
import time
import threading
import requests
from flask import Flask, request, jsonify
from bs4 import BeautifulSoup
from urllib.parse import quote

app = Flask(__name__)

DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "")
PORT = int(os.environ.get("PORT", 5000))

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
})

ELDORADO_SEARCH_URL = "https://www.eldorado.gg/steal-a-brainrot/items"

def normalize(name: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", name.lower()).strip()

def similarity_score(query: str, title: str) -> float:
    q_words = set(normalize(query).split())
    t_words = set(normalize(title).split())
    if not q_words:
        return 0.0
    return len(q_words & t_words) / len(q_words)

def search_eldorado(pet_name: str) -> dict | None:
    query = normalize(pet_name)
    search_url = f"{ELDORADO_SEARCH_URL}?search={quote(pet_name)}"

    try:
        resp = SESSION.get(search_url, timeout=12)
        if resp.status_code != 200:
            return None

        soup = BeautifulSoup(resp.text, "html.parser")

        listings = []

        cards = soup.select("[data-testid='offer-card'], .offer-card, article[class*='offer'], div[class*='OfferCard'], div[class*='offer-item']")

        if not cards:
            cards = soup.find_all("div", class_=re.compile(r"offer|listing|card|item", re.I))

        for card in cards:
            title_el = card.find(["h2", "h3", "h4", "span", "p"], class_=re.compile(r"title|name|offer", re.I))
            price_el = card.find(["span", "div", "p"], class_=re.compile(r"price|cost|amount", re.I))
            link_el = card.find("a", href=True)

            if not title_el or not price_el:
                continue

            title_text = title_el.get_text(strip=True)
            price_text = price_el.get_text(strip=True)
            href = link_el["href"] if link_el else ""

            price_match = re.search(r"[\$€£]?\s*([\d,\.]+)", price_text)
            if not price_match:
                continue

            try:
                price_val = float(price_match.group(1).replace(",", ""))
            except ValueError:
                continue

            score = similarity_score(pet_name, title_text)
            if score < 0.4:
                continue

            listings.append({
                "title": title_text,
                "price": price_val,
                "price_text": price_text,
                "url": f"https://www.eldorado.gg{href}" if href.startswith("/") else href,
                "score": score,
            })

        if not listings:
            return None

        listings.sort(key=lambda x: (x["price"], -x["score"]))
        return listings[0]

    except Exception as e:
        print(f"[ELDORADO] Error searching '{pet_name}': {e}")
        return None

def post_to_discord(content: str):
    if not DISCORD_WEBHOOK:
        print("[DISCORD] No webhook configured.")
        return

    payload = {
        "content": content,
        "username": "Brainrot Price Checker",
        "avatar_url": "https://i.imgur.com/example.png",
    }

    try:
        r = requests.post(DISCORD_WEBHOOK, json=payload, timeout=10)
        if r.status_code in (200, 204):
            print(f"[DISCORD] Posted successfully.")
        else:
            print(f"[DISCORD] Failed: {r.status_code} {r.text}")
    except Exception as e:
        print(f"[DISCORD] Exception: {e}")

def process_pets_background(pet_list: list):
    results = []

    unique_names = []
    seen = set()
    for pet in pet_list:
        name = pet.get("name", "").strip()
        if name and name not in seen:
            seen.add(name)
            unique_names.append((name, pet))

    top = unique_names[:10]

    for name, pet_data in top:
        print(f"[SEARCH] Looking up: {name}")
        result = search_eldorado(name)

        gen_text = pet_data.get("genText", "?")
        mutation = pet_data.get("mutation", "None")
        traits = pet_data.get("traits", "")

        if result:
            results.append({
                "name": name,
                "gen": gen_text,
                "mutation": mutation,
                "traits": traits,
                "price": result["price_text"],
                "listing_title": result["title"],
                "url": result["url"],
            })
        else:
            results.append({
                "name": name,
                "gen": gen_text,
                "mutation": mutation,
                "traits": traits,
                "price": "Not found",
                "listing_title": None,
                "url": None,
            })

        time.sleep(1.2)

    if not results:
        post_to_discord("**Brainrot Price Check:** No results found on Eldorado.")
        return

    lines = ["**Brainrot Eldorado Price Check (Top 10)**", "```"]
    for i, r in enumerate(results, 1):
        mut_str = f" [{r['mutation']}]" if r["mutation"] != "None" else ""
        trait_str = f" {{{r['traits']}}}" if r["traits"] else ""
        price_str = r["price"]
        if r["url"]:
            lines.append(f"#{i:<2} {r['name']}{mut_str}{trait_str}")
            lines.append(f"    Gen: {r['gen']}  |  Cheapest: {price_str}")
            lines.append(f"    {r['url']}")
        else:
            lines.append(f"#{i:<2} {r['name']}{mut_str}{trait_str}")
            lines.append(f"    Gen: {r['gen']}  |  Not listed on Eldorado")
        lines.append("")

    lines.append("```")
    post_to_discord("\n".join(lines))


@app.route("/", methods=["GET"])
def index():
    return jsonify({"status": "Brainrot Eldorado Middleware running"}), 200


@app.route("/scan", methods=["POST"])
def scan():
    try:
        data = request.get_json(force=True)
    except Exception:
        return jsonify({"error": "Invalid JSON"}), 400

    if not data or "pets" not in data:
        return jsonify({"error": "Missing 'pets' key"}), 400

    pet_list = data["pets"]
    if not isinstance(pet_list, list) or len(pet_list) == 0:
        return jsonify({"error": "Empty or invalid pet list"}), 400

    print(f"[SCAN] Received {len(pet_list)} pets. Processing in background...")
    thread = threading.Thread(target=process_pets_background, args=(pet_list,), daemon=True)
    thread.start()

    return jsonify({"status": "processing", "count": len(pet_list)}), 202


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
