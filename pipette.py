import cv2
import numpy as np

# Charger l'image capturée
image = cv2.imread("captured_window.png")
if image is None:
    print("Image 'captured_window.png' introuvable.")
    exit(1)

def mouse_callback(event, x, y, flags, param):
    if event == cv2.EVENT_LBUTTONDOWN:
        pixel = image[y, x]  # Valeur en BGR
        print(f"Position: ({x}, {y}) - Couleur BGR: {pixel}")
        hsv_pixel = cv2.cvtColor(np.uint8([[pixel]]), cv2.COLOR_BGR2HSV)
        print(f"Couleur HSV: {hsv_pixel[0][0]}")

cv2.namedWindow("Image")
cv2.setMouseCallback("Image", mouse_callback)

# Affichage de l'image et attente de l'appui sur la touche Échap (ESC)
while True:
    cv2.imshow("Image", image)
    if cv2.waitKey(1) & 0xFF == 27:
        break

cv2.destroyAllWindows()
