import cv2
import numpy as np
import pytesseract
from select_zone import crop_image_from_coordinates

def crop_brawler_trophy_zone(captured_image_path):
    print(f"Chargement de l'image depuis {captured_image_path}")
    cropped_image = crop_image_from_coordinates(captured_image_path, "brawler_trophy")
    if cropped_image is not None:
        print("Image recadrée avec succès.")
        target_color = np.array([255, 255, 255])
        tolerance = 50  # Augmenter la tolérance de couleur
        diff = np.abs(cropped_image.astype(np.int16) - target_color)
        print(f"Différence de couleur calculée : {diff}")
        mask = np.all(diff <= tolerance, axis=-1)
        print(f"Masque de couleur appliqué : {mask}")
        filtered_image = np.zeros_like(cropped_image)
        filtered_image[mask] = cropped_image[mask]
        if filtered_image.size > 0:  # Vérification si l'image n'est pas vide
            print("Image filtrée avec succès.")
            cv2.imwrite("brawler_trophy_processed.png", filtered_image)
            if filtered_image.shape[0] > 0 and filtered_image.shape[1] > 0:  # Vérification des dimensions de l'image
                print("Dimensions de l'image vérifiées.")
                # Appliquer un lissage pour améliorer la lecture
                smoothed_image = cv2.GaussianBlur(filtered_image, (5, 5), 0)
                print("Lissage de l'image appliqué.")
                text = pytesseract.image_to_string(smoothed_image)
                print(f"Texte extrait de l'image : {text}")
                try:
                    number = int(''.join(filter(str.isdigit, text)))
                    print(f"Nombre de trophées détecté : {number}")
                    return number
                except ValueError:
                    print("Erreur : Impossible de convertir le texte en nombre.")
                    return None
        else:
            print("Erreur : L'image filtrée est vide.")
    else:
        print("Erreur : Impossible de recadrer l'image.")
    return None

if __name__ == '__main__':
    captured_image_path = "captured_window.png"  # Remplacez par le chemin de votre image de test
    print(f"Début du test avec l'image : {captured_image_path}")
    brawler_trophy = crop_brawler_trophy_zone(captured_image_path)
    if brawler_trophy is not None:
        print(f"Nombre de trophées du brawler: {brawler_trophy}")
    else:
        print("Impossible de lire le nombre de trophées du brawler.")
