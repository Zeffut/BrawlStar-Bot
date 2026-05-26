# Tests

Suite pytest minimale, sans dépendance réseau (sauf le fixture brawlace
qui est figé sur disque).

## Lancer

```bash
# Local (Mac, py 3.9+ OK — les tests OCR sont skip-if-missing)
python3 -m pytest tests/ -q --ignore=tests/lobby_automation

# Sur le HP (où easyocr est installé) — les tests OCR tournent aussi
ssh hp 'cd ~/BrawlStar-Bot && venv/bin/python -m pytest tests/ -q --ignore=tests/lobby_automation'
```

## Ce qui est couvert

| Fichier | Vérifie |
|---|---|
| `test_brawlace_parse.py` | Regex brawlace.com → nom joueur + liste brawlers (12 dans le fixture) |
| `test_trophy_ocr.py` | OCR du compteur trophées top-left lobby Mi 9T → 616 (skip sans easyocr) |
| `test_cloud_db.py` | Persistance brawlers en SQLite + détection des comptes à rafraîchir |
| `test_webhook_signature.py` | HMAC-SHA256 GitHub : signature valide / falsifiée / mauvaise clé |
| `lobby_automation/` | (existant) test interactif de sélection brawler — exige le téléphone |

## Fixtures (`tests/fixtures/`)

- `lobby_mi9t_2340x1080.png` — capture lobby Mi 9T à 616 trophées
- `brawlace_qprcq9rv2.html` — page brawlace de Zeffut5.0 (12 brawlers)

Pour rafraîchir le fixture brawlace :
```bash
ssh root@vps 'curl -s -X POST http://127.0.0.1:8191/v1 -H "Content-Type: application/json" \
  -d "{\"cmd\":\"request.get\",\"url\":\"https://brawlace.com/players/QPRCQ9RV2\",\"maxTimeout\":60000}" \
  | jq -r .solution.response' > tests/fixtures/brawlace_qprcq9rv2.html
```
