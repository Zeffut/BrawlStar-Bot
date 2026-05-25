# State fixtures

Real phone screenshots labeled by state. Used by `test_state_finder_regression.py`
to verify that the state_finder correctly identifies each known screen.

## Layout

```
tests/fixtures/states/
    lobby/
        lobby_1.png          # main lobby with JOUER button
        ...
    match/
        brawl_ball_1.png     # mid-match Brawl Ball
        solo_showdown_1.png
        ...
    end/
        victoire_1.png       # post-match VICTOIRE screen
        defeat_1.png
        temps_forts_1.png
    disconnect/
        afk_kick_1.png       # "Déconnexion pour non-participation"
    starting/
        star_drop_1.png      # "TOUCHEZ ET MAINTENEZ" daily reward
        loading_match_1.png
    popup/
        starr_nova_1.png
        pass_brawl_1.png
        quetes_1.png
        brawler_unlocked_1.png
        pieces_reward_1.png
        power_points_1.png
```

## Adding a new fixture

When the bot encounters an unknown screen in the wild, save the debug
screenshot (`debug/*.png`) to the corresponding state folder here and
add a new template under `src/bsbot/data/state_templates/<state>/` if
needed. The regression test will then enforce that the screen is
correctly detected.
