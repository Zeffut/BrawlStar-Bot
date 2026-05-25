# ONNX models

Ces 4 modèles ONNX (YOLOv8 ultralytics, input 640×640) sont copiés depuis le
projet [PylaAI](https://github.com/PylaAI/PylaAI) (branche `compatibility`)
et utilisés tels quels.

| Modèle | Rôle |
|---|---|
| `brawlersInGame.onnx` | Détection des brawlers en partie (ennemis / alliés) |
| `tileDetector.onnx` | Détection des tuiles de map : `wall`, `bush`, `close_bush` |
| `mainInGameModel.onnx` | Détection d'état général en partie |
| `startingScreenModel.onnx` | Détection des écrans de lobby / menus |

## Attribution

Modèles entraînés par les développeurs PylaAI :
- ivanyordanovgt / iyordanov
- AngelFireLA / AngelFire
- awarzu
- Maayan080 (port Linux/macOS)

Licence "No Selling". Usage personnel uniquement, pas de redistribution.
