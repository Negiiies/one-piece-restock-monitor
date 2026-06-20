# 🏴‍☠️ One Piece Restock Monitor

Surveille la page de précommande **One Piece Card Game** de [Ludisphère](https://ludisphere.fr/collections/one-piece-card-game-precommande)
et envoie une alerte dans un **groupe Telegram** dès qu'il y a :

- 🆕 une **nouveauté** (produit récemment publié et disponible) ;
- 🔁 un **retour en stock** (une variante repasse de « épuisé » à « disponible »).

Le moniteur interroge l'endpoint JSON stable de Shopify (`/products.json`) — pas de scraping HTML fragile.
Il tourne **gratuitement** sur GitHub Actions.

---

## 1. Prérequis Telegram (à faire une seule fois)

1. **Créer le bot** : dans Telegram, ouvre **@BotFather** (badge bleu) → `/newbot` → choisis un nom puis un username finissant par `bot`.
   BotFather te donne un **TOKEN** (`123456789:AAH...`). Garde-le secret.
2. **Créer le groupe** et y **ajouter ton bot** (idéalement comme administrateur).
3. **Récupérer le CHAT_ID** du groupe (nombre négatif, ex. `-1001234567890`) — via `@getidsbot`, `@RawDataBot`,
   ou l'URL `https://api.telegram.org/bot<TOKEN>/getUpdates` après avoir envoyé `/test` dans le groupe.

Tu repars avec **2 valeurs** : `TELEGRAM_TOKEN` et `TELEGRAM_CHAT_ID`.

---

## 2. Mettre le projet sur GitHub

1. Crée un **compte GitHub** (gratuit) si tu n'en as pas : https://github.com/signup
2. Crée un **nouveau dépôt PUBLIC** (le public donne des minutes Actions illimitées et gratuites).
   Exemple de nom : `one-piece-restock-monitor`.
3. Envoie le contenu de ce dossier (`restock-monitor/`) dans le dépôt. En ligne de commande :

   ```bash
   cd restock-monitor
   git init
   git add .
   git commit -m "init: moniteur de restock One Piece"
   git branch -M main
   git remote add origin https://github.com/<TON_PSEUDO>/one-piece-restock-monitor.git
   git push -u origin main
   ```

   (Ou utilise l'interface web GitHub « Add file → Upload files ».)

---

## 3. Configurer les secrets (jamais dans le code !)

Sur ton dépôt GitHub : **Settings → Secrets and variables → Actions → New repository secret**.
Crée ces secrets :

| Nom du secret      | Valeur                                   | Obligatoire |
|--------------------|------------------------------------------|-------------|
| `TELEGRAM_TOKEN`   | le token donné par BotFather             | ✅ |
| `TELEGRAM_CHAT_ID` | le chat_id du groupe (nombre négatif)    | ✅ |
| `HEALTHCHECK_URL`  | une URL de ping [healthchecks.io](https://healthchecks.io) | ⬜ (recommandé) |

> **HEALTHCHECK_URL** est le « dead-man's switch » : crée un check gratuit sur healthchecks.io,
> colle son URL de ping ici, et configure-le pour t'alerter (email/Telegram) si le moniteur
> arrête de pinger → tu sauras tout de suite si le bot tombe en panne silencieusement.

---

## 4. Activer

L'onglet **Actions** du dépôt → active les workflows si demandé.
Le moniteur démarre automatiquement (cron toutes les 5 min) ; tu peux aussi le lancer à la main
via **Actions → Restock Monitor → Run workflow**.

- **Premier run** = *seed silencieux* : il mémorise l'inventaire actuel et envoie un seul message
  « Moniteur démarré ». Aucune alerte produit (sinon il faudrait crier sur les ~50 produits existants).
- **Runs suivants** : tu reçois uniquement les vraies nouveautés et retours en stock.

---

## 5. Tester en local (optionnel)

```bash
cd restock-monitor
python3 -m pip install -r requirements.txt
export TELEGRAM_TOKEN="123456789:AAH..."
export TELEGRAM_CHAT_ID="-1001234567890"
export LOOP_DURATION="0"     # un seul cycle puis on sort
python3 monitor.py
```

Le premier lancement crée `state.json` (seed) et envoie le message de démarrage dans ton groupe.

---

## Réglages (variables d'environnement)

| Variable             | Défaut                              | Rôle |
|----------------------|-------------------------------------|------|
| `POLL_INTERVAL`      | `60`                                | secondes entre deux vérifications |
| `LOOP_DURATION`      | `240`                               | durée de la boucle interne par run (s) |
| `NEW_WINDOW_HOURS`   | `72`                                | un produit est « nouveau » s'il a été créé il y a moins de X h |
| `SHOP_BASE_URL`      | `https://ludisphere.fr`             | domaine de la boutique |
| `SHOP_COLLECTION`    | `one-piece-card-game-precommande`   | handle de la collection surveillée |
| `SEND_STARTUP_MESSAGE`| `1`                                | envoyer le message « démarré » au seed |

---

## Comment ça marche (résumé technique)

- **Source** : `GET /collections/<collection>/products.json?limit=250` (JSON stable, 1 requête).
- **État** : `state.json` (clé = `variant.id` → disponibilité, prix). Committé dans le dépôt entre les runs.
- **Détection** : transition `available: false → true` = restock ; `product.id` inédit + récent = nouveauté.
- **Anti-spam** : seed silencieux au 1er run ; alerte uniquement sur la *transition*, jamais sur l'état stable.
- **Robustesse** : respect des `429/Retry-After`, sanity-check (0 produit ≠ « tout épuisé »), heartbeat optionnel.
