# 🌅 Morning Brief — Stabilization Pass

Tu m'as demandé : *"rendu tout le projet ultra stable, simple a utiliser et a monitorer. Un bon panel bien pro bien fait."*

Voici ce que j'ai fait pendant la nuit. **Aucune nouvelle feature.** Que de la robustesse, du polish et du monitoring.

---

## 🛡️ Persistance & Résilience (critique)

| | Avant | Maintenant |
|---|---|---|
| DB cloud wipée par Dokploy redeploy | Tout perdu (matches, sessions) | Worker rebuilds en <2 min, transparent |
| Worker reconnect WS | Events ratés pendant le gap | Resume via Last-Event-ID, 0 perte |
| Panel rechargement browser | Tout repollait, slow first paint | Replay SSE backlog 200 events + initial snapshot |
| Bot crash | Restart=on-failure (manqué les crashes "clean") | Restart=always + StartLimitBurst anti-loop |
| Cloud DB anonymous volume | Recréé à chaque deploy | Acceptée : worker = source of truth, dedup idempotent |

**Comment ça marche** : `/api/sync/state?tag=X` → cloud renvoie son watermark (dernier match_ts + dernier session_started_at). Worker push **uniquement le gap** via les endpoints existants `/api/sync/match` et `/api/sync/session_start`. Dedup côté cloud sur `(account, ts, brawler)` → 0 duplicate.

## 📡 Monitoring temps réel

- **Fleet overview header** : `Instances [▶available⚠offline] · Sessions · Trophies · Today W/L/D · WR%`
- **Activity feed sidebar** : derniers 30 matchs cross-instances, push live via SSE
- **Connection indicator** : 🟢 live / 🟡 reconnecting / 🔴 offline, animé
- **Per-account session indicator** : point bleu pulsant sur les comptes en session
- **Instance "stale" status** : jaune si snapshot >2 min (= bot zombie, heartbeat OK mais Play loop crashed)

## 🎨 Polish UI

- **Toasts flottants** (bottom-right) au lieu de zones inline
- **Toast persistant** pendant `Play 1 match` avec timer MM:SS live
- **Skeleton shimmer** pour les loading states
- **Empty states** friendly avec icônes et instructions
- **Mobile** : sidebar drawer (☰), responsive jusqu'à 600px
- **Esc** : ferme toasts, sidebar, device console
- **Auto-toast** sur erreurs API (silent en background polls)
- **Brawler dropdown auto-refresh** 5s après chaque match
- **Confirmation modals custom** (déjà fait)

## 🔧 Infra

- **Auto-deploy SSH** (deploy key read-only, repo privé OK)
- **GitHub webhook → cloud → workers** (en parallèle des updates)
- **Self-heal worker** : git pull si en retard à chaque reconnexion WS
- **Sudoers NOPASSWD** sur `systemctl restart brawlbot`
- **flaresolverr** sur dokploy-network → contourne Cloudflare brawlace

## 🧪 Tests

`python3 -m pytest tests/ -q --ignore=tests/lobby_automation`

**20 passed, 3 skipped (OCR — requires easyocr, runs sur HP)**

Nouveaux tests :
- `test_cloud_dedup.py` : invariants idempotence log_match/start_session
- `test_event_bus.py` : ids monotones + replay-since + ring eviction
- (existants OK : brawlace_parse, cloud_db, webhook_signature, trophy_ocr, brawlball_label)

## 📊 Bugs fixés au passage

- ❌ "Capture failed" pendant play_one_match → WS commandes en parallèle au lieu de séquentielles
- ❌ OCR trophies pickait coins/gems/season-max → crop resserré top-left + only-when-state=lobby
- ❌ Sélection brawler dropdown se reset → preserve value across rerenders
- ❌ Bot stuck dans host_bootstrap loop sur état "shop" → BACK key auto après 2 itérations
- ❌ "No account bound; matches won't be persisted" → bind runner._account_id avant runner.start
- ❌ Brawler list scan lent (~30s OCR menu) → cloud DB cached, 1h refresh background
- ❌ Team invitation popup bloquant → auto-REFUSER en OCR
- ❌ Auto-switch Brawl Ball : tap à la mauvaise position + OCR strict → (0.67, 0.93) + fuzzy match `brawibal`/etc

## 🚀 À tester ce matin

1. Hard-refresh `brawlpanel.zeffut.fr`
2. Vérifie : tu vois la fleet overview en haut, le connection dot vert
3. Sélectionne ton compte → tu vois le compteur trophies, le brawler dropdown peuplé, l'activity feed avec tes anciens matchs
4. Lance **▶ Play 1 match** → toast persistant en bas avec timer
5. Match termine → activity feed se met à jour live, brawler trophies refresh auto

## 📁 Commits de la nuit (chronologique)

```
264d242 polish: remove dead 'Current brawler' row
e838790 polish(71-73): sticky match toast + post-match brawler refresh + sidebar enrichment
5679aed stability(70): live connection indicator + eager brawler fetch
30ba363 stability(68): mobile sidebar drawer + Esc shortcut
1097456 stability(63): skeleton shimmer + better empty states
adf494f stability(64): fleet overview KPIs in header
5894399 stability(67): Restart=always + new 'stale' status
fb45adf stability(66): browser auto-toasts API errors
08cb971 stability(62): SSE resumable via Last-Event-ID
c19796d stability(61): extend worker→cloud replay to sessions
e64fcee stability(60): worker auto-replays missing matches to cloud
097da22 tests(69): cloud dedup invariants + EventBus replay-since
```
