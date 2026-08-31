import os
import re
import json
import time
import threading
import requests
import urllib3
from flask import Flask, request, jsonify, render_template
from bs4 import BeautifulSoup
from urllib.parse import quote

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)

DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "")
PORT = int(os.environ.get("PORT", 5000))

SCRAPER_API_KEY = os.environ.get("SCRAPER_API_KEY", "77d634d4d1b8cdc9368131a85957809b")
ELDORADO_SEARCH_URL = "https://www.eldorado.gg/steal-a-brainrot/items"

PRICE_DB_FILE = "price_db.json"
price_db_lock = threading.Lock()


def load_price_db():
    if not os.path.exists(PRICE_DB_FILE):
        return {}
    try:
        with open(PRICE_DB_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def save_price_db(db):
    with open(PRICE_DB_FILE, "w") as f:
        json.dump(db, f, indent=2)


def normalize_name(name: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", name.lower()).strip()


def get_cached_price(name: str):
    with price_db_lock:
        db = load_price_db()
    key = normalize_name(name)
    if key not in db:
        for k in db:
            if k in key or key in k:
                entry = db[k]
                age = time.time() - entry.get("timestamp", 0)
                if age < 86400 * 3:
                    return entry
        return None
    entry = db[key]
    age = time.time() - entry.get("timestamp", 0)
    if age > 86400 * 3:
        return None
    return entry


def scraper_get(url: str, timeout: int = 30):
    params = {
        "api_key": SCRAPER_API_KEY,
        "url": url,
        "render": "true",
        "country_code": "us",
    }
    return requests.get("https://api.scraperapi.com/", params=params, timeout=timeout)

state = {
    "pets": [],
    "prices": {},
    "status": "idle",
    "timestamp": None,
    "trigger_pending": False,
}
state_lock = threading.Lock()


def normalize(name: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", name.lower()).strip()


def similarity_score(query: str, title: str) -> float:
    q_words = set(normalize(query).split())
    t_words = set(normalize(title).split())
    if not q_words:
        return 0.0
    return len(q_words & t_words) / len(q_words)


def make_eldorado_slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9 ]", "", name.lower()).strip()
    slug = re.sub(r"\s+", "-", slug)
    return f"{slug}-steal-a-brainrot"


def search_eldorado(pet_name: str) -> dict | None:
    cached = get_cached_price(pet_name)
    if cached:
        print(f"[CACHE] Hit for '{pet_name}': {cached['price_text']}")
        return cached

    slug = make_eldorado_slug(pet_name)
    item_url = f"https://www.eldorado.gg/{slug}/i/259"
    search_url = f"{ELDORADO_SEARCH_URL}?search={quote(pet_name)}"

    for url in [item_url, search_url]:
        try:
            print(f"[ELDORADO] Trying: {url}")
            resp = scraper_get(url, timeout=30)
            print(f"[ELDORADO] Status: {resp.status_code} len={len(resp.text)}")
            if resp.status_code != 200:
                continue

            soup = BeautifulSoup(resp.text, "html.parser")
            listings = []

            price_els = soup.find_all(string=re.compile(r'\$[\d,\.]+'))
            for el in price_els:
                price_match = re.search(r'\$([\d,\.]+)', el)
                if not price_match:
                    continue
                try:
                    price_val = float(price_match.group(1).replace(",", ""))
                except ValueError:
                    continue
                if price_val < 0.01 or price_val > 10000:
                    continue

                parent = el.parent
                link = None
                for _ in range(6):
                    if parent is None:
                        break
                    a = parent.find("a", href=True)
                    if a:
                        link = a["href"]
                        break
                    parent = parent.parent

                href = link or f"/{slug}/i/259"
                full_url = f"https://www.eldorado.gg{href}" if href.startswith("/") else href

                listings.append({
                    "price": price_val,
                    "price_text": f"${price_val:.2f}",
                    "url": full_url,
                    "score": 1.0,
                })

            if listings:
                listings.sort(key=lambda x: x["price"])
                result = listings[0]
                result["url"] = item_url
                return result

        except Exception as e:
            print(f"[ELDORADO] Error on '{url}': {e}")
            continue

    return None


def post_to_discord(content: str):
    if not DISCORD_WEBHOOK:
        return
    payload = {
        "content": content,
        "username": "Brainrot Angel",
        "avatar_url": "https://i.imgur.com/example.png",
    }
    try:
        r = requests.post(DISCORD_WEBHOOK, json=payload, timeout=10)
        print(f"[DISCORD] Status: {r.status_code}")
    except Exception as e:
        print(f"[DISCORD] Exception: {e}")


def process_pets_background(pet_list: list):
    with state_lock:
        state["status"] = "processing"
        state["prices"] = {}

    unique_names = []
    seen = set()
    for pet in pet_list:
        name = pet.get("name", "").strip()
        if name and name not in seen:
            seen.add(name)
            unique_names.append((name, pet))

    top = unique_names[:10]
    results = []

    for name, pet_data in top:
        print(f"[SEARCH] {name}")
        result = search_eldorado(name)
        gen_text = pet_data.get("genText", "?")
        mutation = pet_data.get("mutation", "None")
        traits = pet_data.get("traits", "")

        price_entry = {
            "price": result["price_text"] if result else "Not found",
            "url": result["url"] if result else None,
        }

        with state_lock:
            state["prices"][name] = price_entry

        results.append({
            "name": name,
            "gen": gen_text,
            "mutation": mutation,
            "traits": traits,
            **price_entry,
        })

        time.sleep(1.2)

    with state_lock:
        state["status"] = "done"

    if not results:
        post_to_discord("**Brainrot Price Check:** No results found on Eldorado.")
        return

    lines = ["**Brainrot Eldorado Price Check (Top 10)**", "```"]
    for i, r in enumerate(results, 1):
        mut_str = f" [{r['mutation']}]" if r["mutation"] != "None" else ""
        trait_str = f" {{{r['traits']}}}" if r["traits"] else ""
        if r["url"]:
            lines.append(f"#{i:<2} {r['name']}{mut_str}{trait_str}")
            lines.append(f"    Gen: {r['gen']}  |  Cheapest: {r['price']}")
            lines.append(f"    {r['url']}")
        else:
            lines.append(f"#{i:<2} {r['name']}{mut_str}{trait_str}")
            lines.append(f"    Gen: {r['gen']}  |  Not listed on Eldorado")
        lines.append("")
    lines.append("```")
    post_to_discord("\n".join(lines))


@app.route("/", methods=["GET"])
def index():
    return render_template("dashboard.html")


@app.route("/results", methods=["GET"])
def results():
    with state_lock:
        return jsonify({
            "pets": state["pets"],
            "prices": state["prices"],
            "status": state["status"],
            "timestamp": state["timestamp"],
        })


@app.route("/trigger", methods=["POST"])
def trigger():
    with state_lock:
        state["trigger_pending"] = True
    return jsonify({"status": "trigger set"}), 200


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

    with state_lock:
        state["pets"] = pet_list
        state["timestamp"] = time.time()
        state["status"] = "processing"
        state["prices"] = {}
        state["trigger_pending"] = False

    post_to_discord(
        "**Top 25 Brainrot Pets (Server Scan):**\n```\n" +
        "\n".join(
            f"#{i+1:<2} {p.get('name','?')} ({p.get('genText','?')}) [{p.get('mutation','None')}] {{{p.get('traits','')}}}"
            for i, p in enumerate(pet_list[:25])
        ) + "\n```"
    )

    thread = threading.Thread(target=process_pets_background, args=(pet_list,), daemon=True)
    thread.start()

    print(f"[SCAN] Received {len(pet_list)} pets. Processing...")
    return jsonify({"status": "processing", "count": len(pet_list)}), 202


@app.route("/lookup", methods=["POST"])
def lookup():
    try:
        data = request.get_json(force=True)
    except Exception:
        return jsonify({"error": "Invalid JSON"}), 400

    name = (data or {}).get("name", "").strip()
    if not name:
        return jsonify({"error": "Missing name"}), 400

    result = search_eldorado(name)
    if result:
        return jsonify({"price": result["price_text"], "url": result["url"]})
    return jsonify({"price": "Not found", "url": None})


@app.route("/submit-price", methods=["POST"])
def submit_price():
    try:
        data = request.get_json(force=True)
    except Exception:
        return jsonify({"error": "Invalid JSON"}), 400

    entries = (data or {}).get("prices", [])
    if not entries:
        single_name = (data or {}).get("name", "").strip()
        single_price = (data or {}).get("price")
        single_url = (data or {}).get("url", "")
        if single_name and single_price:
            entries = [{"name": single_name, "price": single_price, "price_text": f"${float(single_price):.2f}", "url": single_url}]

    if not entries:
        return jsonify({"error": "No price entries"}), 400

    saved = 0
    with price_db_lock:
        db = load_price_db()
        for entry in entries:
            name = entry.get("name", "").strip()
            price = entry.get("price")
            if not name or price is None:
                continue
            try:
                price_val = float(price)
            except (ValueError, TypeError):
                continue
            if price_val < 0.01 or price_val > 100000:
                continue

            key = normalize_name(name)
            existing = db.get(key)
            if existing:
                existing_price = existing.get("price", float("inf"))
                if price_val < existing_price:
                    db[key] = {
                        "price": price_val,
                        "price_text": entry.get("price_text", f"${price_val:.2f}"),
                        "url": entry.get("url", ""),
                        "timestamp": time.time(),
                        "source": "userscript",
                    }
                    saved += 1
            else:
                db[key] = {
                    "price": price_val,
                    "price_text": entry.get("price_text", f"${price_val:.2f}"),
                    "url": entry.get("url", ""),
                    "timestamp": time.time(),
                    "source": "userscript",
                }
                saved += 1

        save_price_db(db)

    print(f"[SUBMIT] Saved {saved}/{len(entries)} prices from userscript")
    return jsonify({"saved": saved, "total": len(entries)}), 200


@app.route("/prices", methods=["GET"])
def list_prices():
    with price_db_lock:
        db = load_price_db()
    return jsonify({
        "count": len(db),
        "prices": {k: v for k, v in sorted(db.items(), key=lambda x: x[1].get("price", 0))}
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
