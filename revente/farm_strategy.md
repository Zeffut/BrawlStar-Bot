# Stratégie de farm — rentabilité €/temps (recherche 2026-06-01)

> Quoi farmer, à quel niveau, pour maximiser la valeur de revente par heure de bot.
> Source : recherche économie BS (version 2025-2026) + `revente/grille_prix.md`.

## Le constat fondateur
**L'or (coins) et les points de pouvoir sont plafonnés par le CALENDRIER, pas par le temps de jeu.**
Un bot 24/7 n'accélère que les **trophées** — l'axe de valeur le plus faible.

| Ressource | Débit réaliste | Grindable en jouant + ? |
|---|---|---|
| Coins | ~330/jour (6 daily wins + star drops) + quêtes | ❌ non (match n°7 = ~0 coin) |
| Power points | ~170/jour | ❌ non |
| Gemmes | ~100+/an (F2P) | ❌ non |
| **Trophées** | illimité (temps bot) | ✅ **oui — mais faible valeur** |

> Mastery (grosse source de coins/PP) **supprimé en juin 2025** — les vieux guides sont périmés.

## Coûts de progression (confirmés)
- Maxer un brawler **P1→P11** : 3 740 PP + **7 765 coins**
- **Hypercharge** : **5 000 coins** (brawler doit être P11 d'abord)
- Gadget 1 000 / Star Power 2 000 / Gear 1 000-2 000 coins
- Compte « chargé » (≈10 max + 7 HC) ≈ **~112 000 coins ≈ ~1 an** de daily wins → infabricable vite, même par bot.

## Rendement revente par coin dépensé (priorité)
1. 🥇 **Hypercharge** (sur brawler méta déjà P11) : 5 000 c → +5-8 $
2. 🥈 **Maxer P11** un brawler méta : 7 765 c → +10-30 $ (par lot) + débloque l'HC
3. ❌ Gadgets/star powers/gears : impact faible isolé → skip (sauf recherche de complétude)
4. Trophées : 0 coin (coût = heures bot), mais 20k « basique » = 7-13 $ seulement

## Trophées : farmer en LARGEUR
- Soft cap **~1 000-1 100 / brawler** (au-delà : net négatif + **reset saisonnier** à 1 000).
- Gros total = **beaucoup de brawlers à ~1 000**, pas quelques-uns très haut.
- Cible : **~20-25k total**. Au-delà = lent + skill (un bot plafonne). Cohérent avec [[feedback-push-max-ceiling]].

## Stratégie optimale — scale en LARGEUR (nb de comptes), pas en heures/compte
Par compte, chaque jour :
1. **Sécuriser 6 victoires + quêtes + Mega Pig** (~30-45 min) → 100 % du revenu or/PP
2. **Dépenser l'or en hypercharges** sur 5-10 brawlers **méta** (maxés P11 d'abord ; concentrer)
3. Heures restantes → **trophées en largeur** vers ~20-25k
4. **Thésauriser les gemmes** (ne pas dépenser) → valeur revente

**Levier €/heure n°1 : multiplier les comptes.** Chaque compte ajoute son quota d'or quotidien (non plafonné globalement) + son farm trophées parallèle. Faire tourner UN compte 24/7 au-delà du quota quotidien ≈ gaspillage (ne produit que des trophées à faible valeur).

## Implications produit (à relayer au dev)
- `push_max` doit farmer **en largeur** (plusieurs brawlers vers ~1 000), pas pousser un brawler haut.
- Le bot doit **prioriser le quota quotidien** (6 wins + quêtes) puis dépenser l'or auto en hypercharges sur brawlers méta.
- Roadmap prod = **plus d'instances/comptes en parallèle**, pas plus d'heures/compte.

## Incertitudes (à mesurer en jeu sur un compte test, 7 jours)
- Total exact coins/PP gagnables par semaine post-juin-2025 (sources tierces périmées).
- Montants coins du Trophy Road 100k palier par palier.
