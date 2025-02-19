import cv2
import numpy as np
import mss
import os
import Quartz
import pyautogui
import time
import random
import subprocess
import pytesseract
import json
import requests  # Importer la bibliothèque requests pour envoyer des messages Telegram
from select_zone import get_zone_coordinates  # Importer la fonction pour obtenir les coordonnées de la zone
from select_zone import crop_image_from_coordinates  # Importer la fonction pour découper l'image
import tkinter as tk
from tkinter import messagebox
import threading

debug = True
window_name = "BlueStacks"
output_path = "captured_window.png"
reference_folder = "references"

def get_window_coordinates(window_name):
    """
    Obtient les coordonnées d'une fenêtre en fonction de son nom.
    """
    windows = Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID)
    for window in windows:
        if window_name in window.get('kCGWindowName', ''):
            bounds = window['kCGWindowBounds']
            left = bounds['X']
            top = bounds['Y']
            right = left + bounds['Width']
            bottom = top + bounds['Height']
            return left, top, right, bottom
    return None

def bring_window_to_front(window_name):
    """
    Met la fenêtre au premier plan.
    """
    script = f'''
    tell application "System Events"
        set frontmost of the first process whose name is "{window_name}" to true
    end tell
    '''
    subprocess.run(['osascript', '-e', script])

def resize_window(window_name, width, height):
    """
    Redimensionne la fenêtre spécifiée par son nom aux dimensions données.
    """
    script = f'''
    tell application "System Events"
        set the size of the first window of application process "{window_name}" to {{ {width}, {height} }}
    end tell
    '''
    subprocess.run(['osascript', '-e', script])

def capture_window_image(window_name):
    """
    Capture l'image de la fenêtre spécifiée et la retourne.
    """
    coordinates = get_window_coordinates(window_name)
    if coordinates is None:
        print(f"Fenêtre '{window_name}' introuvable.")
        return None

    left, top, right, bottom = coordinates
    width = right - left
    height = bottom - top

    with mss.mss() as sct:
        screenshot = sct.grab({"top": top, "left": left, "width": width, "height": height})
        frame = np.array(screenshot)
        frame = cv2.cvtColor(frame, cv2.COLOR_RGBA2RGB)  # Convertir en RGB pour OpenCV
        return frame

def resize_image_to_fit(captured_image, reference_image):
    """
    Redimensionne l'image de référence pour qu'elle s'adapte à l'image capturée.
    """
    captured_height, captured_width = captured_image.shape[:2]
    reference_height, reference_width = reference_image.shape[:2]

    if reference_height > captured_height or reference_width > captured_width:
        scaling_factor = min(captured_height / reference_height, captured_width / reference_width)
        new_size = (int(reference_width * scaling_factor), int(reference_height * scaling_factor))
        reference_image = cv2.resize(reference_image, new_size, interpolation=cv2.INTER_AREA)
    
    return reference_image

def is_image_present(captured_image_path, reference_image_path, threshold=0.8):
    """
    Vérifie si l'image de référence est présente dans l'image capturée.
    Retourne True si une correspondance est trouvée au-dessus du seuil.
    """
    # Vérifier si le fichier capturé existe
    if not os.path.exists(captured_image_path):
        print(f"Le fichier {captured_image_path} est introuvable.")
        return False

    # Chargement des images
    captured_image = cv2.imread(captured_image_path)
    reference_image = cv2.imread(reference_image_path)

    if captured_image is None or reference_image is None:
        return False

    # Redimensionner l'image de référence si nécessaire
    reference_image = resize_image_to_fit(captured_image, reference_image)

    # Conversion en niveaux de gris
    captured_image_gray = cv2.cvtColor(captured_image, cv2.COLOR_BGR2GRAY)
    reference_image_gray = cv2.cvtColor(reference_image, cv2.COLOR_BGR2GRAY)

    # Correspondance par modèle
    result = cv2.matchTemplate(captured_image_gray, reference_image_gray, cv2.TM_CCOEFF_NORMED)
    min_val, max_val, min_loc, max_loc = cv2.minMaxLoc(result)

    return max_val >= threshold

def find_all_similar_images(captured_image_path, reference_folder, threshold=0.8):
    """
    Compare l'image capturée avec toutes les images dans le dossier de références
    et retourne une liste des images qui sont présentes dans l'image capturée.
    """
    similar_images = []

    for reference_image in os.listdir(reference_folder):
        if not reference_image.lower().endswith('.png'):
            continue
        reference_image_path = os.path.join(reference_folder, reference_image)
        if is_image_present(captured_image_path, reference_image_path, threshold):
            similar_images.append(reference_image)

    return similar_images

def move_randomly():
    """
    Effectue un mouvement aléatoire avec les touches W, A, S, D.
    """
    if debug:
        print("Débogage: mouvement aléatoire")
        return
    directions = ['w', 'a', 's', 'd']
    direction = random.choice(directions)
    pyautogui.keyDown(direction)
    move_duration = random.uniform(0.2, 5.0)  # Déplacement plus court pour plus de dynamisme
    time.sleep(move_duration)
    pyautogui.keyUp(direction)

def shoot():
    if debug:
        print("Débogage: tir")
        return
    pyautogui.press('e')

def afk():
    """
    Simule le comportement d'un joueur humain jusqu'à ce que le statut ne contienne plus 'in_game'.
    """

    # Simuler un mouvement constant
    if random.random() < 0.9:  # 90% de chance de bouger en permanence
            move_randomly()
        
    for _ in range(random.randint(1, 3)):
        shoot()

    # Petite pause entre les cycles pour éviter des mouvemeents trop rapides
    cycle_delay = random.uniform(0.1, 0.2)
    time.sleep(cycle_delay)

def check_missing_images(reference_folder, required_images):
    """
    Vérifie si certaines images de référence sont manquantes.
    """
    missing_images = []
    for image in required_images:
        if not os.path.exists(os.path.join(reference_folder, image)):
            missing_images.append(image)
    return missing_images

def adjust_click_coordinates(x, y, window_coordinates, captured_image_shape):
    """
    Ajuste les coordonnées de clic en fonction de la position et de la taille de la fenêtre.
    """
    left, top, right, bottom = window_coordinates
    window_width = right - left
    window_height = bottom - top
    captured_height, captured_width = captured_image_shape[:2]

    adjusted_x = int(x * (window_width / captured_width)) + left
    adjusted_y = int(y * (window_height / captured_height)) + top
    print(f"Coordonnées ajustées: ({adjusted_x}, {adjusted_y})")
    return adjusted_x, adjusted_y

def find_text_coordinates(image, text):
    """
    Trouve les coordonnées du texte spécifié dans l'image.
    """
    data = pytesseract.image_to_data(image, output_type=pytesseract.Output.DICT)
    n_boxes = len(data['level'])
    for i in range(n_boxes):
        if text.lower() in data['text'][i].lower():
            (x, y, w, h) = (data['left'][i], data['top'][i], data['width'][i], data['height'][i])
            return x + w // 2, y + h // 2
    return None, None

def crop_global_trophy_zone(captured_image_path):
    """
    Découpe l'image capturée pour extraire la zone "global_trophy" et retourne le texte extrait converti en chiffre.
    """
    cropped_image = crop_image_from_coordinates(captured_image_path, "global_trophy")
    if cropped_image is not None:
        text = pytesseract.image_to_string(cropped_image)
        try:
            number = int(''.join(filter(str.isdigit, text)))
            return number
        except ValueError:
            return None
    return None

def crop_brawler_trophy_zone(captured_image_path):
    """
    Découpe l'image capturée pour extraire la zone "brawler_trophy" et retourne le texte extrait converti en chiffre.
    """
    cropped_image = crop_image_from_coordinates(captured_image_path, "brawler_trophy")
    if cropped_image is not None:
        text = pytesseract.image_to_string(cropped_image)
        try:
            number = int(''.join(filter(str.isdigit, text)))
            return number
        except ValueError:
            return None
    return None

def send_telegram_message(message):
    """
    Envoie un message sur Telegram.
    """
    bot_token = ''
    chat_id = ''
    url = f'https://api.telegram.org/bot{bot_token}/sendMessage'
    data = {'chat_id': chat_id, 'text': message}
    response = requests.post(url, data=data)
    return response

def start_script():
    global objective
    try:
        objective = int(entry_objective.get())
        if objective > 500:
            messagebox.showwarning("Attention", "Le script peut ne pas réussir à monter aussi haut en trophée.")
        threading.Thread(target=run_script).start()
    except ValueError:
        messagebox.showerror("Erreur", "Veuillez entrer un chiffre valide pour l'objectif de trophée.")

def run_script():
    status = "none"
    message = "none"
    global_trophy = 0
    brawler_trophy = 0
    victory = 0
    defeat = 0

    required_images = [
        "disconnected.png", "afk.png", "connexion_lost.png", 
        "play_button.png", "brawball.png", "brawball1.png", 
        "other_device.png", "new_brawler.png", "prix_star.png", 
        "in_game.png", "continue_button.png", "leave_button.png"
    ]
    missing_images = check_missing_images(reference_folder, required_images)
    if missing_images:
        update_text(f"Images manquantes: {', '.join(missing_images)}")
    else:
        update_text("Toutes les images nécessaires sont présentes.")
    
    target_width = 944.0
    target_height = 545.0
    bring_window_to_front(window_name)
    resize_window(window_name, target_width, target_height)
    
    trophy_zone_coordinates = get_zone_coordinates("global_trophy")  # Spécifier le nom de la zone
    
    # Attendre que l'utilisateur mette le jeu sur le lobby pour obtenir une première valeur pour les trophées
    while True:
        bring_window_to_front(window_name)
        captured_image = capture_window_image(window_name)
        
        if captured_image is None:
            time.sleep(1)
            continue

        cv2.imwrite(output_path, captured_image)
        
        similar_images = find_all_similar_images(output_path, reference_folder)
        
        if "play_button.png" in similar_images:
            global_trophy = crop_global_trophy_zone(output_path)
            brawler_trophy = crop_brawler_trophy_zone(output_path)
            if global_trophy is not None:
                if brawler_trophy is None:
                    while True:
                        brawler_trophy_input = input("Impossible de détecter le nombre de trophées du brawler. Veuillez entrer le nombre de trophées manuellement: ")
                        if brawler_trophy_input.isdigit():
                            brawler_trophy = int(brawler_trophy_input)
                            break
                        else:
                            update_text("Veuillez entrer un chiffre valide.")
                update_text(f"Trophés Global: {global_trophy}")
                update_text(f"Trophés Brawler: {brawler_trophy}")
                break
            else:
                update_text("Impossible d'obtenir les trophées. Veuillez vérifier que le jeu est sur le lobby.")
        
        time.sleep(1)
    
    # Initialiser les variables pour stocker les informations précédentes
    previous_status = ""
    previous_global_trophy = 0
    previous_brawler_trophy = 0
    previous_victory = 0
    previous_defeat = 0

    while True:
        bring_window_to_front(window_name)
        captured_image = capture_window_image(window_name)
        
        if captured_image is None:
            time.sleep(1)
            continue

        cv2.imwrite(output_path, captured_image)
        
        similar_images = find_all_similar_images(output_path, reference_folder)
        
        if "disconnected.png" in similar_images or "afk.png" in similar_images or "connexion_lost.png" in similar_images:
            if not debug:
                pyautogui.press('space')
            status = "disconnected"
        if "play_button.png" in similar_images:
            time.sleep(1)
            new_global_trophy = crop_global_trophy_zone(output_path)
            if new_global_trophy is not None:
                if new_global_trophy < global_trophy:
                    defeat += 1
                elif new_global_trophy > global_trophy:
                    victory += 1
                global_trophy = new_global_trophy
            new_brawler_trophy = crop_brawler_trophy_zone(output_path)
            if new_brawler_trophy is not None:
                brawler_trophy = new_brawler_trophy
            status = "lobby"
        if "play_button.png" in similar_images and ("brawball.png" in similar_images or "brawball1.png" in similar_images):
            status = "lobby"
            if not debug:
                pyautogui.press('f')
        if "play_button.png" in similar_images and not ("brawball.png" in similar_images or "brawball1.png" in similar_images):
            update_text("LE MODE DE JEU EST INCORRECT !!!")
        if "other_device.png" in similar_images:
            time.sleep(60)
            if not debug:
                pyautogui.press('space')
            status = "other_device"
        if "new_brawler.png" in similar_images:
            if not debug:
                pyautogui.press('n')
                time.sleep(1)
                pyautogui.press('space')
            status = "new_brawler"
        if "prix_star.png" in similar_images:
            for _ in range(7):
                if not debug:
                    pyautogui.press('space')
                status = "star_prize"
                time.sleep(1)
        elif "in_game.png" in similar_images:
            afk()
            status = "in_game"
        elif "continue_button.png" in similar_images or "leave_button.png" in similar_images:
            if not debug:
                pyautogui.press('f')
            status = "match_ended"
        elif "crashed.png" in similar_images:
            if not debug:
                pyautogui.press('b')
            status = "crashed"
        if brawler_trophy >= objective:
            update_text("Objectif atteint!")
            send_telegram_message(f"[BrawlStarBot] Objectif atteint! Trophés Brawler: {brawler_trophy}")
            break
        if status == "disconnected" or status == "other_device" or status == "crashed":
            send_telegram_message(f"[BrawlStarBot] Le script est bloqué. Statut: {status}")

        # Afficher les informations uniquement si elles ont changé
        if (status != previous_status or global_trophy != previous_global_trophy or 
            brawler_trophy != previous_brawler_trophy or victory != previous_victory or 
            defeat != previous_defeat):
            update_text(f"Images similaires: {', '.join(similar_images)}")
            update_text(f"Statut: {status}")
            update_text(f"Trophés Global: {global_trophy}")
            update_text(f"Trophés Brawler: {brawler_trophy}")
            update_text(f"Victoires: {victory}")
            update_text(f"Défaites: {defeat}")

            # Mettre à jour les informations précédentes
            previous_status = status
            previous_global_trophy = global_trophy
            previous_brawler_trophy = brawler_trophy
            previous_victory = victory
            previous_defeat = defeat

        os.remove(output_path)

def update_text(message):
    text_widget.config(state=tk.NORMAL)
    text_widget.insert(tk.END, message + "\n")
    text_widget.config(state=tk.DISABLED)
    text_widget.see(tk.END)

# Interface utilisateur avec tkinter
root = tk.Tk()
root.title("BrawlStar Mastery Bot")

tk.Label(root, text="Objectif de Trophée:").pack()
entry_objective = tk.Entry(root)
entry_objective.pack()

start_button = tk.Button(root, text="Démarrer", command=start_script)
start_button.pack()

text_widget = tk.Text(root, state=tk.DISABLED, width=80, height=20)
text_widget.pack()

root.mainloop()
