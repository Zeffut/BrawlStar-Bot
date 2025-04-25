# BrawlStarMasteryBot

BrawlStarMasteryBot est un bot conçu pour automatiser certaines tâches dans le jeu Brawl Stars. Il capture des images de la fenêtre du jeu, analyse les trophées et envoie des notifications via Telegram.

## Fonctionnalités

- Capture d'images de la fenêtre de jeu.
- Détection des trophées globaux et des trophées de brawler.
- Envoi de notifications via Telegram lorsque des objectifs sont atteints.
- Gestion des déconnexions et des erreurs de connexion.
- Déplacement aléatoire du personnage pour éviter d'être inactif.

## Prérequis

Avant de commencer, assurez-vous d'avoir installé les dépendances suivantes :

- Python 3.x
- Pip 3.x

Vous pouvez installer les dépendances avec la commande suivante :

```bash
pip install -r requirements.txt
```

## Installation

1. Clonez le dépôt :

   ```bash
   git clone https://github.com/Zeffut/BrawlStar-Bot.git
   cd BrawlStar-Bot
   ```

2. Assurez-vous que Tesseract est installé sur votre système. Vous pouvez le télécharger depuis [Tesseract OCR](https://github.com/tesseract-ocr/tesseract).

3. Modifiez le fichier `macos.py` pour configurer votre token Telegram et votre chat ID.

## Utilisation

1. Exécutez le script :

   ```bash
   python macos.py
   ```

2. Suivez les instructions à l'écran pour définir votre objectif de trophée.

## Avertissements

- Ce bot interagit directement avec le jeu, ce qui peut entraîner des sanctions si utilisé de manière inappropriée. Utilisez-le à vos risques et périls.
- Assurez-vous que la fenêtre du jeu est visible et que le bot a les autorisations nécessaires pour interagir avec elle.

## Contribuer

Les contributions sont les bienvenues ! Si vous souhaitez améliorer ce projet, n'hésitez pas à soumettre une demande de tirage.