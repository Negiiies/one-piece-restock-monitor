#!/usr/bin/env python3
"""
Moniteur de restocks / nouveautes pour une collection Shopify (ludisphere.fr).

Recupere /collections/<collection>/products.json, compare a l'etat precedent
(state.json) et envoie une alerte Telegram pour :
  - NOUVEAUTE : un produit jamais vu dont created_at est recent
  - RESTOCK   : une variante qui passe de indisponible -> disponible

Concu pour tourner sur GitHub Actions en boucle interne (voir .github/workflows/monitor.yml).
Toute la configuration sensible passe par des variables d'environnement (secrets GitHub).
"""

import json
import os
import sys
import time
import urllib.parse
from datetime import datetime, timezone, timedelta

import requests

# --------------------------------------------------------------------------- #
# Configuration (via variables d'environnement / secrets GitHub)
# --------------------------------------------------------------------------- #
BASE_URL = os.environ.get("SHOP_BASE_URL", "https://ludisphere.fr")
COLLECTION = os.environ.get(
    "SHOP_COLLECTION", "one-piece-card-game-precommande"
)
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

# Comportement
STATE_FILE = os.environ.get("STATE_FILE", "state.json")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "60"))          # secondes entre 2 checks
LOOP_DURATION = int(os.environ.get("LOOP_DURATION", "240"))         # duree totale de la boucle (s)
NEW_WINDOW_HOURS = int(os.environ.get("NEW_WINDOW_HOURS", "72"))    # un produit "recent" = cree il y a moins de X h
HEALTHCHECK_URL = os.environ.get("HEALTHCHECK_URL", "").strip()     # ping dead-man's switch (optionnel)
SEND_STARTUP_MESSAGE = os.environ.get("SEND_STARTUP_MESSAGE", "1") == "1"

USER_AGENT = os.environ.get(
    "USER_AGENT",
    "OnePieceRestockMonitor/1.0 (+https://github.com; contact via Telegram)",
)

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"


# --------------------------------------------------------------------------- #
# Etat persistant
# --------------------------------------------------------------------------- #
def load_state():
    if not os.path.exists(STATE_FILE):
        return {"variants": {}, "seen_product_ids": [], "seeded": False}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        # state corrompu : on repart d'un seed silencieux plutot que de spammer
        return {"variants": {}, "seen_product_ids": [], "seeded": False}
    data.setdefault("variants", {})
    data.setdefault("seen_product_ids", [])
    data.setdefault("seeded", False)
    return data


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)  # ecriture atomique


# --------------------------------------------------------------------------- #
# Recuperation des produits (endpoint JSON Shopify, stable)
# --------------------------------------------------------------------------- #
def fetch_all_products(session):
    """Renvoie la liste complete des produits, toutes pages confondues.

    Leve une exception si le reseau echoue : l'appelant decide quoi faire
    (ne JAMAIS interpreter un echec comme 'tout est en rupture').
    """
    products = []
    page = 1
    while True:
        url = "{base}/collections/{col}/products.json?limit=250&page={page}".format(
            base=BASE_URL, col=COLLECTION, page=page
        )
        resp = session.get(url, timeout=20)

        # Respect du rate-limit Shopify
        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", "10"))
            print("[warn] 429 recu, pause {}s".format(retry_after), flush=True)
            time.sleep(retry_after)
            continue

        resp.raise_for_status()
        batch = resp.json().get("products", [])
        if not batch:
            break
        products.extend(batch)
        page += 1
        if page > 20:  # garde-fou : jamais plus de 20 pages
            break
        time.sleep(1)  # politesse entre les pages
    return products


def is_recent(created_at_str):
    """True si le produit a ete cree il y a moins de NEW_WINDOW_HOURS heures."""
    if not created_at_str:
        return False
    try:
        # Shopify : "2026-06-09T18:05:04+02:00"
        created = datetime.fromisoformat(created_at_str)
    except ValueError:
        return False
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - created < timedelta(hours=NEW_WINDOW_HOURS)


# --------------------------------------------------------------------------- #
# Telegram
# --------------------------------------------------------------------------- #
def _telegram_call(method, payload):
    """Appel generique a l'API Telegram avec retries et respect du 429."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[error] TELEGRAM_TOKEN ou TELEGRAM_CHAT_ID manquant", flush=True)
        return False
    url = TELEGRAM_API.format(token=TELEGRAM_TOKEN, method=method)
    payload = dict(payload, chat_id=TELEGRAM_CHAT_ID, parse_mode="HTML")
    for attempt in range(3):
        try:
            r = requests.post(url, data=payload, timeout=20)
            if r.status_code == 429:
                retry_after = int(r.json().get("parameters", {}).get("retry_after", 5))
                time.sleep(retry_after)
                continue
            r.raise_for_status()
            return True
        except requests.RequestException as exc:
            print("[warn] appel Telegram {} echoue (essai {}): {}".format(method, attempt + 1, exc), flush=True)
            time.sleep(2 ** attempt)
    return False


def send_telegram(text):
    """Message texte simple (utilise pour le message de demarrage)."""
    return _telegram_call("sendMessage", {"text": text, "disable_web_page_preview": False})


def send_alert(caption, photo_url=None):
    """Envoie une alerte : avec photo (sendPhoto) si disponible, sinon texte.

    Si l'envoi avec photo echoue (URL invalide cote Telegram), on retombe
    automatiquement sur un message texte pour ne jamais perdre l'alerte.
    """
    if photo_url:
        if _telegram_call("sendPhoto", {"photo": photo_url, "caption": caption}):
            return True
        print("[warn] sendPhoto echoue, repli sur message texte", flush=True)
    return _telegram_call("sendMessage", {"text": caption, "disable_web_page_preview": False})


def product_url(handle):
    return "{base}/products/{handle}".format(base=BASE_URL, handle=handle)


def main_image(product):
    """URL de la photo principale du produit, ou None s'il n'y en a pas."""
    images = product.get("images") or []
    if images and images[0].get("src"):
        return images[0]["src"]
    return None


def format_alert(kind, product, variant, price):
    title = product.get("title", "Produit")
    handle = product.get("handle", "")
    url = product_url(handle)
    vtitle = variant.get("title", "")
    vlabel = "" if vtitle in ("", "Default Title") else " — {}".format(vtitle)
    tag = "🆕 <b>NOUVEAUTÉ</b>" if kind == "new" else "🔁 <b>RETOUR EN STOCK</b>"
    price_line = "💶 {} €".format(price) if price else ""
    return (
        "{tag}\n"
        "🏴‍☠️ <b>{title}</b>{vlabel}\n"
        "{price}\n"
        "🔗 {url}"
    ).format(tag=tag, title=title, vlabel=vlabel, price=price_line, url=url)


# --------------------------------------------------------------------------- #
# Un cycle de verification
# --------------------------------------------------------------------------- #
def check_once(session, state):
    """Effectue un cycle : fetch -> diff -> alertes. Met state a jour en place.

    Renvoie le nombre d'alertes envoyees, ou -1 en cas d'erreur (etat non modifie).
    """
    try:
        products = fetch_all_products(session)
    except requests.RequestException as exc:
        print("[error] fetch echoue : {}".format(exc), flush=True)
        return -1  # erreur reseau : on NE touche PAS l'etat

    # Sanity-check : 0 produit alors qu'on en avait avant => page cassee / ban, PAS "tout en rupture"
    if not products and state["variants"]:
        print("[error] 0 produit recu alors que l'etat en contenait : anomalie, etat preserve", flush=True)
        return -1

    seen_pids = set(state["seen_product_ids"])
    variants_state = state["variants"]
    first_run = not state.get("seeded", False)
    alerts = 0

    for product in products:
        pid = product.get("id")
        is_new_product = pid not in seen_pids
        seen_pids.add(pid)

        for variant in product.get("variants", []):
            vid = str(variant.get("id"))
            available = bool(variant.get("available"))
            price = variant.get("price")
            prev = variants_state.get(vid)

            # On enregistre toujours le nouvel etat de la variante
            new_entry = {
                "available": available,
                "price": price,
                "title": product.get("title"),
                "handle": product.get("handle"),
                "product_id": pid,
            }

            if first_run:
                # Seed silencieux : on memorise sans alerter
                variants_state[vid] = new_entry
                continue

            # NOUVEAUTE : produit jamais vu + recent + dispo (sinon on attend le restock)
            if is_new_product and is_recent(product.get("created_at")) and available:
                if send_alert(format_alert("new", product, variant, price), main_image(product)):
                    alerts += 1
                    time.sleep(1)  # eviter le flood Telegram
            # RESTOCK : transition indisponible -> disponible
            elif prev is not None and prev.get("available") is False and available is True:
                if send_alert(format_alert("restock", product, variant, price), main_image(product)):
                    alerts += 1
                    time.sleep(1)

            variants_state[vid] = new_entry

    state["seen_product_ids"] = sorted(seen_pids)
    state["seeded"] = True

    if first_run:
        print("[info] seed initial : {} variantes memorisees, aucune alerte envoyee".format(
            len(variants_state)), flush=True)
    return alerts


def ping_healthcheck():
    if not HEALTHCHECK_URL:
        return
    try:
        requests.get(HEALTHCHECK_URL, timeout=10)
    except requests.RequestException:
        pass


# --------------------------------------------------------------------------- #
# Boucle principale
# --------------------------------------------------------------------------- #
def main():
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})

    state = load_state()
    was_seeded = state.get("seeded", False)

    if not was_seeded and SEND_STARTUP_MESSAGE:
        send_telegram(
            "✅ <b>Moniteur One Piece démarré</b>\n"
            "Je surveille la page de précommande Ludisphère.\n"
            "Tu recevras les 🆕 nouveautés et 🔁 retours en stock ici."
        )

    start = time.time()
    total_alerts = 0
    cycles = 0

    # Boucle interne : poll toutes les POLL_INTERVAL s pendant LOOP_DURATION s,
    # puis on sort (le cron GitHub Actions relancera un nouveau run).
    while True:
        cycles += 1
        result = check_once(session, state)
        if result >= 0:
            total_alerts += result
            save_state(state)        # on persiste apres chaque cycle reussi
            ping_healthcheck()       # dead-man's switch : on signale qu'on est vivant
            print("[info] cycle {} ok, {} alerte(s)".format(cycles, result), flush=True)
        else:
            print("[warn] cycle {} en erreur, etat preserve".format(cycles), flush=True)

        if time.time() - start >= LOOP_DURATION:
            break
        time.sleep(POLL_INTERVAL)

    print("[done] {} cycle(s), {} alerte(s) au total".format(cycles, total_alerts), flush=True)


if __name__ == "__main__":
    sys.exit(main())
