import cv2
import numpy as np
import os
import pyautogui
import time
import random
import subprocess
import pytesseract
import json
import requests
from select_zone import get_zone_coordinates
from select_zone import crop_image_from_coordinates
import sys
import threading
from datetime import datetime
from colorama import Fore, Style, init
import pygetwindow as gw
from pywinauto import Application
init(autoreset=True)


window_name = "BlueStacks"
output_path = "captured_window.png"
reference_folder = "references"

# Fonctions spécifiques à Windows
def get_window_coordinates(window_name):
    try:
        window = gw.getWindowsWithTitle(window_name)[0]
        if window:
            left, top, right, bottom = window.left, window.top, window.right, window.bottom
            return left, top, right, bottom
    except IndexError:
        print(f"Fenêtre '{window_name}' introuvable.")
    return None

def bring_window_to_front(window_name):
    try:
        window = gw.getWindowsWithTitle(window_name)[0]
        if window:
            try:
                window.activate()
            except Exception as e:
                msg = str(e)
                if "L’opération a réussi" in msg:
                    pass  # Suppression du message si l'opération a réussi malgré l'exception.
                else:
                    print(f"Erreur lors de l'activation de la fenêtre '{window_name}': {msg}")
    except IndexError:
        print(f"Fenêtre '{window_name}' introuvable.")

def resize_window(window_name, width, height):
    try:
        window = gw.getWindowsWithTitle(window_name)[0]
        if window:
            window.resizeTo(int(width), int(height))
    except IndexError:
        print(f"Fenêtre '{window_name}' introuvable.")

def capture_window_image(window_name):
    coordinates = get_window_coordinates(window_name)
    if coordinates is None:
        print(f"Fenêtre '{window_name}' introuvable.")
        return None
    left, top, right, bottom = coordinates
    width = right - left
    height = bottom - top
    screenshot = pyautogui.screenshot(region=(left, top, width, height))
    frame = np.array(screenshot)
    frame = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
    return frame

# Fonctions communes (copiées de macos.py)
def resize_image_to_fit(captured_image, reference_image):
    captured_height, captured_width = captured_image.shape[:2]
    reference_height, reference_width = reference_image.shape[:2]
    if reference_height > captured_height or reference_width > captured_width:
        scaling_factor = min(captured_height / reference_height, captured_width / reference_width)
        new_size = (int(reference_width * scaling_factor), int(reference_height * scaling_factor))
        reference_image = cv2.resize(reference_image, new_size, interpolation=cv2.INTER_AREA)
    return reference_image

def is_image_present(captured_image_path, reference_image_path, threshold=0.8):
    # Chargement des images et conversion en niveaux de gris
    captured = cv2.imread(captured_image_path)
    reference = cv2.imread(reference_image_path)
    if captured is None or reference is None:
        print(f"Erreur de lecture pour {captured_image_path} ou {reference_image_path}.")
        return False
    cap_gray = cv2.cvtColor(captured, cv2.COLOR_BGR2GRAY)
    ref_gray = cv2.cvtColor(reference, cv2.COLOR_BGR2GRAY)
    
    # Détection et description avec ORB
    orb = cv2.ORB_create()
    kp1, des1 = orb.detectAndCompute(cap_gray, None)
    kp2, des2 = orb.detectAndCompute(ref_gray, None)
    if des1 is None or des2 is None:
        return False

    # Matching avec BFMatcher
    bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)
    matches = bf.match(des1, des2)
    if not matches:
        return False
    matches = sorted(matches, key=lambda x: x.distance)
    
    # On considère comme "bonnes" les correspondances dont la distance est inférieure à 60 (valeur ajustable)
    num_good = sum(1 for m in matches if m.distance < 60)
    score = num_good / len(matches)
    return score >= threshold

def find_all_similar_images(captured_image_path, reference_folder, threshold=0.8):
    similar_images = []
    for reference_image in os.listdir(reference_folder):
        if not reference_image.lower().endswith('.png'):
            continue
        reference_image_path = os.path.join(reference_folder, reference_image)
        if is_image_present(captured_image_path, reference_image_path, threshold):
            similar_images.append(reference_image)
    return similar_images

def move_randomly():
    directions = ['w', 'a', 's', 'd']
    direction = random.choice(directions)
    pyautogui.keyDown(direction)
    move_duration = random.uniform(0.2, 5.0)
    time.sleep(move_duration)
    pyautogui.keyUp(direction)

def shoot():
    pyautogui.press('e')

def afk():
    if random.random() < 0.9:
        move_randomly()
    for _ in range(random.randint(1, 3)):
        shoot()
    cycle_delay = random.uniform(0.1, 0.2)
    time.sleep(cycle_delay)

def check_missing_images(reference_folder, required_images):
    missing_images = []
    for image in required_images:
        if not os.path.exists(os.path.join(reference_folder, image)):
            missing_images.append(image)
    return missing_images

def adjust_click_coordinates(x, y, window_coordinates, captured_image_shape):
    left, top, right, bottom = window_coordinates
    window_width = right - left
    window_height = bottom - top
    captured_height, captured_width = captured_image_shape[:2]
    adjusted_x = int(x * (window_width / captured_width)) + left
    adjusted_y = int(y * (window_height / captured_height)) + top
    print(f"Coordonnées ajustées: ({adjusted_x}, {adjusted_y})")
    return adjusted_x, adjusted_y

def find_text_coordinates(image, text):
    data = pytesseract.image_to_data(image, output_type=pytesseract.Output.DICT)
    n_boxes = len(data['level'])
    for i in range(n_boxes):
        if text.lower() in data['text'][i].lower():
            (x, y, w, h) = (data['left'][i], data['top'][i], data['width'][i], data['height'][i])
            return x + w // 2, y + h // 2
    return None, None

def crop_global_trophy_zone(captured_image_path):
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
    cropped_image = crop_image_from_coordinates(captured_image_path, "brawler_trophy")
    if cropped_image is not None:
        target_color = np.array([255, 255, 255])
        tolerance = 50
        diff = np.abs(cropped_image.astype(np.int16) - target_color)
        mask = np.all(diff <= tolerance, axis=-1)
        filtered_image = np.zeros_like(cropped_image)
        filtered_image[mask] = cropped_image[mask]
        cv2.imwrite("brawler_trophy_processed.png", filtered_image)
        text = pytesseract.image_to_string(filtered_image)
        try:
            number = int(''.join(filter(str.isdigit, text)))
            return number
        except ValueError:
            return None
    return None

def send_telegram_message(message):
    bot_token = ''
    chat_id = ''
    url = f'https://api.telegram.org/bot{bot_token}/sendMessage'
    data = {'chat_id': chat_id, 'text': message}
    retries = 3
    delay = 2
    response = None
    for attempt in range(1, retries + 1):
        try:
            response = requests.post(url, data=data, timeout=10)
            if response.status_code == 401:
                update_text("Erreur 401: Non autorisé. Vérifiez le token bot Telegram.")
                break
            if response.ok:
                break
            else:
                update_text(f"⚠️ Erreur Telegram, tentative {attempt}/{retries}: Statut {response.status_code}")
        except requests.exceptions.RequestException as e:
            update_text(f"⚠️ Exception Telegram, tentative {attempt}/{retries}: {str(e)}")
        time.sleep(delay)
    return response

def run_script():
    update_text("Étape 1: Démarrage de run_script")
    status = "none"
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
        update_text(f"Étape 2: Erreur - Images manquantes: {', '.join(missing_images)}")
    else:
        update_text("Étape 2: Toutes les images nécessaires sont présentes.")
    target_width = 944.0
    target_height = 545.0
    
    update_text("Étape 3: Activation de la fenêtre")
    bring_window_to_front(window_name)
    update_text("Étape 4: Redimensionnement de la fenêtre")
    resize_window(window_name, target_width, target_height)
    trophy_zone_coordinates = get_zone_coordinates("global_trophy")
    
    update_text("Étape 5: Attente de la première capture d'image")
    while True:
        update_text("Étape 6: Capture de l'image de la fenêtre")
        captured_image = capture_window_image(window_name)
        if captured_image is None:
            update_text("Étape 6: Échec de capture, réessai dans 1 seconde")
            time.sleep(1)
            continue
        cv2.imwrite(output_path, captured_image)
        update_text("Étape 6: Image enregistrée")
        similar_images = find_all_similar_images(output_path, reference_folder)
        update_text(f"Images détectées: {', '.join(similar_images) if similar_images else 'aucune'}")
        if "play_button.png" in similar_images:
            update_text("Étape 7: Bouton play détecté, tentative d'extraction des trophées")
            temp_global = crop_global_trophy_zone(output_path)
            temp_brawler = crop_brawler_trophy_zone(output_path)
            if temp_global is not None and temp_brawler is not None:
                global_trophy = temp_global
                brawler_trophy = temp_brawler
                if brawler_trophy >= objective:
                    update_text("Étape 8: Objectif déjà atteint au démarrage!")
                    send_telegram_message(f"[BS Bot] 🎉 Objectif déjà atteint au démarrage! Trophés du brawler: {brawler_trophy}")
                    sys.exit(0)
                break
            else:
                update_text("Étape 7: Trophées non détectés, recalibrage de la zone brawler")
                from select_zone import select_zone
                select_zone(output_path, "brawler_trophy")
                trophy_img = crop_image_from_coordinates(output_path, "brawler_trophy")
                if trophy_img is not None:
                    processed_img = cv2.cvtColor(trophy_img, cv2.COLOR_BGR2GRAY)
                    _, processed_img = cv2.threshold(processed_img, 100, 255, cv2.THRESH_BINARY)
                    cv2.imshow("Trophées traités", processed_img)
                    cv2.waitKey(5000)
                    cv2.destroyWindow("Trophées traités")
        time.sleep(1)
    
    last_f_press = 0
    previous_status = ""
    previous_global_trophy = 0
    previous_brawler_trophy = 0
    previous_victory = 0
    previous_defeat = 0
    update_text("Étape 9: Démarrage de la boucle principale")
    while True:
        update_text("Étape 10: Capture de la fenêtre dans la boucle principale")
        captured_image = capture_window_image(window_name)
        if captured_image is None:
            update_text("Étape 10: Échec de capture, réessai dans 1 seconde")
            time.sleep(1)
            continue
        cv2.imwrite(output_path, captured_image)
        similar_images = find_all_similar_images(output_path, reference_folder)
        update_text(f"Images détectées: {', '.join(similar_images) if similar_images else 'aucune'}")
        if "disconnected.png" in similar_images or "connexion_lost.png" in similar_images:
            pyautogui.press('space')
            status = "🔌 disconnected"
        if "play_button.png" in similar_images:
            time.sleep(1)
            new_global = crop_global_trophy_zone(output_path)
            new_brawler = crop_brawler_trophy_zone(output_path)
            if new_brawler is None:
                update_text("❌ Trophées brawler non détectés.")
                new_brawler = crop_brawler_trophy_zone(output_path)
                if new_brawler is None:
                    update_text("❌ Impossibilité d'obtenir le nombre de trophées après mise à jour.")
                    continue
            if new_global is not None:
                if new_global < global_trophy:
                    defeat += 1
                elif new_global > global_trophy:
                    victory += 1
                global_trophy = new_global
            brawler_trophy = new_brawler
            if brawler_trophy >= objective:
                update_text("🎉 Objectif atteint!")
                send_telegram_message(f"[BS Bot] 🎉 Objectif atteint! Trophés du brawler: {brawler_trophy}")
                sys.exit(0)
            status = "🏠 lobby"
        if "play_button.png" in similar_images and ("brawball.png" in similar_images or "brawball1.png" in similar_images):
            status = "🏠 lobby"
            if time.time() - last_f_press > 2:
                pyautogui.press('f')
                last_f_press = time.time()
        if "play_button.png" in similar_images and not ("brawball.png" in similar_images or "brawball1.png" in similar_images):
            pass
        if "other_device.png" in similar_images:
            time.sleep(60)
            pyautogui.press('space')
            status = "📱 other_device"
        if "new_brawler.png" in similar_images:
            pyautogui.press('n')
            time.sleep(1)
            pyautogui.press('space')
            status = "🆕 new_brawler"
        if "prix_star.png" in similar_images:
            for _ in range(7):
                pyautogui.press('space')
                status = "⭐ star_prize"
                time.sleep(1)
        elif "in_game.png" in similar_images:
            afk()
            status = "🎮 in_game"
        elif "continue_button.png" in similar_images or "leave_button.png" in similar_images:
            lobby_detected = False
            while not lobby_detected:
                bring_window_to_front(window_name)
                captured_image = capture_window_image(window_name)
                if captured_image is None:
                    time.sleep(1)
                    continue
                cv2.imwrite(output_path, captured_image)
                lobby_similar = find_all_similar_images(output_path, reference_folder)
                if "play_button.png" in lobby_similar:
                    lobby_detected = True
                else:
                    pyautogui.press('f')
                    time.sleep(5)
            status = "🏠 lobby"
        elif "crashed.png" in similar_images:
            pyautogui.press('b')
            status = "💥 crashed"
        if brawler_trophy is not None and brawler_trophy >= objective:
            update_text("🎉 Objectif atteint!")
            send_telegram_message(f"[BS Bot] 🎉 Objectif atteint! Trophés du brawler: {brawler_trophy}")
            sys.exit(0)
        if status == "🔌 disconnected" or status == "📱 other_device" or status == "💥 crashed":
            send_telegram_message(f"[BS Bot] 🚨 Le script est bloqué. Statut: {status}")
        if (status != previous_status or global_trophy != previous_global_trophy or 
            brawler_trophy != previous_brawler_trophy or victory != previous_victory or 
            defeat != previous_defeat):
            update_text(f"📊 Statut: {status}")
            update_text(f"🏆 Trophés Global: {global_trophy}")
            update_text(f"🏅 Trophés Brawler: {brawler_trophy}")
            update_text(f"✅ Victoires: {victory}")
            update_text(f"❌ Défaites: {defeat}")
            previous_status = status
            previous_global_trophy = global_trophy
            previous_brawler_trophy = brawler_trophy
            previous_victory = victory
            previous_defeat = defeat
        os.remove(output_path)

def update_text(message):
    timestamp = datetime.now().strftime('%H:%M:%S')
    if message.startswith("❌"):
        color = Fore.RED
    elif message.startswith("✅"):
        color = Fore.GREEN
    elif message.startswith("🚀"):
        color = Fore.BLUE
    elif message.startswith("🎉"):
        color = Fore.MAGENTA
    elif message.startswith("🏆") or message.startswith("🏅"):
        color = Fore.YELLOW
    elif message.startswith("📊"):
        color = Fore.CYAN
    else:
        color = Fore.WHITE
    print(f"[{timestamp}] {color}{message}{Style.RESET_ALL}")

def start_script():
    global objective
    try:
        objective = int(input("Entrez votre objectif de trophée: "))
        if objective > 500:
            print("⚠️ Attention : Le script peut ne pas réussir à monter aussi haut en trophée.")
    except ValueError:
        print("❌ Erreur : Veuillez entrer un chiffre valide pour l'objectif de trophée.")
        sys.exit(1)
    bring_window_to_front(window_name)
    update_text("🚀 Démarrage du script...")
    run_script()

if __name__ == '__main__':
    start_script()
