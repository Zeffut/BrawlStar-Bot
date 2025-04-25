import pyautogui
import time
import cv2
import json
import numpy as np

def get_zone_coordinates(zone_name):
    """
    Retourne les coordonnées de la zone spécifiée depuis un fichier JSON.
    """
    with open("zone_coordinates.json", "r") as f:
        data = json.load(f)
    
    if zone_name not in data:
        raise KeyError(f"La zone '{zone_name}' n'existe pas dans le fichier de coordonnées.")
    
    zone = data[zone_name]
    return zone["top_left"][0], zone["top_left"][1], zone["bottom_right"][0], zone["bottom_right"][1]

def save_zone_coordinates(zone_name, top_left, bottom_right):
    """
    Enregistre les coordonnées de la zone sélectionnée dans un fichier JSON avec un nom de zone.
    """
    try:
        with open("zone_coordinates.json", "r") as f:
            data = json.load(f)
    except FileNotFoundError:
        data = {}

    data[zone_name] = {
        "top_left": top_left,
        "bottom_right": bottom_right
    }

    with open("zone_coordinates.json", "w") as f:
        json.dump(data, f, indent=4)

def select_zone(image_path, zone_name):
    """
    Permet à l'utilisateur de sélectionner une zone sur l'image spécifiée et de lui donner un nom.
    Si la zone sélectionnée est "brawler_trophy", l'image recadrée est traitée : toutes les couleurs sont
    remplacées par du noir sauf celles dont la différence avec la couleur [145, 143, 146] est inférieure à une tolérance.
    Sinon, l'image recadrée est simplement affichée.
    """
    image = cv2.imread(image_path)
    if image is None:
        print(f"Erreur: Impossible de charger l'image '{image_path}'")
        return

    r = cv2.selectROI("Sélection de la zone", image, fromCenter=False, showCrosshair=True)
    cv2.destroyAllWindows()

    top_left = (int(r[0]), int(r[1]))
    bottom_right = (int(r[0] + r[2]), int(r[1] + r[3]))

    save_zone_coordinates(zone_name, top_left, bottom_right)
    print(f"Coordonnées de la zone '{zone_name}' enregistrées: {top_left}, {bottom_right}")

    # Recadrer l'image
    cropped_image = image[top_left[1]:bottom_right[1], top_left[0]:bottom_right[0]]
    
    # Si la zone est "brawler_trophy", appliquer le filtre couleur
    if zone_name == "brawler_trophy":
        target_color = np.array([145, 143, 146])
        tolerance = 40  # tolérance pour la différence
        diff = np.abs(cropped_image.astype(np.int16) - target_color)
        mask = np.all(diff <= tolerance, axis=-1)
        processed_image = np.zeros_like(cropped_image)
        processed_image[mask] = cropped_image[mask]
        display_image = processed_image
    else:
        display_image = cropped_image

    cv2.imshow("Image traitée", display_image)
    cv2.destroyAllWindows()

def crop_image_from_coordinates(image_path, zone_name):
    """
    Découpe l'image à partir des coordonnées enregistrées pour la zone spécifiée et retourne l'image recadrée.
    """
    with open("zone_coordinates.json", "r") as f:
        data = json.load(f)

    if zone_name not in data:
        print(f"Erreur: La zone '{zone_name}' n'existe pas dans le fichier de coordonnées.")
        return None

    top_left = data[zone_name]["top_left"]
    bottom_right = data[zone_name]["bottom_right"]

    image = cv2.imread(image_path)
    if image is None:
        print(f"Erreur: Impossible de charger l'image '{image_path}'")
        return None

    cropped_image = image[top_left[1]:bottom_right[1], top_left[0]:bottom_right[0]]
    return cropped_image

if __name__ == "__main__":
    zone_name = input("Entrez le nom de la zone: ")
    action = input("Voulez-vous (1) sélectionner une nouvelle zone ou (2) découper l'image à partir des coordonnées enregistrées? Entrez 1 ou 2: ")

    if action == "1":
        select_zone("captured_window.png", zone_name)
    elif action == "2":
        cropped_image = crop_image_from_coordinates("captured_window.png", zone_name)
        if cropped_image is not None:
            cv2.imshow("Image recadrée", cropped_image)
            cv2.destroyAllWindows()
    else:
        print("Action non valide.")
