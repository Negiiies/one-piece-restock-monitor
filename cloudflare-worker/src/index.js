/**
 * Moniteur de restocks / nouveautes One Piece (Ludisphere) — Cloudflare Worker.
 *
 * Declenche par un Cron Trigger toutes les minutes (fiable, sur l'edge Cloudflare).
 * Recupere /collections/<collection>/products.json, compare a l'etat stocke dans KV,
 * et envoie une alerte Telegram pour :
 *   - NOUVEAUTE : produit jamais vu, recent et disponible
 *   - RESTOCK   : variante qui passe indisponible -> disponible
 *
 * Ecrit dans KV UNIQUEMENT si l'etat change (respecte la limite gratuite : 1000 ecritures/jour).
 *
 * Secrets attendus (wrangler secret put) :
 *   TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, HEALTHCHECK_URL (optionnel)
 * Binding KV attendu : STATE_KV
 */

const BASE_URL = "https://ludisphere.fr";
const COLLECTION = "one-piece-card-game-precommande";
const NEW_WINDOW_HOURS = 72;
const STATE_KEY = "state";
const USER_AGENT =
  "OnePieceRestockMonitor/1.0 (Cloudflare Worker; +https://github.com/Negiiies/one-piece-restock-monitor)";

export default {
  // Declenche par le Cron Trigger (toutes les minutes)
  async scheduled(event, env, ctx) {
    ctx.waitUntil(runCheck(env));
  },
  // Permet aussi un declenchement manuel via URL (test / debug)
  async fetch(request, env, ctx) {
    const result = await runCheck(env);
    return new Response(JSON.stringify(result, null, 2), {
      headers: { "content-type": "application/json; charset=utf-8" },
    });
  },
};

// --------------------------------------------------------------------------- //
// Cycle principal
// --------------------------------------------------------------------------- //
async function runCheck(env) {
  let products;
  try {
    products = await fetchAllProducts();
  } catch (err) {
    console.log("[error] fetch echoue : " + err);
    return { ok: false, error: String(err) };
  }

  // Charge l'etat precedent depuis KV
  const raw = await env.STATE_KV.get(STATE_KEY);
  let state = raw
    ? JSON.parse(raw)
    : { variants: {}, seen_product_ids: [], seeded: false };
  if (!state.variants) state.variants = {};
  if (!state.seen_product_ids) state.seen_product_ids = [];

  // Sanity-check : 0 produit alors qu'on en avait => anomalie, on preserve l'etat
  if (products.length === 0 && Object.keys(state.variants).length > 0) {
    console.log("[error] 0 produit recu alors que l'etat en contenait : anomalie, etat preserve");
    return { ok: false, error: "empty_response_guard" };
  }

  const firstRun = !state.seeded;
  const seen = new Set(state.seen_product_ids);
  let alerts = 0;
  let changed = false;

  // Message de demarrage au tout premier seed
  if (firstRun) {
    await tg(env, "sendMessage", {
      text:
        "✅ <b>Moniteur One Piece démarré (Cloudflare)</b>\n" +
        "Je surveille la page de précommande Ludisphère, vérification chaque minute.\n" +
        "Tu recevras les 🆕 nouveautés et 🔁 retours en stock ici.",
      disable_web_page_preview: true,
    });
  }

  for (const product of products) {
    const pid = product.id;
    const isNewProduct = !seen.has(pid);
    seen.add(pid);

    for (const variant of product.variants || []) {
      const vid = String(variant.id);
      const available = Boolean(variant.available);
      const price = variant.price;
      const prev = state.variants[vid];

      const newEntry = {
        available,
        price,
        title: product.title,
        handle: product.handle,
        product_id: pid,
      };

      if (firstRun) {
        state.variants[vid] = newEntry;
        changed = true;
        continue;
      }

      // NOUVEAUTE : produit jamais vu + recent + disponible
      if (isNewProduct && isRecent(product.created_at) && available) {
        if (await sendAlert(env, formatAlert("new", product, variant, price), mainImage(product))) {
          alerts++;
        }
      }
      // RESTOCK : transition indisponible -> disponible
      else if (prev && prev.available === false && available === true) {
        if (await sendAlert(env, formatAlert("restock", product, variant, price), mainImage(product))) {
          alerts++;
        }
      }

      // Detecte un changement d'etat a persister
      if (!prev || prev.available !== available || prev.price !== price) {
        changed = true;
      }
      state.variants[vid] = newEntry;
    }
  }

  state.seen_product_ids = Array.from(seen).sort((a, b) => a - b);
  if (firstRun) {
    state.seeded = true;
    changed = true;
    console.log("[info] seed initial : " + Object.keys(state.variants).length + " variantes memorisees");
  }

  // Ecrit dans KV uniquement si quelque chose a change (economise les ecritures)
  if (changed) {
    await env.STATE_KV.put(STATE_KEY, JSON.stringify(state));
  }

  // Heartbeat / dead-man's switch
  if (env.HEALTHCHECK_URL) {
    try {
      await fetch(env.HEALTHCHECK_URL, { method: "GET" });
    } catch (e) {
      /* ignore */
    }
  }

  console.log("[done] " + products.length + " produits, " + alerts + " alerte(s), changed=" + changed);
  return { ok: true, products: products.length, alerts, changed };
}

// --------------------------------------------------------------------------- //
// Recuperation des produits (endpoint JSON Shopify)
// --------------------------------------------------------------------------- //
async function fetchAllProducts() {
  const products = [];
  let page = 1;
  while (page <= 20) {
    const url = `${BASE_URL}/collections/${COLLECTION}/products.json?limit=250&page=${page}`;
    const resp = await fetch(url, {
      headers: { "User-Agent": USER_AGENT, Accept: "application/json" },
      // On veut la donnee la plus fraiche possible cote origine
      cf: { cacheTtl: 0, cacheEverything: false },
    });
    if (resp.status === 429) {
      throw new Error("rate_limited_429");
    }
    if (!resp.ok) {
      throw new Error("HTTP " + resp.status);
    }
    const data = await resp.json();
    const batch = data.products || [];
    if (batch.length === 0) break;
    products.push(...batch);
    page++;
  }
  return products;
}

function isRecent(createdAt) {
  if (!createdAt) return false;
  const t = Date.parse(createdAt);
  if (Number.isNaN(t)) return false;
  return Date.now() - t < NEW_WINDOW_HOURS * 3600 * 1000;
}

function productUrl(handle) {
  return `${BASE_URL}/products/${handle}`;
}

function mainImage(product) {
  const imgs = product.images || [];
  if (imgs.length && imgs[0].src) return imgs[0].src;
  return null;
}

// --------------------------------------------------------------------------- //
// Telegram
// --------------------------------------------------------------------------- //
async function tg(env, method, payload) {
  if (!env.TELEGRAM_TOKEN || !env.TELEGRAM_CHAT_ID) {
    console.log("[error] TELEGRAM_TOKEN ou TELEGRAM_CHAT_ID manquant");
    return false;
  }
  const url = `https://api.telegram.org/bot${env.TELEGRAM_TOKEN}/${method}`;
  const body = Object.assign({}, payload, {
    chat_id: env.TELEGRAM_CHAT_ID,
    parse_mode: "HTML",
  });
  for (let attempt = 0; attempt < 3; attempt++) {
    try {
      const resp = await fetch(url, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(body),
      });
      if (resp.status === 429) {
        const j = await resp.json().catch(() => ({}));
        const retry = (j.parameters && j.parameters.retry_after) || 2;
        await sleep(retry * 1000);
        continue;
      }
      if (resp.ok) return true;
      console.log("[warn] Telegram " + method + " HTTP " + resp.status);
    } catch (e) {
      console.log("[warn] Telegram " + method + " erreur : " + e);
    }
    await sleep(1000 * (attempt + 1));
  }
  return false;
}

async function sendAlert(env, caption, photoUrl) {
  if (photoUrl) {
    if (await tg(env, "sendPhoto", { photo: photoUrl, caption })) return true;
    console.log("[warn] sendPhoto echoue, repli sur message texte");
  }
  return await tg(env, "sendMessage", { text: caption, disable_web_page_preview: false });
}

function formatAlert(kind, product, variant, price) {
  const title = product.title || "Produit";
  const url = productUrl(product.handle || "");
  const vtitle = variant.title || "";
  const vlabel = vtitle === "" || vtitle === "Default Title" ? "" : " — " + vtitle;
  const tag = kind === "new" ? "🆕 <b>NOUVEAUTÉ</b>" : "🔁 <b>RETOUR EN STOCK</b>";
  const priceLine = price ? "💶 " + price + " €" : "";
  return (
    tag + "\n" +
    "🏴‍☠️ <b>" + escapeHtml(title) + "</b>" + escapeHtml(vlabel) + "\n" +
    priceLine + "\n" +
    "🔗 " + url
  );
}

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}
